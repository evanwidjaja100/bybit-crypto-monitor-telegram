"""Phase H3 - container health must represent critical application failure.

The HealthMonitor classifies each snapshot (healthy/degraded/unhealthy),
persists it into the ``kv`` table, and ``scripts/container_healthcheck.py``
turns the persisted snapshot + heartbeat freshness into an exit code.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from app.bybit.models import Instrument
from app.health.monitor import HealthMonitor
from app.persistence.repository import Repository
from tests.conftest import make_settings

ROOT = Path(__file__).resolve().parents[2]
HEALTHCHECK = ROOT / "scripts" / "container_healthcheck.py"


class FakeWsClient:
    def __init__(
        self,
        connected: bool,
        last_ticker_at: float | None = 0.0,
        last_message_at: float | None = None,
    ) -> None:
        self.connected = connected
        self.last_ticker_at = last_ticker_at
        self.last_message_at = last_message_at


class FakeWsManager:
    def __init__(self, spot, linear) -> None:
        self.clients = {"spot": spot, "linear": linear}


class FakeDiscovery:
    def __init__(self, last_success_at) -> None:
        self.last_success_at = last_success_at


class FakeTelegram:
    def __init__(
        self, last_success_at=None, last_error_at=None, last_error_type=None
    ) -> None:
        self.last_success_at = last_success_at
        self.last_error_at = last_error_at
        self.last_error_type = last_error_type


class FakeDispatcher:
    def __init__(self, worker_healthy: bool = True) -> None:
        self.worker_healthy = worker_healthy

    async def depth(self) -> int:
        return 0


class FakeDatabase:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy

    async def fetchone(self, sql: str):
        if not self.healthy:
            raise RuntimeError("database is gone")
        return {"one": 1}


def make_monitor(config, **overrides) -> HealthMonitor:
    now = time.time()
    defaults = dict(
        discovery=FakeDiscovery(now - 10),
        ws_manager=FakeWsManager(
            FakeWsClient(True, now - 5),
            FakeWsClient(True, now - 3),
        ),
        telegram=FakeTelegram(last_success_at=now - 60),
        dispatcher=FakeDispatcher(),
        database=FakeDatabase(),
        repository=None,
    )
    defaults.update(overrides)
    return HealthMonitor(config, **defaults)


def config_with(tmp_path, **overrides) -> object:
    overrides.setdefault("database_path", str(tmp_path / "health.sqlite"))
    overrides.setdefault("telegram_bot_token", "fake")
    overrides.setdefault("telegram_chat_id", "-100fake")
    return make_settings(**overrides)


class TestClassification:
    async def test_healthy_state_is_healthy(self, tmp_path):
        config = config_with(tmp_path, critical_health_failure_seconds=0)
        monitor = make_monitor(config)
        state = await monitor.snapshot()
        assert state.overall == "healthy"
        assert state.critical_issues == []

    async def test_temporary_telegram_failure_is_degraded_not_critical(
        self, tmp_path
    ):
        config = config_with(tmp_path, critical_health_failure_seconds=0)
        monitor = make_monitor(
            config,
            telegram=FakeTelegram(last_success_at=None, last_error_at=1_000),
        )
        state = await monitor.snapshot(now=1_700_000_000)
        assert state.overall == "degraded"
        assert state.degraded_issues == ["telegram"]
        assert state.critical_issues == []

    async def test_disabled_stream_is_not_required_for_health(self, tmp_path):
        config = config_with(
            tmp_path,
            enable_spot=False,
            enable_linear_usdt=False,
            enable_linear_usdc=True,
            critical_health_failure_seconds=0,
        )
        # Spot is disabled and its fake client is dead; only the enabled
        # Linear stream is required.
        now = time.time()
        monitor = make_monitor(
            config,
            ws_manager=FakeWsManager(
                FakeWsClient(False), FakeWsClient(True, now - 3)
            ),
        )
        state = await monitor.snapshot()
        assert state.overall == "healthy"
        assert state.critical_issues == []

    async def test_spot_ws_stale_becomes_unhealthy_after_grace(self, tmp_path):
        config = config_with(tmp_path, critical_health_failure_seconds=0.2)
        monitor = make_monitor(
            config,
            ws_manager=FakeWsManager(
                FakeWsClient(False), FakeWsClient(True, time.time() - 3)
            ),
        )
        first = await monitor.snapshot()
        assert first.overall == "healthy"  # within grace
        await asyncio.sleep(0.3)
        second = await monitor.snapshot()
        assert second.overall == "unhealthy"
        assert "spot_ws" in second.critical_issues

    async def test_linear_ws_stale_becomes_unhealthy_after_grace(self, tmp_path):
        config = config_with(tmp_path, critical_health_failure_seconds=0.2)
        monitor = make_monitor(
            config,
            ws_manager=FakeWsManager(
                FakeWsClient(True, time.time() - 3), FakeWsClient(False)
            ),
        )
        assert (await monitor.snapshot()).overall == "healthy"
        await asyncio.sleep(0.3)
        state = await monitor.snapshot()
        assert state.overall == "unhealthy"
        assert "linear_ws" in state.critical_issues

    async def test_dispatcher_unhealthy_becomes_critical_after_grace(
        self, tmp_path
    ):
        config = config_with(tmp_path, critical_health_failure_seconds=0.2)
        monitor = make_monitor(config, dispatcher=FakeDispatcher(worker_healthy=False))
        assert (await monitor.snapshot()).overall == "healthy"
        await asyncio.sleep(0.3)
        state = await monitor.snapshot()
        assert state.overall == "unhealthy"
        assert "dispatcher" in state.critical_issues

    async def test_rest_stale_becomes_unhealthy_after_grace(self, tmp_path):
        config = config_with(tmp_path, critical_health_failure_seconds=0.2)
        monitor = make_monitor(config, discovery=FakeDiscovery(None))
        assert (await monitor.snapshot()).overall == "healthy"
        await asyncio.sleep(0.3)
        state = await monitor.snapshot()
        assert state.overall == "unhealthy"
        assert "rest" in state.critical_issues

    async def test_database_failure_is_immediately_unhealthy(self, tmp_path):
        config = config_with(tmp_path, critical_health_failure_seconds=180)
        monitor = make_monitor(config, database=FakeDatabase(healthy=False))
        state = await monitor.snapshot()
        assert state.overall == "unhealthy"
        assert "database" in state.critical_issues


class TestPersistedSnapshot:
    async def test_health_snapshot_persisted(self, tmp_path, db, monkeypatch):
        config = config_with(
            tmp_path,
            database_path=db.path,
            health_summary_seconds=0.05,
            health_write_interval_seconds=0.05,
        )
        monitor = make_monitor(config, repository=Repository(db))
        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run_forever(stop))
        try:
            await asyncio.sleep(0.2)
        finally:
            stop.set()
            await task
        raw = await Repository(db).kv_get("health:snapshot")
        assert raw is not None
        snapshot = json.loads(raw)
        assert snapshot["overall"] == "healthy"
        assert snapshot["database"] == "ok"
        assert snapshot["dispatcher"] == "ok"
        assert snapshot["rest"] == "ok"
        assert snapshot["spot_ws"] == "ok"
        assert snapshot["linear_ws"] == "ok"
        assert abs(int(time.time()) - snapshot["last_updated_at"]) < 5

    async def test_unhealthy_overall_persisted(self, tmp_path, db):
        config = config_with(tmp_path, database_path=db.path, critical_health_failure_seconds=0)
        monitor = make_monitor(
            config,
            repository=Repository(db),
            database=FakeDatabase(healthy=False),
        )
        await monitor.snapshot()
        await monitor.persist_snapshot(monitor.last_state)
        snapshot = json.loads(await Repository(db).kv_get("health:snapshot"))
        assert snapshot["overall"] == "unhealthy"
        assert snapshot["database"] == "fail"
        assert "database" in snapshot["critical_issues"]


# ----------------------------------------------------------------------
# Container healthcheck script (real subprocess against a temp SQLite)
# ----------------------------------------------------------------------

def write_kv_db(path: str, payload: dict, updated_at: int | None = None) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
            (
                "health:snapshot",
                json.dumps(payload, sort_keys=True),
                updated_at if updated_at is not None else int(time.time()),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def healthy_payload(**overrides) -> dict:
    payload = {
        "last_updated_at": int(time.time()),
        "overall": "healthy",
        "database": "ok",
        "dispatcher": "ok",
        "rest": "ok",
        "spot_ws": "ok",
        "linear_ws": "ok",
        "critical_issues": [],
    }
    payload.update(overrides)
    return payload


def run_healthcheck(
    db_path: str, extra_env: dict | None = None
) -> tuple[int, str]:
    env = {"DATABASE_PATH": db_path, "PATH": "x"}  # minimal env, no secrets
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        [sys.executable, str(HEALTHCHECK)],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


class TestContainerHealthcheck:
    def test_container_healthcheck_passes_when_critical_subsystems_healthy(
        self, tmp_path
    ):
        db_path = str(tmp_path / "ok.sqlite")
        write_kv_db(db_path, healthy_payload())
        code, message = run_healthcheck(db_path)
        assert code == 0, message

    def test_degraded_passes_healthcheck(self, tmp_path):
        db_path = str(tmp_path / "degraded.sqlite")
        write_kv_db(
            db_path,
            healthy_payload(overall="degraded", spot_ws="fail"),
        )
        code, message = run_healthcheck(db_path)
        assert code == 0, message

    def test_container_healthcheck_fails_when_heartbeat_stale(self, tmp_path):
        db_path = str(tmp_path / "stale.sqlite")
        write_kv_db(
            db_path,
            healthy_payload(last_updated_at=int(time.time()) - 600),
        )
        code, message = run_healthcheck(db_path)
        assert code == 1
        assert "heartbeat stale" in message

    def test_container_healthcheck_fails_when_dispatcher_unhealthy(self, tmp_path):
        db_path = str(tmp_path / "dispatcher.sqlite")
        write_kv_db(
            db_path,
            healthy_payload(
                overall="unhealthy",
                dispatcher="fail",
                critical_issues=["dispatcher"],
            ),
        )
        code, message = run_healthcheck(db_path)
        assert code == 1
        assert "critical application failure" in message

    def test_container_healthcheck_fails_when_spot_ws_stale(self, tmp_path):
        db_path = str(tmp_path / "spot.sqlite")
        write_kv_db(
            db_path,
            healthy_payload(
                overall="unhealthy",
                spot_ws="fail",
                critical_issues=["spot_ws"],
            ),
        )
        code, message = run_healthcheck(db_path)
        assert code == 1

    def test_container_healthcheck_fails_when_linear_ws_stale(self, tmp_path):
        db_path = str(tmp_path / "linear.sqlite")
        write_kv_db(
            db_path,
            healthy_payload(
                overall="unhealthy",
                linear_ws="fail",
                critical_issues=["linear_ws"],
            ),
        )
        code, message = run_healthcheck(db_path)
        assert code == 1

    def test_container_healthcheck_fails_without_snapshot(self, tmp_path):
        db_path = str(tmp_path / "empty.sqlite")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                "updated_at INTEGER NOT NULL)"
            )
            conn.commit()
        finally:
            conn.close()
        code, message = run_healthcheck(db_path)
        assert code == 1
        assert "no health snapshot" in message

    def test_container_healthcheck_fails_when_db_unreadable(self, tmp_path):
        db_path = str(tmp_path / "missing.sqlite")
        code, message = run_healthcheck(db_path)
        assert code == 1
        assert "unreadable" in message


class TestHeartbeatThreshold:
    """Phase J4 - HEALTH_HEARTBEAT_STALE_SECONDS must drive the healthcheck."""

    def test_healthcheck_uses_default_heartbeat_threshold(self, tmp_path):
        db_path = str(tmp_path / "default.sqlite")
        # Snapshot 200s old: over the default 120s threshold -> unhealthy.
        write_kv_db(
            db_path,
            healthy_payload(last_updated_at=int(time.time()) - 200),
        )
        code, message = run_healthcheck(db_path)
        assert code == 1
        assert "heartbeat stale" in message

    def test_healthcheck_respects_custom_heartbeat_threshold(self, tmp_path):
        db_path = str(tmp_path / "custom.sqlite")
        write_kv_db(
            db_path,
            healthy_payload(last_updated_at=int(time.time()) - 200),
        )
        # Case A: threshold 300 -> the 200s-old heartbeat is healthy.
        code, message = run_healthcheck(
            db_path, {"HEALTH_HEARTBEAT_STALE_SECONDS": "300"}
        )
        assert code == 0, message
        # Case B: threshold 120 -> the same snapshot is unhealthy.
        code, message = run_healthcheck(
            db_path, {"HEALTH_HEARTBEAT_STALE_SECONDS": "120"}
        )
        assert code == 1
        assert "heartbeat stale" in message

    def test_healthcheck_rejects_zero_threshold(self, tmp_path):
        db_path = str(tmp_path / "zero.sqlite")
        write_kv_db(db_path, healthy_payload())
        code, message = run_healthcheck(
            db_path, {"HEALTH_HEARTBEAT_STALE_SECONDS": "0"}
        )
        assert code == 1
        assert "must be finite and > 0" in message

    def test_healthcheck_rejects_negative_threshold(self, tmp_path):
        db_path = str(tmp_path / "neg.sqlite")
        write_kv_db(db_path, healthy_payload())
        code, message = run_healthcheck(
            db_path, {"HEALTH_HEARTBEAT_STALE_SECONDS": "-1"}
        )
        assert code == 1
        assert "must be finite and > 0" in message

    def test_healthcheck_rejects_nan_threshold(self, tmp_path):
        db_path = str(tmp_path / "nan.sqlite")
        write_kv_db(db_path, healthy_payload())
        code, message = run_healthcheck(
            db_path, {"HEALTH_HEARTBEAT_STALE_SECONDS": "nan"}
        )
        assert code == 1
        assert "must be finite and > 0" in message