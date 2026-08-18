"""Phase 1 - database and migrations tests."""

from __future__ import annotations

import pytest

from app.persistence.database import Database
from app.persistence.migrations import apply_migrations

EXPECTED_TABLES = {
    "schema_migrations",
    "instruments",
    "price_samples",
    "alert_state",
    "listing_events",
    "outgoing_notifications",
    "kv",
}


async def _table_names(db: Database) -> set[str]:
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")
    return {row["name"] for row in rows}


class TestDatabaseInitialization:
    async def test_initializes_all_schema_tables(self, db):
        names = await _table_names(db)
        assert EXPECTED_TABLES <= names

    async def test_migrations_are_idempotent(self, db):
        await apply_migrations(db)
        await apply_migrations(db)
        names = await _table_names(db)
        assert EXPECTED_TABLES <= names
        row = await db.fetchone(
            "SELECT COUNT(*) AS c FROM schema_migrations WHERE version = 1"
        )
        assert row["c"] == 1  # type: ignore[index]

    async def test_wal_mode_enabled(self, db):
        row = await db.fetchone("PRAGMA journal_mode")
        assert str(row[0]).lower() in ("wal", "memory")  # type: ignore[index]

    async def test_foreign_keys_enabled(self, db):
        row = await db.fetchone("PRAGMA foreign_keys")
        assert row[0] == 1  # type: ignore[index]

    async def test_instrument_table_schema(self, db):
        row = await db.fetchone(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='instruments'"
        )
        sql = str(row[0]).lower()  # type: ignore[index]
        for column in ("category", "symbol", "base_coin", "status"):
            assert column in sql
        assert "primary key (category, symbol)" in sql

    async def test_insert_and_read(self, db):
        await db.execute(
            "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
            ("answer", "42", 1),
        )
        await db.commit()
        row = await db.fetchone("SELECT value FROM kv WHERE key = ?", ("answer",))
        assert row["value"] == "42"  # type: ignore[index]

    async def test_transaction_rolls_back_on_error(self, db):
        with pytest.raises(RuntimeError):
            async with db.transaction():
                await db.execute(
                    "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                    ("should_not_persist", "x", 1),
                )
                raise RuntimeError("boom")
        row = await db.fetchone("SELECT * FROM kv WHERE key = 'should_not_persist'")
        assert row is None

    async def test_connect_to_memory_database(self):
        db = Database(":memory:")
        await db.connect()
        await apply_migrations(db)
        names = await _table_names(db)
        assert EXPECTED_TABLES <= names
        await db.close()

    async def test_operations_require_connection(self, tmp_path):
        db = Database(str(tmp_path / "never_connected.sqlite"))
        with pytest.raises(RuntimeError):
            await db.execute("SELECT 1")


class TestRepository:
    async def test_kv_roundtrip(self, db):
        from app.persistence.repository import Repository

        repo = Repository(db)
        await repo.kv_set("hello", "world")
        assert await repo.kv_get("hello") == "world"

    async def test_kv_json_roundtrip(self, db):
        from app.persistence.repository import Repository

        repo = Repository(db)
        await repo.kv_set_json("coins", ["BTC", "ETH"])
        assert await repo.kv_get_json("coins") == ["BTC", "ETH"]

    async def test_notification_records(self, db):
        from app.persistence.repository import Repository

        repo = Repository(db)
        nid = await repo.insert_outgoing_notification("tag-1", "hello")
        await repo.mark_notification_sent(nid)
        records = await repo.list_notifications(status="sent")
        assert len(records) == 1
        assert records[0]["message_tag"] == "tag-1"
        assert records[0]["status"] == "sent"
