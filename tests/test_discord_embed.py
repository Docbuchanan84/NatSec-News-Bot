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
        self.content = None

    async def send(self, content=None, *, embed):
        self.content = content
        self.embed = embed
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
