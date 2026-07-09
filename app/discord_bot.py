from __future__ import annotations

import logging
import os
import json
import re
import time
from dataclasses import replace
from datetime import UTC
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import unquote, urlparse

import aiohttp
import discord
from discord import app_commands

from app.codex_draft import build_codex_draft_payload, discord_message_url, json_row_value, related_article_candidates
from app.config_loader import ConfigError, ConfigService
from app.database import Database
from app.feed_fetcher import clean_html_text
from app.logging_config import configure_logging
from app.models import PostJob
from app.publisher import PublisherAdapter, PublisherService
from app.routing import RoutingConfigError
from app.routing.importance import (
    ImportanceTerm,
    apply_importance,
    build_importance_config,
    default_importance_terms,
    normalize_watch_term,
)
from app.routing.models import RoutingArticle
from app.routing.reporting import format_backtest_summary, format_decision, truncate
from app.routing.runtime import load_selected_routing_engine, selected_routing_engine_name
from app.routing_v2.teaching import (
    RoutingTeachError,
    apply_duplicate_teaching_rule,
    apply_duplicate_source_url_teaching_rule,
    apply_source_url_teaching_rule,
    apply_teaching_rule,
    decision_summary,
    evidence_json_snippet,
    make_source_url_teaching_rule,
    make_teaching_rule,
    preview_duplicate_source_url_teaching_rule,
    preview_duplicate_teaching_rule,
    preview_source_url_teaching_rule,
    preview_teaching_rule,
    route_score_suggestions,
    restore_latest_backup,
)
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
DEFAULT_DRAFT_WORKER_URL = "http://host.docker.internal:8765/draft"
DRAFT_PROFILES = ("fast", "quality")
IMPORTANCE_COLOR_STOPS = (
    (0, 0x2ECC71),
    (50, 0xF1C40F),
    (100, 0xE74C3C),
)
SCHEDULE_EVENT_COLORS = {
    "status_lid": 0x95A5A6,
    "press_status": 0x5D8AA8,
    "public_remarks": 0x1F6FEB,
    "meeting": 0x2E86C1,
    "travel": 0x8E44AD,
    "schedule_digest": 0x1F6FEB,
    "schedule_event": 0x3498DB,
}
SCHEDULE_EVENT_LABELS = {
    "status_lid": "Schedule status",
    "press_status": "Press logistics",
    "public_remarks": "Public remarks",
    "meeting": "Meeting",
    "travel": "Travel",
    "schedule_digest": "Advance daily schedule",
    "schedule_event": "Schedule event",
}
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
    elif _is_public_schedule_post(job):
        embed = _build_public_schedule_embed(job)
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

        async def scores_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
            return self._route_score_autocomplete(current)

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
                f"{config.settings.routing.mode}/{selected_routing_engine_name(config)}"
                if config.settings.routing.enabled
                else "off"
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
            await interaction.response.send_message(self._importance_watchlist_text(), ephemeral=True)

        @group.command(name="importance-add", description="Add or update an importance watch term")
        @app_commands.describe(
            term="Word or phrase to boost",
            weight="Importance adjustment, -50 to +50",
            category="Short category label",
            expires_at="Optional ISO timestamp or YYYY-MM-DD expiration",
            notes="Optional note for why this term matters",
        )
        async def importance_add(
            interaction: discord.Interaction,
            term: str,
            weight: int,
            category: str = "watch",
            expires_at: str | None = None,
            notes: str | None = None,
        ) -> None:
            try:
                row = self.db.upsert_importance_watch_term(
                    term,
                    weight=weight,
                    category=category,
                    notes=notes,
                    enabled=True,
                    expires_at=_parse_importance_expiration(expires_at),
                    source="human",
                )
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.send_message(
                f"Importance term active: {row['term']} {_signed_int(int(row['weight']))} ({row['category']}).",
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

        @group.command(name="importance-proposals", description="Show pending Codex importance suggestions")
        async def importance_proposals(interaction: discord.Interaction) -> None:
            proposals = self.db.list_importance_watch_term_proposals(status="pending")
            await interaction.response.send_message(
                _format_importance_proposals(proposals),
                ephemeral=True,
                view=ImportanceProposalActionView(self, proposals),
            )

        @group.command(name="importance-approve", description="Approve a pending Codex importance suggestion")
        @app_commands.describe(proposal_id="Proposal ID from the importance proposal list")
        async def importance_approve(interaction: discord.Interaction, proposal_id: int) -> None:
            try:
                proposal = self.db.apply_importance_watch_term_proposal(proposal_id)
            except (KeyError, ValueError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.send_message(
                f"Applied proposal #{proposal['id']}: {proposal['action']} {proposal['term']}.",
                ephemeral=True,
            )

        @group.command(name="importance-reject", description="Reject a pending Codex importance suggestion")
        @app_commands.describe(proposal_id="Proposal ID from the importance proposal list")
        async def importance_reject(interaction: discord.Interaction, proposal_id: int) -> None:
            try:
                proposal = self.db.reject_importance_watch_term_proposal(proposal_id)
            except (KeyError, ValueError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.send_message(
                f"Rejected proposal #{proposal['id']}: {proposal['action']} {proposal['term']}.",
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
                _engine, routing_config, engine_name = load_selected_routing_engine(config)
                status = "valid"
                detail = _routing_status_lines(config, routing_config, engine_name, status, self.db.recent_routing_error_count())
            except RoutingConfigError as exc:
                detail = [
                    f"Routing enabled: {config.settings.routing.enabled}",
                    f"Routing mode: {config.settings.routing.mode}",
                    f"Routing engine: {selected_routing_engine_name(config)}",
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

        @group.command(name="teach", description="Teach routing from an article post or article ID")
        @app_commands.describe(
            term="Word, phrase, or regex to score",
            scores="Route scores like sea:+35, air:-5",
            message_id="Discord message ID for a bot article post",
            article_id="Article ID from the SQLite articles table",
            rule_type="literal by default; use pattern or regex for regex",
            fields="Fields to match, default title,summary,url_slug",
            notes="Optional reason for this routing change",
        )
        @app_commands.autocomplete(scores=scores_autocomplete)
        async def teach(
            interaction: discord.Interaction,
            term: str,
            scores: str,
            message_id: str | None = None,
            article_id: int | None = None,
            rule_type: str = "literal",
            fields: str | None = None,
            notes: str | None = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True)
            await self._teach_from_inputs(
                interaction,
                term=term,
                scores=scores,
                message_id=message_id,
                article_id=article_id,
                rule_type=rule_type,
                fields=fields,
                notes=notes,
            )

        @group.command(name="teach-feed-url", description="Teach routing from a configured feed URL")
        @app_commands.describe(
            scores="Route scores like sea:+10, sports:-20",
            host="Optional feed host, e.g. fifa.com",
            path_term="Optional complete path term, e.g. world cup",
            path_regex="Optional regex matched against the feed URL path",
            message_id="Discord message ID for a bot article post",
            article_id="Article ID from the SQLite articles table",
            notes="Optional reason for this feed URL routing change",
            url_bias_only="Keep positive URL-only matches from routing by themselves; default yes",
        )
        @app_commands.autocomplete(scores=scores_autocomplete)
        async def teach_feed_url(
            interaction: discord.Interaction,
            scores: str,
            host: str | None = None,
            path_term: str | None = None,
            path_regex: str | None = None,
            message_id: str | None = None,
            article_id: int | None = None,
            notes: str | None = None,
            url_bias_only: bool = True,
        ) -> None:
            await interaction.response.defer(ephemeral=True)
            await self._teach_source_url_from_inputs(
                interaction,
                scores=scores,
                host=host,
                path_term=path_term,
                path_regex=path_regex,
                message_id=message_id,
                article_id=article_id,
                notes=notes,
                url_bias_only=url_bias_only,
            )

        @group.command(name="preview-rule", description="Preview a routing teaching rule without saving it")
        @app_commands.describe(
            term="Word, phrase, or regex to score",
            scores="Route scores like sea:+35, air:-5",
            message_id="Discord message ID for a bot article post",
            article_id="Article ID from the SQLite articles table",
            rule_type="literal by default; use pattern or regex for regex",
            fields="Fields to match, default title,summary,url_slug",
            notes="Optional reason for this routing change",
        )
        @app_commands.autocomplete(scores=scores_autocomplete)
        async def preview_rule(
            interaction: discord.Interaction,
            term: str,
            scores: str,
            message_id: str | None = None,
            article_id: int | None = None,
            rule_type: str = "literal",
            fields: str | None = None,
            notes: str | None = None,
        ) -> None:
            await interaction.response.defer(ephemeral=True)
            await self._preview_teach_from_inputs(
                interaction,
                term=term,
                scores=scores,
                message_id=message_id,
                article_id=article_id,
                rule_type=rule_type,
                fields=fields,
                notes=notes,
            )

        @group.command(name="undo-rule", description="Undo the latest Discord-taught routing rule")
        async def undo_rule(interaction: discord.Interaction) -> None:
            await interaction.response.defer(ephemeral=True)
            config = self.config_service.active_config
            if config is None:
                await interaction.followup.send("No active config.", ephemeral=True)
                return
            try:
                restored = restore_latest_backup(config.settings.routing.weighted_config_dir)
                self._reload_runtime_config()
            except (ConfigError, RoutingTeachError) as exc:
                message = str(exc)
                self.db.record_routing_teach_event(
                    action="rollback",
                    status="failed",
                    user_id=str(interaction.user.id) if interaction.user else None,
                    user_name=str(interaction.user) if interaction.user else None,
                    channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                    error=message,
                )
                await interaction.followup.send("Rollback failed: " + truncate(message, 1600), ephemeral=True)
                return
            event_id = self.db.record_routing_teach_event(
                action="rollback",
                status="applied",
                user_id=str(interaction.user.id) if interaction.user else None,
                user_name=str(interaction.user) if interaction.user else None,
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                backup_path=str(restored),
            )
            await interaction.followup.send(f"Restored latest routing backup and reloaded config. Event #{event_id}.", ephemeral=True)

        @group.command(name="rule-history", description="Show recent Discord-taught routing changes")
        @app_commands.describe(limit="Number of recent events, max 50")
        async def rule_history(interaction: discord.Interaction, limit: int = 10) -> None:
            rows = self.db.recent_routing_teach_events(limit=limit)
            await interaction.response.send_message(_format_routing_teach_history(rows), ephemeral=True)

        @group.command(name="rule-help", description="Show examples for teaching routing terms")
        async def rule_help(interaction: discord.Interaction) -> None:
            await interaction.response.send_message(_routing_teach_help(), ephemeral=True)

        @group.command(name="draft-post", description="Draft a NatSec News X post from an article or bot message")
        @app_commands.describe(
            article_id="Article ID from the RSS bot",
            message_id="Discord message ID for a bot article post",
            tweet_only="Return only the tweet text instead of a full draft pack",
            profile="Draft model profile: fast is the default; quality uses the frontier model",
        )
        @app_commands.choices(
            profile=[
                app_commands.Choice(name="Fast", value="fast"),
                app_commands.Choice(name="Quality", value="quality"),
            ]
        )
        async def draft_post(
            interaction: discord.Interaction,
            article_id: int | None = None,
            message_id: str | None = None,
            tweet_only: bool = False,
            profile: app_commands.Choice[str] | None = None,
        ) -> None:
            await interaction.response.defer(ephemeral=False, thinking=True)
            try:
                resolved_article_id = self._resolve_teach_article_id(
                    article_id=article_id,
                    message_id=message_id,
                    channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                )
                result = await self._draft_natsec_x_post(
                    article_id=resolved_article_id,
                    source_message_id=message_id,
                    source_channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                    guild_id=str(interaction.guild_id) if interaction.guild_id else None,
                    tweet_only=tweet_only,
                    profile=profile.value if profile is not None else "fast",
                )
            except Exception as exc:
                await interaction.followup.send("Draft failed: " + truncate(str(exc), 1800), ephemeral=False)
                return
            await _send_long_followup(interaction, result)

        self.tree.add_command(group)

        async def teach_routing_term_context(interaction: discord.Interaction, message: discord.Message) -> None:
            resolved = self._article_id_for_context_teach_message(message)
            if resolved is None:
                await interaction.response.send_message(
                    "I could not connect that Discord message or its replied-to message to a posted article. "
                    "Use `/rss teach` with an article ID or bot message ID instead.",
                    ephemeral=True,
                )
                return
            article_id, matched_message_id, matched_channel_id = resolved
            await interaction.response.send_modal(
                RoutingTeachModal(
                    self,
                    article_id=article_id,
                    message_id=matched_message_id,
                    channel_id=matched_channel_id,
                )
            )

        self.tree.add_command(app_commands.ContextMenu(name="Teach routing term", callback=teach_routing_term_context))

        async def teach_feed_url_context(interaction: discord.Interaction, message: discord.Message) -> None:
            resolved = self._article_id_for_context_teach_message(message)
            if resolved is None:
                await interaction.response.send_message(
                    "I could not connect that Discord message or its replied-to message to a posted article. "
                    "Use `/rss teach-feed-url` with an article ID or bot message ID instead.",
                    ephemeral=True,
                )
                return
            article_id, matched_message_id, matched_channel_id = resolved
            article = self._routing_article_for_teach(article_id)
            await interaction.response.send_modal(
                RoutingFeedUrlTeachModal(
                    self,
                    article=article,
                    article_id=article_id,
                    message_id=matched_message_id,
                    channel_id=matched_channel_id,
                )
            )

        self.tree.add_command(app_commands.ContextMenu(name="Teach feed URL", callback=teach_feed_url_context))

        async def importance_watchlist_context(interaction: discord.Interaction, message: discord.Message) -> None:
            await interaction.response.send_message(self._importance_watchlist_text(), ephemeral=True)

        self.tree.add_command(app_commands.ContextMenu(name="Importance watchlist", callback=importance_watchlist_context))

        async def manage_importance_context(interaction: discord.Interaction, message: discord.Message) -> None:
            resolved = self._article_id_for_context_teach_message(message)
            article_id = resolved[0] if resolved is not None else None
            suggested_term = _suggest_importance_term_from_message(message)
            await interaction.response.send_message(
                _importance_manage_text(article_id=article_id, suggested_term=suggested_term),
                ephemeral=True,
                view=ImportanceManageView(self, article_id=article_id, suggested_term=suggested_term),
            )

        self.tree.add_command(app_commands.ContextMenu(name="Manage importance", callback=manage_importance_context))

        async def draft_natsec_x_context(interaction: discord.Interaction, message: discord.Message) -> None:
            resolved = self._article_id_for_context_teach_message(message)
            if resolved is None:
                await interaction.response.send_message(
                    "I could not connect that Discord message or its replied-to message to a posted article.",
                    ephemeral=True,
                )
                return
            article_id, matched_message_id, matched_channel_id = resolved
            await interaction.response.defer(ephemeral=False, thinking=True)
            try:
                result = await self._draft_natsec_x_post(
                    article_id=article_id,
                    source_message_id=matched_message_id,
                    source_channel_id=matched_channel_id,
                    guild_id=str(interaction.guild_id) if interaction.guild_id else None,
                    tweet_only=False,
                    profile="fast",
                )
            except Exception as exc:
                await interaction.followup.send("Draft failed: " + truncate(str(exc), 1800), ephemeral=False)
                return
            await _send_long_followup(interaction, result)

        self.tree.add_command(app_commands.ContextMenu(name="Draft NatSec X post", callback=draft_natsec_x_context))

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

    def _routing_engine_for_command(self) -> Any:
        config = self.config_service.active_config
        if config is None:
            raise RoutingConfigError(["No active config."])
        engine, _routing_config, _engine_name = load_selected_routing_engine(config)
        return engine

    def _reload_runtime_config(self) -> None:
        config = self.config_service.reload()
        configure_logging(config)
        self.publisher.configure(config)
        self.scheduler.configure(config)
        self.scheduler.start()
        self.social_link_embeds.configure(config)

    async def _preview_teach_from_inputs(
        self,
        interaction: discord.Interaction,
        *,
        term: str,
        scores: str,
        message_id: str | None = None,
        article_id: int | None = None,
        rule_type: str = "literal",
        fields: str | None = None,
        notes: str | None = None,
    ) -> None:
        try:
            resolved_article_id = self._resolve_teach_article_id(article_id=article_id, message_id=message_id, channel_id=None)
            article = self._routing_article_for_teach(resolved_article_id)
            config = self._weighted_teach_config()
            rule = make_teaching_rule(
                term=term,
                scores=scores,
                app_config=config,
                rule_type=rule_type,
                fields=fields,
                notes=notes,
            )
            preview = preview_teaching_rule(article, config, rule)
            duplicate = preview_duplicate_teaching_rule(article, config, rule)
        except (RoutingConfigError, RoutingTeachError) as exc:
            self.db.record_routing_teach_event(
                action="preview",
                status="failed",
                user_id=str(interaction.user.id) if interaction.user else None,
                user_name=str(interaction.user) if interaction.user else None,
                article_id=article_id,
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                message_id=message_id,
                term=term,
                rule_type=rule_type,
                notes=notes,
                error=str(exc),
            )
            await interaction.followup.send("Rule preview failed: " + truncate(str(exc), 1600), ephemeral=True)
            return
        if duplicate is not None:
            event_id = self.db.record_routing_teach_event(
                action="preview_duplicate",
                status="previewed",
                user_id=str(interaction.user.id) if interaction.user else None,
                user_name=str(interaction.user) if interaction.user else None,
                article_id=resolved_article_id,
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                message_id=message_id,
                rule_id=duplicate.duplicate.rule_id,
                term=rule.term,
                rule_type=rule.rule_type,
                fields=rule.fields,
                scores=rule.scores,
                notes=rule.notes,
                before_decision=duplicate.before.to_json_dict(),
                after_decision=duplicate.merge_after.to_json_dict(),
            )
            await interaction.followup.send(_format_duplicate_preview(event_id, resolved_article_id, duplicate), ephemeral=True)
            return
        event_id = self.db.record_routing_teach_event(
            action="preview",
            status="previewed",
            user_id=str(interaction.user.id) if interaction.user else None,
            user_name=str(interaction.user) if interaction.user else None,
            article_id=resolved_article_id,
            channel_id=str(interaction.channel_id) if interaction.channel_id else None,
            message_id=message_id,
            rule_id=rule.id,
            term=rule.term,
            rule_type=rule.rule_type,
            fields=rule.fields,
            scores=rule.scores,
            notes=rule.notes,
            before_decision=preview.before.to_json_dict(),
            after_decision=preview.after.to_json_dict(),
        )
        await interaction.followup.send(_format_teach_preview(event_id, resolved_article_id, preview), ephemeral=True)

    async def _teach_from_inputs(
        self,
        interaction: discord.Interaction,
        *,
        term: str,
        scores: str,
        message_id: str | None = None,
        article_id: int | None = None,
        rule_type: str = "literal",
        fields: str | None = None,
        notes: str | None = None,
    ) -> None:
        try:
            resolved_article_id = self._resolve_teach_article_id(article_id=article_id, message_id=message_id, channel_id=None)
            await self._teach_article(
                interaction,
                article_id=resolved_article_id,
                message_id=message_id,
                term=term,
                scores=scores,
                rule_type=rule_type,
                fields=fields,
                notes=notes,
            )
        except (RoutingConfigError, RoutingTeachError) as exc:
            self.db.record_routing_teach_event(
                action="teach",
                status="failed",
                user_id=str(interaction.user.id) if interaction.user else None,
                user_name=str(interaction.user) if interaction.user else None,
                article_id=article_id,
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                message_id=message_id,
                term=term,
                rule_type=rule_type,
                notes=notes,
                error=str(exc),
            )
            await interaction.followup.send("Teaching failed: " + truncate(str(exc), 1600), ephemeral=True)

    async def _teach_source_url_from_inputs(
        self,
        interaction: discord.Interaction,
        *,
        scores: str,
        host: str | None = None,
        path_term: str | None = None,
        path_regex: str | None = None,
        message_id: str | None = None,
        article_id: int | None = None,
        notes: str | None = None,
        url_bias_only: bool = True,
    ) -> None:
        try:
            resolved_article_id: int | None = None
            article: RoutingArticle
            source_url: str | None = None
            if article_id is not None or message_id:
                resolved_article_id = self._resolve_teach_article_id(
                    article_id=article_id,
                    message_id=message_id,
                    channel_id=None,
                )
                article = self._routing_article_for_teach(resolved_article_id)
                source_url = article.source_url
            else:
                source_url = f"https://{host.strip()}/" if host and host.strip() else None
                article = RoutingArticle(
                    title="Feed URL teaching preview",
                    source_name="Feed URL",
                    source_url=source_url,
                )
            await self._teach_source_url_article(
                interaction,
                article=article,
                article_id=resolved_article_id,
                message_id=message_id,
                scores=scores,
                host=host,
                path_term=path_term,
                path_regex=path_regex,
                source_url=source_url,
                notes=notes,
                url_bias_only=url_bias_only,
            )
        except (RoutingConfigError, RoutingTeachError) as exc:
            self.db.record_routing_teach_event(
                action="teach_source_url",
                status="failed",
                user_id=str(interaction.user.id) if interaction.user else None,
                user_name=str(interaction.user) if interaction.user else None,
                article_id=article_id,
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                message_id=message_id,
                term=host or path_term or path_regex,
                rule_type="source_url",
                notes=notes,
                error=str(exc),
            )
            await interaction.followup.send("Feed URL teaching failed: " + truncate(str(exc), 1600), ephemeral=True)

    async def _teach_source_url_article(
        self,
        interaction: discord.Interaction,
        *,
        article: RoutingArticle,
        article_id: int | None,
        message_id: str | None,
        scores: str,
        host: str | None,
        path_term: str | None,
        path_regex: str | None,
        source_url: str | None,
        notes: str | None,
        url_bias_only: bool,
    ) -> None:
        config = self._weighted_teach_config()
        rule = make_source_url_teaching_rule(
            scores=scores,
            app_config=config,
            host=host,
            path_term=path_term,
            path_regex=path_regex,
            source_url=source_url,
            notes=notes,
            url_bias_only=url_bias_only,
        )
        duplicate = preview_duplicate_source_url_teaching_rule(article, config, rule)
        if duplicate is not None:
            event_id = self.db.record_routing_teach_event(
                action="teach_source_url_duplicate",
                status="pending",
                user_id=str(interaction.user.id) if interaction.user else None,
                user_name=str(interaction.user) if interaction.user else None,
                article_id=article_id,
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                message_id=message_id,
                rule_id=duplicate.duplicate.rule_id,
                term=rule.term,
                rule_type=rule.rule_type,
                fields=rule.fields,
                scores=rule.scores,
                notes=rule.notes,
                before_decision=duplicate.before.to_json_dict(),
                after_decision=duplicate.merge_after.to_json_dict(),
            )
            await interaction.followup.send(
                _format_duplicate_prompt(event_id, article_id, duplicate),
                view=RoutingDuplicateSourceUrlRuleView(
                    self,
                    article=article,
                    article_id=article_id,
                    message_id=message_id,
                    scores=scores,
                    host=host,
                    path_term=path_term,
                    path_regex=path_regex,
                    source_url=source_url,
                    notes=notes,
                    url_bias_only=url_bias_only,
                ),
                ephemeral=True,
            )
            return
        result = apply_source_url_teaching_rule(article, config, rule)
        try:
            self._reload_runtime_config()
        except ConfigError as exc:
            restore_latest_backup(config.settings.routing.weighted_config_dir)
            self._reload_runtime_config()
            self.db.record_routing_teach_event(
                action="teach_source_url",
                status="failed",
                user_id=str(interaction.user.id) if interaction.user else None,
                user_name=str(interaction.user) if interaction.user else None,
                article_id=article_id,
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                message_id=message_id,
                rule_id=rule.id,
                term=rule.term,
                rule_type=rule.rule_type,
                fields=rule.fields,
                scores=rule.scores,
                notes=rule.notes,
                before_decision=result.before.to_json_dict(),
                after_decision=result.after.to_json_dict(),
                error="Config reload failed after feed URL write; backup restored. " + str(exc),
                backup_path=str(result.backup_path),
            )
            raise RoutingTeachError("Config reload failed after write, so the backup was restored.") from exc
        event_id = self.db.record_routing_teach_event(
            action="teach_source_url",
            status="applied",
            user_id=str(interaction.user.id) if interaction.user else None,
            user_name=str(interaction.user) if interaction.user else None,
            article_id=article_id,
            channel_id=str(interaction.channel_id) if interaction.channel_id else None,
            message_id=message_id,
            rule_id=result.rule.id,
            term=result.rule.term,
            rule_type=result.rule.rule_type,
            fields=result.rule.fields,
            scores=result.rule.scores,
            notes=result.rule.notes,
            before_decision=result.before.to_json_dict(),
            after_decision=result.after.to_json_dict(),
            backup_path=str(result.backup_path),
        )
        await self._send_teach_changelog(
            event_id=event_id,
            article_id=article_id,
            action_label="Added feed URL rule",
            actor=str(interaction.user) if interaction.user else None,
            result=result,
        )
        await interaction.followup.send(_format_teach_applied(event_id, article_id, result), ephemeral=True)

    async def _teach_article(
        self,
        interaction: discord.Interaction,
        *,
        article_id: int,
        message_id: str | None,
        term: str,
        scores: str,
        rule_type: str = "literal",
        fields: str | None = None,
        notes: str | None = None,
    ) -> None:
        article = self._routing_article_for_teach(article_id)
        config = self._weighted_teach_config()
        rule = make_teaching_rule(
            term=term,
            scores=scores,
            app_config=config,
            rule_type=rule_type,
            fields=fields,
            notes=notes,
        )
        duplicate = preview_duplicate_teaching_rule(article, config, rule)
        if duplicate is not None:
            event_id = self.db.record_routing_teach_event(
                action="teach_duplicate",
                status="pending",
                user_id=str(interaction.user.id) if interaction.user else None,
                user_name=str(interaction.user) if interaction.user else None,
                article_id=article_id,
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                message_id=message_id,
                rule_id=duplicate.duplicate.rule_id,
                term=rule.term,
                rule_type=rule.rule_type,
                fields=rule.fields,
                scores=rule.scores,
                notes=rule.notes,
                before_decision=duplicate.before.to_json_dict(),
                after_decision=duplicate.merge_after.to_json_dict(),
            )
            await interaction.followup.send(
                _format_duplicate_prompt(event_id, article_id, duplicate),
                view=RoutingDuplicateRuleView(
                    self,
                    article_id=article_id,
                    message_id=message_id,
                    term=term,
                    scores=scores,
                    rule_type=rule_type,
                    fields=fields,
                    notes=notes,
                ),
                ephemeral=True,
            )
            return
        result = apply_teaching_rule(article, config, rule)
        try:
            self._reload_runtime_config()
        except ConfigError as exc:
            restore_latest_backup(config.settings.routing.weighted_config_dir)
            self._reload_runtime_config()
            self.db.record_routing_teach_event(
                action="teach",
                status="failed",
                user_id=str(interaction.user.id) if interaction.user else None,
                user_name=str(interaction.user) if interaction.user else None,
                article_id=article_id,
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                message_id=message_id,
                rule_id=rule.id,
                term=rule.term,
                rule_type=rule.rule_type,
                fields=rule.fields,
                scores=rule.scores,
                notes=rule.notes,
                before_decision=result.before.to_json_dict(),
                after_decision=result.after.to_json_dict(),
                error="Config reload failed after write; backup restored. " + str(exc),
                backup_path=str(result.backup_path),
            )
            raise RoutingTeachError("Config reload failed after write, so the backup was restored.") from exc
        event_id = self.db.record_routing_teach_event(
            action="teach",
            status="applied",
            user_id=str(interaction.user.id) if interaction.user else None,
            user_name=str(interaction.user) if interaction.user else None,
            article_id=article_id,
            channel_id=str(interaction.channel_id) if interaction.channel_id else None,
            message_id=message_id,
            rule_id=result.rule.id,
            term=result.rule.term,
            rule_type=result.rule.rule_type,
            fields=result.rule.fields,
            scores=result.rule.scores,
            notes=result.rule.notes,
            before_decision=result.before.to_json_dict(),
            after_decision=result.after.to_json_dict(),
            backup_path=str(result.backup_path),
        )
        await self._send_teach_changelog(
            event_id=event_id,
            article_id=article_id,
            action_label="Added routing term",
            actor=str(interaction.user) if interaction.user else None,
            result=result,
        )
        await interaction.followup.send(_format_teach_applied(event_id, article_id, result), ephemeral=True)

    async def _apply_duplicate_teach(
        self,
        interaction: discord.Interaction,
        *,
        mode: str,
        article_id: int,
        message_id: str | None,
        term: str,
        scores: str,
        rule_type: str,
        fields: str | None,
        notes: str | None,
    ) -> None:
        article = self._routing_article_for_teach(article_id)
        config = self._weighted_teach_config()
        rule = make_teaching_rule(
            term=term,
            scores=scores,
            app_config=config,
            rule_type=rule_type,
            fields=fields,
            notes=notes,
        )
        result = apply_duplicate_teaching_rule(article, config, rule, mode)
        try:
            self._reload_runtime_config()
        except ConfigError as exc:
            restore_latest_backup(config.settings.routing.weighted_config_dir)
            self._reload_runtime_config()
            self.db.record_routing_teach_event(
                action=f"teach_{mode}",
                status="failed",
                user_id=str(interaction.user.id) if interaction.user else None,
                user_name=str(interaction.user) if interaction.user else None,
                article_id=article_id,
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                message_id=message_id,
                rule_id=result.rule.id,
                term=result.rule.term,
                rule_type=result.rule.rule_type,
                fields=result.rule.fields,
                scores=result.rule.scores,
                notes=result.rule.notes,
                before_decision=result.before.to_json_dict(),
                after_decision=result.after.to_json_dict(),
                error="Config reload failed after duplicate write; backup restored. " + str(exc),
                backup_path=str(result.backup_path),
            )
            raise RoutingTeachError("Config reload failed after write, so the backup was restored.") from exc
        event_id = self.db.record_routing_teach_event(
            action=f"teach_{mode}",
            status="applied",
            user_id=str(interaction.user.id) if interaction.user else None,
            user_name=str(interaction.user) if interaction.user else None,
            article_id=article_id,
            channel_id=str(interaction.channel_id) if interaction.channel_id else None,
            message_id=message_id,
            rule_id=result.rule.id,
            term=result.rule.term,
            rule_type=result.rule.rule_type,
            fields=result.rule.fields,
            scores=result.rule.scores,
            notes=result.rule.notes,
            before_decision=result.before.to_json_dict(),
            after_decision=result.after.to_json_dict(),
            backup_path=str(result.backup_path),
        )
        await self._send_teach_changelog(
            event_id=event_id,
            article_id=article_id,
            action_label=f"{mode.title()}d routing term",
            actor=str(interaction.user) if interaction.user else None,
            result=result,
        )
        await interaction.followup.send(_format_duplicate_applied(event_id, article_id, mode, result), ephemeral=True)

    async def _apply_duplicate_source_url_teach(
        self,
        interaction: discord.Interaction,
        *,
        mode: str,
        article: RoutingArticle,
        article_id: int | None,
        message_id: str | None,
        scores: str,
        host: str | None,
        path_term: str | None,
        path_regex: str | None,
        source_url: str | None,
        notes: str | None,
        url_bias_only: bool,
    ) -> None:
        config = self._weighted_teach_config()
        rule = make_source_url_teaching_rule(
            scores=scores,
            app_config=config,
            host=host,
            path_term=path_term,
            path_regex=path_regex,
            source_url=source_url,
            notes=notes,
            url_bias_only=url_bias_only,
        )
        result = apply_duplicate_source_url_teaching_rule(article, config, rule, mode)
        try:
            self._reload_runtime_config()
        except ConfigError as exc:
            restore_latest_backup(config.settings.routing.weighted_config_dir)
            self._reload_runtime_config()
            self.db.record_routing_teach_event(
                action=f"teach_source_url_{mode}",
                status="failed",
                user_id=str(interaction.user.id) if interaction.user else None,
                user_name=str(interaction.user) if interaction.user else None,
                article_id=article_id,
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                message_id=message_id,
                rule_id=result.rule.id,
                term=result.rule.term,
                rule_type=result.rule.rule_type,
                fields=result.rule.fields,
                scores=result.rule.scores,
                notes=result.rule.notes,
                before_decision=result.before.to_json_dict(),
                after_decision=result.after.to_json_dict(),
                error="Config reload failed after duplicate feed URL write; backup restored. " + str(exc),
                backup_path=str(result.backup_path),
            )
            raise RoutingTeachError("Config reload failed after write, so the backup was restored.") from exc
        event_id = self.db.record_routing_teach_event(
            action=f"teach_source_url_{mode}",
            status="applied",
            user_id=str(interaction.user.id) if interaction.user else None,
            user_name=str(interaction.user) if interaction.user else None,
            article_id=article_id,
            channel_id=str(interaction.channel_id) if interaction.channel_id else None,
            message_id=message_id,
            rule_id=result.rule.id,
            term=result.rule.term,
            rule_type=result.rule.rule_type,
            fields=result.rule.fields,
            scores=result.rule.scores,
            notes=result.rule.notes,
            before_decision=result.before.to_json_dict(),
            after_decision=result.after.to_json_dict(),
            backup_path=str(result.backup_path),
        )
        await self._send_teach_changelog(
            event_id=event_id,
            article_id=article_id,
            action_label=f"{mode.title()}d feed URL rule",
            actor=str(interaction.user) if interaction.user else None,
            result=result,
        )
        await interaction.followup.send(_format_duplicate_applied(event_id, article_id, mode, result), ephemeral=True)

    async def _send_teach_changelog(
        self,
        *,
        event_id: int,
        article_id: int | None,
        action_label: str,
        actor: str | None,
        result,
    ) -> None:
        config = self.config_service.active_config
        channel_id = config.settings.routing.teach_changelog_channel_id if config else None
        if not channel_id:
            return
        try:
            channel = self.get_channel(int(channel_id))
            if channel is None:
                channel = await self.fetch_channel(int(channel_id))
            if not hasattr(channel, "send"):
                logger.warning("Routing teach changelog channel %s cannot receive messages.", channel_id)
                return
            await channel.send(
                embed=_build_teach_changelog_embed(
                    event_id=event_id,
                    article_id=article_id,
                    action_label=action_label,
                    actor=actor,
                    result=result,
                )
            )
        except Exception:
            logger.warning("Routing teach changelog send failed for event_id=%s channel_id=%s", event_id, channel_id, exc_info=True)

    def _resolve_teach_article_id(
        self,
        *,
        article_id: int | None,
        message_id: str | None,
        channel_id: str | None,
    ) -> int:
        if article_id is not None and message_id:
            raise RoutingTeachError("Use either article_id or message_id, not both.")
        if article_id is not None:
            return int(article_id)
        if message_id:
            resolved = self.db.article_id_for_discord_message(str(message_id), channel_id)
            if resolved is None and channel_id is not None:
                resolved = self.db.article_id_for_discord_message(str(message_id), None)
            if resolved is None:
                raise RoutingTeachError("No posted article was found for that Discord message ID.")
            return resolved
        raise RoutingTeachError("Provide an article_id or a Discord message_id.")

    def _article_id_for_context_teach_message(self, message: discord.Message) -> tuple[int, str, str] | None:
        for message_id, channel_id in _teach_message_lookup_candidates(message):
            resolved = self.db.article_id_for_discord_message(message_id, channel_id)
            if resolved is None and channel_id is not None:
                resolved = self.db.article_id_for_discord_message(message_id, None)
            if resolved is not None:
                return resolved, message_id, channel_id or ""
        article_id = _article_id_from_message_embeds(message)
        if article_id is not None:
            channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
            return article_id, str(getattr(message, "id", "") or ""), channel_id
        return None

    def _routing_article_for_teach(self, article_id: int) -> RoutingArticle:
        row = self.db.get_article_for_routing(article_id)
        if row is None:
            raise RoutingTeachError(f"Article not found: {article_id}")
        return replace(_routing_article_from_row(row), source_url=self._source_url_for_article(article_id))

    def _source_url_for_article(self, article_id: int) -> str | None:
        if not hasattr(self.db, "feed_key_for_article"):
            return None
        feed_key = self.db.feed_key_for_article(article_id)
        if not feed_key:
            return None
        source = self.scheduler.feeds.get(feed_key) or self.scheduler.email_sources.get(feed_key)
        if source is None:
            return None
        return getattr(source, "normalized_url", None) or getattr(source, "url", None)

    async def _draft_natsec_x_post(
        self,
        *,
        article_id: int,
        source_message_id: str | None,
        source_channel_id: str | None,
        guild_id: str | None,
        tweet_only: bool,
        profile: str,
    ) -> str:
        profile = profile.strip().casefold()
        if profile not in DRAFT_PROFILES:
            raise RuntimeError(f"Unknown draft profile: {profile}")
        worker_url = (
            os.environ.get("DRAFT_WORKER_URL")
            or os.environ.get("CODEX_DRAFT_WORKER_URL")
            or DEFAULT_DRAFT_WORKER_URL
        ).strip()
        if not worker_url:
            raise RuntimeError("DRAFT_WORKER_URL is not configured.")
        timeout_seconds = _env_int(
            "DRAFT_TIMEOUT_SECONDS",
            _env_int("CODEX_DRAFT_TIMEOUT_SECONDS", 900),
        )
        job = self.db.get_post_job(article_id, source_channel_id or REVIEW_CHANNEL_ID, is_new_article=False)
        routing_row = self.db.latest_routing_decision_for_article(article_id)
        routing = _routing_row_to_payload(routing_row)
        related_rows = [
            _article_row_to_payload(row)
            for row in self.db.recent_articles_for_routing(limit=700, days=3)
        ]
        payload = build_codex_draft_payload(
            job=job,
            routing=routing,
            related_articles=related_article_candidates(job, related_rows, limit=8),
            request={
                "tweet_only": tweet_only,
                "source_message_id": source_message_id,
                "source_channel_id": source_channel_id,
                "source_message_url": discord_message_url(guild_id, source_channel_id, source_message_id),
                "profile": profile,
            },
        )
        client_timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=client_timeout) as session:
            async with session.post(worker_url, json=payload) as response:
                body = await response.text()
                if response.status >= 400:
                    raise RuntimeError(f"worker HTTP {response.status}: {body[:1200]}")
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    raise RuntimeError("worker returned non-JSON response: " + body[:1200])
        if not data.get("ok"):
            raise RuntimeError(str(data.get("error") or "worker failed"))
        result = str(data.get("result") or "").strip()
        if not result:
            raise RuntimeError("worker returned an empty draft")
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        audit_logger.info(
            "draft_completed article_id=%s channel_id=%s message_id=%s worker_url=%s "
            "backend=%s profile=%s model=%s elapsed_ms=%s estimated_cost_usd=%s",
            article_id,
            source_channel_id,
            source_message_id,
            worker_url,
            meta.get("backend"),
            meta.get("profile") or profile,
            meta.get("model"),
            meta.get("elapsed_ms"),
            meta.get("estimated_cost_usd"),
        )
        return result

    def _weighted_teach_config(self):
        config = self.config_service.active_config
        if config is None:
            raise RoutingTeachError("No active config.")
        if selected_routing_engine_name(config) != "weighted_v2":
            raise RoutingTeachError("Routing teaching requires the weighted_v2 engine to be active.")
        return config

    def _route_score_autocomplete(self, current: str) -> list[app_commands.Choice[str]]:
        config = self.config_service.active_config
        if config is None or selected_routing_engine_name(config) != "weighted_v2":
            return []
        try:
            suggestions = route_score_suggestions(config, current)
        except RoutingTeachError:
            return []
        return [app_commands.Choice(name=name, value=value) for name, value in suggestions]

    def _apply_importance_for_command(self, decision, article: RoutingArticle):
        recent_articles = (
            self.db.recent_articles_for_importance_similarity(article.article_id)
            if article.article_id is not None and hasattr(self.db, "recent_articles_for_importance_similarity")
            else ()
        )
        return apply_importance(
            decision,
            article,
            build_importance_config(
                self.db.list_importance_watch_terms(include_disabled=True),
                recent_articles=recent_articles,
            ),
        )

    def _importance_watchlist_text(self) -> str:
        terms = build_importance_config(
            self.db.list_importance_watch_terms(include_disabled=True)
        ).watch_terms
        proposals = (
            self.db.list_importance_watch_term_proposals(status="pending", limit=10)
            if hasattr(self.db, "list_importance_watch_term_proposals")
            else []
        )
        return _format_importance_terms(terms, pending_proposals=proposals)

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


class ImportanceManageView(discord.ui.View):
    def __init__(self, client: RSSDiscordClient, *, article_id: int | None, suggested_term: str | None) -> None:
        super().__init__(timeout=300)
        self.client = client
        self.article_id = article_id
        self.suggested_term = suggested_term

    @discord.ui.button(label="Score Article", style=discord.ButtonStyle.secondary)
    async def score_article(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.article_id is None:
            await interaction.response.send_message("No RSS article was found for that message.", ephemeral=True)
            return
        try:
            engine = self.client._routing_engine_for_command()
            row = self.client.db.get_article_for_routing(self.article_id)
            if row is None:
                await interaction.response.send_message(f"Article not found: {self.article_id}", ephemeral=True)
                return
            article = _routing_article_from_row(row)
            decision = self.client._apply_importance_for_command(engine.route(article), article)
        except RoutingConfigError as exc:
            await self.client._send_routing_config_error(interaction, exc)
            return
        await interaction.response.send_message(format_decision(decision), ephemeral=True)

    @discord.ui.button(label="Add Term", style=discord.ButtonStyle.primary)
    async def add_term(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            ImportanceWatchTermModal(self.client, default_term=self.suggested_term or "")
        )

    @discord.ui.button(label="Disable Term", style=discord.ButtonStyle.danger)
    async def disable_term(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(ImportanceDisableTermModal(self.client))

    @discord.ui.button(label="Review Proposals", style=discord.ButtonStyle.secondary)
    async def review_proposals(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        proposals = self.client.db.list_importance_watch_term_proposals(status="pending")
        await interaction.response.send_message(
            _format_importance_proposals(proposals),
            ephemeral=True,
            view=ImportanceProposalActionView(self.client, proposals),
        )


class ImportanceProposalActionView(discord.ui.View):
    def __init__(self, client: RSSDiscordClient, proposals: list[dict[str, object]]) -> None:
        super().__init__(timeout=300)
        self.client = client
        for proposal in proposals[:5]:
            proposal_id = int(proposal["id"])
            approve = discord.ui.Button(
                label=f"Approve #{proposal_id}",
                style=discord.ButtonStyle.primary,
                custom_id=f"importance_approve_{proposal_id}",
            )
            reject = discord.ui.Button(
                label=f"Reject #{proposal_id}",
                style=discord.ButtonStyle.secondary,
                custom_id=f"importance_reject_{proposal_id}",
            )
            approve.callback = self._approve_callback(proposal_id)
            reject.callback = self._reject_callback(proposal_id)
            self.add_item(approve)
            self.add_item(reject)

    def _approve_callback(self, proposal_id: int):
        async def callback(interaction: discord.Interaction) -> None:
            try:
                proposal = self.client.db.apply_importance_watch_term_proposal(proposal_id)
            except (KeyError, ValueError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.send_message(
                f"Applied proposal #{proposal['id']}: {proposal['action']} {proposal['term']}.",
                ephemeral=True,
            )

        return callback

    def _reject_callback(self, proposal_id: int):
        async def callback(interaction: discord.Interaction) -> None:
            try:
                proposal = self.client.db.reject_importance_watch_term_proposal(proposal_id)
            except (KeyError, ValueError) as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            await interaction.response.send_message(
                f"Rejected proposal #{proposal['id']}: {proposal['action']} {proposal['term']}.",
                ephemeral=True,
            )

        return callback


class ImportanceWatchTermModal(discord.ui.Modal):
    def __init__(self, client: RSSDiscordClient, *, default_term: str = "") -> None:
        super().__init__(title="Add importance term")
        self.client = client
        self.term = discord.ui.TextInput(
            label="Term",
            default=default_term[:100],
            max_length=100,
            required=True,
        )
        self.weight = discord.ui.TextInput(
            label="Weight (-50 to +50)",
            default="15",
            max_length=4,
            required=True,
        )
        self.category = discord.ui.TextInput(
            label="Category",
            default="watch",
            max_length=40,
            required=True,
        )
        self.expires_at = discord.ui.TextInput(
            label="Expiration (optional)",
            placeholder="2026-07-09 or 2026-07-09T18:00:00Z",
            max_length=40,
            required=False,
        )
        self.notes = discord.ui.TextInput(
            label="Notes",
            style=discord.TextStyle.paragraph,
            max_length=300,
            required=False,
        )
        for item in (self.term, self.weight, self.category, self.expires_at, self.notes):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            row = self.client.db.upsert_importance_watch_term(
                str(self.term.value),
                weight=int(str(self.weight.value).strip()),
                category=str(self.category.value),
                expires_at=_parse_importance_expiration(str(self.expires_at.value)),
                notes=str(self.notes.value).strip() or None,
                enabled=True,
                source="human",
            )
        except (TypeError, ValueError) as exc:
            await interaction.response.send_message("Importance term update failed: " + str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Importance term active: {row['term']} {_signed_int(int(row['weight']))} ({row['category']}).",
            ephemeral=True,
        )


class ImportanceDisableTermModal(discord.ui.Modal):
    def __init__(self, client: RSSDiscordClient) -> None:
        super().__init__(title="Disable importance term")
        self.client = client
        self.term = discord.ui.TextInput(label="Term", max_length=100, required=True)
        self.add_item(self.term)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            default = _default_importance_term(str(self.term.value))
            self.client.db.set_importance_watch_term_enabled(
                str(self.term.value),
                enabled=False,
                default_weight=default.weight if default else 1,
                default_category=default.category if default else "watch",
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"Importance term disabled: {normalize_watch_term(str(self.term.value))}.",
            ephemeral=True,
        )


class RoutingTeachModal(discord.ui.Modal):
    def __init__(self, client: RSSDiscordClient, *, article_id: int, message_id: str, channel_id: str) -> None:
        super().__init__(title="Teach routing term")
        self.client = client
        self.article_id = article_id
        self.message_id = message_id
        self.channel_id = channel_id
        self.term = discord.ui.TextInput(
            label="Term or phrase",
            placeholder="nuclear deterrence",
            max_length=200,
            required=True,
        )
        self.scores = discord.ui.TextInput(
            label="Scores",
            placeholder="US Politics:+50 or strategic-weapons:+55, air:+6",
            max_length=500,
            required=True,
        )
        self.rule_type = discord.ui.TextInput(
            label="Type",
            placeholder="literal (default) or regex",
            default="literal",
            max_length=20,
            required=False,
        )
        self.fields = discord.ui.TextInput(
            label="Fields",
            placeholder="title,summary,url_slug",
            default="title,summary,url_slug",
            max_length=100,
            required=False,
        )
        self.notes = discord.ui.TextInput(
            label="Notes",
            placeholder="Optional reason",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=False,
        )
        for item in (self.term, self.scores, self.rule_type, self.fields, self.notes):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await self.client._teach_article(
                interaction,
                article_id=self.article_id,
                message_id=self.message_id,
                term=str(self.term.value),
                scores=str(self.scores.value),
                rule_type=str(self.rule_type.value or "literal"),
                fields=str(self.fields.value or "title,summary,url_slug"),
                notes=str(self.notes.value or ""),
            )
        except (RoutingConfigError, RoutingTeachError) as exc:
            self.client.db.record_routing_teach_event(
                action="teach",
                status="failed",
                user_id=str(interaction.user.id) if interaction.user else None,
                user_name=str(interaction.user) if interaction.user else None,
                article_id=self.article_id,
                channel_id=self.channel_id,
                message_id=self.message_id,
                term=str(self.term.value),
                rule_type=str(self.rule_type.value or "literal"),
                notes=str(self.notes.value or ""),
                error=str(exc),
            )
            await interaction.followup.send("Teaching failed: " + truncate(str(exc), 1600), ephemeral=True)


class RoutingFeedUrlTeachModal(discord.ui.Modal):
    def __init__(
        self,
        client: RSSDiscordClient,
        *,
        article: RoutingArticle,
        article_id: int,
        message_id: str,
        channel_id: str,
    ) -> None:
        super().__init__(title="Teach feed URL")
        self.client = client
        self.article = article
        self.article_id = article_id
        self.message_id = message_id
        self.channel_id = channel_id
        self.scores = discord.ui.TextInput(
            label="Scores",
            placeholder="sports:+35 or sports:+35, review:-5",
            max_length=500,
            required=True,
        )
        self.host = discord.ui.TextInput(
            label="Feed host",
            placeholder="Optional. Leave as inferred host unless needed.",
            default=_host_from_url(article.source_url) or "",
            max_length=200,
            required=False,
        )
        self.path_term = discord.ui.TextInput(
            label="URL path term",
            placeholder="sports",
            max_length=200,
            required=False,
        )
        self.path_regex = discord.ui.TextInput(
            label="URL path regex",
            placeholder="Optional advanced regex for the feed URL path",
            max_length=500,
            required=False,
        )
        self.notes = discord.ui.TextInput(
            label="Notes",
            placeholder="Optional reason",
            style=discord.TextStyle.paragraph,
            max_length=500,
            required=False,
        )
        for item in (self.scores, self.host, self.path_term, self.path_regex, self.notes):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await self.client._teach_source_url_article(
                interaction,
                article=self.article,
                article_id=self.article_id,
                message_id=self.message_id,
                scores=str(self.scores.value),
                host=str(self.host.value or ""),
                path_term=str(self.path_term.value or ""),
                path_regex=str(self.path_regex.value or ""),
                source_url=self.article.source_url,
                notes=str(self.notes.value or ""),
                url_bias_only=True,
            )
        except (RoutingConfigError, RoutingTeachError) as exc:
            self.client.db.record_routing_teach_event(
                action="teach_source_url",
                status="failed",
                user_id=str(interaction.user.id) if interaction.user else None,
                user_name=str(interaction.user) if interaction.user else None,
                article_id=self.article_id,
                channel_id=self.channel_id,
                message_id=self.message_id,
                term=str(self.host.value or self.path_term.value or self.path_regex.value or ""),
                rule_type="source_url",
                notes=str(self.notes.value or ""),
                error=str(exc),
            )
            await interaction.followup.send("Feed URL teaching failed: " + truncate(str(exc), 1600), ephemeral=True)


class RoutingDuplicateRuleView(discord.ui.View):
    def __init__(
        self,
        client: RSSDiscordClient,
        *,
        article_id: int,
        message_id: str | None,
        term: str,
        scores: str,
        rule_type: str,
        fields: str | None,
        notes: str | None,
    ) -> None:
        super().__init__(timeout=600)
        self.client = client
        self.article_id = article_id
        self.message_id = message_id
        self.term = term
        self.scores = scores
        self.rule_type = rule_type
        self.fields = fields
        self.notes = notes

    @discord.ui.button(label="Merge", style=discord.ButtonStyle.primary)
    async def merge(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._apply(interaction, "merge")

    @discord.ui.button(label="Replace", style=discord.ButtonStyle.secondary)
    async def replace(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._apply(interaction, "replace")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Duplicate routing change canceled. Nothing was saved.", view=self)

    async def _apply(self, interaction: discord.Interaction, mode: str) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"Applying duplicate rule {mode}...", view=self)
        try:
            await self.client._apply_duplicate_teach(
                interaction,
                mode=mode,
                article_id=self.article_id,
                message_id=self.message_id,
                term=self.term,
                scores=self.scores,
                rule_type=self.rule_type,
                fields=self.fields,
                notes=self.notes,
            )
        except (RoutingConfigError, RoutingTeachError) as exc:
            self.client.db.record_routing_teach_event(
                action=f"teach_{mode}",
                status="failed",
                user_id=str(interaction.user.id) if interaction.user else None,
                user_name=str(interaction.user) if interaction.user else None,
                article_id=self.article_id,
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                message_id=self.message_id,
                term=self.term,
                rule_type=self.rule_type,
                notes=self.notes,
                error=str(exc),
            )
            await interaction.followup.send(f"Duplicate rule {mode} failed: " + truncate(str(exc), 1600), ephemeral=True)


class RoutingDuplicateSourceUrlRuleView(discord.ui.View):
    def __init__(
        self,
        client: RSSDiscordClient,
        *,
        article: RoutingArticle,
        article_id: int | None,
        message_id: str | None,
        scores: str,
        host: str | None,
        path_term: str | None,
        path_regex: str | None,
        source_url: str | None,
        notes: str | None,
        url_bias_only: bool,
    ) -> None:
        super().__init__(timeout=600)
        self.client = client
        self.article = article
        self.article_id = article_id
        self.message_id = message_id
        self.scores = scores
        self.host = host
        self.path_term = path_term
        self.path_regex = path_regex
        self.source_url = source_url
        self.notes = notes
        self.url_bias_only = url_bias_only

    @discord.ui.button(label="Merge", style=discord.ButtonStyle.primary)
    async def merge(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._apply(interaction, "merge")

    @discord.ui.button(label="Replace", style=discord.ButtonStyle.secondary)
    async def replace(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await self._apply(interaction, "replace")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Duplicate feed URL change canceled. Nothing was saved.", view=self)

    async def _apply(self, interaction: discord.Interaction, mode: str) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content=f"Applying duplicate feed URL rule {mode}...", view=self)
        try:
            await self.client._apply_duplicate_source_url_teach(
                interaction,
                mode=mode,
                article=self.article,
                article_id=self.article_id,
                message_id=self.message_id,
                scores=self.scores,
                host=self.host,
                path_term=self.path_term,
                path_regex=self.path_regex,
                source_url=self.source_url,
                notes=self.notes,
                url_bias_only=self.url_bias_only,
            )
        except (RoutingConfigError, RoutingTeachError) as exc:
            self.client.db.record_routing_teach_event(
                action=f"teach_source_url_{mode}",
                status="failed",
                user_id=str(interaction.user.id) if interaction.user else None,
                user_name=str(interaction.user) if interaction.user else None,
                article_id=self.article_id,
                channel_id=str(interaction.channel_id) if interaction.channel_id else None,
                message_id=self.message_id,
                term=self.host or self.path_term or self.path_regex,
                rule_type="source_url",
                notes=self.notes,
                error=str(exc),
            )
            await interaction.followup.send(
                f"Duplicate feed URL rule {mode} failed: " + truncate(str(exc), 1600),
                ephemeral=True,
            )


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
        title_signature=row["title_signature"],
        story_cluster_key=row["story_cluster_key"],
        published_at=_parse_datetime(row["normalized_published_at"]),
        ingested_at=_parse_datetime(row["ingested_at"]),
        timestamp_status=row["timestamp_status"] or "valid",
    )


def _routing_status_lines(config, routing_config, engine_name: str, status: str, recent_errors: int) -> list[str]:
    lines = [
        f"Routing enabled: {config.settings.routing.enabled}",
        f"Routing mode: {config.settings.routing.mode}",
        f"Routing engine: {engine_name}",
        f"Validation: {status}",
    ]
    if engine_name == "weighted_v2":
        regex_rules = sum(1 for rule in routing_config.evidence_rules if rule.type == "pattern")
        literal_rules = len(routing_config.evidence_rules) - regex_rules
        lines.extend(
            [
                f"Weighted config version: {routing_config.version}",
                f"Routes: {len(routing_config.routes)}",
                f"Evidence rules: {len(routing_config.evidence_rules)} ({literal_rules} literal, {regex_rules} regex)",
                f"Source rules: {len(routing_config.source_rules)}",
                f"Mirror rules: {len(routing_config.mirror_rules)}",
                (
                    "Thresholds: "
                    f"primary {routing_config.primary_threshold}, review {routing_config.review_threshold}, "
                    f"noise {routing_config.noise_threshold}, secondaries within {routing_config.secondary_within_percent}%"
                ),
            ]
        )
    else:
        lines.extend(
            [
                f"Taxonomy version: {routing_config.taxonomy_version}",
                f"Knowledge base version: {routing_config.knowledge_base_version}",
                f"Channel rules: {len(routing_config.channel_rules)}",
                f"Loaded tags: {len(routing_config.taxonomy)}",
                f"Loaded knowledge entries: {len(routing_config.knowledge_entries)}",
            ]
        )
    lines.append(f"Recent routing errors: {recent_errors}")
    return lines


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _host_from_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    return parsed.hostname or None


def _teach_message_lookup_candidates(message: Any) -> list[tuple[str, str | None]]:
    candidates: list[tuple[str, str | None]] = []
    seen: set[tuple[str, str | None]] = set()

    def snowflake(value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def add(message_id: object, channel_id: object | None) -> None:
        resolved_message_id = snowflake(message_id)
        if resolved_message_id is None:
            return
        resolved_channel_id = snowflake(channel_id)
        key = (resolved_message_id, resolved_channel_id)
        if key in seen:
            return
        seen.add(key)
        candidates.append(key)

    selected_channel_id = snowflake(getattr(getattr(message, "channel", None), "id", None))
    add(getattr(message, "id", None), selected_channel_id)

    reference = getattr(message, "reference", None)
    if reference is not None:
        reference_channel_id = snowflake(getattr(reference, "channel_id", None)) or selected_channel_id
        add(getattr(reference, "message_id", None), reference_channel_id)

        resolved = getattr(reference, "resolved", None)
        if resolved is not None:
            resolved_channel_id = snowflake(getattr(getattr(resolved, "channel", None), "id", None)) or reference_channel_id
            add(getattr(resolved, "id", None), resolved_channel_id)

    return candidates


def _article_id_from_message_embeds(message: Any) -> int | None:
    for embed in getattr(message, "embeds", None) or []:
        values: list[str] = []
        footer = getattr(embed, "footer", None)
        footer_text = getattr(footer, "text", None)
        if footer_text:
            values.append(str(footer_text))
        for field in getattr(embed, "fields", None) or []:
            name = str(getattr(field, "name", "") or "")
            value = str(getattr(field, "value", "") or "")
            values.append(f"{name}\n{value}")
        for value in values:
            match = re.search(r"\bArticle ID:\s*(\d+)\b", value, re.IGNORECASE)
            if match:
                return int(match.group(1))
    return None


def _format_importance_terms(
    terms: tuple[ImportanceTerm, ...],
    limit: int = 1900,
    *,
    pending_proposals: list[dict[str, object]] | None = None,
) -> str:
    if not terms:
        base = "No active importance watch terms."
        if pending_proposals:
            return truncate(base + "\n\n" + _format_importance_proposals(pending_proposals), limit)
        return base
    sorted_terms = sorted(terms, key=lambda item: (item.category, -item.weight, item.term))
    lines = ["Active importance watch terms:"]
    for term in sorted_terms[:60]:
        meta = [term.category, term.source]
        if term.expires_at:
            meta.append("expires " + _format_datetime_short(term.expires_at))
        if term.notes:
            meta.append(truncate(term.notes, 80))
        lines.append(f"- {term.term}: {_signed_int(term.weight)} ({'; '.join(meta)})")
    if len(sorted_terms) > 60:
        lines.append(f"... +{len(sorted_terms) - 60} more")
    if pending_proposals:
        lines.append("")
        lines.append(_format_importance_proposals(pending_proposals, title="Pending Codex proposals:"))
    return truncate("\n".join(lines), limit)


def _format_importance_proposals(
    proposals: list[dict[str, object]],
    *,
    title: str = "Pending Codex importance proposals:",
    limit: int = 1900,
) -> str:
    if not proposals:
        return "No pending Codex importance proposals."
    lines = [title]
    for proposal in proposals[:20]:
        weight = proposal.get("weight")
        weight_text = "" if weight is None else f" {_signed_int(int(weight))}"
        expires = str(proposal.get("expires_at") or "")
        expires_text = f", expires {expires[:16]}" if expires else ""
        rationale = str(proposal.get("rationale") or proposal.get("notes") or "").strip()
        lines.append(
            f"- #{proposal['id']} {proposal['action']} {proposal['term']}{weight_text}"
            f" ({proposal.get('category') or 'watch'}{expires_text})"
        )
        if rationale:
            lines.append("  " + truncate(rationale, 160))
    if len(proposals) > 20:
        lines.append(f"... +{len(proposals) - 20} more")
    return truncate("\n".join(lines), limit)


def _importance_manage_text(*, article_id: int | None, suggested_term: str | None) -> str:
    lines = ["Importance management"]
    if article_id is not None:
        lines.append(f"Article ID: {article_id}")
    if suggested_term:
        lines.append(f"Suggested term seed: {truncate(suggested_term, 120)}")
    lines.append("Use the buttons below to score this article, add a watch term, disable a term, or review Codex proposals.")
    return "\n".join(lines)


def _suggest_importance_term_from_message(message: discord.Message) -> str | None:
    for embed in getattr(message, "embeds", None) or []:
        title = str(getattr(embed, "title", "") or "").strip()
        if title:
            return _compact_term_seed(title)
    content = str(getattr(message, "content", "") or "").strip()
    return _compact_term_seed(content) if content else None


def _compact_term_seed(value: str) -> str:
    cleaned = re.sub(r"https?://\S+", "", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:100]


def _parse_importance_expiration(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return datetime.fromisoformat(text).replace(tzinfo=UTC) + timedelta(days=1)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Expiration must be YYYY-MM-DD or ISO datetime.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_datetime_short(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


def _signed_int(value: int) -> str:
    return f"+{value}" if value >= 0 else str(value)


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
        f"Article ID: {article_id}",
        "Teach: right-click this post -> Apps -> Teach routing term or Teach feed URL",
        f"Decision: {str(row['decision_status']).upper()} -> {', '.join(selected) or 'none'}",
        f"Reason: {row['reason'] or 'none'}",
        f"Top score: {row['top_score']}",
        f"Importance: {int(row['importance_score'] or 0)}/100",
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
        f"Importance: {int(row['importance_score'] or 0)}/100",
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


def _format_teach_preview(event_id: int, article_id: int | None, preview) -> str:
    lines = [
        f"Routing rule preview #{event_id}",
        f"Article: {article_id or 'none'}",
        f"Rule: {preview.rule.id}",
        f"Term: {preview.rule.term}",
        "Scores: " + _format_score_map(preview.rule.scores),
        "Before: " + decision_summary(preview.before),
        "After: " + decision_summary(preview.after),
        "",
        "Nothing was saved.",
    ]
    return truncate("\n".join(lines), 1900)


def _format_teach_applied(event_id: int, article_id: int | None, result) -> str:
    lines = [
        f"Routing rule saved #{event_id}",
        f"Article: {article_id or 'none'}",
        f"Rule: {result.rule.id}",
        f"Term: {result.rule.term}",
        "Scores: " + _format_score_map(result.rule.scores),
        "Before: " + decision_summary(result.before),
        "After: " + decision_summary(result.after),
        "",
        "Config reloaded. Use `/rss undo-rule` to restore the latest backup.",
    ]
    return truncate("\n".join(lines), 1900)


def _format_duplicate_preview(event_id: int, article_id: int | None, duplicate) -> str:
    return _format_duplicate_message(
        header=f"Duplicate routing rule preview #{event_id}",
        article_id=article_id,
        duplicate=duplicate,
        footer="Nothing was saved.",
    )


def _format_duplicate_prompt(event_id: int, article_id: int | None, duplicate) -> str:
    return _format_duplicate_message(
        header=f"Duplicate term detected #{event_id}",
        article_id=article_id,
        duplicate=duplicate,
        footer="Choose Merge to update scores in the existing rule, Replace to overwrite its scores/fields/term, or Cancel.",
    )


def _format_duplicate_applied(event_id: int, article_id: int | None, mode: str, result) -> str:
    lines = [
        f"Duplicate routing rule {mode} saved #{event_id}",
        f"Article: {article_id or 'none'}",
        f"Rule: {result.rule.id}",
        f"Term: {result.rule.term}",
        "Scores: " + _format_score_map(result.rule.scores),
        "Before: " + decision_summary(result.before),
        "After: " + decision_summary(result.after),
        "",
        "Config reloaded. Use `/rss undo-rule` to restore the latest backup.",
    ]
    return truncate("\n".join(lines), 1900)


def _build_teach_changelog_embed(
    *,
    event_id: int,
    article_id: int | None,
    action_label: str,
    actor: str | None,
    result,
) -> discord.Embed:
    embed = discord.Embed(
        title=action_label,
        description=f"`{result.rule.term}`",
        color=0x2ECC71,
        timestamp=datetime.now(UTC),
    )
    embed.add_field(name="Scores", value=truncate(_format_score_map(result.rule.scores), 1024), inline=False)
    embed.add_field(name="Rule", value=f"`{result.rule.id}`\nType: `{result.rule.rule_type}`", inline=True)
    embed.add_field(name="Article", value=f"`{article_id or 'none'}`", inline=True)
    embed.add_field(name="Event", value=f"`#{event_id}`", inline=True)
    embed.add_field(name="Fields", value=", ".join(f"`{field}`" for field in result.rule.fields) or "none", inline=False)
    embed.add_field(
        name="Routing",
        value=truncate(f"Before: {decision_summary(result.before)}\nAfter: {decision_summary(result.after)}", 1024),
        inline=False,
    )
    if result.rule.notes:
        embed.add_field(name="Notes", value=truncate(result.rule.notes, 1024), inline=False)
    embed.set_footer(text=f"By {actor or 'unknown user'}")
    return embed


def _format_duplicate_message(header: str, article_id: int | None, duplicate, footer: str) -> str:
    lines = [
        header,
        f"Article: {article_id or 'none'}",
        f"Existing rule: {duplicate.duplicate.rule_id} ({duplicate.duplicate.match_type})",
        f"Submitted scores: {_format_score_map(duplicate.submitted.scores)}",
        "",
        "Current config snippet:",
        "```json",
        evidence_json_snippet(duplicate.current_json, 520),
        "```",
        "Merge preview:",
        "```json",
        evidence_json_snippet(duplicate.merge_json, 520),
        "```",
        "Before: " + decision_summary(duplicate.before),
        "Merge after: " + decision_summary(duplicate.merge_after),
        "Replace after: " + decision_summary(duplicate.replace_after),
        "",
        footer,
    ]
    return truncate("\n".join(lines), 1900)


def _format_routing_teach_history(rows) -> str:
    if not rows:
        return "No routing teaching events yet."
    lines = ["Recent routing teaching events:"]
    for row in rows:
        actor = row["user_name"] or "unknown user"
        status = row["status"]
        action = row["action"]
        article = row["article_id"] or "none"
        rule = row["rule_id"] or "none"
        term = row["term"] or "none"
        error = f" error={truncate(str(row['error']), 80)}" if row["error"] else ""
        lines.append(f"#{row['id']} {status}/{action} article={article} rule={rule} by {actor}: {term}{error}")
    return truncate("\n".join(lines), 1900)


def _routing_row_to_payload(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    output = dict(row)
    for key in (
        "selected_channel_keys",
        "primary_channel_keys",
        "mirror_channel_keys",
        "review_channel_keys",
        "final_channel_keys",
        "score_details",
        "matched_entries",
        "emitted_tags",
        "expanded_tags",
        "importance_reasons",
    ):
        if key in output:
            output[key] = json_row_value(output[key])
    return output


def _article_row_to_payload(row: Any) -> dict[str, Any]:
    output = dict(row)
    return {
        "id": output.get("id"),
        "title": output.get("title"),
        "url": output.get("url"),
        "summary": output.get("summary"),
        "source_name": output.get("source_name"),
        "source_id": output.get("source_id"),
        "source_class": output.get("source_class"),
        "normalized_published_at": output.get("normalized_published_at"),
        "timestamp_status": output.get("timestamp_status"),
    }


async def _send_long_followup(interaction: discord.Interaction, content: str) -> None:
    draft_sections = _draft_pack_sections(content)
    if draft_sections:
        tweet = draft_sections.get("tweet")
        context_messages: list[str] = []
        for key, label in (("evidence", "Evidence"), ("media", "Media"), ("notes", "Notes")):
            value = draft_sections.get(key)
            if not value:
                continue
            for chunk in _discord_chunks(value, limit=1850):
                context_messages.append(f"{label}:\n{chunk}")

        thread: discord.Thread | None = None
        thread_name = _draft_thread_name(tweet or content)
        if tweet:
            tweet_chunks = _discord_chunks(tweet, limit=1900)
            for index, chunk in enumerate(tweet_chunks):
                message = await interaction.followup.send(chunk, ephemeral=False, wait=True)
                if index == 0 and context_messages:
                    thread = await _try_create_draft_thread(message, interaction.channel, name=thread_name)
        if thread:
            sent_to_thread = 0
            try:
                for message in context_messages:
                    await thread.send(message)
                    sent_to_thread += 1
            except (discord.Forbidden, discord.HTTPException, AttributeError, ValueError):
                logger.warning("draft_thread_send_failed; falling back to channel followups", exc_info=True)
                for message in context_messages[sent_to_thread:]:
                    await interaction.followup.send(message, ephemeral=False)
        else:
            for message in context_messages:
                await interaction.followup.send(message, ephemeral=False)
        return

    chunks = _discord_chunks(content, limit=1900)
    if not chunks:
        await interaction.followup.send("Draft worker returned no content.", ephemeral=False)
        return
    capped = chunks[:4]
    for index, chunk in enumerate(capped, start=1):
        prefix = "" if len(chunks) == 1 else f"Part {index}/{len(capped)}\n"
        await interaction.followup.send(prefix + chunk, ephemeral=False)
    if len(chunks) > len(capped):
        await interaction.followup.send(
            "Draft output was longer than Discord can reasonably post; truncated after 4 messages.",
            ephemeral=False,
        )


async def _try_create_draft_thread(
    message: discord.Message | None,
    channel: Any = None,
    *,
    name: str = "NatSec draft context",
) -> discord.Thread | None:
    if message is None:
        return None
    try:
        return await message.create_thread(name=name, auto_archive_duration=60)
    except ValueError:
        fetched = await _fetch_message_for_thread(channel, getattr(message, "id", None))
        if fetched is None:
            logger.warning("draft_thread_create_failed; followup message had no guild info and could not be refetched")
            return None
        try:
            return await fetched.create_thread(name=name, auto_archive_duration=60)
        except (discord.Forbidden, discord.HTTPException, AttributeError, ValueError):
            logger.warning("draft_thread_create_failed_after_refetch", exc_info=True)
            return None
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        logger.warning("draft_thread_create_failed", exc_info=True)
        return None


async def _fetch_message_for_thread(channel: Any, message_id: Any) -> discord.Message | None:
    if channel is None or message_id is None or not hasattr(channel, "fetch_message"):
        return None
    try:
        return await channel.fetch_message(int(message_id))
    except (discord.Forbidden, discord.HTTPException, AttributeError, TypeError, ValueError):
        logger.warning("draft_thread_message_refetch_failed", exc_info=True)
        return None


def _draft_thread_name(text: str) -> str:
    cleaned = re.sub(r"https?://\S+", "", str(text or ""))
    cleaned = re.sub(r"[\r\n\t]+", " ", cleaned)
    cleaned = re.sub(r"[`*_~>|#@\[\]():;]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = "".join(char for char in cleaned if char.isprintable())
    cleaned = cleaned.strip(" -.,")
    if not cleaned:
        return "NatSec draft context"
    name = f"Draft: {cleaned}"
    return name[:97].rstrip(" -.,") + "..." if len(name) > 100 else name


def _discord_chunks(content: str, *, limit: int) -> list[str]:
    text = str(content or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        piece = paragraph.strip()
        if not piece:
            continue
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(piece) > limit:
            chunks.append(piece[:limit])
            piece = piece[limit:]
        current = piece
    if current:
        chunks.append(current)
    return chunks


def _draft_pack_sections(content: str) -> dict[str, str]:
    text = str(content or "").strip()
    if not text:
        return {}
    heading_re = re.compile(r"(?im)^(Tweet|Evidence|Media|Notes):\s*$")
    matches = list(heading_re.finditer(text))
    if not matches:
        return {}
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        key = match.group(1).casefold()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[start:end].strip()
        if value:
            sections[key] = value
    return sections if "tweet" in sections else {}


def _routing_teach_help() -> str:
    return "\n".join(
        [
            "Best path: right-click or long-press a bot article post, then choose Apps -> Teach routing term.",
            "For feed URL terms, use Apps -> Teach feed URL from the same article menu.",
            "",
            "Score format:",
            "`sea:+35, strategic-weapons:+55, air:-5`",
            "",
            "Slash fallback examples:",
            "`/rss teach message_id:123 term:\"nuclear deterrence\" scores:\"strategic-weapons:+55\"`",
            "`/rss preview-rule article_id:456 term:\"sub sandwich\" scores:\"noise:+45, sea:-25\"`",
            "`/rss teach-feed-url message_id:123 path_term:\"sports\" scores:\"sports:+35\"`",
            "`/rss teach-feed-url host:\"fifa.com\" path_term:\"world cup\" scores:\"sports:+15, review:-5\"`",
            "",
            "Literal is the default for terms. Use type `regex` only when you intentionally need a text pattern.",
            "Feed URL positives are bias-only by default: they boost matching routes but do not route by themselves.",
        ]
    )


def _format_score_map(scores: dict[str, int]) -> str:
    return ", ".join(f"{key}:{value:+}" for key, value in sorted(scores.items())) or "none"


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


def _is_public_schedule_post(job: PostJob) -> bool:
    metadata = job.rich_metadata or {}
    if metadata.get("calendar_event") is True or metadata.get("schedule_digest") is True:
        return True
    return job.source_id == "factbase-white-house-calendar" and str(metadata.get("source") or "").casefold() == "ical"


def _build_public_schedule_embed(job: PostJob) -> discord.Embed:
    metadata = job.rich_metadata or {}
    event_kind = str(metadata.get("event_kind") or "schedule_event")
    label = SCHEDULE_EVENT_LABELS.get(event_kind, SCHEDULE_EVENT_LABELS["schedule_event"])
    event_start = _schedule_event_start(metadata)
    display_timestamp = event_start or job.normalized_published_at.astimezone(UTC)
    title = _clean_schedule_title(clean_html_text(job.title) or job.title)
    description = _format_schedule_description(job, label)
    embed = discord.Embed(
        title=title[:256],
        url=job.url,
        description=description[:4096] if description else None,
        timestamp=display_timestamp,
        color=SCHEDULE_EVENT_COLORS.get(event_kind, SCHEDULE_EVENT_COLORS["schedule_event"]),
    )
    time_text = _schedule_time_field(event_start)
    if time_text:
        embed.add_field(name="Time", value=time_text, inline=True)
    location = str(metadata.get("event_location") or "").strip()
    if location:
        embed.add_field(name="Location", value=location[:1024], inline=True)
    embed.add_field(name="Type", value=label, inline=True)
    embed.set_footer(text=_post_footer(job, display_timestamp))
    return embed


def _schedule_event_start(metadata: dict[str, Any]) -> datetime | None:
    value = str(metadata.get("event_start_utc") or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _schedule_time_field(event_start: datetime | None) -> str | None:
    if event_start is None:
        return None
    unix_seconds = int(event_start.timestamp())
    return f"<t:{unix_seconds}:F>\n<t:{unix_seconds}:R>"


def _clean_schedule_title(title: str) -> str:
    cleaned = " ".join(title.replace("**", "").split()).strip() or "Public schedule update"
    cleaned = re.sub(r"\s*\(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+UTC\)\s*$", "", cleaned)
    cleaned = re.sub(r"^Public Schedule:\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^White House Press Office:\s*", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip(" -:") or "Public schedule update"


def _format_schedule_description(job: PostJob, label: str) -> str | None:
    metadata = job.rich_metadata or {}
    description = clean_html_text(str(metadata.get("event_description") or ""))
    compact = bool(metadata.get("event_compact"))
    if compact:
        if description and not _same_display_text(description, job.title):
            return f"{label}: {description}"
        return f"{label} update for the White House public schedule."
    if description:
        return description
    if not job.summary:
        return None
    useful_lines: list[str] = []
    for raw_line in job.summary.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("Start:", "Location:")):
            continue
        useful_lines.append(line)
    return "\n".join(useful_lines).strip() or None


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
    if _is_public_schedule_post(job):
        event_kind = str((job.rich_metadata or {}).get("event_kind") or "schedule_event")
        label = SCHEDULE_EVENT_LABELS.get(event_kind, SCHEDULE_EVENT_LABELS["schedule_event"])
        return f"{job.source_name} · {label} · {status}"
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
    return max(0, min(100, int(score)))


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


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default
