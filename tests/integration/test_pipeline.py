"""Phase 13 - end-to-end pipeline integration tests.

Wires the real components (registry, poll service, momentum engine,
aggregator, state machine, dispatcher) with a fake Bybit REST API and
asserts the locked business scenarios of plan section 19.
"""

from __future__ import annotations

import pytest

from app.alerts.dispatcher import AlertDispatcher
from app.alerts.service import AlertService
from app.alerts.state_machine import STATE_ACTIVE_RANGE, STATE_OVER_RANGE, AlertStateMachine
from app.bybit.rest import BybitError
from app.market.deduplication import aggregate_qualifying
from app.market.discovery import InstrumentDiscovery, InstrumentRegistry
from app.market.momentum import MomentumEngine, MomentumEvaluator, SpotHistory
from app.market.price_engine import PriceEngine, TickerPollService
from app.persistence.repository import (
    AlertStateRepository,
    InstrumentRepository,
    PriceSampleRepository,
    Repository,
)
from tests.conftest import make_settings
from tests.unit.test_discovery import mk
from tests.unit.test_price_engine import ticker


class FakeRest:
    """In-memory Bybit REST with failure injection."""

    def __init__(self, instruments=None, tickers=None) -> None:
        self.instruments = instruments or []
        self.tickers = tickers or []
        self.fail_tickers = False
        self.fail_instruments = False

    async def get_spot_instruments(self, limit=1000):
        if self.fail_instruments:
            raise BybitError("http_500")
        return [i for i in self.instruments if i.category == "spot"]

    async def get_linear_instruments(self, status="Trading", limit=1000):
        if self.fail_instruments:
            raise BybitError("http_500")
        return [i for i in self.instruments if i.category == "linear"]

    async def get_spot_tickers(self):
        if self.fail_tickers:
            raise BybitError("http_500")
        return [t for t in self.tickers if t.category == "spot"]

    async def get_linear_tickers(self):
        if self.fail_tickers:
            raise BybitError("http_500")
        return [t for t in self.tickers if t.category == "linear"]


class FakeTelegram:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, text: str) -> None:
        self.sent.append(text)


def pipeline_config():
    return make_settings(
        database_path=":memory:",
        telegram_bot_token="123456789:AAfaketokenvaluefortests_only",
        telegram_chat_id="-1001234567890",
        alert_debounce_seconds=0,
        immediate_transition_alerts=True,
        hourly_active_alerts=False,
        composition_change_alerts=False,
        enable_spot=True,
        enable_linear_usdt=True,
        enable_linear_usdc=True,
    )


def lin(symbol: str, base: str, last: float, prev1h: float):
    return ticker("linear", symbol, last, prev1h=prev1h)


async def run_pipeline(db, rest: FakeRest, now: int) -> dict:
    """One full cycle: discovery -> poll -> momentum -> aggregate."""
    config = pipeline_config()
    registry = InstrumentRegistry(InstrumentRepository(db))
    if not await registry.repo.count_active():
        await registry.reconcile(rest.instruments, now=now)

    price_engine = PriceEngine(config)
    await TickerPollService(rest, registry, price_engine, config).poll_once(now=now)

    momentum = MomentumEngine(config, SpotHistory(PriceSampleRepository(db)))
    values = await MomentumEvaluator(
        registry, price_engine, momentum, config
    ).evaluate_all(now=now)
    return {
        "config": config,
        "registry": registry,
        "price_engine": price_engine,
        "values": values,
        "qualifying": aggregate_qualifying(
            values, config.alert_threshold_percent
        ),
    }


async def run_alert_cycle(db, ctx: dict, now: int) -> dict:
    """State machine + service over the aggregated qualifying set."""
    config = ctx["config"]
    dispatcher = AlertDispatcher(FakeTelegram(), Repository(db), config)
    service = AlertService(
        AlertStateMachine(config, AlertStateRepository(db)), dispatcher, config
    )
    decision = await service.process(ctx["qualifying"], now)
    pending = await Repository(db).list_notifications(limit=10, status="pending")
    return {
        "decision": decision,
        "dispatcher": dispatcher,
        "pending": pending,
    }


class TestCriticalAlertScenario:
    """Plan 19.3: 3 coins alert, 4 coins suppressed, back to 3 alerts."""

    async def test_three_four_three_transitions_end_to_end(self, db):
        instruments = [
            mk("linear", "BTCUSDT", "BTC", settle="USDT"),
            mk("linear", "ETHUSDT", "ETH", settle="USDT"),
            mk("linear", "SOLUSDT", "SOL", settle="USDT"),
            mk("linear", "DOGEUSDT", "DOGE", settle="USDT"),
            mk("spot", "BTCUSDT", "BTC"),
        ]
        tickers = {
            "phase1": [
                lin("BTCUSDT", "BTC", 63600.0, 60000.0),
                lin("ETHUSDT", "ETH", 64200.0, 60000.0),
                lin("SOLUSDT", "SOL", 64800.0, 60000.0),
                ticker("spot", "BTCUSDT", 60000.0),
            ],
            "phase2": [
                *[
                    lin("BTCUSDT", "BTC", 63600.0, 60000.0),
                    lin("ETHUSDT", "ETH", 64200.0, 60000.0),
                    lin("SOLUSDT", "SOL", 64800.0, 60000.0),
                    lin("DOGEUSDT", "DOGE", 63300.0, 60000.0),
                ],
                ticker("spot", "BTCUSDT", 60000.0),
            ],
            "phase3": [
                lin("BTCUSDT", "BTC", 63600.0, 60000.0),
                lin("SOLUSDT", "SOL", 64800.0, 60000.0),
                lin("DOGEUSDT", "DOGE", 63300.0, 60000.0),
                lin("ETHUSDT", "ETH", 60000.0, 60000.0),  # dropped below threshold
                ticker("spot", "BTCUSDT", 60000.0),
            ],
        }

        rest = FakeRest(instruments=instruments, tickers=tickers["phase1"])
        ctx = await run_pipeline(db, rest, now=1_700_000_000)
        assert ctx["qualifying"].count == 3
        assert ctx["qualifying"].base_coins == ("BTC", "ETH", "SOL")

        result = await run_alert_cycle(db, ctx, now=1_700_000_000)
        assert result["decision"].live_transition is True
        assert result["decision"].state == STATE_ACTIVE_RANGE
        assert len(result["pending"]) == 1

        rest.tickers = tickers["phase2"]
        ctx = await run_pipeline(db, rest, now=1_700_000_100)
        assert ctx["qualifying"].count == 4
        result = await run_alert_cycle(db, ctx, now=1_700_000_100)
        assert result["decision"].state == STATE_OVER_RANGE
        assert result["decision"].live_transition is False
        assert len(result["pending"]) == 1  # no new alert

        rest.tickers = tickers["phase3"]
        ctx = await run_pipeline(db, rest, now=1_700_000_200)
        assert ctx["qualifying"].count == 3
        assert ctx["qualifying"].base_coins == ("BTC", "DOGE", "SOL")
        result = await run_alert_cycle(db, ctx, now=1_700_000_200)
        assert result["decision"].live_transition is True
        assert result["decision"].state == STATE_ACTIVE_RANGE
        assert len(result["pending"]) == 2  # fresh alert for the new group


class TestCrossMarketDuplicate:
    """Plan 19.4: XYZ spot + USDT + USDC collapse to one representative."""

    async def test_representative_selected_across_categories(self, db):
        instruments = [
            mk("spot", "XYZUSDT", "XYZ"),
            mk("linear", "XYZUSDT", "XYZ", settle="USDT"),
            mk("linear", "XYZUSDC", "XYZ", settle="USDC"),
        ]
        rest = FakeRest(
            instruments=instruments,
            tickers=[
                ticker("spot", "XYZUSDT", 106.0),
                lin("XYZUSDT", "XYZ", 109.0, 100.0),
                lin("XYZUSDC", "XYZ", 108.0, 100.0),
            ],
        )
        ctx = await run_pipeline(db, rest, now=1_700_000_000)
        assert ctx["qualifying"].count == 1
        rep = ctx["qualifying"].representatives[0]
        assert rep.base_coin == "XYZ"
        assert rep.representative.symbol == "XYZUSDT"
        assert rep.representative.change_1h == pytest.approx(9.0)


class TestChaosRecovery:
    """Plan 19.7: a failing fetch must not poison later cycles."""

    async def test_pipeline_recovers_after_rest_failure(self, db):
        instruments = [
            mk("linear", "BTCUSDT", "BTC", settle="USDT"),
            mk("spot", "BTCUSDT", "BTC"),
        ]
        rest = FakeRest(
            instruments=instruments,
            tickers=[lin("BTCUSDT", "BTC", 63600.0, 60000.0)],
        )
        ctx = await run_pipeline(db, rest, now=1_700_000_000)
        assert ctx["qualifying"].count == 1

        rest.fail_tickers = True
        with pytest.raises(BybitError):
            await run_pipeline(db, rest, now=1_700_000_100)

        rest.fail_tickers = False
        ctx = await run_pipeline(db, rest, now=1_700_000_200)
        assert ctx["qualifying"].count == 1
        result = await run_alert_cycle(db, ctx, now=1_700_000_200)
        assert result["decision"].live_transition is True
        assert len(result["pending"]) == 1

    async def test_discovery_recovers_after_instrument_failure(self, db):
        instruments = [
            mk("linear", "BTCUSDT", "BTC", settle="USDT"),
            mk("linear", "ETHUSDT", "ETH", settle="USDT"),
        ]
        rest = FakeRest(
            instruments=instruments,
            tickers=[
                lin("BTCUSDT", "BTC", 63600.0, 60000.0),
                lin("ETHUSDT", "ETH", 64200.0, 60000.0),
            ],
        )
        registry = InstrumentRegistry(InstrumentRepository(db))
        await registry.reconcile(instruments, now=1_700_000_000)
        discovery = InstrumentDiscovery(rest, registry, pipeline_config())

        rest.fail_instruments = True
        with pytest.raises(BybitError):
            await discovery.discover_once(now=1_700_000_100)

        rest.fail_instruments = False
        result = await discovery.discover_once(now=1_700_000_200)
        assert result.instrument_count == 2
        assert discovery.last_success_at == 1_700_000_200
