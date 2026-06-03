from __future__ import annotations

import pytest

from app.feed_fetcher import FeedFetchError, FeedService, clean_html_text, extract_bluesky_media
from app.models import FeedEntry, FeedRuntime


def bluesky_feed() -> FeedRuntime:
    return FeedRuntime(
        feed_key="feed_bsky",
        display_name="Bluesky: Reuters",
        url="https://bsky.app/profile/reuters.com/rss",
        normalized_url="https://bsky.app/profile/reuters.com/rss",
        interval_seconds=300,
        channel_ids=("111111111111111111",),
        channel_keys=("reuters",),
    )


def test_bluesky_entry_uses_post_text_title_and_external_link() -> None:
    service = FeedService(timeout_seconds=10, max_entries_per_feed=30)
    entry = service._entry_from_parsed(
        bluesky_feed(),
        {
            "id": "at://did:example/app.bsky.feed.post/abc",
            "link": "https://bsky.app/profile/reuters.com/post/abc",
            "summary": "Market story moves quickly reut.rs/abc\nhttps://reut.rs/abc",
            "published": "02 Jun 2026 00:50 +0000",
        },
    )

    assert entry.raw_title == "Market story moves quickly"
    assert entry.raw_url == "https://reut.rs/abc"
    assert entry.summary == "Market story moves quickly reut.rs/abc\nhttps://reut.rs/abc"


def test_bluesky_entry_falls_back_to_bluesky_post_link() -> None:
    service = FeedService(timeout_seconds=10, max_entries_per_feed=30)
    entry = service._entry_from_parsed(
        bluesky_feed(),
        {
            "id": "at://did:example/app.bsky.feed.post/abc",
            "link": "https://bsky.app/profile/reuters.com/post/abc",
            "summary": "Plain post with no external link",
            "published": "02 Jun 2026 00:50 +0000",
        },
    )

    assert entry.raw_title == "Plain post with no external link"
    assert entry.raw_url == "https://bsky.app/profile/reuters.com/post/abc"


def test_bluesky_external_card_media_is_extracted() -> None:
    media = extract_bluesky_media(
        {
            "thread": {
                "post": {
                    "embed": {
                        "$type": "app.bsky.embed.external#view",
                        "external": {
                            "uri": "https://www.cnn.com/story?utm_source=bluesky",
                            "thumb": "https://cdn.bsky.app/img/feed_thumbnail/plain/did/bafk",
                        },
                    }
                }
            }
        }
    )

    assert media == (
        "https://www.cnn.com/story?utm_source=bluesky",
        "https://cdn.bsky.app/img/feed_thumbnail/plain/did/bafk",
        "bluesky_external_thumb",
    )


def test_bluesky_native_image_prefers_fullsize() -> None:
    media = extract_bluesky_media(
        {
            "thread": {
                "post": {
                    "embed": {
                        "$type": "app.bsky.embed.images#view",
                        "images": [
                            {
                                "thumb": "https://cdn.bsky.app/img/feed_thumbnail/plain/did/thumb",
                                "fullsize": "https://cdn.bsky.app/img/feed_fullsize/plain/did/full",
                            }
                        ],
                    }
                }
            }
        }
    )

    assert media == (None, "https://cdn.bsky.app/img/feed_fullsize/plain/did/full", "bluesky_image")


def test_bluesky_record_with_media_uses_media_first() -> None:
    media = extract_bluesky_media(
        {
            "thread": {
                "post": {
                    "embed": {
                        "$type": "app.bsky.embed.recordWithMedia#view",
                        "media": {
                            "$type": "app.bsky.embed.external#view",
                            "external": {
                                "uri": "https://www.youtube.com/watch?v=abc123",
                                "thumb": "https://cdn.bsky.app/img/feed_thumbnail/plain/did/video",
                            },
                        },
                        "record": {
                            "record": {
                                "value": {
                                    "embed": {
                                        "$type": "app.bsky.embed.images#view",
                                        "images": [{"fullsize": "https://cdn.bsky.app/quoted"}],
                                    }
                                }
                            }
                        },
                    }
                }
            }
        }
    )

    assert media == (
        "https://www.youtube.com/watch?v=abc123",
        "https://cdn.bsky.app/img/feed_thumbnail/plain/did/video",
        "bluesky_video_thumb",
    )


class FakeBlueskyMediaResponse:
    status = 200

    def __init__(self, payload):
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        return None

    async def json(self):
        return self.payload


class FakeBlueskyMediaSession:
    def __init__(self, payload):
        self.payload = payload

    def get(self, *args, **kwargs):
        return FakeBlueskyMediaResponse(self.payload)


class FailingBlueskyMediaSession:
    def get(self, *args, **kwargs):
        raise TimeoutError("media timeout")


@pytest.mark.asyncio
async def test_bluesky_media_lookup_updates_entry_url_and_image() -> None:
    service = FeedService(timeout_seconds=10, max_entries_per_feed=30)
    entry = FeedEntry(
        feed_key="feed_bsky",
        feed_name="Bluesky: CNN",
        raw_guid="at://did:example/app.bsky.feed.post/abc",
        raw_title="CNN post",
        raw_url="https://cnn.it/abc",
        summary="CNN post https://cnn.it/abc",
        image_url=None,
        image_source=None,
        raw_published_at=None,
        parsed={},
    )

    enriched = await service._enrich_bluesky_entry(
        FakeBlueskyMediaSession(
            {
                "thread": {
                    "post": {
                        "embed": {
                            "$type": "app.bsky.embed.external#view",
                            "external": {
                                "uri": "https://www.cnn.com/full-story",
                                "thumb": "https://cdn.bsky.app/img/feed_thumbnail/plain/did/thumb",
                            },
                        }
                    }
                }
            }
        ),
        entry,
    )

    assert enriched.raw_url == "https://www.cnn.com/full-story"
    assert enriched.image_url == "https://cdn.bsky.app/img/feed_thumbnail/plain/did/thumb"
    assert enriched.image_source == "bluesky_external_thumb"


@pytest.mark.asyncio
async def test_bluesky_media_lookup_failure_keeps_original_entry() -> None:
    service = FeedService(timeout_seconds=10, max_entries_per_feed=30)
    entry = FeedEntry(
        feed_key="feed_bsky",
        feed_name="Bluesky: CNN",
        raw_guid="at://did:example/app.bsky.feed.post/abc",
        raw_title="CNN post",
        raw_url="https://cnn.it/abc",
        summary="CNN post https://cnn.it/abc",
        image_url=None,
        image_source=None,
        raw_published_at=None,
        parsed={},
    )

    enriched = await service._enrich_bluesky_entry(FailingBlueskyMediaSession(), entry)

    assert enriched == entry


def test_html_summary_is_cleaned_for_discord_embeds() -> None:
    service = FeedService(timeout_seconds=10, max_entries_per_feed=30)
    entry = service._entry_from_parsed(
        FeedRuntime(
            feed_key="feed_toi",
            display_name="Times of Israel",
            url="https://example.com/rss",
            normalized_url="https://example.com/rss",
            interval_seconds=300,
            channel_ids=("111111111111111111",),
            channel_keys=("middle-east",),
        ),
        {
            "title": "New York leaders decry Smotrich’s participation in NYC Israel parade",
            "link": "https://www.timesofisrael.com/example",
            "summary": (
                "<p>State's governor <strong>strongly condemns</strong> appearance</p>"
                "<p>The post <a href=\"https://example.com\">New York leaders</a> appeared first on "
                "<a href=\"https://www.timesofisrael.com\">The Times of Israel</a>.</p>"
                "<figure><img src=\"https://example.com/image.jpg\" /></figure>"
            ),
            "published": "02 Jun 2026 20:16 +0000",
        },
    )

    assert entry.summary == "State's governor strongly condemns appearance"
    assert "<p>" not in entry.summary
    assert "appeared first on" not in entry.summary
    assert "image.jpg" not in entry.summary


def test_dvids_media_thumbnail_is_extracted() -> None:
    service = FeedService(timeout_seconds=10, max_entries_per_feed=30)
    entry = service._entry_from_parsed(
        FeedRuntime(
            feed_key="feed_dvids",
            display_name="CENTCOM DVIDS",
            url="https://www.dvidshub.net/rss/unit/72",
            normalized_url="https://www.dvidshub.net/rss/unit/72",
            interval_seconds=300,
            channel_ids=("111111111111111111",),
            channel_keys=("middle-east",),
        ),
        {
            "id": "image:9709381",
            "title": "USS Tripoli Rappel Training [Image 2 of 5]",
            "link": "https://www.dvidshub.net/image/9709381/uss-tripoli-rappel-training",
            "summary": (
                "U.S. Navy photo<br />"
                '<a href="https://www.dvidshub.net/image/9709381/uss-tripoli-rappel-training">'
                '<img alt="USS Tripoli Rappel Training" '
                'src="https://d1ldvf68ux039x.cloudfront.net/thumbs/photos/2605/9709381/250w_q95.jpg" />'
                "</a>"
            ),
            "media_thumbnail": [
                {"url": "https://d1ldvf68ux039x.cloudfront.net/thumbs/photos/2605/9709381/250w_q95.jpg"}
            ],
            "published": "02 Jun 2026 20:16 +0000",
        },
    )

    assert entry.image_url == "https://d1ldvf68ux039x.cloudfront.net/thumbs/photos/2605/9709381/250w_q95.jpg"
    assert entry.image_source == "media_thumbnail"


def test_generic_image_enclosure_is_extracted() -> None:
    service = FeedService(timeout_seconds=10, max_entries_per_feed=30)
    entry = service._entry_from_parsed(
        FeedRuntime(
            feed_key="feed_generic",
            display_name="Generic Feed",
            url="https://example.com/rss",
            normalized_url="https://example.com/rss",
            interval_seconds=300,
            channel_ids=("111111111111111111",),
            channel_keys=("news",),
        ),
        {
            "title": "Story",
            "link": "https://example.com/story",
            "summary": "Summary",
            "enclosures": [{"href": "https://cdn.example.com/story.webp", "type": "image/webp"}],
        },
    )

    assert entry.image_url == "https://cdn.example.com/story.webp"
    assert entry.image_source == "enclosure"


def test_summary_img_fallback_ignores_invalid_urls() -> None:
    service = FeedService(timeout_seconds=10, max_entries_per_feed=30)
    entry = service._entry_from_parsed(
        FeedRuntime(
            feed_key="feed_generic",
            display_name="Generic Feed",
            url="https://example.com/rss",
            normalized_url="https://example.com/rss",
            interval_seconds=300,
            channel_ids=("111111111111111111",),
            channel_keys=("news",),
        ),
        {
            "title": "Story",
            "link": "https://example.com/story",
            "summary": '<p>Summary</p><img src="data:image/png;base64,abc" /><img src="/images/story.jpg" />',
        },
    )

    assert entry.image_url == "https://example.com/images/story.jpg"
    assert entry.image_source == "html_img"


def test_long_guardian_html_summary_keeps_plain_text() -> None:
    cleaned = clean_html_text(
        "<p>Budanov backs Zelenskyy call to capitalise on Kyiv’s strong position.</p>"
        "<p>A deal to <strong>end the war against Russia by winter is a “realistic” outcome</strong>.</p>"
        "<p>A suspected <strong>Russian “shadow fleet”</strong> oil tanker has been "
        '<a href="https://www.theguardian.com/world/example">detained by France</a>.</p>'
    )

    assert cleaned == (
        "Budanov backs Zelenskyy call to capitalise on Kyiv’s strong position.\n"
        "A deal to end the war against Russia by winter is a “realistic” outcome.\n"
        "A suspected Russian “shadow fleet” oil tanker has been detained by France."
    )


class FakeResponse:
    status = 429
    headers = {"Retry-After": "900"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeSession:
    def get(self, *args, **kwargs):
        return FakeResponse()


@pytest.mark.asyncio
async def test_fetch_reports_rate_limit_with_retry_after() -> None:
    service = FeedService(timeout_seconds=10, max_entries_per_feed=30)

    with pytest.raises(FeedFetchError, match="rate limited; retry after 900s"):
        await service.fetch(FakeSession(), bluesky_feed())
