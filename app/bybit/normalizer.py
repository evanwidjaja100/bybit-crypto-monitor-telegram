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


def _as_bool(value: Any) -> bool:
    """Robust boolean parsing for boolean-like API strings."""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("true", "1", "yes", "on")


def parse_instrument(category: str, raw: dict[str, Any]) -> Instrument:
    status = str(raw.get("status") or "Unknown")
    is_pre_listing = status == "PreLaunch" or _as_bool(raw.get("isPreListing"))
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


def parse_ticker(
    category: str, raw: dict[str, Any], ts_ms: Any = None
) -> Ticker:
    """Parse a ticker.

    ``ts_ms`` is the WebSocket message-level timestamp (milliseconds);
    when present it takes precedence over the (often absent) ``data.ts``.
    """
    if ts_ms is not None:
        timestamp = _as_int_seconds(ts_ms)
    else:
        timestamp = _as_int_seconds(raw.get("timestamp"))
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
        timestamp=timestamp or 0,
    )


def _announcement_type(raw: dict[str, Any]) -> tuple[Any, Any, tuple[str, ...]]:
    """Extract structured type/tags from the real announcement schema.

    ``type`` is nested (``{key, title}``); a legacy flat string is still
    accepted as the type key.
    """
    type_raw = raw.get("type")
    if isinstance(type_raw, dict):
        type_key = type_raw.get("key")
        type_title = type_raw.get("title")
    else:
        type_key = type_raw
        type_title = None
    tags_raw = raw.get("tags")
    if isinstance(tags_raw, list):
        tags = tuple(t for t in tags_raw if isinstance(t, str))
    elif isinstance(tags_raw, str) and tags_raw.strip():
        tags = (tags_raw,)
    else:
        tags = ()
    return type_key, type_title, tags


def parse_announcement(raw: dict[str, Any]) -> Announcement:
    metadata = {
        k: raw.get(k)
        for k in ("type", "summary", "author", "tag")
        if raw.get(k) is not None
    }
    type_key, type_title, tags = _announcement_type(raw)
    return Announcement(
        id=str(raw.get("id") or "") or "",
        title=str(raw.get("title") or ""),
        description=str(raw.get("description") or ""),
        type_key=type_key,
        type_title=type_title,
        tags=tags,
        timestamp=_as_int_seconds(raw.get("dateTimestamp")) or 0,
        announcement_type=type_key,
        metadata=metadata,
    )


__all__ = [
    "parse_instrument",
    "parse_ticker",
    "parse_announcement",
]