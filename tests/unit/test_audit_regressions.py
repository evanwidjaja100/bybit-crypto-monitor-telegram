"""Phase R1 - audit regression tests.

Every test reproduces one confirmed defect from the master implementation
plan before the fix exists. These tests are the contract for the
remediation; they are written against real production paths (Application
wiring, real formatters, real dispatcher/repository boundaries, real
Bybit payload shapes) wherever the defect is a wiring bug.

The full list of addressed findings:

    P0-1  listing formatter production wiring raises TypeError
    P0-2  hourly alert bypasses the transition debounce
    P0-3  alert state and outgoing notification are not atomic
    P0-4  failed Telegram notifications are abandoned permanently
    P0-5  listing events are marked sent when only queued
    P1-1  announcement normalization does not match the real schema
    P1-2  wrong prelisting field (preList vs isPreListing)
    P1-3  spot instruments request sends pagination arguments
    P1-4  discovery health timestamp can remain None
    P1-5  health monitor mixes monotonic and epoch clocks
    P1-6  WebSocket top-level ts is discarded
    P2-1  spot anchor tolerance configuration is not wired
    P2-4  duplicate task registration in main.py
    -     threshold comparison must not round before strict > comparison
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Optional

import httpx
import pytest
from websockets.asyncio.server import serve

from app.alerts.dispatcher import AlertDispatcher
from app.alerts.formatter import format_listing_alert
from app.alerts.service import AlertService
from app.alerts.state_machine import (
    STATE_EMPTY,
    AlertStateMachine,
)
from app.bybit.models import Announcement, Instrument
from app.bybit.normalizer import parse_announcement, parse_instrument
from app.bybit.rest import BybitRestClient
from app.bybit.websocket import BybitWebSocketClient, WebSocketManager
from app.health.monitor import HealthMonitor
from app.main import Application
from app.market.deduplication import QualifyingSet, RepresentativeMarket
from app.market.discovery import (
    DiscoveryResult,
    InstrumentDiscovery,
    InstrumentRegistry,
    RegistryEvent,
    STATUS_TRADING,
)
from app.market.listing import EVENT_ANNOUNCED, EVENT_TRADING, ListingTracker
from app.market.momentum import MomentumEngine, MomentumValue, SpotHistory
from app.market.price_engine import PriceEngine
from app.persistence.database import Database
from app.persistence.migrations import apply_migrations
from app.persistence.repository import (
    AlertStateRepository,
    InstrumentRepository,
    ListingEventRepository,
    PriceSampleRepository,
    Repository,
)
from app.telegram.client import TelegramSendError
from tests.conftest import make_settings


# ----------------------------------------------------------------------
# Shared helpers
# ----------------------------------------------------------------------

async def fresh_db(path: str) -> Database:
    db = Database(path)
    await db.connect()
    await apply_migrations(db)
    await db.commit()
    return db


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


async def latest_status(db) -> Optional[str]:
    rows = await Repository(db).list_notifications()
    return rows[0]["status"] if rows else None


async def status_is(db, expected: str) -> bool:
    return (await latest_status(db)) == expected


def make_cfg(tmp_path, **overrides) -> object:
    overrides.setdefault("database_path", str(tmp_path / "audit.sqlite"))
    overrides.setdefault("telegram_bot_token", "123456789:AAfaketokenvaluefortests_only")
    overrides.setdefault("telegram_chat_id", "-1001234567890")
    return make_settings(**overrides)


def coin(change_1h: float = 6.0) -> MomentumValue:
    return MomentumValue(
        category="linear",
        symbol="BTCUSDT",
        base_coin="BTC",
        change_1h=change_1h,
        status="OK",
        settle_coin="USDT",
        quote_coin="USDT",
    )


def qset(*coins: str) -> QualifyingSet:
    reps = [
        RepresentativeMarket(base_coin=c, representative=coin())
        for c in sorted(coins)
    ]
    return QualifyingSet(reps)


class FakeClient:
    def __init__(self, fail: bool = False, error: Optional[Exception] = None) -> None:
        self.sent: list[str] = []
        self.fail = fail
        self.error = error
        self.closed = False

    async def send_message(self, text: str) -> None:
        if self.fail:
            raise self.error if self.error is not None else TelegramSendError("simulated_outage")
        self.sent.append(text)

    async def close(self) -> None:
        self.closed = True


def registry_event(
    symbol: str,
    event: str,
    new_status: Optional[str] = None,
    old_status: Optional[str] = None,
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


def listing_notify(dispatcher: AlertDispatcher):
    """The production listing-notification boundary (see main.py)."""
    async def notify(event: dict) -> None:
        await dispatcher.enqueue(
            format_listing_alert(event),
            tag="listing",
            dedupe_key=f"listing:{event['event_key']}",
            origin_type="listing",
            origin_key=event["event_key"],
        )

    return notify


# ----------------------------------------------------------------------
# P0-1 - listing formatter production wiring
# ----------------------------------------------------------------------

class TestP01ListingWiring:
    async def test_real_application_listing_callback_formats_and_enqueues(
        self, config, monkeypatch
    ):
        """Application wiring -> real callback -> real formatter -> dispatcher."""
        sent: list[str] = []

        async def fake_send(self, text: str) -> None:  # noqa: ANN001
            sent.append(text)

        async def fake_announcements(self, limit: int = 50) -> list:  # noqa: ANN001
            return []

        monkeypatch.setattr("app.telegram.client.TelegramClient.send_message", fake_send)
        monkeypatch.setattr("app.bybit.rest.BybitRestClient.get_announcements", fake_announcements)

        cfg = make_settings(
            _env_file=None,
            database_path=config.database_path,
            telegram_bot_token=config.telegram_bot_token,
            telegram_chat_id=config.telegram_chat_id,
            enable_websocket=False,
            rest_fallback_enabled=False,
            listing_notifications_enabled=True,
        )
        app = Application(cfg)
        await app.start()
        try:
            event = {
                "event_key": "linear:XYZUSDT:trading",
                "category": "linear",
                "symbol": "XYZUSDT",
                "event_type": "trading",
                "first_seen_at": 100,
                "telegram_sent": 0,
            }
            # The production callback must not raise TypeError by passing
            # the Settings object as the formatter's `now` argument.
            await app._notify_listing(event)
            assert await wait_for(lambda: len(sent) == 1)
            rows = await Repository(app.db).list_notifications()
            assert len(rows) == 1
            assert rows[0]["message_tag"] == "listing"
            assert "🆕 BYBIT NEW LISTING" in rows[0]["message"]
            assert "XYZUSDT" in rows[0]["message"]
            assert "Status: Now Trading" in rows[0]["message"]
        finally:
            await app.shutdown()


# ----------------------------------------------------------------------
# P0-2 - debounce cannot be bypassed
# ----------------------------------------------------------------------

def debounce_machine(tmp_path, db, **overrides):
    cfg = make_cfg(
        tmp_path,
        alert_debounce_seconds=20,
        hourly_active_alerts=True,
        immediate_transition_alerts=True,
        composition_change_alerts=True,
        **overrides,
    )
    return AlertStateMachine(cfg, AlertStateRepository(db))


class TestP02Debounce:
    async def test_initial_active_entry_does_not_emit_hourly_before_debounce(
        self, tmp_path, db
    ):
        sm = debounce_machine(tmp_path, db)
        d0 = await sm.update(qset("BTC"), now=100)
        assert d0.live_transition is False
        assert d0.hourly_update is False
        assert d0.composition_update is False

    async def test_transient_active_range_shorter_than_debounce_emits_nothing(
        self, tmp_path, db
    ):
        sm = debounce_machine(tmp_path, db)
        d0 = await sm.update(qset("BTC"), now=0)
        assert d0.live_transition is False
        assert d0.hourly_update is False
        # The blip dies before the debounce completes: pending is cleared.
        d1 = await sm.update(qset(), now=10)
        assert d1.live_transition is False
        assert d1.hourly_update is False
        state = await AlertStateRepository(db).load()
        assert state["pending_since"] is None
        # A later entry needs a fresh, complete debounce.
        d2 = await sm.update(qset("BTC"), now=30)
        assert d2.live_transition is False
        assert d2.hourly_update is False
        d3 = await sm.update(qset("BTC"), now=50)
        assert d3.live_transition is True
        assert d3.hourly_update is False

    async def test_debounce_completion_emits_exactly_one_transition(
        self, tmp_path, db
    ):
        sm = debounce_machine(tmp_path, db)
        d0 = await sm.update(qset("BTC"), now=0)
        assert d0.live_transition is False
        assert d0.hourly_update is False
        d1 = await sm.update(qset("BTC"), now=20)
        assert d1.live_transition is True
        assert d1.hourly_update is False  # the transition owns this bucket
        assert d1.composition_update is False
        d2 = await sm.update(qset("BTC"), now=21)
        assert d2.live_transition is False
        assert d2.hourly_update is False
        d3 = await sm.update(qset("BTC"), now=3700)
        assert d3.live_transition is False
        assert d3.hourly_update is True  # confirmed state: next bucket is free
        d4 = await sm.update(qset("BTC"), now=3701)
        assert d4.live_transition is False
        assert d4.hourly_update is False

    async def test_over_range_reentry_is_debounced(self, tmp_path, db):
        sm = debounce_machine(tmp_path, db)
        await sm.update(qset("A", "B", "C", "D"), now=0)  # OVER_RANGE
        d1 = await sm.update(qset("BTC"), now=10)
        assert d1.live_transition is False
        assert d1.hourly_update is False
        d2 = await sm.update(qset("BTC"), now=30)
        assert d2.live_transition is True
        assert d2.transition_reason == "OVER_RANGE -> ACTIVE_RANGE"
        assert d2.hourly_update is False


# ----------------------------------------------------------------------
# P0-3 - atomic alert state + durable outbox
# ----------------------------------------------------------------------

class TestP03Atomicity:
    async def test_alert_state_and_outbox_commit_atomically(
        self, tmp_path, db, monkeypatch
    ):
        cfg = make_cfg(
            tmp_path,
            alert_debounce_seconds=0,
            hourly_active_alerts=False,
            composition_change_alerts=False,
        )
        repo = Repository(db)
        state_repo = AlertStateRepository(db)
        dispatcher = AlertDispatcher(FakeClient(), repo, cfg)
        service = AlertService(AlertStateMachine(cfg, state_repo), dispatcher, cfg)
        await dispatcher.start()
        try:
            await service.process(qset(), now=0)  # EMPTY baseline

            async def boom(*args, **kwargs) -> None:
                raise RuntimeError("injected failure before outbox insert")

            # Fail at the outbox-insert boundary (after state SQL, before
            # outbox SQL): a send-producing decision must roll back entirely.
            monkeypatch.setattr(Repository, "insert_outgoing_notification", boom)
            with pytest.raises(RuntimeError):
                await service.process(qset("BTC"), now=10)
            state = await state_repo.load()
            assert state["state"] == STATE_EMPTY
            assert await repo.list_notifications() == []
        finally:
            await dispatcher.stop()

    async def test_crash_after_atomic_commit_requeues_notification_on_restart(
        self, tmp_path
    ):
        path = str(tmp_path / "crash.sqlite")
        cfg = make_cfg(
            tmp_path,
            database_path=path,
            alert_debounce_seconds=0,
            hourly_active_alerts=True,
            immediate_transition_alerts=True,
            composition_change_alerts=False,
        )
        # Session 1: the decision is persisted (state + outbox) but the
        # process crashes before any Telegram delivery (worker not started).
        db = await fresh_db(path)
        repo = Repository(db)
        service = AlertService(
            AlertStateMachine(cfg, AlertStateRepository(db)),
            AlertDispatcher(FakeClient(), repo, cfg),
            cfg,
        )
        decision = await service.process(qset("BTC"), now=100)
        assert decision.live_transition is True
        rows = await repo.list_notifications()
        assert len(rows) == 1
        assert rows[0]["status"] == "pending"
        assert rows[0]["dedupe_key"] == "transition:100:BTC"
        await db.close()

        # Session 2: restart -> pending outbox is requeued and delivered.
        db = await fresh_db(path)
        client = FakeClient()
        dispatcher = AlertDispatcher(client, Repository(db), cfg)
        await dispatcher.start()
        assert await wait_for(lambda: len(client.sent) == 1)
        rows = await Repository(db).list_notifications()
        assert client.sent == [rows[0]["message"]]
        assert rows[0]["status"] == "sent"
        assert rows[0]["dedupe_key"] == "transition:100:BTC"
        await dispatcher.stop()
        await db.close()

        # Session 3: a successful row is never sent again.
        db = await fresh_db(path)
        client2 = FakeClient()
        dispatcher2 = AlertDispatcher(client2, Repository(db), cfg)
        await dispatcher2.start()
        await asyncio.sleep(0.2)
        assert client2.sent == []
        await dispatcher2.stop()
        await db.close()

    async def test_duplicate_decision_creates_one_logical_outbox_row(
        self, tmp_path, db
    ):
        cfg = make_cfg(
            tmp_path,
            alert_debounce_seconds=0,
            hourly_active_alerts=False,
            composition_change_alerts=False,
        )
        repo = Repository(db)
        service = AlertService(
            AlertStateMachine(cfg, AlertStateRepository(db)),
            AlertDispatcher(FakeClient(), repo, cfg),
            cfg,
        )
        await service.process(qset("BTC"), now=100)
        await service.process(qset("BTC"), now=100)
        rows = await repo.list_notifications()
        assert len(rows) == 1
        assert rows[0]["dedupe_key"] == "transition:100:BTC"


# ----------------------------------------------------------------------
# P0-4 - durable Telegram retry
# ----------------------------------------------------------------------

class TestP04Retry:
    async def test_failed_telegram_notification_is_retried_after_recovery(
        self, tmp_path, db
    ):
        cfg = make_cfg(tmp_path)
        client = FakeClient(fail=True)
        dispatcher = AlertDispatcher(client, Repository(db), cfg)
        await dispatcher.start()
        try:
            await dispatcher.enqueue("hello", tag="transition")
            # A transient Telegram failure must not be terminal.
            assert await wait_for(lambda: status_is(db, "retry"), timeout=5.0)
            client.fail = False
            # The retry becomes due (time passes / restart happens).
            await db.execute("UPDATE outgoing_notifications SET next_attempt_at = 0")
            await db.commit()
            dispatcher.wake()
            assert await wait_for(lambda: status_is(db, "sent"), timeout=5.0)
            rows = await Repository(db).list_notifications()
            assert rows[0]["sent_at"] is not None
            assert int(rows[0]["attempt_count"]) >= 1
        finally:
            await dispatcher.stop()

    async def test_telegram_429_retry_after_is_honored(self, tmp_path, db):
        cfg = make_cfg(tmp_path)
        retry_after_error = TelegramSendError("http_429")
        retry_after_error.retry_after = 60
        client = FakeClient(fail=True, error=retry_after_error)
        dispatcher = AlertDispatcher(client, Repository(db), cfg)
        await dispatcher.start()
        try:
            await dispatcher.enqueue("hello", tag="transition")
            assert await wait_for(lambda: status_is(db, "retry"), timeout=5.0)
            rows = await Repository(db).list_notifications()
            assert int(rows[0]["next_attempt_at"]) >= int(time.time()) + 55
        finally:
            await dispatcher.stop()

    async def test_retry_rows_do_not_spin_continuously(self, tmp_path, db):
        cfg = make_cfg(tmp_path)
        client = FakeClient(fail=True)
        dispatcher = AlertDispatcher(client, Repository(db), cfg)
        await dispatcher.start()
        try:
            await dispatcher.enqueue("hello", tag="transition")
            assert await wait_for(lambda: status_is(db, "retry"), timeout=5.0)
            await asyncio.sleep(0.3)
            rows = await Repository(db).list_notifications()
            assert int(rows[0]["attempt_count"]) == 1
        finally:
            await dispatcher.stop()


# ----------------------------------------------------------------------
# P0-5 - listing delivery acknowledgement
# ----------------------------------------------------------------------

class TestP05ListingDelivery:
    async def test_listing_is_not_marked_sent_when_only_enqueued(self, tmp_path, db):
        cfg = make_cfg(tmp_path, listing_notifications_enabled=True)
        repo = Repository(db)
        # Worker deliberately not started: enqueue must not mean delivery.
        dispatcher = AlertDispatcher(FakeClient(), repo, cfg)
        tracker = ListingTracker(
            ListingEventRepository(db), cfg, notify=listing_notify(dispatcher)
        )
        created = await tracker.handle_registry(
            result(registry_event("ABCUSDT", "new", new_status="Trading")), now=100
        )
        assert len(created) == 1
        event = (await ListingEventRepository(db).list_all())[0]
        assert event["telegram_sent"] == 0
        rows = await repo.list_notifications()
        assert len(rows) == 1
        assert rows[0]["origin_type"] == "listing"
        assert rows[0]["origin_key"] == created[0]["event_key"]
        assert rows[0]["status"] == "pending"
        assert rows[0]["dedupe_key"] == f"listing:{created[0]['event_key']}"

    async def test_listing_is_marked_sent_after_successful_delivery(self, tmp_path, db):
        cfg = make_cfg(tmp_path, listing_notifications_enabled=True)
        client = FakeClient()
        repo = Repository(db)
        dispatcher = AlertDispatcher(client, repo, cfg)
        await dispatcher.start()
        try:
            tracker = ListingTracker(
                ListingEventRepository(db), cfg, notify=listing_notify(dispatcher)
            )
            await tracker.handle_registry(
                result(registry_event("ABCUSDT", "new", new_status="Trading")), now=100
            )
            assert await wait_for(
                lambda: any("ABCUSDT" in text for text in client.sent)
            )
            rows = await repo.list_notifications()
            assert rows[0]["status"] == "sent"
            event = (await ListingEventRepository(db).list_all())[0]
            assert event["telegram_sent"] == 1
        finally:
            await dispatcher.stop()


# ----------------------------------------------------------------------
# P1-1 - real announcement schema
# ----------------------------------------------------------------------

class TestP11AnnouncementSchema:
    def test_real_bybit_announcement_schema_new_crypto(self):
        raw = {
            "id": "1851345",
            "title": "New Listing: Example (XYZ) on Bybit",
            "description": "Bybit is pleased to list Example (XYZ).",
            "type": {"title": "New Listings", "key": "new_crypto"},
            "tags": ["Spot", "Spot Listings"],
            "dateTimestamp": "1700000000000",
        }
        announcement = parse_announcement(raw)
        assert announcement.type_key == "new_crypto"
        assert announcement.type_title == "New Listings"
        assert announcement.tags == ("Spot", "Spot Listings")
        assert announcement.timestamp == 1700000000
        # Nested structured fields must not leak into the id/title fields.
        assert announcement.id == "1851345"
        assert "XYZ" in announcement.title

    async def test_announcement_classified_without_usdt_in_title(self, tmp_path, db):
        """A listing announcement is recognized even without a full symbol."""
        cfg = make_cfg(tmp_path, listing_notifications_enabled=True)
        notify_calls: list[dict] = []

        async def notify(event: dict) -> None:
            notify_calls.append(event)

        tracker = ListingTracker(ListingEventRepository(db), cfg, notify=notify)
        announcement = Announcement(
            id="9",
            title="New Listing: Example (XYZ) on Bybit",
            description="",
            type_key="new_crypto",
            type_title="New Listings",
            tags=("Spot", "Spot Listings"),
            timestamp=1700000000,
        )
        created = await tracker.handle_announcements([announcement], now=100)
        assert len(created) == 1
        assert created[0]["event_type"] == EVENT_ANNOUNCED
        # Only the base ticker is available; a full market pair must not
        # be fabricated.
        assert created[0]["symbol"] == "XYZ"


# ----------------------------------------------------------------------
# P1-2 - isPreListing
# ----------------------------------------------------------------------

class TestP12IsPreListing:
    RAW = {
        "symbol": "XYZUSDT",
        "status": "Trading",
        "baseCoin": "XYZ",
        "quoteCoin": "USDT",
        "settleCoin": "USDT",
        "contractType": "LinearPerpetual",
    }

    def test_is_pre_listing_uses_real_field(self):
        raw = dict(self.RAW, isPreListing=True)
        instrument = parse_instrument("linear", raw)
        assert instrument.is_pre_listing is True
        assert instrument.status == "Trading"

    def test_is_pre_listing_accepts_boolean_like_strings(self):
        raw = dict(self.RAW, isPreListing="true")
        assert parse_instrument("linear", raw).is_pre_listing is True

    def test_prelaunch_status_still_recognized(self):
        raw = dict(self.RAW, status="PreLaunch")
        assert parse_instrument("linear", raw).is_pre_listing is True


# ----------------------------------------------------------------------
# P1-3 - spot instruments request
# ----------------------------------------------------------------------

def ok_payload(result) -> dict:
    return {"retCode": 0, "retMsg": "OK", "result": result, "time": 1700000000000}


def spot_item(symbol: str, base: str = "BTC") -> dict:
    return {"symbol": symbol, "baseCoin": base, "quoteCoin": "USDT", "status": "Trading"}


async def _noop_sleep(_delay: float) -> None:
    return None


class TestP13SpotRequest:
    async def test_spot_instruments_request_has_no_limit_or_cursor(self):
        calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, json=ok_payload({"list": [spot_item("BTCUSDT")]}))

        client = BybitRestClient(
            transport=httpx.MockTransport(handler),
            sleep_fn=_noop_sleep,
            max_retries=0,
        )
        try:
            instruments = await client.get_spot_instruments()
            assert len(instruments) == 1
            params = calls[0].url.params
            assert params["category"] == "spot"
            assert "limit" not in params
            assert "cursor" not in params
        finally:
            await client.close()


# ----------------------------------------------------------------------
# P1-4 - discovery health timestamp
# ----------------------------------------------------------------------

class FakeRest:
    def __init__(self, spot=None, trading=None, prelaunch=None, inverse=None):
        self.spot = spot or []
        self.trading = trading or []
        self.prelaunch = prelaunch or []
        self.inverse = inverse or []

    async def get_spot_instruments(self):
        return self.spot

    async def get_linear_instruments(self, status):
        return self.prelaunch if status == "PreLaunch" else self.trading

    async def get_inverse_instruments(self):
        return self.inverse


class TestP14DiscoveryTimestamp:
    async def test_discovery_success_time_is_set_without_injected_now(self, db):
        config = make_settings(
            database_path=":memory:",
            enable_spot=True,
            enable_linear_usdt=False,
            enable_linear_usdc=False,
        )
        registry = InstrumentRegistry(InstrumentRepository(db))
        fake = FakeRest(
            spot=[
                Instrument(category="spot", symbol="BTCUSDT", base_coin="BTC", status="Trading")
            ]
        )
        discovery = InstrumentDiscovery(fake, registry, config)
        before = int(time.time())
        result = await discovery.discover_once()
        after = int(time.time())
        assert result.instrument_count == 1
        assert discovery.last_success_at is not None
        assert before <= discovery.last_success_at <= after


# ----------------------------------------------------------------------
# P1-5 - clock separation
# ----------------------------------------------------------------------

class FakeWsClient:
    def __init__(self, connected: bool, last_ticker_at: float | None = 0.0) -> None:
        self.connected = connected
        self.last_ticker_at = last_ticker_at


class FakeWsManager:
    def __init__(self, spot, linear) -> None:
        self.clients = {"spot": spot, "linear": linear}


class TestP15ClockSeparation:
    def test_websocket_client_tracks_both_clock_domains(self, tmp_path):
        cfg = make_cfg(tmp_path, bybit_ws_base_url="ws://127.0.0.1:1/v5/public")
        client = BybitWebSocketClient("spot", cfg, on_message=lambda payload: None)
        raw = json.dumps(
            {
                "topic": "tickers.BTCUSDT",
                "type": "snapshot",
                "ts": 1700000000000,
                "data": {"symbol": "BTCUSDT", "lastPrice": "105"},
            }
        )
        client._handle_raw(raw)
        # Ticker freshness must be epoch wall-clock (health uses it).
        assert client.last_ticker_at is not None
        assert abs(client.last_ticker_at - time.time()) < 10
        # Connection freshness is tracked too (separate from tickers).
        assert client.last_any_message_at is not None
        assert abs(client.last_any_message_at - time.time()) < 10
        # The stale watchdog keeps its own monotonic timestamps.
        assert abs(client.last_ticker_monotonic - time.monotonic()) < 10
        assert abs(client.last_any_message_monotonic - time.monotonic()) < 10

    async def test_health_does_not_mix_monotonic_and_epoch_time(self, config):
        now = int(time.time())
        monitor = HealthMonitor(
            config,
            ws_manager=FakeWsManager(FakeWsClient(True, now - 5), FakeWsClient(False)),
        )
        state = await monitor.snapshot(now=now)
        assert state.last_spot_ticker_age == 5

    async def test_health_age_is_never_negative(self, config):
        now = int(time.time())
        monitor = HealthMonitor(
            config,
            ws_manager=FakeWsManager(FakeWsClient(True, now + 50), FakeWsClient(False)),
        )
        state = await monitor.snapshot(now=now)
        assert state.last_spot_ticker_age == 0


# ----------------------------------------------------------------------
# P1-6 - WebSocket top-level ts
# ----------------------------------------------------------------------

async def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


class TestP16WebSocketTs:
    async def test_websocket_top_level_ts_is_preserved(self, tmp_path, db):
        snapshot = json.dumps(
            {
                "topic": "tickers.XYZUSDT",
                "type": "snapshot",
                "ts": 1700000000123,
                # Real Bybit ticker data carries no `ts` inside `data`.
                "data": {"symbol": "XYZUSDT", "lastPrice": "105", "prevPrice1h": "100"},
            }
        )
        delta = json.dumps(
            {
                "topic": "tickers.XYZUSDT",
                "type": "delta",
                "ts": 1700000001123,
                "data": {"lastPrice": "106"},
            }
        )

        async def handler(websocket):
            # Snapshot on subscribe; the delta only follows a client ping so
            # the snapshot state is observable before it is overwritten.
            try:
                async for message in websocket:
                    data = json.loads(message)
                    if data.get("op") == "subscribe":
                        await websocket.send(snapshot)
                    elif data.get("op") == "ping":
                        await websocket.send(delta)
            except Exception:
                pass

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            cfg = make_cfg(
                tmp_path,
                bybit_ws_base_url=f"ws://127.0.0.1:{port}/v5/public",
                ws_heartbeat_interval_seconds=0.2,
            )
            repo = InstrumentRepository(db)
            await repo.upsert_many(
                [
                    Instrument(
                        category="spot", symbol="XYZUSDT", base_coin="XYZ", status="Trading"
                    )
                ],
                now=100,
            )
            price_engine = PriceEngine(cfg)
            manager = WebSocketManager(InstrumentRegistry(repo), price_engine, cfg)
            stop = asyncio.Event()
            tasks = await manager.start(stop)
            await manager.sync_subscriptions()
            assert await wait_until(
                lambda: (
                    ticker := price_engine.get("spot", "XYZUSDT")
                )
                is not None
                and ticker.timestamp == 1700000000
            )
            assert await wait_until(
                lambda: (
                    ticker := price_engine.get("spot", "XYZUSDT")
                )
                is not None
                and ticker.last_price == 106
                and ticker.timestamp == 1700000001
            )
            stop.set()
            await asyncio.gather(*tasks, return_exceptions=True)


# ----------------------------------------------------------------------
# P2-1 - spot anchor tolerance wiring
# ----------------------------------------------------------------------

class TestP21SpotAnchorTolerance:
    async def test_spot_anchor_tolerance_config_is_wired(self, tmp_path, db):
        cfg = make_cfg(tmp_path, spot_anchor_tolerance_seconds=7)
        # Constructed exactly like main.py wires it.
        history = SpotHistory(
            PriceSampleRepository(db),
            sample_seconds=cfg.spot_sample_seconds,
            tolerance_seconds=cfg.spot_anchor_tolerance_seconds,
        )
        assert history.tolerance_seconds == 7


# ----------------------------------------------------------------------
# P2-4 - duplicate task registration
# ----------------------------------------------------------------------

class TestP24TaskTracking:
    async def test_no_duplicate_task_registration(self, config):
        cfg = make_settings(
            _env_file=None,
            database_path=config.database_path,
            telegram_bot_token=config.telegram_bot_token,
            telegram_chat_id=config.telegram_chat_id,
            enable_websocket=False,
            rest_fallback_enabled=False,
            listing_notifications_enabled=True,
        )
        app = Application(cfg)
        await app.start()
        try:
            ids = [id(task) for task in app._tasks]
            assert len(ids) == len(set(ids))
            assert len(app._tasks) >= 4
        finally:
            await app.shutdown()


# ----------------------------------------------------------------------
# Business rule - threshold strictness
# ----------------------------------------------------------------------

class TestThresholdStrictness:
    def test_threshold_does_not_round_before_strict_comparison(self):
        assert MomentumEngine.qualifies(5.0000001) is True
        assert MomentumEngine.qualifies(5.000001) is True
        assert MomentumEngine.qualifies(5.0000000) is False
        assert MomentumEngine.qualifies(4.9999999) is False
        assert MomentumEngine.qualifies(4.999999) is False
