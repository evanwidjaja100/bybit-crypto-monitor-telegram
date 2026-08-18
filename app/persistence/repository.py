"""Data access layer built on top of the aiosqlite ``Database``.

Concrete repositories are expanded phase-by-phase:

- Instrument repository  (Phase 3)
- Spot price-history store (Phase 5)
- Alert-state store (Phase 7)
- Notification records (Phase 8)
- Listing-event store (Phase 9)
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional

from app.bybit.models import Instrument
from app.persistence.database import Database


class Repository:
    """Shared data-access helpers over the SQLite database."""

    def __init__(self, db: Database) -> None:
        self.db = db

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
        await self.db.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, now),
        )
        await self.db.commit()

    async def kv_set_json(self, key: str, value: Any) -> None:
        await self.kv_set(key, json.dumps(value, sort_keys=True))

    async def kv_delete(self, key: str) -> None:
        await self.db.execute("DELETE FROM kv WHERE key = ?", (key,))
        await self.db.commit()

    # ------------------------------------------------------------------
    # Notification records
    # ------------------------------------------------------------------
    async def insert_outgoing_notification(
        self,
        message_tag: str,
        message: str,
        status: str = "pending",
        created_at: Optional[int] = None,
    ) -> int:
        created_at = created_at if created_at is not None else int(time.time())
        cursor = await self.db.execute(
            "INSERT INTO outgoing_notifications "
            "(message_tag, message, created_at, status) VALUES (?, ?, ?, ?)",
            (message_tag, message, created_at, status),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def mark_notification_sent(self, notification_id: int) -> None:
        await self.db.execute(
            "UPDATE outgoing_notifications SET status = 'sent', sent_at = ? "
            "WHERE id = ?",
            (int(time.time()), notification_id),
        )
        await self.db.commit()

    async def mark_notification_failed(self, notification_id: int, error: str) -> None:
        await self.db.execute(
            "UPDATE outgoing_notifications SET status = 'failed', error = ? "
            "WHERE id = ?",
            (error[:500], notification_id),
        )
        await self.db.commit()

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


class PriceSampleRepository:
    """Persistent spot price-history store for 1h momentum anchors."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def insert_sample(
        self, category: str, symbol: str, timestamp: int, price: float
    ) -> bool:
        """Insert a sample; returns True only when a new row was written."""
        cursor = await self.db.execute(
            "INSERT OR IGNORE INTO price_samples "
            "(category, symbol, timestamp, price) VALUES (?, ?, ?, ?)",
            (category, symbol, timestamp, price),
        )
        await self.db.commit()
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
        cursor = await self.db.execute(
            "DELETE FROM price_samples WHERE timestamp < ?", (cutoff_ts,)
        )
        await self.db.commit()
        return cursor.rowcount

    async def count(self) -> int:
        row = await self.db.fetchone("SELECT COUNT(*) AS c FROM price_samples")
        return int(row["c"])  # type: ignore[index]


__all__ = [
    "Repository",
    "InstrumentRepository",
    "PriceSampleRepository",
    "row_to_instrument",
]
