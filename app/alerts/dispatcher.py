"""Alert dispatcher.

Architecture:

    market event
    -> alert decision
    -> persistent outgoing alert record
    -> Telegram queue
    -> Telegram dispatcher
    -> Telegram

A Telegram outage never stops monitoring: enqueued records are persisted
and marked failed/sent by the background worker. Pending records from a
previous run are requeued on startup so restarts lose nothing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.config import Settings
from app.persistence.repository import Repository
from app.telegram.client import TelegramClient

logger = logging.getLogger("bybit_monitor.alerts.dispatcher")


class AlertDispatcher:
    def __init__(
        self,
        client: TelegramClient,
        repo: Repository,
        config: Settings,
    ) -> None:
        self.client = client
        self.repo = repo
        self.config = config
        self.queue: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        self._stop = asyncio.Event()
        self._worker: Optional[asyncio.Task] = None

    async def start(self) -> None:
        await self._requeue_pending()
        self._worker = asyncio.create_task(self._run())
        logger.info("event=telegram_dispatcher_started")

    async def enqueue(
        self,
        text: str,
        tag: str = "alert",
        dedupe_key: Optional[str] = None,
        origin_type: Optional[str] = None,
        origin_key: Optional[str] = None,
    ) -> int:
        """Persist the notification and hand it to the Telegram queue."""
        notification_id = await self.repo.insert_outgoing_notification(
            tag,
            text,
            dedupe_key=dedupe_key,
            origin_type=origin_type,
            origin_key=origin_key,
        )
        await self.queue.put((notification_id, text))
        return notification_id

    def feed(self, notification_id: int, message: str) -> None:
        """Hand an already-persisted row to the queue (atomic path)."""
        self.queue.put_nowait((notification_id, message))

    async def _requeue_pending(self) -> None:
        rows = await self.repo.list_notifications(limit=1000, status="pending")
        for row in rows:
            await self.queue.put((row["id"], row["message"]))
        if rows:
            logger.info("event=telegram_requeue_pending count=%d", len(rows))

    async def _run(self) -> None:
        while not self._stop.is_set() or not self.queue.empty():
            try:
                notification_id, text = await asyncio.wait_for(
                    self.queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            try:
                await self.client.send_message(text)
                await self.repo.mark_notification_sent(notification_id)
            except Exception as exc:
                await self.repo.mark_notification_failed(
                    notification_id, f"{type(exc).__name__}: {exc}"
                )
                logger.warning(
                    "event=telegram_send_failed id=%d error=%s",
                    notification_id,
                    type(exc).__name__,
                )
            finally:
                self.queue.task_done()

    async def stop(self, timeout: float = 30.0) -> None:
        """Flush the queue (bounded) and stop the worker."""
        self._stop.set()
        if self._worker is not None:
            await asyncio.wait_for(self._worker, timeout=timeout)
            self._worker = None
        await self.client.close()
        logger.info("event=telegram_dispatcher_stopped")

    @property
    def depth(self) -> int:
        return self.queue.qsize()


__all__ = ["AlertDispatcher"]