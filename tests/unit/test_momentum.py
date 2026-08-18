"""Phase 5 - one-hour momentum engine tests."""

from __future__ import annotations

import pytest

from app.bybit.models import Instrument, Ticker
from app.market.discovery import InstrumentRegistry
from app.market.momentum import (
    MomentumEngine,
    MomentumEvaluator,
    SpotHistory,
    STATUS_NO_DATA,
    STATUS_OK,
    STATUS_WARMING_UP,
)
from app.market.price_engine import PriceEngine
from app.persistence.repository import InstrumentRepository, PriceSampleRepository
from tests.conftest import make_settings
from tests.unit.test_discovery import mk

HOUR = 3600


def linear_inst(symbol="BTCUSDT", base="BTC", settle="USDT") -> Instrument:
    return Instrument(
        category="linear",
        symbol=symbol,
        base_coin=base,
        quote_coin=settle,
        settle_coin=settle,
        contract_type="LinearPerpetual",
        status="Trading",
    )


def spot_inst(symbol="BTCUSDT", base="BTC") -> Instrument:
    return Instrument(
        category="spot",
        symbol=symbol,
        base_coin=base,
        quote_coin="USDT",
        status="Trading",
    )


@pytest.fixture
def spot_history(db):
    return SpotHistory(
        PriceSampleRepository(db), sample_seconds=60.0, tolerance_seconds=90.0
    )


@pytest.fixture
def momentum(spot_history):
    return MomentumEngine(make_settings(), spot_history)


class TestLinearBoundaryRules:
    """The exact required mathematical test vector."""

    def test_100_to_105_000_does_not_qualify(self):
        change = MomentumEngine.linear_change(105.000, 100.0)
        assert change is not None
        assert round(change, 3) == 5.000
        assert MomentumEngine.qualifies(change) is False

    def test_100_to_105_001_qualifies(self):
        change = MomentumEngine.linear_change(105.001, 100.0)
        assert MomentumEngine.qualifies(change) is True

    def test_100_to_105_000000001_qualifies(self):
        """Plan 7.3: 100 -> 105.000000001 must qualify (raw > 5.0)."""
        change = MomentumEngine.linear_change(105.000000001, 100.0)
        assert change is not None
        assert change > 5.0
        assert MomentumEngine.qualifies(change) is True

    def test_100_to_105_0000000004_qualifies(self):
        """No pre-qualification rounding: a raw change of 5.0000000004
        must still qualify even though it rounds to 5.0 at 9 decimals."""
        change = MomentumEngine.linear_change(105.0000000004, 100.0)
        assert change is not None
        assert change > 5.0
        assert round(change, 9) == 5.0
        assert MomentumEngine.qualifies(change) is True

    def test_raw_change_is_never_rounded_in_calculation(self):
        change = MomentumEngine.linear_change(105.0000000004, 100.0)
        assert change is not None
        assert change > 5.0
        assert abs(change - 5.0000000004) < 1e-10

    def test_100_to_110_is_plus_10(self):
        change = MomentumEngine.linear_change(110.0, 100.0)
        assert round(change, 3) == 10.000
        assert MomentumEngine.qualifies(change) is True

    def test_200_to_210_does_not_qualify(self):
        change = MomentumEngine.linear_change(210.0, 200.0)
        assert round(change, 3) == 5.000
        assert MomentumEngine.qualifies(change) is False

    def test_10_to_9_is_minus_10(self):
        change = MomentumEngine.linear_change(9.0, 10.0)
        assert round(change, 3) == -10.000
        assert MomentumEngine.qualifies(change) is False


class TestLinearChangeGuardrails:
    def test_none_inputs_give_none(self):
        assert MomentumEngine.linear_change(None, 100.0) is None
class TestSpotHistory:
    async def test_records_sample_and_finds_anchor(self, spot_history, db):
        now = 20_000
        await spot_history.record("spot", "BTCUSDT", now - HOUR, 100.0)
        anchor = await spot_history.find_anchor("spot", "BTCUSDT", now)
        assert anchor == 100.0

    async def test_anchor_within_tolerance(self, spot_history):
        now = 20_000
        # 50s late is within +/-90s tolerance
        await spot_history.record("spot", "BTCUSDT", now - HOUR + 50, 101.0)
        assert await spot_history.find_anchor("spot", "BTCUSDT", now) == 101.0

    async def test_out_of_tolerance_anchor_not_found(self, spot_history):
        now = 20_000
        # 100s late exceeds +/-90s tolerance
        await spot_history.record("spot", "BTCUSDT", now - HOUR + 100, 101.0)
        assert await spot_history.find_anchor("spot", "BTCUSDT", now) is None

    async def test_downsampling_writes_one_sample_per_minute(self, spot_history, db):
        inserted1 = await spot_history.record("spot", "BTCUSDT", 1_000, 100.0)
        inserted2 = await spot_history.record("spot", "BTCUSDT", 1_030, 101.0)
        inserted3 = await spot_history.record("spot", "BTCUSDT", 1_061, 102.0)
        assert inserted1 is True
        assert inserted2 is False  # within 60s window
        assert inserted3 is True  # beyond 60s window
        count = await PriceSampleRepository(db).count()
        assert count == 2

    async def test_history_survives_restart(self, db):
        now = 50_000
        first = SpotHistory(PriceSampleRepository(db))
        await first.record("spot", "BTCUSDT", now - HOUR, 100.0)
        # Simulate application restart: a brand-new SpotHistory instance.
        second = SpotHistory(PriceSampleRepository(db))
        anchor = await second.find_anchor("spot", "BTCUSDT", now)
        assert anchor == 100.0

    async def test_prune_removes_old_samples(self, spot_history, db):
        now = 100_000
        await spot_history.record("spot", "BTCUSDT", now - 10_000, 1.0)  # old
        await spot_history.record("spot", "BTCUSDT", now - 1_000, 2.0)  # fresh
        deleted = await spot_history.prune(now, retention_seconds=7_200)
        assert deleted == 1
        count = await PriceSampleRepository(db).count()
        assert count == 1


class TestSpotMomentumEvaluation:
    async def test_warming_up_without_history(self, momentum):
        now = 20_000
        value = await momentum.evaluate(
            spot_inst(),
            Ticker(category="spot", symbol="BTCUSDT", last_price=105.0),
            now,
        )
        assert value.status == STATUS_WARMING_UP
        assert value.change_1h is None

    async def test_computes_change_from_persisted_anchor(self, momentum, spot_history):
        now = 20_000
        await spot_history.record("spot", "BTCUSDT", now - HOUR, 100.0)
        value = await momentum.evaluate(
            spot_inst(),
            Ticker(category="spot", symbol="BTCUSDT", last_price=105.0),
            now,
        )
        assert value.status == STATUS_OK
        assert value.change_1h is not None
        assert MomentumEngine.qualifies(value.change_1h) is False

    async def test_qualifying_spot_market(self, momentum, spot_history):
        now = 20_000
        await spot_history.record("spot", "BTCUSDT", now - HOUR, 100.0)
        value = await momentum.evaluate(
            spot_inst(),
            Ticker(category="spot", symbol="BTCUSDT", last_price=106.0),
            now,
        )
        assert value.status == STATUS_OK
        assert MomentumEngine.qualifies(value.change_1h) is True

    async def test_spot_change_not_pre_rounded(self, momentum, spot_history):
        """End-to-end: prices -> change -> qualification without rounding."""
        now = 20_000
        await spot_history.record("spot", "BTCUSDT", now - HOUR, 100.0)
        value = await momentum.evaluate(
            spot_inst(),
            Ticker(category="spot", symbol="BTCUSDT", last_price=105.0000000004),
            now,
        )
        assert value.status == STATUS_OK
        assert value.change_1h > 5.0
        assert MomentumEngine.qualifies(value.change_1h) is True

    async def test_linear_end_to_end_not_pre_rounded(self, momentum):
        """End-to-end: ticker prices -> change -> qualification."""
        value = await momentum.evaluate(
            linear_inst(),
            Ticker(
                category="linear",
                symbol="BTCUSDT",
                last_price=105.0000000004,
                prev_price_1h=100.0,
            ),
            20_000,
        )
        assert value.status == STATUS_OK
        assert value.change_1h > 5.0
        assert MomentumEngine.qualifies(value.change_1h) is True


class TestLinearMomentumEvaluation:
    async def test_linear_change_computed(self, momentum):
        now = 20_000
        value = await momentum.evaluate(
            linear_inst(),
            Ticker(
                category="linear",
                symbol="BTCUSDT",
                last_price=110.0,
                prev_price_1h=100.0,
            ),
            now,
        )
        assert value.status == STATUS_OK
        assert round(value.change_1h, 3) == 10.0

    async def test_linear_no_data_without_prev_price(self, momentum):
        value = await momentum.evaluate(
            linear_inst(),
            Ticker(category="linear", symbol="BTCUSDT", last_price=110.0),
            20_000,
        )
        assert value.status == STATUS_NO_DATA
        assert value.change_1h is None

    async def test_linear_zero_price_no_data(self, momentum):
        value = await momentum.evaluate(
            linear_inst(),
            Ticker(category="linear", symbol="BTCUSDT", last_price=0.0),
            20_000,
        )
        assert value.status == STATUS_NO_DATA
        assert value.change_1h is None


class TestMomentumEvaluator:
    async def test_evaluates_supported_trading_instruments(self, db):
        config = make_settings(
            database_path=":memory:",
            enable_spot=True,
            enable_linear_usdt=True,
            enable_linear_usdc=False,
        )
        registry = InstrumentRegistry(InstrumentRepository(db))
        await registry.reconcile(
            [
                mk("spot", "BTCUSDT", "BTC"),
                mk("linear", "BTCUSDT", "BTC", settle="USDT"),
                mk("linear", "BTCUSDC", "BTC", settle="USDC"),
                mk("linear", "PREUSDT", "PRE", status="PreLaunch", pre=True),
            ]
        )
        engine = PriceEngine(config)
        engine.update_tickers(
            [
                Ticker(category="spot", symbol="BTCUSDT", last_price=105.0),
                Ticker(
                    category="linear",
                    symbol="BTCUSDT",
                    last_price=110.0,
                    prev_price_1h=100.0,
                ),
                Ticker(
                    category="linear",
                    symbol="BTCUSDC",
                    last_price=110.0,
                    prev_price_1h=100.0,
                ),
                Ticker(
                    category="linear",
                    symbol="PREUSDT",
                    last_price=110.0,
                    prev_price_1h=100.0,
                ),
            ]
        )
        spot_history = SpotHistory(PriceSampleRepository(db))
        momentum = MomentumEngine(config, spot_history)
        evaluator = MomentumEvaluator(registry, engine, momentum, config)
        values = await evaluator.evaluate_all(now=20_000)
        symbols = {v.symbol for v in values}
        # PreLaunch skipped; USDC linear excluded because disabled.
        assert symbols == {"BTCUSDT"}
        assert len(values) == 2  # spot BTCUSDT + linear BTCUSDT
        assert MomentumEngine.linear_change(100.0, None) is None
        assert MomentumEngine.linear_change(None, None) is None

    def test_non_positive_prices_give_none(self):
        assert MomentumEngine.linear_change(0.0, 100.0) is None
        assert MomentumEngine.linear_change(-1.0, 100.0) is None
        assert MomentumEngine.linear_change(100.0, 0.0) is None
        assert MomentumEngine.linear_change(100.0, -1.0) is None

    def test_qualifies_none_is_false(self):
        assert MomentumEngine.qualifies(None) is False