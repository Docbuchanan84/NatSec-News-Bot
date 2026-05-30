from __future__ import annotations

import logging
import os
import time
from datetime import UTC
from datetime import datetime

import discord
from discord import app_commands

from app.config_loader import ConfigError, ConfigService
from app.database import Database
from app.logging_config import configure_logging
from app.models import PostJob
from app.publisher import PublisherAdapter, PublisherService
from app.scheduler import SchedulerService

logger = logging.getLogger(__name__)


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

        self.tree.add_command(group)
