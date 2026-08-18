"""Phase 8 - alert dispatcher tests (queue, persistence, outage resilience)."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from app.alerts.dispatcher import AlertDispatcher
from app.persistence.repository import Repository
from app.telegram.client import TelegramSendError
from tests.conftest import make_settings


class FakeClient:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[str] = []
        self.fail = fail
        self.closed = False

    async def send_message(self, text: str) -> None:
        if self.fail:
            raise TelegramSendError("simulated_outage")
        self.sent.append(text)

    async def close(self) -> None:
        self.closed = True


def config(tmp_path, **overrides):
    overrides.setdefault("database_path", str(tmp_path / "dispatch.sqlite"))
    overrides.setdefault("telegram_bot_token", "fake")
    overrides.setdefault("telegram_chat_id", "-100fake")
    return make_settings(**overrides)


async def wait_for(predicate, timeout: float = 5.0) -> bool:
    """Poll ``predicate`` (sync or async) until truthy or timeout."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return True
        await asyncio.sleep(0.02)
    return False


async def latest_status(db) -> str:
    rows = await Repository(db).list_notifications()
    return rows[0]["status"]


async def status_is(db, expected: str) -> bool:
    return (await latest_status(db)) == expected


class TestDispatcher:
    async def test_enqueue_sends_and_marks_sent(self, tmp_path, db):
        client = FakeClient()
        dispatcher = AlertDispatcher(client, Repository(db), config(tmp_path))
        await dispatcher.start()
        notification_id = await dispatcher.enqueue("hello", tag="transition")
        assert await wait_for(lambda: client.sent == ["hello"])
        await dispatcher.stop()
        rows = await Repository(db).list_notifications()
        assert rows[0]["status"] == "sent"
        assert rows[0]["id"] == notification_id
        assert rows[0]["message_tag"] == "transition"

    async def test_failure_marks_failed_and_keeps_worker_alive(self, tmp_path, db):
        client = FakeClient(fail=True)
        dispatcher = AlertDispatcher(client, Repository(db), config(tmp_path))
        await dispatcher.start()
        await dispatcher.enqueue("boom", tag="transition")
        assert await wait_for(lambda: status_is(db, "failed"))
        await dispatcher.stop()
        rows = await Repository(db).list_notifications()
        assert rows[0]["status"] == "failed"
        assert "simulated_outage" in rows[0]["error"]

    async def test_failure_then_success(self, tmp_path, db):
        client = FakeClient()
        dispatcher = AlertDispatcher(client, Repository(db), config(tmp_path))
        await dispatcher.start()
        client.fail = True
        await dispatcher.enqueue("first")
        assert await wait_for(lambda: status_is(db, "failed"))
        client.fail = False
        await dispatcher.enqueue("second")
        assert await wait_for(lambda: client.sent == ["second"])
        await dispatcher.stop()

    async def test_pending_records_requeued_on_start(self, tmp_path, db):
        repo = Repository(db)
        await repo.insert_outgoing_notification(
            "transition", "leftover-message", status="pending", created_at=100
        )
        client = FakeClient()
        dispatcher = AlertDispatcher(client, repo, config(tmp_path))
        await dispatcher.start()
        assert await wait_for(lambda: client.sent == ["leftover-message"])
        await dispatcher.stop()
        rows = await repo.list_notifications()
        assert rows[0]["status"] == "sent"

    async def test_stop_flushes_queue(self, tmp_path, db):
        client = FakeClient()
        dispatcher = AlertDispatcher(client, Repository(db), config(tmp_path))
        await dispatcher.start()
        for i in range(5):
            await dispatcher.enqueue(f"msg-{i}")
        await dispatcher.stop()
        assert client.closed
        assert len(client.sent) == 5
        rows = await Repository(db).list_notifications(limit=10)
        assert all(r["status"] == "sent" for r in rows)

    async def test_monitoring_continues_during_telegram_outage(self, tmp_path, db):
        """State machine keeps deciding while delivery is down (Phase 8 gate)."""
        from app.alerts.state_machine import AlertStateMachine
        from app.market.deduplication import QualifyingSet, RepresentativeMarket
        from app.market.momentum import MomentumValue
        from app.persistence.repository import AlertStateRepository

        client = FakeClient(fail=True)
        dispatcher = AlertDispatcher(client, Repository(db), config(tmp_path))
        await dispatcher.start()

        cfg = config(tmp_path, alert_debounce_seconds=0)
        sm = AlertStateMachine(cfg, AlertStateRepository(db))
        qualifying = QualifyingSet(
            [
                RepresentativeMarket(
                    base_coin="BTC",
                    representative=MomentumValue(
                        "linear", "BTCUSDT", "BTC", 6.5, "OK"
                    ),
                )
            ]
        )
        decision = await sm.update(qualifying, now=100)
        assert decision.live_transition is True
        await dispatcher.enqueue("ignored-by-failing-telegram")
        decision = await sm.update(qualifying, now=200)
        assert decision.live_transition is False
        await dispatcher.stop()
        rows = await Repository(db).list_notifications()
        assert rows[0]["status"] == "failed"