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


__all__ = ["Repository"]
