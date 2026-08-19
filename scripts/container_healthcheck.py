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
import os
import sqlite3
import sys
import time

DEFAULT_DB = "/data/bybit_monitor.sqlite"
HEALTH_KEY = "health:snapshot"
# Tuned against health_write_interval_seconds=30: the heartbeat is stale
# when 4+ consecutive writes were missed. Align with
# HEALTH_HEARTBEAT_STALE_SECONDS if the app configuration is tuned.
HEARTBEAT_STALE_SECONDS = 120.0


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


def check_health(db_path: str) -> tuple[bool, str]:
    try:
        snapshot = read_snapshot(db_path)
    except LookupError as exc:
        return False, str(exc)
    except Exception as exc:
        return False, f"database or snapshot unreadable: {type(exc).__name__}"
    now = time.time()
    heartbeat_age = now - float(snapshot.get("last_updated_at", 0))
    if heartbeat_age > HEARTBEAT_STALE_SECONDS:
        return False, f"health heartbeat stale: {heartbeat_age:.0f}s"
    if snapshot.get("overall") == "unhealthy":
        issues = ",".join(snapshot.get("critical_issues", [])) or "unknown"
        return False, f"critical application failure: {issues}"
    return True, f"healthy ({snapshot.get('overall', 'healthy')})"


def main() -> int:
    db_path = os.environ.get("DATABASE_PATH", DEFAULT_DB)
    ok, message = check_health(db_path)
    print(f"healthcheck: {message}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())