from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import aiohttp

from app.database import Database
from app.feed_fetcher import FeedFetchError, FeedFetchResult, FeedService
from app.models import AppConfig, FeedRuntime
from app.normalizer import build_candidate, normalize_feed_url, stable_hash
from app.publisher import PublisherService
from app.routing import RoutingConfigError, RoutingEngine, load_routing_config
from app.routing.models import RoutingArticle, RoutingDecision

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("app.audit")


@dataclass(frozen=True)
class RefreshSummary:
    feeds_checked: int = 0
    new_articles: int = 0
    duplicates_skipped: int = 0
    posts_queued: int = 0
    errors: int = 0


class SchedulerService:
    def __init__(self, db: Database, publisher: PublisherService) -> None:
        self.db = db
        self.publisher = publisher
        self.config: AppConfig | None = None
        self.feeds: dict[str, FeedRuntime] = {}
        self.channel_to_feed_keys: dict[str, tuple[str, ...]] = {}
        self.channel_key_to_id: dict[str, str] = {}
        self.routing_engine: RoutingEngine | None = None
        self.routing_mode = "observe_only"
        self._next_due: dict[str, datetime] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_next_available: dict[str, datetime] = {}
        self._task: asyncio.Task[None] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._stopping = False
        self._idle_sleep_seconds = 5
        self._next_maintenance_at = datetime.now(UTC) + timedelta(hours=2)

    def configure(self, config: AppConfig) -> None:
        self.config = config
        self.feeds = build_feed_runtime_map(config)
        self.channel_key_to_id = {channel.key: channel.discord_channel_id for channel in config.channels}
        self.routing_engine = None
        self.routing_mode = config.settings.routing.mode
        self._next_maintenance_at = datetime.now(UTC) + timedelta(
            hours=max(1, config.settings.maintenance.interval_hours)
        )
        if config.settings.routing.enabled:
            try:
                routing_config = load_routing_config(config.settings.routing.config_dir, config)
                self.routing_engine = RoutingEngine(routing_config)
                logger.info(
                    "Routing config loaded: %s tags, %s knowledge entries, %s channel rules, mode=%s",
                    len(routing_config.taxonomy),
                    len(routing_config.knowledge_entries),
                    len(routing_config.channel_rules),
                    self.routing_mode,
                )
                audit_logger.info(
                    "routing_config_loaded taxonomy_version=%s knowledge_base_version=%s channels_version=%s mode=%s",
                    routing_config.taxonomy_version,
                    routing_config.knowledge_base_version,
                    routing_config.channels_version,
                    self.routing_mode,
                )
            except RoutingConfigError as exc:
                self.routing_mode = "observe_only"
                logger.error("Routing config failed validation; routing enforcement disabled: %s", "; ".join(exc.errors))
                audit_logger.error("routing_config_invalid errors=%r", exc.errors)
        self.channel_to_feed_keys = {}
        for feed in self.feeds.values():
            for channel_id in feed.channel_ids:
                self.channel_to_feed_keys.setdefault(channel_id, tuple())
        temp: dict[str, list[str]] = {channel_id: [] for channel_id in self.channel_to_feed_keys}
        for feed in self.feeds.values():
            for channel_id in feed.channel_ids:
                temp[channel_id].append(feed.feed_key)
        self.channel_to_feed_keys = {channel_id: tuple(keys) for channel_id, keys in temp.items()}
        now = datetime.now(UTC)
        for feed_key in self.feeds:
            self._next_due.setdefault(feed_key, now)
        for removed in set(self._next_due) - set(self.feeds):
            del self._next_due[removed]
        logger.info("Configured %s unique feeds", len(self.feeds))
        audit_logger.info(
            "config_applied unique_feeds=%s channels=%s audit_enabled=%s detailed_errors=%s",
            len(self.feeds),
            len(config.channels),
            config.settings.logging.audit_enabled,
            config.settings.logging.detailed_errors,
        )

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping = False
            self._task = asyncio.create_task(self._run())

    async def shutdown(self) -> None:
        logger.info("Scheduler shutdown requested")
        self._stopping = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        async with self._new_client_session() as session:
            self._session = session
            try:
                while not self._stopping:
                    if not self.config:
                        await asyncio.sleep(1)
                        continue
                    now = datetime.now(UTC)
                    due_feeds = [
                        feed for feed_key, feed in self.feeds.items() if self._next_due.get(feed_key, now) <= now
                    ]
                    if due_feeds:
                        await self.poll_feeds(due_feeds)
                        self._maybe_prune_runtime_history()
                    await asyncio.sleep(self._seconds_until_next_poll())
            except asyncio.CancelledError:
                logger.info("Scheduler loop cancelled")
                raise
            finally:
                self._session = None

    def _seconds_until_next_poll(self) -> float:
        if not self._next_due:
            return float(self._idle_sleep_seconds)
        now = datetime.now(UTC)
        next_due = min(self._next_due.values())
        return max(1.0, min(float(self._idle_sleep_seconds), (next_due - now).total_seconds()))

    def next_poll_at(self) -> datetime | None:
        if not self._next_due:
            return None
        return min(self._next_due.values())

    async def refresh_channel(self, channel_id: str) -> RefreshSummary:
        feed_keys = self.channel_to_feed_keys.get(channel_id)
        if not feed_keys:
            raise ValueError("This Discord channel is not configured for RSS refresh.")
        feeds = [self.feeds[key] for key in feed_keys if key in self.feeds]
        return await self.poll_feeds(feeds)

    def _maybe_prune_runtime_history(self) -> None:
        if not self.config or not self.config.settings.maintenance.enabled:
            return
        now = datetime.now(UTC)
        if now < self._next_maintenance_at:
            return
        maintenance = self.config.settings.maintenance
        self._next_maintenance_at = now + timedelta(hours=maintenance.interval_hours)
        try:
            before_bytes = self.db.database_size_bytes()
            stats = self.db.prune_runtime_history(
                article_retention_days=maintenance.article_retention_days,
                posted_retention_days=maintenance.posted_retention_days,
                non_post_retention_hours=maintenance.non_post_retention_hours,
                seen_retention_days=maintenance.seen_retention_days,
                feed_entry_seen_retention_days=maintenance.feed_entry_seen_retention_days,
                article_batch_size=maintenance.article_batch_size,
            )
            if maintenance.optimize_on_maintenance:
                self.db.optimize()
            after_bytes = self.db.database_size_bytes()
            if before_bytes != after_bytes:
                stats["database_bytes_before"] = before_bytes
                stats["database_bytes_after"] = after_bytes
        except Exception:
            logger.exception("Runtime DB maintenance failed")
            audit_logger.exception("runtime_db_maintenance_failed")
            return
        if any(stats.values()):
            logger.info("Runtime DB maintenance pruned rows: %s", stats)
            audit_logger.info("runtime_db_maintenance stats=%r", stats)

    async def poll_feeds(self, feeds: list[FeedRuntime]) -> RefreshSummary:
        if not self.config or not feeds:
            return RefreshSummary()
        feed_service = FeedService(
            timeout_seconds=self.config.settings.polling.fetch_timeout_seconds,
            max_entries_per_feed=self.config.settings.polling.max_entries_per_feed,
        )
        semaphore = asyncio.Semaphore(self.config.settings.polling.max_concurrent_feed_fetches)
        summary = RefreshSummary()

        session = self._session
        if session is not None and not session.closed:
            return await self._poll_feeds_with_session(session, feed_service, semaphore, feeds, summary)
        async with self._new_client_session() as temp_session:
            return await self._poll_feeds_with_session(temp_session, feed_service, semaphore, feeds, summary)

    def _new_client_session(self) -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(limit=64, limit_per_host=4, ttl_dns_cache=300)
        return aiohttp.ClientSession(max_field_size=32768, connector=connector)

    async def _poll_feeds_with_session(
        self,
        session: aiohttp.ClientSession,
        feed_service: FeedService,
        semaphore: asyncio.Semaphore,
        feeds: list[FeedRuntime],
        summary: RefreshSummary,
    ) -> RefreshSummary:
        tasks = [
            asyncio.create_task(self._fetch_with_status(session, feed_service, semaphore, feed))
            for feed in feeds
        ]
        try:
            for completed in asyncio.as_completed(tasks):
                try:
                    result = await completed
                except FeedFetchError:
                    summary = _merge(summary, errors=1)
                    continue
                except Exception:
                    logger.exception("Unexpected feed fetch task failure")
                    audit_logger.exception("feed_task_exception")
                    summary = _merge(summary, errors=1)
                    continue
                try:
                    feed_summary = await self._process_feed_result(result)
                except Exception:
                    logger.exception("Feed processing failed for %s", result.feed.display_name)
                    audit_logger.exception(
                        "feed_processing_exception feed_key=%s feed_name=%r",
                        result.feed.feed_key,
                        result.feed.display_name,
                    )
                    summary = _merge(summary, errors=1)
                    continue
                summary = _combine(summary, feed_summary)
        finally:
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return summary

    async def _fetch_with_status(
        self,
        session: aiohttp.ClientSession,
        feed_service: FeedService,
        semaphore: asyncio.Semaphore,
        feed: FeedRuntime,
    ) -> FeedFetchResult:
        first_success = self.db.is_first_feed_success(feed.feed_key)
        await self._throttle_feed_host(feed)
        async with semaphore:
            try:
                result = await feed_service.fetch(session, feed)
                result = FeedFetchResult(feed=result.feed, entries=result.entries, first_success=first_success)
                next_due = datetime.now(UTC) + timedelta(seconds=feed.interval_seconds)
                self._next_due[feed.feed_key] = next_due
                self.db.mark_feed_success(feed.feed_key, feed.display_name, feed.url, next_due)
                logger.debug("Fetched %s entries from %s", len(result.entries), feed.display_name)
                audit_logger.debug(
                    "feed_success feed_key=%s feed_name=%r entries=%s first_success=%s next_poll_at=%s",
                    feed.feed_key,
                    feed.display_name,
                    len(result.entries),
                    first_success,
                    next_due.isoformat(),
                )
                return result
            except FeedFetchError as exc:
                next_due = datetime.now(UTC) + timedelta(seconds=self._failure_retry_seconds(feed, exc))
                self._next_due[feed.feed_key] = next_due
                self.db.mark_feed_failure(feed.feed_key, feed.display_name, feed.url, str(exc), next_due)
                if self.config and self.config.settings.logging.detailed_errors:
                    logger.exception("Feed failed: %s: %r", feed.display_name, exc)
                else:
                    logger.warning("Feed failed: %s: %s", feed.display_name, exc)
                audit_logger.error(
                    "feed_failure feed_key=%s feed_name=%r url=%r error=%r next_poll_at=%s",
                    feed.feed_key,
                    feed.display_name,
                    feed.url,
                    exc,
                    next_due.isoformat(),
                )
                raise

    async def _process_feed_result(self, result: FeedFetchResult) -> RefreshSummary:
        if not self.config:
            return RefreshSummary()
        first_success_limited_backfill = (
            result.first_success and not self.config.settings.polling.post_old_articles_on_first_run
        )
        new_articles = 0
        duplicates = 0
        posts_queued = 0

        for entry in result.entries:
            candidate = build_candidate(entry, self.config.settings.timestamps)
            if self.db.has_feed_entry_seen(candidate):
                duplicates += 1
                continue
            if first_success_limited_backfill and not self._is_recent_valid_for_first_success(
                candidate.normalized_published_at,
                candidate.timestamp_status,
            ):
                duplicates += 1
                self.db.record_feed_entry_seen(candidate)
                if candidate.timestamp_status in {"valid", "timezone_corrected"}:
                    audit_logger.debug(
                        "post_skipped_stale feed_key=%s channel_count=%s published_at=%s title=%r",
                        result.feed.feed_key,
                        len(result.feed.channel_ids),
                        candidate.normalized_published_at.isoformat(),
                        candidate.title,
                    )
                else:
                    audit_logger.debug(
                        "post_suppressed_first_run feed_key=%s channel_count=%s timestamp_status=%s title=%r",
                        result.feed.feed_key,
                        len(result.feed.channel_ids),
                        candidate.timestamp_status,
                        candidate.title,
                    )
                continue
            if self._is_stale_for_posting(candidate.normalized_published_at, candidate.timestamp_status):
                duplicates += 1
                self.db.record_feed_entry_seen(candidate)
                audit_logger.debug(
                    "post_skipped_stale feed_key=%s channel_count=%s published_at=%s title=%r",
                    result.feed.feed_key,
                    len(result.feed.channel_ids),
                    candidate.normalized_published_at.isoformat(),
                    candidate.title,
                )
                continue
            dedupe = self.db.resolve_article(candidate, self.config.settings.dedupe.title_match_window_hours)
            if dedupe.is_new_article:
                new_articles += 1
            else:
                duplicates += 1
            persist_routing = dedupe.is_new_article or not self.db.has_routing_decision(dedupe.article_id)
            routing_decision = self._route_candidate(dedupe.article_id, candidate, persist=persist_routing)
            for channel_id in self._target_channel_ids(result.feed.channel_ids, routing_decision):
                if self.db.has_channel_post(dedupe.article_id, channel_id):
                    duplicates += 1
                    continue
                if self.db.has_channel_title(channel_id, candidate.normalized_title, candidate.source_id):
                    duplicates += 1
                    self.db.record_channel_skipped(
                        dedupe.article_id,
                        channel_id,
                        candidate.normalized_title,
                        candidate.title_signature,
                        "duplicate_same_source",
                        candidate.source_id,
                        candidate.story_cluster_key,
                    )
                    audit_logger.debug(
                        "post_skipped_duplicate_same_source feed_key=%s article_id=%s channel_id=%s source_id=%s normalized_title=%r title=%r",
                        result.feed.feed_key,
                        dedupe.article_id,
                        channel_id,
                        candidate.source_id,
                        candidate.normalized_title,
                        candidate.title,
                    )
                    continue
                if self.db.has_channel_title_signature(channel_id, candidate.title_signature, candidate.source_id):
                    duplicates += 1
                    self.db.record_channel_skipped(
                        dedupe.article_id,
                        channel_id,
                        candidate.normalized_title,
                        candidate.title_signature,
                        "duplicate_title_signature_same_source",
                        candidate.source_id,
                        candidate.story_cluster_key,
                    )
                    audit_logger.debug(
                        "post_skipped_duplicate_title_signature_same_source feed_key=%s article_id=%s channel_id=%s source_id=%s title_signature=%r title=%r",
                        result.feed.feed_key,
                        dedupe.article_id,
                        channel_id,
                        candidate.source_id,
                        candidate.title_signature,
                        candidate.title,
                    )
                    continue
                if self.db.has_channel_story_source(channel_id, candidate.story_cluster_key, candidate.source_id):
                    duplicates += 1
                    self.db.record_channel_skipped(
                        dedupe.article_id,
                        channel_id,
                        candidate.normalized_title,
                        candidate.title_signature,
                        "duplicate_same_source",
                        candidate.source_id,
                        candidate.story_cluster_key,
                    )
                    audit_logger.debug(
                        "post_skipped_duplicate_story_same_source feed_key=%s article_id=%s channel_id=%s source_id=%s story_cluster_key=%s title=%r",
                        result.feed.feed_key,
                        dedupe.article_id,
                        channel_id,
                        candidate.source_id,
                        candidate.story_cluster_key,
                        candidate.title,
                    )
                    continue
                if self.db.channel_story_source_count(channel_id, candidate.story_cluster_key) >= 5:
                    duplicates += 1
                    self.db.record_channel_skipped(
                        dedupe.article_id,
                        channel_id,
                        candidate.normalized_title,
                        candidate.title_signature,
                        "cluster_cap",
                        candidate.source_id,
                        candidate.story_cluster_key,
                        reserve_seen=False,
                    )
                    audit_logger.debug(
                        "post_skipped_cluster_cap feed_key=%s article_id=%s channel_id=%s source_id=%s story_cluster_key=%s title=%r",
                        result.feed.feed_key,
                        dedupe.article_id,
                        channel_id,
                        candidate.source_id,
                        candidate.story_cluster_key,
                        candidate.title,
                    )
                    continue
                if not self.db.reserve_channel_title(
                    dedupe.article_id,
                    channel_id,
                    candidate.normalized_title,
                    candidate.title_signature,
                    "queued",
                    candidate.source_id,
                    candidate.story_cluster_key,
                ):
                    duplicates += 1
                    continue
                job = self.db.get_post_job(dedupe.article_id, channel_id)
                if await self.publisher.enqueue(job):
                    posts_queued += 1
                    audit_logger.debug(
                        "post_queued feed_key=%s article_id=%s channel_id=%s title=%r",
                        result.feed.feed_key,
                        dedupe.article_id,
                        channel_id,
                        candidate.title,
                    )
        audit_logger.debug(
            "feed_processed feed_key=%s entries=%s new_articles=%s duplicates=%s posts_queued=%s suppressed=%s",
            result.feed.feed_key,
            len(result.entries),
            new_articles,
            duplicates,
            posts_queued,
            first_success_limited_backfill,
        )
        return RefreshSummary(
            feeds_checked=1,
            new_articles=new_articles,
            duplicates_skipped=duplicates,
            posts_queued=posts_queued,
            errors=0,
        )

    def _is_stale_for_posting(self, published_at: datetime, timestamp_status: str) -> bool:
        if not self.config:
            return False
        if timestamp_status not in {"valid", "timezone_corrected"}:
            return False
        cutoff = datetime.now(UTC) - timedelta(hours=self.config.settings.timestamps.max_post_age_hours)
        return published_at < cutoff

    def _is_recent_valid_for_first_success(self, published_at: datetime, timestamp_status: str) -> bool:
        if timestamp_status not in {"valid", "timezone_corrected"}:
            return False
        return not self._is_stale_for_posting(published_at, timestamp_status)

    async def _throttle_feed_host(self, feed: FeedRuntime) -> None:
        host = urlparse(feed.url).netloc.casefold()
        if host not in {"bsky.app", "www.dvidshub.net"}:
            return
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = datetime.now(UTC)
            next_available = self._host_next_available.get(host, now)
            if next_available > now:
                await asyncio.sleep((next_available - now).total_seconds())
            delay_seconds = 2 if host == "bsky.app" else 3
            self._host_next_available[host] = datetime.now(UTC) + timedelta(seconds=delay_seconds)

    def _failure_retry_seconds(self, feed: FeedRuntime, exc: FeedFetchError) -> int:
        host = urlparse(feed.url).netloc.casefold()
        if host == "www.dvidshub.net" and "waf action=challenge" in str(exc).casefold():
            return max(feed.interval_seconds, 900)
        if host != "bsky.app":
            return max(feed.interval_seconds, 3600)
        match = re.search(r"retry after (\d+)s", str(exc), re.IGNORECASE)
        if match:
            return max(feed.interval_seconds, int(match.group(1)))
        if "rate limited" in str(exc).casefold():
            return max(feed.interval_seconds, 900)
        return feed.interval_seconds

    def _route_candidate(self, article_id: int, candidate, *, persist: bool = True) -> RoutingDecision | None:
        if self.routing_engine is None:
            return None
        try:
            decision = self.routing_engine.route(
                RoutingArticle(
                    article_id=article_id,
                    title=candidate.title,
                    summary=candidate.summary,
                    source_name=candidate.source_name,
                    source_id=candidate.source_id,
                    source_class=candidate.source_class,
                    url=candidate.url,
                    normalized_title=candidate.normalized_title,
                )
            )
            selected_ids = [self.channel_key_to_id[key] for key in decision.final_channel_keys if key in self.channel_key_to_id]
            if persist:
                self.db.record_routing_decision(article_id, decision, selected_ids)
            audit_logger.debug(
                "routing_decision article_id=%s status=%s final_keys=%s top_score=%s source_id=%s title=%r",
                article_id,
                decision.decision_status,
                list(decision.final_channel_keys),
                decision.top_score,
                candidate.source_id,
                candidate.title,
            )
            return decision
        except Exception as exc:
            logger.exception("Routing failed for article %s", article_id)
            audit_logger.exception("routing_exception article_id=%s title=%r error=%r", article_id, candidate.title, exc)
            return None

    def _target_channel_ids(
        self,
        existing_channel_ids: tuple[str, ...],
        decision: RoutingDecision | None,
    ) -> tuple[str, ...]:
        if self.routing_mode != "enforced" or decision is None:
            return existing_channel_ids
        if decision.decision_status not in {"routed", "review"}:
            return tuple()
        selected = tuple(
            self.channel_key_to_id[key]
            for key in decision.final_channel_keys
            if key in self.channel_key_to_id
        )
        return selected


def build_feed_runtime_map(config: AppConfig) -> dict[str, FeedRuntime]:
    grouped: dict[str, dict[str, object]] = {}
    channel_by_key = {channel.key: channel for channel in config.channels}
    feed_configs: list[FeedRuntime] = []
    for feed in config.feeds:
        channel_ids = []
        channel_keys = []
        for key in feed.legacy_channel_keys:
            channel = channel_by_key.get(key)
            if channel is None:
                continue
            channel_ids.append(channel.discord_channel_id)
            channel_keys.append(channel.key)
        feed_configs.append(
            FeedRuntime(
                feed_key=feed.id or f"feed_{stable_hash(normalize_feed_url(feed.url))}",
                display_name=feed.name,
                url=feed.url,
                normalized_url=normalize_feed_url(feed.url),
                interval_seconds=feed.poll_interval_seconds or config.settings.polling.default_interval_seconds,
                source_id=feed.source_id,
                source_class=feed.source_class,
                route_policy=feed.route_policy,
                channel_ids=tuple(channel_ids),
                channel_keys=tuple(channel_keys),
            )
        )
    for channel in config.channels:
        interval = channel.poll_interval_seconds or config.settings.polling.default_interval_seconds
        for feed in channel.feeds:
            feed_configs.append(
                FeedRuntime(
                    feed_key=feed.id or f"feed_{stable_hash(normalize_feed_url(feed.url))}",
                    display_name=feed.name,
                    url=feed.url,
                    normalized_url=normalize_feed_url(feed.url),
                    interval_seconds=feed.poll_interval_seconds or interval,
                    source_id=feed.source_id,
                    source_class=feed.source_class,
                    route_policy=feed.route_policy,
                    channel_ids=(channel.discord_channel_id,),
                    channel_keys=(channel.key,),
                )
            )
    for feed in feed_configs:
        if feed.route_policy == "ignore":
            continue
        normalized_url = normalize_feed_url(feed.url)
        group = grouped.setdefault(
            normalized_url,
            {
                "feed_key": feed.feed_key,
                "display_name": feed.display_name,
                "url": feed.url,
                "normalized_url": normalized_url,
                "interval_seconds": feed.interval_seconds,
                "source_id": feed.source_id,
                "source_class": feed.source_class,
                "route_policy": feed.route_policy,
                "channel_ids": [],
                "channel_keys": [],
            },
        )
        group["interval_seconds"] = min(int(group["interval_seconds"]), feed.interval_seconds)
        cast_channel_ids = group["channel_ids"]
        cast_channel_keys = group["channel_keys"]
        assert isinstance(cast_channel_ids, list)
        assert isinstance(cast_channel_keys, list)
        for channel_id, channel_key in zip(feed.channel_ids, feed.channel_keys, strict=False):
            if channel_id not in cast_channel_ids:
                cast_channel_ids.append(channel_id)
                cast_channel_keys.append(channel_key)
    return {
        str(group["feed_key"]): FeedRuntime(
            feed_key=str(group["feed_key"]),
            display_name=str(group["display_name"]),
            url=str(group["url"]),
            normalized_url=str(group["normalized_url"]),
            interval_seconds=int(group["interval_seconds"]),
            source_id=str(group["source_id"]),
            source_class=str(group["source_class"]),
            route_policy=str(group["route_policy"]),
            channel_ids=tuple(group["channel_ids"]),
            channel_keys=tuple(group["channel_keys"]),
        )
        for group in grouped.values()
    }


def _merge(summary: RefreshSummary, **changes: int) -> RefreshSummary:
    return RefreshSummary(
        feeds_checked=summary.feeds_checked + changes.get("feeds_checked", 0),
        new_articles=summary.new_articles + changes.get("new_articles", 0),
        duplicates_skipped=summary.duplicates_skipped + changes.get("duplicates_skipped", 0),
        posts_queued=summary.posts_queued + changes.get("posts_queued", 0),
        errors=summary.errors + changes.get("errors", 0),
    )


def _combine(a: RefreshSummary, b: RefreshSummary) -> RefreshSummary:
    return RefreshSummary(
        feeds_checked=a.feeds_checked + b.feeds_checked,
        new_articles=a.new_articles + b.new_articles,
        duplicates_skipped=a.duplicates_skipped + b.duplicates_skipped,
        posts_queued=a.posts_queued + b.posts_queued,
        errors=a.errors + b.errors,
    )
