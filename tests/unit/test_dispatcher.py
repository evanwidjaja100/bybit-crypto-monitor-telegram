"""Phase R4 - alert dispatcher tests (outbox polling, retry, durability)."""

from __future__ import annotations

import asyncio
import inspect
import time

import pytest

from app.alerts.dispatcher import AlertDispatcher, RETRY_DELAYS, retry_delay_for
from app.persistence.repository import Repository
from app.telegram.client import TelegramPermanentError, TelegramSendError
from tests.conftest import make_settings


class FakeClient:
    def __init__(
        self, fail: bool = False, error: Exception | None = None
    ) -> None:
        self.sent: list[str] = []
        self.fail = fail
        self.error = error
        self.closed = False

    async def send_message(self, text: str) -> None:
        if self.fail:
            raise self.error if self.error is not None else TelegramSendError(
                "simulated_outage"
            )
        self.sent.append(text)

    async def close(self) -> None:
        self.closed = True


def config(tmp_path, **overrides):
    overrides.setdefault("database_path", str(tmp_path / "dispatch.sqlite"))
    overrides.setdefault("telegram_bot_token", "fake")
    overrides.setdefault("telegram_chat_id", "-100fake")
    overrides.setdefault("dispatcher_poll_seconds", 0.05)
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
    return rows[0]["status"] if rows else None


async def status_is(db, expected: str) -> bool:
    return (await latest_status(db)) == expected


class TestRetryDelay:
    def test_retry_delay_progression(self):
        assert retry_delay_for(1) == 10
        assert retry_delay_for(2) == 30
        assert retry_delay_for(3) == 60
        assert retry_delay_for(4) == 300
        assert retry_delay_for(8) == 3600
        assert retry_delay_for(99) == 3600  # capped
        assert RETRY_DELAYS == (10, 30, 60, 300, 600, 1200, 2400, 3600)


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
        assert rows[0]["sent_at"] is not None

    async def test_transient_failure_marks_retry_and_schedules(self, tmp_path, db):
        client = FakeClient(fail=True)
        dispatcher = AlertDispatcher(client, Repository(db), config(tmp_path))
        await dispatcher.start()
        await dispatcher.enqueue("boom", tag="transition")
        assert await wait_for(lambda: status_is(db, "retry"))
        await dispatcher.stop()
        rows = await Repository(db).list_notifications()
        assert rows[0]["status"] == "retry"
        assert "simulated_outage" in rows[0]["error"]
        assert int(rows[0]["attempt_count"]) == 1
        assert int(rows[0]["next_attempt_at"]) >= int(time.time()) + 9

    async def test_failure_then_recovery_delivers(self, tmp_path, db):
        client = FakeClient()
        dispatcher = AlertDispatcher(client, Repository(db), config(tmp_path))
        await dispatcher.start()
        client.fail = True
        await dispatcher.enqueue("first")
        assert await wait_for(lambda: status_is(db, "retry"))
        # The retry becomes due (time passes / operator resets the clock).
        await Repository(db).db.execute(
            "UPDATE outgoing_notifications SET next_attempt_at = 0"
        )
        await Repository(db).db.commit()
        client.fail = False
        dispatcher.wake()
        assert await wait_for(lambda: status_is(db, "sent"))
        assert client.sent == ["first"]
        await dispatcher.stop()

    async def test_retry_rows_do_not_spin_continuously(self, tmp_path, db):
        client = FakeClient(fail=True)
        dispatcher = AlertDispatcher(client, Repository(db), config(tmp_path))
        await dispatcher.start()
        await dispatcher.enqueue("hello")
        assert await wait_for(lambda: status_is(db, "retry"))
        await asyncio.sleep(0.3)
        rows = await Repository(db).list_notifications()
        assert int(rows[0]["attempt_count"]) == 1
        await dispatcher.stop()

    async def test_pending_records_requeued_on_start(self, tmp_path, db):
        repo = Repository(db)
        await repo.insert_outgoing_notification(
            "transition",
            "leftover-message",
            status="pending",
            created_at=int(time.time()),
        )
        client = FakeClient()
        dispatcher = AlertDispatcher(client, repo, config(tmp_path))
        await dispatcher.start()
        assert await wait_for(lambda: client.sent == ["leftover-message"])
        await dispatcher.stop()
        rows = await repo.list_notifications()
        assert rows[0]["status"] == "sent"

    async def test_restart_while_retry_due_is_delivered(self, tmp_path, db):
        repo = Repository(db)
        nid = await repo.insert_outgoing_notification(
            "transition",
            "mid-retry",
            status="retry",
            created_at=int(time.time()),
        )
        await repo.mark_notification_retry(
            nid, "TelegramSendError: out", next_attempt_at=9999999999
        )
        # Time passes; the row is due again on the next start.
        await db.execute(
            "UPDATE outgoing_notifications SET next_attempt_at = 0"
        )
        await db.commit()
        client = FakeClient()
        dispatcher = AlertDispatcher(client, repo, config(tmp_path))
        await dispatcher.start()
        assert await wait_for(lambda: client.sent == ["mid-retry"])
        await dispatcher.stop()
        rows = await repo.list_notifications()
        assert rows[0]["status"] == "sent"
        assert int(rows[0]["attempt_count"]) == 1  # original failure counted

    async def test_successful_row_not_sent_again_on_restart(self, tmp_path, db):
        repo = Repository(db)
        nid = await repo.insert_outgoing_notification("transition", "done")
        await repo.mark_notification_sent(nid)
        client = FakeClient()
        dispatcher = AlertDispatcher(client, repo, config(tmp_path))
        await dispatcher.start()
        await asyncio.sleep(0.2)
        assert client.sent == []
        await dispatcher.stop()

    async def test_permanent_error_marks_dead(self, tmp_path, db):
        client = FakeClient(fail=True, error=TelegramPermanentError("http_400"))
        dispatcher = AlertDispatcher(client, Repository(db), config(tmp_path))
        await dispatcher.start()
        await dispatcher.enqueue("doomed")
        assert await wait_for(lambda: status_is(db, "dead"))
        await dispatcher.stop()
        rows = await Repository(db).list_notifications()
        assert "http_400" in rows[0]["error"]

    async def test_429_retry_after_is_honored(self, tmp_path, db):
        error = TelegramSendError("http_429")
        error.retry_after = 60
        client = FakeClient(fail=True, error=error)
        dispatcher = AlertDispatcher(client, Repository(db), config(tmp_path))
        await dispatcher.start()
        await dispatcher.enqueue("hello")
        assert await wait_for(lambda: status_is(db, "retry"))
        rows = await Repository(db).list_notifications()
        assert int(rows[0]["next_attempt_at"]) >= int(time.time()) + 55
        await dispatcher.stop()

    async def test_expired_rows_are_marked_dead(self, tmp_path, db):
        cfg = config(tmp_path, notification_max_age_seconds=60)
        repo = Repository(db)
        await repo.insert_outgoing_notification(
            "transition", "stale", status="retry", created_at=100
        )
        dispatcher = AlertDispatcher(FakeClient(fail=True), repo, cfg)
        await dispatcher.start()
        assert await wait_for(lambda: status_is(db, "dead"))
        await dispatcher.stop()
        rows = await repo.list_notifications()
        assert rows[0]["error"] == "expired"

    async def test_listing_expiration_uses_longer_window(self, tmp_path, db):
        cfg = config(
            tmp_path,
            notification_max_age_seconds=60,
            listing_notification_max_age_seconds=7200,
        )
        repo = Repository(db)
        await repo.insert_outgoing_notification(
            "listing",
            "still relevant",
            status="retry",
            created_at=int(time.time()) - 600,
            origin_type="listing",
            origin_key="linear:ABC:trading",
        )
        dispatcher = AlertDispatcher(FakeClient(fail=True), repo, cfg)
        await dispatcher.start()
        await asyncio.sleep(0.2)
        rows = await repo.list_notifications()
        assert rows[0]["status"] == "retry"  # not yet expired
        await dispatcher.stop()

    async def test_stop_flushes_due_work(self, tmp_path, db):
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
        assert rows[0]["status"] == "retry"
        assert int(rows[0]["attempt_count"]) == 1


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
