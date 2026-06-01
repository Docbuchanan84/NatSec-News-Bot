from __future__ import annotations

import logging
import os
import time
import json
from datetime import UTC
from datetime import datetime

import discord
from discord import app_commands

from app.config_loader import ConfigError, ConfigService
from app.database import Database
from app.logging_config import configure_logging
from app.models import PostJob
from app.publisher import PublisherAdapter, PublisherService
from app.routing import RoutingConfigError, RoutingEngine, load_routing_config
from app.routing.models import RoutingArticle
from app.routing.reporting import format_backtest_summary, format_decision, truncate
from app.scheduler import SchedulerService

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("app.audit")


class DiscordPublisherAdapter(PublisherAdapter):
    def __init__(self, client: discord.Client) -> None:
        self.client = client

    async def send(self, job: PostJob) -> str:
        channel = self.client.get_channel(int(job.channel_id))
        if channel is None:
            channel = await self.client.fetch_channel(int(job.channel_id))
        if not hasattr(channel, "send"):
            raise RuntimeError(f"Configured channel {job.channel_id} cannot receive messages.")
        if job.timestamp_status in {"valid", "timezone_corrected"}:
            display_timestamp = job.normalized_published_at.astimezone(UTC)
            footer = f"{job.source_name} · published"
        else:
            display_timestamp = datetime.now(UTC)
            footer = f"{job.source_name} · detected"
        embed = discord.Embed(
            title=job.title[:256],
            url=job.url,
            description=(job.summary or "")[:4096] if job.summary else None,
            timestamp=display_timestamp,
        )
        embed.set_footer(text=footer)
        if getattr(self.client, "debug_mode_enabled", False):
            debug_text = _format_routing_debug_field(self.client.db, job.article_id)
            if debug_text:
                embed.add_field(name="Routing Debug", value=debug_text[:1024], inline=False)
                audit_logger.info(
                    "routing_debug_embed article_id=%s channel_id=%s title=%r debug=%r",
                    job.article_id,
                    job.channel_id,
                    job.title,
                    debug_text,
                )
        message = await channel.send(embed=embed)
        return str(message.id)


class RSSDiscordClient(discord.Client):
    def __init__(self, config_service: ConfigService, db: Database) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.config_service = config_service
        self.db = db
        self.publisher = PublisherService(db, DiscordPublisherAdapter(self))
        self.scheduler = SchedulerService(db, self.publisher)
        self.started_at = time.monotonic()
        self.debug_mode_enabled = _env_bool("ROUTING_DEBUG_EMBEDS", default=False)
        audit_logger.info("routing_debug_mode_initial enabled=%s", self.debug_mode_enabled)

    async def setup_hook(self) -> None:
        config = self.config_service.active_config
        if config is None:
            raise RuntimeError("Config must be loaded before Discord client setup.")
        self.publisher.configure(config)
        self.scheduler.configure(config)
        self.scheduler.start()
        self._register_commands()
        guild_id = os.environ.get(config.discord.guild_id_env)
        if guild_id:
            guild = discord.Object(id=int(guild_id))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def close(self) -> None:
        await self.scheduler.shutdown()
        await self.publisher.shutdown()
        await super().close()

    async def on_ready(self) -> None:
        logger.info("Connected to Discord as %s", self.user)

    def _register_commands(self) -> None:
        group = app_commands.Group(name="rss", description="RSS dispatch bot commands")

        @group.command(name="status", description="Show RSS bot status")
        async def status(interaction: discord.Interaction) -> None:
            config = self.config_service.active_config
            if config is None:
                await interaction.response.send_message("No active config.", ephemeral=True)
                return
            uptime_seconds = int(time.monotonic() - self.started_at)
            queue_total = sum(stat.size for stat in self.publisher.queue_stats())
            status_rows = self.db.feed_status_rows(limit=5)
            lines = [
                "RSS Dispatch Bot Status",
                "",
                f"Uptime: {uptime_seconds // 60}m {uptime_seconds % 60}s",
                f"Channels: {len(config.channels)} configured",
                f"Unique feeds: {len(self.scheduler.feeds)}",
                f"Queue: {queue_total} pending",
                "",
                "Recent feed status:",
            ]
            for row in status_rows:
                state = "OK" if int(row["consecutive_failures"] or 0) == 0 else "FAILING"
                detail = row["last_error"] or f"last success {row['last_success_at'] or 'never'}"
                lines.append(f"{row['feed_name'] or row['feed_key']}: {state}, {detail}")
            await interaction.response.send_message("\n".join(lines), ephemeral=True)

        @group.command(name="reload", description="Reload and validate config/config.json")
        async def reload_config(interaction: discord.Interaction) -> None:
            try:
                config = self.config_service.reload()
            except ConfigError as exc:
                await interaction.response.send_message(
                    "Config reload failed. Previous config is still active.\n\nError:\n" + "\n".join(exc.errors[:10]),
                    ephemeral=True,
                )
                return
            configure_logging(config)
            self.publisher.configure(config)
            self.scheduler.configure(config)
            await interaction.response.send_message(
                f"Config reloaded: {len(config.channels)} channels, {len(self.scheduler.feeds)} unique feeds.",
                ephemeral=True,
            )

        @group.command(name="refresh", description="Force refresh for this configured Discord channel")
        async def refresh(interaction: discord.Interaction) -> None:
            channel_id = str(interaction.channel_id)
            await interaction.response.defer(ephemeral=True)
            try:
                summary = await self.scheduler.refresh_channel(channel_id)
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return
            await interaction.followup.send(
                "\n".join(
                    [
                        f"Refresh complete for <#{channel_id}>:",
                        f"{summary.feeds_checked} feeds checked",
                        f"{summary.new_articles} new articles seen",
                        f"{summary.posts_queued} posts queued",
                        f"{summary.duplicates_skipped} duplicates skipped",
                        f"{summary.errors} errors",
                    ]
                ),
                ephemeral=True,
            )

        @group.command(name="testpost", description="Send one controlled test embed in this configured channel")
        async def testpost(interaction: discord.Interaction) -> None:
            channel_id = str(interaction.channel_id)
            if channel_id not in self.scheduler.channel_to_feed_keys:
                await interaction.response.send_message(
                    "This Discord channel is not configured for RSS test posts.",
                    ephemeral=True,
                )
                return
            job = self.db.create_test_article(channel_id)
            queued = await self.publisher.enqueue(job)
            if not queued:
                await interaction.response.send_message("Test post could not be queued.", ephemeral=True)
                return
            await interaction.response.send_message("Queued one RSS test post for this channel.", ephemeral=True)

        @group.command(name="route-test", description="Preview routing for a supplied article title")
        @app_commands.describe(
            title="Article title to test",
            summary="Optional article summary or stub",
            source="Optional source/feed name",
            url="Optional article URL",
        )
        async def route_test(
            interaction: discord.Interaction,
            title: str,
            summary: str | None = None,
            source: str | None = None,
            url: str | None = None,
        ) -> None:
            try:
                engine = self._routing_engine_for_command()
            except RoutingConfigError as exc:
                await self._send_routing_config_error(interaction, exc)
                return
            decision = engine.route(RoutingArticle(title=title, summary=summary, source_name=source, url=url))
            await interaction.response.send_message(format_decision(decision), ephemeral=True)

        @group.command(name="route-article", description="Preview routing for an article already in SQLite")
        @app_commands.describe(article_id="Article ID from the SQLite articles table")
        async def route_article(interaction: discord.Interaction, article_id: int) -> None:
            try:
                engine = self._routing_engine_for_command()
            except RoutingConfigError as exc:
                await self._send_routing_config_error(interaction, exc)
                return
            row = self.db.get_article_for_routing(article_id)
            if row is None:
                await interaction.response.send_message(f"Article not found: {article_id}", ephemeral=True)
                return
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
            await interaction.response.send_message(format_decision(decision), ephemeral=True)

        @group.command(name="route-backtest", description="Backtest routing against recent SQLite articles")
        @app_commands.describe(limit="Number of recent articles to test, max 100")
        async def route_backtest(interaction: discord.Interaction, limit: int = 25) -> None:
            await interaction.response.defer(ephemeral=True)
            try:
                engine = self._routing_engine_for_command()
            except RoutingConfigError as exc:
                await self._send_routing_config_error(interaction, exc, followup=True)
                return
            bounded_limit = max(1, min(limit, 100))
            results = []
            for row in self.db.recent_articles_for_routing(limit=bounded_limit):
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
            await interaction.followup.send(format_backtest_summary(results), ephemeral=True)

        @group.command(name="routing-status", description="Show routing config and validation status")
        async def routing_status(interaction: discord.Interaction) -> None:
            config = self.config_service.active_config
            if config is None:
                await interaction.response.send_message("No active config.", ephemeral=True)
                return
            try:
                routing_config = load_routing_config(config.settings.routing.config_dir, config)
                status = "valid"
                detail = [
                    f"Routing enabled: {config.settings.routing.enabled}",
                    f"Routing mode: {config.settings.routing.mode}",
                    f"Validation: {status}",
                    f"Taxonomy version: {routing_config.taxonomy_version}",
                    f"Knowledge base version: {routing_config.knowledge_base_version}",
                    f"Channel rules: {len(routing_config.channel_rules)}",
                    f"Loaded tags: {len(routing_config.taxonomy)}",
                    f"Loaded knowledge entries: {len(routing_config.knowledge_entries)}",
                    f"Recent routing errors: {self.db.recent_routing_error_count()}",
                ]
            except RoutingConfigError as exc:
                detail = [
                    f"Routing enabled: {config.settings.routing.enabled}",
                    f"Routing mode: {config.settings.routing.mode}",
                    "Validation: invalid",
                    "Errors:",
                    truncate("\n".join(exc.errors), 1500),
                ]
            await interaction.response.send_message("\n".join(detail), ephemeral=True)

        self.tree.add_command(group)

        @self.tree.command(name="debugmode", description="Toggle routing score details on RSS embeds")
        @app_commands.describe(enabled="Show routing score details on future RSS embeds")
        async def debugmode(interaction: discord.Interaction, enabled: bool) -> None:
            await interaction.response.defer(ephemeral=True)
            self.debug_mode_enabled = bool(enabled)
            state = "enabled" if self.debug_mode_enabled else "disabled"
            audit_logger.info(
                "routing_debug_mode_changed enabled=%s user_id=%s guild_id=%s channel_id=%s",
                self.debug_mode_enabled,
                interaction.user.id if interaction.user else None,
                interaction.guild_id,
                interaction.channel_id,
            )
            await interaction.followup.send(f"Routing embed debug mode {state}.", ephemeral=True)

    def _routing_engine_for_command(self) -> RoutingEngine:
        config = self.config_service.active_config
        if config is None:
            raise RoutingConfigError(["No active config."])
        return RoutingEngine(load_routing_config(config.settings.routing.config_dir, config))

    async def _send_routing_config_error(
        self,
        interaction: discord.Interaction,
        exc: RoutingConfigError,
        followup: bool = False,
    ) -> None:
        message = "Routing config is invalid:\n" + truncate("\n".join(exc.errors), 1800)
        if followup:
            await interaction.followup.send(message, ephemeral=True)
            return
        await interaction.response.send_message(message, ephemeral=True)


def _format_routing_debug_field(db: Database, article_id: int) -> str | None:
    row = db.latest_routing_decision_for_article(article_id)
    if row is None:
        return None
    try:
        selected = json.loads(row["selected_channel_keys"] or "[]")
        scores = json.loads(row["score_details"] or "[]")
        matches = json.loads(row["matched_entries"] or "[]")
        tags = json.loads(row["emitted_tags"] or "[]")
    except (TypeError, json.JSONDecodeError):
        return None

    selected_set = set(selected)
    selected_scores = [score for score in scores if score.get("channel_key") in selected_set]
    contender_scores = [
        score
        for score in scores
        if score.get("channel_key") not in selected_set and int(score.get("score") or 0) > 0
    ][:2]

    lines = [
        f"Decision: {str(row['decision_status']).upper()} -> {', '.join(selected) or 'none'}",
        f"Top score: {row['top_score']}",
    ]
    if matches:
        match_text = ", ".join(
            f"{match.get('knowledge_entry_id')}='{match.get('matched_alias')}'" for match in matches[:4]
        )
        if len(matches) > 4:
            match_text += f", +{len(matches) - 4} more"
        lines.append(f"Matches: {match_text}")
    if tags:
        tag_text = ", ".join(tags[:10])
        if len(tags) > 10:
            tag_text += f", +{len(tags) - 10} more"
        lines.append(f"Tags: {tag_text}")
    if selected_scores:
        lines.append("Selected because:")
        for score in selected_scores[:2]:
            lines.append(_format_score_line(score))
    if contender_scores:
        lines.append("Next closest:")
        for score in contender_scores:
            lines.append(_format_score_line(score))
    return _fit_embed_field(lines)


def _format_score_line(score: dict) -> str:
    channel_key = score.get("channel_key", "unknown")
    score_value = score.get("score", 0)
    minimum = score.get("minimum_score", 0)
    reasons = [reason for reason in score.get("reasons", []) if not str(reason).startswith("required_any")]
    reason_text = "; ".join(str(reason) for reason in reasons[:3]) or "no score contributions"
    if len(reasons) > 3:
        reason_text += "; ..."
    return f"- {channel_key}: {score_value}/{minimum} ({reason_text})"


def _fit_embed_field(lines: list[str], limit: int = 1024) -> str:
    output: list[str] = []
    current = 0
    for line in lines:
        line_len = len(line) + (1 if output else 0)
        if current + line_len > limit:
            remaining = limit - current
            if remaining > 20:
                output.append("... truncated")
            break
        output.append(line)
        current += line_len
    return "\n".join(output)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}
