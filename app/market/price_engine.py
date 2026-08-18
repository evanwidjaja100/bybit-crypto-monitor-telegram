"""Price engine and REST ticker polling.

Phase 4 proves complete market monitoring using REST polling before any
WebSocket complexity is introduced.

Flow:

    fetch spot tickers + linear tickers
    -> normalize
    -> validate / accept-reject
    -> update latest market state
    -> log periodic counts
    -> repeat
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Optional

from app.bybit.models import Ticker
from app.bybit.rest import BybitError, BybitRestClient
from app.config import Settings
from app.market.discovery import InstrumentRegistry

logger = logging.getLogger("bybit_monitor.market.price_engine")


@dataclass
class PollSummary:
    """Aggregated result of one ticker polling cycle."""

    received: int = 0
    accepted: int = 0
    rejected: int = 0
    updated_count: int = 0
    spot_instruments: int = 0
    linear_usdt: int = 0
    linear_usdc: int = 0
    duration_seconds: float = 0.0


def _percent_from_fraction(value) -> Optional[float]:
    """Convert a Bybit fractional percentage (0.0523) to percent (5.23)."""
    if value is None:
        return None
    try:
        result = float(value) * 100.0
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return round(result, 6)


class PriceEngine:
    """Holds the latest normalized price state for all monitored markets.

    Both REST polling and (later) WebSocket updates flow through
    ``update_tickers`` so the two ingestion paths share one state store.
    """

    def __init__(self, config: Settings) -> None:
        self.config = config
        self._latest: dict[tuple[str, str], Ticker] = {}
        self.received = 0
        self.accepted = 0
        self.rejected = 0

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------
    def update_tickers(self, tickers: list[Ticker]) -> dict[tuple[str, str], Ticker]:
        updated: dict[tuple[str, str], Ticker] = {}
        for ticker in tickers:
            self.received += 1
            if self._accept(ticker):
                key = ticker.identity
                self._latest[key] = ticker
                self.accepted += 1
                updated[key] = ticker
            else:
                self.rejected += 1
        return updated

    def _accept(self, ticker: Ticker) -> bool:
        if not ticker.symbol:
            return False
        if ticker.last_price is not None and ticker.last_price < 0:
            return False
        return True

    def update_from_delta(self, category: str, symbol: str, data: dict) -> None:
        """Merge a WebSocket delta into the existing snapshot.

        The existing ticker (if any) is copied and updated field-by-field
        with the delta; a partial delta never replaces the full snapshot.
        """
        key = (category, symbol)
        existing = self._latest.get(key)
        if existing is None:
            logger.debug(
                "event=delta_without_snapshot category=%s symbol=%s", category, symbol
            )
            return
        change_24h = _percent_from_fraction(data.get("price24hPcnt"))
        latest = Ticker(
            category=existing.category,
            symbol=existing.symbol,
            last_price=data.get("lastPrice", existing.last_price),
            mark_price=data.get("markPrice", existing.mark_price),
            index_price=data.get("indexPrice", existing.index_price),
            prev_price_1h=data.get("prevPrice1h", existing.prev_price_1h),
            change_24h=(
                change_24h if change_24h is not None else existing.change_24h
            ),
            turnover_24h=data.get("turnover24h", existing.turnover_24h),
            volume_24h=data.get("volume24h", existing.volume_24h),
            open_interest=data.get("openInterest", existing.open_interest),
            funding_rate=data.get("fundingRate", existing.funding_rate),
            timestamp=(
                int(float(data["ts"])) // 1000 if data.get("ts") else existing.timestamp
            ),
        )
        self._latest[key] = latest

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def get(self, category: str, symbol: str) -> Optional[Ticker]:
        return self._latest.get((category, symbol))

    def snapshot(self) -> dict[tuple[str, str], Ticker]:
        return dict(self._latest)

    def latest_count(self) -> int:
        return len(self._latest)


class TickerPollService:
    """REST polling loop for tickers."""

    def __init__(
        self,
        rest: BybitRestClient,
        registry: InstrumentRegistry,
        price_engine: PriceEngine,
        config: Settings,
        on_tickers: Optional[Callable[[list[Ticker]], object]] = None,
    ) -> None:
        self.rest = rest
        self.registry = registry
        self.price_engine = price_engine
        self.config = config
        self.on_tickers = on_tickers

    async def poll_once(self, now: Optional[int] = None) -> PollSummary:
        start = time.monotonic()
        fetched: list[Ticker] = []

        if self.config.enable_spot:
            fetched.extend(await self.rest.get_spot_tickers())
        if (
            self.config.enable_linear_usdt
            or self.config.enable_linear_usdc
            or self.config.enable_inverse
        ):
            fetched.extend(await self.rest.get_linear_tickers())

        updated = self.price_engine.update_tickers(fetched)

        if self.on_tickers is not None:
            await _maybe_await(self.on_tickers(fetched))

        summary = PollSummary(
            received=self.price_engine.received,
            accepted=self.price_engine.accepted,
            rejected=self.price_engine.rejected,
            updated_count=len(updated),
            duration_seconds=round(time.monotonic() - start, 3),
        )
        await self._fill_instrument_counts(summary)
        logger.info(
            "event=ticker_poll "
            "received=%d accepted=%d rejected=%d updated=%d "
            "spot=%d linear_usdt=%d linear_usdc=%d duration=%.3fs",
            summary.received,
            summary.accepted,
            summary.rejected,
            summary.updated_count,
            summary.spot_instruments,
            summary.linear_usdt,
            summary.linear_usdc,
            summary.duration_seconds,
        )
        return summary

    async def _fill_instrument_counts(self, summary: PollSummary) -> None:
        all_instruments = await self.registry.repo.load_all()
        spot = linear_usdt = linear_usdc = 0
        for inst in all_instruments.values():
            if inst.status != "Trading":
                continue
            if inst.category == "spot":
                spot += 1
            elif inst.category == "linear":
                if inst.settle_coin == "USDT":
                    linear_usdt += 1
                elif inst.settle_coin == "USDC":
                    linear_usdc += 1
        summary.spot_instruments = spot
        summary.linear_usdt = linear_usdt
        summary.linear_usdc = linear_usdc

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        logger.info(
            "event=ticker_poll_loop_started interval=%.1fs",
            self.config.rest_ticker_poll_seconds,
        )
        while not stop_event.is_set():
            try:
                await self.poll_once()
            except BybitError as exc:
                logger.warning(
                    "event=ticker_poll_failed error=%s", type(exc).__name__
                )
            except Exception:
                logger.exception("event=ticker_poll_unexpected")
            try:
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=self.config.rest_ticker_poll_seconds,
                )
            except asyncio.TimeoutError:
                continue


async def _maybe_await(callback_result: object) -> None:
    if asyncio.iscoroutine(callback_result):
        await callback_result


__all__ = ["PollSummary", "PriceEngine", "TickerPollService"]