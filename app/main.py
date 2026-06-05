from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
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

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discord RSS Dispatch Bot")
    parser.add_argument("--validate-config", action="store_true", help="Validate config and exit")
    parser.add_argument("--validate-env", action="store_true", help="Also validate Discord env values")
    parser.add_argument("--init-db", action="store_true", help="Initialize SQLite schema and exit")
    parser.add_argument("--maintain-db", action="store_true", help="Prune and optimize SQLite runtime history, then exit")
    parser.add_argument("--vacuum-db", action="store_true", help="Run VACUUM with --maintain-db or at startup when configured")
    parser.add_argument("--validate-routing", action="store_true", help="Validate routing config and exit")
    parser.add_argument("--route-test-title", help="Run routing against a supplied title and exit")
    parser.add_argument("--route-test-summary", help="Optional summary/stub for --route-test-title")
    parser.add_argument("--route-test-source", help="Optional source/feed name for --route-test-title")
    parser.add_argument("--route-test-source-id", help="Optional stable source ID for --route-test-title")
    parser.add_argument("--route-test-source-class", help="Optional source class for --route-test-title")
    parser.add_argument("--route-test-url", help="Optional URL for --route-test-title")
    parser.add_argument("--route-backtest", type=int, metavar="LIMIT", help="Run routing against recent articles")
    parser.add_argument("--routing-diagnostics", action="store_true", help="Compare app config, feed metadata, and routing rules")
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
    if config.settings.maintenance.vacuum_on_startup:
        logger.info("Running startup database VACUUM")
        db.vacuum()
    client = RSSDiscordClient(config_service, db)
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    client_task = asyncio.create_task(client.start(os.environ["DISCORD_BOT_TOKEN"]))
    stop_task = asyncio.create_task(stop_event.wait())
    done, _pending = await asyncio.wait({client_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    if stop_task in done and not client_task.done():
        logger.info("Shutdown signal received; closing Discord client")
        await client.close()
        await client_task
    else:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        await client_task


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
        if args.maintain_db:
            db.initialize()
            before = db.database_size_bytes()
            maintenance = config.settings.maintenance
            stats = db.prune_runtime_history(
                article_retention_days=maintenance.article_retention_days,
                posted_retention_days=maintenance.posted_retention_days,
                non_post_retention_hours=maintenance.non_post_retention_hours,
                seen_retention_days=maintenance.seen_retention_days,
                feed_entry_seen_retention_days=maintenance.feed_entry_seen_retention_days,
                article_batch_size=maintenance.article_batch_size,
            )
            if maintenance.optimize_on_maintenance:
                db.optimize()
            if args.vacuum_db:
                db.vacuum()
            after = db.database_size_bytes()
            print(f"Database maintenance complete: {database_path}")
            print(f"Size: {before} -> {after} bytes")
            for key in sorted(stats):
                if stats[key]:
                    print(f"{key}: {stats[key]}")
            return 0
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
        if args.routing_diagnostics:
            routing_config = load_routing_config(config.settings.routing.config_dir, config)
            print(format_routing_diagnostics(config, routing_config))
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
                    source_id=args.route_test_source_id,
                    source_class=args.route_test_source_class,
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
                        summary=row["summary"],
                        source_name=row["source_name"],
                        source_id=row["source_id"],
                        source_class=row["source_class"],
                        url=row["url"],
                        normalized_title=row["normalized_title"],
                    )
                )
                results.append((int(row["id"]), row["title"], decision))
            print(format_backtest_summary(results, limit=10000))
            return 0
        asyncio.run(run_bot(config_service, db))
        return 0
    except KeyboardInterrupt:
        print("Shutdown requested.", file=sys.stderr)
        return 130
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


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def request_stop(*_args: object) -> None:
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, request_stop)


def format_routing_diagnostics(config, routing_config) -> str:
    channel_keys = {channel.key for channel in config.channels}
    rule_keys = {rule.channel_key for rule in routing_config.channel_rules}
    feed_source_ids = {
        feed.source_id
        for feed in list(config.feeds) + [feed for channel in config.channels for feed in channel.feeds]
    }
    lines = [
        "Routing diagnostics",
        f"channels={len(channel_keys)} rules={len(rule_keys)} top_level_feeds={len(config.feeds)}",
    ]
    missing_channels = sorted(rule_keys - channel_keys)
    channels_without_rules = sorted(channel_keys - rule_keys)
    if missing_channels:
        lines.append("Routing rules missing config channels: " + ", ".join(missing_channels))
    else:
        lines.append("Routing rules missing config channels: none")
    if channels_without_rules:
        lines.append("Config channels without routing rules: " + ", ".join(channels_without_rules))
    else:
        lines.append("Config channels without routing rules: none")
    missing_source_metadata = [
        feed.name
        for feed in list(config.feeds) + [feed for channel in config.channels for feed in channel.feeds]
        if feed.source_id == "unknown" or feed.source_class == "unknown"
    ][:20]
    lines.append(
        "Feeds with missing source metadata: "
        + (", ".join(missing_source_metadata) if missing_source_metadata else "none")
    )
    destination_only = sorted(channel.key for channel in config.channels if not channel.feeds)
    lines.append("Destination-only channels: " + (", ".join(destination_only) if destination_only else "none"))
    legacy_channels = sorted(channel.key for channel in config.channels if channel.feeds)
    lines.append("Legacy channel-scoped feeds detected: " + (", ".join(legacy_channels) if legacy_channels else "none"))
    mirror_without_sources = []
    for rule in routing_config.channel_rules:
        if rule.destination_class != "mirror":
            continue
        required_ids = set(rule.required_source_ids)
        if required_ids and not (required_ids & feed_source_ids):
            mirror_without_sources.append(rule.channel_key)
    lines.append(
        "Source mirrors with no matching source IDs: "
        + (", ".join(sorted(mirror_without_sources)) if mirror_without_sources else "none")
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
