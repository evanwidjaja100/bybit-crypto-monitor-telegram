"""Phase 4 - REST price engine MVP tests."""

from __future__ import annotations

import pytest

from app.bybit.models import Ticker
from app.market.discovery import InstrumentRegistry
from app.market.price_engine import PriceEngine, TickerPollService
from app.persistence.repository import InstrumentRepository
from tests.conftest import make_settings
from tests.unit.test_discovery import mk


def ticker(category, symbol, last, prev1h=None, *args, **kwargs) -> Ticker:
    return Ticker(
        category=category,
        symbol=symbol,
        last_price=last,
        prev_price_1h=prev1h,
        *args,
        **kwargs,
    )


@pytest.fixture
def price_engine():
    return PriceEngine(make_settings())


class TestPriceEngineIngestion:
    def test_accepts_valid_rows_and_updates_state(self, price_engine):
        btc = ticker("linear", "BTCUSDT", 65000.0, prev1h=60000.0)
        eth = ticker("spot", "ETHUSDT", 3000.0)
        updated = price_engine.update_tickers([btc, eth])
        assert len(updated) == 2
        assert price_engine.latest_count() == 2
        assert price_engine.received == 2
        assert price_engine.accepted == 2
        assert price_engine.rejected == 0
        assert price_engine.get("linear", "BTCUSDT") is btc
        assert price_engine.get("spot", "ETHUSDT").last_price == 3000.0

    def test_rejects_missing_symbol(self, price_engine):
        bad = Ticker(category="linear", symbol="")
        updated = price_engine.update_tickers([bad])
        assert updated == {}
        assert price_engine.received == 1
        assert price_engine.rejected == 1
        assert price_engine.accepted == 0

    def test_rejects_negative_price(self, price_engine):
        neg = ticker("linear", "BTCUSDT", -1.0)
        price_engine.update_tickers([neg])
        assert price_engine.received == 1
        assert price_engine.rejected == 1
        assert price_engine.accepted == 0
        assert price_engine.get("linear", "BTCUSDT") is None

    def test_zero_price_is_stored_but_available(self, price_engine):
        zero = ticker("spot", "ABCUSDT", 0.0)
        price_engine.update_tickers([zero])
        assert price_engine.accepted == 1
        assert price_engine.get("spot", "ABCUSDT").last_price == 0.0

    def test_overwrite_same_symbol(self, price_engine):
        price_engine.update_tickers([ticker("linear", "BTCUSDT", 100.0)])
        price_engine.update_tickers([ticker("linear", "BTCUSDT", 200.0)])
        assert price_engine.latest_count() == 1
        assert price_engine.get("linear", "BTCUSDT").last_price == 200.0
        assert price_engine.received == 2


class TestDeltaMerging:
    def test_delta_updates_only_provided_fields(self, price_engine):
        price_engine.update_tickers(
            [ticker("linear", "BTCUSDT", 65000.0, prev1h=60000.0)]
        )
        price_engine.update_from_delta(
            "linear", "BTCUSDT", {"lastPrice": "65100.0", "ts": "1700000000000"}
        )
        updated = price_engine.get("linear", "BTCUSDT")
        assert updated.last_price == 65100.0
        # Fields not in the delta are preserved.
        assert updated.prev_price_1h == 60000.0

    def test_delta_without_snapshot_is_ignored_gracefully(self, price_engine):
        price_engine.update_from_delta("linear", "GHOSTUSDT", {"lastPrice": "1.0"})
        assert price_engine.get("linear", "GHOSTUSDT") is None
        assert price_engine.latest_count() == 0

    def test_delta_merges_and_converts_percent(self, price_engine):
        price_engine.update_tickers([ticker("linear", "BTCUSDT", 65000.0)])
        price_engine.update_from_delta(
            "linear",
            "BTCUSDT",
            {"lastPrice": "70000.0", "price24hPcnt": "0.0750", "ts": "1700000000000"},
        )
        updated = price_engine.get("linear", "BTCUSDT")
        assert updated.last_price == 70000.0
        assert updated.change_24h == 7.5

    def test_delta_snapshot_never_replaced_fully(self, price_engine):
        price_engine.update_tickers(
            [ticker("linear", "BTCUSDT", 65000.0, prev1h=60000.0, funding_rate=0.0001)]
        )
        price_engine.update_from_delta("linear", "BTCUSDT", {"lastPrice": "65100.0"})
        updated = price_engine.get("linear", "BTCUSDT")
        # funding_rate preserved from the original snapshot
        assert updated.funding_rate == 0.0001


class FakeTickerRest:
    def __init__(self, spot=None, linear=None):
        self.spot = spot or []
        self.linear = linear or []

    async def get_spot_tickers(self):
        return self.spot

    async def get_linear_tickers(self):
        return self.linear


class TestTickerPollService:
    async def test_poll_fetches_and_fills_counts(self, db):
        config = make_settings(
            database_path=":memory:",
            enable_spot=True,
            enable_linear_usdt=True,
            enable_linear_usdc=True,
        )
        registry = InstrumentRegistry(InstrumentRepository(db))
        # Seed registry: 1 spot, 1 linear USDT, 1 linear USDC.
        await registry.reconcile(
            [
                mk("spot", "BTCUSDT", "BTC"),
                mk("linear", "BTCUSDT", "BTC", settle="USDT"),
                mk("linear", "BTCUSDC", "BTC", settle="USDC"),
            ]
        )
        engine = PriceEngine(config)
        fake = FakeTickerRest(
            spot=[ticker("spot", "BTCUSDT", 65000.0)],
            linear=[
                ticker("linear", "BTCUSDT", 65000.0, prev1h=60000.0),
                ticker("linear", "BTCUSDC", 64900.0, prev1h=60000.0),
            ],
        )
        service = TickerPollService(fake, registry, engine, config)
        summary = await service.poll_once()

        assert summary.received == 3
        assert summary.accepted == 3
        assert summary.rejected == 0
        assert summary.updated_count == 3
        assert summary.spot_instruments == 1
        assert summary.linear_usdt == 1
        assert summary.linear_usdc == 1
        assert engine.latest_count() == 3

    async def test_poll_metrics_track_all_universes(self, db):
        config = make_settings(
            database_path=":memory:",
            enable_spot=True,
            enable_linear_usdt=True,
            enable_linear_usdc=True,
        )
        registry = InstrumentRegistry(InstrumentRepository(db))
        await registry.reconcile(
            [
                mk("spot", "BTCUSDT", "BTC"),
                mk("spot", "ETHUSDT", "ETH"),
                mk("linear", "BTCUSDT", "BTC", settle="USDT"),
                mk("linear", "ETHUSDT", "ETH", settle="USDT"),
                mk("linear", "BTCUSDC", "BTC", settle="USDC"),
            ]
        )
        engine = PriceEngine(config)
        fake = FakeTickerRest(
            spot=[ticker("spot", "BTCUSDT", 1.0), ticker("spot", "ETHUSDT", 1.0)],
            linear=[
                ticker("linear", "BTCUSDT", 1.0),
                ticker("linear", "ETHUSDT", 1.0),
                ticker("linear", "BTCUSDC", 1.0),
            ],
        )
        service = TickerPollService(fake, registry, engine, config)
        summary = await service.poll_once()
        assert summary.spot_instruments == 2
        assert summary.linear_usdt == 2
        assert summary.linear_usdc == 1

    async def test_on_tickers_callback_invoked(self, db):
        config = make_settings(database_path=":memory:", enable_spot=True)
        registry = InstrumentRegistry(InstrumentRepository(db))
        engine = PriceEngine(config)
        fake = FakeTickerRest(spot=[ticker("spot", "BTCUSDT", 65000.0)])
        seen = []

        async def on_tickers(tickers):
            seen.extend(tickers)

        service = TickerPollService(fake, registry, engine, config, on_tickers=on_tickers)
        await service.poll_once()
        assert len(seen) == 1