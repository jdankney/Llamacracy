"""SQLite access. One connection, WAL mode, serialised through a single
asyncio lock -- writes are tiny (job/message rows) and the queue is strictly
serial anyway, so this is plenty and keeps the code simple.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import get_settings

_SCHEMA = (Path(__file__).parent / "schema.sql").read_text()

# Additive, idempotent migrations for DBs created before a column existed.
# Fresh DBs get the column straight from schema.sql; these just catch up
# existing ones. "duplicate column name" means it's already applied.
_MIGRATIONS = [
    "ALTER TABLE users ADD COLUMN uncapped INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN search_json TEXT",
    "ALTER TABLE messages ADD COLUMN image_upload_id TEXT REFERENCES uploads(id)",
]


class Database:
    def __init__(self, path: str):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise
        self._lock = asyncio.Lock()

    def close(self) -> None:
        self._conn.close()

    # --- low level (call under _lock via the async helpers) --------------
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, tuple(params))

    async def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        async with self._lock:
            self._exec(sql, params)

    async def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        async with self._lock:
            self._conn.executemany(sql, [tuple(r) for r in rows])

    async def fetch_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        async with self._lock:
            return self._exec(sql, params).fetchone()

    async def fetch_all(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        async with self._lock:
            return self._exec(sql, params).fetchall()

    async def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        async with self._lock:
            cur = self._exec(sql, params)
            return int(cur.lastrowid or 0)

    async def transaction(self, statements: list[tuple[str, Iterable[Any]]]) -> None:
        async with self._lock:
            try:
                self._conn.execute("BEGIN")
                for sql, params in statements:
                    self._exec(sql, params)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(get_settings().database_path)
    return _db


def now() -> float:
    """Canonical time source: Unix seconds, UTC."""
    return time.time()
