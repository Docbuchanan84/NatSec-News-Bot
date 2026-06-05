from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlparse

import aiohttp
import feedparser

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
                    if fallback := await self._fetch_dvids_api_fallback(session, feed, action):
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

        parsed = feedparser.parse(body)
        if parsed.bozo and parsed.bozo_exception:
            logger.warning("Feed parse warning for %s: %s", feed.display_name, parsed.bozo_exception)
        if parsed.bozo and not parsed.entries:
            raise FeedFetchError(f"feed parse failed: {parsed.bozo_exception}")
        raw_entries = parsed.entries[: _entry_limit(feed, self.max_entries_per_feed)]
        entries = tuple(self._entry_from_parsed(feed, raw) for raw in raw_entries)
        if _is_bluesky_rss(feed.url):
            entries = await self._enrich_bluesky_entries(session, entries)
        return FeedFetchResult(feed=feed, entries=entries)

    def _entry_from_parsed(self, feed: FeedRuntime, raw: Any) -> FeedEntry:
        raw_dict = dict(raw)
        url = raw_dict.get("link") or raw_dict.get("id")
        guid = raw_dict.get("id") or raw_dict.get("guid")
        raw_summary = raw_dict.get("summary") or raw_dict.get("description")
        summary = clean_html_text(raw_summary)
        title = raw_dict.get("title") or summary or "Untitled article"
        if _is_bluesky_rss(feed.url):
            title = _bluesky_title(summary)
            url = _first_external_url(summary) or url
        image = extract_entry_image(raw_dict, base_url=str(url or feed.url))
        published = raw_dict.get("published") or raw_dict.get("updated") or raw_dict.get("created")
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
        )

    async def _enrich_bluesky_entries(
        self,
        session: aiohttp.ClientSession,
        entries: tuple[FeedEntry, ...],
    ) -> tuple[FeedEntry, ...]:
        enriched: list[FeedEntry] = []
        for entry in entries:
            if not _is_bluesky_post_uri(entry.raw_guid):
                enriched.append(entry)
                continue
            enriched.append(await self._enrich_bluesky_entry(session, entry))
        return tuple(enriched)

    async def _enrich_bluesky_entry(self, session: aiohttp.ClientSession, entry: FeedEntry) -> FeedEntry:
        if not entry.raw_guid:
            return entry
        url = f"{BLUESKY_POST_THREAD_URL}?uri={quote(entry.raw_guid, safe='')}"
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
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
        return replace(
            entry,
            raw_url=article_url or entry.raw_url,
            image_url=image_url or entry.image_url,
            image_source=image_source or entry.image_source,
        )

    async def _fetch_dvids_api_fallback(
        self,
        session: aiohttp.ClientSession,
        feed: FeedRuntime,
        waf_action: str | None,
    ) -> FeedFetchResult | None:
        api_key = os.environ.get("DVIDS_API_KEY", "").strip()
        unit_id = _dvids_unit_id(feed.url)
        if not api_key or not unit_id:
            return None
        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
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


def _is_dvids_rss(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.casefold() == DVIDS_HOST and parsed.path.startswith("/rss/")


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
