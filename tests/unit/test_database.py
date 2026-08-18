"""Phase 1 - database and migrations tests."""

from __future__ import annotations

import asyncio
import time

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


class TestTaskAwareTransactionOwnership:
    """Phase F1 - the connection lock must be task-aware (P0-1).

    While task A owns a transaction, no other task may issue SQL on the
    same connection; the transaction owner itself may execute reentrantly.
    """

    async def test_other_task_cannot_execute_while_transaction_is_owned(
        self, db
    ):
        tx_open = asyncio.Event()
        release_tx = asyncio.Event()

        async def task_a():
            async with db.transaction():
                await db.execute(
                    "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                    ("owner", "1", 1),
                )
                tx_open.set()
                await release_tx.wait()

        owner_task = asyncio.create_task(task_a())
        await tx_open.wait()

        b_done = False

        async def task_b():
            nonlocal b_done
            await db.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                ("intruder", "2", 2),
            )
            b_done = True

        intruder_task = asyncio.create_task(task_b())
        await asyncio.sleep(0.2)
        assert b_done is False, "task B executed SQL while task A owned the transaction"

        release_tx.set()
        await asyncio.wait_for(owner_task, timeout=5.0)
        await asyncio.wait_for(intruder_task, timeout=5.0)
        assert b_done is True

        row = await db.fetchone(
            "SELECT COUNT(*) AS c FROM kv WHERE key IN ('owner', 'intruder')"
        )
        assert row["c"] == 2  # type: ignore[index]

    async def test_transaction_owner_can_execute_reentrantly(self, db):
        async with db.transaction():
            await db.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                ("reentrant", "1", 1),
            )
            row = await db.fetchone(
                "SELECT value FROM kv WHERE key = ?", ("reentrant",)
            )
            assert row["value"] == "1"  # type: ignore[index]
        row = await db.fetchone(
            "SELECT value FROM kv WHERE key = ?", ("reentrant",)
        )
        assert row["value"] == "1"  # type: ignore[index]

    async def test_nested_transaction_raises_runtime_error(self, db):
        with pytest.raises(RuntimeError, match="nested"):
            async with db.transaction():
                async with db.transaction():
                    pass

    async def test_cancelled_transaction_releases_database_lock(self, db):
        tx_open = asyncio.Event()

        async def task_a():
            try:
                async with db.transaction():
                    await db.execute(
                        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                        ("cancelled", "1", 1),
                    )
                    tx_open.set()
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise

        task = asyncio.create_task(task_a())
        await tx_open.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5.0)

        # Lock must be released: another task can use the DB again.
        await db.execute("SELECT 1")
        row = await db.fetchone("SELECT * FROM kv WHERE key = 'cancelled'")
        assert row is None  # rolled back

    async def test_exception_in_transaction_releases_lock(self, db):
        async def task_a():
            with pytest.raises(RuntimeError, match="boom"):
                async with db.transaction():
                    await db.execute(
                        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                        ("rolled", "1", 1),
                    )
                    raise RuntimeError("boom")

        await asyncio.wait_for(task_a(), timeout=5.0)
        # Lock released and no partial data left behind.
        row = await db.fetchone("SELECT * FROM kv WHERE key = 'rolled'")
        assert row is None
        await db.execute("SELECT 1")

    async def test_commit_writes_are_visible_after_release(self, db):
        async with db.transaction():
            await db.execute(
                "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?)",
                ("committed", "3", 3),
            )
        row = await db.fetchone(
            "SELECT value FROM kv WHERE key = ?", ("committed",)
        )
        assert row["value"] == "3"  # type: ignore[index]
