from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
    assert entry.rich_metadata["social_url"] == "https://bsky.app/profile/reuters.com/post/abc"


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
    assert entry.rich_metadata["social_url"] == "https://bsky.app/profile/reuters.com/post/abc"


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
        rich_metadata={"social_url": "https://bsky.app/profile/cnn.com/post/abc"},
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
    assert enriched.rich_metadata["social_url"] == "https://bsky.app/profile/cnn.com/post/abc"


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


@pytest.mark.asyncio
async def test_state_public_schedule_collection_page_is_parsed() -> None:
    service = FeedService(timeout_seconds=10, max_entries_per_feed=15)
    session = FakeRssSession(
        b"""
        <html><body>
          <ul id="col_json_result" class="collection-results">
            <li class="collection-result">
              <p class="collection-result__date">Public Schedule</p>
              <a href="https://www.state.gov/releases/office-of-the-spokesperson/2026/06/public-schedule-june-5-2026/"
                 class="collection-result__link" target="_self">
                 Public Schedule &ndash; June 5, 2026
              </a>
              <div class="collection-result-meta" dir="ltr">
                <span dir="ltr">June 4, 2026</span>
              </div>
            </li>
          </ul>
        </body></html>
        """
    )

    result = await service.fetch(
        session,
        FeedRuntime(
            feed_key="state-public-schedule",
            display_name="State Department Public Schedule",
            url="https://www.state.gov/public-schedule/",
            normalized_url="https://www.state.gov/public-schedule",
            interval_seconds=300,
            channel_ids=("111111111111111111",),
            channel_keys=("north-america",),
            source_id="state-department-public-schedule",
            source_class="official_us_gov",
        ),
    )

    assert len(result.entries) == 1
    assert result.entries[0].raw_title == "Public Schedule \u2013 June 5, 2026"
    assert result.entries[0].raw_url == (
        "https://www.state.gov/releases/office-of-the-spokesperson/2026/06/public-schedule-june-5-2026/"
    )
    assert result.entries[0].raw_published_at == "04 Jun 2026 00:00 +0000"
    assert result.entries[0].source_id == "state-department-public-schedule"


@pytest.mark.asyncio
async def test_ical_feed_emits_only_upcoming_events() -> None:
    service = FeedService(timeout_seconds=10, max_entries_per_feed=15)
    future = datetime.now(UTC) + timedelta(days=1)
    old = datetime.now(UTC) - timedelta(days=10)
    session = FakeRssSession(
        f"""
BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:VIP Calendar
BEGIN:VEVENT
UID:future-event
DTSTART:{future.strftime('%Y%m%dT%H%M%SZ')}
DTSTAMP:{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}
SUMMARY:The President meets with allied leaders
LOCATION:The White House
DESCRIPTION:Open press
END:VEVENT
BEGIN:VEVENT
UID:old-event
DTSTART:{old.strftime('%Y%m%dT%H%M%SZ')}
DTSTAMP:{old.strftime('%Y%m%dT%H%M%SZ')}
SUMMARY:Old schedule event
END:VEVENT
END:VCALENDAR
        """.encode()
    )

    result = await service.fetch(
        session,
        FeedRuntime(
            feed_key="factbase-white-house-calendar",
            display_name="Factba.se White House Calendar",
            url="https://calendar.google.com/calendar/ical/example/public/basic.ics",
            normalized_url="https://calendar.google.com/calendar/ical/example/public/basic.ics",
            interval_seconds=300,
            channel_ids=("111111111111111111",),
            channel_keys=("the-white-house",),
            source_id="factbase-white-house-calendar",
            source_class="official_us_gov",
        ),
    )

    assert len(result.entries) == 1
    assert result.entries[0].raw_guid.startswith("future-event:")
    assert result.entries[0].raw_title.startswith("Public Schedule: The President meets with allied leaders")
    assert result.entries[0].raw_url is None
    assert "Location: The White House" in (result.entries[0].summary or "")
    assert result.entries[0].source_id == "factbase-white-house-calendar"


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


class FakeDvidsChallengeResponse:
    status = 202
    headers = {"x-amzn-waf-action": "challenge"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class FakeDvidsChallengeSession:
    def get(self, *args, **kwargs):
        return FakeDvidsChallengeResponse()


class FakeRssResponse:
    status = 200
    headers = {}

    def __init__(self, body: bytes):
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def raise_for_status(self):
        return None

    async def read(self):
        return self.body


class FakeRssSession:
    def __init__(self, body: bytes):
        self.body = body
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeRssResponse(self.body)


class FakeJsonResponse:
    status = 200
    headers = {}

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


class FakeDvidsFallbackSession:
    def __init__(self):
        self.calls = []

    def get(self, url, *args, **kwargs):
        self.calls.append((url, kwargs))
        if "api.dvidshub.net/search" in str(url):
            return FakeJsonResponse(
                {
                    "results": [
                        {
                            "publishdate": "2026-06-03T17:45:15Z",
                            "title": "USS Iwo Jima Conducts Flight Operations",
                            "id": "image:9722696",
                            "type": "image",
                            "unit_name": "USS Iwo Jima (LHD 7)",
                            "short_description": "An MH-60S Sea Hawk helicopter takes off.",
                            "thumbnail": "https://d1ldvf68ux039x.cloudfront.net/thumbs/photos/2606/9722696/800w_q95.jpg",
                            "url": "https://www.dvidshub.net/image/9722696/uss-iwo-jima-conducts-flight-operations",
                        }
                    ]
                }
            )
        return FakeDvidsChallengeResponse()


@pytest.mark.asyncio
async def test_fetch_reports_rate_limit_with_retry_after() -> None:
    service = FeedService(timeout_seconds=10, max_entries_per_feed=30)

    with pytest.raises(FeedFetchError, match="rate limited; retry after 900s"):
        await service.fetch(FakeSession(), bluesky_feed())


@pytest.mark.asyncio
async def test_fetch_uses_feed_timeout_override() -> None:
    service = FeedService(timeout_seconds=10, max_entries_per_feed=15)
    session = FakeRssSession(
        b"""
        <rss><channel>
          <item>
            <guid>1</guid>
            <title>Example</title>
            <link>https://example.com/story</link>
          </item>
        </channel></rss>
        """
    )

    await service.fetch(
        session,
        FeedRuntime(
            feed_key="slow-calendar",
            display_name="Slow Calendar",
            url="https://example.com/calendar.ics",
            normalized_url="https://example.com/calendar.ics",
            interval_seconds=300,
            channel_ids=("111111111111111111",),
            channel_keys=("the-white-house",),
            fetch_timeout_seconds=20,
        ),
    )

    assert session.calls[0][1]["timeout"].total == 20


@pytest.mark.asyncio
async def test_dvids_empty_waf_challenge_is_fetch_error(monkeypatch) -> None:
    monkeypatch.delenv("DVIDS_API_KEY", raising=False)
    service = FeedService(timeout_seconds=10, max_entries_per_feed=15)

    with pytest.raises(FeedFetchError, match="DVIDS returned empty HTTP 202 response; waf action=challenge"):
        await service.fetch(
            FakeDvidsChallengeSession(),
            FeedRuntime(
                feed_key="feed_dvids",
                display_name="CENTCOM DVIDS",
                url="https://www.dvidshub.net/rss/unit/72",
                normalized_url="https://www.dvidshub.net/rss/unit/72",
                interval_seconds=300,
                channel_ids=("111111111111111111",),
                channel_keys=("middle-east",),
            ),
        )


@pytest.mark.asyncio
async def test_dvids_fetch_reads_burst_beyond_global_entry_cap() -> None:
    service = FeedService(timeout_seconds=10, max_entries_per_feed=15)
    items = "\n".join(
        f"""
        <item>
          <guid>image:{index}</guid>
          <title>DVIDS item {index}</title>
          <link>https://www.dvidshub.net/image/{index}/example</link>
          <description>Example</description>
          <pubDate>Wed, 03 Jun 2026 12:{index:02d}:00 -0400</pubDate>
        </item>
        """
        for index in range(20)
    )
    body = f"""
    <rss version="2.0">
      <channel>
        <title>DVIDS Unit RSS Feed: CENTCOM</title>
        {items}
      </channel>
    </rss>
    """.encode()

    result = await service.fetch(
        FakeRssSession(body),
        FeedRuntime(
            feed_key="feed_dvids",
            display_name="CENTCOM DVIDS",
            url="https://www.dvidshub.net/rss/unit/72",
            normalized_url="https://www.dvidshub.net/rss/unit/72",
            interval_seconds=300,
            channel_ids=("111111111111111111",),
            channel_keys=("middle-east",),
        ),
    )

    assert len(result.entries) == 20


@pytest.mark.asyncio
async def test_dvids_waf_challenge_uses_api_fallback(monkeypatch) -> None:
    monkeypatch.setenv("DVIDS_API_KEY", "key-test")
    service = FeedService(timeout_seconds=10, max_entries_per_feed=15)
    session = FakeDvidsFallbackSession()

    result = await service.fetch(
        session,
        FeedRuntime(
            feed_key="feed_dvids",
            display_name="USS Iwo Jima DVIDS",
            url="https://www.dvidshub.net/rss/unit/4222",
            normalized_url="https://www.dvidshub.net/rss/unit/4222",
            interval_seconds=300,
            channel_ids=("111111111111111111",),
            channel_keys=("sea",),
            source_id="dvids",
            source_class="official_us_defense",
        ),
    )

    assert len(result.entries) == 1
    assert result.entries[0].raw_guid == "image:9722696"
    assert result.entries[0].raw_url == "https://www.dvidshub.net/image/9722696/uss-iwo-jima-conducts-flight-operations"
    assert result.entries[0].image_source == "dvids_api_thumbnail"
    assert result.entries[0].source_id == "dvids"
    assert session.calls[1][1]["params"]["unit_id"] == "4222"
    assert session.calls[1][1]["params"]["max_results"] == "50"
