"""Normalization of raw Bybit REST JSON into domain models.

Rules:
- Convert numeric strings safely.
- Reject non-finite values (convert to None) rather than fabricating zeros.
- Preserve ``None`` for unavailable fields.
"""

from __future__ import annotations

import math
from typing import Any

from app.bybit.models import Announcement, Instrument, Ticker


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result: float = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _as_int_seconds(value: Any) -> int | None:
    """Convert a millisecond epoch timestamp to integer seconds."""
    if value is None:
        return None
    try:
        return int(float(value)) // 1000
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _percent_from_fraction(value: Any) -> float | None:
    """Bybit ``price24hPcnt`` is a decimal fraction (0.0523 == 5.23%)."""
    raw = _as_float(value)
    if raw is None:
        return None
    return round(raw * 100.0, 6)


def parse_instrument(category: str, raw: dict[str, Any]) -> Instrument:
    status = str(raw.get("status") or "Unknown")
    is_pre_listing = status == "PreLaunch" or bool(raw.get("preList"))
    return Instrument(
        category=category,
        symbol=str(raw.get("symbol") or ""),
        base_coin=str(raw.get("baseCoin") or ""),
        quote_coin=raw.get("quoteCoin"),
        settle_coin=raw.get("settleCoin"),
        contract_type=raw.get("contractType"),
        status=status,
        launch_time=_as_int_seconds(raw.get("launchTime")),
        delivery_time=_as_int_seconds(raw.get("deliveryTime")),
        is_pre_listing=is_pre_listing,
    )


def parse_ticker(category: str, raw: dict[str, Any]) -> Ticker:
    return Ticker(
        category=category,
        symbol=str(raw.get("symbol") or ""),
        last_price=_as_float(raw.get("lastPrice")),
        mark_price=_as_float(raw.get("markPrice")),
        index_price=_as_float(raw.get("indexPrice")),
        prev_price_1h=_as_float(raw.get("prevPrice1h")),
        change_24h=_percent_from_fraction(raw.get("price24hPcnt")),
        turnover_24h=_as_float(raw.get("turnover24h")),
        volume_24h=_as_float(raw.get("volume24h")),
        open_interest=_as_float(raw.get("openInterest")),
        funding_rate=_as_float(raw.get("fundingRate")),
        timestamp=_as_int_seconds(raw.get("timestamp")) or 0,
    )


def parse_announcement(raw: dict[str, Any]) -> Announcement:
    metadata = {
        k: raw.get(k)
        for k in ("type", "summary", "author", "tag")
        if raw.get(k) is not None
    }
    return Announcement(
        id=str(raw.get("id") or "") or "",
        title=str(raw.get("title") or ""),
        description=str(raw.get("description") or ""),
        announcement_type=raw.get("type"),
        timestamp=_as_int_seconds(raw.get("dateTimestamp")) or 0,
        metadata=metadata,
    )


__all__ = [
    "parse_instrument",
    "parse_ticker",
    "parse_announcement",
]