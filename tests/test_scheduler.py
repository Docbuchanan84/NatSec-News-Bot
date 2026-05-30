from __future__ import annotations

from app.config_loader import load_config
from app.scheduler import build_feed_runtime_map


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
