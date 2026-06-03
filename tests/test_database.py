from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.database import Database
from app.models import FeedEntry, TimestampSettings
from app.normalizer import build_candidate
from app.routing.models import RoutingDecision


def make_candidate(title: str, url: str, guid: str | None = None):
    return build_candidate(
        FeedEntry(
            feed_key="feed_1",
            feed_name="CBS World",
            raw_guid=guid,
            raw_title=title,
            raw_url=url,
            summary=None,
            image_url=None,
            image_source=None,
            raw_published_at=None,
            parsed={},
        ),
        TimestampSettings(),
        now=datetime(2026, 5, 28, tzinfo=UTC),
    )


def make_image_candidate():
    return build_candidate(
        FeedEntry(
            feed_key="feed_1",
            feed_name="DVIDS",
            raw_guid="image:1",
            raw_title="Photo Story",
            raw_url="https://www.dvidshub.net/image/1/photo-story",
            summary="A useful caption.",
            image_url="https://d1ldvf68ux039x.cloudfront.net/thumbs/photos/2605/1/250w_q95.jpg",
            image_source="media_thumbnail",
            raw_published_at=None,
            parsed={},
        ),
        TimestampSettings(),
        now=datetime(2026, 5, 28, tzinfo=UTC),
    )


def test_dedupes_tracking_url_variants(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    first = db.resolve_article(make_candidate("Story", "https://example.com/a?utm_source=x", "1"), 24)
    second = db.resolve_article(make_candidate("Story", "https://example.com/a", "2"), 24)
    assert first.article_id == second.article_id
    assert first.is_new_article is True
    assert second.is_new_article is False


def test_channel_posts_are_unique(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    article = db.resolve_article(make_candidate("Story", "https://example.com/a", "1"), 24)
    assert db.record_channel_post(article.article_id, "111111111111111111", "m1") is True
    assert db.record_channel_post(article.article_id, "111111111111111111", "m2") is False
    assert db.record_channel_post(article.article_id, "222222222222222222", "m3") is True


def test_channel_title_reservation_blocks_same_title_in_channel(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    first = db.resolve_article(make_candidate("Same Story", "https://example.com/a", "1"), 24)
    second = db.resolve_article(make_candidate("Same Story", "https://example.com/b", "2"), 24)
    assert db.reserve_channel_title(first.article_id, "111111111111111111", "same story", "same story", "queued") is True
    assert db.reserve_channel_title(second.article_id, "111111111111111111", "same story", "same story", "queued") is False
    assert db.reserve_channel_title(second.article_id, "222222222222222222", "same story", "same story", "queued") is True


def test_records_routing_decision_tags_and_matches(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    article = db.resolve_article(make_candidate("Story", "https://example.com/a", "1"), 24)
    decision = RoutingDecision(
        content_mode="title_only",
        matched_entries=(),
        emitted_tags=("china",),
        expanded_tags=("indo_pacific",),
        channel_scores=(),
        selected_channel_keys=("indo-pacific",),
        decision_status="routed",
        top_score=5,
        explanation=("test",),
    )
    db.record_routing_decision(article.article_id, decision, ("111111111111111111",))
    assert db.recent_routing_error_count() == 0


def test_post_job_includes_article_image(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    article = db.resolve_article(make_image_candidate(), 24)
    job = db.get_post_job(article.article_id, "111111111111111111")

    assert job.image_url == "https://d1ldvf68ux039x.cloudfront.net/thumbs/photos/2605/1/250w_q95.jpg"
    assert job.image_source == "media_thumbnail"


def test_initialize_migrates_existing_articles_with_image_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "rss.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_key TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            normalized_title TEXT,
            title_signature TEXT,
            source_family TEXT,
            url TEXT,
            normalized_url TEXT,
            summary TEXT,
            source_name TEXT,
            raw_published_at TEXT,
            normalized_published_at TEXT,
            ingested_at TEXT NOT NULL,
            timestamp_status TEXT NOT NULL,
            first_seen_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    migrated = Database(db_path)
    migrated.initialize()
    columns = {row["name"] for row in migrated._conn.execute("PRAGMA table_info(articles)").fetchall()}

    assert "image_url" in columns
    assert "image_source" in columns
