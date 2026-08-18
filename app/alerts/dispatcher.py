"""Alert dispatcher.

Architecture:

    market event
    -> alert decision
    -> persistent outgoing alert record (outbox)
    -> Telegram dispatcher (polls the outbox)
    -> Telegram

The outbox is the queue. The dispatcher polls ``pending``/``retry`` rows
that are due (``next_attempt_at <= now``), delivers them and marks them
``sent``. Transient failures schedule a new attempt with an increasing
delay; permanent failures (400-class) and expired rows are marked
``dead``. Restarts lose nothing: due rows are picked up on the first
poll. A Telegram outage never stops monitoring.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from app.config import Settings
from app.persistence.repository import Repository
from app.telegram.client import TelegramClient, TelegramPermanentError

logger = logging.getLogger("bybit_monitor.alerts.dispatcher")

# Delay (seconds) to wait after the N-th failed attempt before trying
# again: attempt 1 -> +10s, 2 -> +30s, 3 -> +60s, 4 -> +5min, then capped
# exponential backoff up to 1 hour.
RETRY_DELAYS = (10, 30, 60, 300, 600, 1200, 2400, 3600)

# Consecutive unexpected worker-loop failures after which the dispatcher
# reports itself unhealthy (the process keeps running so the Docker
# healthcheck / restart policy can act on it).
MAX_CONSECUTIVE_WORKER_FAILURES = 5


def retry_delay_for(attempt_count: int) -> int:
    """Delay after ``attempt_count`` failed attempts."""
    return RETRY_DELAYS[min(attempt_count, len(RETRY_DELAYS)) - 1]


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
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()
        self._worker: Optional[asyncio.Task] = None
        # Worker health surface (Phase F4): an unexpected error inside the
        # loop must never silently kill delivery.
        self.worker_started_at: Optional[int] = None
        self.worker_last_iteration_at: Optional[int] = None
        self.worker_last_error_at: Optional[int] = None
        self.worker_error_count: int = 0
        self._consecutive_failures: int = 0

    async def start(self) -> None:
        self._wake.set()
        self.worker_started_at = int(time.time())
        self._worker = asyncio.create_task(self._run())
        logger.info("event=telegram_dispatcher_started")

    @property
    def worker_healthy(self) -> bool:
        """Alive and not stuck in a loop of consecutive failures."""
        if self._worker is None:
            return True
        if self._worker.done():
            return False
        return self._consecutive_failures <= MAX_CONSECUTIVE_WORKER_FAILURES

    async def enqueue(
        self,
        text: str,
        tag: str = "alert",
        dedupe_key: Optional[str] = None,
        origin_type: Optional[str] = None,
        origin_key: Optional[str] = None,
    ) -> int:
        """Persist the notification; the worker delivers it from the outbox."""
        notification_id = await self.repo.insert_outgoing_notification(
            tag,
            text,
            dedupe_key=dedupe_key,
            origin_type=origin_type,
            origin_key=origin_key,
        )
        self.wake()
        return notification_id

    def feed(self, notification_id: int, message: str) -> None:
        """Hand an already-persisted row to the worker (atomic path)."""
        del message  # the worker reads the row from the outbox itself
        self.wake()

    def wake(self) -> None:
        """Ask the worker to poll the outbox immediately."""
        self._wake.set()

    async def _run(self) -> None:
        """Supervised worker loop.

        One unexpected operational error must not terminate delivery: it
        is logged, counted and retried after a short backoff. Cancellation
        still stops the worker cleanly. Permanent programming errors
        surface via ``worker_healthy`` once consecutive failures exceed
        the threshold.
        """
        while not self._stop.is_set():
            try:
                await self._iteration()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("event=telegram_dispatcher_loop_error")
                self._consecutive_failures += 1
                self.worker_error_count += 1
                self.worker_last_error_at = int(time.time())
                await asyncio.sleep(self.config.dispatcher_error_backoff_seconds)
            else:
                self._consecutive_failures = 0
                self.worker_last_iteration_at = int(time.time())

    async def _iteration(self) -> None:
        """One worker cycle: expire, deliver due rows, then wait for work."""
        await self.repo.expire_notifications(
            int(self.config.notification_max_age_seconds),
            int(self.config.listing_notification_max_age_seconds),
        )
        processed = await self._process_due()
        if processed:
            return
        if self._stop.is_set():
            return
        try:
            await asyncio.wait_for(
                self._wake.wait(), timeout=self.config.dispatcher_poll_seconds
            )
        except asyncio.TimeoutError:
            pass
        finally:
            self._wake.clear()

    async def _process_due(self) -> bool:
        rows = await self.repo.due_notifications()
        for row in rows:
            await self._deliver(row)
        return bool(rows)

    async def _deliver(self, row: dict) -> None:
        notification_id = int(row["id"])
        try:
            await self.client.send_message(row["message"])
            if row.get("origin_type") == "listing" and row.get("origin_key"):
                # Confirm outbox + listing event in one transaction so a
                # crash cannot leave them inconsistent.
                async with self.repo.db.transaction():
                    await self.repo.mark_notification_sent(
                        notification_id, commit=False
                    )
                    await self.repo.mark_listing_sent(
                        row["origin_key"], commit=False
                    )
            else:
                await self.repo.mark_notification_sent(notification_id)
        except TelegramPermanentError as exc:
            await self.repo.mark_notification_dead(
                notification_id, f"{type(exc).__name__}: {exc}"
            )
            logger.warning(
                "event=telegram_send_permanent_failure id=%d error=%s",
                notification_id,
                type(exc).__name__,
            )
        except Exception as exc:
            retry_after = getattr(exc, "retry_after", None)
            row_attempts = int(row.get("attempt_count") or 0) + 1
            delay = retry_delay_for(row_attempts)
            if retry_after is not None:
                delay = max(delay, int(retry_after))
            next_attempt_at = int(time.time()) + delay
            await self.repo.mark_notification_retry(
                notification_id,
                f"{type(exc).__name__}: {exc}",
                next_attempt_at,
            )
            logger.warning(
                "event=telegram_send_failed id=%d attempt=%d retry_in=%ds error=%s",
                notification_id,
                row_attempts,
                delay,
                type(exc).__name__,
            )

    async def stop(self, timeout: float = 30.0) -> None:
        """Deliver what is currently due (bounded) and stop the worker."""
        self._stop.set()
        self._wake.set()
        if self._worker is not None:
            await asyncio.wait_for(self._worker, timeout=timeout)
            self._worker = None
        await self.client.close()
        logger.info("event=telegram_dispatcher_stopped")

    async def depth(self) -> int:
        return await self.repo.count_unsent()


__all__ = ["AlertDispatcher", "RETRY_DELAYS", "retry_delay_for"]
