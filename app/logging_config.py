from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.models import AppConfig


AUDIT_HANDLER_NAME = "rssbot-audit-file"
ERROR_HANDLER_NAME = "rssbot-error-file"


def configure_logging(config: AppConfig | None = None) -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    if not any(isinstance(handler, logging.StreamHandler) and handler.name == "rssbot-console" for handler in root.handlers):
        console = logging.StreamHandler()
        console.name = "rssbot-console"
        console.setFormatter(formatter)
        console.setLevel(level)
        root.addHandler(console)

    _remove_named_handler(root, AUDIT_HANDLER_NAME)
    _remove_named_handler(root, ERROR_HANDLER_NAME)

    if config is None or not config.settings.logging.audit_enabled:
        logging.getLogger("app.audit").setLevel(logging.CRITICAL)
        return

    logging_settings = config.settings.logging
    logging.getLogger("app.audit").setLevel(logging.INFO)
    audit_handler = _rotating_handler(
        logging_settings.audit_log_path,
        logging_settings.max_bytes,
        logging_settings.backup_count,
        logging.INFO,
        formatter,
        AUDIT_HANDLER_NAME,
    )
    error_handler = _rotating_handler(
        logging_settings.error_log_path,
        logging_settings.max_bytes,
        logging_settings.backup_count,
        logging.ERROR,
        formatter,
        ERROR_HANDLER_NAME,
    )
    root.addHandler(audit_handler)
    root.addHandler(error_handler)


def _rotating_handler(
    path_value: str,
    max_bytes: int,
    backup_count: int,
    level: int,
    formatter: logging.Formatter,
    name: str,
) -> RotatingFileHandler:
    path = Path(path_value)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
    handler.name = name
    handler.setLevel(level)
    handler.setFormatter(formatter)
    return handler


def _remove_named_handler(logger: logging.Logger, name: str) -> None:
    for handler in list(logger.handlers):
        if handler.name == name:
            logger.removeHandler(handler)
            handler.close()
