"""Docker container HEALTHCHECK (Phase H3).

Container health must represent critical application failure, not merely
whether SQLite is writable. The application persists a compact health
snapshot (``kv`` row ``health:snapshot``) every ~30 seconds; this script
reads it and exits:

    0  - database readable, heartbeat recent, overall != unhealthy
    1  - anything else (no snapshot yet, stale heartbeat, critical failure)

No network calls are made from the healthcheck.

Run directly:    python scripts/container_healthcheck.py
Exit code is the health result (0 = healthy).
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
import time

DEFAULT_DB = "/data/bybit_monitor.sqlite"
HEALTH_KEY = "health:snapshot"


def heartbeat_stale_seconds() -> float:
    """Heartbeat staleness threshold from the application configuration.

    Defaults to 120s (tuned against health_write_interval_seconds=30:
    stale after 4+ consecutive missed writes). An invalid value raises
    instead of silently falling back: the healthcheck must fail closed.
    """
    raw = os.environ.get(
        "HEALTH_HEARTBEAT_STALE_SECONDS",
        "120",
    )
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            "HEALTH_HEARTBEAT_STALE_SECONDS must be finite and > 0"
        )
    return value


def read_snapshot(db_path: str) -> dict:
    """Return the persisted health snapshot, or raise on any failure."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    try:
        conn.execute("SELECT 1")
        row = conn.execute(
            "SELECT value FROM kv WHERE key = ?", (HEALTH_KEY,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise LookupError("no health snapshot persisted yet")
    return json.loads(row[0])


def check_health(db_path: str, threshold: float) -> tuple[bool, str]:
    try:
        snapshot = read_snapshot(db_path)
    except LookupError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"database or snapshot unreadable: {type(exc).__name__}"
    now = time.time()
    heartbeat_age = now - float(snapshot.get("last_updated_at", 0))
    if heartbeat_age > threshold:
        return False, f"health heartbeat stale: {heartbeat_age:.0f}s"
    if snapshot.get("overall") == "unhealthy":
        issues = ",".join(snapshot.get("critical_issues", [])) or "unknown"
        return False, f"critical application failure: {issues}"
    return True, f"healthy ({snapshot.get('overall', 'healthy')})"


def main() -> int:
    db_path = os.environ.get("DATABASE_PATH", DEFAULT_DB)
    try:
        threshold = heartbeat_stale_seconds()
    except ValueError as exc:
        print(f"healthcheck: {exc}")
        return 1
    ok, message = check_health(db_path, threshold)
    print(f"healthcheck: {message}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())