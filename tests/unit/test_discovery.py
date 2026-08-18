"""Phase 3 - instrument registry & discovery tests."""

from __future__ import annotations

import pytest

from app.bybit.models import Instrument
from app.market.discovery import (
    InstrumentDiscovery,
    InstrumentRegistry,
    STATUS_PRELAUNCH,
    STATUS_TRADING,
)
from app.persistence.repository import InstrumentRepository
from tests.conftest import make_settings


def mk(
    category: str,
    symbol: str,
    base: str | None = None,
    status: str = "Trading",
    settle: str | None = None,
    pre: bool = False,
) -> Instrument:
    return Instrument(
        category=category,
        symbol=symbol,
        base_coin=base or symbol,
        quote_coin=settle or "USDT",
        settle_coin=settle,
        status=status,
        is_pre_listing=pre,
    )


@pytest.fixture
def registry(db):
    return InstrumentRegistry(InstrumentRepository(db))


class TestFirstStartRules:
    async def test_first_startup_seeds_silently(self, registry, db):
        result = await registry.reconcile(
            [mk("linear", "BTCUSDT", "BTC"), mk("spot", "BTCUSDT", "BTC")],
            now=1_000,
        )
        assert result.first_run is True
        assert result.instrument_count == 2
        assert result.events == []  # silent seed
        loaded = await InstrumentRepository(db).load_all()
        assert len(loaded) == 2
        row = await db.fetchone(
            "SELECT first_seen_at, last_seen_at FROM instruments "
            "WHERE category='linear' AND symbol='BTCUSDT'"
        )
        assert row["first_seen_at"] == 1_000
        assert row["last_seen_at"] == 1_000

    async def test_restart_does_not_fake_new_listings(self, registry):
        fetched = [mk("linear", "BTCUSDT", "BTC"), mk("linear", "ETHUSDT", "ETH")]
        await registry.reconcile(fetched, now=1_000)
        result = await registry.reconcile(fetched, now=2_000)  # restart
        assert result.first_run is False
        assert result.events == []  # no false new-listing events

    async def test_repeated_restarts_never_fake_new_events(self, registry):
        fetched = [mk("linear", "BTCUSDT", "BTC")]
        await registry.reconcile(fetched, now=1_000)
        for now in (2_000, 3_000, 4_000):
            result = await registry.reconcile(fetched, now=now)
            assert result.events == []
            assert result.first_run is False

    async def test_first_seen_is_preserved_on_upsert(self, registry, db):
        await registry.reconcile([mk("linear", "BTCUSDT", "BTC")], now=1_000)
        await registry.reconcile([mk("linear", "BTCUSDT", "BTC")], now=2_000)
        row = await db.fetchone(
            "SELECT first_seen_at, last_seen_at FROM instruments "
            "WHERE category='linear' AND symbol='BTCUSDT'"
        )
        assert row["first_seen_at"] == 1_000
        assert row["last_seen_at"] == 2_000


class TestDiscoveryEvents:
    async def test_newly_appearing_market_generates_event(self, registry):
        await registry.reconcile([mk("linear", "BTCUSDT", "BTC")], now=1_000)
        result = await registry.reconcile(
            [mk("linear", "BTCUSDT", "BTC"), mk("linear", "SOLUSDT", "SOL")],
            now=2_000,
        )
        new_events = [e for e in result.events if e.event == "new"]
        assert len(new_events) == 1
        assert new_events[0].symbol == "SOLUSDT"
        assert new_events[0].category == "linear"

    async def test_prelaunch_to_trading_transition_detected(self, registry):
        await registry.reconcile(
            [mk("linear", "XYZUSDT", "XYZ", status=STATUS_PRELAUNCH, pre=True)],
            now=1_000,
        )
        result = await registry.reconcile(
            [mk("linear", "XYZUSDT", "XYZ", status=STATUS_TRADING, settle="USDT")],
            now=2_000,
        )
        transitions = [e for e in result.events if e.event == "status_transition"]
        assert len(transitions) == 1
        assert transitions[0].old_status == STATUS_PRELAUNCH
        assert transitions[0].new_status == STATUS_TRADING

    async def test_direct_unknown_to_trading_via_new_event(self, registry):
        await registry.reconcile([mk("linear", "BTCUSDT", "BTC")], now=1_000)
        result2 = await registry.reconcile(
            [mk("linear", "BTCUSDT", "BTC"), mk("linear", "LTCUSDT", "LTC")],
            now=2_000,
        )
        # LTC appeared directly as Trading -> "new" event
        assert any(e.event == "new" and e.symbol == "LTCUSDT" for e in result2.events)

    async def test_removed_market_marked_and_not_corrupting(self, registry, db):
        await registry.reconcile([mk("linear", "BTCUSDT", "BTC")], now=1_000)
        result = await registry.reconcile([], now=2_000)
        removed = [e for e in result.events if e.event == "removed"]
        assert len(removed) == 1
        row = await db.fetchone(
            "SELECT status FROM instruments WHERE category='linear' AND symbol='BTCUSDT'"
        )
        assert row["status"] == "Removed"
        # Reappearance reactivates cleanly.
        result2 = await registry.reconcile([mk("linear", "BTCUSDT", "BTC")], now=3_000)
        transitions = [e for e in result2.events if e.event == "status_transition"]
        assert transitions and transitions[0].old_status == "Removed"
        row2 = await db.fetchone(
            "SELECT status FROM instruments WHERE category='linear' AND symbol='BTCUSDT'"
        )
        assert row2["status"] == "Trading"

    async def test_removed_market_does_not_count_as_active(self, registry):
        await registry.reconcile([mk("linear", "BTCUSDT", "BTC")], now=1_000)
        assert await registry.repo.count_active() == 1
        await registry.reconcile([], now=2_000)
        assert await registry.repo.count_active() == 0


class FakeRest:
    def __init__(self, spot=None, trading=None, prelaunch=None, inverse=None):
        self.spot = spot or []
        self.trading = trading or []
        self.prelaunch = prelaunch or []
        self.inverse = inverse or []

    async def get_spot_instruments(self):
        return self.spot

    async def get_linear_instruments(self, status):
        return self.prelaunch if status == STATUS_PRELAUNCH else self.trading

    async def get_inverse_instruments(self):
        return self.inverse


class TestInstrumentDiscovery:
    async def test_discovery_fetches_all_enabled_universes(self, db):
        config = make_settings(
            database_path=":memory:",
            enable_spot=True,
            enable_linear_usdt=True,
            enable_linear_usdc=True,
            enable_inverse=False,
        )
        registry = InstrumentRegistry(InstrumentRepository(db))
        fake = FakeRest(
            spot=[mk("spot", "BTCUSDT", "BTC")],
            trading=[mk("linear", "ETHUSDT", "ETH", settle="USDT")],
            prelaunch=[
                mk("linear", "XYZUSDT", "XYZ", status=STATUS_PRELAUNCH, pre=True)
            ],
        )
        discovery = InstrumentDiscovery(fake, registry, config)
        result = await discovery.discover_once(now=5_000)
        assert result.instrument_count == 3
        assert result.first_run is True
        loaded = await registry.repo.load_all()
        assert len(loaded) == 3

    async def test_prelaunch_discovered_without_restart(self, db):
        config = make_settings(
            database_path=":memory:",
            enable_spot=False,
            enable_linear_usdt=True,
            enable_linear_usdc=False,
            enable_inverse=False,
        )
        registry = InstrumentRegistry(InstrumentRepository(db))
        fake = FakeRest(trading=[mk("linear", "BTCUSDT", "BTC", settle="USDT")])
        discovery = InstrumentDiscovery(fake, registry, config)
        result = await discovery.discover_once(now=1_000)
        assert result.first_run is True

        # A newly added market appears without restart.
        fake.trading.append(mk("linear", "NEWUSDT", "NEW", settle="USDT"))
        result2 = await discovery.discover_once(now=2_000)
        assert any(e.event == "new" for e in result2.events)