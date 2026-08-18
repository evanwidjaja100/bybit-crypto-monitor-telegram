"""Unique-coin aggregation.

Converts qualifying instruments into qualifying unique base coins.

    qualifying instruments
    -> group by baseCoin
    -> deduplicate
    -> rank markets
    -> select representative
    -> calculate unique count

Representative-market selection:
    primary rule: highest valid 1h increase
    tie-break:    USDT Linear -> USDC Linear -> stablecoin Spot -> other Spot

Alert decisions must consume only the unique-coin result. Raw contract
count must never reach the alert policy.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.market.momentum import MomentumEngine, MomentumValue

STABLECOIN_QUOTES = frozenset(
    {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "BUSD", "PYUSD", "EUR", "USD"}
)


def _tie_break_rank(value: MomentumValue) -> int:
    """Lower rank wins when two markets have the same 1h increase."""
    if value.category == "linear":
        if value.settle_coin == "USDT":
            return 0
        if value.settle_coin == "USDC":
            return 1
        return 2
    if value.category == "spot":
        if (value.quote_coin or "").upper() in STABLECOIN_QUOTES:
            return 3
        return 4
    return 5


@dataclass
class RepresentativeMarket:
    """One base coin with its representative market and the rest."""

    base_coin: str
    representative: MomentumValue
    others: list[MomentumValue] = field(default_factory=list)


@dataclass
class QualifyingSet:
    """Unique qualifying base coins; the only input to the alert policy."""

    representatives: list[RepresentativeMarket] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.representatives)

    @property
    def base_coins(self) -> tuple[str, ...]:
        return tuple(r.base_coin for r in self.representatives)

    @property
    def fingerprint(self) -> tuple[str, ...]:
        """Sorted unique coin tuple used for composition-change detection."""
        return tuple(sorted(self.base_coins))


def aggregate_qualifying(
    values: list[MomentumValue], threshold: float = 5.0
) -> QualifyingSet:
    """Group qualifying momentum values by base coin and pick representatives."""
    by_coin: dict[str, list[MomentumValue]] = {}
    for value in values:
        if not MomentumEngine.qualifies(value.change_1h, threshold):
            continue
        by_coin.setdefault(value.base_coin, []).append(value)

    representatives: list[RepresentativeMarket] = []
    for coin, markets in by_coin.items():
        ordered = sorted(
            markets,
            key=lambda m: (
                -float(m.change_1h or 0.0),
                _tie_break_rank(m),
                m.symbol,
            ),
        )
        representatives.append(
            RepresentativeMarket(
                base_coin=coin,
                representative=ordered[0],
                others=ordered[1:],
            )
        )

    representatives.sort(key=lambda r: r.base_coin)
    return QualifyingSet(representatives)


__all__ = ["RepresentativeMarket", "QualifyingSet", "aggregate_qualifying"]