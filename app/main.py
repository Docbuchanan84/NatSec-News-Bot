from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from app.config_loader import ConfigError, ConfigService, validate_env
from app.database import Database
from app.discord_bot import RSSDiscordClient
from app.logging_config import configure_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discord RSS Dispatch Bot")
    parser.add_argument("--validate-config", action="store_true", help="Validate config and exit")
    parser.add_argument("--validate-env", action="store_true", help="Also validate Discord env values")
    parser.add_argument("--init-db", action="store_true", help="Initialize SQLite schema and exit")
    return parser.parse_args()


async def run_bot(config_service: ConfigService, db: Database) -> None:
    config = config_service.load_initial()
    configure_logging(config)
    env_errors = validate_env(config)
    if env_errors:
        raise ConfigError(env_errors)
    db.initialize()
    client = RSSDiscordClient(config_service, db)
    await client.start(os.environ["DISCORD_BOT_TOKEN"])


def main() -> int:
    load_dotenv()
    configure_logging()
    args = parse_args()
    config_path = Path(os.environ.get("CONFIG_PATH", "config/config.json"))
    database_path = Path(os.environ.get("DATABASE_PATH", "data/rssbot.sqlite"))
    config_service = ConfigService(config_path)
    db = Database(database_path)
    try:
        if args.init_db:
            db.initialize()
            print(f"Initialized database: {database_path}")
            return 0
        config = config_service.load_initial()
        configure_logging(config)
        if args.validate_env:
            env_errors = validate_env(config)
            if env_errors:
                raise ConfigError(env_errors)
        if args.validate_config:
            print(f"Config OK: {len(config.channels)} channels loaded from {config_path}")
            return 0
        asyncio.run(run_bot(config_service, db))
        return 0
    except ConfigError as exc:
        for error in exc.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
