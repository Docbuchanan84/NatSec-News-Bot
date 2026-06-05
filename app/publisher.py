from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.database import Database
from app.models import AppConfig, PostJob

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("app.audit")


class PublisherAdapter(Protocol):
    async def send(self, job: PostJob) -> str:
        ...


@dataclass
class QueueStats:
    channel_id: str
    size: int


class PublisherService:
    def __init__(self, db: Database, adapter: PublisherAdapter) -> None:
        self.db = db
        self.adapter = adapter
        self._queues: dict[str, asyncio.Queue[PostJob]] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pending: set[tuple[int, str]] = set()
        self._delay_seconds = 1.0
        self._max_queue_size = 250
        self._shutdown_drain_seconds = 20
        self._stopping = False

    def configure(self, config: AppConfig) -> None:
        self._delay_seconds = config.settings.publishing.seconds_between_posts_per_channel
        self._max_queue_size = config.settings.publishing.max_queue_size_per_channel
        self._shutdown_drain_seconds = config.settings.publishing.shutdown_drain_seconds
        active_channel_ids = {channel.discord_channel_id for channel in config.channels}
        for channel_id in active_channel_ids:
            if channel_id not in self._queues:
                self._queues[channel_id] = asyncio.Queue(maxsize=self._max_queue_size)
                self._tasks[channel_id] = asyncio.create_task(self._worker(channel_id))
        for channel_id in list(self._queues):
            if channel_id not in active_channel_ids:
                self._tasks[channel_id].cancel()
                del self._tasks[channel_id]
                del self._queues[channel_id]

    async def enqueue(self, job: PostJob) -> bool:
        key = (job.article_id, job.channel_id)
        if self.db.has_channel_post(job.article_id, job.channel_id) or key in self._pending:
            return False
        queue = self._queues.get(job.channel_id)
        if queue is None:
            logger.warning("No publisher queue for configured channel %s", job.channel_id)
            return False
        try:
            queue.put_nowait(job)
        except asyncio.QueueFull:
            logger.error("Publisher queue full for channel %s", job.channel_id)
            return False
        self._pending.add(key)
        return True

    async def _worker(self, channel_id: str) -> None:
        queue = self._queues[channel_id]
        while not self._stopping:
            job = await queue.get()
            key = (job.article_id, job.channel_id)
            try:
                message_id = await self.adapter.send(job)
                recorded = self.db.record_channel_post(job.article_id, job.channel_id, message_id)
                if recorded:
                    logger.debug("Posted article %s to channel %s", job.article_id, job.channel_id)
                    audit_logger.info(
                        "post_sent article_id=%s channel_id=%s message_id=%s title=%r",
                        job.article_id,
                        job.channel_id,
                        message_id,
                        job.title,
                    )
            except Exception:
                logger.exception("Discord post failed for article %s to channel %s", job.article_id, job.channel_id)
                audit_logger.exception(
                    "post_failure article_id=%s channel_id=%s title=%r",
                    job.article_id,
                    job.channel_id,
                    job.title,
                )
            finally:
                self._pending.discard(key)
                queue.task_done()
            if self._delay_seconds:
                await asyncio.sleep(self._delay_seconds)

    def queue_stats(self) -> list[QueueStats]:
        return [QueueStats(channel_id=channel_id, size=queue.qsize()) for channel_id, queue in self._queues.items()]

    async def shutdown(self) -> None:
        if self._shutdown_drain_seconds > 0:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*(queue.join() for queue in self._queues.values())),
                    timeout=self._shutdown_drain_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning("Publisher shutdown timed out with queued posts still pending")
        self._stopping = True
        for task in self._tasks.values():
            task.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)


class LoggingPublisherAdapter:
    async def send(self, job: PostJob) -> str:
        logger.info("DRY RUN post to %s: %s", job.channel_id, job.title)
        return f"dry-run-{job.article_id}-{int(datetime.now().timestamp())}"
