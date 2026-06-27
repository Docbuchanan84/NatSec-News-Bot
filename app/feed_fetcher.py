from __future__ import annotations

import asyncio
import html
import io
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiohttp
import feedparser
from pypdf import PdfReader

from app.models import FeedEntry, FeedRuntime

logger = logging.getLogger(__name__)
BLUESKY_PROFILE_HOST = "bsky.app"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)
URL_RE = re.compile(r"(?:https?://|www\.)\S+|(?<!@)\b[a-z0-9][a-z0-9.-]+\.[a-z]{2,}/\S+", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif")
BLUESKY_POST_THREAD_URL = "https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread"
DVIDS_HOST = "www.dvidshub.net"
DVIDS_API_HOST = "api.dvidshub.net"
DVIDS_SEARCH_URL = "https://api.dvidshub.net/search"
DVIDS_MAX_ENTRIES_PER_FEED = 50
ICAL_LOOKAHEAD_DAYS = 14
ICAL_RECENT_STARTED_HOURS = 24
STATE_HOST = "www.state.gov"
MSCIO_HOST = "mscio.eu"
MSCIO_DOCUMENT_PDF_LIMIT = 6
MSCIO_DOCUMENT_MAX_BYTES = 6 * 1024 * 1024
MSCIO_DOCUMENT_MAX_PAGES = 2
STATE_MONTH_ABBR = {
    "january": "Jan",
    "february": "Feb",
    "march": "Mar",
    "april": "Apr",
    "may": "May",
    "june": "Jun",
    "july": "Jul",
    "august": "Aug",
    "september": "Sep",
    "october": "Oct",
    "november": "Nov",
    "december": "Dec",
}


@dataclass(frozen=True)
class FeedFetchResult:
    feed: FeedRuntime
    entries: tuple[FeedEntry, ...]
    first_success: bool = False
    cursor_high_water: str | None = None


class FeedFetchError(Exception):
    pass


class FeedService:
    def __init__(
        self,
        timeout_seconds: int,
        max_entries_per_feed: int,
        max_routing_summary_chars: int = 2000,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_entries_per_feed = max_entries_per_feed
        self.max_routing_summary_chars = max_routing_summary_chars

    async def fetch(self, session: aiohttp.ClientSession, feed: FeedRuntime) -> FeedFetchResult:
        timeout_seconds = feed.fetch_timeout_seconds or self.timeout_seconds
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        try:
            headers = {
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            }
            async with session.get(feed.url, timeout=timeout, headers=headers) as response:
                if response.status == 429:
                    retry_after = response.headers.get("Retry-After")
                    detail = "rate limited"
                    if retry_after:
                        detail = f"{detail}; retry after {retry_after}s"
                    raise FeedFetchError(detail)
                if _is_dvids_rss(feed.url) and response.status == 202:
                    action = response.headers.get("x-amzn-waf-action")
                    if fallback := await self._fetch_dvids_api_fallback(session, feed, action, timeout_seconds):
                        return fallback
                    detail = "DVIDS returned empty HTTP 202 response"
                    if action:
                        detail = f"{detail}; waf action={action}"
                    raise FeedFetchError(detail)
                response.raise_for_status()
                body = await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            detail = str(exc) or exc.__class__.__name__
            raise FeedFetchError(detail) from exc

        state_collection_entries = _state_collection_entries(feed, body, _entry_limit(feed, self.max_entries_per_feed))
        if state_collection_entries is not None:
            return FeedFetchResult(feed=feed, entries=state_collection_entries)
        mscio_document_entries = await _mscio_document_folder_entries(
            session,
            feed,
            body,
            _entry_limit(feed, self.max_entries_per_feed),
            timeout_seconds,
        )
        if mscio_document_entries is not None:
            return FeedFetchResult(feed=feed, entries=mscio_document_entries)
        calendar_entries = _ical_entries(feed, body, _entry_limit(feed, self.max_entries_per_feed))
        if calendar_entries is not None:
            return FeedFetchResult(feed=feed, entries=calendar_entries)

        parsed = feedparser.parse(body)
        if parsed.bozo and parsed.bozo_exception:
            logger.warning("Feed parse warning for %s: %s", feed.display_name, parsed.bozo_exception)
        if parsed.bozo and not parsed.entries:
            raise FeedFetchError(f"feed parse failed: {parsed.bozo_exception}")
        raw_entries = parsed.entries[: _entry_limit(feed, self.max_entries_per_feed)]
        entries = tuple(self._entry_from_parsed(feed, raw) for raw in raw_entries)
        if _is_bluesky_rss(feed.url):
            entries = await self._enrich_bluesky_entries(session, entries, timeout_seconds)
        return FeedFetchResult(feed=feed, entries=entries)

    def _entry_from_parsed(self, feed: FeedRuntime, raw: Any) -> FeedEntry:
        raw_dict = dict(raw)
        url = raw_dict.get("link") or raw_dict.get("id")
        social_url = str(url) if _is_bluesky_rss(feed.url) and url else None
        guid = raw_dict.get("id") or raw_dict.get("guid")
        raw_summary = raw_dict.get("summary") or raw_dict.get("description")
        summary = clean_html_text(raw_summary)
        routing_summary = _routing_summary_from_parsed(
            raw_dict,
            display_summary=summary,
            title=str(raw_dict.get("title") or ""),
            limit=self.max_routing_summary_chars,
        )
        title = raw_dict.get("title") or summary or "Untitled article"
        if _is_bluesky_rss(feed.url):
            title = _bluesky_title(summary)
            url = _first_external_url(summary) or url
        image = extract_entry_image(raw_dict, base_url=str(url or feed.url))
        published = raw_dict.get("published") or raw_dict.get("updated") or raw_dict.get("created")
        metadata = _bluesky_metadata(social_url)
        if routing_summary:
            metadata["routing_summary"] = routing_summary
        return FeedEntry(
            feed_key=feed.feed_key,
            feed_name=feed.display_name,
            raw_guid=str(guid) if guid else None,
            raw_title=clean_html_text(title) or "Untitled article",
            raw_url=str(url) if url else None,
            summary=summary,
            image_url=image[0],
            image_source=image[1],
            raw_published_at=str(published) if published else None,
            parsed=raw_dict,
            source_id=feed.source_id,
            source_class=feed.source_class,
            rich_metadata=metadata,
            routing_tags=feed.routing_tags,
        )

    async def _enrich_bluesky_entries(
        self,
        session: aiohttp.ClientSession,
        entries: tuple[FeedEntry, ...],
        timeout_seconds: int | None = None,
    ) -> tuple[FeedEntry, ...]:
        enriched: list[FeedEntry] = []
        for entry in entries:
            if not _is_bluesky_post_uri(entry.raw_guid):
                enriched.append(entry)
                continue
            enriched.append(await self._enrich_bluesky_entry(session, entry, timeout_seconds))
        return tuple(enriched)

    async def _enrich_bluesky_entry(
        self,
        session: aiohttp.ClientSession,
        entry: FeedEntry,
        timeout_seconds: int | None = None,
    ) -> FeedEntry:
        if not entry.raw_guid:
            return entry
        url = f"{BLUESKY_POST_THREAD_URL}?uri={quote(entry.raw_guid, safe='')}"
        timeout = aiohttp.ClientTimeout(total=timeout_seconds or self.timeout_seconds)
        try:
            async with session.get(url, timeout=timeout, headers={"User-Agent": DEFAULT_USER_AGENT}) as response:
                if response.status == 429:
                    logger.warning("Bluesky media lookup rate limited for %s", entry.raw_guid)
                    return entry
                response.raise_for_status()
                payload = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            logger.warning("Bluesky media lookup failed for %s: %s", entry.raw_guid, exc)
            return entry

        media = extract_bluesky_media(payload)
        if media is None:
            return entry
        article_url, image_url, image_source = media
        metadata = dict(entry.rich_metadata or {})
        if _is_bluesky_post_url(entry.raw_url):
            metadata.setdefault("social_url", entry.raw_url)
            metadata.setdefault("bluesky_post_url", entry.raw_url)
        return replace(
            entry,
            raw_url=article_url or entry.raw_url,
            image_url=image_url or entry.image_url,
            image_source=image_source or entry.image_source,
            rich_metadata=metadata,
        )

    async def _fetch_dvids_api_fallback(
        self,
        session: aiohttp.ClientSession,
        feed: FeedRuntime,
        waf_action: str | None,
        timeout_seconds: int | None = None,
    ) -> FeedFetchResult | None:
        api_key = os.environ.get("DVIDS_API_KEY", "").strip()
        unit_id = _dvids_unit_id(feed.url)
        if not api_key or not unit_id:
            return None
        timeout = aiohttp.ClientTimeout(total=timeout_seconds or self.timeout_seconds)
        params = {
            "api_key": api_key,
            "unit_id": unit_id,
            "max_results": str(_entry_limit(feed, self.max_entries_per_feed)),
            "sort": "publishdate",
            "sortdir": "desc",
            "thumb_width": "800",
            "thumb_quality": "95",
        }
        try:
            async with session.get(
                DVIDS_SEARCH_URL,
                timeout=timeout,
                params=params,
                headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"},
            ) as response:
                if response.status == 429:
                    raise FeedFetchError("DVIDS API rate limited")
                response.raise_for_status()
                payload = await response.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
            detail = f"DVIDS RSS fallback failed after HTTP 202"
            if waf_action:
                detail = f"{detail}; waf action={waf_action}"
            raise FeedFetchError(f"{detail}; api error={exc}") from exc

        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise FeedFetchError("DVIDS API fallback returned invalid payload")
        entries = tuple(_dvids_api_entry(feed, raw) for raw in results if isinstance(raw, dict))
        logger.debug("Fetched %s DVIDS entries for %s via API fallback", len(entries), feed.display_name)
        return FeedFetchResult(feed=feed, entries=entries)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "figure", "figcaption"}:
            self._skip_depth += 1
        elif tag in {"p", "br", "div", "li"} and self.parts and self.parts[-1] != "\n":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "figure", "figcaption"} and self._skip_depth:
            self._skip_depth -= 1
        elif tag in {"p", "div", "li"} and self.parts and self.parts[-1] != "\n":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        value = "".join(self.parts)
        lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
        return "\n".join(line for line in lines if line and not _is_rss_boilerplate(line))


class _ImageExtractor(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.image_url: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.image_url or tag.casefold() != "img":
            return
        attr_map = {name.casefold(): value for name, value in attrs if value}
        for key in ("src", "data-src", "data-original", "data-lazy-src"):
            candidate = attr_map.get(key)
            if not candidate:
                continue
            cleaned = _clean_image_url(urljoin(self.base_url, candidate), trusted=True)
            if cleaned:
                self.image_url = cleaned
                return


class _StateCollectionParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.results: list[dict[str, str]] = []
        self._current: dict[str, object] | None = None
        self._in_link = False
        self._in_meta = False
        self._in_date = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        attr_map = {name.casefold(): value or "" for name, value in attrs}
        classes = attr_map.get("class", "")
        if tag == "li" and _class_contains(classes, "collection-result"):
            self._current = {"href": "", "title_parts": [], "date_parts": []}
            self._in_link = False
            self._in_meta = False
            self._in_date = False
            return
        if self._current is None:
            return
        if tag == "a" and _class_contains(classes, "collection-result__link"):
            self._current["href"] = urljoin(self.base_url, attr_map.get("href", ""))
            self._in_link = True
            return
        if tag == "div" and _class_contains(classes, "collection-result-meta"):
            self._in_meta = True
            return
        if self._in_meta and tag == "span":
            self._in_date = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._current is None:
            return
        if tag == "a":
            self._in_link = False
        elif tag == "span":
            self._in_date = False
        elif tag == "div" and self._in_meta:
            self._in_meta = False
            self._in_date = False
        elif tag == "li":
            title = clean_html_text(" ".join(self._current.get("title_parts", [])))
            date_text = clean_html_text(" ".join(self._current.get("date_parts", [])))
            href = str(self._current.get("href") or "").strip()
            if title and href:
                self.results.append({"title": title, "href": href, "date": date_text or ""})
            self._current = None
            self._in_link = False
            self._in_meta = False
            self._in_date = False

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        if self._in_link:
            self._current["title_parts"].append(data)  # type: ignore[union-attr]
        elif self._in_date:
            self._current["date_parts"].append(data)  # type: ignore[union-attr]


class _MscioDocumentFolderParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.results: list[dict[str, str]] = []
        self._in_row = False
        self._in_cell = False
        self._cell_index = -1
        self._cells: list[list[str]] = []
        self._href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag == "tr":
            self._in_row = True
            self._in_cell = False
            self._cell_index = -1
            self._cells = []
            self._href = ""
            return
        if not self._in_row:
            return
        if tag == "td":
            self._in_cell = True
            self._cell_index += 1
            self._cells.append([])
            return
        if tag == "a":
            attr_map = {name.casefold(): value or "" for name, value in attrs}
            href = attr_map.get("href", "")
            if href and ("/media/documents/" in href or href.casefold().endswith(".pdf")):
                self._href = urljoin(self.base_url, href)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "td":
            self._in_cell = False
            return
        if tag != "tr" or not self._in_row:
            return
        cells = [clean_html_text(" ".join(parts)) or "" for parts in self._cells]
        title = cells[0].strip() if cells else ""
        created = cells[1].strip() if len(cells) > 1 else ""
        size = cells[2].strip() if len(cells) > 2 else ""
        if title and self._href:
            self.results.append({"title": title, "href": self._href, "created": created, "size": size})
        self._in_row = False
        self._in_cell = False
        self._cell_index = -1
        self._cells = []
        self._href = ""

    def handle_data(self, data: str) -> None:
        if self._in_row and self._in_cell and self._cell_index >= 0:
            self._cells[self._cell_index].append(data)


def extract_entry_image(raw: dict[str, Any], base_url: str) -> tuple[str | None, str | None]:
    for value in _iter_media_urls(raw.get("media_thumbnail"), require_image_type=False):
        if cleaned := _clean_image_url(value, trusted=True):
            return cleaned, "media_thumbnail"

    for value in _iter_media_urls(raw.get("media_content"), require_image_type=True):
        if cleaned := _clean_image_url(value, trusted=True):
            return cleaned, "media_content"

    for value in _iter_media_urls(raw.get("enclosures"), require_image_type=True):
        if cleaned := _clean_image_url(value, trusted=True):
            return cleaned, "enclosure"

    for key in ("image", "itunes_image"):
        if cleaned := _image_from_field(raw.get(key)):
            return cleaned, key

    for value in _iter_image_links(raw.get("links")):
        if cleaned := _clean_image_url(value, trusted=True):
            return cleaned, "link"

    for key in ("summary", "description"):
        if cleaned := _image_from_html(raw.get(key), base_url):
            return cleaned, "html_img"

    for content in _as_list(raw.get("content")):
        if isinstance(content, dict) and (cleaned := _image_from_html(content.get("value"), base_url)):
            return cleaned, "html_img"

    return None, None


def extract_bluesky_media(payload: dict[str, Any]) -> tuple[str | None, str | None, str | None] | None:
    thread = payload.get("thread")
    if not isinstance(thread, dict):
        return None
    post = thread.get("post")
    if not isinstance(post, dict):
        return None
    return _bluesky_media_from_post(post)


def _bluesky_media_from_post(post: dict[str, Any]) -> tuple[str | None, str | None, str | None] | None:
    embed = post.get("embed")
    if isinstance(embed, dict):
        media = _bluesky_media_from_embed(embed, in_record=False)
        if media is not None:
            return media
    record = post.get("record")
    if isinstance(record, dict):
        embed = record.get("embed")
        if isinstance(embed, dict):
            return _bluesky_media_from_embed(embed, in_record=False)
    return None


def _entry_limit(feed: FeedRuntime, configured_limit: int) -> int:
    if _is_dvids_rss(feed.url):
        return max(configured_limit, DVIDS_MAX_ENTRIES_PER_FEED)
    return configured_limit


def _dvids_api_entry(feed: FeedRuntime, raw: dict[str, Any]) -> FeedEntry:
    title = clean_html_text(raw.get("title")) or "Untitled DVIDS asset"
    summary = clean_html_text(raw.get("short_description") or raw.get("description"))
    published = raw.get("publishdate") or raw.get("date_published") or raw.get("timestamp") or raw.get("date")
    image_url = _clean_image_url(raw.get("thumbnail"), trusted=True)
    return FeedEntry(
        feed_key=feed.feed_key,
        feed_name=feed.display_name,
        raw_guid=str(raw.get("id")) if raw.get("id") else None,
        raw_title=title,
        raw_url=str(raw.get("url")) if raw.get("url") else None,
        summary=summary,
        image_url=image_url,
        image_source="dvids_api_thumbnail" if image_url else None,
        raw_published_at=str(published) if published else None,
        parsed=raw,
        source_id=feed.source_id,
        source_class=feed.source_class,
    )


def _bluesky_media_from_embed(embed: dict[str, Any], in_record: bool) -> tuple[str | None, str | None, str | None] | None:
    embed_type = str(embed.get("$type") or "")

    if embed_type == "app.bsky.embed.images#view":
        image = _first_dict(embed.get("images"))
        if image:
            image_url = _clean_image_url(image.get("fullsize"), trusted=True) or _clean_image_url(
                image.get("thumb"), trusted=True
            )
            if image_url:
                return None, image_url, "bluesky_record_media" if in_record else "bluesky_image"

    if embed_type == "app.bsky.embed.external#view":
        external = embed.get("external")
        if isinstance(external, dict):
            article_url = _clean_article_url(external.get("uri"))
            image_url = _clean_image_url(external.get("thumb"), trusted=True)
            source = "bluesky_video_thumb" if _is_video_url(article_url) else "bluesky_external_thumb"
            if in_record:
                source = "bluesky_record_media"
            if article_url or image_url:
                return article_url, image_url, source if image_url else None

    if embed_type == "app.bsky.embed.video#view":
        image_url = (
            _clean_image_url(embed.get("thumbnail"), trusted=True)
            or _clean_image_url(embed.get("thumb"), trusted=True)
        )
        if image_url:
            return None, image_url, "bluesky_record_media" if in_record else "bluesky_video_thumb"

    if embed_type == "app.bsky.embed.recordWithMedia#view":
        media = embed.get("media")
        if isinstance(media, dict):
            resolved = _bluesky_media_from_embed(media, in_record=False)
            if resolved is not None:
                return resolved
        record_media = _bluesky_media_from_record_view(embed.get("record"))
        if record_media is not None:
            article_url, image_url, _source = record_media
            return article_url, image_url, "bluesky_record_media" if image_url else None

    if embed_type == "app.bsky.embed.record#view":
        return _bluesky_media_from_record_view(embed)

    return None


def _bluesky_media_from_record_view(value: object) -> tuple[str | None, str | None, str | None] | None:
    if not isinstance(value, dict):
        return None
    record = value.get("record")
    if isinstance(record, dict):
        embed = record.get("embed")
        if isinstance(embed, dict):
            return _bluesky_media_from_embed(embed, in_record=True)
        inner_record = record.get("record")
        if isinstance(inner_record, dict):
            inner_value = inner_record.get("value")
            embed = inner_record.get("embed")
            if not isinstance(embed, dict) and isinstance(inner_value, dict):
                embed = inner_value.get("embed")
            if isinstance(embed, dict):
                return _bluesky_media_from_embed(embed, in_record=True)
        value_embed = record.get("value", {}).get("embed") if isinstance(record.get("value"), dict) else None
        if isinstance(value_embed, dict):
            return _bluesky_media_from_embed(value_embed, in_record=True)
    value_embed = value.get("value", {}).get("embed") if isinstance(value.get("value"), dict) else None
    if isinstance(value_embed, dict):
        return _bluesky_media_from_embed(value_embed, in_record=True)
    return None


def _first_dict(value: object) -> dict[str, Any] | None:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                return item
    return None


def _clean_article_url(value: object) -> str | None:
    if value is None:
        return None
    url = html.unescape(str(value)).strip()
    if not url or any(char.isspace() for char in url):
        return None
    parsed = urlparse(url)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _is_video_url(value: str | None) -> bool:
    if not value:
        return False
    host = urlparse(value).netloc.casefold()
    return "youtube.com" in host or "youtu.be" in host


def _iter_media_urls(value: object, require_image_type: bool) -> list[str]:
    urls: list[str] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            content_type = str(item.get("type") or "").casefold()
            medium = str(item.get("medium") or "").casefold()
            url = item.get("url") or item.get("href")
            if not url:
                continue
            if require_image_type and not (
                content_type.startswith("image/") or medium == "image" or _looks_like_image_url(str(url))
            ):
                continue
            urls.append(str(url))
        elif isinstance(item, str) and (not require_image_type or _looks_like_image_url(item)):
            urls.append(item)
    return urls


def _iter_image_links(value: object) -> list[str]:
    urls: list[str] = []
    for item in _as_list(value):
        if not isinstance(item, dict):
            continue
        rel = str(item.get("rel") or "").casefold()
        content_type = str(item.get("type") or "").casefold()
        href = item.get("href")
        if href and (content_type.startswith("image/") or (rel == "enclosure" and _looks_like_image_url(str(href)))):
            urls.append(str(href))
    return urls


def _image_from_field(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ("href", "url"):
            if cleaned := _clean_image_url(value.get(key), trusted=True):
                return cleaned
        return None
    if isinstance(value, str):
        return _clean_image_url(value, trusted=_looks_like_image_url(value))
    return None


def _image_from_html(value: object, base_url: str) -> str | None:
    if value is None:
        return None
    parser = _ImageExtractor(base_url)
    try:
        parser.feed(str(value))
        parser.close()
    except Exception:
        return None
    return parser.image_url


def _as_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _clean_image_url(value: object, trusted: bool) -> str | None:
    if value is None:
        return None
    url = html.unescape(str(value)).strip()
    if not url or any(char.isspace() for char in url):
        return None
    parsed = urlparse(url)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    if not trusted and not _looks_like_image_url(url):
        return None
    return url


def _looks_like_image_url(value: str) -> bool:
    path = urlparse(value).path.casefold()
    return path.endswith(IMAGE_EXTENSIONS)


def clean_html_text(value: object) -> str | None:
    if value is None:
        return None
    text = html.unescape(str(value)).strip()
    if not text:
        return None
    if "<" not in text or ">" not in text:
        return re.sub(r"[ \t]+", " ", text).strip()
    parser = _TextExtractor()
    try:
        parser.feed(text)
        parser.close()
        cleaned = parser.text()
    except Exception:
        cleaned = HTML_TAG_RE.sub(" ", text)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if cleaned:
        lines = [line for line in cleaned.splitlines() if not _is_rss_boilerplate(line)]
        cleaned = "\n".join(lines).strip()
    return cleaned or None


def _is_rss_boilerplate(line: str) -> bool:
    normalized = line.casefold().strip()
    return normalized.startswith("the post ") and " appeared first on " in normalized


def _is_bluesky_rss(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.casefold() == BLUESKY_PROFILE_HOST and parsed.path.endswith("/rss")


def _bluesky_metadata(social_url: str | None) -> dict[str, Any]:
    if not _is_bluesky_post_url(social_url):
        return {}
    return {"social_url": social_url, "bluesky_post_url": social_url}


def _is_bluesky_post_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.casefold()
    if host.startswith("www."):
        host = host[4:]
    return host == BLUESKY_PROFILE_HOST and "/post/" in parsed.path


def _routing_summary_from_parsed(
    raw: dict[str, Any],
    *,
    display_summary: str | None,
    title: str,
    limit: int,
) -> str | None:
    candidates: list[str] = []
    for key in (
        "content",
        "content_encoded",
        "content:encoded",
        "full_content",
        "summary",
        "description",
        "subtitle",
    ):
        candidates.extend(_routing_text_values(raw.get(key)))
    if display_summary:
        candidates.append(display_summary)

    lines: list[str] = []
    seen: set[str] = set()
    title_clean = _routing_line_key(clean_html_text(title) or title)
    for value in candidates:
        cleaned = clean_html_text(value)
        if not cleaned:
            continue
        for raw_line in cleaned.splitlines():
            line = re.sub(r"[ \t]+", " ", raw_line).strip()
            if not line or _is_rss_boilerplate(line):
                continue
            line_key = _routing_line_key(line)
            if not line_key or line_key == title_clean or line_key in seen:
                continue
            seen.add(line_key)
            lines.append(line)
            if sum(len(item) + 1 for item in lines) >= limit:
                return _truncate_text("\n".join(lines), limit)
            if len(lines) >= 14:
                return _truncate_text("\n".join(lines), limit)
    return _truncate_text("\n".join(lines), limit) if lines else None


def _routing_text_values(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        values: list[str] = []
        for key in ("value", "content", "summary", "description"):
            child = value.get(key)
            if isinstance(child, str):
                values.append(child)
        return values
    if isinstance(value, (list, tuple)):
        values: list[str] = []
        for item in value:
            values.extend(_routing_text_values(item))
        return values
    return [str(value)]


def _routing_line_key(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _truncate_text(value: str, limit: int) -> str | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "..."


def _is_dvids_rss(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.casefold() == DVIDS_HOST and parsed.path.startswith("/rss/")


def _ical_entries(feed: FeedRuntime, body: bytes, entry_limit: int) -> tuple[FeedEntry, ...] | None:
    text = body.decode("utf-8", errors="ignore")
    if not text.lstrip().startswith("BEGIN:VCALENDAR"):
        return None
    now = datetime.now(UTC)
    events = []
    for event in _parse_ical_events(text):
        start = _parse_ical_datetime(event.get("DTSTART"), event.get("DTSTART_PARAMS"))
        if start is None or not _ical_in_window(start, now):
            continue
        uid = event.get("UID") or event.get("URL") or f"{event.get('SUMMARY', 'event')}:{event.get('DTSTART', '')}"
        title = clean_html_text(event.get("SUMMARY")) or "Calendar event"
        if _is_vip_schedule_calendar(feed) and "public schedule" not in title.casefold():
            title = f"Public Schedule: {title}"
        location = clean_html_text(event.get("LOCATION"))
        description = clean_html_text(event.get("DESCRIPTION"))
        start_label = start.strftime("%Y-%m-%d %H:%M UTC")
        summary_parts = [f"Start: {start_label}"]
        if location:
            summary_parts.append(f"Location: {location}")
        if description:
            summary_parts.append(description)
        events.append(
            (
                start,
                FeedEntry(
                    feed_key=feed.feed_key,
                    feed_name=feed.display_name,
                    raw_guid=f"{uid}:{event.get('DTSTART', '')}",
                    raw_title=f"{title} ({start_label})",
                    raw_url=event.get("URL") or None,
                    summary="\n".join(summary_parts),
                    image_url=None,
                    image_source=None,
                    raw_published_at=format_datetime(now),
                    parsed={"source": "ical", **event},
                    source_id=feed.source_id,
                    source_class=feed.source_class,
                ),
            )
        )
    return tuple(entry for _start, entry in sorted(events, key=lambda item: item[0])[:entry_limit])


def _is_vip_schedule_calendar(feed: FeedRuntime) -> bool:
    return feed.feed_key == "factbase-white-house-calendar" or feed.source_id == "factbase-white-house-calendar"


def _parse_ical_events(text: str) -> list[dict[str, str]]:
    lines = _unfold_ical_lines(text)
    events: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        name = key.split(";", 1)[0].upper()
        current[name] = _unescape_ical_value(value)
        if ";" in key:
            current[f"{name}_PARAMS"] = key.split(";", 1)[1]
    return events


def _unfold_ical_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.startswith((" ", "\t")) and lines:
            lines[-1] += raw_line[1:]
        elif raw_line:
            lines.append(raw_line)
    return lines


def _parse_ical_datetime(value: str | None, params: str | None = None) -> datetime | None:
    if not value:
        return None
    cleaned = value.strip()
    timezone = _ical_timezone(params)
    try:
        if re.fullmatch(r"\d{8}", cleaned):
            return datetime.strptime(cleaned, "%Y%m%d").replace(tzinfo=timezone).astimezone(UTC)
        if cleaned.endswith("Z"):
            return datetime.strptime(cleaned, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        parsed = datetime.strptime(cleaned, "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone).astimezone(UTC)


def _ical_timezone(params: str | None):
    if not params:
        return UTC
    match = re.search(r"(?:^|;)TZID=([^;]+)", params)
    if not match:
        return UTC
    try:
        return ZoneInfo(match.group(1))
    except ZoneInfoNotFoundError:
        return UTC


def _ical_in_window(start: datetime, now: datetime) -> bool:
    start_utc = start.astimezone(UTC)
    earliest = now - timedelta(hours=ICAL_RECENT_STARTED_HOURS)
    latest = now + timedelta(days=ICAL_LOOKAHEAD_DAYS)
    return earliest <= start_utc <= latest


def _unescape_ical_value(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
        .strip()
    )


def _state_collection_entries(feed: FeedRuntime, body: bytes, entry_limit: int) -> tuple[FeedEntry, ...] | None:
    if not _is_state_public_schedule_collection(feed.url):
        return None
    parser = _StateCollectionParser(feed.url)
    try:
        parser.feed(body.decode("utf-8", errors="ignore"))
        parser.close()
    except Exception as exc:
        raise FeedFetchError(f"state.gov collection parse failed: {exc}") from exc
    entries: list[FeedEntry] = []
    for result in parser.results[:entry_limit]:
        published = _state_collection_date_to_rfc(result.get("date", ""))
        entries.append(
            FeedEntry(
                feed_key=feed.feed_key,
                feed_name=feed.display_name,
                raw_guid=result["href"],
                raw_title=result["title"],
                raw_url=result["href"],
                summary="Official U.S. Department of State public schedule item.",
                image_url=None,
                image_source=None,
                raw_published_at=published,
                parsed={"source": "state_collection", "published": result.get("date", "")},
                source_id=feed.source_id,
                source_class=feed.source_class,
            )
        )
    return tuple(entries)


async def _mscio_document_folder_entries(
    session: aiohttp.ClientSession,
    feed: FeedRuntime,
    body: bytes,
    entry_limit: int,
    timeout_seconds: int,
) -> tuple[FeedEntry, ...] | None:
    if not _is_mscio_document_folder(feed.url):
        return None
    parser = _MscioDocumentFolderParser(feed.url)
    try:
        parser.feed(body.decode("utf-8", errors="ignore"))
        parser.close()
    except Exception as exc:
        raise FeedFetchError(f"MSCIO document folder parse failed: {exc}") from exc
    entries: list[FeedEntry] = []
    routing_tags = _mscio_document_routing_tags(feed)
    content_by_url: dict[str, str] = {}
    for result in parser.results[: min(entry_limit, MSCIO_DOCUMENT_PDF_LIMIT)]:
        if text := await _fetch_mscio_pdf_text(session, result["href"], timeout_seconds):
            content_by_url[result["href"]] = text
    for result in parser.results[:entry_limit]:
        title = _mscio_document_title(result["title"])
        summary = _mscio_document_summary(feed.display_name, result, content_by_url.get(result["href"]))
        entries.append(
            FeedEntry(
                feed_key=feed.feed_key,
                feed_name=feed.display_name,
                raw_guid=result["href"],
                raw_title=title,
                raw_url=result["href"],
                summary=summary,
                image_url=None,
                image_source=None,
                raw_published_at=_mscio_folder_date_to_rfc(result.get("created", "")),
                parsed={"source": "mscio_document_folder", **result},
                source_id=feed.source_id,
                source_class=feed.source_class,
                rich_metadata={"routing_summary": summary},
                routing_tags=routing_tags,
            )
        )
    return tuple(entries)


async def _fetch_mscio_pdf_text(session: aiohttp.ClientSession, url: str, timeout_seconds: int) -> str | None:
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=max(timeout_seconds, 20)),
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/pdf,*/*"},
        ) as response:
            if response.status != 200:
                logger.warning("MSCIO PDF fetch failed for %s: HTTP %s", url, response.status)
                return None
            body = await response.read()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("MSCIO PDF fetch failed for %s: %s", url, exc)
        return None
    if len(body) > MSCIO_DOCUMENT_MAX_BYTES:
        logger.warning("MSCIO PDF too large for content extraction: %s bytes from %s", len(body), url)
        return None
    try:
        return _extract_pdf_text(body, max_pages=MSCIO_DOCUMENT_MAX_PAGES)
    except Exception as exc:
        logger.warning("MSCIO PDF text extraction failed for %s: %s", url, exc)
        return None


def _extract_pdf_text(body: bytes, *, max_pages: int) -> str | None:
    reader = PdfReader(io.BytesIO(body))
    parts = []
    for page in reader.pages[:max_pages]:
        text = page.extract_text() or ""
        if text.strip():
            parts.append(text)
    return _clean_pdf_text("\n".join(parts)) or None


def _is_state_public_schedule_collection(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.casefold() == STATE_HOST and parsed.path.rstrip("/") == "/public-schedule"


def _is_mscio_document_folder(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/").casefold()
    return parsed.netloc.casefold() == MSCIO_HOST and path.startswith("/folder/documents/")


def _state_collection_date_to_rfc(value: str) -> str | None:
    match = re.fullmatch(r"\s*([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})\s*", value or "")
    if not match:
        return None
    month = STATE_MONTH_ABBR.get(match.group(1).casefold())
    if not month:
        return None
    return f"{int(match.group(2)):02d} {month} {match.group(3)} 00:00 +0000"


def _mscio_folder_date_to_rfc(value: str) -> str | None:
    match = re.fullmatch(
        r"\s*([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4}),\s+(\d{1,2}):(\d{2})\s+([ap])\.m\.\s*",
        value or "",
        re.IGNORECASE,
    )
    if not match:
        return None
    month = STATE_MONTH_ABBR.get(match.group(1).casefold())
    if not month:
        return None
    hour = int(match.group(4))
    minute = int(match.group(5))
    if match.group(6).casefold() == "p" and hour != 12:
        hour += 12
    elif match.group(6).casefold() == "a" and hour == 12:
        hour = 0
    return f"{int(match.group(2)):02d} {month} {match.group(3)} {hour:02d}:{minute:02d} +0000"


def _mscio_document_title(value: str) -> str:
    title = re.sub(r"^\d{8}[-_\s]+", "", value.strip())
    title = title.replace("_", " ")
    title = re.sub(r"\s+", " ", title).strip(" -")
    return title or "MSCIO maritime security document"


def _mscio_document_summary(feed_name: str, result: dict[str, str], pdf_text: str | None = None) -> str:
    content_summary = _mscio_pdf_content_summary(pdf_text)
    parts = [f"{feed_name} document."]
    if created := result.get("created", "").strip():
        parts.append(f"Created: {_with_period(created)}")
    if size := result.get("size", "").strip():
        parts.append(f"Size: {_with_period(size)}")
    if content_summary:
        parts.append(content_summary)
        return " ".join(parts)
    parts.append(
        "Maritime and naval security product covering UKMTO/JMIC reporting areas including the Red Sea, "
        "Gulf of Aden, Bab el-Mandeb, Strait of Hormuz, Gulf of Oman, Arabian Sea, Yemen, Somalia, Oman, "
        "UAE, Iran, Qatar, the Middle East, Africa, and Indian Ocean maritime routes."
    )
    return " ".join(parts)


def _clean_pdf_text(value: str) -> str:
    text = value.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    text = text.replace("w ithin", "within").replace("acti vity", "activity")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _mscio_pdf_content_summary(value: str | None) -> str | None:
    text = _clean_pdf_text(value or "")
    if not text:
        return None
    if "UKMTO WARNING" in text.upper():
        return _ukmto_warning_summary(text)
    return _generic_mscio_document_summary(text)


def _ukmto_warning_summary(text: str) -> str | None:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    warning = _first_match(text, r"\b(\d{3}-\d{2}\s*-\s*[A-Z][A-Z ]{2,})\b")
    report_date = _first_match(text, r"Report Date:\s*Report Time:\s*Issue Date:\s*Source\s+([0-9]{1,2}\s+[A-Za-z]+\s+\d{4})")
    report_time = _first_match(text, r"Report Date:\s*Report Time:\s*Issue Date:\s*Source\s+[0-9]{1,2}\s+[A-Za-z]+\s+\d{4}\s+([0-9]{3,4}UTC)")
    source = _first_match(
        text,
        r"Report Date:\s*Report Time:\s*Issue Date:\s*Source\s+[0-9]{1,2}\s+[A-Za-z]+\s+\d{4}\s+[0-9]{3,4}UTC\s+[0-9]{1,2}\s+[A-Za-z]+\s+\d{4}\s+([A-Za-z\s]+?)(?=\s+UKMTO has|\s*$)",
    )
    incident = _first_sentence_after(text, "UKMTO has received a report")
    details = _first_meaningful_sentence_after(text, incident)
    advice = _first_sentence_containing(lines, "advised")
    location = _location_from_ukmto_text(text, incident)

    parts = []
    if warning:
        parts.append(f"Warning: {warning.strip()}.")
    if report_date or report_time:
        parts.append("Report: " + " ".join(value for value in (report_date, report_time) if value).strip() + ".")
    if source:
        parts.append(f"Source: {source.strip()}.")
    if location:
        parts.append(f"Location: {location}.")
    if incident:
        parts.append(f"Incident: {incident}.")
    if details and details != incident:
        parts.append(f"Details: {details}.")
    if advice:
        parts.append(f"Advice: {advice}.")
    return " ".join(parts) if parts else _generic_mscio_document_summary(text)


def _generic_mscio_document_summary(text: str) -> str | None:
    useful = []
    for line in text.splitlines():
        cleaned = line.strip(" \t|")
        if not cleaned or "watchkeepers@ukmto.org" in cleaned or cleaned.startswith("+44"):
            continue
        if cleaned.casefold() in {"www.ukmto.org", "ukmto warning"}:
            continue
        useful.append(cleaned)
        if len(useful) >= 5:
            break
    return " ".join(useful)[:1200].strip() or None


def _first_match(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return re.sub(r"\s+", " ", match.group(1)).strip(" .")


def _first_sentence_after(text: str, needle: str) -> str | None:
    index = text.casefold().find(needle.casefold())
    if index < 0:
        return None
    tail = text[index:]
    return _first_sentence(tail)


def _first_meaningful_sentence_after(text: str, previous: str | None) -> str | None:
    normalized = re.sub(r"\s+", " ", text).strip()
    sentences = _sentences(text)
    if previous:
        previous_key = previous.casefold()
        previous_index = normalized.casefold().find(previous_key)
        if previous_index >= 0:
            tail = normalized[previous_index + len(previous) :]
            for candidate in _sentences(tail):
                if _looks_like_incident_detail(candidate):
                    return candidate
    for sentence in sentences:
        if _looks_like_incident_detail(sentence) and "watchkeepers@ukmto.org" not in sentence:
            return sentence
    return None


def _first_sentence_containing(lines: list[str], needle: str) -> str | None:
    joined = " ".join(lines)
    for sentence in _sentences(joined):
        if needle.casefold() in sentence.casefold():
            return sentence
    return None


def _first_sentence(text: str) -> str | None:
    return next(iter(_sentences(text)), None)


def _sentences(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z0-9])", normalized)
    return [part.strip(" .") for part in parts if part.strip(" .")]


def _looks_like_incident_detail(sentence: str) -> bool:
    folded = sentence.casefold()
    return any(
        term in folded
        for term in (
            "vessel has",
            "vessel was",
            "approached",
            "fired upon",
            "skiff",
            "uncrewed",
            "attack",
            "suspicious",
            "crew are",
            "crew is",
        )
    )


def _location_from_ukmto_text(text: str, incident: str | None) -> str | None:
    source = incident or text
    match = re.search(r"incident\s+(.+?)(?:\.|$)", source, re.IGNORECASE)
    if match:
        value = re.sub(r"\s+", " ", match.group(1)).strip()
        if value:
            return value
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in reversed(lines[-8:]):
        if "," in line and len(line) <= 80:
            return line
    return None


def _with_period(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()
    if not cleaned:
        return ""
    return cleaned if cleaned.endswith((".", "!", "?")) else f"{cleaned}."


def _mscio_document_routing_tags(feed: FeedRuntime) -> tuple[str, ...]:
    base = ["maritime", "naval", "middle_east", "africa", "indo_pacific"]
    for channel_key in feed.channel_keys:
        candidate = channel_key.replace("-", "_")
        if candidate not in base:
            base.append(candidate)
    return tuple(base)


def _class_contains(classes: str, expected: str) -> bool:
    return expected in {value.strip() for value in classes.split()}


def _dvids_unit_id(url: str) -> str | None:
    parsed = urlparse(url)
    match = re.fullmatch(r"/rss/unit/(\d+)", parsed.path.rstrip("/"))
    if not match:
        return None
    return match.group(1)


def _is_bluesky_post_uri(value: str | None) -> bool:
    return bool(value and value.startswith("at://") and "/app.bsky.feed.post/" in value)


def _bluesky_title(summary: str | None) -> str:
    for line in (summary or "").splitlines():
        cleaned = _strip_urls(line).strip(" \t-:|")
        if cleaned:
            return cleaned[:256]
    return "Bluesky post"


def _strip_urls(value: str) -> str:
    return re.sub(r"\s+", " ", URL_RE.sub("", value)).strip()


def _first_external_url(summary: str | None) -> str | None:
    for match in URL_RE.finditer(summary or ""):
        value = match.group(0).rstrip(").,;]")
        if value.startswith("www."):
            value = f"https://{value}"
        elif not value.startswith(("http://", "https://")):
            value = f"https://{value}"
        parsed = urlparse(value)
        if parsed.netloc.casefold() != BLUESKY_PROFILE_HOST:
            return value
    return None
