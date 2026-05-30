from __future__ import annotations

import json
from pathlib import Path

from app.config_loader import load_config


def test_logging_settings_are_loaded(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "version": 1,
                "settings": {
                    "logging": {
                        "auditEnabled": True,
                        "detailedErrors": True,
                        "auditLogPath": "logs/audit.log",
                        "errorLogPath": "logs/errors.log",
                        "maxBytes": 2048,
                        "backupCount": 2,
                    }
                },
                "channels": [
                    {
                        "key": "a",
                        "name": "A",
                        "discordChannelId": "111111111111111111",
                        "feeds": [{"name": "Feed", "url": "https://example.com/rss"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.settings.logging.audit_enabled is True
    assert config.settings.logging.detailed_errors is True
    assert config.settings.logging.audit_log_path == "logs/audit.log"
