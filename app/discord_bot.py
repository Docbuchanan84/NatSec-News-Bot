from __future__ import annotations

import logging
import os
import json
import re
import time
from datetime import UTC
from datetime import datetime
from urllib.parse import unquote, urlparse

import discord
from discord import app_commands

from app.config_loader import ConfigError, ConfigService
from app.database import Database
from app.feed_fetcher import clean_html_text
from app.logging_config import configure_logging
from app.models import PostJob
from app.publisher import PublisherAdapter, PublisherService
from app.routing import RoutingConfigError, RoutingEngine, load_routing_config
from app.routing.importance import (
    ImportanceTerm,
    apply_importance,
    build_importance_config,
    default_importance_terms,
    normalize_watch_term,
)
from app.routing.models import RoutingArticle
from app.routing.reporting import format_backtest_summary, format_decision, truncate
from app.scheduler import SchedulerService
from app.social_link_embed import SocialLinkEmbedService
from app.x_media import PreparedMedia, prepared_remote_media_files, prepared_x_media_files

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("app.audit")
URLISH_RE = re.compile(r"^(https?://\S+|[\w.-]+\.[a-z]{2,}/\S*)$", re.IGNORECASE)
URLISH_TITLE_RE = re.compile(r"^(?:https?://)?(?:www\.)?[\w.-]+\.[a-z]{2,}(?:[/\?#].*)?$", re.IGNORECASE)
LINK_LABEL_RE = re.compile(r"^(more|watch and subscribe|read more|full story|link)\s*:?\s*", re.IGNORECASE)
YOUTUBE_MARKETING_LINE_RE = re.compile(
    r"^(?:"
    r"subscribe\s+to\s+our\s+(?:youtube\s+)?channel\b|"
    r"follow\s+us\s+on\b|"
    r"find\s+us\s+on\b|"
    r"like\s+us\s+on\b|"
    r"check\s+(?:our\s+website|out\s+our\s+instagram\s+page)\b|"
    r"download\s+(?:aje\s+)?mobile\s+app\b|"
    r"for\s+more\s+content\s+go\s+to\b|"
    r"listen\s+to\s+.{0,80}\bpodcast\b|"
    r"sky\s+news\s+daily\s+podcast\b|"
    r"to\s+enquire\s+about\s+licensing\b"
    r")",
    re.IGNORECASE,
)
HASHTAG_ONLY_RE = re.compile(r"^(?:#[A-Za-z0-9_][\w-]*\s*)+$")
MARKETING_CONTINUATION_URL_RE = re.compile(
    r"(?:podfollow\.com|itunes\.apple\.com|play\.google\.com|youtube\.com/skynews)",
    re.IGNORECASE,
)
REVIEW_CHANNEL_ID = "1511541774642843789"
IMPORTANCE_COLOR_STOPS = (
    (0, 0x808080),
    (3, 0x2ECC71),
    (7, 0xF1C40F),
    (10, 0xE74C3C),
)
TRACKING_TITLE_HOST_FRAGMENTS = (
    "hubspotlinks.com",
    "pardot.",
    "dripemail",
    "sendgrid.net",
    "list-manage.com",
)


class DiscordPublisherAdapter(PublisherAdapter):
    def __init__(self, client: discord.Client) -> None:
        self.client = client

    async def send(self, job: PostJob) -> str:
        channel = self.client.get_channel(int(job.channel_id))
        if channel is None:
            channel = await self.client.fetch_channel(int(job.channel_id))
        if not hasattr(channel, "send"):
            raise RuntimeError(f"Configured channel {job.channel_id} cannot receive messages.")
        embed = _build_post_embed(job, self.client)
        if _social_post_details(job):
            message = await self._send_social_message(channel, job, embed)
        else:
            message = await self._send_message_with_media(channel, job, embed)
        return str(message.id)

    async def send_social_reply(self, job: PostJob, source_message) -> str:
        embed = _build_post_embed(job, self.client)
        message = await self._send_social_message(source_message, job, embed, as_reply=True)
        return str(message.id)

    async def _send_social_message(self, target, job: PostJob, text_embed: discord.Embed, *, as_reply: bool = False):
        send = target.reply if as_reply and hasattr(target, "reply") else target.send
        send_kwargs = {"mention_author": False} if as_reply and hasattr(target, "reply") else {}
        if _should_upload_social_media(job):
            async with prepared_x_media_files(job.rich_metadata or {}) as prepared:
                if prepared:
                    try:
                        return await _send_prepared_media(send, prepared, text_embed, send_kwargs)
                    except discord.HTTPException as exc:
                        logger.warning(
                            "Discord X media upload failed for article_id=%s; falling back to direct media upload: %s",
                            job.article_id,
                            exc,
                        )
        return await self._send_message_with_media(target, job, text_embed, as_reply=as_reply)

    async def _send_message_with_media(
        self,
        target,
        job: PostJob,
        text_embed: discord.Embed,
        *,
        as_reply: bool = False,
    ):
        send = target.reply if as_reply and hasattr(target, "reply") else target.send
        send_kwargs = {"mention_author": False} if as_reply and hasattr(target, "reply") else {}
        media_items = _direct_media_items(job)
        if media_items:
            async with prepared_remote_media_files(media_items, max_files=_media_upload_limit(job)) as prepared:
                if prepared:
                    try:
                        return await _send_prepared_media(send, prepared, text_embed, send_kwargs)
                    except discord.HTTPException as exc:
                        logger.warning(
                            "Discord direct media upload failed for article_id=%s; sending embed without media URL: %s",
                            job.article_id,
                            exc,
                        )
        content = job.url if _is_link_preview_video_reference(job.url) else None
        return await send(content=content, embed=text_embed, **send_kwargs)


def _build_post_embed(job: PostJob, client: discord.Client) -> discord.Embed:
    social_post = _social_post_details(job)
    if social_post:
        title = social_post["account_name"]
        description = social_post["body"]
        embed_url = social_post["post_url"] or job.url
    elif _is_email_post(job):
        title = _clean_embed_title(clean_html_text(job.title) or job.title, job.url, job.source_name)
        description = _format_email_post_description(job)
        embed_url = job.url
    else:
        title = _clean_embed_title(clean_html_text(job.title) or job.title, job.url, job.source_name)
        description = clean_html_text(job.summary) if job.summary else None
        if _is_video_reference(job.url):
            description = _scrub_youtube_description(description)
        description = _dedupe_description(title, description)
        embed_url = job.url
    display_timestamp = job.normalized_published_at.astimezone(UTC)
    footer = _post_footer(job, display_timestamp)
    embed = discord.Embed(
        title=title[:256],
        url=embed_url,
        description=description[:4096] if description else None,
        timestamp=display_timestamp,
        color=_importance_color(job.importance_score),
    )
    embed.set_footer(text=footer)
    if getattr(client, "debug_mode_enabled", False) or job.channel_id == REVIEW_CHANNEL_ID:
        debug_text = _format_routing_debug_field(client.db, job.article_id)
        if debug_text:
            embed.add_field(name="Routing Debug", value=debug_text[:1024], inline=False)
            audit_logger.info(
                "routing_debug_embed article_id=%s channel_id=%s title=%r debug=%r",
                job.article_id,
                job.channel_id,
                job.title,
                debug_text,
            )
    return embed


class RSSDiscordClient(discord.Client):
    def __init__(self, config_service: ConfigService, db: Database) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.config_service = config_service
        self.db = db
        self.discord_publisher = DiscordPublisherAdapter(self)
        self.publisher = PublisherService(db, self.discord_publisher)
        self.scheduler = SchedulerService(db, self.publisher)
        self.social_link_embeds = SocialLinkEmbedService(db, self.discord_publisher)
        self.started_at = time.monotonic()
        self.debug_mode_enabled = _env_bool("ROUTING_DEBUG_EMBEDS", default=False)
        audit_logger.info("routing_debug_mode_initial enabled=%s", self.debug_mode_enabled)

    async def setup_hook(self) -> None:
        config = self.config_service.active_config
        if config is None:
            raise RuntimeError("Config must be loaded before Discord client setup.")
        self.publisher.configure(config)
        self.scheduler.configure(config)
        self.social_link_embeds.configure(config)
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

    async def on_message(self, message: discord.Message) -> None:
        await self.social_link_embeds.handle_message(message)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        if "embeds" not in payload.data:
            return
        try:
            channel = self.get_channel(payload.channel_id) or await self.fetch_channel(payload.channel_id)
            if not hasattr(channel, "fetch_message"):
                return
            message = await channel.fetch_message(payload.message_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
            logger.debug("Could not fetch edited message %s for social link handling: %s", payload.message_id, exc)
            return
        await self.social_link_embeds.handle_message(message)

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
            result_queue = self.scheduler.result_queue_size()
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
                f"Queue: {queue_total} pending posts, {result_queue} fetched results | Posted last 24h: {recent_posts}",
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
            self.scheduler.start()
            self.social_link_embeds.configure(config)
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
            article = RoutingArticle(
                title=title,
                summary=summary,
                source_name=source,
                source_id=source_id,
                source_class=source_class,
                url=url,
            )
            decision = self._apply_importance_for_command(engine.route(article), article)
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
            article = _routing_article_from_row(row)
            decision = self._apply_importance_for_command(engine.route(article), article)
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
                article = _routing_article_from_row(row)
                decision = self._apply_importance_for_command(engine.route(article), article)
                results.append((int(row["id"]), row["title"], decision))
            await interaction.followup.send(format_backtest_summary(results), ephemeral=True)

        @group.command(name="importance-list", description="Show active importance watch terms")
        async def importance_list(interaction: discord.Interaction) -> None:
            terms = build_importance_config(
                self.db.list_importance_watch_terms(include_disabled=True)
            ).watch_terms
            await interaction.response.send_message(_format_importance_terms(terms), ephemeral=True)

        @group.command(name="importance-add", description="Add or update an importance watch term")
        @app_commands.describe(
            term="Word or phrase to boost",
            weight="Importance boost, 1 to 10",
            category="Short category label",
            notes="Optional note for why this term matters",
        )
        async def importance_add(
            interaction: discord.Interaction,
            term: str,
            weight: int,
            category: str = "watch",
            notes: str | None = None,
        ) -> None:
            try:
                row = self.db.upsert_importance_watch_term(
                    term,
                    weight=weight,
                    category=category,
                    notes=notes,
                    enabled=True,
                )
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.send_message(
                f"Importance term active: {row['term']} +{row['weight']} ({row['category']}).",
                ephemeral=True,
            )

        @group.command(name="importance-remove", description="Disable an importance watch term")
        @app_commands.describe(term="Word or phrase to disable")
        async def importance_remove(interaction: discord.Interaction, term: str) -> None:
            try:
                default = _default_importance_term(term)
                self.db.set_importance_watch_term_enabled(
                    term,
                    enabled=False,
                    default_weight=default.weight if default else 1,
                    default_category=default.category if default else "watch",
                )
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.send_message(
                f"Importance term disabled: {normalize_watch_term(term)}.",
                ephemeral=True,
            )

        @group.command(name="importance-enable", description="Enable or disable an importance watch term")
        @app_commands.describe(term="Word or phrase to toggle", enabled="Whether this term should affect scoring")
        async def importance_enable(interaction: discord.Interaction, term: str, enabled: bool) -> None:
            try:
                default = _default_importance_term(term)
                self.db.set_importance_watch_term_enabled(
                    term,
                    enabled=enabled,
                    default_weight=default.weight if default else 1,
                    default_category=default.category if default else "watch",
                )
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            state = "enabled" if enabled else "disabled"
            await interaction.response.send_message(
                f"Importance term {state}: {normalize_watch_term(term)}.",
                ephemeral=True,
            )

        @group.command(name="importance-test", description="Preview importance scoring for a supplied article")
        @app_commands.describe(
            title="Article title to test",
            summary="Optional article summary or stub",
            source="Optional source/feed name",
            source_id="Optional stable source ID",
            source_class="Optional source class",
            url="Optional article URL",
        )
        async def importance_test(
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
            article = RoutingArticle(
                title=title,
                summary=summary,
                source_name=source,
                source_id=source_id,
                source_class=source_class,
                url=url,
            )
            decision = self._apply_importance_for_command(engine.route(article), article)
            await interaction.response.send_message(format_decision(decision), ephemeral=True)

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

    def _apply_importance_for_command(self, decision, article: RoutingArticle):
        return apply_importance(
            decision,
            article,
            build_importance_config(self.db.list_importance_watch_terms(include_disabled=True)),
        )

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


def _routing_article_from_row(row) -> RoutingArticle:
    return RoutingArticle(
        article_id=int(row["id"]),
        title=row["title"],
        summary=row["summary"],
        source_name=row["source_name"],
        source_id=row["source_id"],
        source_class=row["source_class"],
        url=row["url"],
        normalized_title=row["normalized_title"],
        published_at=_parse_datetime(row["normalized_published_at"]),
        ingested_at=_parse_datetime(row["ingested_at"]),
        timestamp_status=row["timestamp_status"] or "valid",
    )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _format_importance_terms(terms: tuple[ImportanceTerm, ...], limit: int = 1900) -> str:
    if not terms:
        return "No active importance watch terms."
    sorted_terms = sorted(terms, key=lambda item: (item.category, -item.weight, item.term))
    lines = ["Active importance watch terms:"]
    for term in sorted_terms[:60]:
        lines.append(f"- {term.term}: +{term.weight} ({term.category})")
    if len(sorted_terms) > 60:
        lines.append(f"... +{len(sorted_terms) - 60} more")
    return truncate("\n".join(lines), limit)


def _default_importance_term(term: str) -> ImportanceTerm | None:
    normalized = normalize_watch_term(term)
    for default in default_importance_terms():
        if normalize_watch_term(default.term) == normalized:
            return default
    return None


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
        importance_reasons = json.loads(row["importance_reasons"] or "[]")
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
        f"Importance: {int(row['importance_score'] or 0)}/10",
    ]
    if importance_reasons:
        lines.append("Importance reasons: " + "; ".join(str(reason) for reason in importance_reasons[:4]))
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
        importance_reasons = json.loads(row["importance_reasons"] or "[]")
    except (TypeError, json.JSONDecodeError):
        return "Routing decision exists, but stored JSON could not be parsed."
    lines = [
        f"Decision: {row['decision_status']}",
        f"Reason: {row['reason'] or 'none'}",
        f"Importance: {int(row['importance_score'] or 0)}/10",
        "Importance reasons: " + ("; ".join(str(reason) for reason in importance_reasons[:8]) or "none"),
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


def _is_email_post(job: PostJob) -> bool:
    metadata = job.rich_metadata or {}
    return str(metadata.get("source") or "").casefold() == "email"


def _format_email_post_description(job: PostJob) -> str | None:
    if not job.summary:
        return None
    title = _clean_embed_title(clean_html_text(job.title) or job.title, job.url, job.source_name)
    useful: list[str] = []
    for raw_line in job.summary.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if not line:
            continue
        if _same_display_text(_clean_embed_title(line, job.url, job.source_name), title):
            continue
        useful.append(line)
        if len(useful) >= 4:
            break
    description = _dedupe_description(title, "\n".join(useful).strip())
    return description[:4096] if description else None


def _post_footer(job: PostJob, display_timestamp: datetime) -> str:
    article_state = "New" if job.is_new_article else "Update"
    status = f"{article_state} · Imp {_clamp_importance(job.importance_score)}"
    if not _is_email_post(job):
        return f"{job.source_name} · {status}"
    metadata = job.rich_metadata or {}
    sender = str(metadata.get("from") or "").strip()
    if not sender:
        return f"{job.source_name} · {status}"
    return f"{job.source_name} · {sender[:80]} · {status}"


def _importance_color(score: int) -> int:
    bounded = _clamp_importance(score)
    for index, (stop_score, stop_color) in enumerate(IMPORTANCE_COLOR_STOPS):
        if bounded == stop_score:
            return stop_color
        if bounded < stop_score:
            previous_score, previous_color = IMPORTANCE_COLOR_STOPS[index - 1]
            span = stop_score - previous_score
            ratio = (bounded - previous_score) / span if span else 0
            return _interpolate_rgb(previous_color, stop_color, ratio)
    return IMPORTANCE_COLOR_STOPS[-1][1]


def _interpolate_rgb(start_color: int, end_color: int, ratio: float) -> int:
    start = ((start_color >> 16) & 0xFF, (start_color >> 8) & 0xFF, start_color & 0xFF)
    end = ((end_color >> 16) & 0xFF, (end_color >> 8) & 0xFF, end_color & 0xFF)
    channels = tuple(round(start_part + (end_part - start_part) * ratio) for start_part, end_part in zip(start, end))
    return (channels[0] << 16) | (channels[1] << 8) | channels[2]


def _clamp_importance(score: int) -> int:
    return max(0, min(10, int(score)))


def _clean_embed_title(title: str, url: str | None, source_name: str | None) -> str:
    cleaned = " ".join(title.replace("**", "").split()).strip() or "Untitled article"
    if not _looks_like_url_title(cleaned):
        return cleaned
    slug_title = _title_from_url_path(url or cleaned)
    if slug_title:
        return slug_title
    return source_name or "Article"


def _looks_like_url_title(value: str) -> bool:
    cleaned = " ".join(value.split()).strip(" .:-")
    if not cleaned:
        return False
    if URLISH_RE.match(cleaned):
        return True
    return bool(URLISH_TITLE_RE.match(cleaned))


def _title_from_url_path(value: str) -> str | None:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    host = parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    if any(fragment in host for fragment in TRACKING_TITLE_HOST_FRAGMENTS):
        return None
    segments = [unquote(segment).strip() for segment in parsed.path.split("/") if segment.strip()]
    if not segments:
        return None
    slug = re.sub(r"\.[a-z0-9]{2,5}$", "", segments[-1], flags=re.IGNORECASE)
    slug = re.sub(r"[-_+]+", " ", slug)
    slug = re.sub(r"\s+", " ", slug).strip(" .:-")
    if len(re.findall(r"[A-Za-z0-9][A-Za-z0-9'-]*", slug)) < 3:
        return None
    return slug[:1].upper() + slug[1:]


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


def _scrub_youtube_description(description: str | None) -> str | None:
    if not description:
        return None
    kept: list[str] = []
    skip_marketing_url = False
    for raw_line in description.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if not line:
            if kept:
                kept.append("")
            continue
        if skip_marketing_url and MARKETING_CONTINUATION_URL_RE.search(line):
            continue
        skip_marketing_url = False
        if HASHTAG_ONLY_RE.match(line):
            continue
        if YOUTUBE_MARKETING_LINE_RE.match(line):
            skip_marketing_url = True
            if kept:
                break
            continue
        kept.append(line)
    cleaned = "\n".join(kept).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned or None


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
    path = parsed.path.casefold()
    return (
        host in {"youtube.com", "youtu.be"}
        or host.endswith(".youtube.com")
        or path.endswith((".mp4", ".m4v", ".mov", ".webm"))
    )


def _is_http_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme.casefold() in {"http", "https"} and bool(parsed.netloc)


def _is_direct_video_url(url: str | None) -> bool:
    if not url:
        return False
    path = urlparse(url).path.casefold()
    return _is_http_url(url) and path.endswith((".mp4", ".m4v", ".mov", ".webm"))


def _is_link_preview_video_reference(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    return host in {"youtube.com", "youtu.be"} or host.endswith(".youtube.com")


def _playable_video_content(job: PostJob) -> str | None:
    if job.video_url and _is_video_reference(job.video_url):
        return job.video_url
    if _is_video_reference(job.url):
        return job.url
    return None


def _direct_media_items(job: PostJob) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    seen: set[str] = set()

    def append(media_type: str, url: str | None, source: str | None = None) -> None:
        if not url or url in seen:
            return
        if media_type == "video" and not _is_direct_video_url(url):
            return
        if media_type == "image" and not _is_http_url(url):
            return
        seen.add(url)
        item = {"type": media_type, "url": url}
        if source:
            item["source"] = source
        items.append(item)

    metadata_items = job.rich_metadata.get("media_items") if isinstance(job.rich_metadata, dict) else None
    if isinstance(metadata_items, list):
        for item in metadata_items:
            if not isinstance(item, dict):
                continue
            media_type = str(item.get("type") or "").casefold()
            url = str(item.get("url") or "").strip()
            source = str(item.get("source") or "").strip() or None
            if media_type in {"video", "animated_gif"}:
                append("video", url, source)
            elif media_type in {"image", "photo"}:
                append("image", url, source)

    append("video", job.video_url, job.video_source)
    if not job.video_url:
        append("image", job.image_url, job.image_source)
    elif not any(item.get("type") == "video" for item in items):
        append("image", job.image_url, job.image_source)
    return items


def _media_upload_limit(job: PostJob) -> int:
    return 10 if _should_upload_social_media(job) else 4


async def _send_prepared_media(send, prepared: list[PreparedMedia], embed: discord.Embed, send_kwargs: dict[str, object]):
    files = [discord.File(media.path, filename=media.filename) for media in prepared]
    try:
        return await send(files=files, embed=embed, **send_kwargs)
    finally:
        for file in files:
            file.close()


def _social_post_details(job: PostJob) -> dict[str, str | None] | None:
    if not _is_social_post(job):
        return None
    account_name = _social_account_name(job.source_name)
    body = _format_social_post_body(job)
    post_url = _social_post_url(job)
    return {"account_name": account_name, "body": body, "post_url": post_url}


def _is_social_post(job: PostJob) -> bool:
    if job.source_name.startswith(("Bluesky:", "X:")):
        return True
    if job.source_id.startswith("x-"):
        return True
    return job.source_class in {"social_core", "social_defense_industry", "social_centcom", "social_breaking_news", "owned_social"}


def _should_upload_social_media(job: PostJob) -> bool:
    metadata = job.rich_metadata or {}
    return str(metadata.get("source") or "").casefold() == "x_message" and bool(metadata.get("post_id"))


def _social_account_name(source_name: str) -> str:
    for prefix in ("Bluesky:", "X:"):
        if source_name.startswith(prefix):
            value = source_name[len(prefix) :].strip()
            return value or source_name
    return source_name or "Social post"


def _format_social_post_body(job: PostJob) -> str | None:
    raw = job.summary or job.title
    cleaned = clean_html_text(raw) if raw else None
    if not cleaned:
        return None
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in cleaned.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines) or None


def _social_post_url(job: PostJob) -> str | None:
    metadata = job.rich_metadata or {}
    for key in ("social_url", "bluesky_post_url", "x_post_url", "tweet_url"):
        value = metadata.get(key)
        if isinstance(value, str) and (_is_bluesky_post_url(value) or _is_x_post_url(value)):
            return value
    if _is_bluesky_post_url(job.url) or _is_x_post_url(job.url):
        return job.url
    return None


def _is_bluesky_post_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    return host == "bsky.app" and "/post/" in parsed.path


def _is_x_post_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    return host in {"x.com", "twitter.com"} and re.search(r"/status(?:es)?/\d+", parsed.path) is not None


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
