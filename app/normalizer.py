from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.models import ArticleCandidate, FeedEntry, TimestampResult, TimestampSettings

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "cmpid",
    "linkid",
}

UPDATE_PREFIX_RE = re.compile(r"^(updated?|breaking|live updates?)\s*:\s*", re.IGNORECASE)
OUTLET_SUFFIX_RE = re.compile(r"\s+[-|]\s+(cbs news|ap news|associated press|reuters|bbc news|cnn|fox news)$", re.IGNORECASE)
REPEATED_PUNCT_RE = re.compile(r"([!?.,])\1+")
WHITESPACE_RE = re.compile(r"\s+")
TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "of",
    "on",
    "or",
    "over",
    "the",
    "to",
    "with",
}
SOURCE_FAMILY_ALIASES = {
    "abc": ("abc",),
    "ap": ("ap", "associated press"),
    "bbc": ("bbc",),
    "bloomberg": ("bloomberg",),
    "cbs": ("cbs",),
    "cnbc": ("cnbc",),
    "defense news": ("defense news",),
    "fox": ("fox",),
    "guardian": ("guardian",),
    "miami herald": ("miami herald",),
    "nbc": ("nbc",),
    "nyt": ("nyt", "new york times"),
    "politico": ("politico",),
    "reuters": ("reuters",),
    "state department": ("state department",),
    "wapo": ("wapo", "washington post"),
    "wsj": ("wsj", "wall street journal"),
}


def stable_hash(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def normalize_feed_url(url: str) -> str:
    return _normalize_url(url, strip_tracking=True)


def normalize_article_url(url: str | None) -> str | None:
    if not url:
        return None
    return _normalize_url(html.unescape(url), strip_tracking=True)


def _normalize_url(url: str, strip_tracking: bool) -> str:
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or ""
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if strip_tracking and key.lower() in TRACKING_PARAMS:
            continue
        query_pairs.append((key, value))
    query = urlencode(query_pairs, doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def normalize_title(title: str | None) -> str:
    value = html.unescape(title or "").strip()
    value = value.replace("\u2018", "'").replace("\u2019", "'").replace("\u201c", '"').replace("\u201d", '"')
    value = UPDATE_PREFIX_RE.sub("", value)
    value = OUTLET_SUFFIX_RE.sub("", value)
    value = REPEATED_PUNCT_RE.sub(r"\1", value)
    value = WHITESPACE_RE.sub(" ", value)
    return value.lower().strip()


def title_signature(title: str | None) -> str:
    normalized = normalize_title(title)
    tokens = [token for token in TOKEN_RE.findall(normalized) if token not in STOPWORDS and len(token) > 1]
    return " ".join(tokens)


def source_family(source_name: str | None) -> str:
    normalized = normalize_title(source_name)
    normalized = normalized.replace(" via google news", "")
    normalized = normalized.replace(" top stories", "")
    normalized = normalized.replace(" main", "")
    normalized = normalized.replace(" u.s.", "")
    normalized = normalized.replace(" us", "")
    normalized = WHITESPACE_RE.sub(" ", normalized).strip()
    for family, aliases in SOURCE_FAMILY_ALIASES.items():
        if any(alias in normalized for alias in aliases):
            return family
    return normalized


def parse_timestamp(raw_value: str | None, ingested_at: datetime, settings: TimestampSettings) -> TimestampResult:
    ingested_utc = ensure_utc(ingested_at)
    if not raw_value:
        return TimestampResult(None, ingested_utc, ingested_utc, "missing")

    parsed: datetime | None = None
    status = "valid"
    try:
        parsed = parsedate_to_datetime(raw_value)
    except (TypeError, ValueError, IndexError, OverflowError):
        try:
            parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return TimestampResult(raw_value, ingested_utc, ingested_utc, "invalid")

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
        status = "timezone_corrected"
    normalized = parsed.astimezone(UTC)
    future_limit = ingested_utc + timedelta(minutes=settings.allowed_future_skew_minutes)
    if normalized > future_limit:
        return TimestampResult(raw_value, ingested_utc, ingested_utc, "future_corrected")
    return TimestampResult(raw_value, normalized, ingested_utc, status)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def isoformat(value: datetime) -> str:
    return ensure_utc(value).isoformat()


def build_candidate(entry: FeedEntry, timestamp_settings: TimestampSettings, now: datetime | None = None) -> ArticleCandidate:
    ingested_at = ensure_utc(now or datetime.now(UTC))
    timestamp = parse_timestamp(entry.raw_published_at, ingested_at, timestamp_settings)
    normalized_url = normalize_article_url(entry.raw_url)
    normalized_title_value = normalize_title(entry.raw_title)
    title_signature_value = title_signature(entry.raw_title)
    source_family_value = source_family(entry.feed_name)
    source_id_value = entry.source_id or source_family_value or "unknown"
    story_cluster_key = stable_hash(title_signature_value or normalized_title_value or entry.raw_title, 24)
    fingerprints: list[tuple[str, str]] = []

    if normalized_url:
        fingerprints.append(("normalized_url", normalized_url))
    if entry.raw_guid:
        fingerprints.append(("feed_guid", f"{entry.feed_key}:{entry.raw_guid.strip()}"))
    if normalized_title_value:
        source = source_id_value
        fingerprints.append(("title_source", f"{source}:{normalized_title_value}"))

    return ArticleCandidate(
        feed_key=entry.feed_key,
        source_name=entry.feed_name,
        source_id=source_id_value,
        source_class=entry.source_class or "unknown",
        title=html.unescape(entry.raw_title).strip() or "Untitled article",
        normalized_title=normalized_title_value,
        title_signature=title_signature_value,
        source_family=source_family_value,
        story_cluster_key=story_cluster_key,
        url=entry.raw_url,
        normalized_url=normalized_url,
        summary=html.unescape(entry.summary or "").strip() or None,
        image_url=entry.image_url,
        image_source=entry.image_source,
        raw_guid=entry.raw_guid,
        raw_published_at=timestamp.raw_published_at,
        normalized_published_at=timestamp.normalized_published_at,
        ingested_at=timestamp.ingested_at,
        timestamp_status=timestamp.timestamp_status,
        fingerprints=tuple(fingerprints),
    )
