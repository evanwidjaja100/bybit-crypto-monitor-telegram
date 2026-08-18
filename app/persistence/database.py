"""Async SQLite database wrapper.

Applies the recommended SQLite production settings:

- WAL journal mode
- foreign keys ON
- busy timeout
- transactions
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any, AsyncIterator, Iterable, Optional

import aiosqlite


class Database:
    """Thin async wrapper around a single aiosqlite connection."""

    def __init__(self, path: str) -> None:
        self.path: str = path
        self._conn: Optional[aiosqlite.Connection] = None
        # Serializes access to the single underlying connection so that
        # concurrent tasks (market loop + dispatcher worker) cannot
        # interleave BEGIN/COMMIT or split an execute/commit pair.
        self._lock: asyncio.Lock = asyncio.Lock()
        self._in_transaction: bool = False

    @property
    def connected(self) -> bool:
        return self._conn is not None

    async def connect(self) -> None:
        if self._conn is not None:
            return
        if self.path != ":memory:":
            parent = Path(self.path).parent
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path, timeout=10.0)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA synchronous=NORMAL")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        self._require_connection()
        async with self._guarded():
            return await self._conn.execute(sql, params)  # type: ignore[union-attr]

    async def executemany(
        self, sql: str, seq_of_params: Iterable[Iterable[Any]]
    ) -> aiosqlite.Cursor:
        self._require_connection()
        async with self._guarded():
            return await self._conn.executemany(sql, seq_of_params)  # type: ignore[union-attr]

    async def fetchone(
        self, sql: str, params: Iterable[Any] = ()
    ) -> Optional[aiosqlite.Row]:
        self._require_connection()
        async with self._guarded():
            cursor = await self._conn.execute(sql, params)  # type: ignore[union-attr]
            try:
                return await cursor.fetchone()
            finally:
                await cursor.close()

    async def fetchall(
        self, sql: str, params: Iterable[Any] = ()
    ) -> list[aiosqlite.Row]:
        self._require_connection()
        async with self._guarded():
            cursor = await self._conn.execute(sql, params)  # type: ignore[union-attr]
            try:
                return list(await cursor.fetchall())
            finally:
                await cursor.close()

    async def commit(self) -> None:
        if self._conn is not None:
            async with self._guarded():
                await self._conn.commit()

    async def rollback(self) -> None:
        if self._conn is not None:
            async with self._guarded():
                await self._conn.rollback()

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Run a block inside an explicit transaction with rollback on error.

        The connection lock is held for the whole block: no other task can
        BEGIN/COMMIT or execute statements in between. Calls made inside
        the block run on the same connection without re-acquiring the lock.
        """
        self._require_connection()
        await self._lock.acquire()
        self._in_transaction = True
        try:
            await self._conn.execute("BEGIN")  # type: ignore[union-attr]
            try:
                yield
                await self._conn.commit()  # type: ignore[union-attr]
            except BaseException:
                await self._conn.rollback()  # type: ignore[union-attr]
                raise
        finally:
            self._in_transaction = False
            self._lock.release()

    @contextlib.asynccontextmanager
    async def _guarded(self) -> AsyncIterator[None]:
        """Yield with the connection lock unless already inside a transaction."""
        if self._in_transaction:
            yield
            return
        async with self._lock:
            yield

    def _require_connection(self) -> None:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
