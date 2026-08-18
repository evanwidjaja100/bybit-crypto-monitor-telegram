"""Instrument discovery and the persistent instrument registry.

Discovery runs on a configured interval:

    fetch Spot
    + fetch Linear Trading
    + fetch Linear PreLaunch
    -> normalize
    -> compare with registry
    -> insert new
    -> update existing
    -> detect status transitions
    -> persist
    -> publish internal discovery events

On an empty database the existing markets are seeded silently (no "new
listing" storm). Only instruments first observed after initialization are
reported as newly discovered.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from app.bybit.models import Instrument
from app.bybit.rest import BybitError, BybitRestClient
from app.config import Settings
from app.persistence.repository import InstrumentRepository

logger = logging.getLogger("bybit_monitor.market.discovery")

# Registry statuses.
STATUS_REMOVED = "Removed"
STATUS_TRADING = "Trading"
STATUS_PRELAUNCH = "PreLaunch"


@dataclass
class RegistryEvent:
    """An internal discovery event for a single instrument."""

    category: str
    symbol: str
    base_coin: str
    event: str  # "new" | "status_transition" | "removed"
    old_status: Optional[str] = None
    new_status: Optional[str] = None
    is_pre_listing: bool = False


@dataclass
class DiscoveryResult:
    events: list[RegistryEvent] = field(default_factory=list)
    instrument_count: int = 0
    first_run: bool = False


class InstrumentRegistry:
    """Reconciles fetched instruments against the persistent registry."""

    def __init__(self, repo: InstrumentRepository) -> None:
        self.repo = repo

    async def reconcile(
        self, fetched: list[Instrument], now: Optional[int] = None
    ) -> DiscoveryResult:
        now = int(now if now is not None else time.time())
        current = await self.repo.load_all()
        first_run = len(current) == 0

        events: list[RegistryEvent] = []
        upserts: list[Instrument] = []
        seen: set[tuple[str, str]] = set()

        for inst in fetched:
            key = inst.identity
            seen.add(key)
            prev = current.get(key)
            if prev is None:
                upserts.append(inst)
                if not first_run:
                    events.append(
                        RegistryEvent(
                            category=inst.category,
                            symbol=inst.symbol,
                            base_coin=inst.base_coin,
                            event="new",
                            new_status=inst.status,
                            is_pre_listing=inst.is_pre_listing,
                        )
                    )
            else:
                upserts.append(inst)
                if prev.status != inst.status:
                    events.append(
                        RegistryEvent(
                            category=inst.category,
                            symbol=inst.symbol,
                            base_coin=inst.base_coin,
                            event="status_transition",
                            old_status=prev.status,
                            new_status=inst.status,
                            is_pre_listing=inst.is_pre_listing,
                        )
                    )

        # Previously monitored instruments no longer present in the API.
        removed_keys: list[tuple[str, str]] = []
        for key, existing in current.items():
            if existing.status != STATUS_REMOVED and key not in seen:
                removed_keys.append(key)
                events.append(
                    RegistryEvent(
                        category=existing.category,
                        symbol=existing.symbol,
                        base_coin=existing.base_coin,
                        event="removed",
                        old_status=existing.status,
                        is_pre_listing=existing.is_pre_listing,
                    )
                )

        await self.repo.upsert_many(upserts, now)
        await self.repo.mark_removed(removed_keys, now)

        return DiscoveryResult(
            events=events,
            instrument_count=len(seen),
            first_run=first_run,
        )


class InstrumentDiscovery:
    """Background discovery loop that keeps the registry current."""

    def __init__(
        self,
        rest: BybitRestClient,
        registry: InstrumentRegistry,
        config: Settings,
        on_events: Optional[Callable[[DiscoveryResult], object]] = None,
    ) -> None:
        self.rest = rest
        self.registry = registry
        self.config = config
        self.on_events = on_events
        self.last_result: Optional[DiscoveryResult] = None

    async def discover_once(self, now: Optional[int] = None) -> DiscoveryResult:
        fetched: list[Instrument] = []
        if self.config.enable_spot:
            fetched.extend(await self.rest.get_spot_instruments())
        if (
            self.config.enable_linear_usdt
            or self.config.enable_linear_usdc
            or self.config.enable_inverse
        ):
            fetched.extend(
                await self.rest.get_linear_instruments(status=STATUS_TRADING)
            )
            fetched.extend(
                await self.rest.get_linear_instruments(status=STATUS_PRELAUNCH)
            )
        if self.config.enable_inverse:
            fetched.extend(await self.rest.get_inverse_instruments())

        result = await self.registry.reconcile(fetched, now)
        self.last_result = result
        logger.info(
            "event=discovery_complete instruments=%d first_run=%s events=%d",
            result.instrument_count,
            result.first_run,
            len(result.events),
        )
        if self.on_events is not None:
            await _maybe_await(self.on_events(result))
        return result

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        logger.info(
            "event=discovery_loop_started interval=%.0fs",
            self.config.instrument_refresh_seconds,
        )
        while not stop_event.is_set():
            try:
                await self.discover_once()
            except BybitError as exc:
                logger.warning("event=discovery_failed error=%s", type(exc).__name__)
            except Exception:
                logger.exception("event=discovery_error_unexpected")
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.config.instrument_refresh_seconds,
                )
            except asyncio.TimeoutError:
                continue


async def _maybe_await(callback_result: object) -> None:
    """Await a callback result if it is awaitable (supports both styles)."""
    if asyncio.iscoroutine(callback_result):
        await callback_result


__all__ = [
    "RegistryEvent",
    "DiscoveryResult",
    "InstrumentRegistry",
    "InstrumentDiscovery",
    "STATUS_REMOVED",
    "STATUS_TRADING",
    "STATUS_PRELAUNCH",
]