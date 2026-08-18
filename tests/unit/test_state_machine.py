"""Phase 7 - alert state machine tests."""

from __future__ import annotations

import json

import pytest

from app.alerts.state_machine import (
    STATE_ACTIVE_RANGE,
    STATE_EMPTY,
    STATE_OVER_RANGE,
    AlertStateMachine,
    classify_state,
    hourly_bucket,
)
from app.market.deduplication import QualifyingSet, RepresentativeMarket
from app.market.momentum import MomentumValue
from app.persistence.repository import AlertStateRepository
from tests.conftest import make_settings


def coin(change_1h: float = 6.0) -> MomentumValue:
    return MomentumValue(
        category="linear",
        symbol="XUSDT",
        base_coin="X",
        change_1h=change_1h,
        status="OK",
        settle_coin="USDT",
        quote_coin="USDT",
    )


def qset(*coins: str, change: float = 6.0) -> QualifyingSet:
    reps = [
        RepresentativeMarket(base_coin=c, representative=coin(change))
        for c in sorted(coins)
    ]
    return QualifyingSet(reps)


def machine(config, db):
    return AlertStateMachine(config, AlertStateRepository(db))


def default_config(tmp_path, **overrides):
    overrides.setdefault("database_path", str(tmp_path / "alert.sqlite"))
    overrides.setdefault("telegram_bot_token", "123456789:AAfaketokenvaluetests_only")
    overrides.setdefault("telegram_chat_id", "-1001234567890")
    return make_settings(**overrides)


class TestClassify:
    def test_states(self):
        assert classify_state(0, 1, 3) == STATE_EMPTY
        assert classify_state(1, 1, 3) == STATE_ACTIVE_RANGE
        assert classify_state(3, 1, 3) == STATE_ACTIVE_RANGE
        assert classify_state(4, 1, 3) == STATE_OVER_RANGE
        assert classify_state(10, 1, 3) == STATE_OVER_RANGE

    def test_hourly_bucket_format(self):
        bucket = hourly_bucket(1787015820)
        assert len(bucket) == 13
        assert bucket[4] == "-" and bucket[7] == "-" and bucket[10] == "-"


class TestRangeRule:
    async def test_0_no_alert(self, tmp_path, db):
        sm = machine(default_config(tmp_path), db)
        decision = await sm.update(qset(), now=100)
        assert decision.live_transition is False
        assert decision.state == STATE_EMPTY

    async def test_1_alert(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        decision = await sm.update(qset("BTC"), now=100)
        assert decision.live_transition is True
        assert decision.state == STATE_ACTIVE_RANGE

    async def test_2_alert(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        decision = await sm.update(qset("BTC", "ETH"), now=100)
        assert decision.live_transition is True

    async def test_3_alert(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        decision = await sm.update(qset("BTC", "ETH", "SOL"), now=100)
        assert decision.live_transition is True

    async def test_4_no_alert(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        decision = await sm.update(qset("BTC", "ETH", "SOL", "DOGE"), now=100)
        assert decision.live_transition is False
        assert decision.state == STATE_OVER_RANGE

    async def test_10_no_alert(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        decision = await sm.update(qset("A", "B", "C", "D", "E", "F", "G", "H", "I", "J"), now=100)
        assert decision.live_transition is False
        assert decision.state == STATE_OVER_RANGE


class TestTransitions:
    async def test_0_to_1_alerts(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        await sm.update(qset(), now=0)
        decision = await sm.update(qset("BTC"), now=10)
        assert decision.live_transition is True
        assert decision.transition_reason == "EMPTY -> ACTIVE_RANGE"

    async def test_1_to_2_no_duplicate(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        await sm.update(qset("BTC"), now=0)
        decision = await sm.update(qset("BTC", "ETH"), now=10)
        assert decision.live_transition is False
        assert decision.composition_update is False

    async def test_2_to_3_no_duplicate(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        await sm.update(qset("BTC", "ETH"), now=0)
        decision = await sm.update(qset("BTC", "ETH", "SOL"), now=10)
        assert decision.live_transition is False

    async def test_3_to_4_suppress(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        await sm.update(qset("BTC", "ETH", "SOL"), now=0)
        decision = await sm.update(qset("BTC", "ETH", "SOL", "DOGE"), now=10)
        assert decision.live_transition is False
        assert decision.state == STATE_OVER_RANGE

    async def test_4_to_3_alerts(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        await sm.update(qset("BTC", "ETH", "SOL", "DOGE"), now=0)
        decision = await sm.update(qset("BTC", "ETH", "SOL"), now=10)
        assert decision.live_transition is True
        assert decision.transition_reason == "OVER_RANGE -> ACTIVE_RANGE"

    async def test_3_to_0_resets(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        await sm.update(qset("BTC", "ETH", "SOL"), now=0)
        decision = await sm.update(qset(), now=10)
        assert decision.live_transition is False
        assert decision.state == STATE_EMPTY

    async def test_4_to_0_silent(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        await sm.update(qset("BTC", "ETH", "SOL", "DOGE"), now=0)
        decision = await sm.update(qset(), now=10)
        assert decision.live_transition is False
        assert decision.state == STATE_EMPTY


class TestDebounce:
    async def test_noisy_sequence_single_message(self, tmp_path, db):
        """5.01 -> 4.99 -> 5.02 must not produce repeated messages."""
        sm = machine(default_config(tmp_path, alert_debounce_seconds=20), db)
        assert (await sm.update(qset("BTC"), now=0)).live_transition is False
        assert (await sm.update(qset(), now=10)).live_transition is False
        assert (await sm.update(qset("BTC"), now=15)).live_transition is False
        assert (await sm.update(qset("BTC"), now=35)).live_transition is True
        assert (await sm.update(qset(), now=40)).live_transition is False
        assert (await sm.update(qset("BTC"), now=41)).live_transition is False
        assert (await sm.update(qset("BTC"), now=61)).live_transition is True

    async def test_restart_resumes_pending_debounce(self, tmp_path, db):
        cfg = default_config(tmp_path, alert_debounce_seconds=20)
        sm1 = machine(cfg, db)
        await sm1.update(qset("BTC"), now=0)
        sm2 = machine(cfg, db)
        decision = await sm2.update(qset("BTC"), now=15)
        assert decision.live_transition is False
        decision = await sm2.update(qset("BTC"), now=20)
        assert decision.live_transition is True

    async def test_transition_alerts_disabled(self, tmp_path, db):
        sm = machine(
            default_config(tmp_path, alert_debounce_seconds=0, immediate_transition_alerts=False),
            db,
        )
        decision = await sm.update(qset("BTC"), now=0)
        assert decision.live_transition is False


class TestComposition:
    async def test_composition_change_alert_off_by_default(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        await sm.update(qset("BTC"), now=0)
        decision = await sm.update(qset("BTC", "ETH"), now=10)
        assert decision.composition_update is False

    async def test_composition_change_fires_and_cooldown_holds(self, tmp_path, db):
        sm = machine(
            default_config(
                tmp_path,
                alert_debounce_seconds=0,
                composition_change_alerts=True,
                composition_change_cooldown_seconds=300,
            ),
            db,
        )
        await sm.update(qset("BTC"), now=0)
        assert (await sm.update(qset("BTC", "ETH"), now=10)).composition_update is True
        assert (await sm.update(qset("BTC", "SOL"), now=20)).composition_update is False
        assert (await sm.update(qset("BTC", "DOGE"), now=310)).composition_update is True

    async def test_composition_churn_does_not_spam(self, tmp_path, db):
        sm = machine(
            default_config(
                tmp_path,
                alert_debounce_seconds=0,
                composition_change_alerts=True,
                composition_change_cooldown_seconds=300,
            ),
            db,
        )
        await sm.update(qset("BTC"), now=0)
        messages = 0
        for t in range(10, 310, 10):
            decision = await sm.update(qset(f"COIN{t}"), now=t)
            messages += int(decision.composition_update)
        assert messages == 1


class TestHourly:
    async def test_hourly_once_per_bucket(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        d1 = await sm.update(qset("BTC"), now=100)
        d2 = await sm.update(qset("BTC"), now=200)
        assert d1.hourly_update is True or d1.live_transition is True
        assert d2.hourly_update is False
        assert d2.live_transition is False

    async def test_hourly_fires_in_next_bucket(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        await sm.update(qset("BTC"), now=100)
        decision = await sm.update(qset("BTC"), now=3700)
        assert decision.hourly_update is True

    async def test_no_hourly_when_out_of_range(self, tmp_path, db):
        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        await sm.update(qset("BTC", "ETH", "SOL", "DOGE"), now=100)
        decision = await sm.update(qset("BTC", "ETH", "SOL", "DOGE"), now=3700)
        assert decision.hourly_update is False

    async def test_hourly_disabled(self, tmp_path, db):
        sm = machine(
            default_config(tmp_path, alert_debounce_seconds=0, hourly_active_alerts=False),
            db,
        )
        decision = await sm.update(qset("BTC"), now=100)
        assert decision.hourly_update is False

    async def test_restart_does_not_duplicate_hourly(self, tmp_path, db):
        cfg = default_config(tmp_path, alert_debounce_seconds=0)
        sm1 = machine(cfg, db)
        await sm1.update(qset("BTC"), now=100)
        sm2 = machine(cfg, db)
        decision = await sm2.update(qset("BTC"), now=500)
        assert decision.hourly_update is False
        assert decision.live_transition is False


class TestPersistence:
    async def test_restart_keeps_state_and_fingerprint(self, tmp_path, db):
        cfg = default_config(tmp_path, alert_debounce_seconds=0)
        sm1 = machine(cfg, db)
        await sm1.update(qset("BTC", "ETH"), now=100)
        row = await db.fetchone("SELECT * FROM alert_state WHERE id = 1")
        assert row["state"] == STATE_ACTIVE_RANGE  # type: ignore[index]
        assert json.loads(row["fingerprint"]) == ["BTC", "ETH"]  # type: ignore[index]

        sm2 = machine(cfg, db)
        decision = await sm2.update(qset("BTC", "ETH"), now=200)
        assert decision.live_transition is False
        assert decision.composition_update is False

    async def test_restart_while_active_no_duplicate_transition(self, tmp_path, db):
        """Restart with unchanged market state must not re-alert (19.5)."""
        cfg = default_config(tmp_path, alert_debounce_seconds=0)
        sm1 = machine(cfg, db)
        await sm1.update(qset("BTC"), now=100)
        sm2 = machine(cfg, db)
        decision = await sm2.update(qset("BTC"), now=500)
        assert decision.live_transition is False


class TestContract:
    async def test_machine_consumes_only_unique_coin_result(self, tmp_path, db):
        """Raw contract count never reaches the alert policy (Phase 6 gate)."""
        from app.market.deduplication import aggregate_qualifying
        from app.market.momentum import MomentumValue

        values = [
            MomentumValue("linear", "XYZUSDT", "XYZ", 5.9, "OK"),
            MomentumValue("linear", "XYZUSDC", "XYZ", 8.4, "OK"),
            MomentumValue("spot", "XYZUSDT", "XYZ", 8.1, "OK"),
            MomentumValue("linear", "ABCUSDT", "ABC", 6.2, "OK"),
        ]
        qualifying = aggregate_qualifying(values)
        assert qualifying.count == 2

        sm = machine(default_config(tmp_path, alert_debounce_seconds=0), db)
        decision = await sm.update(qualifying, now=0)
        assert decision.qualifying_count == 2
        assert decision.state == STATE_ACTIVE_RANGE