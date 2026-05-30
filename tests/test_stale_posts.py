from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config_loader import load_config
from app.database import Database
from app.publisher import PublisherService
from app.scheduler import SchedulerService


class FakeAdapter:
    async def send(self, job):
        return "message-id"


def test_stale_guard_only_applies_to_valid_feed_timestamps(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        """
        {
          "version": 1,
          "settings": {
            "timestamps": {"maxPostAgeHours": 48}
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
    db = Database(tmp_path / "rss.sqlite")
    db.initialize()
    scheduler = SchedulerService(db, PublisherService(db, FakeAdapter()))
    scheduler.configure(load_config(config_path))

    assert scheduler._is_stale_for_posting(datetime.now(UTC) - timedelta(days=3), "valid") is True
    assert scheduler._is_stale_for_posting(datetime.now(UTC) - timedelta(days=3), "missing") is False
    assert scheduler._is_stale_for_posting(datetime.now(UTC) - timedelta(hours=1), "valid") is False
