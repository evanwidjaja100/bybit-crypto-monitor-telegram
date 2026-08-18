"""Bybit domain models (instruments, tickers, announcements)."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Instrument:
    """A normalized Bybit instrument.

    Identity is ``(category, symbol)`` - never symbol alone.
    """

    category: str
    symbol: str
    base_coin: str
    quote_coin: str | None = None
    settle_coin: str | None = None
    contract_type: str | None = None
    status: str = "Trading"
    launch_time: int | None = None  # epoch seconds
    delivery_time: int | None = None  # epoch seconds
    is_pre_listing: bool = False

    @property
    def identity(self) -> tuple[str, str]:
        return (self.category, self.symbol)


@dataclass
class Ticker:
    """A normalized Bybit ticker."""

    category: str
    symbol: str
    last_price: float | None = None
    mark_price: float | None = None
    index_price: float | None = None
    prev_price_1h: float | None = None
    change_24h: float | None = None
    turnover_24h: float | None = None
    volume_24h: float | None = None
    open_interest: float | None = None
    funding_rate: float | None = None
    timestamp: int = 0  # epoch seconds

    @property
    def identity(self) -> tuple[str, str]:
        return (self.category, self.symbol)


@dataclass
class Announcement:
    """A Bybit announcement (used as a soft listing signal).

    The real API nests the announcement type as ``type: {key, title}``
    plus a ``tags`` list; both are modelled explicitly.
    """

    id: str
    title: str
    description: str
    type_key: str | None = None
    type_title: str | None = None
    tags: tuple[str, ...] = ()
    timestamp: int = 0  # epoch seconds
    # Compatibility alias for the previous flat ``announcement_type``.
    announcement_type: str | None = None
    # Extra metadata captured for classifier heuristics.
    metadata: dict = field(default_factory=dict)


@dataclass
class PaginatedResult:
    """A page of items plus the next pagination cursor."""

    items: list
    next_cursor: str | None = None


__all__ = ["Instrument", "Ticker", "Announcement", "PaginatedResult"]