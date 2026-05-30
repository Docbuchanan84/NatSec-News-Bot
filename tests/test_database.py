from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.database import Database
from app.models import FeedEntry, TimestampSettings
from app.normalizer import build_candidate


def make_candidate(title: str, url: str, guid: str | None = None):
    return build_candidate(
        FeedEntry(
            feed_key="feed_1",
            feed_name="CBS World",
            raw_guid=guid,
            raw_title=title,
            raw_url=url,
            summary=None,
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
