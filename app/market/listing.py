"""New-listing detection.

Three independent signals feed the listing-event store:

    Signal A - instrument registry   (authoritative; discovery events)
    Signal B - Linear PreLaunch      (registry status = PreLaunch)
    Signal C - Bybit announcements   (keyword classification)

Event types: ``prelaunch``, ``trading``, ``announced``, ``delisted``.

Idempotency: events are keyed ``(category, symbol, event_type)`` and
inserted with ``INSERT OR IGNORE``; restarts never resend. The
``telegram_sent`` flag means CONFIRMED Telegram delivery: it is only set
by the dispatcher after a successful send. On startup ``reconcile_unsent``
re-pairs unsent events with the outbox instead of blindly re-notifying.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

from app.bybit.models import Announcement
from app.config import Settings
from app.market.discovery import (
    STATUS_PRELAUNCH,
    STATUS_TRADING,
    DiscoveryResult,
    RegistryEvent,
)
from app.persistence.repository import ListingEventRepository

logger = logging.getLogger("bybit_monitor.market.listing")

EVENT_PRELAUNCH = "prelaunch"
EVENT_TRADING = "trading"
EVENT_ANNOUNCED = "announced"
EVENT_DELISTED = "delisted"

_LISTING_KEYWORDS = (
    "list",
    "listing",
    "listed",
    "上线",
    "上架",
    "perpetual",
    "spot trading",
)

_SYMBOL_RE = re.compile(r"\b([A-Z0-9]{2,12}?(?:USDT|USDC))\b")
_PSEUDO_CATEGORY = "announcement"


def _symbols_from_announcement(announcement: Announcement) -> set[str]:
    text = f"{announcement.title} {announcement.description}"
    lowered = text.lower()
    if not any(keyword in lowered for keyword in _LISTING_KEYWORDS):
        return set()
    return set(_SYMBOL_RE.findall(text))


def event_key_for(
    category: Optional[str], symbol: str, event_type: str
) -> str:
    return f"{category or _PSEUDO_CATEGORY}:{symbol}:{event_type}"


def listing_dedupe_key(event_key: str) -> str:
    """Deterministic outbox dedupe key for a listing event."""
    return f"listing:{event_key}"


class ListingTracker:
    """Persists listing events and enqueues delivery once per event."""

    def __init__(
        self,
        repo: ListingEventRepository,
        config: Settings,
        notify: Optional[Any] = None,
        outbox: Optional[Any] = None,
    ) -> None:
        """``notify`` is an async callable ``(event: dict) -> None``.

        ``outbox`` (optional ``Repository``) powers the R5 reconciliation:
        unsent events are paired with their durable outbox rows instead of
        being blindly re-notified on restart.
        """
        self.repo = repo
        self.config = config
        self.notify = notify
        self.outbox = outbox

    # ------------------------------------------------------------------
    # Signal A + B: registry events
    # ------------------------------------------------------------------
    async def handle_registry(
        self, result: DiscoveryResult, now: Optional[int] = None
    ) -> list[dict[str, Any]]:
        now = int(now if now is not None else time.time())
        created: list[dict[str, Any]] = []
        if result.first_run:
            # Empty-database seeding is silent by design.
            return created
        for event in result.events:
            event_type = self._event_type_for(event)
            if event_type is None:
                continue
            recorded = await self.repo.record(
                event_key_for(event.category, event.symbol, event_type),
                event.category,
                event.symbol,
                event_type,
                now,
            )
            if recorded is not None:
                created.append(recorded)
                logger.info(
                    "event=listing_event type=%s category=%s symbol=%s",
                    event_type,
                    event.category,
                    event.symbol,
                )
                await self._notify(recorded)
        return created

    @staticmethod
    def _event_type_for(event: RegistryEvent) -> Optional[str]:
        if event.event == "removed":
            return EVENT_DELISTED
        if event.event == "new":
            if event.new_status == STATUS_PRELAUNCH:
                return EVENT_PRELAUNCH
            if event.new_status == STATUS_TRADING:
                return EVENT_TRADING
            return None
        if event.event == "status_transition":
            if (
                event.old_status == STATUS_PRELAUNCH
                and event.new_status == STATUS_TRADING
            ):
                return EVENT_TRADING
            return None
        return None

    # ------------------------------------------------------------------
    # Signal C: announcements
    # ------------------------------------------------------------------
    async def handle_announcements(
        self, announcements: list[Announcement], now: Optional[int] = None
    ) -> list[dict[str, Any]]:
        now = int(now if now is not None else time.time())
        created: list[dict[str, Any]] = []
        for announcement in announcements:
            for symbol in _symbols_from_announcement(announcement):
                recorded = await self.repo.record(
                    event_key_for(None, symbol, EVENT_ANNOUNCED),
                    None,
                    symbol,
                    EVENT_ANNOUNCED,
                    now,
                )
                if recorded is not None:
                    created.append(recorded)
                    await self._notify(recorded)
        return created

    # ------------------------------------------------------------------
    # Startup retry: re-pair unsent events with the durable outbox
    # ------------------------------------------------------------------
    async def reconcile_unsent(self) -> int:
        """Repair notification state for events that were never confirmed.

        With an outbox (production): a pending/retry row is left to the
        dispatcher, a sent row repairs the stale ``telegram_sent`` flag,
        and a missing row is re-created with the deterministic dedupe key.
        Without an outbox (standalone tests): every unsent event is
        notified again.
        """
        repaired = 0
        for event in await self.repo.unsent():
            if self.outbox is not None:
                row = await self.outbox.find_notification_by_dedupe(
                    listing_dedupe_key(event["event_key"])
                )
                if row is not None:
                    if row["status"] == "sent":
                        # The outbox confirms delivery; repair the flag.
                        await self.repo.mark_sent(event["event_key"])
                        repaired += 1
                        logger.info(
                            "event=listing_flag_repaired key=%s",
                            event["event_key"],
                        )
                    # pending/retry: the dispatcher owns delivery.
                    continue
            await self._notify(event)
            repaired += 1
        return repaired

    # ------------------------------------------------------------------
    # Notification
    # ------------------------------------------------------------------
    async def _notify(self, event: dict[str, Any]) -> None:
        if event["event_type"] == EVENT_DELISTED:
            return
        if self.notify is None or not self.config.listing_notifications_enabled:
            return
        # Enqueue only: ``telegram_sent`` is set by the dispatcher after
        # confirmed delivery, never here.
        await self.notify(event)


__all__ = [
    "EVENT_PRELAUNCH",
    "EVENT_TRADING",
    "EVENT_ANNOUNCED",
    "EVENT_DELISTED",
    "ListingTracker",
    "event_key_for",
    "listing_dedupe_key",
]