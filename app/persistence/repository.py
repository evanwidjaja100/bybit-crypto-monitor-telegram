"""Data access layer built on top of the aiosqlite ``Database``.

Concrete repositories are expanded phase-by-phase:

- Instrument repository  (Phase 3)
- Spot price-history store (Phase 5)
- Alert-state store (Phase 7)
- Notification records (Phase 8)
- Listing-event store (Phase 9)
"""

from __future__ import annotations

import contextlib
import json
import time
from typing import Any, AsyncIterator, Optional

from app.bybit.models import Instrument
from app.persistence.database import Database


class Repository:
    """Read/write access to the notification outbox and misc key-values."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @contextlib.asynccontextmanager
    async def _committed(self, commit: bool) -> AsyncIterator[None]:
        """Wrap a write in a transaction when it must commit on its own."""
        if commit:
            async with self.db.transaction():
                yield
        else:
            yield

    # ------------------------------------------------------------------
    # Generic key/value state
    # ------------------------------------------------------------------
    async def kv_get(self, key: str) -> Optional[str]:
        row = await self.db.fetchone("SELECT value FROM kv WHERE key = ?", (key,))
        return row["value"] if row else None  # type: ignore[index]

    async def kv_get_json(self, key: str, default: Any = None) -> Any:
        raw = await self.kv_get(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return default

    async def kv_set(self, key: str, value: str) -> None:
        now = int(time.time())
        async with self.db.transaction():
            await self.db.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (key, value, now),
            )

    async def kv_set_json(self, key: str, value: Any) -> None:
        await self.kv_set(key, json.dumps(value, sort_keys=True))

    async def kv_delete(self, key: str) -> None:
        async with self.db.transaction():
            await self.db.execute("DELETE FROM kv WHERE key = ?", (key,))

    # ------------------------------------------------------------------
    # Notification records
    # ------------------------------------------------------------------
    async def insert_outgoing_notification(
        self,
        message_tag: str,
        message: str,
        status: str = "pending",
        created_at: Optional[int] = None,
        dedupe_key: Optional[str] = None,
        origin_type: Optional[str] = None,
        origin_key: Optional[str] = None,
        commit: bool = True,
    ) -> int:
        created_at = created_at if created_at is not None else int(time.time())
        async with self._committed(commit):
            cursor = await self.db.execute(
                "INSERT INTO outgoing_notifications "
                "(message_tag, message, created_at, status, dedupe_key, "
                "origin_type, origin_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (message_tag, message, created_at, status, dedupe_key, origin_type, origin_key),
            )
        return cursor.lastrowid  # type: ignore[return-value]

    async def mark_notification_sent(
        self, notification_id: int, commit: bool = True
    ) -> None:
        async with self._committed(commit):
            await self.db.execute(
                "UPDATE outgoing_notifications SET status = 'sent', sent_at = ? "
                "WHERE id = ?",
                (int(time.time()), notification_id),
            )

    async def mark_notification_retry(
        self,
        notification_id: int,
        error: str,
        next_attempt_at: int,
        now: Optional[int] = None,
    ) -> None:
        """Record a transient failure and schedule the next attempt."""
        now = now if now is not None else int(time.time())
        async with self.db.transaction():
            await self.db.execute(
                "UPDATE outgoing_notifications SET "
                "status = 'retry', error = ?, attempt_count = attempt_count + 1, "
                "last_attempt_at = ?, next_attempt_at = ? WHERE id = ?",
                (error[:500], now, next_attempt_at, notification_id),
            )

    async def mark_notification_dead(
        self, notification_id: int, error: str
    ) -> None:
        async with self.db.transaction():
            await self.db.execute(
                "UPDATE outgoing_notifications SET status = 'dead', error = ? "
                "WHERE id = ?",
                (error[:500], notification_id),
            )
    async def due_notifications(self, limit: int = 100) -> list[dict[str, Any]]:
        """pending + retry rows that may be attempted right now."""
        rows = await self.db.fetchall(
            "SELECT * FROM outgoing_notifications WHERE status IN ('pending', 'retry') "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "ORDER BY id ASC LIMIT ?",
            (int(time.time()), limit),
        )
        return [dict(row) for row in rows]

    async def expire_notifications(
        self,
        alert_max_age: int,
        listing_max_age: int,
        now: Optional[int] = None,
    ) -> int:
        """Mark stale pending/retry rows dead (returns affected row count)."""
        now = now if now is not None else int(time.time())
        async with self.db.transaction():
            cursor = await self.db.execute(
                "UPDATE outgoing_notifications SET status = 'dead', error = 'expired' "
                "WHERE status IN ('pending', 'retry') "
                "AND ((origin_type = 'listing' AND created_at < ?) "
                "     OR (COALESCE(origin_type, '') != 'listing' AND created_at < ?))",
                (now - listing_max_age, now - alert_max_age),
            )
        return cursor.rowcount  # type: ignore[return-value]

    async def count_unsent(self) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS c FROM outgoing_notifications "
            "WHERE status IN ('pending', 'retry')"
        )
        return int(row["c"])  # type: ignore[index]

    async def find_notification_by_dedupe(
        self, dedupe_key: str
    ) -> Optional[dict[str, Any]]:
        row = await self.db.fetchone(
            "SELECT * FROM outgoing_notifications WHERE dedupe_key = ?",
            (dedupe_key,),
        )
        return dict(row) if row else None

    async def mark_listing_sent(self, event_key: str, commit: bool = True) -> None:
        async with self._committed(commit):
            await self.db.execute(
                "UPDATE listing_events SET telegram_sent = 1 WHERE event_key = ?",
                (event_key,),
            )

    async def list_notifications(
        self, limit: int = 100, status: Optional[str] = None
    ) -> list[dict[str, Any]]:
        if status:
            rows = await self.db.fetchall(
                "SELECT * FROM outgoing_notifications WHERE status = ? "
                "ORDER BY id DESC LIMIT ?",
                (status, limit),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM outgoing_notifications ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        return [dict(row) for row in rows]


def row_to_instrument(row) -> "Instrument":
    """Convert an instruments-table row/dict back into an Instrument."""
    return Instrument(
        category=row["category"],
        symbol=row["symbol"],
        base_coin=row["base_coin"],
        quote_coin=row["quote_coin"],
        settle_coin=row["settle_coin"],
        contract_type=row["contract_type"],
        status=row["status"],
        launch_time=row["launch_time"],
        delivery_time=row["delivery_time"],
        is_pre_listing=bool(row["is_pre_listing"]),
    )


class InstrumentRepository:
    """Persistent authoritative store for discovered instruments."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def load_all(self) -> dict[tuple[str, str], "Instrument"]:
        rows = await self.db.fetchall("SELECT * FROM instruments")
        return {
            (row["category"], row["symbol"]): row_to_instrument(row)
            for row in rows
        }

    async def get(
        self, category: str, symbol: str
    ) -> Optional["Instrument"]:
        row = await self.db.fetchone(
            "SELECT * FROM instruments WHERE category = ? AND symbol = ?",
            (category, symbol),
        )
        return row_to_instrument(row) if row else None

    async def upsert_many(
        self, instruments: list["Instrument"], now: int
    ) -> None:
        """Insert new instruments and update existing ones.

        ``first_seen_at`` is written on insert only and preserved on
        conflict, so repeated runs never fake new-listing events.
        """
        async with self.db.transaction():
            for inst in instruments:
                await self.db.execute(
                    """
                    INSERT INTO instruments (
                        category, symbol, base_coin, quote_coin, settle_coin,
                        contract_type, status, launch_time, delivery_time,
                        is_pre_listing, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(category, symbol) DO UPDATE SET
                        base_coin = excluded.base_coin,
                        quote_coin = excluded.quote_coin,
                        settle_coin = excluded.settle_coin,
                        contract_type = excluded.contract_type,
                        status = excluded.status,
                        launch_time = excluded.launch_time,
                        delivery_time = excluded.delivery_time,
                        is_pre_listing = excluded.is_pre_listing,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        inst.category,
                        inst.symbol,
                        inst.base_coin,
                        inst.quote_coin,
                        inst.settle_coin,
                        inst.contract_type,
                        inst.status,
                        inst.launch_time,
                        inst.delivery_time,
                        int(inst.is_pre_listing),
                        now,
                        now,
                    ),
                )

    async def mark_removed(self, keys: list[tuple[str, str]], now: int) -> None:
        async with self.db.transaction():
            for category, symbol in keys:
                await self.db.execute(
                    "UPDATE instruments SET status = 'Removed', last_seen_at = ? "
                    "WHERE category = ? AND symbol = ?",
                    (now, category, symbol),
                )

    async def count_active(self) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS c FROM instruments WHERE status != 'Removed'"
        )
        return int(row["c"])  # type: ignore[index]


class AlertStateRepository:
    """Persistent single-row store for the alert state machine."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def load(self) -> dict[str, Any]:
        row = await self.db.fetchone("SELECT * FROM alert_state WHERE id = 1")
        if row is None:
            return {
                "state": "EMPTY",
                "fingerprint": "[]",
                "updated_at": 0,
                "last_transition_at": None,
                "pending_since": None,
                "pending_from": None,
                "last_hourly_bucket": None,
                "last_composition_at": None,
            }
        return dict(row)

    async def save(
        self,
        state: str,
        fingerprint: tuple[str, ...],
        updated_at: int,
        last_transition_at: Optional[int],
        pending_since: Optional[int],
        pending_from: Optional[str],
        last_hourly_bucket: Optional[str],
        last_composition_at: Optional[int],
    ) -> None:
        async with self.db.transaction():
            await self.save_no_commit(
                state,
                fingerprint,
                updated_at,
                last_transition_at,
                pending_since,
                pending_from,
                last_hourly_bucket,
                last_composition_at,
            )
    async def save_no_commit(
        self,
        state: str,
        fingerprint: tuple[str, ...],
        updated_at: int,
        last_transition_at: Optional[int],
        pending_since: Optional[int],
        pending_from: Optional[str],
        last_hourly_bucket: Optional[str],
        last_composition_at: Optional[int],
    ) -> None:
        """Same upsert as :meth:`save` but without committing, so callers
        can combine it with other writes in one transaction."""
        await self.db.execute(
            """
            INSERT INTO alert_state (
                id, state, fingerprint, updated_at, last_transition_at,
                pending_since, pending_from, last_hourly_bucket,
                last_composition_at
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                state = excluded.state,
                fingerprint = excluded.fingerprint,
                updated_at = excluded.updated_at,
                last_transition_at = excluded.last_transition_at,
                pending_since = excluded.pending_since,
                pending_from = excluded.pending_from,
                last_hourly_bucket = excluded.last_hourly_bucket,
                last_composition_at = excluded.last_composition_at
            """,
            (
                state,
                json.dumps(list(fingerprint), sort_keys=True),
                updated_at,
                last_transition_at,
                pending_since,
                pending_from,
                last_hourly_bucket,
                last_composition_at,
            ),
        )


class ListingEventRepository:
    """Idempotent persistent store for listing events (Phase 9)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def record(
        self,
        event_key: str,
        category: Optional[str],
        symbol: str,
        event_type: str,
        now: int,
    ) -> Optional[dict[str, Any]]:
        """Insert a listing event; returns it only when newly created."""
        async with self.db.transaction():
            cursor = await self.db.execute(
                "INSERT OR IGNORE INTO listing_events "
                "(event_key, category, symbol, event_type, first_seen_at, telegram_sent) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (event_key, category, symbol, event_type, now),
            )
        if cursor.rowcount == 0:
            return None
        return {
            "event_key": event_key,
            "category": category,
            "symbol": symbol,
            "event_type": event_type,
            "first_seen_at": now,
            "telegram_sent": 0,
        }

    async def mark_sent(self, event_key: str) -> None:
        async with self.db.transaction():
            await self.db.execute(
                "UPDATE listing_events SET telegram_sent = 1 WHERE event_key = ?",
                (event_key,),
            )

    async def unsent(self) -> list[dict[str, Any]]:
        """Events that were never notified (delisted events are silent)."""
        rows = await self.db.fetchall(
            "SELECT * FROM listing_events "
            "WHERE telegram_sent = 0 AND event_type != 'delisted'"
        )
        return [dict(row) for row in rows]

    async def list_all(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT * FROM listing_events ORDER BY first_seen_at DESC LIMIT ?",
            (limit,),
        )
        return [dict(row) for row in rows]


class PriceSampleRepository:
    """Persistent spot price-history store for 1h momentum anchors."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def insert_sample(
        self, category: str, symbol: str, timestamp: int, price: float
    ) -> bool:
        """Insert a sample; returns True only when a new row was written."""
        async with self.db.transaction():
            cursor = await self.db.execute(
                "INSERT OR IGNORE INTO price_samples "
                "(category, symbol, timestamp, price) VALUES (?, ?, ?, ?)",
                (category, symbol, timestamp, price),
            )
        return cursor.rowcount > 0

    async def latest_timestamp(
        self, category: str, symbol: str
    ) -> Optional[int]:
        row = await self.db.fetchone(
            "SELECT MAX(timestamp) AS t FROM price_samples "
            "WHERE category = ? AND symbol = ?",
            (category, symbol),
        )
        value = row["t"]  # type: ignore[index]
        return int(value) if value is not None else None

    async def find_reference(
        self,
        category: str,
        symbol: str,
        target_ts: int,
        tolerance: float,
    ) -> Optional[float]:
        """Return the price closest to ``target_ts`` within tolerance."""
        lo = int(target_ts - tolerance)
        hi = int(target_ts + tolerance)
        row = await self.db.fetchone(
            """
            SELECT price FROM price_samples
            WHERE category = ? AND symbol = ? AND timestamp BETWEEN ? AND ?
            ORDER BY ABS(timestamp - ?) ASC, timestamp DESC LIMIT 1
            """,
            (category, symbol, lo, hi, target_ts),
        )
        return float(row["price"]) if row else None  # type: ignore[index]

    async def cleanup_older_than(self, cutoff_ts: int) -> int:
        """Delete samples older than ``cutoff_ts``; returns rows deleted."""
        async with self.db.transaction():
            cursor = await self.db.execute(
                "DELETE FROM price_samples WHERE timestamp < ?", (cutoff_ts,)
            )
        return cursor.rowcount

    async def count(self) -> int:
        row = await self.db.fetchone("SELECT COUNT(*) AS c FROM price_samples")
        return int(row["c"])  # type: ignore[index]


__all__ = [
    "Repository",
    "InstrumentRepository",
    "AlertStateRepository",
    "ListingEventRepository",
    "PriceSampleRepository",
    "row_to_instrument",
]
