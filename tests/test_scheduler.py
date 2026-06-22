from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import asyncio
import pytest

from app.config_loader import load_config
from app.feed_fetcher import FeedFetchError, FeedFetchResult
from app.models import (
    AppConfig,
    EmailSourceRuntime,
    FailureBackoffSettings,
    FeedRuntime,
    MaintenanceSettings,
    Settings,
)
from app.routing.models import RoutingDecision
from app.scheduler import FetchBatchResult, RefreshSummary, build_email_source_runtime_map, build_feed_runtime_map
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


def make_feed_runtime(
    feed_key: str = "feed-1",
    *,
    url: str | None = None,
    interval_seconds: int = 300,
) -> FeedRuntime:
    feed_url = url or f"https://example.com/{feed_key}.xml"
    return FeedRuntime(
        feed_key=feed_key,
        display_name=f"Feed {feed_key}",
        url=feed_url,
        normalized_url=feed_url,
        interval_seconds=interval_seconds,
        channel_ids=("111111111111111111",),
        channel_keys=("middle-east",),
    )


def make_email_runtime(feed_key: str = "email-news-inbox") -> EmailSourceRuntime:
    return EmailSourceRuntime(
        feed_key=feed_key,
        display_name="Email: News Inbox",
        imap_host_env="EMAIL_IMAP_HOST",
        imap_port_env="EMAIL_IMAP_PORT",
        username_env="EMAIL_USERNAME",
        password_env="EMAIL_PASSWORD",
        mailbox="INBOX",
        from_contains=(),
        list_id_contains=(),
        subject_contains=(),
        match_all=True,
        url=f"imap://EMAIL_IMAP_HOST/INBOX/{feed_key}",
        normalized_url=f"imap://email_imap_host/INBOX/{feed_key}",
        interval_seconds=300,
        channel_ids=(),
        channel_keys=(),
    )


def test_due_feeds_are_bounded_and_prioritize_short_interval_feeds():
    scheduler = SchedulerService(db=object(), publisher=object())
    scheduler.config = AppConfig(
        version=1,
        bot=object(),
        discord=object(),
        settings=Settings(),
        feeds=(),
        channels=(),
        raw={},
    )
    now = datetime.now(UTC)
    hourly_dvids = {
        f"dvids-{index}": make_feed_runtime(
            f"dvids-{index}",
            url=f"https://www.dvidshub.net/rss/unit/{index}",
            interval_seconds=3600,
        )
        for index in range(100)
    }
    regular = make_feed_runtime(
        "regular-feed",
        url="https://example.com/regular-feed.xml",
        interval_seconds=300,
    )
    scheduler.feeds = {**hourly_dvids, regular.feed_key: regular}
    scheduler._next_due = {
        feed_key: now - timedelta(hours=1) for feed_key in hourly_dvids
    }
    scheduler._next_due[regular.feed_key] = now - timedelta(minutes=1)

    due = scheduler._due_feeds(now)

    assert len(due) == 80
    assert due[0].feed_key == "regular-feed"


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


def test_email_source_runtime_map_uses_configured_metadata(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        """
        {
          "version": 1,
          "emailSources": [
            {
              "id": "email-news-inbox",
              "name": "Email: News Inbox",
              "matchAll": true,
              "sourceId": "email-news",
              "sourceClass": "newsletter",
              "routingTags": ["news"],
              "noMatchPolicy": "review",
              "maxMessagesPerPoll": 100
            }
          ],
          "channels": [
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
    sources = build_email_source_runtime_map(config)
    source = sources["email-news-inbox"]

    assert source.source_id == "email-news"
    assert source.source_class == "newsletter"
    assert source.routing_tags == ("news",)
    assert source.no_match_policy == "review"
    assert source.max_messages_per_poll == 100


def test_email_no_match_policy_routes_high_signal_to_review_channel():
    scheduler = SchedulerService(db=object(), publisher=object())
    scheduler.routing_mode = "enforced"
    scheduler.channel_key_to_id = {"review": "1511541774642843789"}
    source = EmailSourceRuntime(
        feed_key="email-news-inbox",
        display_name="Email: News Inbox",
        imap_host_env="EMAIL_IMAP_HOST",
        imap_port_env="EMAIL_IMAP_PORT",
        username_env="EMAIL_USERNAME",
        password_env="EMAIL_PASSWORD",
        mailbox="INBOX",
        from_contains=(),
        list_id_contains=(),
        subject_contains=(),
        match_all=True,
        url="imap://EMAIL_IMAP_HOST/INBOX/email-news-inbox",
        normalized_url="imap://email_imap_host/INBOX/email-news-inbox",
        interval_seconds=300,
        channel_ids=(),
        channel_keys=(),
        no_match_policy="review",
    )
    decision = RoutingDecision(
        content_mode="title_only",
        matched_entries=(),
        emitted_tags=(),
        expanded_tags=(),
        channel_scores=(),
        selected_channel_keys=(),
        decision_status="no_match",
        top_score=0,
        explanation=(),
    )

    candidate = SimpleNamespace(
        title="ShinyHunters exploits Oracle PeopleSoft zero-day",
        summary="Researchers say the cyber campaign targeted enterprise systems.",
        url="https://cyberscoop.com/shinyhunters-oracle-peoplesoft-zero-day",
        rich_metadata={"routing_summary": "Cybersecurity researchers described a zero-day exploitation campaign."},
    )

    assert scheduler._target_channel_ids((), decision, source, candidate) == ("1511541774642843789",)


def test_email_no_match_policy_drops_low_signal_review_noise():
    scheduler = SchedulerService(db=object(), publisher=object())
    scheduler.routing_mode = "enforced"
    scheduler.channel_key_to_id = {"review": "1511541774642843789"}
    source = EmailSourceRuntime(
        feed_key="email-news-inbox",
        display_name="Email: News Inbox",
        imap_host_env="EMAIL_IMAP_HOST",
        imap_port_env="EMAIL_IMAP_PORT",
        username_env="EMAIL_USERNAME",
        password_env="EMAIL_PASSWORD",
        mailbox="INBOX",
        from_contains=(),
        list_id_contains=(),
        subject_contains=(),
        match_all=True,
        url="imap://EMAIL_IMAP_HOST/INBOX/email-news-inbox",
        normalized_url="imap://email_imap_host/INBOX/email-news-inbox",
        interval_seconds=300,
        channel_ids=(),
        channel_keys=(),
        no_match_policy="review",
    )
    decision = RoutingDecision(
        content_mode="title_only",
        matched_entries=(),
        emitted_tags=(),
        expanded_tags=(),
        channel_scores=(),
        selected_channel_keys=(),
        decision_status="no_match",
        top_score=0,
        explanation=(),
    )
    candidate = SimpleNamespace(
        title="Weekly video recap",
        summary="Register now for the next briefing.",
        url="https://example.com/event",
        rich_metadata={"email_low_signal": True},
    )

    assert scheduler._target_channel_ids((), decision, source, candidate) == ()


@pytest.mark.asyncio
async def test_email_loop_runs_while_rss_fetch_is_slow(monkeypatch):
    scheduler = SchedulerService(db=object(), publisher=object())
    scheduler.config = AppConfig(
        version=1,
        bot=object(),
        discord=object(),
        settings=Settings(maintenance=MaintenanceSettings(enabled=False)),
        feeds=(),
        channels=(),
        raw={},
    )
    scheduler.feeds = {"feed-1": make_feed_runtime("feed-1")}
    scheduler.email_sources = {"email-news-inbox": make_email_runtime("email-news-inbox")}
    scheduler._next_due = {key: datetime.now(UTC) for key in (*scheduler.feeds, *scheduler.email_sources)}
    events: list[str] = []

    async def slow_fetch_feeds(_feeds):
        events.append("rss-start")
        await asyncio.sleep(0.2)
        events.append("rss-done")
        return FetchBatchResult()

    async def fast_fetch_email(_sources):
        events.append("email")
        scheduler._stopping = True
        return FetchBatchResult()

    monkeypatch.setattr(scheduler, "_fetch_feeds", slow_fetch_feeds)
    monkeypatch.setattr(scheduler, "_fetch_email_sources", fast_fetch_email)

    scheduler.start()
    await asyncio.sleep(0.05)

    assert "email" in events
    assert "rss-done" not in events
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_rss_loop_runs_while_email_fetch_is_slow(monkeypatch):
    scheduler = SchedulerService(db=object(), publisher=object())
    scheduler.config = AppConfig(
        version=1,
        bot=object(),
        discord=object(),
        settings=Settings(maintenance=MaintenanceSettings(enabled=False)),
        feeds=(),
        channels=(),
        raw={},
    )
    scheduler.feeds = {"feed-1": make_feed_runtime("feed-1")}
    scheduler.email_sources = {"email-news-inbox": make_email_runtime("email-news-inbox")}
    scheduler._next_due = {key: datetime.now(UTC) for key in (*scheduler.feeds, *scheduler.email_sources)}
    events: list[str] = []

    async def fast_fetch_feeds(_feeds):
        events.append("rss")
        scheduler._stopping = True
        return FetchBatchResult()

    async def slow_fetch_email(_sources):
        events.append("email-start")
        await asyncio.sleep(0.2)
        events.append("email-done")
        return FetchBatchResult()

    monkeypatch.setattr(scheduler, "_fetch_and_enqueue_feeds", fast_fetch_feeds)
    monkeypatch.setattr(scheduler, "_fetch_email_sources", slow_fetch_email)

    scheduler.start()
    await asyncio.sleep(0.05)

    assert "rss" in events
    assert "email-done" not in events
    await scheduler.shutdown()


@pytest.mark.asyncio
async def test_fetch_results_are_processed_from_shared_queue(monkeypatch):
    scheduler = SchedulerService(db=object(), publisher=object())
    scheduler.config = AppConfig(
        version=1,
        bot=object(),
        discord=object(),
        settings=Settings(maintenance=MaintenanceSettings(enabled=False)),
        feeds=(),
        channels=(),
        raw={},
    )
    processed: list[str] = []
    result = FeedFetchResult(feed=make_feed_runtime("feed-1"), entries=())

    async def process_result(fetch_result):
        processed.append(fetch_result.feed.feed_key)
        return RefreshSummary(feeds_checked=1)

    monkeypatch.setattr(scheduler, "_process_queued_result", process_result)

    scheduler.start()
    await scheduler._enqueue_results((result,))
    await asyncio.wait_for(scheduler._result_queue.join(), timeout=1)
    await scheduler.shutdown()

    assert processed == ["feed-1"]


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
