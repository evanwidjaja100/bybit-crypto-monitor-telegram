"""Schema migrations.

Migrations are versioned. ``apply_migrations`` applies only the versions
that have not yet been recorded in ``schema_migrations``, making the
process idempotent across restarts.
"""

from __future__ import annotations

import time
from typing import Sequence

from app.persistence.database import Database

_SCHEMA_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at INTEGER NOT NULL
)
"""

# Each migration is ``(version, [sql statements])``.
MIGRATIONS: Sequence[tuple[int, list[str]]] = [
    (
        1,
        [
            # --- Instrument registry (Phase 3) ---
            """
            CREATE TABLE instruments (
                category TEXT NOT NULL,
                symbol TEXT NOT NULL,
                base_coin TEXT NOT NULL,
                quote_coin TEXT,
                settle_coin TEXT,
                contract_type TEXT,
                status TEXT NOT NULL,
                launch_time INTEGER,
                delivery_time INTEGER,
                is_pre_listing INTEGER NOT NULL DEFAULT 0,
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                PRIMARY KEY (category, symbol)
            )
            """,
            # --- Spot price history (Phase 5) ---
            """
            CREATE TABLE price_samples (
                category TEXT NOT NULL,
                symbol TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                price REAL NOT NULL,
                PRIMARY KEY (category, symbol, timestamp)
            )
            """,
            "CREATE INDEX idx_price_samples_anchor "
            "ON price_samples (category, symbol, timestamp)",
            # --- Alert state machine (Phase 7) ---
            """
            CREATE TABLE alert_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                state TEXT NOT NULL DEFAULT 'EMPTY',
                fingerprint TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL,
                last_transition_at INTEGER,
                pending_since INTEGER,
                last_hourly_bucket TEXT,
                last_composition_at INTEGER
            )
            """,
            # --- Listing events (Phase 9) ---
            """
            CREATE TABLE listing_events (
                event_key TEXT PRIMARY KEY,
                category TEXT,
                symbol TEXT,
                event_type TEXT NOT NULL,
                first_seen_at INTEGER NOT NULL,
                telegram_sent INTEGER NOT NULL DEFAULT 0
            )
            """,
            # --- Outgoing notification records (Phase 8) ---
            """
            CREATE TABLE outgoing_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_tag TEXT,
                message TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                status TEXT NOT NULL,
                sent_at INTEGER,
                error TEXT
            )
            """,
            "CREATE INDEX idx_outgoing_created ON outgoing_notifications (created_at)",
            # --- Generic key/value state ---
            """
            CREATE TABLE kv (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
            """,
        ],
    ),
]


async def apply_migrations(
    db: Database, migrations: Sequence[tuple[int, list[str]]] | None = None
) -> None:
    migrations = migrations if migrations is not None else MIGRATIONS
    await db.execute(_SCHEMA_MIGRATIONS_TABLE)
    await db.commit()

    row = await db.fetchone("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations")
    current = int(row["v"])  # type: ignore[index]

    for version, statements in migrations:
        if version <= current:
            continue
        async with db.transaction():
            for statement in statements:
                await db.execute(statement)
            await db.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, int(time.time())),
            )


__all__ = ["MIGRATIONS", "apply_migrations"]
