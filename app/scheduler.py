from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

    def configure(self, config: AppConfig) -> None:
        self.config = config
        self.feeds = build_feed_runtime_map(config)
        self.channel_key_to_id = {channel.key: channel.discord_channel_id for channel in config.channels}
        self.routing_engine = None
        self.routing_mode = config.settings.routing.mode
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
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def shutdown(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while not self._stopping:
            if not self.config:
                await asyncio.sleep(1)
                continue
            now = datetime.now(UTC)
            due_feeds = [feed for feed_key, feed in self.feeds.items() if self._next_due.get(feed_key, now) <= now]
            if due_feeds:
                await self.poll_feeds(due_feeds)
            await asyncio.sleep(1)

    async def refresh_channel(self, channel_id: str) -> RefreshSummary:
        feed_keys = self.channel_to_feed_keys.get(channel_id)
        if not feed_keys:
            raise ValueError("This Discord channel is not configured for RSS refresh.")
        feeds = [self.feeds[key] for key in feed_keys if key in self.feeds]
        return await self.poll_feeds(feeds)

    async def poll_feeds(self, feeds: list[FeedRuntime]) -> RefreshSummary:
        if not self.config or not feeds:
            return RefreshSummary()
        feed_service = FeedService(
            timeout_seconds=self.config.settings.polling.fetch_timeout_seconds,
            max_entries_per_feed=self.config.settings.polling.max_entries_per_feed,
        )
        semaphore = asyncio.Semaphore(self.config.settings.polling.max_concurrent_feed_fetches)
        summary = RefreshSummary()

        async with aiohttp.ClientSession() as session:
            tasks = [self._fetch_with_status(session, feed_service, semaphore, feed) for feed in feeds]
            for completed in asyncio.as_completed(tasks):
                try:
                    result = await completed
                except FeedFetchError:
                    summary = _merge(summary, errors=1)
                    continue
                feed_summary = await self._process_feed_result(result)
                summary = _combine(summary, feed_summary)
        return summary

    async def _fetch_with_status(
        self,
        session: aiohttp.ClientSession,
        feed_service: FeedService,
        semaphore: asyncio.Semaphore,
        feed: FeedRuntime,
    ) -> FeedFetchResult:
        first_success = self.db.is_first_feed_success(feed.feed_key)
        self.db.mark_feed_attempt(feed.feed_key, feed.display_name, feed.url)
        async with semaphore:
            try:
                result = await feed_service.fetch(session, feed)
                result = FeedFetchResult(feed=result.feed, entries=result.entries, first_success=first_success)
                next_due = datetime.now(UTC) + timedelta(seconds=feed.interval_seconds)
                self._next_due[feed.feed_key] = next_due
                self.db.mark_feed_success(feed.feed_key, next_due)
                logger.info("Fetched %s entries from %s", len(result.entries), feed.display_name)
                audit_logger.info(
                    "feed_success feed_key=%s feed_name=%r entries=%s first_success=%s next_poll_at=%s",
                    feed.feed_key,
                    feed.display_name,
                    len(result.entries),
                    first_success,
                    next_due.isoformat(),
                )
                return result
            except FeedFetchError as exc:
                next_due = datetime.now(UTC) + timedelta(seconds=feed.interval_seconds)
                self._next_due[feed.feed_key] = next_due
                self.db.mark_feed_failure(feed.feed_key, str(exc), next_due)
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
        suppress_posts = result.first_success and not self.config.settings.polling.post_old_articles_on_first_run
        new_articles = 0
        duplicates = 0
        posts_queued = 0

        for entry in result.entries:
            candidate = build_candidate(entry, self.config.settings.timestamps)
            dedupe = self.db.resolve_article(candidate, self.config.settings.dedupe.title_match_window_hours)
            if dedupe.is_new_article:
                new_articles += 1
            else:
                duplicates += 1
            if suppress_posts:
                routing_decision = self._route_candidate(dedupe.article_id, candidate)
                for channel_id in self._target_channel_ids(result.feed.channel_ids, routing_decision):
                    self.db.record_channel_suppressed(
                        dedupe.article_id,
                        channel_id,
                        candidate.normalized_title,
                        candidate.title_signature,
                    )
                    audit_logger.info(
                        "post_suppressed_first_run feed_key=%s article_id=%s channel_id=%s title=%r",
                        result.feed.feed_key,
                        dedupe.article_id,
                        channel_id,
                        candidate.title,
                )
                continue
            routing_decision = self._route_candidate(dedupe.article_id, candidate)
            for channel_id in self._target_channel_ids(result.feed.channel_ids, routing_decision):
                if self._is_stale_for_posting(candidate.normalized_published_at, candidate.timestamp_status):
                    duplicates += 1
                    self.db.record_channel_skipped(
                        dedupe.article_id,
                        channel_id,
                        candidate.normalized_title,
                        candidate.title_signature,
                        "skipped_stale",
                    )
                    audit_logger.info(
                        "post_skipped_stale feed_key=%s article_id=%s channel_id=%s published_at=%s title=%r",
                        result.feed.feed_key,
                        dedupe.article_id,
                        channel_id,
                        candidate.normalized_published_at.isoformat(),
                        candidate.title,
                    )
                    continue
                if self.db.has_channel_post(dedupe.article_id, channel_id):
                    duplicates += 1
                    continue
                if self.db.has_channel_title(channel_id, candidate.normalized_title):
                    duplicates += 1
                    audit_logger.info(
                        "post_skipped_duplicate_title feed_key=%s article_id=%s channel_id=%s normalized_title=%r title=%r",
                        result.feed.feed_key,
                        dedupe.article_id,
                        channel_id,
                        candidate.normalized_title,
                        candidate.title,
                    )
                    continue
                if self.db.has_channel_title_signature(channel_id, candidate.title_signature):
                    duplicates += 1
                    audit_logger.info(
                        "post_skipped_duplicate_title_signature feed_key=%s article_id=%s channel_id=%s title_signature=%r title=%r",
                        result.feed.feed_key,
                        dedupe.article_id,
                        channel_id,
                        candidate.title_signature,
                        candidate.title,
                    )
                    continue
                if not self.db.reserve_channel_title(
                    dedupe.article_id,
                    channel_id,
                    candidate.normalized_title,
                    candidate.title_signature,
                    "queued",
                ):
                    duplicates += 1
                    continue
                job = self.db.get_post_job(dedupe.article_id, channel_id)
                if await self.publisher.enqueue(job):
                    posts_queued += 1
                    audit_logger.info(
                        "post_queued feed_key=%s article_id=%s channel_id=%s title=%r",
                        result.feed.feed_key,
                        dedupe.article_id,
                        channel_id,
                        candidate.title,
                    )
        audit_logger.info(
            "feed_processed feed_key=%s entries=%s new_articles=%s duplicates=%s posts_queued=%s suppressed=%s",
            result.feed.feed_key,
            len(result.entries),
            new_articles,
            duplicates,
            posts_queued,
            suppress_posts,
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

    def _route_candidate(self, article_id: int, candidate) -> RoutingDecision | None:
        if self.routing_engine is None:
            return None
        try:
            decision = self.routing_engine.route(
                RoutingArticle(
                    article_id=article_id,
                    title=candidate.title,
                    summary=candidate.summary,
                    source_name=candidate.source_name,
                    url=candidate.url,
                    normalized_title=candidate.normalized_title,
                )
            )
            selected_ids = [self.channel_key_to_id[key] for key in decision.selected_channel_keys if key in self.channel_key_to_id]
            self.db.record_routing_decision(article_id, decision, selected_ids)
            audit_logger.info(
                "routing_decision article_id=%s status=%s selected_keys=%s top_score=%s title=%r",
                article_id,
                decision.decision_status,
                list(decision.selected_channel_keys),
                decision.top_score,
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
        if decision.decision_status != "routed":
            return tuple()
        selected = tuple(
            self.channel_key_to_id[key]
            for key in decision.selected_channel_keys
            if key in self.channel_key_to_id
        )
        return selected


def build_feed_runtime_map(config: AppConfig) -> dict[str, FeedRuntime]:
    grouped: dict[str, dict[str, object]] = {}
    for channel in config.channels:
        interval = channel.poll_interval_seconds or config.settings.polling.default_interval_seconds
        for feed in channel.feeds:
            normalized_url = normalize_feed_url(feed.url)
            group = grouped.setdefault(
                normalized_url,
                {
                    "feed_key": feed.id or f"feed_{stable_hash(normalized_url)}",
                    "display_name": feed.name,
                    "url": feed.url,
                    "normalized_url": normalized_url,
                    "interval_seconds": interval,
                    "channel_ids": [],
                    "channel_keys": [],
                },
            )
            group["interval_seconds"] = min(int(group["interval_seconds"]), interval)
            cast_channel_ids = group["channel_ids"]
            cast_channel_keys = group["channel_keys"]
            assert isinstance(cast_channel_ids, list)
            assert isinstance(cast_channel_keys, list)
            if channel.discord_channel_id not in cast_channel_ids:
                cast_channel_ids.append(channel.discord_channel_id)
                cast_channel_keys.append(channel.key)
    return {
        str(group["feed_key"]): FeedRuntime(
            feed_key=str(group["feed_key"]),
            display_name=str(group["display_name"]),
            url=str(group["url"]),
            normalized_url=str(group["normalized_url"]),
            interval_seconds=int(group["interval_seconds"]),
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
