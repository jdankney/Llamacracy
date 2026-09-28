# SPDX-License-Identifier: AGPL-3.0-or-later
"""A conversation's workspace: text files the model writes, edits and reads.

Every write is a new row (a version), never an update, and each version is
tied to the user message whose turn wrote it. What a turn sees is the newest
version of each path among the messages on its own branch. So workspaces fork
exactly like conversations do: edit a message and the new branch starts from
the files as they were at that point, while the old branch keeps its own.
Nothing is ever lost, and flipping between branches flips the files too.

Files are text only (notes, drafts, code, CSV, HTML...), capped in size and
number, and always served back as downloads. A path is a name, never a place
on disk: it is validated and stored in the database, nothing more.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from .db import Database, now

MAX_FILES = 40
MAX_FILE_CHARS = 200_000
_PATH_RE = re.compile(r"^[\w\- ]+(?:/[\w\- ]+){0,3}\.[A-Za-z0-9]{1,10}$")


class WorkspaceError(Exception):
    """Why a write was refused, phrased for the model."""


def check_path(path: str) -> str:
    path = (path or "").strip().strip("/")
    if len(path) > 120 or not _PATH_RE.match(path) or ".." in path:
        raise WorkspaceError(
            f"{path!r} isn't a usable file name. Use something like notes.md or "
            "drafts/letter.txt: letters, digits, - _ and spaces, up to four folders, "
            "and an extension.")
    return path


@dataclass
class FileVersion:
    id: int
    path: str
    content: str | None           # None: deleted at this version
    message_id: int
    created_at: float


@dataclass
class Workspace:
    """The files one turn can see, and the writes it has made so far."""

    db: Database
    conversation_id: str
    ancestors: list[int]          # message ids on the branch, oldest first
    files: dict[str, FileVersion] = field(default_factory=dict)
    anchor: int | None = None     # the user message this turn answers (set once it exists)
    changed: dict[str, dict] = field(default_factory=dict)   # path -> what this turn did

    @classmethod
    async def open(cls, db: Database, conversation_id: str | None,
                   ancestors: list[int]) -> Workspace:
        ws = cls(db, conversation_id or "", list(ancestors))
        if conversation_id and ancestors:
            ws.files = await branch_files(db, conversation_id, ancestors)
        return ws

    def listing(self) -> list[FileVersion]:
        return sorted((f for f in self.files.values() if f.content is not None),
                      key=lambda f: f.path)

    def get(self, path: str) -> FileVersion | None:
        f = self.files.get(path)
        return f if f is not None and f.content is not None else None

    async def write(self, path: str, content: str) -> tuple[dict, dict]:
        """Save a new version. Returns (this write, everything this turn has
        done to the file so far): the trail shows the first, the reply's
        file list the second."""
        path = check_path(path)
        if len(content) > MAX_FILE_CHARS:
            raise WorkspaceError(f"that's {len(content):,} characters; one file holds at most "
                                 f"{MAX_FILE_CHARS:,}. Split it into several files.")
        old = self.get(path)
        if old is None and len(self.listing()) >= MAX_FILES:
            raise WorkspaceError(f"the workspace already has {MAX_FILES} files, the most it can hold.")
        if self.anchor is None or not self.conversation_id:
            raise WorkspaceError("the workspace isn't ready yet; try again.")
        ts = now()
        vid = await self.db.insert(
            "INSERT INTO workspace_files (conversation_id, message_id, path, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (self.conversation_id, self.anchor, path, content, ts))
        self.files[path] = FileVersion(vid, path, content, self.anchor, ts)
        added, removed = diff_counts(old.content if old else "", content)
        first = self.changed.get(path)
        change = {"path": path, "id": vid, "chars": len(content),
                  # a file this turn created stays "created" however often it is edited after
                  "created": first["created"] if first else old is None,
                  "added": added + (first["added"] if first else 0),
                  "removed": removed + (first["removed"] if first else 0)}
        self.changed[path] = change
        step = {"path": path, "chars": len(content), "created": old is None,
                "added": added, "removed": removed}
        return step, change


def diff_counts(a: str, b: str) -> tuple[int, int]:
    added = removed = 0
    for line in difflib.ndiff(a.splitlines(), b.splitlines()):
        if line.startswith("+ "):
            added += 1
        elif line.startswith("- "):
            removed += 1
    return added, removed


async def branch_files(db: Database, conversation_id: str,
                       ancestors: list[int]) -> dict[str, FileVersion]:
    """Newest version of each path written on this branch."""
    if not ancestors:
        return {}
    marks = ",".join("?" * len(ancestors))
    rows = await db.fetch_all(
        f"SELECT id, path, content, message_id, created_at FROM workspace_files "
        f"WHERE conversation_id = ? AND message_id IN ({marks}) ORDER BY id",
        (conversation_id, *ancestors))
    out: dict[str, FileVersion] = {}
    for r in rows:
        out[r["path"]] = FileVersion(r["id"], r["path"], r["content"], r["message_id"], r["created_at"])
    return out
