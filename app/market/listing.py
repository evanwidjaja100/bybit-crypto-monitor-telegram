"""New-listing detection.

Three independent signals feed the listing-event store:

    Signal A - instrument registry   (authoritative; discovery events)
    Signal B - Linear PreLaunch      (registry status = PreLaunch)
    Signal C - Bybit announcements   (keyword classification)

Event types: ``prelaunch``, ``trading``, ``announced``, ``delisted``.

Idempotency: events are keyed ``(category, symbol, event_type)`` and
inserted with ``INSERT OR IGNORE``; restarts never resend. The
``telegram_sent`` flag tracks notification state and unsent events are
retried on startup.
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


class ListingTracker:
    """Persists listing events and notifies once per event."""

    def __init__(
        self,
        repo: ListingEventRepository,
        config: Settings,
        notify: Optional[Any] = None,
    ) -> None:
        """``notify`` is an async callable ``(event: dict) -> None``."""
        self.repo = repo
        self.config = config
        self.notify = notify

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
    # Startup retry: notify any event that was never delivered
    # ------------------------------------------------------------------
    async def reconcile_unsent(self) -> int:
        count = 0
        for event in await self.repo.unsent():
            await self._notify(event)
            count += 1
        return count

    # ------------------------------------------------------------------
    # Notification
    # ------------------------------------------------------------------
    async def _notify(self, event: dict[str, Any]) -> None:
        if event["event_type"] == EVENT_DELISTED:
            return
        if self.notify is None or not self.config.listing_notifications_enabled:
            return
        await self.notify(event)
        await self.repo.mark_sent(event["event_key"])


__all__ = [
    "EVENT_PRELAUNCH",
    "EVENT_TRADING",
    "EVENT_ANNOUNCED",
    "EVENT_DELISTED",
    "ListingTracker",
    "event_key_for",
]