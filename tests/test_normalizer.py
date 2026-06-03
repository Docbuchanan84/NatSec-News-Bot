from __future__ import annotations

from datetime import UTC, datetime

from app.models import FeedEntry, TimestampSettings
from app.normalizer import build_candidate, normalize_article_url, normalize_feed_url, normalize_title, parse_timestamp


def test_url_normalization_strips_tracking_and_fragment() -> None:
    url = "https://www.CBSNews.com/news/example-story/?utm_source=x&keep=1#section"
    assert normalize_article_url(url) == "https://www.cbsnews.com/news/example-story?keep=1"


def test_feed_url_normalization_dedupes_trailing_slash() -> None:
    assert normalize_feed_url("HTTPS://Example.com/rss/") == "https://example.com/rss"


def test_title_normalization_removes_update_prefix_and_suffix() -> None:
    assert normalize_title("Updated: Pentagon announces deployment - CBS News") == "pentagon announces deployment"


def test_future_timestamp_is_corrected_to_ingest_time() -> None:
    ingested = datetime(2026, 5, 28, 23, 30, tzinfo=UTC)
    result = parse_timestamp("Fri, 29 May 2026 13:00:00 GMT", ingested, TimestampSettings())
    assert result.timestamp_status == "future_corrected"
    assert result.normalized_published_at == ingested


def test_candidate_generates_guid_and_url_fingerprints() -> None:
    entry = FeedEntry(
        feed_key="feed_1",
        feed_name="CBS World",
        raw_guid="abc",
        raw_title="Updated: Test - CBS News",
        raw_url="https://example.com/a?utm_campaign=x",
        summary="summary",
        image_url="https://example.com/image.jpg",
        image_source="media_thumbnail",
        raw_published_at=None,
        parsed={},
    )
    candidate = build_candidate(entry, TimestampSettings(), now=datetime(2026, 5, 28, tzinfo=UTC))
    assert ("feed_guid", "feed_1:abc") in candidate.fingerprints
    assert ("normalized_url", "https://example.com/a") in candidate.fingerprints
    assert candidate.timestamp_status == "missing"
    assert candidate.image_url == "https://example.com/image.jpg"
    assert candidate.image_source == "media_thumbnail"
