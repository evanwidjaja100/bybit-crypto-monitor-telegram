"""Phase 6 - unique-coin aggregation tests."""

from __future__ import annotations

from app.market.deduplication import QualifyingSet, aggregate_qualifying
from app.market.momentum import MomentumValue


def m(
    base_coin: str,
    change_1h: float,
    *,
    category: str = "linear",
    symbol: str | None = None,
    settle_coin: str = "USDT",
    quote_coin: str | None = None,
) -> MomentumValue:
    return MomentumValue(
        category=category,
        symbol=symbol or f"{base_coin}{settle_coin}",
        base_coin=base_coin,
        change_1h=change_1h,
        status="OK",
        settle_coin=settle_coin,
        quote_coin=quote_coin or settle_coin,
    )


class TestUniqueCoinCounts:
    def test_duplicate_markets_count_once(self):
        values = [
            m("BTC", 6.0, settle_coin="USDT"),
            m("BTC", 7.0, settle_coin="USDC"),
        ]
        result = aggregate_qualifying(values)
        assert result.count == 1
        assert result.base_coins == ("BTC",)

    def test_two_coins_count_two(self):
        result = aggregate_qualifying([m("BTC", 6.0), m("ETH", 6.0)])
        assert result.count == 2

    def test_three_coins_count_three(self):
        result = aggregate_qualifying(
            [m("BTC", 6.0), m("ETH", 6.0), m("SOL", 6.0)]
        )
        assert result.count == 3

    def test_four_coins_count_four(self):
        result = aggregate_qualifying(
            [m("BTC", 6.0), m("ETH", 6.0), m("SOL", 6.0), m("DOGE", 6.0)]
        )
        assert result.count == 4

    def test_empty_input(self):
        assert aggregate_qualifying([]).count == 0

    def test_non_qualifying_values_are_excluded(self):
        values = [m("BTC", 4.999), m("ETH", 5.0), m("SOL", 5.001)]
        result = aggregate_qualifying(values)
        assert result.base_coins == ("SOL",)
        assert result.count == 1


class TestRepresentativeSelection:
    def test_highest_increase_wins(self):
        values = [
            m("XYZ", 5.9, category="spot", settle_coin="USDT", quote_coin="USDT"),
            m("XYZ", 8.4, settle_coin="USDT"),
            m("XYZ", 8.1, settle_coin="USDC"),
        ]
        result = aggregate_qualifying(values)
        assert result.count == 1
        rep = result.representatives[0]
        assert rep.representative.symbol == "XYZUSDT"
        assert rep.representative.change_1h == 8.4
        assert [o.symbol for o in rep.others] == ["XYZUSDC", "XYZUSDT"]

    def test_tie_break_usdt_linear_beats_usdc_linear(self):
        values = [
            m("BTC", 6.0, settle_coin="USDC"),
            m("BTC", 6.0, settle_coin="USDT"),
        ]
        result = aggregate_qualifying(values)
        assert result.representatives[0].representative.settle_coin == "USDT"

    def test_tie_break_stablecoin_spot_beats_other_spot(self):
        values = [
            m("BTC", 6.0, category="spot", settle_coin="USDT", quote_coin="ETH"),
            m("BTC", 6.0, category="spot", settle_coin="USDT", quote_coin="USDT"),
        ]
        result = aggregate_qualifying(values)
        assert result.representatives[0].representative.quote_coin == "USDT"

    def test_tie_break_linear_beats_spot(self):
        values = [
            m("BTC", 6.0, category="spot", settle_coin="USDT", quote_coin="USDT"),
            m("BTC", 6.0, settle_coin="USDT"),
        ]
        result = aggregate_qualifying(values)
        assert result.representatives[0].representative.category == "linear"

    def test_deterministic_symbol_tie_break(self):
        values = [
            m("BTC", 6.0, settle_coin="USDT", symbol="BTCUSDT"),
            m("BTC", 6.0, settle_coin="USDT", symbol="BTCUSDT2"),
        ]
        result = aggregate_qualifying(values)
        assert result.representatives[0].representative.symbol == "BTCUSDT"


class TestFingerprint:
    def test_fingerprint_is_sorted_tuple(self):
        result = aggregate_qualifying([m("SOL", 6.0), m("BTC", 6.0)])
        assert result.fingerprint == ("BTC", "SOL")

    def test_fingerprint_ignores_duplicate_markets(self):
        result = aggregate_qualifying(
            [
                m("BTC", 6.0, settle_coin="USDT"),
                m("BTC", 7.0, settle_coin="USDC"),
                m("ETH", 6.0),
            ]
        )
        assert result.fingerprint == ("BTC", "ETH")


class TestExitGateContract:
    def test_raw_contract_count_is_never_exposed(self):
        """Aggregation collapses markets; count is unique coins only."""
        values = [
            m("XYZ", 5.9, category="spot", quote_coin="USDT"),
            m("XYZ", 8.4, settle_coin="USDT"),
            m("XYZ", 8.1, settle_coin="USDC"),
            m("ABC", 6.2, settle_coin="USDT"),
        ]
        result: QualifyingSet = aggregate_qualifying(values)
        assert result.count == 2
        assert sum(len(r.others) + 1 for r in result.representatives) == 4