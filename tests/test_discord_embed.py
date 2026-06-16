from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.discord_bot import DiscordPublisherAdapter
from app.models import PostJob


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


@pytest.mark.asyncio
async def test_discord_embed_sets_image_when_post_job_has_image() -> None:
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
    assert channel.embed.image.url == "https://d1ldvf68ux039x.cloudfront.net/thumbs/photos/2605/1/250w_q95.jpg"


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
async def test_discord_embed_formats_bluesky_post_without_native_preview() -> None:
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
    assert channel.embed is None
    assert len(channel.embeds) == 2
    assert channel.embeds[0].image.url == "https://cdn.bsky.app/img/feed_fullsize/plain/did/full"
    assert channel.embeds[1].image.url is None
    assert channel.embeds[1].title == "Reuters"
    assert channel.embeds[1].description == "Story summary"
    assert channel.embeds[1].url == "https://bsky.app/profile/reuters.com/post/abc"


@pytest.mark.asyncio
async def test_discord_embed_formats_x_post_without_native_preview() -> None:
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
    assert channel.embed is None
    assert len(channel.embeds) == 2
    assert channel.embeds[0].image.url == "https://pbs.twimg.com/media/example.jpg"
    assert channel.embeds[1].image.url is None
    assert channel.embeds[1].title == "@example"
    assert channel.embeds[1].description == "Drone footage from the front\nhttps://example.com/report"
    assert channel.embeds[1].url == "https://x.com/example/status/1234567890"


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
