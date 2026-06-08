from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class BotSettings:
    name: str = "RSS Dispatch Bot"
    timezone: str = "America/Chicago"


@dataclass(frozen=True)
class DiscordSettings:
    guild_id_env: str = "DISCORD_GUILD_ID"


@dataclass(frozen=True)
class PollingSettings:
    default_interval_seconds: int = 300
    min_interval_seconds: int = 30
    fetch_timeout_seconds: int = 10
    max_entries_per_feed: int = 30
    max_concurrent_feed_fetches: int = 10
    post_old_articles_on_first_run: bool = False


@dataclass(frozen=True)
class FailureBackoffSettings:
    enabled: bool = True
    minor_failure_threshold: int = 10
    major_failure_threshold: int = 100
    suspend_failure_threshold: int = 500
    minor_retry_seconds: int = 21600
    major_retry_seconds: int = 86400
    suspended_retry_seconds: int = 604800


@dataclass(frozen=True)
class DedupeSettings:
    title_match_window_hours: int = 24


@dataclass(frozen=True)
class TimestampSettings:
    allowed_future_skew_minutes: int = 5
    use_ingested_time_when_missing: bool = True
    store_raw_timestamps: bool = True
    max_post_age_hours: int = 48


@dataclass(frozen=True)
class PublishingSettings:
    seconds_between_posts_per_channel: float = 1.0
    max_queue_size_per_channel: int = 250
    shutdown_drain_seconds: int = 20


@dataclass(frozen=True)
class LoggingSettings:
    audit_enabled: bool = False
    detailed_errors: bool = False
    audit_log_path: str = "logs/rssbot-audit.log"
    error_log_path: str = "logs/rssbot-errors.log"
    max_bytes: int = 10485760
    backup_count: int = 5


@dataclass(frozen=True)
class RoutingSettings:
    enabled: bool = False
    mode: str = "observe_only"
    config_dir: str = "config/routing"


@dataclass(frozen=True)
class MaintenanceSettings:
    enabled: bool = True
    interval_hours: int = 12
    article_retention_days: int = 30
    posted_retention_days: int = 30
    non_post_retention_hours: int = 24
    seen_retention_days: int = 14
    feed_entry_seen_retention_days: int = 90
    article_batch_size: int = 500
    optimize_on_maintenance: bool = True
    vacuum_on_startup: bool = False


@dataclass(frozen=True)
class Settings:
    polling: PollingSettings = field(default_factory=PollingSettings)
    failure_backoff: FailureBackoffSettings = field(default_factory=FailureBackoffSettings)
    dedupe: DedupeSettings = field(default_factory=DedupeSettings)
    timestamps: TimestampSettings = field(default_factory=TimestampSettings)
    publishing: PublishingSettings = field(default_factory=PublishingSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    routing: RoutingSettings = field(default_factory=RoutingSettings)
    maintenance: MaintenanceSettings = field(default_factory=MaintenanceSettings)


@dataclass(frozen=True)
class FeedConfig:
    name: str
    url: str
    id: str | None = None
    source_id: str = "unknown"
    source_class: str = "unknown"
    poll_interval_seconds: int | None = None
    fetch_timeout_seconds: int | None = None
    route_policy: str = "normal"
    legacy_channel_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChannelConfig:
    key: str
    name: str
    discord_channel_id: str
    poll_interval_seconds: int | None
    feeds: tuple[FeedConfig, ...]


@dataclass(frozen=True)
class AppConfig:
    version: int
    bot: BotSettings
    discord: DiscordSettings
    settings: Settings
    feeds: tuple[FeedConfig, ...]
    channels: tuple[ChannelConfig, ...]
    raw: dict[str, Any]


@dataclass(frozen=True)
class FeedRuntime:
    feed_key: str
    display_name: str
    url: str
    normalized_url: str
    interval_seconds: int
    channel_ids: tuple[str, ...]
    channel_keys: tuple[str, ...]
    fetch_timeout_seconds: int | None = None
    source_id: str = "unknown"
    source_class: str = "unknown"
    route_policy: str = "normal"


@dataclass(frozen=True)
class FeedEntry:
    feed_key: str
    feed_name: str
    raw_guid: str | None
    raw_title: str
    raw_url: str | None
    summary: str | None
    image_url: str | None
    image_source: str | None
    raw_published_at: str | None
    parsed: dict[str, Any]
    source_id: str = "unknown"
    source_class: str = "unknown"


@dataclass(frozen=True)
class TimestampResult:
    raw_published_at: str | None
    normalized_published_at: datetime
    ingested_at: datetime
    timestamp_status: str


@dataclass(frozen=True)
class ArticleCandidate:
    feed_key: str
    source_name: str
    source_id: str
    source_class: str
    title: str
    normalized_title: str
    title_signature: str
    source_family: str
    story_cluster_key: str
    url: str | None
    normalized_url: str | None
    summary: str | None
    image_url: str | None
    image_source: str | None
    raw_guid: str | None
    raw_published_at: str | None
    normalized_published_at: datetime
    ingested_at: datetime
    timestamp_status: str
    fingerprints: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DedupeResult:
    article_id: int
    is_new_article: bool


@dataclass(frozen=True)
class PostJob:
    article_id: int
    channel_id: str
    title: str
    url: str | None
    summary: str | None
    image_url: str | None
    image_source: str | None
    source_name: str
    normalized_published_at: datetime
    timestamp_status: str = "valid"
    source_id: str = "unknown"
    source_class: str = "unknown"
