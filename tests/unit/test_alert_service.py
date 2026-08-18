"""Phase 8 - AlertService glue tests (decision -> format -> queue)."""

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
    overrides.setdefault("database_path", str(tmp_path / "service.sqlite"))
    overrides.setdefault("telegram_bot_token", "fake")
    overrides.setdefault("telegram_chat_id", "-100fake")
    overrides.setdefault("alert_debounce_seconds", 0)
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


async def build(tmp_path, db):
    cfg = config(tmp_path)
    client = FakeClient()
    dispatcher = AlertDispatcher(client, Repository(db), cfg)
    await dispatcher.start()
    service = AlertService(AlertStateMachine(cfg, AlertStateRepository(db)), dispatcher, cfg)
    return cfg, client, dispatcher, service


class TestAlertService:
    async def test_transition_alert_is_enqueued_and_sent(self, tmp_path, db):
        _, client, dispatcher, service = await build(tmp_path, db)
        decision = await service.process(qset("BTC"), now=100)
        assert decision.live_transition is True
        assert decision.state == STATE_ACTIVE_RANGE
        await dispatcher.stop()
        assert len(client.sent) == 1
        assert "🚨 BYBIT 1H MOMENTUM ALERT" in client.sent[0]
        assert "1 / 3 qualifying coins" in client.sent[0]
        rows = await Repository(db).list_notifications()
        assert rows[0]["message_tag"] == "transition"
        assert rows[0]["status"] == "sent"

    async def test_no_duplicate_transition_message(self, tmp_path, db):
        _, client, dispatcher, service = await build(tmp_path, db)
        await service.process(qset("BTC"), now=100)
        await service.process(qset("BTC"), now=110)
        await dispatcher.stop()
        assert len(client.sent) == 1

    async def test_hourly_snapshot_in_next_bucket(self, tmp_path, db):
        _, client, dispatcher, service = await build(tmp_path, db)
        await service.process(qset("BTC"), now=100)
        decision = await service.process(qset("BTC"), now=3700)
        assert decision.hourly_update is True
        await dispatcher.stop()
        assert len(client.sent) == 2
        assert "⏰ BYBIT 1H MOMENTUM ACTIVE-STATE" in client.sent[1]

    async def test_suppressed_state_sends_nothing(self, tmp_path, db):
        _, client, dispatcher, service = await build(tmp_path, db)
        decision = await service.process(qset("A", "B", "C", "D"), now=100)
        assert decision.state == "OVER_RANGE"
        assert decision.live_transition is False
        await dispatcher.stop()
        assert client.sent == []


class TestHourlyOnlyMode:
    """Phase F5 - IMMEDIATE_TRANSITION_ALERTS=false must not claim the
    hourly bucket without emitting a transition message."""

    async def test_hourly_only_emits_hourly_not_transition(self, tmp_path, db):
        cfg = config(
            tmp_path,
            immediate_transition_alerts=False,
            hourly_active_alerts=True,
            alert_debounce_seconds=20,
        )
        client = FakeClient()
        dispatcher = AlertDispatcher(client, Repository(db), cfg)
        await dispatcher.start()
        service = AlertService(
            AlertStateMachine(cfg, AlertStateRepository(db)), dispatcher, cfg
        )
        try:
            d1 = await service.process(qset("BTC"), now=0)
            assert d1.live_transition is False
            assert d1.hourly_update is False  # debounce still pending

            d2 = await service.process(qset("BTC"), now=30)
            assert d2.live_transition is False
            assert d2.hourly_update is True  # hourly allowed in same bucket

            await asyncio.sleep(0.2)
            rows = await Repository(db).list_notifications()
            assert [r["message_tag"] for r in rows] == ["hourly"]
            assert len(client.sent) == 1
            assert "ACTIVE-STATE" in client.sent[0]
        finally:
            await dispatcher.stop()

    async def test_both_enabled_transition_then_no_same_bucket_hourly(
        self, tmp_path, db
    ):
        cfg = config(
            tmp_path,
            immediate_transition_alerts=True,
            hourly_active_alerts=True,
            alert_debounce_seconds=0,
        )
        client = FakeClient()
        dispatcher = AlertDispatcher(client, Repository(db), cfg)
        await dispatcher.start()
        service = AlertService(
            AlertStateMachine(cfg, AlertStateRepository(db)), dispatcher, cfg
        )
        try:
            d1 = await service.process(qset("BTC"), now=100)
            assert d1.live_transition is True
            assert d1.hourly_update is False  # bucket consumed by transition

            d2 = await service.process(qset("BTC"), now=200)
            assert d2.live_transition is False
            assert d2.hourly_update is False  # no same-bucket duplicate

            await asyncio.sleep(0.2)
            rows = await Repository(db).list_notifications()
            assert [r["message_tag"] for r in rows] == ["transition"]
            assert len(client.sent) == 1
        finally:
            await dispatcher.stop()

    async def test_both_disabled_state_updates_without_notifications(
        self, tmp_path, db
    ):
        cfg = config(
            tmp_path,
            immediate_transition_alerts=False,
            hourly_active_alerts=False,
            alert_debounce_seconds=0,
        )
        client = FakeClient()
        dispatcher = AlertDispatcher(client, Repository(db), cfg)
        await dispatcher.start()
        service = AlertService(
            AlertStateMachine(cfg, AlertStateRepository(db)), dispatcher, cfg
        )
        try:
            d1 = await service.process(qset("BTC"), now=100)
            assert d1.live_transition is False
            assert d1.hourly_update is False
            state = await AlertStateRepository(db).load()
            assert state["state"] == STATE_ACTIVE_RANGE  # state still updates
            await asyncio.sleep(0.2)
            assert client.sent == []
            rows = await Repository(db).list_notifications()
            assert rows == []
        finally:
            await dispatcher.stop()