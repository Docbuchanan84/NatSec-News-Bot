from __future__ import annotations

import logging
import os
import json
import re
import time
from datetime import UTC
from datetime import datetime
from urllib.parse import urlparse

import discord
from discord import app_commands

from app.config_loader import ConfigError, ConfigService
from app.database import Database
from app.feed_fetcher import clean_html_text
from app.logging_config import configure_logging
from app.models import PostJob
from app.publisher import PublisherAdapter, PublisherService
from app.routing import RoutingConfigError, RoutingEngine, load_routing_config
from app.routing.models import RoutingArticle
from app.routing.reporting import format_backtest_summary, format_decision, truncate
from app.scheduler import SchedulerService

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("app.audit")
URLISH_RE = re.compile(r"^(https?://\S+|[\w.-]+\.[a-z]{2,}/\S*)$", re.IGNORECASE)
LINK_LABEL_RE = re.compile(r"^(more|watch and subscribe|read more|full story|link)\s*:?\s*", re.IGNORECASE)
REVIEW_CHANNEL_ID = "1511541774642843789"


class DiscordPublisherAdapter(PublisherAdapter):
    def __init__(self, client: discord.Client) -> None:
        self.client = client

    async def send(self, job: PostJob) -> str:
        channel = self.client.get_channel(int(job.channel_id))
        if channel is None:
            channel = await self.client.fetch_channel(int(job.channel_id))
        if not hasattr(channel, "send"):
            raise RuntimeError(f"Configured channel {job.channel_id} cannot receive messages.")
        title = clean_html_text(job.title) or job.title
        description = clean_html_text(job.summary) if job.summary else None
        description = _dedupe_description(title, description)
        if job.timestamp_status in {"valid", "timezone_corrected"}:
            display_timestamp = job.normalized_published_at.astimezone(UTC)
            footer = f"{job.source_name} · published"
        else:
            display_timestamp = datetime.now(UTC)
            footer = f"{job.source_name} · detected"
        embed = discord.Embed(
            title=title[:256],
            url=job.url,
            description=description[:4096] if description else None,
            timestamp=display_timestamp,
        )
        if job.image_url:
            embed.set_image(url=job.image_url)
        embed.set_footer(text=footer)
        if getattr(self.client, "debug_mode_enabled", False) or job.channel_id == REVIEW_CHANNEL_ID:
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
        content = job.url if _is_video_reference(job.url) else None
        message = await channel.send(content=content, embed=embed)
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
            health = self.db.feed_health_summary()
            status_rows = self.db.feed_status_rows(limit=8, failures_first=True)
            next_poll = self.scheduler.next_poll_at()
            next_poll_text = _format_relative_seconds((next_poll - datetime.now(UTC)).total_seconds()) if next_poll else "unknown"
            recent_posts = self.db.recent_post_count(hours=24)
            routing_state = (
                f"{config.settings.routing.mode}" if config.settings.routing.enabled else "off"
            )
            lines = [
                "**RSS Dispatch Bot Status**",
                f"Uptime: {_format_duration(uptime_seconds)}",
                f"Channels: {len(config.channels)} | Unique feeds: {len(self.scheduler.feeds)} | Tracked feeds: {health['tracked']}",
                f"Feed health: {health['healthy']} healthy, {health['failing']} failing, {health['never_succeeded']} never succeeded",
                f"Queue: {queue_total} pending | Posted last 24h: {recent_posts}",
                f"Next poll: {next_poll_text} | Routing: {routing_state} | Debug embeds: {'on' if self.debug_mode_enabled else 'off'}",
                "",
                "**Feed watchlist**",
            ]
            for row in status_rows:
                failures = int(row["consecutive_failures"] or 0)
                state = "OK" if failures == 0 and row["last_success_at"] else ("NEW" if failures == 0 else f"FAIL x{failures}")
                detail = row["last_error"] or f"success {row['last_success_at'] or 'never'}"
                lines.append(f"{state}: {row['feed_name'] or row['feed_key']} - {truncate(str(detail), 160)}")
            message = truncate("\n".join(lines), 1900)
            await interaction.response.send_message(message, ephemeral=True)

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
            source_id="Optional stable source ID",
            source_class="Optional source class",
            url="Optional article URL",
        )
        async def route_test(
            interaction: discord.Interaction,
            title: str,
            summary: str | None = None,
            source: str | None = None,
            source_id: str | None = None,
            source_class: str | None = None,
            url: str | None = None,
        ) -> None:
            try:
                engine = self._routing_engine_for_command()
            except RoutingConfigError as exc:
                await self._send_routing_config_error(interaction, exc)
                return
            decision = engine.route(
                RoutingArticle(
                    title=title,
                    summary=summary,
                    source_name=source,
                    source_id=source_id,
                    source_class=source_class,
                    url=url,
                )
            )
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
                    summary=row["summary"],
                    source_name=row["source_name"],
                    source_id=row["source_id"],
                    source_class=row["source_class"],
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
                        summary=row["summary"],
                        source_name=row["source_name"],
                        source_id=row["source_id"],
                        source_class=row["source_class"],
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

        @group.command(name="explain", description="Show the latest persisted routing explanation for an article")
        @app_commands.describe(article_id="Article ID from the SQLite articles table")
        async def explain(interaction: discord.Interaction, article_id: int) -> None:
            row = self.db.latest_routing_decision_for_article(article_id)
            if row is None:
                await interaction.response.send_message(f"No routing decision recorded for article {article_id}.", ephemeral=True)
                return
            await interaction.response.send_message(_format_persisted_routing_explanation(row), ephemeral=True)

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
        selected = json.loads(row["final_channel_keys"] or row["selected_channel_keys"] or "[]")
        scores = json.loads(row["score_details"] or "[]")
        matches = json.loads(row["matched_entries"] or "[]")
        tags = json.loads(row["emitted_tags"] or "[]")
        expanded_tags = json.loads(row["expanded_tags"] or "[]")
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
        f"Reason: {row['reason'] or 'none'}",
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
    if expanded_tags:
        expanded_text = ", ".join(expanded_tags[:10])
        if len(expanded_tags) > 10:
            expanded_text += f", +{len(expanded_tags) - 10} more"
        lines.append(f"Expanded: {expanded_text}")
    if selected_scores:
        lines.append("Selected because:")
        for score in selected_scores[:2]:
            lines.append(_format_score_line(score))
    if contender_scores:
        lines.append("Next closest:")
        for score in contender_scores:
            lines.append(_format_score_line(score))
    return _fit_embed_field(lines)


def _format_persisted_routing_explanation(row) -> str:
    try:
        final = json.loads(row["final_channel_keys"] or row["selected_channel_keys"] or "[]")
        primary = json.loads(row["primary_channel_keys"] or "[]")
        mirrors = json.loads(row["mirror_channel_keys"] or "[]")
        review = json.loads(row["review_channel_keys"] or "[]")
        scores = json.loads(row["score_details"] or "[]")
        matches = json.loads(row["matched_entries"] or "[]")
        emitted = json.loads(row["emitted_tags"] or "[]")
        expanded = json.loads(row["expanded_tags"] or "[]")
        explanation = json.loads(row["explanation"] or "[]")
    except (TypeError, json.JSONDecodeError):
        return "Routing decision exists, but stored JSON could not be parsed."
    lines = [
        f"Decision: {row['decision_status']}",
        f"Reason: {row['reason'] or 'none'}",
        f"Final: {', '.join(final) or 'none'}",
        f"Primary: {', '.join(primary) or 'none'}",
        f"Mirrors: {', '.join(mirrors) or 'none'}",
        f"Review: {', '.join(review) or 'none'}",
        f"Emitted tags: {', '.join(emitted) or 'none'}",
        f"Expanded tags: {', '.join(expanded) or 'none'}",
        "Matches: "
        + (
            ", ".join(f"{match.get('knowledge_entry_id')} ({match.get('matched_alias')})" for match in matches[:8])
            if matches
            else "none"
        ),
        "Top scores:",
    ]
    for score in scores[:8]:
        lines.append(_format_score_line(score))
    if explanation:
        lines.append("Explanation:")
        lines.extend(str(item) for item in explanation[:8])
    return truncate("\n".join(lines), 1900)


def _format_score_line(score: dict) -> str:
    channel_key = score.get("channel_key", "unknown")
    destination_class = score.get("destination_class", "primary")
    score_value = score.get("score", 0)
    minimum = score.get("minimum_score", 0)
    reasons = [reason for reason in score.get("reasons", []) if not str(reason).startswith("required_any")]
    reason_text = "; ".join(str(reason) for reason in reasons[:3]) or "no score contributions"
    if len(reasons) > 3:
        reason_text += "; ..."
    return f"- {channel_key} [{destination_class}]: {score_value}/{minimum} ({reason_text})"


def _same_display_text(left: str, right: str) -> bool:
    return " ".join(left.split()).casefold() == " ".join(right.split()).casefold()


def _dedupe_description(title: str, description: str | None) -> str | None:
    if not description:
        return None
    if _same_display_text(title, description):
        return None
    if _starts_with_display_text(description, title):
        remainder = description[len(title) :].strip()
        if not remainder or _link_only_text(remainder):
            return None
        return remainder
    return description


def _starts_with_display_text(text: str, prefix: str) -> bool:
    return " ".join(text.split()).casefold().startswith(" ".join(prefix.split()).casefold())


def _link_only_text(value: str) -> bool:
    normalized = LINK_LABEL_RE.sub("", " ".join(value.split())).strip()
    if not normalized:
        return True
    return all(URLISH_RE.match(token) for token in normalized.split())


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


def _is_video_reference(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    return host in {"youtube.com", "youtu.be"} or host.endswith(".youtube.com")


def _format_duration(seconds: int) -> str:
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, secs = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {secs}s"


def _format_relative_seconds(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"in {seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 60:
        return f"in {minutes}m {secs}s"
    hours, minutes = divmod(minutes, 60)
    return f"in {hours}h {minutes}m"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}
