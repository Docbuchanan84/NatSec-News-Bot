from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from pathlib import Path
import tempfile

import pytest

import app.discord_bot as discord_bot
from app.discord_bot import DiscordPublisherAdapter, _format_importance_terms, _importance_color
from app.routing.importance import ImportanceTerm
from app.models import PostJob
from app.x_media import PreparedMedia


class FakeMessage:
    id = 12345


class FakeChannel:
    def __init__(self) -> None:
        self.embed = None
        self.embeds = None
        self.content = None
        self.files = None
        self.suppress_embeds = None

    async def send(self, content=None, *, embed=None, embeds=None, files=None, suppress_embeds=False):
        self.content = content
        self.embed = embed
        self.embeds = embeds
        self.files = files
        self.suppress_embeds = suppress_embeds
        return FakeMessage()


class FakeClient:
    debug_mode_enabled = False

    def __init__(self, channel: FakeChannel) -> None:
        self.channel = channel

    def get_channel(self, channel_id: int):
        return self.channel


def patch_remote_media_upload(monkeypatch) -> None:
    @contextlib.asynccontextmanager
    async def fake_prepared_remote_media_files(media_items, **kwargs):
        with tempfile.TemporaryDirectory() as temp_name:
            prepared = []
            for index, item in enumerate(media_items, start=1):
                media_type = "video" if item.get("type") == "video" else "photo"
                suffix = ".mp4" if media_type == "video" else ".jpg"
                path = Path(temp_name) / f"media-{index}{suffix}"
                path.write_bytes(b"fake-media")
                prepared.append(PreparedMedia(path, path.name, item["url"], media_type))
            yield prepared

    @contextlib.asynccontextmanager
    async def fake_prepared_x_media_files(metadata, **kwargs):
        yield []

    monkeypatch.setattr(discord_bot, "prepared_remote_media_files", fake_prepared_remote_media_files)
    monkeypatch.setattr(discord_bot, "prepared_x_media_files", fake_prepared_x_media_files)


@pytest.mark.asyncio
async def test_discord_embed_sets_image_when_post_job_has_image(monkeypatch) -> None:
    patch_remote_media_upload(monkeypatch)
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="DVIDS photo story",
        url="https://www.dvidshub.net/image/1/story",
        summary="Caption",
        image_url="https://d1ldvf68ux039x.cloudfront.net/thumbs/photos/2605/1/250w_q95.jpg",
        image_source="media_thumbnail",
        source_name="DVIDS",
        normalized_published_at=datetime(2026, 6, 2, tzinfo=UTC),
    )

    message_id = await adapter.send(job)

    assert message_id == "12345"
    assert channel.content is None
    assert len(channel.files) == 1
    assert channel.files[0].filename == "media-1.jpg"
    assert channel.embed.image.url is None


@pytest.mark.asyncio
async def test_discord_embed_footer_uses_local_timestamp_and_new_article_state() -> None:
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    published_at = datetime(2026, 6, 2, 12, 30, tzinfo=UTC)
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="Regional talks resume",
        url="https://example.com/story",
        summary="Officials said talks resumed.",
        image_url=None,
        image_source=None,
        source_name="Example",
        normalized_published_at=published_at,
        importance_score=7,
    )

    await adapter.send(job)

    assert channel.embed.footer.text == "Example · New · Imp 7"
    assert channel.embed.timestamp == published_at
    assert channel.embed.color.value == 0xF1C40F


@pytest.mark.asyncio
async def test_discord_embed_footer_marks_update_posts() -> None:
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    updated_at = datetime(2026, 6, 2, 13, 45, tzinfo=UTC)
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="Regional talks resume",
        url="https://example.com/story",
        summary="Officials said talks resumed.",
        image_url=None,
        image_source=None,
        source_name="Example",
        normalized_published_at=updated_at,
        is_new_article=False,
        importance_score=3,
    )

    await adapter.send(job)

    assert channel.embed.footer.text == "Example · Update · Imp 3"
    assert channel.embed.timestamp == updated_at
    assert channel.embed.color.value == 0x2ECC71


def test_importance_color_stop_points() -> None:
    assert _importance_color(0) == 0x808080
    assert _importance_color(3) == 0x2ECC71
    assert _importance_color(7) == 0xF1C40F
    assert _importance_color(10) == 0xE74C3C


def test_format_importance_terms_lists_active_terms() -> None:
    text = _format_importance_terms(
        (
            ImportanceTerm("sunk", 4, "major_event"),
            ImportanceTerm("urgent", 1, "urgency"),
        )
    )

    assert "Active importance watch terms:" in text
    assert "- sunk: +4 (major_event)" in text
    assert "- urgent: +1 (urgency)" in text


@pytest.mark.asyncio
async def test_discord_embed_omits_description_when_title_and_summary_match() -> None:
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="Same story title",
        url="https://example.com/story",
        summary="  Same story title  ",
        image_url=None,
        image_source=None,
        source_name="Example",
        normalized_published_at=datetime(2026, 6, 2, tzinfo=UTC),
    )

    await adapter.send(job)

    assert channel.embed.title == "Same story title"
    assert channel.embed.description is None


@pytest.mark.asyncio
async def test_discord_embed_omits_description_when_summary_is_title_plus_link() -> None:
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="Oil market could be underpricing risks, Vitol's Bahrain chief says",
        url="https://example.com/story",
        summary=(
            "Oil market could be underpricing risks, Vitol's Bahrain chief says reut.rs/49xtVcz\n"
            "https://reut.rs/49xtVcz"
        ),
        image_url=None,
        image_source=None,
        source_name="Reuters",
        normalized_published_at=datetime(2026, 6, 2, tzinfo=UTC),
    )

    await adapter.send(job)

    assert channel.embed.description is None


@pytest.mark.asyncio
async def test_discord_embed_keeps_non_redundant_summary_remainder() -> None:
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="Regional talks resume",
        url="https://example.com/story",
        summary="Regional talks resume\nOfficials said negotiations would continue through Friday.",
        image_url=None,
        image_source=None,
        source_name="Example",
        normalized_published_at=datetime(2026, 6, 2, tzinfo=UTC),
    )

    await adapter.send(job)

    assert channel.embed.description == "Officials said negotiations would continue through Friday."


@pytest.mark.asyncio
async def test_discord_embed_sends_youtube_url_as_content_for_native_preview() -> None:
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="Defense update",
        url="https://www.youtube.com/watch?v=abc123",
        summary="Video summary",
        image_url="https://i.ytimg.com/vi/abc123/hqdefault.jpg",
        image_source="media_thumbnail",
        source_name="Perun YouTube",
        normalized_published_at=datetime(2026, 6, 2, tzinfo=UTC),
    )

    await adapter.send(job)

    assert channel.content == "https://www.youtube.com/watch?v=abc123"


@pytest.mark.asyncio
async def test_discord_embed_sends_direct_video_above_embed_without_duplicate_thumbnail(monkeypatch) -> None:
    patch_remote_media_upload(monkeypatch)
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="Video update",
        url="https://example.com/story",
        summary="Video summary",
        image_url="https://cdn.example.com/story.jpg",
        image_source="media_thumbnail",
        video_url="https://cdn.example.com/story.mp4",
        video_source="enclosure",
        source_name="Example",
        normalized_published_at=datetime(2026, 6, 2, tzinfo=UTC),
    )

    await adapter.send(job)

    assert channel.content is None
    assert len(channel.files) == 1
    assert channel.files[0].filename == "media-1.mp4"
    assert channel.embed.image.url is None


@pytest.mark.asyncio
async def test_discord_embed_removes_youtube_marketing_tail() -> None:
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="How high will temperatures get this week?",
        url="https://www.youtube.com/watch?v=abc123",
        summary=(
            "Temperatures are forecast to rise as high as 39C this week.\n\n"
            "Live updates: https://news.sky.com/story/heatwave-latest\n\n"
            "#skynews #weather #heatwave #uknews\n\n"
            "SUBSCRIBE to our YouTube channel for more videos: http://www.youtube.com/skynews\n"
            "Follow us on Twitter: https://twitter.com/skynews\n"
            "Like us on Facebook: https://www.facebook.com/skynews\n"
            "For more content go to http://news.sky.com and download our apps: Apple "
            "https://itunes.apple.com/gb/app/sky-news/id316391924?mt=8 Android\n"
            "https://play.google.com/store/apps/details?id=com.bskyb.skynews.android&hl=en_GB\n"
            "To enquire about licensing Sky News content, you can find more information here: "
            "https://news.sky.com/info/library-sales"
        ),
        image_url=None,
        image_source=None,
        source_name="Sky News YouTube",
        normalized_published_at=datetime(2026, 6, 2, tzinfo=UTC),
    )

    await adapter.send(job)

    assert channel.embed.description == (
        "Temperatures are forecast to rise as high as 39C this week.\n\n"
        "Live updates: https://news.sky.com/story/heatwave-latest"
    )


@pytest.mark.asyncio
async def test_discord_embed_omits_youtube_description_when_only_marketing() -> None:
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="Iran and Oman reaffirm sovereign rights in Strait of Hormuz",
        url="https://www.youtube.com/watch?v=abc123",
        summary=(
            "Subscribe to our channel: http://bit.ly/AJSubscribe\n"
            "Follow us on X : https://twitter.com/AJEnglish\n"
            "Find us on Facebook: https://www.facebook.com/aljazeera\n"
            "Check our website: http://www.aljazeera.com/\n"
            "Check out our Instagram page: https://www.instagram.com/aljazeeraenglish/\n"
            "Download AJE Mobile App: https://aje.news/AJEMobile"
        ),
        image_url=None,
        image_source=None,
        source_name="Al Jazeera English YouTube",
        normalized_published_at=datetime(2026, 6, 2, tzinfo=UTC),
    )

    await adapter.send(job)

    assert channel.embed.description is None


@pytest.mark.asyncio
async def test_discord_embed_formats_bluesky_post_without_native_preview(monkeypatch) -> None:
    patch_remote_media_upload(monkeypatch)
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="Reuters post",
        url="https://www.reuters.com/world/story",
        summary="Story summary",
        image_url="https://cdn.bsky.app/img/feed_fullsize/plain/did/full",
        image_source="bluesky_image",
        source_name="Bluesky: Reuters",
        normalized_published_at=datetime(2026, 6, 2, tzinfo=UTC),
        rich_metadata={"social_url": "https://bsky.app/profile/reuters.com/post/abc"},
    )

    await adapter.send(job)

    assert channel.content is None
    assert channel.suppress_embeds is False
    assert channel.embeds is None
    assert len(channel.files) == 1
    assert channel.files[0].filename == "media-1.jpg"
    assert channel.embed.image.url is None
    assert channel.embed.title == "Reuters"
    assert channel.embed.description == "Story summary"
    assert channel.embed.url == "https://bsky.app/profile/reuters.com/post/abc"


@pytest.mark.asyncio
async def test_discord_embed_formats_x_post_without_native_preview(monkeypatch) -> None:
    patch_remote_media_upload(monkeypatch)
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="Drone footage from the front",
        url="https://x.com/example/status/1234567890",
        summary="Drone footage from the front\nhttps://example.com/report",
        image_url="https://pbs.twimg.com/media/example.jpg",
        image_source="x_media",
        source_name="X: @example",
        source_id="x-example",
        source_class="social_core",
        normalized_published_at=datetime(2026, 6, 2, tzinfo=UTC),
        rich_metadata={"social_url": "https://x.com/example/status/1234567890"},
    )

    await adapter.send(job)

    assert channel.content is None
    assert channel.suppress_embeds is False
    assert channel.embeds is None
    assert len(channel.files) == 1
    assert channel.files[0].filename == "media-1.jpg"
    assert channel.embed.image.url is None
    assert channel.embed.title == "@example"
    assert channel.embed.description == "Drone footage from the front\nhttps://example.com/report"
    assert channel.embed.url == "https://x.com/example/status/1234567890"


@pytest.mark.asyncio
async def test_discord_embed_uploads_all_metadata_images(monkeypatch) -> None:
    patch_remote_media_upload(monkeypatch)
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="Photo set",
        url="https://example.com/story",
        summary="Photo set summary",
        image_url="https://cdn.example.com/one.jpg",
        image_source="media_thumbnail",
        source_name="Example",
        normalized_published_at=datetime(2026, 6, 2, tzinfo=UTC),
        rich_metadata={
            "media_items": [
                {"type": "image", "url": "https://cdn.example.com/one.jpg"},
                {"type": "image", "url": "https://cdn.example.com/two.jpg"},
                {"type": "image", "url": "https://cdn.example.com/three.jpg"},
            ]
        },
    )

    await adapter.send(job)

    assert channel.content is None
    assert [file.filename for file in channel.files] == ["media-1.jpg", "media-2.jpg", "media-3.jpg"]
    assert channel.embed.image.url is None


@pytest.mark.asyncio
async def test_discord_embed_formats_x_video_as_playable_content(monkeypatch) -> None:
    patch_remote_media_upload(monkeypatch)
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="Flight video",
        url="https://x.com/example/status/1234567890",
        summary="Flight video",
        image_url="https://pbs.twimg.com/media/example.jpg",
        image_source="x_media",
        video_url="https://video.twimg.com/ext_tw_video/example/vid/avc1/640x360/story.mp4",
        video_source="x_media",
        source_name="X: @example",
        source_id="x-example",
        source_class="social_core",
        normalized_published_at=datetime(2026, 6, 2, tzinfo=UTC),
        rich_metadata={"source": "x_message", "post_id": "1234567890", "social_url": "https://x.com/example/status/1234567890"},
    )

    await adapter.send(job)

    assert channel.content is None
    assert len(channel.files) == 1
    assert channel.files[0].filename == "media-1.mp4"
    assert channel.embed.url == "https://x.com/example/status/1234567890"


@pytest.mark.asyncio
async def test_discord_embed_humanizes_urlish_title() -> None:
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="defensescoop.com/2026/06/14/army-counter-drone-package-europe/",
        url="https://defensescoop.com/2026/06/14/army-counter-drone-package-europe/",
        summary="Officials said the prototype is moving into field trials.",
        image_url=None,
        image_source=None,
        source_name="Email: News Inbox",
        normalized_published_at=datetime(2026, 6, 2, tzinfo=UTC),
    )

    await adapter.send(job)

    assert channel.embed.title == "Army counter drone package europe"


@pytest.mark.asyncio
async def test_discord_embed_formats_email_with_short_markdown_stub_and_sender_footer() -> None:
    channel = FakeChannel()
    adapter = DiscordPublisherAdapter(FakeClient(channel))
    job = PostJob(
        article_id=1,
        channel_id="111111111111111111",
        title="Navy tracks Russian submarine near allied waters",
        url="https://example.com/navy-submarine",
        summary=(
            "Navy tracks Russian submarine near allied waters\n"
            "Officials said allied maritime patrol aircraft monitored the transit.\n"
            "Commanders said the activity would inform future undersea surveillance planning.\n"
            "[Read article](https://example.com/navy-submarine)\n"
            "Extra newsletter context that may still fit.\n"
            "Fifth useful line that should not be displayed."
        ),
        image_url=None,
        image_source=None,
        source_name="Email: News Inbox",
        normalized_published_at=datetime(2026, 6, 2, tzinfo=UTC),
        rich_metadata={"source": "email", "from": "Security Brief <briefing@example.com>"},
    )

    await adapter.send(job)

    assert channel.embed.title == "Navy tracks Russian submarine near allied waters"
    assert channel.embed.description == (
        "Officials said allied maritime patrol aircraft monitored the transit.\n"
        "Commanders said the activity would inform future undersea surveillance planning.\n"
        "[Read article](https://example.com/navy-submarine)\n"
        "Extra newsletter context that may still fit."
    )
    assert "Fifth useful line" not in channel.embed.description
    assert channel.embed.footer.text == (
        "Email: News Inbox · Security Brief <briefing@example.com> · "
        "New · Imp 0"
    )
