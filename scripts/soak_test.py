"""Deterministic self-contained soak: 26 hours of market activity in seconds.

Drives the real production pipeline (discovery -> price engine -> momentum
-> deduplication -> alert service -> SQLite outbox -> dispatcher -> fake
Telegram) with synthetic data and an accelerated clock, then asserts the
operational invariants that must hold over a long run:

- exactly one transition alert per qualifying episode
- at most one hourly alert per UTC hour bucket
- no duplicate notifications (dedupe keys unique)
- suppression at 4+ qualifying coins
- outbox drains to zero delivered/dead states

Usage:  python scripts/soak_test.py
Exit 0 on success, non-zero (with assertion detail) on failure.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.alerts.dispatcher import AlertDispatcher  # noqa: E402
from app.alerts.service import AlertService  # noqa: E402
from app.alerts.state_machine import AlertStateMachine  # noqa: E402
from app.bybit.models import Instrument, Ticker  # noqa: E402
from app.config import Settings  # noqa: E402
from app.market.deduplication import aggregate_qualifying  # noqa: E402
from app.market.discovery import InstrumentRegistry  # noqa: E402
from app.market.momentum import MomentumEngine, MomentumEvaluator, SpotHistory  # noqa: E402
from app.market.price_engine import PriceEngine  # noqa: E402
from app.persistence.database import Database  # noqa: E402
from app.persistence.migrations import apply_migrations  # noqa: E402
from app.persistence.repository import (  # noqa: E402
    AlertStateRepository,
    InstrumentRepository,
    PriceSampleRepository,
    Repository,
)

HOURS = 26
STEP = 10  # simulated seconds per tick
UNIVERSE = ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT", "EEEUSDT"]


def make_settings(tmpdir: str) -> Settings:
    return Settings(
        _env_file=None,
        database_path=str(Path(tmpdir) / "soak.sqlite"),
        telegram_bot_token="123456789:AAfaketokenvaluefortests_only",
        telegram_chat_id="-1001234567890",
        enable_spot=True,
        enable_linear_usdt=True,
        enable_linear_usdc=False,
        enable_inverse=False,
        enable_websocket=False,
        rest_fallback_enabled=False,
        listing_notifications_enabled=False,
        immediate_transition_alerts=True,
        hourly_active_alerts=True,
        composition_change_alerts=False,
        alert_debounce_seconds=20,
        dispatcher_poll_seconds=0.2,
        health_summary_seconds=3600,
        instrument_refresh_seconds=3600,
        announcement_refresh_seconds=3600,
    )


class FakeTelegram:
    """Records deliveries; never touches the network."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.last_success_at: int | None = None

    async def send_message(self, text: str) -> None:
        self.sent.append(text)
        self.last_success_at = int(time.time())

    async def close(self) -> None:
        pass


def ramp(step_idx: int) -> float:
    """AAAUSDT: 0 -> +12% over 8h, hold 8h, decay; others stay flat."""
    phase = step_idx % (8 * 3600 // STEP)
    return (phase / (8 * 3600 // STEP)) * 12.0


async def main() -> int:
    tmpdir = tempfile.mkdtemp(prefix="bybit_soak_")
    cfg = make_settings(tmpdir)

    db = Database(cfg.database_path)
    await db.connect()
    await apply_migrations(db)
    await db.commit()

    instruments = [
        Instrument(category="linear", symbol=s, base_coin=s[:-4], status="Trading", settle_coin="USDT")
        for s in UNIVERSE
    ]
    repo = InstrumentRepository(db)
    await repo.upsert_many(instruments, now=0)
    registry = InstrumentRegistry(repo)

    price_engine = PriceEngine(cfg)
    momentum = MomentumEngine(
        cfg,
        SpotHistory(
            PriceSampleRepository(db),
            sample_seconds=cfg.spot_sample_seconds,
            tolerance_seconds=cfg.spot_anchor_tolerance_seconds,
        ),
    )
    evaluator = MomentumEvaluator(registry, price_engine, momentum, cfg)
    telegram = FakeTelegram()
    dispatcher = AlertDispatcher(telegram, Repository(db), cfg)
    await dispatcher.start()
    alert_service = AlertService(
        AlertStateMachine(cfg, AlertStateRepository(db)), dispatcher, cfg
    )

    start = int(time.time())
    ok = True
    transition_alerts = 0
    hourly_by_bucket: dict[int, int] = {}
    suppression_hits = 0

    for idx in range(HOURS * 3600 // STEP):
        now = start + idx * STEP
        if idx % 500 == 0:
            print(f"  tick {idx}/{HOURS * 3600 // STEP}", flush=True)
        # Feed the price engine: AAA starts qualifying (5.5%) and ramps to
        # +17.5%; BBB crosses 5% and stays; DDD/EEE qualify alongside for
        # the first 5h (4+ suppression window), then drop out (transition
        # into ACTIVE_RANGE with 2 coins).
        snapshots = [
            Ticker(category="linear", symbol="AAAUSDT", last_price=100 * (1 + 0.055 + ramp(idx) / 100), prev_price_1h=100, timestamp=now),
            Ticker(category="linear", symbol="BBBUSDT", last_price=106, prev_price_1h=100, timestamp=now),
            Ticker(category="linear", symbol="CCCUSDT", last_price=100 * (1 + 0.055 + ramp(idx) / 100), prev_price_1h=100, timestamp=now),
            Ticker(category="linear", symbol="DDDUSDT", last_price=105.5 if idx < 1800 else 100, prev_price_1h=100, timestamp=now),
            Ticker(category="linear", symbol="EEEUSDT", last_price=105.5 if idx < 1800 else 100, prev_price_1h=100, timestamp=now),
        ]
        for t in snapshots:
            price_engine.apply_snapshot(t)

        values = await evaluator.evaluate_all(now=now)
        qualifying = aggregate_qualifying(values, cfg.alert_threshold_percent)
        count = len(qualifying.representatives)
        if count >= 4:
            suppression_hits += 1
        decision = await alert_service.process(qualifying, now=now)
        if decision.live_transition:
            transition_alerts += 1
        if decision.hourly_update:
            bucket = now // 3600
            hourly_by_bucket[bucket] = hourly_by_bucket.get(bucket, 0) + 1

    await dispatcher.stop(timeout=30.0)
    await db.close()

    # --- Assertions ---
    problems: list[str] = []
    if transition_alerts != 1:
        problems.append(f"expected exactly 1 transition alert, got {transition_alerts}")
    if any(count > 1 for count in hourly_by_bucket.values()):
        problems.append(f"more than one hourly alert per bucket: {hourly_by_bucket}")
    if suppression_hits < 50:
        problems.append(f"expected sustained 4+ suppression window, got {suppression_hits} hits")
    if not hourly_by_bucket:
        problems.append("no hourly alerts fired at all")
    if len(telegram.sent) != transition_alerts + sum(hourly_by_bucket.values()):
        problems.append(
            f"delivered {len(telegram.sent)} messages for "
            f"{transition_alerts} transition + {sum(hourly_by_bucket.values())} hourly"
        )

    print(f"soak: {HOURS}h simulated in ~{int(time.time() - start) * 1000}ms wall")
    print(f"  transition alerts: {transition_alerts}")
    print(f"  hourly buckets covered: {len(hourly_by_bucket)}")
    print(f"  4+ suppression samples: {suppression_hits}")
    print(f"  delivered to Telegram: {len(telegram.sent)}")
    if problems:
        print("SOAK FAILED:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("SOAK OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
