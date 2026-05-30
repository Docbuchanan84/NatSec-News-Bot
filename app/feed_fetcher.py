from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp
import feedparser

from app.models import FeedEntry, FeedRuntime

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeedFetchResult:
    feed: FeedRuntime
    entries: tuple[FeedEntry, ...]
    first_success: bool = False


class FeedFetchError(Exception):
    pass


class FeedService:
    def __init__(self, timeout_seconds: int, max_entries_per_feed: int) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_entries_per_feed = max_entries_per_feed

    async def fetch(self, session: aiohttp.ClientSession, feed: FeedRuntime) -> FeedFetchResult:
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 RSSDispatchBot/0.1 (+https://github.com/rss-dispatch-bot)",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            }
            async with session.get(feed.url, timeout=timeout, headers=headers) as response:
                response.raise_for_status()
                body = await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            detail = str(exc) or exc.__class__.__name__
            raise FeedFetchError(detail) from exc

        parsed = feedparser.parse(body)
        if parsed.bozo and parsed.bozo_exception:
            logger.warning("Feed parse warning for %s: %s", feed.display_name, parsed.bozo_exception)
        if parsed.bozo and not parsed.entries:
            raise FeedFetchError(f"feed parse failed: {parsed.bozo_exception}")
        raw_entries = parsed.entries[: self.max_entries_per_feed]
        entries = tuple(self._entry_from_parsed(feed, raw) for raw in raw_entries)
        return FeedFetchResult(feed=feed, entries=entries)

    def _entry_from_parsed(self, feed: FeedRuntime, raw: Any) -> FeedEntry:
        raw_dict = dict(raw)
        title = raw_dict.get("title") or raw_dict.get("summary") or "Untitled article"
        url = raw_dict.get("link") or raw_dict.get("id")
        guid = raw_dict.get("id") or raw_dict.get("guid")
        summary = raw_dict.get("summary") or raw_dict.get("description")
        published = raw_dict.get("published") or raw_dict.get("updated") or raw_dict.get("created")
        return FeedEntry(
            feed_key=feed.feed_key,
            feed_name=feed.display_name,
            raw_guid=str(guid) if guid else None,
            raw_title=str(title),
            raw_url=str(url) if url else None,
            summary=str(summary) if summary else None,
            raw_published_at=str(published) if published else None,
            parsed=raw_dict,
        )
