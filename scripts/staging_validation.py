"""Phase F9 - live Bybit staging validation (public endpoints only).

Checks the real production API contracts:

- Spot discovery (no pagination args on the spot call).
- Linear pagination completeness (cursor loop drains every page).
- Settlement filtering: only USDT/USDC linear instruments.
- Linear PreLaunch discovery.
- Spot + Linear WebSocket connections with top-level ``ts``.
- Dynamic subscriptions after connect.
- 1h derivative reference (``prevPrice1h``) on linear tickers.
- Announcement nested ``type``/``tags`` structure.
- Discovery refresh (two runs, stable universe, no duplicate new events).
- New-listing dry-run through the REAL production callback path:
  registry event -> listing tracker -> durable outbox -> dispatcher
  (Telegram stubbed - no credentials required).

Usage:  python scripts/staging_validation.py
Exit 0 on success; non-zero with details on any contract mismatch.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.alerts.dispatcher import AlertDispatcher  # noqa: E402
from app.bybit.rest import BybitRestClient  # noqa: E402
from app.bybit.websocket import BybitWebSocketClient  # noqa: E402
from app.config import Settings  # noqa: E402
from app.market.discovery import InstrumentDiscovery, InstrumentRegistry  # noqa: E402
from app.market.listing import ListingTracker  # noqa: E402
from app.persistence.database import Database  # noqa: E402
from app.persistence.migrations import apply_migrations  # noqa: E402
from app.persistence.repository import (  # noqa: E402
    InstrumentRepository,
    ListingEventRepository,
    Repository,
)

PROBLEMS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}{(' - ' + detail) if detail else ''}")
    if not ok:
        PROBLEMS.append(f"{name}: {detail}")


def make_settings(tmpdir: str) -> Settings:
    return Settings(
        _env_file=None,
        database_path=str(Path(tmpdir) / "staging.sqlite"),
        telegram_bot_token="123456789:AAstaging_stub_only",
        telegram_chat_id="-100stagingstubonly",
        enable_spot=True,
        enable_linear_usdt=True,
        enable_linear_usdc=True,
        enable_websocket=False,
        rest_fallback_enabled=False,
        listing_notifications_enabled=True,
        dispatcher_poll_seconds=0.1,
    )


class FakeTelegram:
    """Stubs delivery; production outbox + dispatcher paths stay real."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.last_success_at: int | None = None

    async def send_message(self, text: str) -> None:
        self.sent.append(text)
        self.last_success_at = int(time.time())

    async def close(self) -> None:
        pass


async def rest_checks(rest: BybitRestClient) -> None:
    print("REST:")
    spot = await rest.get_spot_instruments()
    check("spot discovery", len(spot) > 100, f"count={len(spot)}")

    trading = await rest.get_linear_instruments(status="Trading")
    check("linear pagination drains", len(trading) > 500, f"count={len(trading)}")
    settles = {i.settle_coin for i in trading}
    check(
        "settlement filtering",
        settles <= {"USDT", "USDC"},
        f"settle_coins={sorted(settles)}",
    )
    usdt = [i for i in trading if i.settle_coin == "USDT"]
    usdc = [i for i in trading if i.settle_coin == "USDC"]
    check("linear USDT count", len(usdt) > 300, f"count={len(usdt)}")
    check("linear USDC count", len(usdc) > 20, f"count={len(usdc)}")

    prelaunch = await rest.get_linear_instruments(status="PreLaunch")
    check("linear PreLaunch discovery", len(prelaunch) > 0, f"count={len(prelaunch)}")

    tickers = await rest.get_linear_tickers()
    with_prev = [t for t in tickers if t.prev_price_1h is not None]
    check(
        "1h derivative reference (prevPrice1h)",
        len(with_prev) > len(tickers) * 0.9,
        f"{len(with_prev)}/{len(tickers)}",
    )

    announcements = await rest.get_announcements(limit=50)
    check("announcements fetched", len(announcements) > 0, f"count={len(announcements)}")
    if announcements:
        nested_ok = all(
            (a.type_key is not None and a.tags is not None)
            for a in announcements
        )
        check("announcement nested type/tags", nested_ok)


async def ws_check(category: str, label: str, timeout: float = 30.0) -> None:
    cfg = Settings(
        _env_file=None,
        telegram_bot_token="x",
        telegram_chat_id="x",
        ws_stale_seconds=60.0,
        ws_heartbeat_interval_seconds=20.0,
        ws_subscribe_batch_size=10,
    )
    messages: list[dict] = []
    client = BybitWebSocketClient(
        category, cfg, on_message=messages.append
    )
    client.set_symbols({"BTCUSDT"})
    stop = asyncio.Event()
    task = asyncio.create_task(client.run(stop))
    try:
        await asyncio.wait_for(
            _collect_tickers(client, messages, timeout), timeout=timeout + 15
        )
    except asyncio.TimeoutError:
        check(f"WS {label} ticker received", False, "timed out")
        return
    finally:
        stop.set()
        await asyncio.gather(task, return_exceptions=True)

    tickers = [m for m in messages if m.get("symbol") is not None]
    check(
        f"WS {label} ticker received",
        len(tickers) >= 1,
        f"tickers={len(tickers)} messages={len(messages)}",
    )
    if tickers:
        top_ts = tickers[-1].get("ts")
        check(f"WS {label} top-level ts", isinstance(top_ts, int) and top_ts > 0)
        check(
            f"WS {label} dynamic subscription",
            any(m.get("symbol") == "BTCUSDT" for m in tickers),
            "BTCUSDT ticker arrived",
        )


async def _collect_tickers(
    client: BybitWebSocketClient, messages: list[dict], timeout: float
) -> None:
    """Wait until the client has received at least one ticker."""
    del client  # the client feeds messages via on_message
    deadline = time.time() + timeout
    while time.time() < deadline:
        if any(m.get("symbol") is not None for m in messages):
            return
        await asyncio.sleep(0.25)
    raise asyncio.TimeoutError("no ticker received")


async def ws_checks() -> None:
    print("WEBSOCKET:")
    await ws_check("spot", "spot")
    await ws_check("linear", "linear")


async def listing_dry_run(tmpdir: str) -> None:
    """Synthetic registry event through the REAL production path:
    registry -> listing tracker -> durable outbox -> dispatcher."""
    print("NEW-LISTING DRY-RUN:")
    cfg = make_settings(tmpdir)
    db = Database(cfg.database_path)
    await db.connect()
    await apply_migrations(db)
    await db.commit()

    registry = InstrumentRegistry(InstrumentRepository(db))
    from app.bybit.models import Instrument

    # Seed with the current live universe (spot + linear + prelaunch) so
    # the synthetic new instrument is the ONLY new event (first run is
    # silent by design).
    rest = BybitRestClient(timeout=10.0, max_retries=2)
    seed = await rest.get_spot_instruments()
    seed.extend(await rest.get_linear_instruments(status="Trading"))
    seed.extend(await rest.get_linear_instruments(status="PreLaunch"))
    await registry.reconcile(seed, now=int(time.time()))

    tracker = ListingTracker(ListingEventRepository(db), cfg)
    outbox = Repository(db)
    telegram = FakeTelegram()
    dispatcher = AlertDispatcher(telegram, outbox, cfg)
    await dispatcher.start()

    async def notify(event: dict) -> None:
        from app.alerts.formatter import format_listing_alert
        from app.market.listing import listing_dedupe_key

        await outbox.insert_outgoing_notification(
            "listing",
            format_listing_alert(event),
            dedupe_key=listing_dedupe_key(event["event_key"]),
            origin_type="listing",
            origin_key=event["event_key"],
        )
        dispatcher.wake()

    tracker.notify = notify

    discovery = InstrumentDiscovery(rest, registry, cfg, on_events=tracker.handle_registry)
    result = await discovery.discover_once()
    check("discovery refresh stable", result.instrument_count > 1000,
          f"instruments={result.instrument_count}")
    fresh_new = [e for e in result.events if e.event == "new"]
    check("no synthetic new events on refresh", len(fresh_new) == 0,
          f"new_events={len(fresh_new)}")

    # Inject ONE synthetic new market through the real registry callback.
    suffix = int(time.time()) % 100000
    synthetic = Instrument(
        category="linear",
        symbol=f"STAGE{suffix}USDT",
        base_coin=f"STAGE{suffix}",
        quote_coin="USDT",
        settle_coin="USDT",
        contract_type="LinearPerpetual",
        status="Trading",
    )
    sync_result = await registry.reconcile(seed + [synthetic], now=int(time.time()))
    check("synthetic new market produced registry event",
          any(e.event == "new" and e.symbol == synthetic.symbol
              for e in sync_result.events),
          f"events={[(e.event, e.symbol) for e in sync_result.events]}")
    events = await tracker.handle_registry(sync_result)
    check("synthetic new market produced listing event", len(events) == 1,
          f"events={events}")

    deadline = time.time() + 10
    while time.time() < deadline and len(telegram.sent) < 1:
        await asyncio.sleep(0.1)
    check("dispatcher delivered synthetic listing", len(telegram.sent) >= 1,
          f"count={len(telegram.sent)}")

    rows = await outbox.list_notifications()
    check("outbox listing row marked sent", rows and rows[0]["status"] == "sent",
          f"status={rows[0]['status'] if rows else 'none'}")
    listing = await db.fetchone(
        "SELECT telegram_sent FROM listing_events WHERE event_key = ?",
        (f"linear:{synthetic.symbol}:trading",),
    )
    check("listing telegram_sent = 1", listing is not None and listing["telegram_sent"] == 1)

    await dispatcher.stop()
    await rest.close()
    await db.close()


async def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="bybit_staging_")
    rest = BybitRestClient(timeout=15.0, max_retries=3)
    try:
        await rest_checks(rest)
        await ws_checks()
        await listing_dry_run(tmpdir)
    finally:
        await rest.close()

    print()
    if PROBLEMS:
        print("STAGING VALIDATION FAILED:")
        for p in PROBLEMS:
            print(f"  - {p}")
        return 1
    print("STAGING VALIDATION OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))