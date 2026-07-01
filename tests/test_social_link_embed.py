from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.social_link_embed as social_link_embed
from app.models import AppConfig, BotSettings, DiscordSettings, FeedEntry, PostJob, Settings
from app.social_link_embed import SocialLinkEmbedService, extract_social_post_reference


class FakeDB:
    def __init__(self, *, duplicate: bool = False) -> None:
        self.duplicate = duplicate
        self.recorded = None

    def has_recent_social_link_embed(self, channel_id: str, platform: str, post_id: str, hours: int) -> bool:
        return self.duplicate

    def resolve_article(self, candidate, title_window_hours: int):
        return SimpleNamespace(article_id=42, is_new_article=True)

    def get_post_job(self, article_id: int, channel_id: str) -> PostJob:
        return PostJob(
            article_id=article_id,
            channel_id=channel_id,
            title="Social post",
            url="https://x.com/notwoofers/status/2066697461284978998",
            summary="Post body",
            image_url="https://example.com/image.jpg",
            image_source="x_media",
            source_name="X: @notwoofers",
            source_id="x-notwoofers",
            source_class="social_core",
            rich_metadata={"source": "x_message", "post_id": "2066697461284978998"},
            normalized_published_at=datetime(2026, 6, 15, tzinfo=UTC),
        )

    def record_social_link_embed(self, **kwargs) -> None:
        self.recorded = kwargs


class FakeSender:
    def __init__(self) -> None:
        self.job = None
        self.source_message = None

    async def send_social_reply(self, job: PostJob, source_message) -> str:
        self.job = job
        self.source_message = source_message
        return "999"


class FakeMessage:
    def __init__(self, *, content: str = "", embeds=None) -> None:
        self.id = 1516256421820240015
        self.content = content
        self.embeds = embeds or []
        self.author = SimpleNamespace(bot=False)
        self.webhook_id = None
        self.guild = object()
        self.channel = SimpleNamespace(id=1508935297298071552)
        self.suppressed = False

    async def edit(self, *, suppress: bool = False) -> None:
        self.suppressed = suppress


def _config() -> AppConfig:
    return AppConfig(
        version=1,
        bot=BotSettings(),
        discord=DiscordSettings(),
        settings=Settings(),
        feeds=(),
        channels=(),
        raw={},
    )


def _entry(platform: str = "x") -> FeedEntry:
    post_url = (
        "https://bsky.app/profile/example.com/post/abc"
        if platform == "bluesky"
        else "https://x.com/notwoofers/status/2066697461284978998"
    )
    return FeedEntry(
        feed_key=f"{platform}:post",
        feed_name="Bluesky: Example" if platform == "bluesky" else "X: @notwoofers",
        raw_guid=f"{platform}:post",
        raw_title="Social post",
        raw_url=post_url,
        summary="Post body",
        image_url="https://example.com/image.jpg",
        image_source=f"{platform}_image",
        raw_published_at="2026-06-15T00:00:00+00:00",
        parsed={"source": platform},
        source_id=f"{platform}-example",
        source_class="social_core",
        rich_metadata={"source": f"{platform}_message", "social_url": post_url, "post_id": "abc"},
    )


def test_extracts_x_reference_from_native_embed_url() -> None:
    reference = extract_social_post_reference("https://twitter.com/notwoofers/status/2066697461284978998?s=46")

    assert reference is not None
    assert reference.platform == "x"
    assert reference.post_id == "2066697461284978998"
    assert reference.url == "https://x.com/notwoofers/status/2066697461284978998"


def test_extracts_bluesky_reference() -> None:
    reference = extract_social_post_reference("https://bsky.app/profile/example.com/post/abc123")

    assert reference is not None
    assert reference.platform == "bluesky"
    assert reference.account == "example.com"
    assert reference.post_id == "abc123"


def test_x_entry_extracts_direct_video_and_thumbnail() -> None:
    entry = social_link_embed.feed_entry_from_fxtwitter_status(
        {
            "id": "2066697461284978998",
            "url": "https://x.com/notwoofers/status/2066697461284978998",
            "text": "Video post",
            "author": {"screen_name": "notwoofers", "name": "No Twoofers"},
            "media": {
                "videos": [
                    {
                        "url": "https://video.twimg.com/ext_tw_video/high.mp4",
                        "thumbnail_url": "https://pbs.twimg.com/ext_tw_video/thumb.jpg",
                        "type": "video",
                        "formats": [
                            {
                                "url": "https://video.twimg.com/ext_tw_video/low.mp4",
                                "container": "mp4",
                                "bitrate": 832000,
                            },
                            {
                                "url": "https://video.twimg.com/ext_tw_video/high.mp4",
                                "container": "mp4",
                                "bitrate": 10368000,
                            },
                        ],
                    }
                ]
            },
            "created_at": "2026-06-15T00:00:00+00:00",
        },
        social_link_embed.SocialPostReference(
            platform="x",
            account="notwoofers",
            post_id="2066697461284978998",
            url="https://x.com/notwoofers/status/2066697461284978998",
        ),
    )

    assert entry.video_url == "https://video.twimg.com/ext_tw_video/low.mp4"
    assert entry.video_source == "x_media"
    assert entry.image_url == "https://pbs.twimg.com/ext_tw_video/thumb.jpg"
    assert entry.rich_metadata["media_items"][0]["type"] == "video"


@pytest.mark.asyncio
async def test_handler_suppresses_native_x_embed_url_and_sends_reply(monkeypatch) -> None:
    async def fake_x_entry(session, reference):
        return _entry("x")

    monkeypatch.setattr(social_link_embed, "_x_entry_from_reference", fake_x_entry)
    db = FakeDB()
    sender = FakeSender()
    service = SocialLinkEmbedService(db, sender)
    service.configure(_config())
    message = FakeMessage(embeds=[SimpleNamespace(url="https://twitter.com/notwoofers/status/2066697461284978998?s=46")])

    await service.handle_message(message)

    assert message.suppressed is True
    assert sender.job is not None
    assert db.recorded["platform"] == "x"
    assert db.recorded["post_id"] == "2066697461284978998"
    assert db.recorded["source_message_id"] == "1516256421820240015"
    assert db.recorded["bot_message_id"] == "999"


@pytest.mark.asyncio
async def test_handler_suppresses_bluesky_and_sends_reply(monkeypatch) -> None:
    async def fake_bluesky_entry(session, reference):
        return _entry("bluesky")

    monkeypatch.setattr(social_link_embed, "_bluesky_entry_from_reference", fake_bluesky_entry)
    db = FakeDB()
    sender = FakeSender()
    service = SocialLinkEmbedService(db, sender)
    service.configure(_config())
    message = FakeMessage(content="https://bsky.app/profile/example.com/post/abc")

    await service.handle_message(message)

    assert message.suppressed is True
    assert sender.job is not None
    assert db.recorded["platform"] == "bluesky"
    assert db.recorded["post_id"] == "abc"


@pytest.mark.asyncio
async def test_handler_suppresses_duplicate_but_does_not_send_second_reply() -> None:
    db = FakeDB(duplicate=True)
    sender = FakeSender()
    service = SocialLinkEmbedService(db, sender)
    service.configure(_config())
    message = FakeMessage(content="https://x.com/notwoofers/status/2066697461284978998")

    await service.handle_message(message)

    assert message.suppressed is True
    assert sender.job is None
    assert db.recorded is None
