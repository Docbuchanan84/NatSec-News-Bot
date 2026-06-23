from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from app.models import (
    AppConfig,
    BotSettings,
    ChannelConfig,
    DedupeSettings,
    DiscordSettings,
    EmailSourceConfig,
    FailureBackoffSettings,
    FeedConfig,
    LoggingSettings,
    MaintenanceSettings,
    PollingSettings,
    PublishingSettings,
    RoutingSettings,
    Settings,
    SocialLinkEmbedSettings,
    TimestampSettings,
)


class ConfigError(Exception):
    """Raised when config cannot be loaded or validated."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("\n".join(errors))


SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
SOURCE_CLASS_BY_ID = {
    "reuters": "wire_service",
    "associated-press": "wire_service",
    "ap": "wire_service",
    "defense-gov": "official_us_defense",
    "department-of-defense": "official_us_defense",
    "dod": "official_us_defense",
    "us-navy": "official_us_defense",
    "us-army": "official_us_defense",
    "us-air-force": "official_us_defense",
    "us-marine-corps": "official_us_defense",
    "us-space-force": "official_us_defense",
    "dvids": "official_us_defense",
    "breaking-defense": "defense_media",
    "defense-news": "defense_media",
    "war-zone": "defense_media",
    "usni": "defense_media",
    "csis": "think_tank",
    "rand": "think_tank",
    "brookings": "think_tank",
    "cfr": "think_tank",
    "cnas": "think_tank",
    "nyt": "major_media",
    "wapo": "major_media",
    "npr": "major_media",
    "newsweek": "major_media",
}


@dataclass
class ConfigService:
    path: Path
    active_config: AppConfig | None = None

    def load_initial(self) -> AppConfig:
        config = load_config(self.path)
        self.active_config = config
        return config

    def reload(self) -> AppConfig:
        config = load_config(self.path)
        self.active_config = config
        return config


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError([f"Config file not found: {config_path}"]) from exc
    except json.JSONDecodeError as exc:
        raise ConfigError([f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"]) from exc

    errors: list[str] = []
    if not isinstance(raw, dict):
        raise ConfigError(["config root must be a JSON object"])

    version = _int(raw.get("version", 1), "version", errors, min_value=1)
    bot_raw = _object(raw.get("bot", {}), "bot", errors)
    discord_raw = _object(raw.get("discord", {}), "discord", errors)
    settings_raw = _object(raw.get("settings", {}), "settings", errors)

    bot = BotSettings(
        name=_string(bot_raw.get("name", "RSS Dispatch Bot"), "bot.name", errors),
        timezone=_string(bot_raw.get("timezone", "America/Chicago"), "bot.timezone", errors),
    )
    discord = DiscordSettings(
        guild_id_env=_string(discord_raw.get("guildIdEnv", "DISCORD_GUILD_ID"), "discord.guildIdEnv", errors)
    )
    settings = _parse_settings(settings_raw, errors)
    feeds = _parse_top_level_feeds(raw.get("feeds", []), settings, errors)
    email_sources = _parse_email_sources(raw.get("emailSources", []), settings, errors)
    channels = _parse_channels(raw.get("channels"), settings, errors)
    channel_keys = {channel.key for channel in channels}
    channel_ids = {channel.discord_channel_id for channel in channels}
    if any(source.no_match_policy == "review" for source in email_sources) and "review" not in channel_keys:
        errors.append("emailSources with noMatchPolicy=review require a channel with key 'review'.")
    for source in email_sources:
        missing_targets = sorted(set(source.target_channel_ids) - channel_ids)
        if missing_targets:
            errors.append(
                f"emailSources[{source.id}].targetChannelIds must reference configured channels: "
                + ", ".join(missing_targets)
            )
        missing_fast_lane_targets = sorted(set(source.fast_lane_channel_ids) - channel_ids)
        if missing_fast_lane_targets:
            errors.append(
                f"emailSources[{source.id}].fastLaneChannelIds must reference configured channels: "
                + ", ".join(missing_fast_lane_targets)
            )
    feed_ids = {feed.id for feed in feeds if feed.id}
    duplicate_source_ids = feed_ids & {source.id for source in email_sources}
    if duplicate_source_ids:
        errors.append("source ids must not duplicate feed or email source ids: " + ", ".join(sorted(duplicate_source_ids)))

    if errors:
        raise ConfigError(errors)
    return AppConfig(
        version=version,
        bot=bot,
        discord=discord,
        settings=settings,
        feeds=tuple(feeds),
        channels=tuple(channels),
        raw=raw,
        email_sources=tuple(email_sources),
    )


def validate_env(config: AppConfig, env: dict[str, str] | None = None) -> list[str]:
    env_map = os.environ if env is None else env
    errors: list[str] = []
    token = env_map.get("DISCORD_BOT_TOKEN", "").strip()
    guild_id = env_map.get(config.discord.guild_id_env, "").strip()
    if not token or token == "replace_with_discord_bot_token":
        errors.append("DISCORD_BOT_TOKEN is missing or still set to the placeholder value.")
    if not SNOWFLAKE_RE.match(guild_id):
        errors.append(f"{config.discord.guild_id_env} must be a valid Discord guild ID.")
    checked_envs: set[str] = set()
    for source in config.email_sources:
        if source.route_policy == "ignore":
            continue
        for env_name, label in (
            (source.imap_host_env, "IMAP host"),
            (source.username_env, "username"),
            (source.password_env, "password"),
        ):
            if env_name in checked_envs:
                continue
            checked_envs.add(env_name)
            if not env_map.get(env_name, "").strip():
                errors.append(f"{env_name} is required for email {label}.")
        if source.imap_port_env not in checked_envs:
            checked_envs.add(source.imap_port_env)
            port_value = env_map.get(source.imap_port_env, "").strip()
            if port_value and not port_value.isdigit():
                errors.append(f"{source.imap_port_env} must be an integer IMAP port when set.")
    return errors


def _parse_settings(raw: dict[str, Any], errors: list[str]) -> Settings:
    polling_raw = _object(raw.get("polling", {}), "settings.polling", errors)
    failure_backoff_raw = _object(raw.get("failureBackoff", {}), "settings.failureBackoff", errors)
    dedupe_raw = _object(raw.get("dedupe", {}), "settings.dedupe", errors)
    timestamps_raw = _object(raw.get("timestamps", {}), "settings.timestamps", errors)
    publishing_raw = _object(raw.get("publishing", {}), "settings.publishing", errors)
    logging_raw = _object(raw.get("logging", {}), "settings.logging", errors)
    routing_raw = _object(raw.get("routing", {}), "settings.routing", errors)
    maintenance_raw = _object(raw.get("maintenance", {}), "settings.maintenance", errors)
    social_link_embeds_raw = _object(
        raw.get("socialLinkEmbeds", raw.get("xLinkEmbeds", {})),
        "settings.socialLinkEmbeds",
        errors,
    )

    polling = PollingSettings(
        default_interval_seconds=_int(
            polling_raw.get("defaultIntervalSeconds", 300),
            "settings.polling.defaultIntervalSeconds",
            errors,
            min_value=30,
            max_value=86400,
        ),
        min_interval_seconds=_int(
            polling_raw.get("minIntervalSeconds", 30),
            "settings.polling.minIntervalSeconds",
            errors,
            min_value=30,
            max_value=86400,
        ),
        fetch_timeout_seconds=_int(
            polling_raw.get("fetchTimeoutSeconds", 10),
            "settings.polling.fetchTimeoutSeconds",
            errors,
            min_value=1,
            max_value=120,
        ),
        max_entries_per_feed=_int(
            polling_raw.get("maxEntriesPerFeed", 30),
            "settings.polling.maxEntriesPerFeed",
            errors,
            min_value=1,
            max_value=500,
        ),
        max_concurrent_feed_fetches=_int(
            polling_raw.get("maxConcurrentFeedFetches", 10),
            "settings.polling.maxConcurrentFeedFetches",
            errors,
            min_value=1,
            max_value=100,
        ),
        max_concurrent_email_fetches=_int(
            polling_raw.get("maxConcurrentEmailFetches", 4),
            "settings.polling.maxConcurrentEmailFetches",
            errors,
            min_value=1,
            max_value=50,
        ),
        result_processor_workers=_int(
            polling_raw.get("resultProcessorWorkers", 2),
            "settings.polling.resultProcessorWorkers",
            errors,
            min_value=1,
            max_value=8,
        ),
        backlog_drain_enabled=_bool(
            polling_raw.get("backlogDrainEnabled", True),
            "settings.polling.backlogDrainEnabled",
            errors,
        ),
        post_old_articles_on_first_run=_bool(
            polling_raw.get("postOldArticlesOnFirstRun", False),
            "settings.polling.postOldArticlesOnFirstRun",
            errors,
        ),
    )
    dedupe = DedupeSettings(
        title_match_window_hours=_int(
            dedupe_raw.get("titleMatchWindowHours", 24),
            "settings.dedupe.titleMatchWindowHours",
            errors,
            min_value=1,
            max_value=168,
        )
    )
    failure_backoff = FailureBackoffSettings(
        enabled=_bool(
            failure_backoff_raw.get("enabled", True),
            "settings.failureBackoff.enabled",
            errors,
        ),
        minor_failure_threshold=_int(
            failure_backoff_raw.get("minorFailureThreshold", 10),
            "settings.failureBackoff.minorFailureThreshold",
            errors,
            min_value=1,
            max_value=10000,
        ),
        major_failure_threshold=_int(
            failure_backoff_raw.get("majorFailureThreshold", 100),
            "settings.failureBackoff.majorFailureThreshold",
            errors,
            min_value=1,
            max_value=10000,
        ),
        suspend_failure_threshold=_int(
            failure_backoff_raw.get("suspendFailureThreshold", 500),
            "settings.failureBackoff.suspendFailureThreshold",
            errors,
            min_value=1,
            max_value=100000,
        ),
        minor_retry_seconds=_int(
            failure_backoff_raw.get("minorRetrySeconds", 21600),
            "settings.failureBackoff.minorRetrySeconds",
            errors,
            min_value=300,
            max_value=2592000,
        ),
        major_retry_seconds=_int(
            failure_backoff_raw.get("majorRetrySeconds", 86400),
            "settings.failureBackoff.majorRetrySeconds",
            errors,
            min_value=300,
            max_value=2592000,
        ),
        suspended_retry_seconds=_int(
            failure_backoff_raw.get("suspendedRetrySeconds", 604800),
            "settings.failureBackoff.suspendedRetrySeconds",
            errors,
            min_value=300,
            max_value=2592000,
        ),
    )
    if failure_backoff.minor_failure_threshold > failure_backoff.major_failure_threshold:
        errors.append("settings.failureBackoff.minorFailureThreshold must be less than or equal to majorFailureThreshold.")
    if failure_backoff.major_failure_threshold > failure_backoff.suspend_failure_threshold:
        errors.append("settings.failureBackoff.majorFailureThreshold must be less than or equal to suspendFailureThreshold.")
    if failure_backoff.minor_retry_seconds > failure_backoff.major_retry_seconds:
        errors.append("settings.failureBackoff.minorRetrySeconds must be less than or equal to majorRetrySeconds.")
    if failure_backoff.major_retry_seconds > failure_backoff.suspended_retry_seconds:
        errors.append("settings.failureBackoff.majorRetrySeconds must be less than or equal to suspendedRetrySeconds.")
    timestamps = TimestampSettings(
        allowed_future_skew_minutes=_int(
            timestamps_raw.get("allowedFutureSkewMinutes", 5),
            "settings.timestamps.allowedFutureSkewMinutes",
            errors,
            min_value=0,
            max_value=1440,
        ),
        use_ingested_time_when_missing=_bool(
            timestamps_raw.get("useIngestedTimeWhenMissing", True),
            "settings.timestamps.useIngestedTimeWhenMissing",
            errors,
        ),
        store_raw_timestamps=_bool(
            timestamps_raw.get("storeRawTimestamps", True),
            "settings.timestamps.storeRawTimestamps",
            errors,
        ),
        max_post_age_hours=_int(
            timestamps_raw.get("maxPostAgeHours", 48),
            "settings.timestamps.maxPostAgeHours",
            errors,
            min_value=1,
            max_value=8760,
        ),
    )
    publishing = PublishingSettings(
        seconds_between_posts_per_channel=_float(
            publishing_raw.get("secondsBetweenPostsPerChannel", 1.0),
            "settings.publishing.secondsBetweenPostsPerChannel",
            errors,
            min_value=0.0,
            max_value=60.0,
        ),
        max_queue_size_per_channel=_int(
            publishing_raw.get("maxQueueSizePerChannel", 250),
            "settings.publishing.maxQueueSizePerChannel",
            errors,
            min_value=1,
            max_value=10000,
        ),
        shutdown_drain_seconds=_int(
            publishing_raw.get("shutdownDrainSeconds", 20),
            "settings.publishing.shutdownDrainSeconds",
            errors,
            min_value=0,
            max_value=300,
        ),
    )
    logging_settings = LoggingSettings(
        audit_enabled=_bool(
            logging_raw.get("auditEnabled", False),
            "settings.logging.auditEnabled",
            errors,
        ),
        detailed_errors=_bool(
            logging_raw.get("detailedErrors", False),
            "settings.logging.detailedErrors",
            errors,
        ),
        audit_log_path=_string(
            logging_raw.get("auditLogPath", "logs/rssbot-audit.log"),
            "settings.logging.auditLogPath",
            errors,
        ),
        error_log_path=_string(
            logging_raw.get("errorLogPath", "logs/rssbot-errors.log"),
            "settings.logging.errorLogPath",
            errors,
        ),
        max_bytes=_int(
            logging_raw.get("maxBytes", 10485760),
            "settings.logging.maxBytes",
            errors,
            min_value=1024,
            max_value=104857600,
        ),
        backup_count=_int(
            logging_raw.get("backupCount", 5),
            "settings.logging.backupCount",
            errors,
            min_value=1,
            max_value=50,
        ),
    )
    routing = RoutingSettings(
        enabled=_bool(routing_raw.get("enabled", False), "settings.routing.enabled", errors),
        mode=_choice(
            routing_raw.get("mode", "observe_only"),
            "settings.routing.mode",
            errors,
            {"observe_only", "route_preview", "enforced"},
        ),
        config_dir=_string(
            routing_raw.get("configDir", "config/routing"),
            "settings.routing.configDir",
            errors,
        ),
        max_routing_summary_chars=_int(
            routing_raw.get("maxRoutingSummaryChars", 2000),
            "settings.routing.maxRoutingSummaryChars",
            errors,
            min_value=200,
            max_value=8000,
        ),
    )
    maintenance = MaintenanceSettings(
        enabled=_bool(maintenance_raw.get("enabled", True), "settings.maintenance.enabled", errors),
        interval_hours=_int(
            maintenance_raw.get("intervalHours", 12),
            "settings.maintenance.intervalHours",
            errors,
            min_value=1,
            max_value=168,
        ),
        article_retention_days=_int(
            maintenance_raw.get("articleRetentionDays", 30),
            "settings.maintenance.articleRetentionDays",
            errors,
            min_value=1,
            max_value=3650,
        ),
        posted_retention_days=_int(
            maintenance_raw.get("postedRetentionDays", 30),
            "settings.maintenance.postedRetentionDays",
            errors,
            min_value=1,
            max_value=3650,
        ),
        non_post_retention_hours=_int(
            maintenance_raw.get("nonPostRetentionHours", 24),
            "settings.maintenance.nonPostRetentionHours",
            errors,
            min_value=1,
            max_value=8760,
        ),
        seen_retention_days=_int(
            maintenance_raw.get("seenRetentionDays", 14),
            "settings.maintenance.seenRetentionDays",
            errors,
            min_value=1,
            max_value=3650,
        ),
        feed_entry_seen_retention_days=_int(
            maintenance_raw.get("feedEntrySeenRetentionDays", 90),
            "settings.maintenance.feedEntrySeenRetentionDays",
            errors,
            min_value=1,
            max_value=3650,
        ),
        article_batch_size=_int(
            maintenance_raw.get("articleBatchSize", 500),
            "settings.maintenance.articleBatchSize",
            errors,
            min_value=1,
            max_value=10000,
        ),
        optimize_on_maintenance=_bool(
            maintenance_raw.get("optimizeOnMaintenance", True),
            "settings.maintenance.optimizeOnMaintenance",
            errors,
        ),
        vacuum_on_startup=_bool(
            maintenance_raw.get("vacuumOnStartup", False),
            "settings.maintenance.vacuumOnStartup",
            errors,
        ),
    )
    social_link_embeds = SocialLinkEmbedSettings(
        enabled=_bool(social_link_embeds_raw.get("enabled", True), "settings.socialLinkEmbeds.enabled", errors),
        suppress_original_preview=_bool(
            social_link_embeds_raw.get("suppressOriginalPreview", True),
            "settings.socialLinkEmbeds.suppressOriginalPreview",
            errors,
        ),
        dedupe_window_hours=_int(
            social_link_embeds_raw.get("dedupeWindowHours", 24),
            "settings.socialLinkEmbeds.dedupeWindowHours",
            errors,
            min_value=1,
            max_value=168,
        ),
        max_concurrent_lookups=_int(
            social_link_embeds_raw.get("maxConcurrentLookups", 2),
            "settings.socialLinkEmbeds.maxConcurrentLookups",
            errors,
            min_value=1,
            max_value=10,
        ),
        target_channel_ids=_snowflake_tuple(
            social_link_embeds_raw.get("targetChannelIds", []),
            "settings.socialLinkEmbeds.targetChannelIds",
            errors,
        ),
    )
    return Settings(
        polling=polling,
        failure_backoff=failure_backoff,
        dedupe=dedupe,
        timestamps=timestamps,
        publishing=publishing,
        logging=logging_settings,
        routing=routing,
        maintenance=maintenance,
        social_link_embeds=social_link_embeds,
    )


def _parse_channels(raw: Any, settings: Settings, errors: list[str]) -> list[ChannelConfig]:
    if not isinstance(raw, list):
        errors.append("channels must be a non-empty array.")
        return []
    if not raw:
        errors.append("channels must not be empty.")
        return []

    channels: list[ChannelConfig] = []
    seen_keys: set[str] = set()
    seen_discord_ids: set[str] = set()
    for index, channel_raw in enumerate(raw):
        path = f"channels[{index}]"
        channel_obj = _object(channel_raw, path, errors)
        key = _string(channel_obj.get("key"), f"{path}.key", errors)
        if key and not KEY_RE.match(key):
            errors.append(f"{path}.key must use lowercase letters, numbers, hyphens, or underscores.")
        if key in seen_keys:
            errors.append(f"{path}.key duplicates another channel key: {key}")
        seen_keys.add(key)

        name = _string(channel_obj.get("name"), f"{path}.name", errors)
        discord_channel_id = _string(channel_obj.get("discordChannelId"), f"{path}.discordChannelId", errors)
        if discord_channel_id and not SNOWFLAKE_RE.match(discord_channel_id):
            errors.append(f"{path}.discordChannelId must be a valid Discord channel ID.")
        if discord_channel_id in seen_discord_ids:
            errors.append(f"{path}.discordChannelId duplicates another channel ID: {discord_channel_id}")
        seen_discord_ids.add(discord_channel_id)

        interval_raw = channel_obj.get("pollIntervalSeconds", settings.polling.default_interval_seconds)
        parsed_interval = _int(interval_raw, f"{path}.pollIntervalSeconds", errors, min_value=30, max_value=86400)
        interval = max(parsed_interval, settings.polling.min_interval_seconds)
        feeds = _parse_feeds(
            channel_obj.get("feeds", []),
            path,
            settings,
            errors,
            default_interval_seconds=interval,
            legacy_channel_keys=(key,),
            require_non_empty=False,
        )
        channels.append(
            ChannelConfig(
                key=key,
                name=name,
                discord_channel_id=discord_channel_id,
                poll_interval_seconds=interval,
                feeds=tuple(feeds),
            )
        )
    return channels


def _parse_top_level_feeds(raw: Any, settings: Settings, errors: list[str]) -> list[FeedConfig]:
    if raw in (None, []):
        return []
    return _parse_feeds(
        raw,
        "feeds",
        settings,
        errors,
        default_interval_seconds=settings.polling.default_interval_seconds,
        legacy_channel_keys=(),
        require_non_empty=False,
        path_is_feed_array=True,
    )


def _parse_email_sources(raw: Any, settings: Settings, errors: list[str]) -> list[EmailSourceConfig]:
    path = "emailSources"
    if raw in (None, []):
        return []
    if not isinstance(raw, list):
        errors.append(f"{path} must be an array.")
        return []

    sources: list[EmailSourceConfig] = []
    seen_ids: set[str] = set()
    for index, source_raw in enumerate(raw):
        source_path = f"{path}[{index}]"
        source_obj = _object(source_raw, source_path, errors)
        source_id_value = _string(source_obj.get("id"), f"{source_path}.id", errors)
        if source_id_value and not KEY_RE.match(source_id_value):
            errors.append(f"{source_path}.id must use lowercase letters, numbers, hyphens, or underscores.")
        if source_id_value in seen_ids:
            errors.append(f"{source_path}.id duplicates another email source id: {source_id_value}")
        seen_ids.add(source_id_value)

        name = _string(source_obj.get("name"), f"{source_path}.name", errors)
        from_contains = _lower_string_tuple(source_obj.get("fromContains", []), f"{source_path}.fromContains", errors)
        list_id_contains = _lower_string_tuple(
            source_obj.get("listIdContains", []),
            f"{source_path}.listIdContains",
            errors,
        )
        subject_contains = _lower_string_tuple(
            source_obj.get("subjectContains", []),
            f"{source_path}.subjectContains",
            errors,
        )
        match_all = _bool(source_obj.get("matchAll", False), f"{source_path}.matchAll", errors)
        if match_all and (from_contains or list_id_contains or subject_contains):
            errors.append(f"{source_path}.matchAll cannot be combined with fromContains, listIdContains, or subjectContains.")
        if not match_all and not from_contains and not list_id_contains and not subject_contains:
            errors.append(
                f"{source_path} must set matchAll=true or at least one of fromContains, listIdContains, or subjectContains."
            )

        timeout = source_obj.get("fetchTimeoutSeconds")
        fetch_timeout_seconds = None
        if timeout is not None:
            fetch_timeout_seconds = _int(
                timeout,
                f"{source_path}.fetchTimeoutSeconds",
                errors,
                min_value=1,
                max_value=120,
            )
        interval = source_obj.get("pollIntervalSeconds")
        if interval is None:
            interval_seconds = settings.polling.default_interval_seconds
        else:
            interval_seconds = _int(
                interval,
                f"{source_path}.pollIntervalSeconds",
                errors,
                min_value=30,
                max_value=86400,
            )
        route_policy = _choice(
            source_obj.get("routePolicy", "normal"),
            f"{source_path}.routePolicy",
            errors,
            {"normal", "ignore", "direct"},
        )
        target_channel_ids = _snowflake_tuple(
            source_obj.get("targetChannelIds", []),
            f"{source_path}.targetChannelIds",
            errors,
        )
        fast_lane_channel_ids = _snowflake_tuple(
            source_obj.get("fastLaneChannelIds", []),
            f"{source_path}.fastLaneChannelIds",
            errors,
        )
        if route_policy == "direct" and not target_channel_ids:
            errors.append(f"{source_path}.targetChannelIds is required when routePolicy is direct.")
        initial_backfill_hours = _int(
            source_obj.get("initialBackfillHours", 24),
            f"{source_path}.initialBackfillHours",
            errors,
            min_value=0,
            max_value=168,
        )
        no_match_policy = _choice(
            source_obj.get("noMatchPolicy", "drop"),
            f"{source_path}.noMatchPolicy",
            errors,
            {"drop", "review"},
        )
        max_messages_per_poll_raw = source_obj.get("maxMessagesPerPoll")
        max_messages_per_poll = None
        if max_messages_per_poll_raw is not None:
            max_messages_per_poll = _int(
                max_messages_per_poll_raw,
                f"{source_path}.maxMessagesPerPoll",
                errors,
                min_value=1,
                max_value=500,
            )
        sources.append(
            EmailSourceConfig(
                id=source_id_value,
                name=name,
                imap_host_env=_env_var(
                    source_obj.get("imapHostEnv", "EMAIL_IMAP_HOST"),
                    f"{source_path}.imapHostEnv",
                    errors,
                ),
                imap_port_env=_env_var(
                    source_obj.get("imapPortEnv", "EMAIL_IMAP_PORT"),
                    f"{source_path}.imapPortEnv",
                    errors,
                ),
                username_env=_env_var(
                    source_obj.get("usernameEnv", "EMAIL_USERNAME"),
                    f"{source_path}.usernameEnv",
                    errors,
                ),
                password_env=_env_var(
                    source_obj.get("passwordEnv", "EMAIL_PASSWORD"),
                    f"{source_path}.passwordEnv",
                    errors,
                ),
                mailbox=_string(source_obj.get("mailbox", "INBOX"), f"{source_path}.mailbox", errors),
                from_contains=from_contains,
                list_id_contains=list_id_contains,
                subject_contains=subject_contains,
                match_all=match_all,
                source_id=_optional_key(
                    source_obj.get("sourceId"),
                    f"{source_path}.sourceId",
                    errors,
                    fallback=source_id_value or "email",
                ),
                source_class=_optional_key(
                    source_obj.get("sourceClass"),
                    f"{source_path}.sourceClass",
                    errors,
                    fallback="newsletter",
                ),
                poll_interval_seconds=max(interval_seconds, settings.polling.min_interval_seconds),
                fetch_timeout_seconds=fetch_timeout_seconds,
                route_policy=route_policy,
                initial_backfill_hours=initial_backfill_hours,
                no_match_policy=no_match_policy,
                target_channel_ids=target_channel_ids,
                fast_lane_channel_ids=fast_lane_channel_ids,
                routing_tags=_key_tuple(source_obj.get("routingTags", []), f"{source_path}.routingTags", errors),
                max_messages_per_poll=max_messages_per_poll,
                priority=_bool(source_obj.get("priority", bool(fast_lane_channel_ids)), f"{source_path}.priority", errors),
            )
        )
    return sources


def _parse_feeds(
    raw: Any,
    owner_path: str,
    settings: Settings,
    errors: list[str],
    *,
    default_interval_seconds: int,
    legacy_channel_keys: tuple[str, ...],
    require_non_empty: bool,
    path_is_feed_array: bool = False,
) -> list[FeedConfig]:
    path = owner_path if path_is_feed_array else f"{owner_path}.feeds"
    if not isinstance(raw, list):
        errors.append(f"{path} must be an array.")
        return []
    if require_non_empty and not raw:
        errors.append(f"{path} must not be empty.")
        return []

    feeds: list[FeedConfig] = []
    seen_top_level_ids: set[str] = set()
    for index, feed_raw in enumerate(raw):
        feed_path = f"{path}[{index}]"
        feed_obj = _object(feed_raw, feed_path, errors)
        feed_id = feed_obj.get("id")
        if feed_id is not None:
            feed_id = _string(feed_id, f"{feed_path}.id", errors)
            if feed_id and not KEY_RE.match(feed_id):
                errors.append(f"{feed_path}.id must use lowercase letters, numbers, hyphens, or underscores.")
            if path_is_feed_array and feed_id in seen_top_level_ids:
                errors.append(f"{feed_path}.id duplicates another feed id: {feed_id}")
            seen_top_level_ids.add(feed_id)
        name = _string(feed_obj.get("name"), f"{feed_path}.name", errors)
        url = _string(feed_obj.get("url"), f"{feed_path}.url", errors)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            errors.append(f"{feed_path}.url must be an HTTP or HTTPS URL.")
        source_id = _optional_key(
            feed_obj.get("sourceId"),
            f"{feed_path}.sourceId",
            errors,
            fallback=_derive_source_id(feed_id, name),
        )
        source_class = _optional_key(
            feed_obj.get("sourceClass"),
            f"{feed_path}.sourceClass",
            errors,
            fallback=_derive_source_class(source_id),
        )
        interval = feed_obj.get("pollIntervalSeconds")
        if interval is None:
            interval_seconds = max(default_interval_seconds, settings.polling.min_interval_seconds)
        else:
            parsed_interval = _int(
                interval,
                f"{feed_path}.pollIntervalSeconds",
                errors,
                min_value=30,
                max_value=86400,
            )
            interval_seconds = max(parsed_interval, settings.polling.min_interval_seconds)
        timeout = feed_obj.get("fetchTimeoutSeconds")
        if timeout is None:
            timeout_seconds = None
        else:
            timeout_seconds = _int(
                timeout,
                f"{feed_path}.fetchTimeoutSeconds",
                errors,
                min_value=1,
                max_value=120,
            )
        route_policy = _choice(
            feed_obj.get("routePolicy", "normal"),
            f"{feed_path}.routePolicy",
            errors,
            {"normal", "ignore"},
        )
        configured_legacy_keys = _string_list(
            feed_obj.get("legacyChannelKeys", list(legacy_channel_keys)),
            f"{feed_path}.legacyChannelKeys",
            errors,
        )
        configured_mirror_keys = _string_list(
            feed_obj.get("mirrorChannelKeys", []),
            f"{feed_path}.mirrorChannelKeys",
            errors,
        )
        feeds.append(
            FeedConfig(
                id=feed_id,
                name=name,
                url=url,
                source_id=source_id,
                source_class=source_class,
                poll_interval_seconds=interval_seconds,
                fetch_timeout_seconds=timeout_seconds,
                route_policy=route_policy,
                legacy_channel_keys=tuple(configured_legacy_keys),
                mirror_channel_keys=tuple(configured_mirror_keys),
            )
        )
    return feeds


def _derive_source_id(feed_id: str | None, name: str) -> str:
    if feed_id:
        return feed_id
    normalized = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return normalized[:63] or "unknown"


def _derive_source_class(source_id: str) -> str:
    return SOURCE_CLASS_BY_ID.get(source_id, "unknown")


def _optional_key(value: Any, path: str, errors: list[str], fallback: str) -> str:
    if value is None:
        return fallback
    parsed = _string(value, path, errors)
    if parsed and not KEY_RE.match(parsed):
        errors.append(f"{path} must use lowercase letters, numbers, hyphens, or underscores.")
    return parsed or fallback


def _env_var(value: Any, path: str, errors: list[str]) -> str:
    parsed = _string(value, path, errors)
    if parsed and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", parsed):
        errors.append(f"{path} must be an uppercase environment variable name.")
    return parsed


def _string_list(value: Any, path: str, errors: list[str]) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{path} must be an array of strings.")
        return []
    parsed: list[str] = []
    for index, item in enumerate(value):
        if isinstance(item, str) and item.strip():
            parsed.append(item.strip())
        else:
            errors.append(f"{path}[{index}] must be a non-empty string.")
    return parsed


def _lower_string_tuple(value: Any, path: str, errors: list[str]) -> tuple[str, ...]:
    return tuple(item.casefold() for item in _string_list(value, path, errors))


def _key_tuple(value: Any, path: str, errors: list[str]) -> tuple[str, ...]:
    values = _string_list(value, path, errors)
    for item in values:
        if not KEY_RE.match(item):
            errors.append(f"{path} values must use lowercase letters, numbers, hyphens, or underscores.")
    return tuple(values)


def _snowflake_tuple(value: Any, path: str, errors: list[str]) -> tuple[str, ...]:
    values = _string_list(value, path, errors)
    for item in values:
        if not SNOWFLAKE_RE.match(item):
            errors.append(f"{path} values must be valid Discord channel IDs.")
    return tuple(values)


def _object(value: Any, path: str, errors: list[str]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    errors.append(f"{path} must be an object.")
    return {}


def _string(value: Any, path: str, errors: list[str]) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    errors.append(f"{path} is missing or must be a non-empty string.")
    return ""


def _int(value: Any, path: str, errors: list[str], min_value: int | None = None, max_value: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{path} must be an integer.")
        return 0
    if min_value is not None and value < min_value:
        errors.append(f"{path} must be at least {min_value}.")
    if max_value is not None and value > max_value:
        errors.append(f"{path} must be at most {max_value}.")
    return value


def _float(value: Any, path: str, errors: list[str], min_value: float, max_value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(f"{path} must be a number.")
        return 0.0
    parsed = float(value)
    if parsed < min_value:
        errors.append(f"{path} must be at least {min_value}.")
    if parsed > max_value:
        errors.append(f"{path} must be at most {max_value}.")
    return parsed


def _bool(value: Any, path: str, errors: list[str]) -> bool:
    if isinstance(value, bool):
        return value
    errors.append(f"{path} must be true or false.")
    return False


def _choice(value: Any, path: str, errors: list[str], allowed: set[str]) -> str:
    if isinstance(value, str) and value in allowed:
        return value
    errors.append(f"{path} must be one of: {', '.join(sorted(allowed))}.")
    return sorted(allowed)[0]
