from __future__ import annotations

import json
import re
from collections import Counter
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from app.models import PostJob


STOPWORDS = {
    "about",
    "after",
    "again",
    "against",
    "amid",
    "and",
    "are",
    "but",
    "for",
    "from",
    "has",
    "have",
    "into",
    "its",
    "new",
    "not",
    "over",
    "says",
    "that",
    "the",
    "their",
    "this",
    "with",
}


def build_codex_draft_payload(
    *,
    job: PostJob,
    routing: dict[str, Any] | None,
    related_articles: list[dict[str, Any]] | None = None,
    request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "request": request or {},
        "article": {
            "id": job.article_id,
            "title": job.title,
            "url": job.url,
            "summary": job.summary,
            "source_name": job.source_name,
            "source_id": job.source_id,
            "source_class": job.source_class,
            "normalized_published_at": job.normalized_published_at.isoformat(),
            "timestamp_status": job.timestamp_status,
            "image_url": job.image_url,
            "image_source": job.image_source,
            "video_url": job.video_url,
            "video_source": job.video_source,
            "rich_metadata": job.rich_metadata,
            "importance_score": job.importance_score,
            "importance_reasons": list(job.importance_reasons),
        },
        "routing": routing or {},
        "related_articles": related_articles or [],
        "media_options": media_options_for_job(job),
    }


def media_options_for_job(job: PostJob) -> list[dict[str, Any]]:
    metadata = job.rich_metadata or {}
    items: list[dict[str, str]] = []
    metadata_items = metadata.get("media_items")
    if isinstance(metadata_items, list):
        for raw in metadata_items:
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("url") or raw.get("media_url") or "").strip()
            if not url:
                continue
            items.append(
                {
                    "type": str(raw.get("type") or "image"),
                    "url": url,
                    "source": str(raw.get("source") or ""),
                }
            )
    if job.video_url:
        items.append({"type": "video", "url": job.video_url, "source": job.video_source or ""})
    if job.image_url:
        items.append({"type": "image", "url": job.image_url, "source": job.image_source or ""})

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        url = item["url"]
        if url in seen:
            continue
        seen.add(url)
        rights = media_rights_label(job, url)
        output.append(
            {
                "type": normalized_media_type(item["type"]),
                "media_url": url,
                "source_url": media_source_url(job),
                "source": item.get("source") or job.source_name,
                "attribution": media_attribution(job, item["type"]),
                "rights_label": rights,
                "recommended": rights in {"safe", "likely usable with attribution"},
                "alt_text_seed": alt_text_seed(job),
            }
        )
    return output


def media_rights_label(job: PostJob, media_url: str) -> str:
    source_name = (job.source_name or "").casefold()
    source_class = (job.source_class or "").casefold()
    url = media_url.casefold()
    if "dvidshub.net" in url or "dvids" in source_name:
        return "safe"
    if source_class.startswith("official_") or any(
        token in source_name
        for token in ("centcom", "nato", "u.s. navy", "u.s. army", "department of defense")
    ):
        return "likely usable with attribution"
    if job.source_name.startswith(("X:", "Bluesky:")) or source_class.startswith("social_"):
        return "likely usable with attribution"
    if source_class in {"wire_service", "major_media", "defense_media"}:
        return "preview only"
    return "verify before reposting"


def media_attribution(job: PostJob, media_type: str) -> str:
    label = "Video" if normalized_media_type(media_type) == "video" else "Image"
    if "dvids" in job.source_name.casefold():
        return f"{label}: {job.source_name} / DVIDS"
    if job.source_name.startswith("X:"):
        return f"{label}: {job.source_name[2:].strip()} via X"
    if job.source_name.startswith("Bluesky:"):
        return f"{label}: {job.source_name[len('Bluesky:'):].strip()} via Bluesky"
    return f"{label}: {job.source_name}"


def media_source_url(job: PostJob) -> str | None:
    metadata = job.rich_metadata or {}
    for key in ("social_url", "x_post_url", "bluesky_post_url"):
        value = metadata.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return job.url


def alt_text_seed(job: PostJob) -> str:
    title = " ".join(job.title.split())
    if job.source_name:
        return f"{title} ({job.source_name})"[:240]
    return title[:240]


def normalized_media_type(media_type: str) -> str:
    value = media_type.casefold()
    if value in {"video", "animated_gif", "gif"}:
        return "video"
    return "image"


def related_article_candidates(target: PostJob, rows: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    target_terms = keywords(f"{target.title} {target.summary or ''}")
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for row in rows:
        if int(row.get("id") or 0) == target.article_id:
            continue
        text = f"{row.get('title') or ''} {row.get('summary') or ''}"
        overlap = target_terms & keywords(text)
        if len(overlap) < 2:
            continue
        scored.append((len(overlap), str(row.get("normalized_published_at") or ""), compact_article(row)))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [item[2] for item in scored[:limit]]


def compact_article(row: dict[str, Any]) -> dict[str, Any]:
    summary = row.get("summary")
    if isinstance(summary, str) and len(summary) > 320:
        row = dict(row)
        row["summary"] = summary[:317].rstrip() + "..."
    return row


def keywords(text: str) -> set[str]:
    tokens = re.findall(r"[a-z0-9][a-z0-9'-]{2,}", text.casefold())
    counts = Counter(token for token in tokens if token not in STOPWORDS)
    return {term for term, _count in counts.most_common(12)}


def json_row_value(value: Any) -> Any:
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def discord_message_url(guild_id: int | str | None, channel_id: int | str | None, message_id: int | str | None) -> str | None:
    if not guild_id or not channel_id or not message_id:
        return None
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def host_from_url(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlparse(value)
    return parsed.netloc.casefold() or None
