from __future__ import annotations

from app.config_loader import load_config
from app.feed_fetcher import FeedFetchError
from app.models import FeedRuntime
from app.scheduler import build_feed_runtime_map
from app.scheduler import SchedulerService


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
