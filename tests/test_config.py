from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config_loader import ConfigError, load_config


def write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def minimal_config() -> dict:
    return {
        "version": 1,
        "channels": [
            {
                "key": "world-news",
                "name": "World News",
                "discordChannelId": "111111111111111111",
                "feeds": [{"name": "CBS World", "url": "https://www.cbsnews.com/latest/rss/world"}],
            }
        ],
    }


def test_loads_minimal_valid_config(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path, minimal_config()))
    assert config.channels[0].key == "world-news"
    assert config.settings.polling.default_interval_seconds == 300
    assert config.settings.polling.max_concurrent_feed_fetches == 10
    assert config.settings.timestamps.max_post_age_hours == 48


def test_rejects_missing_feed_url(tmp_path: Path) -> None:
    data = minimal_config()
    del data["channels"][0]["feeds"][0]["url"]
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, data))
    assert "channels[0].feeds[0].url" in str(exc.value)


def test_rejects_duplicate_channel_ids(tmp_path: Path) -> None:
    data = minimal_config()
    data["channels"].append(
        {
            "key": "copy",
            "name": "Copy",
            "discordChannelId": "111111111111111111",
            "feeds": [{"name": "AP", "url": "https://apnews.com/rss/world"}],
        }
    )
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, data))
    assert "duplicates another channel ID" in str(exc.value)
