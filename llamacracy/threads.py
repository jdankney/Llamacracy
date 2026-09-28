# SPDX-License-Identifier: AGPL-3.0-or-later
"""Conversation trees: what editing a message forks, and which fork is shown.

Every message points at its `parent_id` (NULL for a conversation's first
messages), so editing a message never overwrites anything: the edit is a new
sibling of the original, with its own reply below it. Each message remembers
which of its children is currently shown (`active_child_id`), and the
conversation remembers which first message is (`active_root_id`). Following
those pointers from the root gives the *active path*: the one chain of
messages the user sees, and the only one ever sent to the model.

Compaction is per branch for the same reason. A summary lives on the message
where history was cut (`messages.context_summary`) and stands in for that
message and everything above it, so it only ever applies to paths that pass
through that message. A fork that split off earlier keeps its full history.

Message ids only grow and a child is always created after its parent, so ids
increase along any path. The front end relies on that for its "id > boundary"
checks.
"""

from __future__ import annotations

from . import documents
from .db import Database

_COLS = (
    "id, role, content, model_id, prompt_tokens, completion_tokens, usage_estimated, "
    "search_json, image_upload_id, doc_ids_json, tools_json, reasoning, thinking_seconds, parent_id, "
    "active_child_id, context_summary, created_at"
)


async def load(db: Database, conv_id: str) -> list[dict]:
    rows = await db.fetch_all(
        f"SELECT {_COLS} FROM messages WHERE conversation_id = ? ORDER BY id", (conv_id,))
    return [dict(r) for r in rows]


def active_path(root_id: int | None, rows: list[dict]) -> list[dict]:
    """The chain the user sees: from the active root, follow active children."""
    by_id = {r["id"]: r for r in rows}
    path: list[dict] = []
    seen: set[int] = set()
    cur = root_id
    while cur is not None and cur in by_id and cur not in seen:
        seen.add(cur)
        path.append(by_id[cur])
        cur = by_id[cur]["active_child_id"]
    return path


def path_to(msg_id: int | None, rows: list[dict]) -> list[dict]:
    """Root-to-message chain ending at `msg_id` (inclusive), via parent links.
    Empty for None: a message with no parent starts a conversation."""
    by_id = {r["id"]: r for r in rows}
    path: list[dict] = []
    seen: set[int] = set()
    cur = msg_id
    while cur is not None and cur in by_id and cur not in seen:
        seen.add(cur)
        path.append(by_id[cur])
        cur = by_id[cur]["parent_id"]
    return path[::-1]


def siblings(rows: list[dict]) -> dict[int | None, list[int]]:
    """parent id -> its children's ids, oldest first. Roots share key None."""
    out: dict[int | None, list[int]] = {}
    for r in rows:
        out.setdefault(r["parent_id"], []).append(r["id"])
    return out


def summary_point(path: list[dict]) -> tuple[int | None, str | None]:
    """The deepest compaction on this path: (boundary message id, summary)."""
    for r in reversed(path):
        if r.get("context_summary"):
            return r["id"], r["context_summary"]
    return None, None


def history(path: list[dict], documents: dict[str, str] | None = None) -> list[dict]:
    """What the model is sent for a path: the latest summary on it (if any)
    standing in for everything up to its boundary, then the rest verbatim.
    A reply with no text (all thinking, nothing answered) is left out.

    `documents` maps upload id -> the block documents.render_for_prompt made
    of it; a message's attached documents go back in ahead of its text, so
    they stay in the conversation for as long as the message itself does."""
    _, summary = summary_point(path)
    after = after_summary(path)
    out = [{"role": r["role"], "content": _with_documents(r, documents or {})}
           for r in after if r["content"]]
    if summary:
        out.insert(0, {
            "role": "system",
            "content": "Summary of the earlier part of this conversation "
                       "(context only -- don't refer to this note explicitly):\n" + summary,
        })
    return out


def after_summary(path: list[dict]) -> list[dict]:
    """The part of a path that is still sent verbatim (below any compaction)."""
    boundary, _ = summary_point(path)
    return path if boundary is None else path[[r["id"] for r in path].index(boundary) + 1:]


def document_ids(path: list[dict]) -> list[str]:
    """Every document attached along a path, in order, without repeats."""
    seen: dict[str, None] = {}
    for r in path:
        for i in documents.doc_ids(r):
            seen.setdefault(i, None)
    return list(seen)


def _with_documents(row: dict, blocks: dict[str, str]) -> str:
    ids = documents.doc_ids(row)
    if not ids:
        return row["content"]
    return "".join(blocks.get(i, "") for i in ids) + row["content"]
