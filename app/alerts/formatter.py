"""Alert message formatting.

Produces the readable Telegram alert format from a unique-coin
aggregation. ``4 / 3`` style output is impossible: formatting raises
when handed more qualifying coins than the configured maximum.
"""

from __future__ import annotations

import time
from typing import Optional

from app.config import Settings
from app.market.deduplication import QualifyingSet
from app.market.listing import EVENT_ANNOUNCED, EVENT_DELISTED, EVENT_PRELAUNCH, EVENT_TRADING
from app.market.momentum import MomentumValue

HEADER_TRANSITION = "🚨 BYBIT 1H MOMENTUM ALERT"
HEADER_HOURLY = "⏰ BYBIT 1H MOMENTUM ACTIVE-STATE"
HEADER_COMPOSITION = "🔄 BYBIT 1H MOMENTUM COMPOSITION CHANGE"

_HEADERS = {
    "transition": HEADER_TRANSITION,
    "hourly": HEADER_HOURLY,
    "composition": HEADER_COMPOSITION,
}


def _fmt_percent(value: Optional[float], decimals: int = 2) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.{decimals}f}%"


def _fmt_price(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"${value:,.5g}"


def _fmt_turnover(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    magnitude = abs(value)
    if magnitude >= 1e9:
        return f"${value / 1e9:.2f}B"
    if magnitude >= 1e6:
        return f"${value / 1e6:.2f}M"
    if magnitude >= 1e3:
        return f"${value / 1e3:.2f}K"
    return f"${value:.2f}"


def _market_label(value: MomentumValue) -> str:
    if value.category == "spot":
        return "Spot"
    if value.contract_type == "LinearPerpetual":
        return f"{value.settle_coin or 'USDT'} Perpetual"
    if value.contract_type:
        return f"{value.settle_coin or ''} {value.contract_type}".strip()
    return f"{value.settle_coin or ''} Linear".strip()


def _coin_block(value: MomentumValue) -> str:
    lines = [
        f"🔥 {value.base_coin}",
        _market_label(value),
        f"1H: {_fmt_percent(value.change_1h)}",
        f"Price: {_fmt_price(value.last_price)}",
    ]
    if value.change_24h is not None:
        lines.append(f"24H: {_fmt_percent(value.change_24h)}")
    if value.category != "spot":
        if value.mark_price is not None:
            lines.append(f"Mark: {_fmt_price(value.mark_price)}")
        if value.funding_rate is not None:
            lines.append(f"Funding: {_fmt_percent(value.funding_rate, 3)}")
    if value.turnover_24h is not None:
        lines.append(f"24H Turnover: {_fmt_turnover(value.turnover_24h)}")
    return "\n".join(lines)


def _timestamp_line(now: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now))


def format_alert(
    qualifying: QualifyingSet,
    config: Settings,
    now: Optional[int] = None,
    kind: str = "transition",
) -> str:
    """Format a unique-coin aggregation into a Telegram message."""
    now = int(now if now is not None else time.time())
    count = qualifying.count
    if count > config.max_qualifying_coins:
        raise ValueError(
            f"refusing to format {count} qualifying coins "
            f"(max {config.max_qualifying_coins})"
        )

    header = _HEADERS.get(kind, HEADER_TRANSITION)
    parts = [header, "", f"{count} / {config.max_qualifying_coins} qualifying coins"]

    for representative in qualifying.representatives:
        parts.extend(["", _coin_block(representative.representative)])
        if representative.others:
            others = ", ".join(
                f"{o.symbol} {_fmt_percent(o.change_1h)}" for o in representative.others
            )
            parts.append(f"  also: {others}")

    parts.extend(
        [
            "",
            "Rule:",
            f"{config.min_qualifying_coins}-{config.max_qualifying_coins} unique coins "
            f"> +{config.alert_threshold_percent:g}% / 1H",
            "",
            f"Updated: {_timestamp_line(now)}",
        ]
    )
    return "\n".join(parts)


def format_listing_alert(
    event: dict,
    now: Optional[int] = None,
) -> str:
    """Format a listing event notification (Phase 9)."""
    now = int(now if now is not None else time.time())
    event_type = event["event_type"]
    label = {
        EVENT_PRELAUNCH: "PreLaunch",
        EVENT_TRADING: "Now Trading",
        EVENT_ANNOUNCED: "Announced",
        EVENT_DELISTED: "Delisted",
    }.get(event_type, event_type.title())
    parts = [
        "🆕 BYBIT NEW LISTING",
        "",
        f"{event['symbol']} ({event['category'] or 'announcement'})",
        f"Status: {label}",
        "",
        f"Updated: {_timestamp_line(now)}",
    ]
    return "\n".join(parts)


__all__ = [
    "format_alert",
    "format_listing_alert",
    "HEADER_TRANSITION",
    "HEADER_HOURLY",
    "HEADER_COMPOSITION",
]