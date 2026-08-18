"""One-hour momentum engine.

Linear derivatives:

    change_1h = ((last_price / prev_price_1h) - 1) * 100
    (only when last_price > 0 and prev_price_1h > 0)

Spot uses locally-maintained, persisted history. A sample is recorded
per symbol approximately every ``SPOT_SAMPLE_SECONDS``. The 1-hour anchor
is the sample closest to ``T - 3600`` within ``+/- 90`` seconds. If no
valid anchor exists the market is ``WARMING_UP``; no result is fabricated.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from app.bybit.models import Instrument, Ticker
from app.config import Settings
from app.market.discovery import InstrumentRegistry
from app.market.price_engine import PriceEngine
from app.persistence.repository import PriceSampleRepository

logger = logging.getLogger("bybit_monitor.market.momentum")

SECONDS_PER_HOUR = 3600

STATUS_OK = "OK"
STATUS_WARMING_UP = "WARMING_UP"
STATUS_NO_DATA = "NO_DATA"


@dataclass
class MomentumValue:
    """Momentum result for one market."""

    category: str
    symbol: str
    base_coin: str
    change_1h: Optional[float]
    status: str
    last_price: Optional[float] = None
    mark_price: Optional[float] = None
    change_24h: Optional[float] = None
    turnover_24h: Optional[float] = None
    funding_rate: Optional[float] = None
    contract_type: Optional[str] = None
    settle_coin: Optional[str] = None
    quote_coin: Optional[str] = None


class SpotHistory:
    """Persisted spot-price history with ~1/min downsampling.

    Downsampling is enforced with an in-memory map during a run; on
    restart the map is empty so one extra sample is written immediately,
    which is harmless. The authoritative history lives in SQLite and
    survives restarts.
    """

    def __init__(
        self,
        repo: PriceSampleRepository,
        sample_seconds: float = 60.0,
        tolerance_seconds: float = 90.0,
    ) -> None:
        self.repo = repo
        self.sample_seconds = sample_seconds
        self.tolerance_seconds = tolerance_seconds
        self._last_sample_at: dict[tuple[str, str], int] = {}

    async def record(
        self, category: str, symbol: str, timestamp: int, price: Optional[float]
    ) -> bool:
        if price is None or price <= 0:
            return False
        last = self._last_sample_at.get((category, symbol))
        if last is not None and (timestamp - last) < self.sample_seconds:
            return False
        inserted = await self.repo.insert_sample(category, symbol, timestamp, price)
        if inserted:
            self._last_sample_at[(category, symbol)] = timestamp
        return inserted

    async def find_anchor(
        self, category: str, symbol: str, now: int
    ) -> Optional[float]:
        target = now - SECONDS_PER_HOUR
        return await self.repo.find_reference(
            category, symbol, target, self.tolerance_seconds
        )

    async def prune(self, now: int, retention_seconds: float) -> int:
        cutoff = now - int(retention_seconds)
        return await self.repo.cleanup_older_than(cutoff)


class MomentumEngine:
    """Computes 1-hour percentage change per market type."""

    PRECISION_DECIMALS = 6

    def __init__(self, config: Settings, spot_history: SpotHistory) -> None:
        self.config = config
        self.threshold = config.alert_threshold_percent
        self.spot_history = spot_history

    # ------------------------------------------------------------------
    # Pure math helpers
    # ------------------------------------------------------------------
    @staticmethod
    def linear_change(
        last_price: Optional[float], prev_price_1h: Optional[float]
    ) -> Optional[float]:
        if last_price is None or prev_price_1h is None:
            return None
        if last_price <= 0 or prev_price_1h <= 0:
            return None
        return round((last_price / prev_price_1h - 1.0) * 100.0, 9)

    @staticmethod
    def spot_change(
        last_price: Optional[float], anchor: Optional[float]
    ) -> Optional[float]:
        if last_price is None or anchor is None:
            return None
        if last_price <= 0 or anchor <= 0:
            return None
        return round((last_price / anchor - 1.0) * 100.0, 9)

    @staticmethod
    def qualifies(
        change_1h: Optional[float], threshold: float = 5.0
    ) -> bool:
        """Strictly-greater-than comparison, deterministically rounded.

        ``round(change, 6)`` removes float-representation error so that
        exactly +5.000% (from exact integer inputs) does not qualify
        while +5.001% does.
        """
        if change_1h is None:
            return False
        return round(change_1h, MomentumEngine.PRECISION_DECIMALS) > round(
            threshold, MomentumEngine.PRECISION_DECIMALS
        )

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------
    async def evaluate(
        self, instrument: Instrument, ticker: Ticker, now: int
    ) -> MomentumValue:
        base = MomentumValue(
            category=instrument.category,
            symbol=instrument.symbol,
            base_coin=instrument.base_coin,
            change_1h=None,
            status=STATUS_NO_DATA,
            last_price=ticker.last_price,
            mark_price=ticker.mark_price,
            change_24h=ticker.change_24h,
            turnover_24h=ticker.turnover_24h,
            funding_rate=ticker.funding_rate,
            contract_type=instrument.contract_type,
            settle_coin=instrument.settle_coin,
            quote_coin=instrument.quote_coin,
        )

        if ticker.last_price is None or ticker.last_price <= 0:
            return base

        if instrument.category == "linear":
            base.change_1h = self.linear_change(
                ticker.last_price, ticker.prev_price_1h
            )
            base.status = STATUS_OK if base.change_1h is not None else STATUS_NO_DATA
            return base

        # Spot: record a sample and look up the 1h anchor.
        await self.spot_history.record(
            instrument.category, instrument.symbol, now, ticker.last_price
        )
        anchor = await self.spot_history.find_anchor(
            instrument.category, instrument.symbol, now
        )
        if anchor is None or anchor <= 0:
            base.status = STATUS_WARMING_UP
            return base
        base.change_1h = self.spot_change(ticker.last_price, anchor)
        base.status = STATUS_OK if base.change_1h is not None else STATUS_WARMING_UP
        return base


class MomentumEvaluator:
    """Iterates supported, tradable instruments and computes momentum."""

    def __init__(
        self,
        registry: InstrumentRegistry,
        price_engine: PriceEngine,
        momentum: MomentumEngine,
        config: Settings,
    ) -> None:
        self.registry = registry
        self.price_engine = price_engine
        self.momentum = momentum
        self.config = config

    def _is_supported(self, instrument: Instrument) -> bool:
        if instrument.status != "Trading":
            return False
        if instrument.category == "spot":
            return self.config.enable_spot
        if instrument.category == "linear":
            if instrument.settle_coin == "USDT":
                return self.config.enable_linear_usdt
            if instrument.settle_coin == "USDC":
                return self.config.enable_linear_usdc
            return False
        if instrument.category == "inverse":
            return self.config.enable_inverse
        return False

    async def evaluate_all(self, now: Optional[int] = None) -> list[MomentumValue]:
        now = int(now if now is not None else time.time())
        snapshot = self.price_engine.snapshot()
        instruments = await self.registry.repo.load_all()
        values: list[MomentumValue] = []
        for instrument in instruments.values():
            if not self._is_supported(instrument):
                continue
            ticker = snapshot.get(instrument.identity)
            if ticker is None:
                continue
            values.append(await self.momentum.evaluate(instrument, ticker, now))
        return values


__all__ = [
    "MomentumValue",
    "SpotHistory",
    "MomentumEngine",
    "MomentumEvaluator",
    "STATUS_OK",
    "STATUS_WARMING_UP",
    "STATUS_NO_DATA",
]