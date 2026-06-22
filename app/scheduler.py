from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import aiohttp

from app.database import Database
from app.email_ingest import EmailIngestService
from app.feed_fetcher import FeedFetchError, FeedFetchResult, FeedService
from app.models import AppConfig, EmailSourceRuntime, FeedRuntime, MaintenanceSettings
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


@dataclass(frozen=True)
class FetchBatchResult:
    results: tuple[FeedFetchResult, ...] = ()
    errors: int = 0


class SchedulerService:
    def __init__(self, db: Database, publisher: PublisherService) -> None:
        self.db = db
        self.publisher = publisher
        self.config: AppConfig | None = None
        self.feeds: dict[str, FeedRuntime] = {}
        self.email_sources: dict[str, EmailSourceRuntime] = {}
        self.channel_to_feed_keys: dict[str, tuple[str, ...]] = {}
        self.channel_key_to_id: dict[str, str] = {}
        self.routing_engine: RoutingEngine | None = None
        self.routing_mode = "observe_only"
        self._next_due: dict[str, datetime] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_next_available: dict[str, datetime] = {}
        self._task: asyncio.Task[None] | None = None
        self._email_task: asyncio.Task[None] | None = None
        self._processor_task: asyncio.Task[None] | None = None
        self._maintenance_task: asyncio.Task[None] | None = None
        self._result_queue: asyncio.Queue[FeedFetchResult] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._stopping = False
        self._idle_sleep_seconds = 5
        self._result_queue_size = 500
        self._next_maintenance_at = datetime.now(UTC) + timedelta(hours=2)

    def configure(self, config: AppConfig) -> None:
        self.config = config
        self.feeds = build_feed_runtime_map(config)
        self.email_sources = build_email_source_runtime_map(config)
        active_feed_keys = frozenset(set(self.feeds) | set(self.email_sources))
        pruned_feed_status = self.db.prune_inactive_feed_status(active_feed_keys)
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
        for source_key in self.email_sources:
            self._next_due.setdefault(source_key, now)
        for removed in set(self._next_due) - set(active_feed_keys):
            del self._next_due[removed]
        if pruned_feed_status:
            logger.info("Pruned %s inactive feed status rows", pruned_feed_status)
        logger.info("Configured %s unique feeds", len(self.feeds))
        if self.email_sources:
            logger.info("Configured %s email sources", len(self.email_sources))
        audit_logger.info(
            "config_applied unique_feeds=%s channels=%s inactive_feed_status_pruned=%s audit_enabled=%s detailed_errors=%s",
            len(self.feeds),
            len(config.channels),
            pruned_feed_status,
            config.settings.logging.audit_enabled,
            config.settings.logging.detailed_errors,
        )

    def start(self) -> None:
        self._stopping = False
        if self._result_queue is None:
            self._result_queue = asyncio.Queue(maxsize=self._result_queue_size)
        if self._processor_task is None or self._processor_task.done():
            self._processor_task = asyncio.create_task(self._process_results_loop())
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_rss_loop())
        if self._email_task is None or self._email_task.done():
            self._email_task = asyncio.create_task(self._run_email_loop())

    async def shutdown(self) -> None:
        logger.info("Scheduler shutdown requested")
        self._stopping = True
        poll_tasks = [task for task in (self._task, self._email_task) if task is not None]
        for task in poll_tasks:
            task.cancel()
        if poll_tasks:
            await asyncio.gather(*poll_tasks, return_exceptions=True)
        self._task = None
        self._email_task = None
        if self._result_queue is not None:
            try:
                await asyncio.wait_for(
                    self._result_queue.join(),
                    timeout=(self.config.settings.publishing.shutdown_drain_seconds if self.config else 20),
                )
            except asyncio.TimeoutError:
                logger.warning("Scheduler result processing timed out with queued results still pending")
        if self._processor_task:
            self._processor_task.cancel()
            await asyncio.gather(self._processor_task, return_exceptions=True)
            self._processor_task = None
        if self._maintenance_task and not self._maintenance_task.done():
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
        self._maintenance_task = None

    async def _run_rss_loop(self) -> None:
        async with self._new_client_session() as session:
            self._session = session
            try:
                while not self._stopping:
                    if not self.config:
                        await asyncio.sleep(1)
                        continue
                    now = datetime.now(UTC)
                    due_feeds = self._due_feeds(now)
                    if due_feeds:
                        batch = await self._fetch_feeds(due_feeds)
                        await self._enqueue_results(batch.results)
                        self._maybe_prune_runtime_history()
                    await asyncio.sleep(self._seconds_until_next_poll(self.feeds))
            except asyncio.CancelledError:
                logger.info("RSS scheduler loop cancelled")
                raise
            finally:
                self._session = None

    async def _run_email_loop(self) -> None:
        try:
            while not self._stopping:
                if not self.config:
                    await asyncio.sleep(1)
                    continue
                now = datetime.now(UTC)
                due_email_sources = self._due_email_sources(now)
                if due_email_sources:
                    batch = await self._fetch_email_sources(due_email_sources)
                    await self._enqueue_results(batch.results)
                    self._maybe_prune_runtime_history()
                await asyncio.sleep(self._seconds_until_next_poll(self.email_sources))
        except asyncio.CancelledError:
            logger.info("Email scheduler loop cancelled")
            raise

    def _seconds_until_next_poll(self, sources: dict[str, object]) -> float:
        if not sources:
            return float(self._idle_sleep_seconds)
        now = datetime.now(UTC)
        next_due = min(self._next_due.get(source_key, now) for source_key in sources)
        return max(1.0, min(float(self._idle_sleep_seconds), (next_due - now).total_seconds()))

    def _due_feeds(self, now: datetime) -> list[FeedRuntime]:
        return [
            feed for feed_key, feed in self.feeds.items() if self._next_due.get(feed_key, now) <= now
        ]

    def _due_email_sources(self, now: datetime) -> list[EmailSourceRuntime]:
        return [
            source
            for source_key, source in self.email_sources.items()
            if self._next_due.get(source_key, now) <= now
        ]

    def next_poll_at(self) -> datetime | None:
        if not self._next_due:
            return None
        return min(self._next_due.values())

    async def _enqueue_results(self, results: tuple[FeedFetchResult, ...]) -> None:
        if not results:
            return
        if self._result_queue is None:
            self._result_queue = asyncio.Queue(maxsize=self._result_queue_size)
        for result in results:
            await self._result_queue.put(result)

    async def _process_results_loop(self) -> None:
        try:
            while not self._stopping or (self._result_queue is not None and not self._result_queue.empty()):
                if self._result_queue is None:
                    await asyncio.sleep(1)
                    continue
                try:
                    result = await asyncio.wait_for(self._result_queue.get(), timeout=1)
                except asyncio.TimeoutError:
                    continue
                try:
                    await self._process_queued_result(result)
                finally:
                    self._result_queue.task_done()
        except asyncio.CancelledError:
            logger.info("Scheduler result processor cancelled")
            raise

    async def _process_queued_result(self, result: FeedFetchResult) -> RefreshSummary:
        try:
            return await self._process_feed_result(result)
        except Exception:
            logger.exception("Queued source processing failed for %s", result.feed.display_name)
            audit_logger.exception(
                "queued_source_processing_exception source_key=%s source_name=%r",
                result.feed.feed_key,
                result.feed.display_name,
            )
            return RefreshSummary(errors=1)

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
        if self._maintenance_task and not self._maintenance_task.done():
            return
        maintenance = self.config.settings.maintenance
        self._next_maintenance_at = now + timedelta(hours=maintenance.interval_hours)
        self._maintenance_task = asyncio.create_task(self._run_runtime_maintenance(maintenance))

    async def _run_runtime_maintenance(self, maintenance: MaintenanceSettings) -> None:
        started = time.perf_counter()
        logger.info("Runtime DB maintenance started")
        audit_logger.info("runtime_db_maintenance_started")
        try:
            stats = await asyncio.to_thread(self._run_runtime_maintenance_sync, maintenance)
        except Exception:
            logger.exception("Runtime DB maintenance failed")
            audit_logger.exception("runtime_db_maintenance_failed")
            return
        duration_seconds = time.perf_counter() - started
        if any(stats.values()):
            logger.info("Runtime DB maintenance pruned rows in %.2fs: %s", duration_seconds, stats)
            audit_logger.info("runtime_db_maintenance duration_seconds=%.2f stats=%r", duration_seconds, stats)
        else:
            logger.info("Runtime DB maintenance completed in %.2fs with no rows pruned", duration_seconds)
            audit_logger.info("runtime_db_maintenance duration_seconds=%.2f stats=%r", duration_seconds, stats)

    def _run_runtime_maintenance_sync(self, maintenance: MaintenanceSettings) -> dict[str, int]:
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
        return stats

    async def poll_feeds(self, feeds: list[FeedRuntime]) -> RefreshSummary:
        batch = await self._fetch_feeds(feeds)
        summary = RefreshSummary(errors=batch.errors)
        for result in batch.results:
            summary = _combine(summary, await self._process_queued_result(result))
        return summary

    async def _fetch_feeds(self, feeds: list[FeedRuntime]) -> FetchBatchResult:
        if not self.config or not feeds:
            return FetchBatchResult()
        feed_service = FeedService(
            timeout_seconds=self.config.settings.polling.fetch_timeout_seconds,
            max_entries_per_feed=self.config.settings.polling.max_entries_per_feed,
        )
        semaphore = asyncio.Semaphore(self.config.settings.polling.max_concurrent_feed_fetches)

        session = self._session
        if session is not None and not session.closed:
            return await self._fetch_feeds_with_session(session, feeds, feed_service, semaphore)
        async with self._new_client_session() as temp_session:
            return await self._fetch_feeds_with_session(temp_session, feeds, feed_service, semaphore)

    async def poll_email_sources(self, sources: list[EmailSourceRuntime]) -> RefreshSummary:
        batch = await self._fetch_email_sources(sources)
        summary = RefreshSummary(errors=batch.errors)
        for result in batch.results:
            summary = _combine(summary, await self._process_queued_result(result))
        return summary

    async def _fetch_email_sources(self, sources: list[EmailSourceRuntime]) -> FetchBatchResult:
        if not self.config or not sources:
            return FetchBatchResult()
        email_service = EmailIngestService(
            timeout_seconds=self.config.settings.polling.fetch_timeout_seconds,
            max_messages_per_source=self.config.settings.polling.max_entries_per_feed,
        )
        semaphore = asyncio.Semaphore(self.config.settings.polling.max_concurrent_email_fetches)
        results: list[FeedFetchResult] = []
        errors = 0
        tasks = [
            asyncio.create_task(self._fetch_email_with_status(email_service, semaphore, source))
            for source in sources
        ]
        try:
            for completed in asyncio.as_completed(tasks):
                try:
                    result = await completed
                except FeedFetchError:
                    errors += 1
                    continue
                except Exception:
                    logger.exception("Unexpected email source fetch task failure")
                    audit_logger.exception("email_source_task_exception")
                    errors += 1
                    continue
                results.append(result)
        finally:
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return FetchBatchResult(results=tuple(results), errors=errors)

    def _new_client_session(self) -> aiohttp.ClientSession:
        connector = aiohttp.TCPConnector(limit=64, limit_per_host=4, ttl_dns_cache=300)
        return aiohttp.ClientSession(max_field_size=32768, connector=connector)

    async def _fetch_feeds_with_session(
        self,
        session: aiohttp.ClientSession,
        feeds: list[FeedRuntime],
        feed_service: FeedService,
        semaphore: asyncio.Semaphore,
    ) -> FetchBatchResult:
        results: list[FeedFetchResult] = []
        errors = 0
        tasks = [
            asyncio.create_task(self._fetch_with_status(session, feed_service, semaphore, feed))
            for feed in feeds
        ]
        try:
            for completed in asyncio.as_completed(tasks):
                try:
                    result = await completed
                except FeedFetchError:
                    errors += 1
                    continue
                except Exception:
                    logger.exception("Unexpected feed fetch task failure")
                    audit_logger.exception("feed_task_exception")
                    errors += 1
                    continue
                results.append(result)
        finally:
            pending = [task for task in tasks if not task.done()]
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return FetchBatchResult(results=tuple(results), errors=errors)

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
                next_due = datetime.now(UTC) + timedelta(
                    seconds=self._failure_retry_seconds(feed, exc, first_success=first_success)
                )
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

    async def _fetch_email_with_status(
        self,
        email_service: EmailIngestService,
        semaphore: asyncio.Semaphore,
        source: EmailSourceRuntime,
    ) -> FeedFetchResult:
        first_success = self.db.is_first_feed_success(source.feed_key)
        since_uid = self.db.email_cursor_uid(source.feed_key, source.mailbox)
        async with semaphore:
            try:
                result = await email_service.fetch(source, since_uid=since_uid)
                result = FeedFetchResult(
                    feed=result.feed,
                    entries=result.entries,
                    first_success=first_success,
                    cursor_high_water=result.cursor_high_water,
                )
                next_due = datetime.now(UTC) + timedelta(seconds=source.interval_seconds)
                self._next_due[source.feed_key] = next_due
                self.db.mark_feed_success(source.feed_key, source.display_name, source.url, next_due)
                self.db.update_email_cursor(source.feed_key, source.mailbox, result.cursor_high_water)
                logger.info("Fetched %s email entries from %s", len(result.entries), source.display_name)
                audit_logger.debug(
                    "email_source_success source_key=%s source_name=%r entries=%s first_success=%s next_poll_at=%s cursor_high_water=%s",
                    source.feed_key,
                    source.display_name,
                    len(result.entries),
                    first_success,
                    next_due.isoformat(),
                    result.cursor_high_water,
                )
                return result
            except FeedFetchError as exc:
                next_due = datetime.now(UTC) + timedelta(
                    seconds=self._failure_retry_seconds(source, exc, first_success=first_success)
                )
                self._next_due[source.feed_key] = next_due
                self.db.mark_feed_failure(source.feed_key, source.display_name, source.url, str(exc), next_due)
                if self.config and self.config.settings.logging.detailed_errors:
                    logger.exception("Email source failed: %s: %r", source.display_name, exc)
                else:
                    logger.warning("Email source failed: %s: %s", source.display_name, exc)
                audit_logger.error(
                    "email_source_failure source_key=%s source_name=%r error=%r next_poll_at=%s",
                    source.feed_key,
                    source.display_name,
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
            initial_backfill_hours = getattr(result.feed, "initial_backfill_hours", None)
            if first_success_limited_backfill and not self._is_recent_valid_for_first_success(
                candidate.normalized_published_at,
                candidate.timestamp_status,
                max_age_hours=initial_backfill_hours,
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
            for channel_id in self._target_channel_ids(result.feed.channel_ids, routing_decision, result.feed, candidate):
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

    def _is_recent_valid_for_first_success(
        self,
        published_at: datetime,
        timestamp_status: str,
        *,
        max_age_hours: int | None = None,
    ) -> bool:
        if timestamp_status not in {"valid", "timezone_corrected"}:
            return False
        cutoff_hours = max_age_hours if max_age_hours is not None else (
            self.config.settings.timestamps.max_post_age_hours if self.config else 48
        )
        cutoff = datetime.now(UTC) - timedelta(hours=cutoff_hours)
        return published_at >= cutoff

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

    def _failure_retry_seconds(self, feed: FeedRuntime, exc: FeedFetchError, *, first_success: bool = False) -> int:
        host = urlparse(feed.url).netloc.casefold()
        if host == "www.dvidshub.net" and "waf action=challenge" in str(exc).casefold():
            base_retry_seconds = max(feed.interval_seconds, 900)
        elif host != "bsky.app":
            base_retry_seconds = max(feed.interval_seconds, 3600)
        else:
            match = re.search(r"retry after (\d+)s", str(exc), re.IGNORECASE)
            if match:
                base_retry_seconds = max(feed.interval_seconds, int(match.group(1)))
            elif "rate limited" in str(exc).casefold():
                base_retry_seconds = max(feed.interval_seconds, 900)
            else:
                base_retry_seconds = feed.interval_seconds

        if not self.config or not self.config.settings.failure_backoff.enabled:
            return base_retry_seconds

        backoff = self.config.settings.failure_backoff
        prior_failures = self.db.feed_consecutive_failures(feed.feed_key)
        failures_after_attempt = prior_failures + 1
        if first_success and failures_after_attempt >= backoff.suspend_failure_threshold:
            return max(base_retry_seconds, backoff.suspended_retry_seconds)
        if failures_after_attempt >= backoff.major_failure_threshold:
            return max(base_retry_seconds, backoff.major_retry_seconds)
        if failures_after_attempt >= backoff.minor_failure_threshold:
            return max(base_retry_seconds, backoff.minor_retry_seconds)
        return base_retry_seconds

    def _route_candidate(self, article_id: int, candidate, *, persist: bool = True) -> RoutingDecision | None:
        if self.routing_engine is None:
            return None
        try:
            routing_summary = candidate.summary
            metadata_summary = candidate.rich_metadata.get("routing_summary") if candidate.rich_metadata else None
            if isinstance(metadata_summary, str) and metadata_summary.strip():
                routing_summary = metadata_summary.strip()
            decision = self.routing_engine.route(
                RoutingArticle(
                    article_id=article_id,
                    title=candidate.title,
                    summary=routing_summary,
                    source_name=candidate.source_name,
                    source_id=candidate.source_id,
                    source_class=candidate.source_class,
                    url=candidate.url,
                    normalized_title=candidate.normalized_title,
                    routing_tags=candidate.routing_tags,
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
        source: FeedRuntime | EmailSourceRuntime,
        candidate=None,
    ) -> tuple[str, ...]:
        fast_lane_ids = tuple(getattr(source, "fast_lane_channel_ids", ()) or ())
        if getattr(source, "route_policy", "normal") == "direct":
            return _dedupe_channel_ids(fast_lane_ids + existing_channel_ids)
        if self.routing_mode != "enforced" or decision is None:
            return _dedupe_channel_ids(fast_lane_ids + existing_channel_ids)
        if decision.decision_status not in {"routed", "review"}:
            if (
                decision.decision_status == "no_match"
                and getattr(source, "no_match_policy", "drop") == "review"
                and "review" in self.channel_key_to_id
                and _should_review_no_match(source, candidate)
            ):
                return _dedupe_channel_ids(fast_lane_ids + (self.channel_key_to_id["review"],))
            return _dedupe_channel_ids(fast_lane_ids)
        selected = tuple(
            self.channel_key_to_id[key]
            for key in decision.final_channel_keys
            if key in self.channel_key_to_id
        )
        return _dedupe_channel_ids(fast_lane_ids + selected)


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
                fetch_timeout_seconds=feed.fetch_timeout_seconds,
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
                    fetch_timeout_seconds=feed.fetch_timeout_seconds,
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
                "fetch_timeout_seconds": feed.fetch_timeout_seconds,
                "source_id": feed.source_id,
                "source_class": feed.source_class,
                "route_policy": feed.route_policy,
                "channel_ids": [],
                "channel_keys": [],
            },
        )
        group["interval_seconds"] = min(int(group["interval_seconds"]), feed.interval_seconds)
        existing_timeout = group["fetch_timeout_seconds"]
        if feed.fetch_timeout_seconds is not None:
            group["fetch_timeout_seconds"] = max(int(existing_timeout or 0), feed.fetch_timeout_seconds)
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
            fetch_timeout_seconds=(
                int(group["fetch_timeout_seconds"]) if group["fetch_timeout_seconds"] is not None else None
            ),
            source_id=str(group["source_id"]),
            source_class=str(group["source_class"]),
            route_policy=str(group["route_policy"]),
            channel_ids=tuple(group["channel_ids"]),
            channel_keys=tuple(group["channel_keys"]),
        )
        for group in grouped.values()
    }


def build_email_source_runtime_map(config: AppConfig) -> dict[str, EmailSourceRuntime]:
    sources: dict[str, EmailSourceRuntime] = {}
    for source in config.email_sources:
        if source.route_policy == "ignore":
            continue
        url = f"imap://{source.imap_host_env}/{source.mailbox}/{source.id}"
        sources[source.id] = EmailSourceRuntime(
            feed_key=source.id,
            display_name=source.name,
            imap_host_env=source.imap_host_env,
            imap_port_env=source.imap_port_env,
            username_env=source.username_env,
            password_env=source.password_env,
            mailbox=source.mailbox,
            from_contains=source.from_contains,
            list_id_contains=source.list_id_contains,
            subject_contains=source.subject_contains,
            match_all=source.match_all,
            url=url,
            normalized_url=normalize_feed_url(url),
            interval_seconds=source.poll_interval_seconds or config.settings.polling.default_interval_seconds,
            channel_ids=source.target_channel_ids,
            channel_keys=(),
            fetch_timeout_seconds=source.fetch_timeout_seconds,
            source_id=source.source_id,
            source_class=source.source_class,
            route_policy=source.route_policy,
            initial_backfill_hours=source.initial_backfill_hours,
            no_match_policy=source.no_match_policy,
            target_channel_ids=source.target_channel_ids,
            fast_lane_channel_ids=source.fast_lane_channel_ids,
            routing_tags=source.routing_tags,
            max_messages_per_poll=source.max_messages_per_poll,
            priority=source.priority,
        )
    return sources


def _dedupe_channel_ids(channel_ids: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    unique: list[str] = []
    for channel_id in channel_ids:
        if channel_id in seen:
            continue
        seen.add(channel_id)
        unique.append(channel_id)
    return tuple(unique)


def _should_review_no_match(source: FeedRuntime | EmailSourceRuntime, candidate) -> bool:
    if not isinstance(source, EmailSourceRuntime):
        return True
    if candidate is None:
        return False
    metadata = getattr(candidate, "rich_metadata", {}) or {}
    if metadata.get("email_low_signal") is True:
        return False
    url = getattr(candidate, "url", None)
    if not url:
        return False
    text = "\n".join(
        str(value or "")
        for value in (
            getattr(candidate, "title", ""),
            getattr(candidate, "summary", ""),
            metadata.get("routing_summary", ""),
        )
    ).casefold()
    if len(text.strip()) < 80:
        return False
    high_signal_terms = (
        "defense",
        "military",
        "army",
        "navy",
        "air force",
        "marine corps",
        "space force",
        "pentagon",
        "nato",
        "ukraine",
        "russia",
        "china",
        "taiwan",
        "iran",
        "cyber",
        "zero-day",
        "zeroday",
        "vulnerability",
        "malware",
        "ransomware",
        "intelligence",
        "sanctions",
        "missile",
        "drone",
        "ship",
    )
    return any(term in text for term in high_signal_terms)


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
