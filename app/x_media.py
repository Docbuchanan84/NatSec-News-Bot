from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from PIL import Image, ImageSequence

from app.feed_fetcher import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)

FXTWITTER_STATUS_URL = "https://api.fxtwitter.com/2/status/{post_id}"
DEFAULT_MAX_FILES = 10
DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_WORKING_BYTES = 250 * 1024 * 1024
IMAGE_TYPES = {"photo", "image"}
VIDEO_TYPES = {"video", "gif", "animated_gif"}


@dataclass(frozen=True)
class PreparedMedia:
    path: Path
    filename: str
    source_url: str
    media_type: str


@contextlib.asynccontextmanager
async def prepared_x_media_files(
    metadata: dict[str, Any],
    *,
    timeout_seconds: int = 20,
    max_files: int | None = None,
    max_file_bytes: int | None = None,
    max_working_bytes: int | None = None,
) -> AsyncIterator[list[PreparedMedia]]:
    max_files = max_files if max_files is not None else _env_int("X_MEDIA_MAX_FILES", DEFAULT_MAX_FILES)
    max_file_bytes = max_file_bytes if max_file_bytes is not None else _env_int(
        "X_MEDIA_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES
    )
    max_working_bytes = max_working_bytes if max_working_bytes is not None else _env_int(
        "X_MEDIA_MAX_WORKING_BYTES", DEFAULT_MAX_WORKING_BYTES
    )
    with tempfile.TemporaryDirectory(prefix="rssbot-x-media-") as temp_name:
        temp_dir = Path(temp_name)
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}
        prepared: list[PreparedMedia] = []
        prepared_hashes: set[str] = set()
        working_bytes = 0
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            media_items = await collect_x_media(metadata, session=session, max_items=max_files)
            for index, item in enumerate(media_items[:max_files], start=1):
                if working_bytes >= max_working_bytes:
                    logger.warning("Skipping X media because temp working-set limit is reached")
                    break
                try:
                    media = await _prepare_one_media(
                        session,
                        item,
                        temp_dir=temp_dir,
                        index=index,
                        max_file_bytes=min(max_file_bytes, max_working_bytes - working_bytes),
                    )
                except Exception as exc:
                    logger.warning("X media preparation failed for %s: %s", item.get("url"), exc)
                    continue
                if media is None:
                    continue
                digest = _file_digest(media.path)
                if digest in prepared_hashes:
                    logger.info("Skipping duplicate X media prepared from %s", media.source_url)
                    continue
                prepared_hashes.add(digest)
                try:
                    working_bytes += media.path.stat().st_size
                except FileNotFoundError:
                    continue
                prepared.append(media)
        try:
            yield prepared
        finally:
            prepared.clear()


@contextlib.asynccontextmanager
async def prepared_remote_media_files(
    media_items: list[dict[str, str]] | tuple[dict[str, str], ...],
    *,
    timeout_seconds: int = 20,
    max_files: int | None = None,
    max_file_bytes: int | None = None,
    max_working_bytes: int | None = None,
) -> AsyncIterator[list[PreparedMedia]]:
    max_files = max_files if max_files is not None else _env_int("MEDIA_UPLOAD_MAX_FILES", DEFAULT_MAX_FILES)
    max_file_bytes = max_file_bytes if max_file_bytes is not None else _env_int(
        "MEDIA_UPLOAD_MAX_FILE_BYTES", DEFAULT_MAX_FILE_BYTES
    )
    max_working_bytes = max_working_bytes if max_working_bytes is not None else _env_int(
        "MEDIA_UPLOAD_MAX_WORKING_BYTES", DEFAULT_MAX_WORKING_BYTES
    )
    with tempfile.TemporaryDirectory(prefix="rssbot-media-") as temp_name:
        temp_dir = Path(temp_name)
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        headers = {"User-Agent": DEFAULT_USER_AGENT, "Accept": "*/*"}
        prepared: list[PreparedMedia] = []
        prepared_hashes: set[str] = set()
        working_bytes = 0
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for index, item in enumerate(_dedupe_media(list(media_items))[:max_files], start=1):
                if working_bytes >= max_working_bytes:
                    logger.warning("Skipping media upload because temp working-set limit is reached")
                    break
                try:
                    media = await _prepare_one_media(
                        session,
                        item,
                        temp_dir=temp_dir,
                        index=index,
                        max_file_bytes=min(max_file_bytes, max_working_bytes - working_bytes),
                    )
                except Exception as exc:
                    logger.warning("Media preparation failed for %s: %s", item.get("url"), exc)
                    continue
                if media is None:
                    continue
                digest = _file_digest(media.path)
                if digest in prepared_hashes:
                    logger.info("Skipping duplicate media prepared from %s", media.source_url)
                    continue
                prepared_hashes.add(digest)
                try:
                    working_bytes += media.path.stat().st_size
                except FileNotFoundError:
                    continue
                prepared.append(media)
        try:
            yield prepared
        finally:
            prepared.clear()


async def collect_x_media(
    metadata: dict[str, Any],
    *,
    session: aiohttp.ClientSession,
    max_items: int = DEFAULT_MAX_FILES,
) -> list[dict[str, str]]:
    collected: list[dict[str, str]] = []
    _add_local_media(collected, metadata)
    post_id = str(metadata.get("post_id") or "").strip()
    if _fxembed_enabled() and post_id and len(collected) < max_items:
        payload = await fetch_fxtwitter_status(session, post_id)
        status = payload.get("status") if isinstance(payload, dict) else None
        if isinstance(status, dict):
            _add_fxtwitter_status_media(collected, status)
    return _dedupe_media(collected)[:max_items]


def _add_local_media(collected: list[dict[str, str]], metadata: dict[str, Any]) -> None:
    for item in _list_of_dicts(metadata.get("media")):
        _append_media_item(collected, item, origin="post")
    repost = metadata.get("repost")
    if isinstance(repost, dict):
        for item in _list_of_dicts(repost.get("media")):
            _append_media_item(collected, item, origin="repost")
    quote = metadata.get("quote")
    if isinstance(quote, dict):
        for item in _list_of_dicts(quote.get("media")):
            _append_media_item(collected, item, origin="quote")


def _append_media_item(collected: list[dict[str, str]], item: dict[str, Any], *, origin: str) -> None:
    media_type = str(item.get("type") or "photo").casefold()
    url = str(item.get("url") or "").strip()
    preview = str(item.get("preview_image_url") or item.get("thumbnail_url") or "").strip()
    if media_type in IMAGE_TYPES and url:
        collected.append({"type": "photo", "url": url, "origin": origin})
    elif media_type in VIDEO_TYPES and url:
        collected.append({"type": "video", "url": url, "origin": origin})
        if preview:
            collected.append({"type": "photo", "url": preview, "origin": origin})
    elif preview:
        collected.append({"type": "photo", "url": preview, "origin": origin})


async def fetch_fxtwitter_status(session: aiohttp.ClientSession, post_id: str) -> dict[str, Any]:
    url = FXTWITTER_STATUS_URL.format(post_id=post_id)
    try:
        async with session.get(url) as response:
            body = await response.text()
            if response.status >= 400:
                logger.warning("FxEmbed status lookup failed for %s: HTTP %s", post_id, response.status)
                return {}
            try:
                payload = await response.json(content_type=None)
            except Exception:
                logger.warning("FxEmbed status lookup returned invalid JSON for %s: %s", post_id, body[:160])
                return {}
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        logger.warning("FxEmbed status lookup failed for %s: %s", post_id, exc)
        return {}
    if not isinstance(payload, dict) or int(payload.get("code") or response.status) >= 400:
        return {}
    return payload


def _add_fxtwitter_status_media(collected: list[dict[str, str]], status: dict[str, Any], *, origin: str = "post") -> None:
    media = status.get("media")
    if isinstance(media, dict):
        for photo in _list_of_dicts(media.get("photos")):
            url = str(photo.get("url") or "").strip()
            if url:
                collected.append({"type": "photo", "url": url, "origin": origin})
        for video in _list_of_dicts(media.get("videos")):
            url = str(video.get("url") or video.get("transcode_url") or "").strip()
            thumb = str(video.get("thumbnail_url") or "").strip()
            if url:
                collected.append({"type": "video", "url": url, "origin": origin})
            elif thumb:
                collected.append({"type": "photo", "url": thumb, "origin": origin})
        external = media.get("external")
        if isinstance(external, dict):
            thumb = str(external.get("thumbnail_url") or "").strip()
            if thumb:
                collected.append({"type": "photo", "url": thumb, "origin": origin})
    quote = status.get("quote")
    if isinstance(quote, dict):
        _add_fxtwitter_status_media(collected, quote, origin="quote")


async def _prepare_one_media(
    session: aiohttp.ClientSession,
    item: dict[str, str],
    *,
    temp_dir: Path,
    index: int,
    max_file_bytes: int,
) -> PreparedMedia | None:
    url = item["url"]
    media_type = item.get("type") or "photo"
    raw_path, content_type = await _download_media(session, url, temp_dir=temp_dir, index=index, max_file_bytes=max_file_bytes)
    if raw_path is None:
        return None
    if media_type == "video" or _looks_like_video(url, content_type):
        output = temp_dir / f"x-media-{index:02d}.mp4"
        if await _strip_video_metadata(raw_path, output, max_file_bytes=max_file_bytes):
            return PreparedMedia(output, output.name, url, "video")
        return None
    output = temp_dir / f"x-media-{index:02d}.jpg"
    if _strip_image_metadata(raw_path, output, max_file_bytes=max_file_bytes):
        return PreparedMedia(output, output.name, url, "photo")
    return None


async def _download_media(
    session: aiohttp.ClientSession,
    url: str,
    *,
    temp_dir: Path,
    index: int,
    max_file_bytes: int,
) -> tuple[Path | None, str]:
    async with session.get(url) as response:
        if response.status >= 400:
            logger.warning("X media download failed for %s: HTTP %s", url, response.status)
            return None, ""
        content_length = response.headers.get("Content-Length")
        if content_length and int(content_length) > max_file_bytes:
            logger.warning("Skipping oversized X media from %s: %s bytes", url, content_length)
            return None, response.headers.get("Content-Type", "")
        suffix = _suffix_from_url(url)
        raw_path = temp_dir / f"raw-{index:02d}{suffix}"
        total = 0
        with raw_path.open("wb") as handle:
            async for chunk in response.content.iter_chunked(1024 * 256):
                total += len(chunk)
                if total > max_file_bytes:
                    logger.warning("Skipping oversized X media from %s after download exceeded limit", url)
                    return None, response.headers.get("Content-Type", "")
                handle.write(chunk)
        return raw_path, response.headers.get("Content-Type", "")


def _strip_image_metadata(input_path: Path, output_path: Path, *, max_file_bytes: int) -> bool:
    with Image.open(input_path) as image:
        frames = [frame.copy() for frame in ImageSequence.Iterator(image)]
        frame = frames[0] if frames else image.copy()
        if frame.mode not in {"RGB", "L"}:
            frame = frame.convert("RGB")
        for quality in (90, 82, 74, 66):
            frame.save(output_path, format="JPEG", quality=quality, optimize=True)
            if output_path.stat().st_size <= max_file_bytes:
                return True
        resized = frame.copy()
        while min(resized.size) > 720:
            width, height = resized.size
            resized = resized.resize((max(1, int(width * 0.85)), max(1, int(height * 0.85))))
            resized.save(output_path, format="JPEG", quality=74, optimize=True)
            if output_path.stat().st_size <= max_file_bytes:
                return True
    return output_path.stat().st_size <= max_file_bytes


async def _strip_video_metadata(input_path: Path, output_path: Path, *, max_file_bytes: int) -> bool:
    command = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(input_path),
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-c",
        "copy",
        str(output_path),
    ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await process.communicate()
    if process.returncode != 0:
        logger.warning("ffmpeg failed to strip X video metadata from %s: %s", input_path, stderr.decode("utf-8", "ignore")[:300])
        return False
    return output_path.exists() and output_path.stat().st_size <= max_file_bytes


def _dedupe_media(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    output: list[dict[str, str]] = []
    for item in items:
        url = item.get("url", "")
        if not url or url in seen:
            continue
        seen.add(url)
        output.append(item)
    return output


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _suffix_from_url(url: str) -> str:
    path = urlparse(url).path
    suffix = Path(path).suffix.lower()
    if re.fullmatch(r"\.[a-z0-9]{1,5}", suffix):
        return suffix
    return ".bin"


def _looks_like_video(url: str, content_type: str) -> bool:
    content_type = content_type.casefold()
    suffix = _suffix_from_url(url)
    return content_type.startswith("video/") or suffix in {".mp4", ".mov", ".m4v", ".webm"}


def _fxembed_enabled() -> bool:
    value = os.environ.get("FXEMBED_MEDIA_FALLBACK", "true").strip().casefold()
    return value in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default
