from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.models import ArticleCandidate, DedupeResult, PostJob
from app.normalizer import isoformat, source_family, stable_hash, title_signature

logger = logging.getLogger(__name__)


def _json_dumps(value: dict[str, Any] | None) -> str:
    if not value:
        return "{}"
    try:
        return json.dumps(value, sort_keys=True)
    except TypeError:
        return "{}"


def _json_dict(value: object) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


SCHEMA = """
PRAGMA journal_mode = DELETE;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_key TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    normalized_title TEXT,
    title_signature TEXT,
    source_family TEXT,
    source_id TEXT,
    source_class TEXT,
    story_cluster_key TEXT,
    url TEXT,
    normalized_url TEXT,
    summary TEXT,
    rich_metadata TEXT,
    image_url TEXT,
    image_source TEXT,
    source_name TEXT,
    raw_published_at TEXT,
    normalized_published_at TEXT,
    ingested_at TEXT NOT NULL,
    timestamp_status TEXT NOT NULL,
    first_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS article_fingerprints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    fingerprint_type TEXT NOT NULL,
    fingerprint_value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(fingerprint_type, fingerprint_value),
    FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS feed_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_key TEXT NOT NULL,
    article_id INTEGER NOT NULL,
    raw_guid TEXT,
    raw_url TEXT,
    raw_title TEXT,
    seen_at TEXT NOT NULL,
    entry_key TEXT NOT NULL,
    UNIQUE(feed_key, entry_key),
    FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS feed_entry_seen (
    feed_key TEXT NOT NULL,
    entry_key TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY(feed_key, entry_key)
);

CREATE TABLE IF NOT EXISTS channel_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    channel_id TEXT NOT NULL,
    discord_message_id TEXT,
    posted_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'posted',
    UNIQUE(article_id, channel_id),
    FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS channel_seen_titles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    article_id INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE(channel_id, normalized_title),
    FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS channel_seen_title_signatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    title_signature TEXT NOT NULL,
    article_id INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE(channel_id, title_signature),
    FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS channel_seen_source_titles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    article_id INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE(channel_id, source_id, normalized_title),
    FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS channel_seen_source_title_signatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title_signature TEXT NOT NULL,
    article_id INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE(channel_id, source_id, title_signature),
    FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS channel_story_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id TEXT NOT NULL,
    story_cluster_key TEXT NOT NULL,
    source_id TEXT NOT NULL,
    article_id INTEGER NOT NULL,
    first_seen_at TEXT NOT NULL,
    status TEXT NOT NULL,
    UNIQUE(channel_id, story_cluster_key, source_id),
    FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS feed_status (
    feed_key TEXT PRIMARY KEY,
    feed_name TEXT,
    feed_url TEXT NOT NULL,
    last_attempt_at TEXT,
    last_success_at TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    last_error TEXT,
    next_poll_at TEXT
);

CREATE TABLE IF NOT EXISTS email_source_cursors (
    source_key TEXT PRIMARY KEY,
    mailbox TEXT NOT NULL,
    last_uid TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS article_routing_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    content_mode TEXT NOT NULL,
    selected_channel_keys TEXT NOT NULL,
    selected_channel_ids TEXT NOT NULL,
    decision_status TEXT NOT NULL,
    top_score INTEGER NOT NULL,
    score_details TEXT NOT NULL,
    matched_entries TEXT NOT NULL,
    emitted_tags TEXT NOT NULL,
    expanded_tags TEXT NOT NULL,
    explanation TEXT NOT NULL,
    primary_channel_keys TEXT,
    mirror_channel_keys TEXT,
    review_channel_keys TEXT,
    final_channel_keys TEXT,
    reason TEXT,
    FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS article_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(article_id, tag, source),
    FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE TABLE IF NOT EXISTS article_matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id INTEGER NOT NULL,
    knowledge_entry_id TEXT NOT NULL,
    matched_alias TEXT NOT NULL,
    match_start INTEGER,
    match_end INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(article_id, knowledge_entry_id, matched_alias, match_start, match_end),
    FOREIGN KEY(article_id) REFERENCES articles(id)
);

CREATE INDEX IF NOT EXISTS idx_articles_normalized_url ON articles(normalized_url);
CREATE INDEX IF NOT EXISTS idx_articles_title_source_time ON articles(normalized_title, source_name, normalized_published_at);
CREATE INDEX IF NOT EXISTS idx_feed_status_next_poll ON feed_status(next_poll_at);
CREATE INDEX IF NOT EXISTS idx_feed_entry_seen_seen_at ON feed_entry_seen(seen_at);
CREATE INDEX IF NOT EXISTS idx_channel_posts_channel ON channel_posts(channel_id, article_id);
CREATE INDEX IF NOT EXISTS idx_channel_seen_titles_channel ON channel_seen_titles(channel_id, normalized_title);
CREATE INDEX IF NOT EXISTS idx_channel_seen_title_signatures_channel ON channel_seen_title_signatures(channel_id, title_signature);
CREATE INDEX IF NOT EXISTS idx_channel_seen_source_titles_channel ON channel_seen_source_titles(channel_id, source_id, normalized_title);
CREATE INDEX IF NOT EXISTS idx_channel_seen_source_title_signatures_channel ON channel_seen_source_title_signatures(channel_id, source_id, title_signature);
CREATE INDEX IF NOT EXISTS idx_channel_story_sources_cluster ON channel_story_sources(channel_id, story_cluster_key, source_id);
CREATE INDEX IF NOT EXISTS idx_article_routing_decisions_article ON article_routing_decisions(article_id, created_at);
CREATE INDEX IF NOT EXISTS idx_article_routing_decisions_status ON article_routing_decisions(decision_status, created_at);
CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags(tag, article_id);
CREATE INDEX IF NOT EXISTS idx_article_matches_entry ON article_matches(knowledge_entry_id, article_id);
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout = 30000")
        self._conn.execute("PRAGMA foreign_keys = ON")

    def initialize(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()
            self._checkpoint_wal_if_enabled()

    def _migrate(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(channel_posts)").fetchall()
        }
        if "status" not in columns:
            self._conn.execute("ALTER TABLE channel_posts ADD COLUMN status TEXT NOT NULL DEFAULT 'posted'")
        article_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(articles)").fetchall()
        }
        if "title_signature" not in article_columns:
            self._conn.execute("ALTER TABLE articles ADD COLUMN title_signature TEXT")
        if "source_family" not in article_columns:
            self._conn.execute("ALTER TABLE articles ADD COLUMN source_family TEXT")
        if "source_id" not in article_columns:
            self._conn.execute("ALTER TABLE articles ADD COLUMN source_id TEXT")
        if "source_class" not in article_columns:
            self._conn.execute("ALTER TABLE articles ADD COLUMN source_class TEXT")
        if "story_cluster_key" not in article_columns:
            self._conn.execute("ALTER TABLE articles ADD COLUMN story_cluster_key TEXT")
        if "summary" not in article_columns:
            self._conn.execute("ALTER TABLE articles ADD COLUMN summary TEXT")
        if "rich_metadata" not in article_columns:
            self._conn.execute("ALTER TABLE articles ADD COLUMN rich_metadata TEXT")
        if "image_url" not in article_columns:
            self._conn.execute("ALTER TABLE articles ADD COLUMN image_url TEXT")
        if "image_source" not in article_columns:
            self._conn.execute("ALTER TABLE articles ADD COLUMN image_source TEXT")
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS email_source_cursors (
                source_key TEXT PRIMARY KEY,
                mailbox TEXT NOT NULL,
                last_uid TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_articles_title_signature_time
            ON articles(title_signature, source_family, normalized_published_at)
            """
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO feed_entry_seen (feed_key, entry_key, seen_at)
            SELECT feed_key, entry_key, seen_at
            FROM feed_entries
            """
        )
        rows = self._conn.execute(
            """
            SELECT id, title, source_name
            FROM articles
            WHERE title_signature IS NULL OR title_signature = ''
               OR source_family IS NULL OR source_family = ''
               OR source_id IS NULL OR source_id = ''
               OR source_class IS NULL OR source_class = ''
               OR story_cluster_key IS NULL OR story_cluster_key = ''
            """
        ).fetchall()
        for row in rows:
            signature = title_signature(row["title"])
            family = source_family(row["source_name"])
            self._conn.execute(
                """
                UPDATE articles
                SET title_signature = ?, source_family = ?, source_id = coalesce(nullif(source_id, ''), ?),
                    source_class = coalesce(nullif(source_class, ''), 'unknown'),
                    story_cluster_key = coalesce(nullif(story_cluster_key, ''), ?)
                WHERE id = ?
                """,
                (signature, family, family or "unknown", stable_hash(signature or row["title"], 24), row["id"]),
            )
        routing_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(article_routing_decisions)").fetchall()
        }
        for column in (
            "primary_channel_keys",
            "mirror_channel_keys",
            "review_channel_keys",
            "final_channel_keys",
            "reason",
        ):
            if column not in routing_columns:
                self._conn.execute(f"ALTER TABLE article_routing_decisions ADD COLUMN {column} TEXT")
        self._conn.execute(
            """
            INSERT OR IGNORE INTO channel_seen_titles (
                channel_id, normalized_title, article_id, first_seen_at, status
            )
            SELECT cp.channel_id, a.normalized_title, a.id, cp.posted_at, cp.status
            FROM channel_posts cp
            JOIN articles a ON a.id = cp.article_id
            WHERE a.normalized_title IS NOT NULL AND a.normalized_title != ''
            """
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO channel_seen_title_signatures (
                channel_id, title_signature, article_id, first_seen_at, status
            )
            SELECT cp.channel_id, a.title_signature, a.id, cp.posted_at, cp.status
            FROM channel_posts cp
            JOIN articles a ON a.id = cp.article_id
            WHERE a.title_signature IS NOT NULL AND a.title_signature != ''
            """
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO channel_seen_source_titles (
                channel_id, source_id, normalized_title, article_id, first_seen_at, status
            )
            SELECT cp.channel_id, coalesce(nullif(a.source_id, ''), 'unknown'), a.normalized_title, a.id,
                   cp.posted_at, cp.status
            FROM channel_posts cp
            JOIN articles a ON a.id = cp.article_id
            WHERE a.normalized_title IS NOT NULL AND a.normalized_title != ''
            """
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO channel_seen_source_title_signatures (
                channel_id, source_id, title_signature, article_id, first_seen_at, status
            )
            SELECT cp.channel_id, coalesce(nullif(a.source_id, ''), 'unknown'), a.title_signature, a.id,
                   cp.posted_at, cp.status
            FROM channel_posts cp
            JOIN articles a ON a.id = cp.article_id
            WHERE a.title_signature IS NOT NULL AND a.title_signature != ''
            """
        )
        self._conn.execute(
            """
            INSERT OR IGNORE INTO channel_story_sources (
                channel_id, story_cluster_key, source_id, article_id, first_seen_at, status
            )
            SELECT cp.channel_id, coalesce(nullif(a.story_cluster_key, ''), a.title_signature, a.normalized_title),
                   coalesce(nullif(a.source_id, ''), 'unknown'), a.id, cp.posted_at, cp.status
            FROM channel_posts cp
            JOIN articles a ON a.id = cp.article_id
            WHERE coalesce(nullif(a.story_cluster_key, ''), a.title_signature, a.normalized_title) IS NOT NULL
            """
        )

    def close(self) -> None:
        with self._lock:
            self._checkpoint_wal_if_enabled(ignore_errors=True)
            self._conn.close()

    def _checkpoint_wal_if_enabled(self, *, ignore_errors: bool = False) -> None:
        try:
            row = self._conn.execute("PRAGMA journal_mode").fetchone()
            mode = str(row[0]).lower() if row else ""
            if mode == "wal":
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error as exc:
            if not ignore_errors:
                logger.warning("SQLite WAL checkpoint failed: %s", exc)

    def database_size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def resolve_article(self, candidate: ArticleCandidate, title_window_hours: int) -> DedupeResult:
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                article_id = self._find_article_id(cursor, candidate, title_window_hours)
                is_new = article_id is None
                if article_id is None:
                    article_id = self._insert_article(cursor, candidate)
                self._insert_fingerprints(cursor, article_id, candidate)
                self._insert_feed_entry(cursor, article_id, candidate)
                self._conn.commit()
                return DedupeResult(article_id=article_id, is_new_article=is_new)
            except Exception:
                self._conn.rollback()
                raise

    def _find_article_id(
        self, cursor: sqlite3.Cursor, candidate: ArticleCandidate, title_window_hours: int
    ) -> int | None:
        for fingerprint_type, fingerprint_value in candidate.fingerprints:
            row = cursor.execute(
                "SELECT article_id FROM article_fingerprints WHERE fingerprint_type = ? AND fingerprint_value = ?",
                (fingerprint_type, fingerprint_value),
            ).fetchone()
            if row:
                return int(row["article_id"])

        if candidate.normalized_url:
            row = cursor.execute(
                "SELECT id FROM articles WHERE normalized_url = ? ORDER BY id ASC LIMIT 1",
                (candidate.normalized_url,),
            ).fetchone()
            if row:
                return int(row["id"])

        if candidate.normalized_title:
            cutoff = datetime.now(UTC) - timedelta(hours=title_window_hours)
            row = cursor.execute(
                """
                SELECT id FROM articles
                WHERE normalized_title = ?
                  AND source_id = ?
                  AND normalized_published_at >= ?
                ORDER BY id ASC LIMIT 1
                """,
                (candidate.normalized_title, candidate.source_id, cutoff.isoformat()),
            ).fetchone()
            if row:
                return int(row["id"])
        if candidate.title_signature and candidate.source_id:
            cutoff = datetime.now(UTC) - timedelta(hours=title_window_hours)
            row = cursor.execute(
                """
                SELECT id FROM articles
                WHERE title_signature = ?
                  AND source_id = ?
                  AND normalized_published_at >= ?
                ORDER BY id ASC LIMIT 1
                """,
                (candidate.title_signature, candidate.source_id, cutoff.isoformat()),
            ).fetchone()
            if row:
                return int(row["id"])
        return None

    def _insert_article(self, cursor: sqlite3.Cursor, candidate: ArticleCandidate) -> int:
        seed = candidate.normalized_url or candidate.raw_guid or f"{candidate.source_name}:{candidate.normalized_title}"
        canonical_key = stable_hash(seed, 32)
        now = datetime.now(UTC).isoformat()
        cursor.execute(
            """
            INSERT INTO articles (
                canonical_key, title, normalized_title, title_signature, source_family, source_id, source_class,
                story_cluster_key, url, normalized_url, summary, rich_metadata,
                image_url, image_source, source_name,
                raw_published_at, normalized_published_at, ingested_at, timestamp_status, first_seen_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canonical_key,
                candidate.title,
                candidate.normalized_title,
                candidate.title_signature,
                candidate.source_family,
                candidate.source_id,
                candidate.source_class,
                candidate.story_cluster_key,
                candidate.url,
                candidate.normalized_url,
                candidate.summary,
                _json_dumps(candidate.rich_metadata),
                candidate.image_url,
                candidate.image_source,
                candidate.source_name,
                candidate.raw_published_at,
                isoformat(candidate.normalized_published_at),
                isoformat(candidate.ingested_at),
                candidate.timestamp_status,
                now,
            ),
        )
        return int(cursor.lastrowid)

    def _insert_fingerprints(self, cursor: sqlite3.Cursor, article_id: int, candidate: ArticleCandidate) -> None:
        now = datetime.now(UTC).isoformat()
        for fingerprint_type, fingerprint_value in candidate.fingerprints:
            cursor.execute(
                """
                INSERT OR IGNORE INTO article_fingerprints (
                    article_id, fingerprint_type, fingerprint_value, created_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (article_id, fingerprint_type, fingerprint_value, now),
            )

    def _insert_feed_entry(self, cursor: sqlite3.Cursor, article_id: int, candidate: ArticleCandidate) -> None:
        entry_key = self._feed_entry_key(candidate)
        cursor.execute(
            """
            INSERT OR IGNORE INTO feed_entries (
                feed_key, article_id, raw_guid, raw_url, raw_title, seen_at, entry_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.feed_key,
                article_id,
                candidate.raw_guid,
                candidate.url,
                candidate.title,
                datetime.now(UTC).isoformat(),
                entry_key,
            ),
        )
        cursor.execute(
            """
            INSERT OR IGNORE INTO feed_entry_seen (feed_key, entry_key, seen_at)
            VALUES (?, ?, ?)
            """,
            (candidate.feed_key, entry_key, datetime.now(UTC).isoformat()),
        )

    def _feed_entry_key(self, candidate: ArticleCandidate) -> str:
        entry_seed = candidate.raw_guid or candidate.normalized_url or candidate.normalized_title
        return stable_hash(entry_seed or f"{candidate.feed_key}:{candidate.title}", 32)

    def has_feed_entry_seen(self, candidate: ArticleCandidate) -> bool:
        entry_key = self._feed_entry_key(candidate)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1
                FROM feed_entry_seen
                WHERE feed_key = ? AND entry_key = ?
                LIMIT 1
                """,
                (candidate.feed_key, entry_key),
            ).fetchone()
            return row is not None

    def record_feed_entry_seen(self, candidate: ArticleCandidate) -> bool:
        entry_key = self._feed_entry_key(candidate)
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO feed_entry_seen (feed_key, entry_key, seen_at)
                VALUES (?, ?, ?)
                """,
                (candidate.feed_key, entry_key, datetime.now(UTC).isoformat()),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def has_channel_post(self, article_id: int, channel_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM channel_posts WHERE article_id = ? AND channel_id = ?",
                (article_id, channel_id),
            ).fetchone()
            return row is not None

    def has_channel_title(self, channel_id: str, normalized_title: str | None, source_id: str = "unknown") -> bool:
        if not normalized_title:
            return False
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM channel_seen_source_titles
                WHERE channel_id = ? AND source_id = ? AND normalized_title = ?
                """,
                (channel_id, source_id or "unknown", normalized_title),
            ).fetchone()
            return row is not None

    def has_channel_title_signature(
        self,
        channel_id: str,
        title_signature: str | None,
        source_id: str = "unknown",
    ) -> bool:
        if not title_signature:
            return False
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM channel_seen_source_title_signatures
                WHERE channel_id = ? AND source_id = ? AND title_signature = ?
                """,
                (channel_id, source_id or "unknown", title_signature),
            ).fetchone()
            return row is not None

    def channel_story_source_count(self, channel_id: str, story_cluster_key: str | None) -> int:
        if not story_cluster_key:
            return 0
        with self._lock:
            row = self._conn.execute(
                """
                SELECT count(DISTINCT source_id)
                FROM channel_story_sources
                WHERE channel_id = ? AND story_cluster_key = ?
                """,
                (channel_id, story_cluster_key),
            ).fetchone()
            return int(row[0] or 0)

    def has_channel_story_source(self, channel_id: str, story_cluster_key: str | None, source_id: str) -> bool:
        if not story_cluster_key:
            return False
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1 FROM channel_story_sources
                WHERE channel_id = ? AND story_cluster_key = ? AND source_id = ?
                """,
                (channel_id, story_cluster_key, source_id or "unknown"),
            ).fetchone()
            return row is not None

    def reserve_channel_title(
        self,
        article_id: int,
        channel_id: str,
        normalized_title: str | None,
        title_signature: str | None,
        status: str,
        source_id: str = "unknown",
        story_cluster_key: str | None = None,
    ) -> bool:
        with self._lock:
            inserted_title = 1
            inserted_signature = 1
            inserted_cluster = 1
            now = datetime.now(UTC).isoformat()
            if normalized_title:
                cursor = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO channel_seen_source_titles (
                        channel_id, source_id, normalized_title, article_id, first_seen_at, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (channel_id, source_id or "unknown", normalized_title, article_id, now, status),
                )
                inserted_title = cursor.rowcount
            if title_signature:
                cursor = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO channel_seen_source_title_signatures (
                        channel_id, source_id, title_signature, article_id, first_seen_at, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (channel_id, source_id or "unknown", title_signature, article_id, now, status),
                )
                inserted_signature = cursor.rowcount
            if story_cluster_key:
                cursor = self._conn.execute(
                    """
                    INSERT OR IGNORE INTO channel_story_sources (
                        channel_id, story_cluster_key, source_id, article_id, first_seen_at, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (channel_id, story_cluster_key, source_id or "unknown", article_id, now, status),
                )
                inserted_cluster = cursor.rowcount
            self._conn.commit()
            return inserted_title == 1 and inserted_signature == 1 and inserted_cluster == 1

    def record_channel_skipped(
        self,
        article_id: int,
        channel_id: str,
        normalized_title: str | None,
        title_signature: str | None,
        status: str,
        source_id: str = "unknown",
        story_cluster_key: str | None = None,
        reserve_seen: bool = True,
    ) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO channel_posts (article_id, channel_id, discord_message_id, posted_at, status)
                VALUES (?, ?, NULL, ?, ?)
                """,
                (article_id, channel_id, datetime.now(UTC).isoformat(), status),
            )
            if reserve_seen:
                self.reserve_channel_title(
                    article_id,
                    channel_id,
                    normalized_title,
                    title_signature,
                    status,
                    source_id,
                    story_cluster_key,
                )
            self._conn.commit()
            return cursor.rowcount == 1

    def record_channel_post(self, article_id: int, channel_id: str, message_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO channel_posts (article_id, channel_id, discord_message_id, posted_at, status)
                VALUES (?, ?, ?, ?, 'posted')
                """,
                (article_id, channel_id, message_id, datetime.now(UTC).isoformat()),
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def record_channel_suppressed(
        self,
        article_id: int,
        channel_id: str,
        normalized_title: str | None = None,
        title_signature: str | None = None,
        source_id: str = "unknown",
        story_cluster_key: str | None = None,
        status: str = "suppressed_first_run",
    ) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT OR IGNORE INTO channel_posts (article_id, channel_id, discord_message_id, posted_at, status)
                VALUES (?, ?, NULL, ?, ?)
                """,
                (article_id, channel_id, datetime.now(UTC).isoformat(), status),
            )
            self.reserve_channel_title(
                article_id,
                channel_id,
                normalized_title,
                title_signature,
                status,
                source_id,
                story_cluster_key,
            )
            self._conn.commit()
            return cursor.rowcount == 1

    def mark_feed_attempt(self, feed_key: str, feed_name: str, feed_url: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO feed_status (feed_key, feed_name, feed_url, last_attempt_at, consecutive_failures)
                VALUES (?, ?, ?, ?, 0)
                ON CONFLICT(feed_key) DO UPDATE SET
                    feed_name = excluded.feed_name,
                    feed_url = excluded.feed_url,
                    last_attempt_at = excluded.last_attempt_at
                """,
                (feed_key, feed_name, feed_url, now),
            )
            self._conn.commit()

    def mark_feed_success(
        self,
        feed_key: str,
        feed_name: str,
        feed_url: str,
        next_poll_at: datetime | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO feed_status (
                    feed_key, feed_name, feed_url, last_attempt_at, last_success_at,
                    consecutive_failures, last_error, next_poll_at
                )
                VALUES (?, ?, ?, ?, ?, 0, NULL, ?)
                ON CONFLICT(feed_key) DO UPDATE SET
                    feed_name = excluded.feed_name,
                    feed_url = excluded.feed_url,
                    last_attempt_at = excluded.last_attempt_at,
                    last_success_at = excluded.last_success_at,
                    consecutive_failures = 0,
                    last_error = NULL,
                    next_poll_at = excluded.next_poll_at
                """,
                (feed_key, feed_name, feed_url, now, now, isoformat(next_poll_at) if next_poll_at else None),
            )
            self._conn.commit()

    def mark_feed_failure(
        self,
        feed_key: str,
        feed_name: str,
        feed_url: str,
        error: str,
        next_poll_at: datetime | None = None,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO feed_status (
                    feed_key, feed_name, feed_url, last_attempt_at, consecutive_failures,
                    last_error, next_poll_at
                )
                VALUES (?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(feed_key) DO UPDATE SET
                    feed_name = excluded.feed_name,
                    feed_url = excluded.feed_url,
                    last_attempt_at = excluded.last_attempt_at,
                    consecutive_failures = feed_status.consecutive_failures + 1,
                    last_error = excluded.last_error,
                    next_poll_at = excluded.next_poll_at
                """,
                (
                    feed_key,
                    feed_name,
                    feed_url,
                    now,
                    error[:1000],
                    isoformat(next_poll_at) if next_poll_at else None,
                ),
            )
            self._conn.commit()

    def is_first_feed_success(self, feed_key: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_success_at FROM feed_status WHERE feed_key = ?",
                (feed_key,),
            ).fetchone()
            return row is None or row["last_success_at"] is None

    def feed_consecutive_failures(self, feed_key: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT consecutive_failures FROM feed_status WHERE feed_key = ?",
                (feed_key,),
            ).fetchone()
            if row is None:
                return 0
            return int(row["consecutive_failures"] or 0)

    def email_cursor_uid(self, source_key: str, mailbox: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT last_uid
                FROM email_source_cursors
                WHERE source_key = ? AND mailbox = ?
                """,
                (source_key, mailbox),
            ).fetchone()
            return str(row["last_uid"]) if row and row["last_uid"] else None

    def update_email_cursor(self, source_key: str, mailbox: str, last_uid: str | None) -> None:
        if not last_uid:
            return
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO email_source_cursors (source_key, mailbox, last_uid, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    mailbox = excluded.mailbox,
                    last_uid = excluded.last_uid,
                    updated_at = excluded.updated_at
                """,
                (source_key, mailbox, last_uid, datetime.now(UTC).isoformat()),
            )
            self._conn.commit()

    def feed_status_rows(self, limit: int = 10, failures_first: bool = False) -> list[sqlite3.Row]:
        order_by = (
            "consecutive_failures DESC, coalesce(last_attempt_at, '') DESC"
            if failures_first
            else "coalesce(last_attempt_at, '') DESC"
        )
        with self._lock:
            return list(
                self._conn.execute(
                    f"""
                    SELECT feed_key, feed_name, feed_url, last_attempt_at, last_success_at,
                           consecutive_failures, last_error, next_poll_at
                    FROM feed_status
                    ORDER BY {order_by}
                    LIMIT ?
                    """,
                    (limit,),
                )
            )

    def feed_health_report_rows(self, min_failures: int = 10, limit: int = 50) -> list[sqlite3.Row]:
        min_failures = max(0, min_failures)
        limit = max(1, min(limit, 500))
        with self._lock:
            return list(
                self._conn.execute(
                    """
                    SELECT feed_key, feed_name, feed_url, last_attempt_at, last_success_at,
                           consecutive_failures, last_error, next_poll_at
                    FROM feed_status
                    WHERE consecutive_failures >= ?
                    ORDER BY consecutive_failures DESC, coalesce(last_attempt_at, '') DESC
                    LIMIT ?
                    """,
                    (min_failures, limit),
                )
            )

    def prune_inactive_feed_status(self, active_feed_keys: set[str] | frozenset[str]) -> int:
        if not active_feed_keys:
            return 0
        placeholders = ", ".join("?" for _ in active_feed_keys)
        with self._lock:
            cursor = self._conn.execute(
                f"DELETE FROM feed_status WHERE feed_key NOT IN ({placeholders})",
                tuple(sorted(active_feed_keys)),
            )
            self._conn.commit()
            return int(cursor.rowcount)

    def feed_health_summary(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    count(*) AS tracked,
                    sum(CASE WHEN consecutive_failures = 0 AND last_success_at IS NOT NULL THEN 1 ELSE 0 END) AS healthy,
                    sum(CASE WHEN consecutive_failures > 0 THEN 1 ELSE 0 END) AS failing,
                    sum(CASE WHEN last_success_at IS NULL THEN 1 ELSE 0 END) AS never_succeeded
                FROM feed_status
                """
            ).fetchone()
            return {
                "tracked": int(row["tracked"] or 0),
                "healthy": int(row["healthy"] or 0),
                "failing": int(row["failing"] or 0),
                "never_succeeded": int(row["never_succeeded"] or 0),
            }

    def recent_post_count(self, hours: int = 24) -> int:
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT count(*)
                FROM channel_posts
                WHERE status = 'posted' AND posted_at >= ?
                """,
                (cutoff.isoformat(),),
            ).fetchone()
            return int(row[0])

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {
                "articles": int(self._conn.execute("SELECT count(*) FROM articles").fetchone()[0]),
                "channel_posts": int(self._conn.execute("SELECT count(*) FROM channel_posts").fetchone()[0]),
                "feeds": int(self._conn.execute("SELECT count(*) FROM feed_status").fetchone()[0]),
            }

    def prune_runtime_history(
        self,
        *,
        article_retention_days: int = 30,
        posted_retention_days: int = 30,
        non_post_retention_hours: int = 24,
        seen_retention_days: int = 14,
        feed_entry_seen_retention_days: int = 90,
        article_batch_size: int = 500,
    ) -> dict[str, int]:
        now = datetime.now(UTC)
        article_cutoff = (now - timedelta(days=article_retention_days)).isoformat()
        posted_cutoff = (now - timedelta(days=posted_retention_days)).isoformat()
        non_post_cutoff = (now - timedelta(hours=non_post_retention_hours)).isoformat()
        seen_cutoff = (now - timedelta(days=seen_retention_days)).isoformat()
        feed_seen_cutoff = (now - timedelta(days=feed_entry_seen_retention_days)).isoformat()
        stats: dict[str, int] = {}
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                cursor.execute("DROP TABLE IF EXISTS temp.prune_article_ids")
                stats["non_post_channel_posts"] = cursor.execute(
                    """
                    DELETE FROM channel_posts
                    WHERE status != 'posted' AND posted_at < ?
                    """,
                    (non_post_cutoff,),
                ).rowcount
                stats["old_post_channel_posts"] = cursor.execute(
                    """
                    DELETE FROM channel_posts
                    WHERE status = 'posted' AND posted_at < ?
                    """,
                    (posted_cutoff,),
                ).rowcount
                for table in (
                    "channel_seen_titles",
                    "channel_seen_title_signatures",
                    "channel_seen_source_titles",
                    "channel_seen_source_title_signatures",
                    "channel_story_sources",
                ):
                    stats[table] = cursor.execute(
                        f"DELETE FROM {table} WHERE first_seen_at < ?",
                        (seen_cutoff,),
                    ).rowcount
                cursor.execute(
                    """
                    CREATE TEMP TABLE prune_article_ids AS
                    SELECT id
                    FROM articles
                    WHERE coalesce(normalized_published_at, first_seen_at) < ?
                       AND id NOT IN (
                           SELECT article_id
                           FROM channel_posts
                           WHERE status = 'posted' AND posted_at >= ?
                       )
                    LIMIT ?
                    """,
                    (article_cutoff, posted_cutoff, article_batch_size),
                )
                old_article_count = int(cursor.execute("SELECT count(*) FROM prune_article_ids").fetchone()[0])
                stats["old_articles"] = old_article_count
                if old_article_count:
                    for table in (
                        "article_routing_decisions",
                        "article_tags",
                        "article_matches",
                        "article_fingerprints",
                        "feed_entries",
                        "channel_posts",
                        "channel_seen_titles",
                        "channel_seen_title_signatures",
                        "channel_seen_source_titles",
                        "channel_seen_source_title_signatures",
                        "channel_story_sources",
                    ):
                        stats[f"{table}_by_article"] = cursor.execute(
                            f"DELETE FROM {table} WHERE article_id IN (SELECT id FROM prune_article_ids)"
                        ).rowcount
                    stats["articles_deleted"] = cursor.execute(
                        "DELETE FROM articles WHERE id IN (SELECT id FROM prune_article_ids)"
                    ).rowcount
                cursor.execute("DROP TABLE IF EXISTS temp.prune_article_ids")
                stats["old_feed_entry_seen"] = cursor.execute(
                    "DELETE FROM feed_entry_seen WHERE seen_at < ?",
                    (feed_seen_cutoff,),
                ).rowcount
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return stats

    def optimize(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA optimize")

    def vacuum(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.execute("VACUUM")

    def record_routing_decision(
        self,
        article_id: int,
        decision: Any,
        selected_channel_ids: list[str] | tuple[str, ...],
    ) -> None:
        data = decision.to_json_dict()
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO article_routing_decisions (
                    article_id, created_at, content_mode, selected_channel_keys, selected_channel_ids,
                    decision_status, top_score, score_details, matched_entries, emitted_tags,
                    expanded_tags, explanation, primary_channel_keys, mirror_channel_keys,
                    review_channel_keys, final_channel_keys, reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    article_id,
                    now,
                    decision.content_mode,
                    json.dumps(list(decision.final_channel_keys), sort_keys=True),
                    json.dumps(list(selected_channel_ids), sort_keys=True),
                    decision.decision_status,
                    int(decision.top_score),
                    json.dumps(data["channel_scores"], sort_keys=True),
                    json.dumps(data["matched_entries"], sort_keys=True),
                    json.dumps(data["emitted_tags"], sort_keys=True),
                    json.dumps(data["expanded_tags"], sort_keys=True),
                    json.dumps(data["explanation"], sort_keys=True),
                    json.dumps(data["primary_channel_keys"], sort_keys=True),
                    json.dumps(data["mirror_channel_keys"], sort_keys=True),
                    json.dumps(data["review_channel_keys"], sort_keys=True),
                    json.dumps(data["final_channel_keys"], sort_keys=True),
                    data.get("reason"),
                ),
            )
            for tag in decision.emitted_tags:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO article_tags (article_id, tag, source, created_at)
                    VALUES (?, ?, 'emitted', ?)
                    """,
                    (article_id, tag, now),
                )
            emitted = set(decision.emitted_tags)
            for tag in decision.expanded_tags:
                if tag in emitted:
                    continue
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO article_tags (article_id, tag, source, created_at)
                    VALUES (?, ?, 'taxonomy_expanded', ?)
                    """,
                    (article_id, tag, now),
                )
            for match in decision.matched_entries:
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO article_matches (
                        article_id, knowledge_entry_id, matched_alias, match_start, match_end, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        article_id,
                        match.knowledge_entry_id,
                        match.matched_alias,
                        match.match_start,
                        match.match_end,
                        now,
                    ),
                )
            self._conn.commit()

    def has_routing_decision(self, article_id: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT 1
                FROM article_routing_decisions
                WHERE article_id = ?
                LIMIT 1
                """,
                (article_id,),
            ).fetchone()
            return row is not None

    def get_article_for_routing(self, article_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                """
                SELECT id, title, normalized_title, url, source_name, source_id, source_class, summary
                FROM articles
                WHERE id = ?
                """,
                (article_id,),
            ).fetchone()

    def recent_articles_for_routing(self, limit: int, days: int | None = None) -> list[sqlite3.Row]:
        with self._lock:
            params: list[object] = []
            where = ""
            if days is not None:
                cutoff = datetime.now(UTC) - timedelta(days=days)
                where = "WHERE coalesce(normalized_published_at, first_seen_at) >= ?"
                params.append(cutoff.isoformat())
            params.append(limit)
            return list(
                self._conn.execute(
                    f"""
                    SELECT id, title, normalized_title, url, source_name
                           , source_id, source_class, summary
                    FROM articles
                    {where}
                    ORDER BY coalesce(normalized_published_at, first_seen_at) DESC, id DESC
                    LIMIT ?
                    """,
                    tuple(params),
                )
            )

    def recent_routing_error_count(self, hours: int = 24) -> int:
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        with self._lock:
            row = self._conn.execute(
                """
                SELECT count(*)
                FROM article_routing_decisions
                WHERE decision_status = 'error' AND created_at >= ?
                """,
                (cutoff.isoformat(),),
            ).fetchone()
            return int(row[0])

    def get_post_job(self, article_id: int, channel_id: str) -> PostJob:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT id, title, url, summary, rich_metadata, image_url, image_source, source_name, source_id, source_class,
                       normalized_published_at, timestamp_status
                FROM articles WHERE id = ?
                """,
                (article_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Article not found: {article_id}")
            return PostJob(
                article_id=int(row["id"]),
                channel_id=channel_id,
                title=row["title"],
                url=row["url"],
                summary=row["summary"],
                image_url=row["image_url"],
                image_source=row["image_source"],
                source_name=row["source_name"] or "RSS",
                source_id=row["source_id"] or "unknown",
                source_class=row["source_class"] or "unknown",
                rich_metadata=_json_dict(row["rich_metadata"]),
                normalized_published_at=datetime.fromisoformat(row["normalized_published_at"]),
                timestamp_status=row["timestamp_status"] or "valid",
            )

    def latest_routing_decision_for_article(self, article_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(
                """
                SELECT selected_channel_keys, decision_status, top_score, score_details,
                       matched_entries, emitted_tags, expanded_tags, explanation, created_at,
                       primary_channel_keys, mirror_channel_keys, review_channel_keys, final_channel_keys, reason
                FROM article_routing_decisions
                WHERE article_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (article_id,),
            ).fetchone()

    def create_test_article(self, channel_id: str) -> PostJob:
        now = datetime.now(UTC)
        title = "RSS Dispatch Bot test post"
        canonical_key = f"testpost:{channel_id}:{now.isoformat()}"
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO articles (
                    canonical_key, title, normalized_title, url, normalized_url, source_name,
                    raw_published_at, normalized_published_at, ingested_at, timestamp_status, first_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    canonical_key,
                    title,
                    "rss dispatch bot test post",
                    None,
                    None,
                    "RSS Dispatch Bot",
                    None,
                    now.isoformat(),
                    now.isoformat(),
                    "ingest_time_used",
                    now.isoformat(),
                ),
            )
            self._conn.commit()
            article_id = int(cursor.lastrowid)
        return PostJob(
            article_id=article_id,
            channel_id=channel_id,
            title=title,
            url=None,
            summary="This is a controlled test message from /rss testpost. RSS feeds and dedupe are still protected.",
            image_url=None,
            image_source=None,
            source_name="RSS Dispatch Bot",
            normalized_published_at=now,
        )
