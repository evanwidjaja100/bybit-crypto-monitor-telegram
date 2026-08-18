"""Phase R10 - recovery and chaos acceptance tests.

Covers the R10 contract scenarios that are not already pinned elsewhere:

- Concurrent dispatcher + alert service on ONE SQLite connection: no
  deadlock, no interleaved BEGIN/COMMIT corruption, every notification
  delivered exactly once.
- The critical market-state sequence (16.2) end-to-end through the real
  AlertService with the production debounce window.
"""

from __future__ import annotations

import asyncio

import pytest

from app.alerts.dispatcher import AlertDispatcher
from app.alerts.service import AlertService
from app.alerts.state_machine import AlertStateMachine, STATE_ACTIVE_RANGE
from app.market.deduplication import QualifyingSet, RepresentativeMarket
from app.market.momentum import MomentumValue
from app.persistence.repository import AlertStateRepository, Repository
from tests.conftest import make_settings


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_message(self, text: str) -> None:
        self.sent.append(text)

    async def close(self) -> None:
        pass


def config(tmp_path, **overrides):
    overrides.setdefault("database_path", str(tmp_path / "chaos.sqlite"))
    overrides.setdefault("telegram_bot_token", "fake")
    overrides.setdefault("telegram_chat_id", "-100fake")
    overrides.setdefault("alert_debounce_seconds", 0)
    overrides.setdefault("dispatcher_poll_seconds", 0.01)
    overrides.setdefault("hourly_active_alerts", False)
    overrides.setdefault("composition_change_alerts", False)
    return make_settings(**overrides)


def qset(*coins: str) -> QualifyingSet:
    return QualifyingSet(
        [
            RepresentativeMarket(
                base_coin=c,
                representative=MomentumValue("linear", f"{c}USDT", c, 6.5, "OK"),
            )
            for c in coins
        ]
    )


async def wait_for_count(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


class TestConcurrentDispatcherAndService:
    """One shared connection, two tasks: the R9 connection-lock contract."""

    async def test_no_deadlock_and_exactly_once_delivery(self, tmp_path, db):
        cfg = config(tmp_path, dispatcher_poll_seconds=0.02)
        client = FakeClient()
        dispatcher = AlertDispatcher(client, Repository(db), cfg)
        await dispatcher.start()
        service = AlertService(
            AlertStateMachine(cfg, AlertStateRepository(db)), dispatcher, cfg
        )
        try:
            # Phase 1: the service writes state + outbox rows while the
            # worker delivers them concurrently. Every qualifying cycle is
            # a fresh transition (new coin) so each produces one message.
            tasks = []
            for coin_id in range(60):
                tasks.append(
                    asyncio.create_task(
                        service.process(qset(f"COIN{coin_id}"), now=1000 + coin_id)
                    )
                )
            await asyncio.gather(*tasks)
            await asyncio.sleep(0.5)  # let the worker drain the outbox
            rows = await Repository(db).list_notifications()
            assert len(rows) == 60
            assert all(r["status"] == "sent" for r in rows)
            assert len(client.sent) == 60
        finally:
            await dispatcher.stop()

    async def test_no_interleaved_transaction_on_failure_path(
        self, tmp_path, db, monkeypatch
    ):
        """A failing write must not corrupt a concurrent transaction."""
        cfg = config(tmp_path, dispatcher_poll_seconds=0.02)
        client = FakeClient()
        dispatcher = AlertDispatcher(client, Repository(db), cfg)
        await dispatcher.start()
        service = AlertService(
            AlertStateMachine(cfg, AlertStateRepository(db)), dispatcher, cfg
        )
        try:
            # Force the outbox insert to fail on a mid-run cycle: the
            # state write must roll back with it, and the connection must
            # remain usable for all subsequent cycles.
            real_insert = Repository.insert_outgoing_notification
            calls = {"n": 0}

            async def flaky_insert(self_, *args, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("injected chaos failure")
                return await real_insert(self_, *args, **kwargs)

            monkeypatch.setattr(
                Repository, "insert_outgoing_notification", flaky_insert
            )
            with pytest.raises(RuntimeError):
                await service.process(qset("FLAKY"), now=100)
            monkeypatch.setattr(
                Repository, "insert_outgoing_notification", real_insert
            )

            await service.process(qset("OKCOIN"), now=110)
            # Back to EMPTY, then a fresh qualifying episode: the
            # connection must still produce and deliver a second message.
            await service.process(qset(), now=115)
            await service.process(qset("OKCOIN2"), now=120)
            await asyncio.sleep(0.5)
            rows = await Repository(db).list_notifications()
            assert [r["message_tag"] for r in rows] == ["transition", "transition"]
            assert all(r["status"] == "sent" for r in rows)
            assert len(client.sent) == 2
        finally:
            await dispatcher.stop()


class TestConcurrentServiceDispatcherAndSamples:
    """Phase F1 6.6 - alert service + dispatcher + price-sample writes on
    one connection: no nested-transaction error, no deadlock, no
    cross-task execution inside another task's transaction."""

    async def test_concurrent_production_paths_serialize(self, tmp_path, db):
        from app.persistence.repository import PriceSampleRepository

        cfg = config(tmp_path, dispatcher_poll_seconds=0.02)
        client = FakeClient()
        dispatcher = AlertDispatcher(client, Repository(db), cfg)
        await dispatcher.start()
        service = AlertService(
            AlertStateMachine(cfg, AlertStateRepository(db)), dispatcher, cfg
        )
        samples = PriceSampleRepository(db)
        try:
            async def writer(label: str):
                for i in range(40):
                    await samples.insert_sample(
                        "spot", f"{label}{i}", 10_000 + i, 100.0 + i
                    )
                    await samples.find_reference("spot", f"{label}{i}", 10_000 + i, 90)
                    await asyncio.sleep(0)

            async def decider():
                for coin_id in range(40):
                    coins = qset(f"DEC{coin_id}") if coin_id % 2 == 0 else qset()
                    await service.process(coins, now=2000 + coin_id)

            await asyncio.gather(
                writer("A"), writer("B"), writer("C"), decider()
            )
            await asyncio.sleep(0.5)
            count = await samples.count()
            assert count == 120
            rows = await Repository(db).list_notifications()
            # 40 cycles alternate EMPTY/ACTIVE: 20 debounced transitions.
            assert len(rows) == 20
            assert all(r["status"] == "sent" for r in rows)
            assert len(client.sent) == 20
        finally:
            await dispatcher.stop()


class TestConcurrentListingAckAndAlertWrite:
    """Phase F8 Test C - listing acknowledgement (dispatcher) and alert
    writes (service) must serialize on the single connection."""

    async def test_listing_ack_and_alert_writes_serialize(self, tmp_path, db):
        from app.persistence.repository import ListingEventRepository

        cfg = config(tmp_path, dispatcher_poll_seconds=0.02)
        client = FakeClient()
        dispatcher = AlertDispatcher(client, Repository(db), cfg)
        await dispatcher.start()
        service = AlertService(
            AlertStateMachine(cfg, AlertStateRepository(db)), dispatcher, cfg
        )
        listings = ListingEventRepository(db)
        try:
            for i in range(10):
                await listings.record(f"linear:L{i}:trading", "linear", f"L{i}USDT", "trading", 1_000 + i)

            async def listing_writer():
                for i in range(10):
                    await listings.mark_sent(f"linear:L{i}:trading")

            async def decider():
                for coin_id in range(20):
                    coins = qset(f"LC{coin_id}") if coin_id % 2 == 0 else qset()
                    await service.process(coins, now=3000 + coin_id)

            await asyncio.gather(listing_writer(), decider())
            await asyncio.sleep(0.5)
            rows = await Repository(db).list_notifications()
            assert len(rows) == 10
            assert all(r["status"] == "sent" for r in rows)
            assert len(client.sent) == 10
            # All listing acks persisted without corruption.
            for i in range(10):
                row = await db.fetchone(
                    "SELECT telegram_sent FROM listing_events WHERE event_key = ?",
                    (f"linear:L{i}:trading",),
                )
                assert row["telegram_sent"] == 1  # type: ignore[index]
        finally:
            await dispatcher.stop()


class TestCriticalMarketStateSequence:
    """Section 16.2 - the full sequence with the real debounce window."""

    async def test_full_sequence(self, tmp_path, db):
        cfg = config(tmp_path, alert_debounce_seconds=20)
        client = FakeClient()
        dispatcher = AlertDispatcher(client, Repository(db), cfg)
        await dispatcher.start()
        service = AlertService(
            AlertStateMachine(cfg, AlertStateRepository(db)), dispatcher, cfg
        )
        try:
            # 0 qualifying.
            await service.process(qset(), now=0)

            # BTC +6%, flip back before the 20s debounce: zero messages.
            await service.process(qset("BTC"), now=5)
            d = await service.process(qset(), now=15)
            assert d.live_transition is False and d.hourly_update is False
            await asyncio.sleep(0.2)
            assert client.sent == []

            # BTC +6% held 20s: exactly one transition.
            await service.process(qset("BTC"), now=30)
            d = await service.process(qset("BTC"), now=50)
            assert d.live_transition is True
            assert d.state == STATE_ACTIVE_RANGE
            await wait_for_count(lambda: len(client.sent) == 1)

            # BTC + ETH + SOL: no duplicate.
            await service.process(qset("BTC", "ETH", "SOL"), now=60)
            await service.process(qset("BTC", "ETH", "SOL"), now=70)
            await asyncio.sleep(0.2)
            assert len(client.sent) == 1

            # 4 coins: suppressed.
            d = await service.process(qset("BTC", "ETH", "SOL", "DOGE"), now=80)
            assert d.state == "OVER_RANGE"
            assert d.live_transition is False
            await asyncio.sleep(0.2)
            assert len(client.sent) == 1

            # ETH drops -> 3 remain -> debounce -> one re-entry alert.
            await service.process(qset("BTC", "SOL", "DOGE"), now=90)
            await asyncio.sleep(0.2)
            assert len(client.sent) == 1  # still debouncing
            d = await service.process(qset("BTC", "SOL", "DOGE"), now=110)
            assert d.live_transition is True
            assert d.transition_reason == "OVER_RANGE -> ACTIVE_RANGE"
            await wait_for_count(lambda: len(client.sent) == 2)
        finally:
            await dispatcher.stop()