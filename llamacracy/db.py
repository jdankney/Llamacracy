# SPDX-License-Identifier: AGPL-3.0-or-later
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
    "ALTER TABLE conversations ADD COLUMN compact_boundary_id INTEGER",
    "ALTER TABLE conversations ADD COLUMN context_summary TEXT",
    "ALTER TABLE users ADD COLUMN prefs_json TEXT",
    "ALTER TABLE messages ADD COLUMN reasoning TEXT",
    "ALTER TABLE messages ADD COLUMN thinking_seconds REAL",
    "ALTER TABLE messages ADD COLUMN parent_id INTEGER",
    "ALTER TABLE messages ADD COLUMN active_child_id INTEGER",
    "ALTER TABLE messages ADD COLUMN context_summary TEXT",
    "ALTER TABLE conversations ADD COLUMN active_root_id INTEGER",
]

# Conversations became trees (message editing). A conversation from before
# that is one straight line: link each message to the one before it, point
# each at the one after, and make the first the active root. Its compaction
# summary moves onto the boundary message. Only touches conversations with no
# active_root_id yet, so it is a no-op after the first run.
_TREE_MIGRATION = """
BEGIN;
UPDATE messages SET context_summary = (
    SELECT c.context_summary FROM conversations c WHERE c.compact_boundary_id = messages.id)
  WHERE context_summary IS NULL AND id IN (
    SELECT compact_boundary_id FROM conversations
    WHERE active_root_id IS NULL AND context_summary IS NOT NULL);
UPDATE messages SET
    parent_id = (SELECT MAX(p.id) FROM messages p
                 WHERE p.conversation_id = messages.conversation_id AND p.id < messages.id),
    active_child_id = (SELECT MIN(c.id) FROM messages c
                       WHERE c.conversation_id = messages.conversation_id AND c.id > messages.id)
  WHERE conversation_id IN (SELECT id FROM conversations WHERE active_root_id IS NULL);
UPDATE conversations SET active_root_id = (
    SELECT MIN(id) FROM messages WHERE conversation_id = conversations.id)
  WHERE active_root_id IS NULL;
COMMIT;
"""


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
        self._conn.executescript(_TREE_MIGRATION)
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
