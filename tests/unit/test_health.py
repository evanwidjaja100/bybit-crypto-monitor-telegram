"""Phase 12 - health monitor tests."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from app.bybit.models import Instrument
from app.health.monitor import HealthMonitor, HealthState
from tests.conftest import make_settings


class FakeWsClient:
    def __init__(self, connected: bool, last_message_at: float = 0.0) -> None:
        self.connected = connected
        self.last_message_at = last_message_at


class FakeWsManager:
    def __init__(self, spot, linear) -> None:
        self.clients = {"spot": spot, "linear": linear}


class FakeDiscovery:
    def __init__(self, last_success_at) -> None:
        self.last_success_at = last_success_at


class FakeTelegram:
    def __init__(self, last_success_at=None, last_error_at=None) -> None:
        self.last_success_at = last_success_at
        self.last_error_at = last_error_at


class FakeDispatcher:
    def __init__(self, queue) -> None:
        self.queue = queue


class FakeDatabase:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy

    async def fetchone(self, sql: str):
        if not self.healthy:
            raise RuntimeError("database is gone")
        return {"one": 1}


class FakeRegistry:
    def __init__(self, instruments) -> None:
        self.repo = FakeRepo(instruments)


class FakeRepo:
    def __init__(self, instruments) -> None:
        self.instruments = instruments

    async def load_all(self):
        return self.instruments


def build_components(**overrides) -> dict:
    now = int(time.time())
    defaults = dict(
        discovery=FakeDiscovery(now - 10),
        ws_manager=FakeWsManager(
            FakeWsClient(True, time.monotonic() - 5),
            FakeWsClient(True, time.monotonic() - 3),
        ),
        telegram=FakeTelegram(last_success_at=now - 60),
        dispatcher=FakeDispatcher(asyncio.Queue()),
        database=FakeDatabase(),
        registry=FakeRegistry(
            {
                ("spot", "BTCUSDT"): Instrument(
                    category="spot", symbol="BTCUSDT", base_coin="BTC", status="Trading"
                ),
                ("linear", "BTCUSDT"): Instrument(
                    category="linear",
                    symbol="BTCUSDT",
                    base_coin="BTC",
                    settle_coin="USDT",
                    status="Trading",
                ),
                ("linear", "SOLUSDT"): Instrument(
                    category="linear",
                    symbol="SOLUSDT",
                    base_coin="SOL",
                    settle_coin="USDC",
                    status="Trading",
                ),
                ("linear", "OLDUSDT"): Instrument(
                    category="linear",
                    symbol="OLDUSDT",
                    base_coin="OLD",
                    settle_coin="USDT",
                    status="Removed",
                ),
            }
        ),
    )
    defaults.update(overrides)
    return defaults


def make_monitor(config, **overrides) -> HealthMonitor:
    return HealthMonitor(config, **build_components(**overrides))


class TestSnapshot:
    async def test_healthy_state_collects_everything(self, config):
        now = int(time.time())
        monitor = make_monitor(
            config,
            discovery=FakeDiscovery(now - 10),
            telegram=FakeTelegram(last_success_at=now - 60),
        )
        monitor.set_qualifying_count(2)
        state = await monitor.snapshot(now=now)
        assert state.rest_healthy is True
        assert state.spot_ws_connected is True
        assert state.linear_ws_connected is True
        assert state.telegram_healthy is True
        assert state.database_healthy is True
        assert state.last_discovery_age == 10
        assert state.last_spot_ticker_age is not None
        assert state.last_linear_ticker_age is not None
        assert state.spot_instrument_count == 1
        assert state.linear_usdt_count == 1
        assert state.linear_usdc_count == 1
        assert state.qualifying_coin_count == 2
        assert state.telegram_queue_depth == 0
        assert monitor.last_state is state

    async def test_unhealthy_components_flagged(self, config):
        monitor = make_monitor(
            config,
            discovery=FakeDiscovery(None),
            ws_manager=FakeWsManager(FakeWsClient(False), FakeWsClient(False)),
            telegram=FakeTelegram(last_success_at=None, last_error_at=1_000),
            database=FakeDatabase(healthy=False),
        )
        state = await monitor.snapshot(now=1_700_000_000)
        assert state.rest_healthy is False
        assert state.spot_ws_connected is False
        assert state.linear_ws_connected is False
        assert state.telegram_healthy is False
        assert state.database_healthy is False
        assert "rest unhealthy" in state.notes
        assert "spot ws disconnected" in state.notes
        assert "linear ws disconnected" in state.notes
        assert "telegram unhealthy" in state.notes
        assert "database unhealthy" in state.notes

    async def test_stale_discovery_is_rest_unhealthy(self, config):
        monitor = make_monitor(config, discovery=FakeDiscovery(1_000))
        state = await monitor.snapshot(now=1_700_000_000)
        assert state.rest_healthy is False

    async def test_telegram_recovered_after_error(self, config):
        monitor = make_monitor(
            config,
            telegram=FakeTelegram(last_success_at=2_000, last_error_at=1_000),
        )
        state = await monitor.snapshot(now=1_700_000_000)
        assert state.telegram_healthy is True


class TestSummary:
    def test_format_summary_renders_block(self):
        state = HealthState(
            collected_at=1_700_000_000,
            rest_healthy=True,
            spot_ws_connected=True,
            linear_ws_connected=False,
            telegram_healthy=True,
            database_healthy=True,
            last_spot_ticker_age=5,
            last_discovery_age=48,
            spot_instrument_count=300,
            linear_usdt_count=200,
            linear_usdc_count=50,
            qualifying_coin_count=2,
            telegram_queue_depth=0,
        )
        text = HealthMonitor.format_summary(state)
        assert text.startswith("HEALTH")
        assert "Spot instruments: 300" in text
        assert "Linear USDT: 200" in text
        assert "Linear USDC: 50" in text
        assert "Spot WS: connected (5s last msg)" in text
        assert "Linear WS: disconnected" in text
        assert "REST: healthy" in text
        assert "Telegram: healthy" in text
        assert "Database: healthy" in text
        assert "Qualifying coins: 2" in text
        assert "Telegram queue: 0" in text
        assert "Last discovery: 48s ago" in text

    def test_format_summary_none_ages(self):
        state = HealthState(
            collected_at=0,
            rest_healthy=False,
            spot_ws_connected=False,
            linear_ws_connected=False,
            telegram_healthy=False,
            database_healthy=False,
        )
        text = HealthMonitor.format_summary(state)
        assert "Spot WS: disconnected" in text
        assert "Last discovery: n/a ago" in text


class TestLoop:
    async def test_run_forever_logs_summary_at_interval(self, config, caplog):
        config = make_settings(**{
            k: v for k, v in vars(config).items() if k != "health_summary_seconds"
        }, health_summary_seconds=0.05)
        monitor = make_monitor(config)
        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run_forever(stop))
        try:
            with caplog.at_level(logging.INFO):
                await asyncio.sleep(0.25)
            assert caplog.text.count("event=health_summary") >= 3
            assert "HEALTH" in caplog.text
        finally:
            stop.set()
            await task
