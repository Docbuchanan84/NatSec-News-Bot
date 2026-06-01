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
from app.routing import RoutingConfigError, RoutingEngine, load_routing_config
from app.routing.bootstrap import bootstrap_routing_config, recent_seed_report
from app.routing.models import RoutingArticle
from app.routing.reporting import format_backtest_summary, format_decision


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discord RSS Dispatch Bot")
    parser.add_argument("--validate-config", action="store_true", help="Validate config and exit")
    parser.add_argument("--validate-env", action="store_true", help="Also validate Discord env values")
    parser.add_argument("--init-db", action="store_true", help="Initialize SQLite schema and exit")
    parser.add_argument("--validate-routing", action="store_true", help="Validate routing config and exit")
    parser.add_argument("--route-test-title", help="Run routing against a supplied title and exit")
    parser.add_argument("--route-test-summary", help="Optional summary/stub for --route-test-title")
    parser.add_argument("--route-test-source", help="Optional source/feed name for --route-test-title")
    parser.add_argument("--route-test-url", help="Optional URL for --route-test-title")
    parser.add_argument("--route-backtest", type=int, metavar="LIMIT", help="Run routing against recent articles")
    parser.add_argument(
        "--bootstrap-routing-config",
        action="store_true",
        help="Create starter routing config files when they do not exist",
    )
    parser.add_argument(
        "--force-bootstrap-routing-config",
        action="store_true",
        help="Allow --bootstrap-routing-config to overwrite existing routing files",
    )
    parser.add_argument("--bootstrap-routing-days", type=int, default=7, help="Days of DB history to inspect while bootstrapping")
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
        if args.validate_routing:
            routing_config = load_routing_config(config.settings.routing.config_dir, config)
            print(
                "Routing OK: "
                f"{len(routing_config.taxonomy)} tags, "
                f"{len(routing_config.knowledge_entries)} knowledge entries, "
                f"{len(routing_config.channel_rules)} channel rules loaded from {config.settings.routing.config_dir}"
            )
            return 0
        if args.bootstrap_routing_config:
            db.initialize()
            print(
                bootstrap_routing_config(
                    config,
                    db,
                    config.settings.routing.config_dir,
                    force=args.force_bootstrap_routing_config,
                    days=args.bootstrap_routing_days,
                )
            )
            print(recent_seed_report(db, days=args.bootstrap_routing_days))
            return 0
        if args.route_test_title:
            routing_config = load_routing_config(config.settings.routing.config_dir, config)
            engine = RoutingEngine(routing_config)
            decision = engine.route(
                RoutingArticle(
                    title=args.route_test_title,
                    summary=args.route_test_summary,
                    source_name=args.route_test_source,
                    url=args.route_test_url,
                )
            )
            print(format_decision(decision, limit=10000))
            return 0
        if args.route_backtest is not None:
            db.initialize()
            routing_config = load_routing_config(config.settings.routing.config_dir, config)
            engine = RoutingEngine(routing_config)
            limit = max(1, min(args.route_backtest, 500))
            results = []
            for row in db.recent_articles_for_routing(limit=limit):
                decision = engine.route(
                    RoutingArticle(
                        article_id=int(row["id"]),
                        title=row["title"],
                        summary=None,
                        source_name=row["source_name"],
                        url=row["url"],
                        normalized_title=row["normalized_title"],
                    )
                )
                results.append((int(row["id"]), row["title"], decision))
            print(format_backtest_summary(results, limit=10000))
            return 0
        asyncio.run(run_bot(config_service, db))
        return 0
    except ConfigError as exc:
        for error in exc.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    except RoutingConfigError as exc:
        for error in exc.errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 3
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
