from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.database import Database
from app.models import FeedEntry, TimestampSettings
from app.normalizer import build_candidate
from app.routing.models import RoutingDecision


def make_candidate(
    title: str,
    url: str,
    guid: str | None = None,
    *,
    source_id: str = "cbs",
    source_class: str = "major_media",
    feed_name: str = "CBS World",
    rich_metadata: dict | None = None,
):
    return build_candidate(
        FeedEntry(
            feed_key="feed_1",
            feed_name=feed_name,
            raw_guid=guid,
            raw_title=title,
            raw_url=url,
            summary=None,
            image_url=None,
            image_source=None,
            raw_published_at=None,
            parsed={},
            source_id=source_id,
            source_class=source_class,
            rich_metadata=rich_metadata or {},
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


def test_email_cursor_persists_high_water_uid(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()

    assert db.email_cursor_uid("email-news-inbox", "INBOX") is None
    db.update_email_cursor("email-news-inbox", "INBOX", "123")

    assert db.email_cursor_uid("email-news-inbox", "INBOX") == "123"


def test_channel_title_reservation_blocks_same_title_in_channel(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    first = db.resolve_article(make_candidate("Same Story", "https://example.com/a", "1"), 24)
    second = db.resolve_article(make_candidate("Same Story", "https://example.com/b", "2"), 24)
    assert db.reserve_channel_title(first.article_id, "111111111111111111", "same story", "same story", "queued") is True
    assert db.reserve_channel_title(second.article_id, "111111111111111111", "same story", "same story", "queued") is False
    assert db.reserve_channel_title(second.article_id, "222222222222222222", "same story", "same story", "queued") is True


def test_same_story_different_sources_can_share_channel_until_cap(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    first = db.resolve_article(
        make_candidate("Same Story", "https://example.com/reuters", "1", source_id="reuters", feed_name="Reuters"),
        24,
    )
    second = db.resolve_article(
        make_candidate(
            "Same Story",
            "https://example.com/ap",
            "2",
            source_id="associated-press",
            feed_name="Associated Press",
        ),
        24,
    )
    assert first.article_id != second.article_id
    assert db.reserve_channel_title(
        first.article_id,
        "111111111111111111",
        "same story",
        "same story",
        "queued",
        "reuters",
        "cluster-1",
    ) is True
    assert db.reserve_channel_title(
        second.article_id,
        "111111111111111111",
        "same story",
        "same story",
        "queued",
        "associated-press",
        "cluster-1",
    ) is True
    assert db.channel_story_source_count("111111111111111111", "cluster-1") == 2


def test_same_source_later_duplicate_is_blocked(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    first = db.resolve_article(make_candidate("Same Story", "https://example.com/a", "1", source_id="reuters"), 24)
    second = db.resolve_article(make_candidate("Same Story", "https://example.com/b", "2", source_id="reuters"), 24)
    assert db.reserve_channel_title(
        first.article_id,
        "111111111111111111",
        "same story",
        "same story",
        "queued",
        "reuters",
        "cluster-1",
    ) is True
    assert db.reserve_channel_title(
        second.article_id,
        "111111111111111111",
        "same story",
        "same story",
        "queued",
        "reuters",
        "cluster-1",
    ) is False


def test_story_cluster_cap_counts_unique_sources(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    for index in range(5):
        article = db.resolve_article(
            make_candidate(f"Variant {index}", f"https://example.com/{index}", str(index), source_id=f"source-{index}"),
            24,
        )
        assert db.reserve_channel_title(
            article.article_id,
            "111111111111111111",
            f"variant {index}",
            f"variant {index}",
            "queued",
            f"source-{index}",
            "cluster-1",
        ) is True
    assert db.channel_story_source_count("111111111111111111", "cluster-1") == 5


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
        importance_score=6,
        importance_reasons=("tag +2: cyber",),
    )


def make_video_candidate():
    return build_candidate(
        FeedEntry(
            feed_key="feed_1",
            feed_name="Video Feed",
            raw_guid="video:1",
            raw_title="Video Story",
            raw_url="https://example.com/story",
            summary="A useful caption.",
            image_url="https://cdn.example.com/story.jpg",
            image_source="media_thumbnail",
            video_url="https://cdn.example.com/story.mp4",
            video_source="enclosure",
            raw_published_at=None,
            parsed={},
            rich_metadata={
                "media_items": [
                    {
                        "type": "video",
                        "url": "https://cdn.example.com/story.mp4",
                        "thumbnail_url": "https://cdn.example.com/story.jpg",
                    }
                ]
            },
        ),
        TimestampSettings(),
        now=datetime(2026, 5, 28, tzinfo=UTC),
    )
    db.record_routing_decision(article.article_id, decision, ("111111111111111111",))
    row = db.latest_routing_decision_for_article(article.article_id)

    assert db.recent_routing_error_count() == 0
    assert row["importance_score"] == 6
    assert row["importance_reasons"] == '["tag +2: cyber"]'


def test_post_job_includes_routing_importance(tmp_path: Path) -> None:
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
        importance_score=7,
        importance_reasons=("tag +3: active_conflict",),
    )
    db.record_routing_decision(article.article_id, decision, ("111111111111111111",))

    job = db.get_post_job(article.article_id, "111111111111111111", is_new_article=False)

    assert job.is_new_article is False
    assert job.importance_score == 7
    assert job.importance_reasons == ("tag +3: active_conflict",)


def test_post_job_includes_article_image(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    article = db.resolve_article(make_image_candidate(), 24)
    job = db.get_post_job(article.article_id, "111111111111111111")

    assert job.image_url == "https://d1ldvf68ux039x.cloudfront.net/thumbs/photos/2605/1/250w_q95.jpg"
    assert job.image_source == "media_thumbnail"


def test_post_job_includes_article_video(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    article = db.resolve_article(make_video_candidate(), 24)
    job = db.get_post_job(article.article_id, "111111111111111111")

    assert job.video_url == "https://cdn.example.com/story.mp4"
    assert job.video_source == "enclosure"
    assert job.image_url == "https://cdn.example.com/story.jpg"
    assert job.rich_metadata["media_items"][0]["type"] == "video"


def test_post_job_includes_rich_metadata(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    article = db.resolve_article(
        make_candidate(
            "Bluesky post",
            "https://example.com/story",
            "1",
            rich_metadata={"social_url": "https://bsky.app/profile/example.com/post/abc"},
        ),
        24,
    )
    job = db.get_post_job(article.article_id, "111111111111111111")

    assert job.rich_metadata == {"social_url": "https://bsky.app/profile/example.com/post/abc"}


def test_initialize_migrates_existing_articles_with_media_columns(tmp_path: Path) -> None:
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
    assert "video_url" in columns
    assert "video_source" in columns
    assert "rich_metadata" in columns


def test_initialize_migrates_existing_routing_decisions_with_importance_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "rss.sqlite"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE article_routing_decisions (
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
            explanation TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()

    migrated = Database(db_path)
    migrated.initialize()
    columns = {
        row["name"]
        for row in migrated._conn.execute("PRAGMA table_info(article_routing_decisions)").fetchall()
    }

    assert "importance_score" in columns
    assert "importance_reasons" in columns


def test_initialize_creates_importance_watch_terms_table(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    columns = {
        row["name"]
        for row in db._conn.execute("PRAGMA table_info(importance_watch_terms)").fetchall()
    }

    assert {"normalized_term", "term", "weight", "category", "enabled", "notes"}.issubset(columns)


def test_importance_watch_terms_can_be_added_listed_and_disabled(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()

    row = db.upsert_importance_watch_term(
        "Sinks",
        weight=4,
        category="major event",
        notes="naval loss",
    )

    assert row["normalized_term"] == "sinks"
    assert row["weight"] == 4
    assert row["category"] == "major_event"
    assert row["enabled"] is True
    assert db.list_importance_watch_terms() == [row]

    db.set_importance_watch_term_enabled("sinks", enabled=False, default_weight=4, default_category="major_event")

    assert db.list_importance_watch_terms() == []
    disabled = db.list_importance_watch_terms(include_disabled=True)
    assert len(disabled) == 1
    assert disabled[0]["enabled"] is False


def test_feed_status_success_and_failure_upsert_once_per_completion(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    next_poll = datetime(2026, 5, 28, tzinfo=UTC)

    db.mark_feed_failure("feed_1", "Feed", "https://example.com/rss", "timeout", next_poll)
    db.mark_feed_success("feed_1", "Feed", "https://example.com/rss", next_poll)
    row = db.feed_status_rows(limit=1)[0]

    assert row["feed_key"] == "feed_1"
    assert row["consecutive_failures"] == 0
    assert row["last_error"] is None
    assert row["next_poll_at"] == next_poll.isoformat()


def test_feed_health_report_filters_and_orders_repeated_failures(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    next_poll = datetime(2026, 5, 28, tzinfo=UTC)

    for _ in range(3):
        db.mark_feed_failure("minor", "Minor Feed", "https://example.com/minor", "timeout", next_poll)
    for _ in range(12):
        db.mark_feed_failure("major", "Major Feed", "https://example.com/major", "timeout", next_poll)

    rows = db.feed_health_report_rows(min_failures=10)

    assert [row["feed_key"] for row in rows] == ["major"]
    assert rows[0]["consecutive_failures"] == 12


def test_prune_inactive_feed_status_removes_removed_config_feeds(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    next_poll = datetime(2026, 5, 28, tzinfo=UTC)
    db.mark_feed_failure("active", "Active Feed", "https://example.com/active", "timeout", next_poll)
    db.mark_feed_failure("removed", "Removed Feed", "https://example.com/removed", "timeout", next_poll)

    removed = db.prune_inactive_feed_status(frozenset({"active"}))

    assert removed == 1
    rows = db.feed_status_rows(limit=10)
    assert [row["feed_key"] for row in rows] == ["active"]


def test_prune_runtime_history_preserves_recent_posted_articles(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    article = db.resolve_article(make_candidate("Recent Posted", "https://example.com/recent", "recent"), 24)
    db.record_channel_post(article.article_id, "111111111111111111", "m1")
    old_timestamp = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    db._conn.execute(
        "UPDATE articles SET normalized_published_at = ?, first_seen_at = ? WHERE id = ?",
        (old_timestamp, old_timestamp, article.article_id),
    )
    db._conn.commit()

    stats = db.prune_runtime_history(article_retention_days=30, posted_retention_days=90)

    assert stats["old_articles"] == 0
    assert db.get_post_job(article.article_id, "111111111111111111").title == "Recent Posted"


def test_prune_runtime_history_removes_old_article_even_with_recent_feed_entry(tmp_path: Path) -> None:
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    article = db.resolve_article(make_candidate("Old Feed Item", "https://example.com/old", "old"), 24)
    old_timestamp = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    db._conn.execute(
        "UPDATE articles SET normalized_published_at = ?, first_seen_at = ? WHERE id = ?",
        (old_timestamp, old_timestamp, article.article_id),
    )
    db._conn.commit()

    stats = db.prune_runtime_history(article_retention_days=30, posted_retention_days=30)

    assert stats["old_articles"] == 1
    assert stats["articles_deleted"] == 1
    assert db.has_feed_entry_seen(make_candidate("Old Feed Item", "https://example.com/old", "old")) is True
