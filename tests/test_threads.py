# SPDX-License-Identifier: AGPL-3.0-or-later
"""Conversation trees: the pure path logic, and the migration that turns
pre-branching (linear) conversations into single-branch trees."""

from __future__ import annotations

import sqlite3

from llamacracy import threads
from llamacracy.db import Database


def _m(id, parent, child=None, role="user", content="", summary=None):
    return {"id": id, "parent_id": parent, "active_child_id": child, "role": role,
            "content": content or f"m{id}", "context_summary": summary}


# 1 - 2 - 3          two versions of message 3 (3 and 5), 5 showing
#       \ 5 - 6
ROWS = [_m(1, None, 2), _m(2, 1, 5, "assistant"), _m(3, 2, 4), _m(4, 3, None, "assistant"),
        _m(5, 2, 6), _m(6, 5, None, "assistant")]


def test_active_path_follows_the_shown_versions():
    assert [r["id"] for r in threads.active_path(1, ROWS)] == [1, 2, 5, 6]


def test_path_to_walks_up_from_any_message():
    assert [r["id"] for r in threads.path_to(4, ROWS)] == [1, 2, 3, 4]
    assert threads.path_to(None, ROWS) == []


def test_siblings_group_versions_by_parent():
    sib = threads.siblings(ROWS)
    assert sib[2] == [3, 5] and sib[None] == [1]


def test_history_uses_the_deepest_summary_on_the_path():
    rows = [_m(1, None, 2), _m(2, 1, 3, "assistant", summary="S"), _m(3, 2, 4), _m(4, 3, None, "assistant")]
    h = threads.history(threads.active_path(1, rows))
    assert h[0]["role"] == "system" and h[0]["content"].endswith("S")
    assert [m["content"] for m in h[1:]] == ["m3", "m4"]


def test_history_skips_a_reply_that_only_thought():
    rows = [_m(1, None, 2), {**_m(2, 1, None, "assistant"), "content": ""}]
    assert threads.history(rows) == [{"role": "user", "content": "m1"}]


def test_a_cycle_cannot_hang_the_walk():
    rows = [_m(1, None, 2), _m(2, 1, 1)]
    assert [r["id"] for r in threads.active_path(1, rows)] == [1, 2]


def test_linear_conversations_migrate_to_one_branch(tmp_path):
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    # the shape a pre-branching install has: no tree columns, compaction on the conversation
    c.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY, oidc_sub TEXT, email TEXT, display_name TEXT,
          is_admin INT, disabled INT, session_credit_limit_override REAL,
          weekly_credit_limit_override REAL, created_at REAL, last_active_at REAL);
        CREATE TABLE conversations (id TEXT PRIMARY KEY, user_id INT, title TEXT, model_id TEXT,
          compact_boundary_id INTEGER, context_summary TEXT, created_at REAL, updated_at REAL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT,
          role TEXT, content TEXT, model_id TEXT, prompt_tokens INT, completion_tokens INT,
          usage_estimated INT DEFAULT 0, created_at REAL);
        INSERT INTO conversations VALUES ('c', 1, 't', 'm', 2, 'SUMMARY', 0, 0);
        INSERT INTO messages (conversation_id, role, content, created_at) VALUES
          ('c', 'user', 'a', 0), ('c', 'assistant', 'b', 0), ('c', 'user', 'c', 0), ('c', 'assistant', 'd', 0);
    """)
    c.close()
    Database(path).close()
    db = Database(path)                     # a second start must change nothing
    rows = [tuple(r) for r in db._conn.execute(
        "SELECT id, parent_id, active_child_id, context_summary FROM messages ORDER BY id")]
    assert rows == [(1, None, 2, None), (2, 1, 3, "SUMMARY"), (3, 2, 4, None), (4, 3, None, None)]
    assert db._conn.execute("SELECT active_root_id FROM conversations").fetchone()[0] == 1
    db.close()
