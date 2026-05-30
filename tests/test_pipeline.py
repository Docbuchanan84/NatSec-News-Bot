from __future__ import annotations

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


@pytest.mark.asyncio
async def test_first_run_suppresses_visible_entries(tmp_path: Path) -> None:
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
                raw_published_at=None,
                parsed={},
            ),
        ),
    )

    summary = await scheduler._process_feed_result(result)
    await publisher.shutdown()

    assert summary.new_articles == 1
    assert summary.posts_queued == 0
    assert db.counts()["channel_posts"] == 1
