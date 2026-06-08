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
    assert config.settings.polling.min_interval_seconds == 30
    assert config.settings.polling.max_concurrent_feed_fetches == 10
    assert config.settings.timestamps.max_post_age_hours == 48
    assert config.settings.routing.enabled is False
    assert config.settings.routing.mode == "observe_only"
    assert config.settings.maintenance.enabled is True
    assert config.settings.maintenance.article_retention_days == 30


def test_min_poll_interval_floor_applies_to_channel_interval(tmp_path: Path) -> None:
    data = minimal_config()
    data["settings"] = {"polling": {"defaultIntervalSeconds": 300, "minIntervalSeconds": 900}}
    data["channels"][0]["pollIntervalSeconds"] = 300

    config = load_config(write_config(tmp_path, data))

    assert config.settings.polling.min_interval_seconds == 900
    assert config.channels[0].poll_interval_seconds == 900


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


def test_rejects_unknown_routing_mode(tmp_path: Path) -> None:
    data = minimal_config()
    data["settings"] = {"routing": {"enabled": True, "mode": "automatic"}}
    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, data))
    assert "settings.routing.mode" in str(exc.value)


def test_loads_top_level_feeds_and_destination_only_channels(tmp_path: Path) -> None:
    data = {
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
                "legacyChannelKeys": ["middle-east"],
            }
        ],
        "channels": [
            {
                "key": "middle-east",
                "name": "Middle East",
                "discordChannelId": "111111111111111111",
            },
            {
                "key": "review",
                "name": "Review",
                "discordChannelId": "1511541774642843789",
            },
        ],
    }

    config = load_config(write_config(tmp_path, data))

    assert config.feeds[0].source_id == "reuters"
    assert config.feeds[0].source_class == "wire_service"
    assert config.feeds[0].fetch_timeout_seconds == 20
    assert config.feeds[0].legacy_channel_keys == ("middle-east",)
    assert config.channels[1].feeds == ()


def test_loads_publishing_and_maintenance_runtime_settings(tmp_path: Path) -> None:
    data = minimal_config()
    data["settings"] = {
        "publishing": {"shutdownDrainSeconds": 45},
        "maintenance": {
            "enabled": True,
            "intervalHours": 6,
            "articleRetentionDays": 21,
            "postedRetentionDays": 45,
            "nonPostRetentionHours": 12,
            "seenRetentionDays": 10,
            "feedEntrySeenRetentionDays": 120,
            "articleBatchSize": 750,
            "optimizeOnMaintenance": False,
            "vacuumOnStartup": True,
        },
    }

    config = load_config(write_config(tmp_path, data))

    assert config.settings.publishing.shutdown_drain_seconds == 45
    assert config.settings.maintenance.interval_hours == 6
    assert config.settings.maintenance.article_retention_days == 21
    assert config.settings.maintenance.posted_retention_days == 45
    assert config.settings.maintenance.non_post_retention_hours == 12
    assert config.settings.maintenance.seen_retention_days == 10
    assert config.settings.maintenance.feed_entry_seen_retention_days == 120
    assert config.settings.maintenance.article_batch_size == 750
    assert config.settings.maintenance.optimize_on_maintenance is False
    assert config.settings.maintenance.vacuum_on_startup is True


def test_loads_failure_backoff_settings(tmp_path: Path) -> None:
    data = minimal_config()
    data["settings"] = {
        "failureBackoff": {
            "enabled": True,
            "minorFailureThreshold": 5,
            "majorFailureThreshold": 25,
            "suspendFailureThreshold": 250,
            "minorRetrySeconds": 1800,
            "majorRetrySeconds": 7200,
            "suspendedRetrySeconds": 86400,
        }
    }

    config = load_config(write_config(tmp_path, data))

    assert config.settings.failure_backoff.minor_failure_threshold == 5
    assert config.settings.failure_backoff.major_failure_threshold == 25
    assert config.settings.failure_backoff.suspend_failure_threshold == 250
    assert config.settings.failure_backoff.minor_retry_seconds == 1800
    assert config.settings.failure_backoff.major_retry_seconds == 7200
    assert config.settings.failure_backoff.suspended_retry_seconds == 86400


def test_rejects_failure_backoff_threshold_inversion(tmp_path: Path) -> None:
    data = minimal_config()
    data["settings"] = {
        "failureBackoff": {
            "minorFailureThreshold": 100,
            "majorFailureThreshold": 10,
        }
    }

    with pytest.raises(ConfigError) as exc:
        load_config(write_config(tmp_path, data))

    assert "minorFailureThreshold" in str(exc.value)
