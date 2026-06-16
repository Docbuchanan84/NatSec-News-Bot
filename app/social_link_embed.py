from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp
import discord

from app.database import Database
from app.feed_fetcher import DEFAULT_USER_AGENT, clean_html_text, extract_bluesky_media
from app.models import AppConfig, FeedEntry, PostJob, SocialLinkEmbedSettings
from app.normalizer import build_candidate
from app.x_media import fetch_fxtwitter_status

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("app.audit")

X_STATUS_URL_RE = re.compile(
    r"https?://(?:mobile\.|www\.)?(?P<host>x\.com|twitter\.com)/"
    r"(?P<username>[A-Za-z0-9_]{1,15})/status(?:es)?/(?P<post_id>\d{2,20})(?:[/?#][^\s<]*)?",
    re.IGNORECASE,
)
BLUESKY_POST_URL_RE = re.compile(
    r"https?://(?:www\.)?bsky\.app/profile/(?P<handle>[^/\s<>]+)/post/(?P<post_id>[^/?#\s<>]+)(?:[/?#][^\s<]*)?",
    re.IGNORECASE,
)
SOCIAL_LINK_TIMEOUT_SECONDS = 20


class SocialReplySender(Protocol):
    async def send_social_reply(self, job: PostJob, source_message: Any) -> str:
        ...


@dataclass(frozen=True)
class SocialPostReference:
    platform: str
    account: str
    post_id: str
    url: str


class SocialLinkEmbedService:
    def __init__(self, db: Database, sender: SocialReplySender) -> None:
        self.db = db
        self.sender = sender
        self.config: AppConfig | None = None
        self.settings = SocialLinkEmbedSettings()
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrent_lookups)
        self._in_flight: set[tuple[str, str, str]] = set()

    def configure(self, config: AppConfig) -> None:
        self.config = config
        self.settings = config.settings.social_link_embeds
        self._semaphore = asyncio.Semaphore(self.settings.max_concurrent_lookups)

    async def handle_message(self, message: discord.Message) -> None:
        if not self.settings.enabled or self.config is None:
            return
        if getattr(getattr(message, "author", None), "bot", False):
            return
        if getattr(message, "webhook_id", None):
            return
        if getattr(message, "guild", None) is None:
            return

        channel_id = str(getattr(getattr(message, "channel", None), "id", "") or "")
        message_id = str(getattr(message, "id", "") or "")
        if not channel_id or not message_id:
            return
        if self.settings.target_channel_ids and channel_id not in self.settings.target_channel_ids:
            return

        reference = extract_social_post_reference(_message_search_text(message))
        if reference is None:
            return

        is_duplicate = self.db.has_recent_social_link_embed(
            channel_id,
            reference.platform,
            reference.post_id,
            self.settings.dedupe_window_hours,
        )
        if self.settings.suppress_original_preview and not _message_has_suppressed_embeds(message):
            await suppress_original_preview(message, reference)

        if is_duplicate:
            audit_logger.info(
                "social_link_embed_duplicate platform=%s channel_id=%s message_id=%s post_id=%s",
                reference.platform,
                channel_id,
                message_id,
                reference.post_id,
            )
            return

        flight_key = (channel_id, reference.platform, reference.post_id)
        if flight_key in self._in_flight:
            return
        self._in_flight.add(flight_key)
        try:
            async with self._semaphore:
                await self._fetch_and_send(message, reference, channel_id, message_id)
        finally:
            self._in_flight.discard(flight_key)

    async def _fetch_and_send(
        self,
        message: discord.Message,
        reference: SocialPostReference,
        channel_id: str,
        source_message_id: str,
    ) -> None:
        config = self.config
        if config is None:
            return
        timeout = aiohttp.ClientTimeout(total=SOCIAL_LINK_TIMEOUT_SECONDS)
        headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "application/json"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            if reference.platform == "x":
                entry = await _x_entry_from_reference(session, reference)
            elif reference.platform == "bluesky":
                entry = await _bluesky_entry_from_reference(session, reference)
            else:
                return
        if entry is None:
            return

        candidate = build_candidate(entry, config.settings.timestamps)
        dedupe = self.db.resolve_article(candidate, config.settings.dedupe.title_match_window_hours)
        job = self.db.get_post_job(dedupe.article_id, channel_id)
        bot_message_id = await self.sender.send_social_reply(job, message)
        self.db.record_social_link_embed(
            channel_id=channel_id,
            platform=reference.platform,
            post_id=reference.post_id,
            normalized_url=reference.url,
            source_message_id=source_message_id,
            bot_message_id=bot_message_id,
        )
        audit_logger.info(
            "social_link_embed_sent platform=%s article_id=%s channel_id=%s source_message_id=%s bot_message_id=%s post_id=%s",
            reference.platform,
            dedupe.article_id,
            channel_id,
            source_message_id,
            bot_message_id,
            reference.post_id,
        )


def extract_social_post_reference(text: str) -> SocialPostReference | None:
    x_ref = extract_x_status_reference(text)
    if x_ref is not None:
        return x_ref
    for match in BLUESKY_POST_URL_RE.finditer(text):
        handle = match.group("handle").strip()
        post_id = match.group("post_id").strip()
        return SocialPostReference(
            platform="bluesky",
            account=handle,
            post_id=post_id,
            url=canonical_bluesky_url(handle, post_id),
        )
    return None


def extract_x_status_reference(text: str) -> SocialPostReference | None:
    for match in X_STATUS_URL_RE.finditer(text):
        username = match.group("username").strip()
        post_id = match.group("post_id").strip()
        if username.casefold() in {"i", "intent", "share"}:
            continue
        return SocialPostReference(platform="x", account=username, post_id=post_id, url=canonical_x_url(username, post_id))
    return None


def canonical_x_url(username: str, post_id: str) -> str:
    return f"https://x.com/{username.strip().lstrip('@')}/status/{post_id.strip()}"


def canonical_bluesky_url(handle: str, post_id: str) -> str:
    return f"https://bsky.app/profile/{handle.strip().lstrip('@')}/post/{post_id.strip()}"


async def suppress_original_preview(message: discord.Message, reference: SocialPostReference) -> None:
    if _message_has_suppressed_embeds(message):
        return
    try:
        await message.edit(suppress=True)
    except discord.Forbidden:
        logger.error(
            "Discord permission issue: Manage Messages is required to suppress %s preview for message %s in channel %s",
            reference.platform,
            getattr(message, "id", None),
            getattr(getattr(message, "channel", None), "id", None),
        )
    except discord.HTTPException as exc:
        logger.warning("Discord failed to suppress %s preview for %s: %s", reference.platform, reference.url, exc)


def _message_has_suppressed_embeds(message: discord.Message) -> bool:
    flags = getattr(message, "flags", None)
    if flags is None:
        return False
    return bool(getattr(flags, "suppress_embeds", False))


def _message_search_text(message: discord.Message) -> str:
    parts = [str(getattr(message, "content", "") or "")]
    for embed in getattr(message, "embeds", []) or []:
        for attr in ("url", "title", "description"):
            value = getattr(embed, attr, None)
            if value:
                parts.append(str(value))
        author = getattr(embed, "author", None)
        author_url = getattr(author, "url", None)
        if author_url:
            parts.append(str(author_url))
    return "\n".join(parts)


async def _x_entry_from_reference(
    session: aiohttp.ClientSession,
    reference: SocialPostReference,
) -> FeedEntry | None:
    payload = await fetch_fxtwitter_status(session, reference.post_id)
    status = payload.get("status") if isinstance(payload, dict) else None
    if not isinstance(status, dict):
        logger.warning("FxEmbed returned no usable status for pasted X link %s", reference.url)
        return None
    return feed_entry_from_fxtwitter_status(status, reference)


def feed_entry_from_fxtwitter_status(status: dict[str, Any], reference: SocialPostReference) -> FeedEntry:
    metadata = rich_metadata_from_fxtwitter_status(status, reference)
    text = str(metadata.get("text") or "").strip() or "X post"
    title = _title_from_text(text) or "X post"
    author = metadata.get("author") if isinstance(metadata.get("author"), dict) else {}
    username = str(author.get("username") or reference.account).strip().lstrip("@") or reference.account
    name = clean_html_text(author.get("name")) or username
    first_media = _first_media_image_url(metadata)
    created_at = str(status.get("created_at") or "").strip() or None
    post_id = str(metadata.get("post_id") or reference.post_id)
    url = str(metadata.get("social_url") or reference.url)
    return FeedEntry(
        feed_key=f"x-message:{post_id}",
        feed_name=f"X: @{username}",
        raw_guid=f"x-message:{post_id}",
        raw_title=title,
        raw_url=url,
        summary=_summary(username, name, text, metadata),
        image_url=first_media,
        image_source="x_media" if first_media else None,
        raw_published_at=created_at,
        parsed={"source": "x_message", "post_id": post_id},
        source_id=_source_id_for_username(username),
        source_class="social_core",
        rich_metadata=metadata,
    )


def rich_metadata_from_fxtwitter_status(status: dict[str, Any], reference: SocialPostReference) -> dict[str, Any]:
    post_id = str(status.get("id") or reference.post_id)
    post_url = str(status.get("url") or reference.url)
    author = _author_metadata(status.get("author"), fallback_username=reference.account)
    metadata: dict[str, Any] = {
        "source": "x_message",
        "post_id": post_id,
        "social_url": post_url,
        "x_post_url": post_url,
        "text": _status_text(status),
        "author": author,
        "media": _media_items(status.get("media")),
    }
    quote_status = status.get("quote")
    if isinstance(quote_status, dict) and quote_status.get("type") != "tombstone":
        metadata["quote"] = {
            "text": _status_text(quote_status),
            "author": _author_metadata(quote_status.get("author"), fallback_username=""),
            "media": _media_items(quote_status.get("media")),
        }
    return metadata


async def _bluesky_entry_from_reference(
    session: aiohttp.ClientSession,
    reference: SocialPostReference,
) -> FeedEntry | None:
    payload = await fetch_bluesky_post_thread(session, reference)
    thread = payload.get("thread") if isinstance(payload, dict) else None
    post = thread.get("post") if isinstance(thread, dict) else None
    if not isinstance(post, dict):
        logger.warning("Bluesky returned no usable post for pasted link %s", reference.url)
        return None
    record = post.get("record") if isinstance(post.get("record"), dict) else {}
    author = post.get("author") if isinstance(post.get("author"), dict) else {}
    handle = str(author.get("handle") or reference.account).strip().lstrip("@")
    display_name = clean_html_text(author.get("displayName")) or f"@{handle}"
    text = clean_html_text(record.get("text")) or "Bluesky post"
    created_at = str(record.get("createdAt") or "").strip() or None
    article_url, image_url, image_source = (extract_bluesky_media(payload) or (None, None, None))
    post_url = canonical_bluesky_url(handle or reference.account, reference.post_id)
    metadata = {
        "source": "bluesky_message",
        "post_id": reference.post_id,
        "social_url": post_url,
        "bluesky_post_url": post_url,
        "text": text,
        "author": {
            "handle": handle,
            "display_name": display_name,
            "did": str(author.get("did") or ""),
        },
    }
    return FeedEntry(
        feed_key=f"bluesky-message:{handle}:{reference.post_id}",
        feed_name=f"Bluesky: {display_name}",
        raw_guid=f"bluesky-message:{handle}:{reference.post_id}",
        raw_title=_title_from_text(text) or "Bluesky post",
        raw_url=article_url or post_url,
        summary=text,
        image_url=image_url,
        image_source=image_source,
        raw_published_at=created_at,
        parsed={"source": "bluesky_message", "post_id": reference.post_id},
        source_id=_source_id_for_username(f"bluesky-{handle or reference.account}"),
        source_class="social_core",
        rich_metadata=metadata,
    )


async def fetch_bluesky_post_thread(
    session: aiohttp.ClientSession,
    reference: SocialPostReference,
) -> dict[str, Any]:
    did = reference.account
    if not did.startswith("did:"):
        resolve_url = "https://public.api.bsky.app/xrpc/com.atproto.identity.resolveHandle"
        async with session.get(resolve_url, params={"handle": reference.account}) as response:
            if response.status >= 400:
                logger.warning("Bluesky handle lookup failed for %s: HTTP %s", reference.account, response.status)
                return {}
            resolved = await response.json(content_type=None)
        did = str(resolved.get("did") or "").strip()
    if not did:
        return {}
    uri = f"at://{did}/app.bsky.feed.post/{reference.post_id}"
    thread_url = "https://public.api.bsky.app/xrpc/app.bsky.feed.getPostThread"
    async with session.get(thread_url, params={"uri": uri, "depth": "0", "parentHeight": "0"}) as response:
        if response.status >= 400:
            logger.warning("Bluesky post lookup failed for %s: HTTP %s", reference.url, response.status)
            return {}
        return await response.json(content_type=None)


def _author_metadata(value: Any, *, fallback_username: str) -> dict[str, str]:
    author = value if isinstance(value, dict) else {}
    username = str(author.get("screen_name") or author.get("username") or fallback_username).strip().lstrip("@")
    name = clean_html_text(author.get("name")) or username or "X user"
    return {
        "id": str(author.get("id") or ""),
        "username": username,
        "name": name,
        "profile_image_url": str(author.get("avatar_url") or author.get("profile_image_url") or ""),
    }


def _status_text(status: dict[str, Any]) -> str:
    raw_text = status.get("raw_text")
    if isinstance(raw_text, dict) and raw_text.get("text"):
        return clean_html_text(raw_text.get("text")) or str(raw_text.get("text") or "")
    return clean_html_text(status.get("text")) or str(status.get("text") or "")


def _media_items(value: Any) -> list[dict[str, str]]:
    media = value if isinstance(value, dict) else {}
    output: list[dict[str, str]] = []
    for photo in _list_of_dicts(media.get("photos")):
        url = str(photo.get("url") or "").strip()
        if url:
            output.append({"type": "photo", "url": url, "preview_image_url": url})
    for video in _list_of_dicts(media.get("videos")):
        url = str(video.get("url") or video.get("transcode_url") or "").strip()
        preview = str(video.get("thumbnail_url") or "").strip()
        if url:
            output.append(
                {
                    "type": "animated_gif" if str(video.get("type") or "").casefold() == "gif" else "video",
                    "url": url,
                    "preview_image_url": preview,
                }
            )
        elif preview:
            output.append({"type": "photo", "url": preview, "preview_image_url": preview})
    external = media.get("external")
    if isinstance(external, dict):
        thumb = str(external.get("thumbnail_url") or "").strip()
        if thumb:
            output.append({"type": "photo", "url": thumb, "preview_image_url": thumb})
    return output


def _first_media_image_url(metadata: dict[str, Any]) -> str | None:
    media = metadata.get("media")
    if not isinstance(media, list):
        return None
    for item in media:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "photo" and item.get("url"):
            return str(item["url"])
        if item.get("preview_image_url"):
            return str(item["preview_image_url"])
    return None


def _summary(username: str, name: str, text: str, metadata: dict[str, Any]) -> str:
    parts = [f"@{username} ({name})", text]
    quote_status = metadata.get("quote")
    if isinstance(quote_status, dict) and quote_status.get("text"):
        parts.append(f"Quote: {quote_status.get('text')}")
    return "\n".join(part for part in parts if part)


def _title_from_text(text: str) -> str:
    for line in text.splitlines():
        cleaned = re.sub(r"https?://\S+", "", line).strip(" \t-:|")
        if cleaned:
            return cleaned[:256]
    return ""


def _source_id_for_username(username: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", username.casefold()).strip("-")
    return f"x-{normalized}"[:64] if normalized else "social-message"


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]
