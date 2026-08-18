"""Phase 9 - new-listing detection tests."""

from __future__ import annotations

import pytest

from app.bybit.models import Announcement
from app.market.discovery import DiscoveryResult, RegistryEvent
from app.market.listing import (
    EVENT_ANNOUNCED,
    EVENT_DELISTED,
    EVENT_PRELAUNCH,
    EVENT_TRADING,
    ListingTracker,
    listing_dedupe_key,
)
from app.persistence.repository import ListingEventRepository, Repository
from tests.conftest import make_settings


def config(tmp_path, **overrides):
    overrides.setdefault("database_path", str(tmp_path / "listing.sqlite"))
    overrides.setdefault("telegram_bot_token", "fake")
    overrides.setdefault("telegram_chat_id", "-100fake")
    return make_settings(**overrides)


def registry_event(
    symbol: str,
    event: str,
    new_status: str | None = None,
    old_status: str | None = None,
    category: str = "linear",
) -> RegistryEvent:
    return RegistryEvent(
        category=category,
        symbol=symbol,
        base_coin=symbol[:3],
        event=event,
        old_status=old_status,
        new_status=new_status,
    )


def result(*events: RegistryEvent) -> DiscoveryResult:
    return DiscoveryResult(events=list(events), instrument_count=1, first_run=False)


class NotifyCollector:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def __call__(self, event: dict) -> None:
        self.sent.append(event)


def tracker(tmp_path, db, notifications: bool = True) -> tuple[ListingTracker, NotifyCollector]:
    notify = NotifyCollector()
    cfg = config(tmp_path, listing_notifications_enabled=notifications)
    return (
        ListingTracker(ListingEventRepository(db), cfg, notify=notify),
        notify,
    )


class TestRegistrySignals:
    async def test_new_prelaunch_market(self, tmp_path, db):
        tr, notify = tracker(tmp_path, db)
        created = await tr.handle_registry(
            result(registry_event("ABCUSDT", "new", new_status="PreLaunch")),
            now=100,
        )
        assert len(created) == 1
        assert created[0]["event_type"] == EVENT_PRELAUNCH
        assert created[0]["symbol"] == "ABCUSDT"
        assert len(notify.sent) == 1

    async def test_prelaunch_to_trading(self, tmp_path, db):
        tr, notify = tracker(tmp_path, db)
        await tr.handle_registry(
            result(
                registry_event(
                    "ABCUSDT", "status_transition", old_status="PreLaunch", new_status="Trading"
                )
            ),
            now=100,
        )
        assert len(notify.sent) == 1
        assert notify.sent[0]["event_type"] == EVENT_TRADING

    async def test_direct_unknown_to_trading(self, tmp_path, db):
        tr, notify = tracker(tmp_path, db)
        created = await tr.handle_registry(
            result(registry_event("ABCUSDT", "new", new_status="Trading")),
            now=100,
        )
        assert created[0]["event_type"] == EVENT_TRADING
        assert len(notify.sent) == 1

    async def test_restart_does_not_recreate_events(self, tmp_path, db):
        tr, notify = tracker(tmp_path, db)
        events = result(registry_event("ABCUSDT", "new", new_status="PreLaunch"))
        await tr.handle_registry(events, now=100)
        assert len(notify.sent) == 1
        created = await tr.handle_registry(events, now=200)
        assert created == []
        assert len(notify.sent) == 1

    async def test_duplicate_api_result_single_event(self, tmp_path, db):
        tr, _ = tracker(tmp_path, db)
        events = [
            registry_event("ABCUSDT", "new", new_status="Trading"),
            registry_event("ABCUSDT", "new", new_status="Trading"),
        ]
        created = await tr.handle_registry(result(*events), now=100)
        assert len(created) == 1

    async def test_delisted_is_recorded_but_silent(self, tmp_path, db):
        tr, notify = tracker(tmp_path, db)
        created = await tr.handle_registry(
            result(
                registry_event(
                    "OLDUSDT", "removed", old_status="Trading"
                )
            ),
            now=100,
        )
        assert created[0]["event_type"] == EVENT_DELISTED
        assert notify.sent == []

    async def test_first_run_seeding_generates_nothing(self, tmp_path, db):
        tr, notify = tracker(tmp_path, db)
        await tr.handle_registry(
            DiscoveryResult(
                events=[registry_event("ABCUSDT", "new", new_status="Trading")],
                instrument_count=1,
                first_run=True,
            ),
            now=100,
        )
        assert notify.sent == []

    async def test_non_listing_status_transition_ignored(self, tmp_path, db):
        tr, notify = tracker(tmp_path, db)
        await tr.handle_registry(
            result(
                registry_event(
                    "ABCUSDT", "status_transition", old_status="Trading", new_status="Suspended"
                )
            ),
            now=100,
        )
        assert notify.sent == []


class TestAnnouncementSignals:
    async def test_listing_keyword_extracts_symbols(self, tmp_path, db):
        tr, notify = tracker(tmp_path, db)
        announcement = Announcement(
            id="1",
            title="ABCUSDT (ABC) Will Be Listed on Bybit Spot",
            description="Trading starts soon.",
        )
        created = await tr.handle_announcements([announcement], now=100)
        assert len(created) == 1
        assert created[0]["event_type"] == EVENT_ANNOUNCED
        assert created[0]["symbol"] == "ABCUSDT"
        assert len(notify.sent) == 1

    async def test_non_listing_announcement_ignored(self, tmp_path, db):
        tr, notify = tracker(tmp_path, db)
        announcement = Announcement(
            id="2",
            title="Maintenance Scheduled",
            description="System upgrade on Sunday.",
        )
        created = await tr.handle_announcements([announcement], now=100)
        assert created == []
        assert notify.sent == []

    async def test_duplicate_announcement_deduplicated(self, tmp_path, db):
        tr, notify = tracker(tmp_path, db)
        announcement = Announcement(
            id="3",
            title="XYZUSDT New Listing",
            description="",
        )
        await tr.handle_announcements([announcement], now=100)
        await tr.handle_announcements([announcement], now=200)
        assert len(notify.sent) == 1


class TestStartupRetry:
    async def test_unsent_events_retried_on_startup(self, tmp_path, db):
        tr, notify = tracker(tmp_path, db)
        await tr.handle_registry(
            result(registry_event("ABCUSDT", "new", new_status="PreLaunch")),
            now=100,
        )
        assert len(notify.sent) == 1
        # Delivery was never confirmed (no dispatcher/outbox in this test),
        # so a restart re-notifies the still-unsent event exactly once.
        tr2, notify2 = tracker(tmp_path, db)
        count = await tr2.reconcile_unsent()
        assert count == 1
        assert len(notify2.sent) == 1
        assert notify2.sent[0]["event_key"] == notify.sent[0]["event_key"]

    async def test_notifications_disabled_events_retried_later(self, tmp_path, db):
        tr, notify = tracker(tmp_path, db, notifications=False)
        await tr.handle_registry(
            result(registry_event("ABCUSDT", "new", new_status="PreLaunch")),
            now=100,
        )
        assert notify.sent == []
        tr2, notify2 = tracker(tmp_path, db)
        count = await tr2.reconcile_unsent()
        assert count == 1
        assert notify2.sent[0]["symbol"] == "ABCUSDT"

    async def test_event_persisted_before_notify_failure(self, tmp_path, db):
        repo = ListingEventRepository(db)
        cfg = config(tmp_path)
        failed = False

        async def broken_notify(event):
            nonlocal failed
            failed = True
            raise RuntimeError("dispatcher down")

        tr = ListingTracker(repo, cfg, notify=broken_notify)
        with pytest.raises(RuntimeError):
            await tr.handle_registry(
                result(registry_event("ABCUSDT", "new", new_status="Trading")),
                now=100,
            )
        rows = await repo.list_all()
        assert rows[0]["event_type"] == EVENT_TRADING
        assert failed is True


class TestOutboxReconcile:
    """Phase R5 - unsent events are reconciled with the durable outbox."""

    class OutboxNotifier:
        """Production-style notify boundary: enqueue with origin metadata."""

        def __init__(self, outbox: Repository) -> None:
            self.outbox = outbox
            self.sent: list[dict] = []

        async def __call__(self, event: dict) -> None:
            self.sent.append(event)
            await self.outbox.insert_outgoing_notification(
                "listing",
                "listing-msg",
                dedupe_key=listing_dedupe_key(event["event_key"]),
                origin_type="listing",
                origin_key=event["event_key"],
            )

    def outbox_tracker(self, tmp_path, db):
        outbox = Repository(db)
        notify = self.OutboxNotifier(outbox)
        cfg = config(tmp_path, listing_notifications_enabled=True)
        tracker = ListingTracker(
            ListingEventRepository(db), cfg, notify=notify, outbox=outbox
        )
        return tracker, notify, outbox

    async def test_pending_outbox_row_is_left_to_dispatcher(self, tmp_path, db):
        tr, notify, outbox = self.outbox_tracker(tmp_path, db)
        created = await tr.handle_registry(
            result(registry_event("ABCUSDT", "new", new_status="Trading")),
            now=100,
        )
        key = created[0]["event_key"]
        assert len(await outbox.list_notifications()) == 1
        # Restart: the pending row exists, so reconcile must not enqueue
        # a duplicate and must not mark the event sent.
        tr2, notify2, outbox2 = self.outbox_tracker(tmp_path, db)
        count = await tr2.reconcile_unsent()
        assert count == 0
        assert notify2.sent == []
        rows = await outbox2.list_notifications()
        assert len(rows) == 1
        assert rows[0]["dedupe_key"] == listing_dedupe_key(key)
        assert rows[0]["status"] == "pending"
        event = (await ListingEventRepository(db).list_all())[0]
        assert event["telegram_sent"] == 0

    async def test_sent_outbox_row_repairs_stale_listing_flag(self, tmp_path, db):
        tr, _, outbox = self.outbox_tracker(tmp_path, db)
        created = await tr.handle_registry(
            result(registry_event("ABCUSDT", "new", new_status="Trading")),
            now=100,
        )
        key = created[0]["event_key"]
        row = (await outbox.list_notifications())[0]
        # Delivery succeeded but the listing flag was lost (crash between
        # the two writes on an older version).
        await outbox.mark_notification_sent(row["id"])
        await db.execute(
            "UPDATE listing_events SET telegram_sent = 0 WHERE event_key = ?",
            (key,),
        )
        await db.commit()
        tr2, notify2, _ = self.outbox_tracker(tmp_path, db)
        count = await tr2.reconcile_unsent()
        assert count == 1
        assert notify2.sent == []
        event = (await ListingEventRepository(db).list_all())[0]
        assert event["telegram_sent"] == 1

    async def test_missing_outbox_row_is_created(self, tmp_path, db):
        repo = ListingEventRepository(db)
        outbox = Repository(db)
        cfg = config(tmp_path, listing_notifications_enabled=True)
        # Simulate a crash between event recording and outbox creation.
        async def crashed_notify(event):
            raise RuntimeError("crash before outbox insert")

        tr = ListingTracker(repo, cfg, notify=crashed_notify, outbox=outbox)
        with pytest.raises(RuntimeError):
            await tr.handle_registry(
                result(registry_event("ABCUSDT", "new", new_status="Trading")),
                now=100,
            )
        assert len(await outbox.list_notifications()) == 0

        notify = self.OutboxNotifier(outbox)
        tr2 = ListingTracker(repo, cfg, notify=notify, outbox=outbox)
        count = await tr2.reconcile_unsent()
        assert count == 1
        assert len(notify.sent) == 1
        rows = await outbox.list_notifications()
        assert len(rows) == 1
        assert rows[0]["dedupe_key"] == "listing:linear:ABCUSDT:trading"
        assert rows[0]["origin_type"] == "listing"
        assert rows[0]["origin_key"] == "linear:ABCUSDT:trading"

    async def test_retry_outbox_row_not_duplicated_after_restart(self, tmp_path, db):
        tr, notify, outbox = self.outbox_tracker(tmp_path, db)
        created = await tr.handle_registry(
            result(registry_event("ABCUSDT", "new", new_status="Trading")),
            now=100,
        )
        key = created[0]["event_key"]
        row = (await outbox.list_notifications())[0]
        await outbox.mark_notification_retry(
            row["id"], "TelegramSendError: out", next_attempt_at=9999999999
        )
        tr2, notify2, _ = self.outbox_tracker(tmp_path, db)
        count = await tr2.reconcile_unsent()
        assert count == 0
        assert notify2.sent == []
        rows = await outbox.list_notifications()
        assert len(rows) == 1
        assert rows[0]["status"] == "retry"