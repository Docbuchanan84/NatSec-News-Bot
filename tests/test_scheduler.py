from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest

from app.config_loader import load_config
from app.feed_fetcher import FeedFetchError
from app.models import AppConfig, FailureBackoffSettings, FeedRuntime, MaintenanceSettings, Settings
from app.scheduler import build_feed_runtime_map
from app.scheduler import SchedulerService


class FakeBackoffDb:
    def __init__(self, failures: int) -> None:
        self.failures = failures

    def feed_consecutive_failures(self, feed_key: str) -> int:
        return self.failures


class FakeMaintenanceDb:
    def __init__(self, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
        self.prune_calls = 0
        self.optimize_calls = 0

    def database_size_bytes(self) -> int:
        return 1

    def prune_runtime_history(self, **kwargs) -> dict[str, int]:
        self.prune_calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return {"articles_deleted": 1}

    def optimize(self) -> None:
        self.optimize_calls += 1


def test_same_feed_under_two_channels_is_one_runtime_feed(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        """
        {
          "version": 1,
          "channels": [
            {
              "key": "a",
              "name": "A",
              "discordChannelId": "111111111111111111",
              "pollIntervalSeconds": 300,
              "feeds": [{"name": "Feed", "url": "https://example.com/rss?utm_source=x"}]
            },
            {
              "key": "b",
              "name": "B",
              "discordChannelId": "222222222222222222",
              "pollIntervalSeconds": 60,
              "feeds": [{"name": "Feed Again", "url": "https://example.com/rss"}]
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    config = load_config(path)
    feeds = build_feed_runtime_map(config)
    assert len(feeds) == 1
    feed = next(iter(feeds.values()))
    assert feed.interval_seconds == 60
    assert set(feed.channel_ids) == {"111111111111111111", "222222222222222222"}


def test_top_level_feed_uses_legacy_channel_keys_for_observe_mode(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        """
        {
          "version": 1,
          "feeds": [
            {
              "id": "reuters-world",
              "sourceId": "reuters",
              "sourceClass": "wire_service",
              "name": "Reuters World",
              "url": "https://example.com/reuters.rss",
              "pollIntervalSeconds": 300,
              "fetchTimeoutSeconds": 20,
              "legacyChannelKeys": ["middle-east"]
            }
          ],
          "channels": [
            {
              "key": "middle-east",
              "name": "Middle East",
              "discordChannelId": "111111111111111111"
            },
            {
              "key": "review",
              "name": "Review",
              "discordChannelId": "1511541774642843789"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    config = load_config(path)
    feeds = build_feed_runtime_map(config)
    feed = feeds["reuters-world"]

    assert feed.source_id == "reuters"
    assert feed.source_class == "wire_service"
    assert feed.fetch_timeout_seconds == 20
    assert feed.channel_ids == ("111111111111111111",)


def test_dvids_waf_challenge_retries_without_hour_long_backoff():
    scheduler = SchedulerService(db=object(), publisher=object())
    feed = FeedRuntime(
        feed_key="dvids-centcom",
        display_name="CENTCOM DVIDS",
        url="https://www.dvidshub.net/rss/unit/72",
        normalized_url="https://www.dvidshub.net/rss/unit/72",
        interval_seconds=300,
        channel_ids=("111111111111111111",),
        channel_keys=("middle-east",),
    )

    assert scheduler._failure_retry_seconds(feed, FeedFetchError("waf action=challenge")) == 900


def test_chronic_feed_failures_use_configured_backoff():
    scheduler = SchedulerService(db=FakeBackoffDb(failures=99), publisher=object())
    scheduler.config = AppConfig(
        version=1,
        bot=object(),
        discord=object(),
        settings=Settings(
            failure_backoff=FailureBackoffSettings(
                minor_failure_threshold=10,
                major_failure_threshold=100,
                suspend_failure_threshold=500,
                minor_retry_seconds=21600,
                major_retry_seconds=86400,
                suspended_retry_seconds=604800,
            )
        ),
        feeds=(),
        channels=(),
        raw={},
    )
    feed = FeedRuntime(
        feed_key="dead-feed",
        display_name="Dead Feed",
        url="https://example.com/rss",
        normalized_url="https://example.com/rss",
        interval_seconds=300,
        channel_ids=("111111111111111111",),
        channel_keys=("middle-east",),
    )

    assert scheduler._failure_retry_seconds(feed, FeedFetchError("timeout"), first_success=False) == 86400


def test_never_succeeded_feed_can_be_suspended_after_threshold():
    scheduler = SchedulerService(db=FakeBackoffDb(failures=499), publisher=object())
    scheduler.config = AppConfig(
        version=1,
        bot=object(),
        discord=object(),
        settings=Settings(),
        feeds=(),
        channels=(),
        raw={},
    )
    feed = FeedRuntime(
        feed_key="never-worked",
        display_name="Never Worked",
        url="https://example.com/rss",
        normalized_url="https://example.com/rss",
        interval_seconds=300,
        channel_ids=("111111111111111111",),
        channel_keys=("middle-east",),
    )

    assert scheduler._failure_retry_seconds(feed, FeedFetchError("not found"), first_success=True) == 604800


@pytest.mark.asyncio
async def test_runtime_maintenance_is_scheduled_without_blocking_event_loop():
    db = FakeMaintenanceDb(delay_seconds=0.1)
    scheduler = SchedulerService(db=db, publisher=object())
    scheduler.config = AppConfig(
        version=1,
        bot=object(),
        discord=object(),
        settings=Settings(maintenance=MaintenanceSettings(interval_hours=1)),
        feeds=(),
        channels=(),
        raw={},
    )
    scheduler._next_maintenance_at = datetime.now(UTC) - timedelta(seconds=1)

    started = time.perf_counter()
    scheduler._maybe_prune_runtime_history()
    elapsed = time.perf_counter() - started

    assert elapsed < 0.05
    assert scheduler._maintenance_task is not None
    assert not scheduler._maintenance_task.done()

    await scheduler._maintenance_task
    assert db.prune_calls == 1
    assert db.optimize_calls == 1


@pytest.mark.asyncio
async def test_runtime_maintenance_does_not_overlap_active_run():
    db = FakeMaintenanceDb(delay_seconds=0.1)
    scheduler = SchedulerService(db=db, publisher=object())
    scheduler.config = AppConfig(
        version=1,
        bot=object(),
        discord=object(),
        settings=Settings(maintenance=MaintenanceSettings(interval_hours=1)),
        feeds=(),
        channels=(),
        raw={},
    )

    scheduler._next_maintenance_at = datetime.now(UTC) - timedelta(seconds=1)
    scheduler._maybe_prune_runtime_history()
    first_task = scheduler._maintenance_task

    scheduler._next_maintenance_at = datetime.now(UTC) - timedelta(seconds=1)
    scheduler._maybe_prune_runtime_history()

    assert scheduler._maintenance_task is first_task
    await first_task
    assert db.prune_calls == 1
