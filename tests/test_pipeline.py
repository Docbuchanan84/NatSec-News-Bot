from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import pytest

from app.config_loader import load_config
from app.database import Database
from app.feed_fetcher import FeedFetchResult
from app.models import FeedEntry, PostJob
from app.publisher import PublisherService
from app.scheduler import SchedulerService, build_feed_runtime_map


class FakeAdapter:
    async def send(self, job: PostJob) -> str:
        return f"message-{job.article_id}"


def write_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.json"
    path.write_text(
        """
        {
          "version": 1,
          "settings": {
            "polling": {
              "postOldArticlesOnFirstRun": false
            },
            "publishing": {
              "secondsBetweenPostsPerChannel": 0
            }
          },
          "channels": [
            {
              "key": "a",
              "name": "A",
              "discordChannelId": "111111111111111111",
              "feeds": [{"name": "Feed", "url": "https://example.com/rss"}]
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    return path


def write_routing_config(tmp_path: Path) -> Path:
    routing_dir = tmp_path / "routing"
    routing_dir.mkdir()
    (routing_dir / "taxonomy.json").write_text(
        """
        {
          "version": 1,
          "tags": {
            "rss_article": {"parent_tags": []},
            "military": {"parent_tags": ["rss_article"]},
            "skip_candidate": {"parent_tags": []}
          }
        }
        """,
        encoding="utf-8",
    )
    (routing_dir / "knowledge_base.json").write_text(
        """
        {
          "version": 1,
          "entries": [
            {"id": "military", "aliases": ["military"], "tags": ["military"], "priority": 10, "score": 4}
          ]
        }
        """,
        encoding="utf-8",
    )
    (routing_dir / "channels.json").write_text(
        """
        {
          "version": 1,
          "max_destinations": 1,
          "review_tags": [],
          "skip_tags": ["skip_candidate"],
          "channels": [
            {
              "channel_key": "a",
              "enabled": true,
              "minimum_score": 4,
              "priority": 1,
              "tag_boosts": {"military": 4}
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    path = tmp_path / "routing-config.json"
    path.write_text(
        """
        {
          "version": 1,
          "settings": {
            "polling": {
              "postOldArticlesOnFirstRun": true
            },
            "publishing": {
              "secondsBetweenPostsPerChannel": 0
            },
            "routing": {
              "enabled": true,
              "mode": "enforced",
              "configDir": "__ROUTING_DIR__"
            }
          },
          "channels": [
            {
              "key": "a",
              "name": "A",
              "discordChannelId": "111111111111111111",
              "feeds": [{"name": "Feed", "url": "https://example.com/rss"}]
            }
          ]
        }
        """.replace("__ROUTING_DIR__", routing_dir.as_posix()),
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def process_first_run_entry(tmp_path: Path, raw_published_at: str | None):
    config = load_config(write_config(tmp_path))
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    publisher = PublisherService(db, FakeAdapter())
    publisher.configure(config)
    scheduler = SchedulerService(db, publisher)
    scheduler.configure(config)
    feed = next(iter(build_feed_runtime_map(config).values()))
    result = FeedFetchResult(
        feed=feed,
        first_success=True,
        entries=(
            FeedEntry(
                feed_key=feed.feed_key,
                feed_name=feed.display_name,
                raw_guid="1",
                raw_title="Story",
                raw_url="https://example.com/story",
                summary=None,
                image_url=None,
                image_source=None,
                raw_published_at=raw_published_at,
                parsed={},
            ),
        ),
    )

    summary = await scheduler._process_feed_result(result)
    await publisher.shutdown()
    return summary, db


@pytest.mark.asyncio
async def test_first_run_suppresses_missing_timestamps(tmp_path: Path) -> None:
    summary, db = await process_first_run_entry(tmp_path, None)
    assert summary.new_articles == 0
    assert summary.posts_queued == 0
    assert db.counts()["channel_posts"] == 0
    assert db._conn.execute("SELECT count(*) FROM feed_entry_seen").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_first_run_posts_recent_valid_timestamps(tmp_path: Path) -> None:
    raw_published_at = format_datetime(datetime.now(UTC) - timedelta(hours=2))
    summary, db = await process_first_run_entry(tmp_path, raw_published_at)

    assert summary.new_articles == 1
    assert summary.posts_queued == 1


@pytest.mark.asyncio
async def test_first_run_skips_stale_valid_timestamps(tmp_path: Path) -> None:
    raw_published_at = format_datetime(datetime.now(UTC) - timedelta(days=3))
    summary, db = await process_first_run_entry(tmp_path, raw_published_at)

    assert summary.new_articles == 0
    assert summary.posts_queued == 0
    assert db.counts()["channel_posts"] == 0
    assert db._conn.execute("SELECT count(*) FROM feed_entry_seen").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_enforced_routing_rejects_irrelevant_article_even_with_image(tmp_path: Path) -> None:
    config = load_config(write_routing_config(tmp_path))
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    publisher = PublisherService(db, FakeAdapter())
    publisher.configure(config)
    scheduler = SchedulerService(db, publisher)
    scheduler.configure(config)
    feed = next(iter(build_feed_runtime_map(config).values()))
    result = FeedFetchResult(
        feed=feed,
        first_success=False,
        entries=(
            FeedEntry(
                feed_key=feed.feed_key,
                feed_name=feed.display_name,
                raw_guid="recipe-1",
                raw_title="Top 5 recipes for spring",
                raw_url="https://example.com/top-5-recipes-for-spring",
                summary="A seasonal cooking list.",
                image_url="https://example.com/recipe.jpg",
                image_source="html_img",
                raw_published_at=format_datetime(datetime.now(UTC)),
                parsed={},
            ),
        ),
    )

    summary = await scheduler._process_feed_result(result)
    await publisher.shutdown()

    assert summary.new_articles == 1
    assert summary.posts_queued == 0
    assert db.counts()["channel_posts"] == 0
