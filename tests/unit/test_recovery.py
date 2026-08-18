"""Phase 11 - recovery tests.

Every scenario simulates a process restart by closing the database
connection and reopening the same SQLite file in a fresh component
instance, mirroring how the real bot is relaunched.
"""

from __future__ import annotations

import asyncio
import time

import aiosqlite
import pytest

from app.alerts.state_machine import (
    STATE_ACTIVE_RANGE,
    STATE_EMPTY,
    STATE_OVER_RANGE,
    AlertStateMachine,
)
from app.bybit.models import Instrument, Ticker
from app.market.deduplication import QualifyingSet, aggregate_qualifying
from app.market.discovery import DiscoveryResult, InstrumentRegistry, RegistryEvent
from app.market.listing import ListingTracker
from app.market.momentum import MomentumEngine, MomentumValue, SpotHistory
from app.persistence.database import Database
from app.persistence.migrations import apply_migrations
from app.persistence.repository import (
    AlertStateRepository,
    InstrumentRepository,
    ListingEventRepository,
    PriceSampleRepository,
)
from tests.conftest import make_settings


def open_db(path: str) -> Database:
    db = Database(path)
    return db


async def fresh_db(path: str) -> Database:
    db = open_db(path)
    await db.connect()
    await apply_migrations(db)
    await db.commit()
    return db


def qualify(*coins: str) -> QualifyingSet:
    values = [
        MomentumValue(
            category="linear",
            symbol=f"{coin}USDT",
            base_coin=coin,
            change_1h=6.0,
            status="OK",
            last_price=106.0,
        )
        for coin in coins
    ]
    return aggregate_qualifying(values, threshold=5.0)


class TestRestartAfterAlert:
    async def test_no_duplicate_alert_after_restart(self, tmp_path):
        path = str(tmp_path / "state.sqlite")
        config = make_settings(
            alert_debounce_seconds=0,
            immediate_transition_alerts=True,
            hourly_active_alerts=True,
        )

        db = await fresh_db(path)
        machine = AlertStateMachine(config, AlertStateRepository(db))
        decision = await machine.update(qualify("BTC"), now=1_700_000_000)
        assert decision.live_transition is True
        await db.close()

        db = await fresh_db(path)
        machine = AlertStateMachine(config, AlertStateRepository(db))
        decision = await machine.update(qualify("BTC"), now=1_700_000_100)
        assert decision.live_transition is False
        assert decision.hourly_update is False
        assert decision.state == STATE_ACTIVE_RANGE
        await db.close()


class TestRestartWhileActiveRange:
    async def test_state_and_hourly_bucket_survive(self, tmp_path):
        path = str(tmp_path / "active.sqlite")
        config = make_settings(
            alert_debounce_seconds=0,
            immediate_transition_alerts=True,
            hourly_active_alerts=True,
        )
        db = await fresh_db(path)
        machine = AlertStateMachine(config, AlertStateRepository(db))
        decision = await machine.update(qualify("BTC"), now=1_700_000_000)
        assert decision.live_transition is True
        await db.close()

        db = await fresh_db(path)
        machine = AlertStateMachine(config, AlertStateRepository(db))
        # Same hour, same composition: nothing to send.
        decision = await machine.update(qualify("BTC"), now=1_700_000_500)
        assert decision.live_transition is False
        assert decision.hourly_update is False
        # Next hour: the persisted bucket triggers a fresh hourly alert.
        decision = await machine.update(qualify("BTC"), now=1_700_3600_000)
        assert decision.hourly_update is True
        assert decision.live_transition is False
        await db.close()

    async def test_new_composition_after_restart_sends_update(self, tmp_path):
        path = str(tmp_path / "composition.sqlite")
        config = make_settings(
            alert_debounce_seconds=0,
            immediate_transition_alerts=True,
            hourly_active_alerts=False,
            composition_change_alerts=True,
            composition_change_cooldown_seconds=0,
        )
        db = await fresh_db(path)
        machine = AlertStateMachine(config, AlertStateRepository(db))
        await machine.update(qualify("BTC"), now=1_700_000_000)
        await db.close()

        db = await fresh_db(path)
        machine = AlertStateMachine(config, AlertStateRepository(db))
        decision = await machine.update(qualify("BTC", "ETH"), now=1_700_000_100)
        assert decision.composition_update is True
        assert decision.live_transition is False
        await db.close()


class TestRestartDuringSuppression:
    async def test_restart_stays_silent_while_4_plus(self, tmp_path):
        path = str(tmp_path / "over.sqlite")
        config = make_settings(
            alert_debounce_seconds=0,
            immediate_transition_alerts=True,
            hourly_active_alerts=True,
        )
        db = await fresh_db(path)
        machine = AlertStateMachine(config, AlertStateRepository(db))
        decision = await machine.update(
            qualify("BTC", "ETH", "SOL", "DOGE"), now=1_700_000_000
        )
        assert decision.state == STATE_OVER_RANGE
        assert decision.live_transition is False
        await db.close()

        db = await fresh_db(path)
        machine = AlertStateMachine(config, AlertStateRepository(db))
        decision = await machine.update(
            qualify("BTC", "ETH", "SOL", "DOGE"), now=1_700_000_500
        )
        assert decision.state == STATE_OVER_RANGE
        assert decision.live_transition is False
        assert decision.hourly_update is False
        await db.close()

    async def test_suppression_clears_into_active_range_after_restart(self, tmp_path):
        path = str(tmp_path / "over_to_active.sqlite")
        config = make_settings(
            alert_debounce_seconds=0,
            immediate_transition_alerts=True,
            hourly_active_alerts=True,
        )
        db = await fresh_db(path)
        machine = AlertStateMachine(config, AlertStateRepository(db))
        await machine.update(
            qualify("BTC", "ETH", "SOL", "DOGE"), now=1_700_000_000
        )
        await db.close()

        db = await fresh_db(path)
        machine = AlertStateMachine(config, AlertStateRepository(db))
        decision = await machine.update(qualify("BTC"), now=1_700_000_100)
        assert decision.state == STATE_ACTIVE_RANGE
        assert decision.live_transition is True
        await db.close()


class TestRestartAfterListingNotification:
    async def test_sent_listing_event_not_notified_again(self, tmp_path):
        path = str(tmp_path / "listing.sqlite")
        config = make_settings(listing_notifications_enabled=True)
        db = await fresh_db(path)
        notify_calls: list[dict] = []
        tracker = ListingTracker(
            ListingEventRepository(db),
            config,
            notify=_notify_record(notify_calls),
        )
        result = DiscoveryResult(
            events=[
                RegistryEvent(
                    event="new",
                    category="linear",
                    symbol="FRESHUSDT",
                    base_coin="FRESH",
                    new_status="Trading",
                )
            ],
            first_run=False,
        )
        created = await tracker.handle_registry(result, now=1_700_000_000)
        assert len(created) == 1
        assert len(notify_calls) == 1
        await db.close()

        db = await fresh_db(path)
        notify_calls.clear()
        tracker = ListingTracker(
            ListingEventRepository(db), config, notify=_notify_record(notify_calls)
        )
        result = DiscoveryResult(
            events=[
                RegistryEvent(
                    event="new",
                    category="linear",
                    symbol="FRESHUSDT",
                    base_coin="FRESH",
                    new_status="Trading",
                )
            ],
            first_run=False,
        )
        created = await tracker.handle_registry(result, now=1_700_000_100)
        assert created == []
        assert notify_calls == []
        # The sent marker survived the restart.
        assert await tracker.repo.unsent() == []
        await db.close()

    async def test_unsent_event_requeued_on_restart(self, tmp_path):
        path = str(tmp_path / "listing_retry.sqlite")
        config = make_settings(listing_notifications_enabled=True)
        db = await fresh_db(path)
        # Session 1 crashes while notifying: the event stays unsent.
        async def _crash_notify(event_: dict) -> None:
            raise RuntimeError("telegram unavailable")

        tracker = ListingTracker(
            ListingEventRepository(db), config, notify=_crash_notify
        )
        result = DiscoveryResult(
            events=[
                RegistryEvent(
                    event="new",
                    category="spot",
                    symbol="NEWCOIN",
                    base_coin="NEWCOIN",
                    new_status="Trading",
                )
            ],
            first_run=False,
        )
        with pytest.raises(RuntimeError):
            await tracker.handle_registry(result, now=1_700_000_000)
        await db.close()

        db = await fresh_db(path)
        notify_calls: list[dict] = []
        tracker = ListingTracker(
            ListingEventRepository(db),
            config,
            notify=_notify_record(notify_calls),
        )
        requeued = await tracker.reconcile_unsent()
        assert requeued == 1
        assert len(notify_calls) == 1
        await db.close()


def _notify_record(calls: list) -> callable:
    async def _fn(event_: dict) -> None:
        calls.append(event_)

    return _fn


class TestRestartWithSpotHistory:
    async def test_anchor_survives_restart(self, tmp_path):
        path = str(tmp_path / "history.sqlite")
        config = make_settings(spot_history_retention_seconds=7200.0)
        db = await fresh_db(path)
        history = SpotHistory(PriceSampleRepository(db))
        now = int(time.time())
        assert await history.record("spot", "BTCUSDT", now - 3600, 100.0) is True
        assert await history.record("spot", "BTCUSDT", now - 60, 105.0) is True
        await db.close()

        db = await fresh_db(path)
        engine = MomentumEngine(config, SpotHistory(PriceSampleRepository(db)))
        instrument = Instrument(
            category="spot",
            symbol="BTCUSDT",
            base_coin="BTC",
            status="Trading",
        )
        ticker = Ticker(
            category="spot",
            symbol="BTCUSDT",
            last_price=105.0,
            timestamp=now,
        )
        value = await engine.evaluate(instrument, ticker, now)
        assert value.status == "OK"
        assert value.change_1h == pytest.approx(5.0, abs=1e-6)
        await db.close()

    async def test_prune_removes_old_samples_only(self, tmp_path):
        path = str(tmp_path / "prune.sqlite")
        db = await fresh_db(path)
        repo = PriceSampleRepository(db)
        now = int(time.time())
        await repo.insert_sample("spot", "OLDUSDT", now - 10_000, 1.0)
        await repo.insert_sample("spot", "OLDUSDT", now - 100, 1.0)
        await db.close()

        db = await fresh_db(path)
        history = SpotHistory(PriceSampleRepository(db))
        pruned = await history.prune(now, retention_seconds=7200.0)
        assert pruned == 1
        assert await history.repo.count() == 1
        await db.close()


class TestDatabaseBusyLock:
    def test_busy_timeout_lets_writer_through_after_lock_release(self, tmp_path):
        # Sync test with an explicit event loop (asyncio.run) to avoid
        # pytest-asyncio/aiosqlite background-thread interplay.
        async def _scenario(path: str) -> None:
            db = await fresh_db(path)
            await db.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES ('k', 'v', 1)"
            )
            await db.commit()

            row = await db.fetchone("PRAGMA busy_timeout")
            assert row["timeout"] == 5000

            locker = await aiosqlite.connect(path, timeout=5.0)
            await locker.execute("BEGIN EXCLUSIVE")

            async def _write() -> None:
                await db.execute(
                    "UPDATE kv SET value = 'updated', updated_at = 2 "
                    "WHERE key = 'k'"
                )
                await db.commit()

            writer = asyncio.create_task(_write())
            await asyncio.sleep(0.2)
            assert not writer.done()
            await locker.execute("ROLLBACK")
            await asyncio.wait_for(writer, timeout=8.0)
            row = await db.fetchone("SELECT value FROM kv WHERE key = 'k'")
            assert row["value"] == "updated"
            await locker.close()
            await db.close()

        asyncio.run(_scenario(str(tmp_path / "busy.sqlite")))


class TestRestartWithInstrumentRegistry:
    async def test_registry_survives_restart(self, tmp_path):
        path = str(tmp_path / "registry.sqlite")
        db = await fresh_db(path)
        repo = InstrumentRepository(db)
        await repo.upsert_many(
            [Instrument(category="spot", symbol="BTCUSDT", base_coin="BTC", status="Trading")],
            now=1_700_000_000,
        )
        await db.close()

        db = await fresh_db(path)
        registry = InstrumentRegistry(InstrumentRepository(db))
        instruments = await registry.repo.load_all()
        assert ("spot", "BTCUSDT") in instruments
        await db.close()
