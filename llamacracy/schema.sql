-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Llamacracy schema. SQLite, WAL mode. Applied idempotently at startup.
-- All timestamps are REAL Unix seconds, UTC. The rolling weekly credit query
-- runs on every enqueue, so jobs is indexed for it.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS users (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    oidc_sub                    TEXT NOT NULL UNIQUE,   -- identity key; emails can change, sub cannot
    email                       TEXT NOT NULL,
    display_name                TEXT NOT NULL DEFAULT '',
    is_admin                    INTEGER NOT NULL DEFAULT 0,  -- cached from ADMIN_EMAILS; auth still checks config
    disabled                    INTEGER NOT NULL DEFAULT 0,
    uncapped                    INTEGER NOT NULL DEFAULT 0,  -- never blocked at enqueue; usage % still shown (can exceed 100)
    session_credit_limit_override  REAL,
    weekly_credit_limit_override   REAL,
    -- per-user UI preferences as a JSON object (appearance: colours, chat
    -- text size). Purely cosmetic; validated on write, never trusted as CSS.
    prefs_json                  TEXT,
    created_at                  REAL NOT NULL,
    last_active_at              REAL
);

CREATE TABLE IF NOT EXISTS conversations (
    id           TEXT PRIMARY KEY,          -- uuid hex, used in URLs
    user_id      INTEGER NOT NULL REFERENCES users(id),
    title        TEXT NOT NULL DEFAULT '',  -- first 50 chars of first user message
    model_id     TEXT NOT NULL,
    -- Legacy: compaction used to live here, one per conversation. It now
    -- lives on the boundary message (messages.context_summary) so that each
    -- branch keeps its own; db.py migrates old rows. No longer written.
    compact_boundary_id  INTEGER,
    context_summary      TEXT,
    -- the first message of the branch being shown (see threads.py)
    active_root_id       INTEGER,
    -- a study session: the course it belongs to (see study.py) and the mode
    -- its last turn ran in (guide/explain/check/quiz); NULL for chat
    course_id            TEXT,
    study_mode           TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_conversations_user ON conversations(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id   TEXT NOT NULL REFERENCES conversations(id),
    role              TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content           TEXT NOT NULL,
    model_id          TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    usage_estimated   INTEGER NOT NULL DEFAULT 0,
    search_json       TEXT,                  -- SearXNG outcome for this (user) turn, if requested
    image_upload_id   TEXT REFERENCES uploads(id),  -- attached image for this (user) turn, if any
    doc_ids_json      TEXT,                  -- JSON list of attached document upload ids, if any
    reasoning         TEXT,                  -- the model's thinking before this (assistant) reply, if it thought
    thinking_seconds  REAL,                  -- first thinking token -> first answer token
    tools_json        TEXT,                  -- agent reply: {"steps": [...], "sources": [...]}
    -- Conversations are trees (editing a message forks it; see threads.py).
    -- Logical links only, no FK: a conversation's messages are deleted together.
    parent_id         INTEGER,               -- NULL for a conversation's first message(s)
    active_child_id   INTEGER,               -- which child (reply, or edited version) is shown
    -- set on the message where history was compacted: stands in for it and
    -- everything above it, on every path through it
    context_summary   TEXT,
    created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_conversation ON messages(conversation_id, id);

-- Study: a person's courses, private to them like their conversations. A
-- course's materials are documents they uploaded for it; its sessions are
-- conversations with course_id set. Nothing about a course is configuration:
-- it all lives here, in the instance's own database.
CREATE TABLE IF NOT EXISTS courses (
    id            TEXT PRIMARY KEY,          -- uuid hex, used in URLs (/study/<id>)
    user_id       INTEGER NOT NULL REFERENCES users(id),
    name          TEXT NOT NULL,
    notes         TEXT NOT NULL DEFAULT '',  -- what the tutor should know: level, textbook, exam format
    quantitative  INTEGER NOT NULL DEFAULT 0, -- 1: every number in an answer must come from run_python
    web           INTEGER NOT NULL DEFAULT 1, -- 1: the tutor searches the web for anything current
    model_id      TEXT,                      -- the model its last session used
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_courses_user ON courses(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS course_materials (
    course_id   TEXT NOT NULL REFERENCES courses(id),
    upload_id   TEXT NOT NULL REFERENCES uploads(id),
    added_at    REAL NOT NULL,
    PRIMARY KEY (course_id, upload_id)
);

-- Practice problems a tutor wrote (practice.py), with keys the server
-- checked, and every attempt at them: what the course's progress is made of.
CREATE TABLE IF NOT EXISTS practice_problems (
    id               TEXT PRIMARY KEY,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    conversation_id  TEXT NOT NULL,
    course_id        TEXT,
    n                INTEGER NOT NULL,           -- its number in the set
    topic            TEXT NOT NULL DEFAULT '',
    kind             TEXT NOT NULL CHECK (kind IN ('number', 'choice', 'short')),
    question         TEXT NOT NULL,
    choices_json     TEXT,                       -- choice: the options
    answer           TEXT NOT NULL,              -- the key, as written (choice: the right option)
    answer_value     REAL,                       -- number: the key's value
    unit             TEXT,
    solution         TEXT NOT NULL DEFAULT '',
    source           TEXT,
    quote            TEXT,                       -- words in the materials the key rests on (checked)
    code             TEXT,                       -- number: the Python that computed the key (run)
    code_output      TEXT,
    verified         TEXT,                       -- how the key was checked: 'code' | 'quote' | NULL
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_practice_course ON practice_problems(user_id, course_id, created_at);
CREATE INDEX IF NOT EXISTS ix_practice_conv ON practice_problems(conversation_id);

CREATE TABLE IF NOT EXISTS practice_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    problem_id  TEXT NOT NULL REFERENCES practice_problems(id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    answer      TEXT,
    outcome     TEXT NOT NULL CHECK (outcome IN ('right', 'wrong', 'revealed', 'self_right', 'self_wrong')),
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_attempts_problem ON practice_attempts(problem_id, id);

-- uploaded images and documents -- on disk under UPLOAD_DIR, never in the DB.
-- Scoped to the uploader; served back only to them (see /api/uploads/{id}).
CREATE TABLE IF NOT EXISTS uploads (
    id           TEXT PRIMARY KEY,       -- uuid hex, also the on-disk filename stem
    user_id      INTEGER NOT NULL REFERENCES users(id),
    path         TEXT NOT NULL,
    mime         TEXT NOT NULL,
    bytes        INTEGER NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'image',   -- image|document
    filename     TEXT,                   -- as uploaded (documents); display only, never a path
    text_path    TEXT,                   -- documents: the extracted Markdown on disk
    meta_json    TEXT,                   -- documents: {tokens, units, unit, warnings}
    created_at   REAL NOT NULL
);

-- A conversation's workspace: files the model writes. Every write is a new
-- row, tied to the user message whose turn wrote it, so a branch sees only
-- the versions on its own path (see workspace.py). content NULL = deleted.
CREATE TABLE IF NOT EXISTS workspace_files (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  TEXT NOT NULL REFERENCES conversations(id),
    message_id       INTEGER NOT NULL,
    path             TEXT NOT NULL,
    content          TEXT,
    created_at       REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_workspace_conv ON workspace_files(conversation_id, id);

-- Per-user API keys for the OpenAI-compatible endpoint (Continue.dev
-- etc.) -- bearer-token auth for that endpoint only; the web UI still uses
-- oauth2-proxy. Only the sha256 hash is stored -- the plaintext key is shown
-- once at creation and never retrievable again.
CREATE TABLE IF NOT EXISTS api_keys (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id),
    key_hash       TEXT NOT NULL UNIQUE,
    label          TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL,
    last_used_at   REAL
);
CREATE INDEX IF NOT EXISTS ix_api_keys_user ON api_keys(user_id);

CREATE TABLE IF NOT EXISTS jobs (
    id                TEXT PRIMARY KEY,       -- uuid hex
    user_id           INTEGER NOT NULL REFERENCES users(id),
    conversation_id   TEXT REFERENCES conversations(id),
    session_id        INTEGER REFERENCES sessions(id),   -- session active at enqueue
    model_id          TEXT NOT NULL,
    state             TEXT NOT NULL,          -- queued|loading_model|generating|done|error|cancelled|limit_exceeded
    lane              TEXT NOT NULL DEFAULT 'exclusive',  -- exclusive|fast (fast lane not full price)
    queued_at         REAL NOT NULL,
    picked_at         REAL,               -- worker took the job = exclusive occupancy starts
    load_started_at   REAL,
    gen_started_at    REAL,
    finished_at       REAL,
    load_seconds      REAL NOT NULL DEFAULT 0,
    gen_seconds       REAL NOT NULL DEFAULT 0,
    occupancy_seconds REAL NOT NULL DEFAULT 0,   -- picked_at -> finished_at; the billing basis
    credits           REAL NOT NULL DEFAULT 0,   -- always measured, never estimated
    cost_usd          REAL NOT NULL DEFAULT 0,
    rate_used         REAL,                       -- $/kWh in effect when the job ran
    gpu_watts_mean    REAL,                       -- measured mean nvidia-smi draw during the job
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    usage_estimated   INTEGER NOT NULL DEFAULT 0,
    cold_start        INTEGER NOT NULL DEFAULT 0,  -- 1 if this job triggered a model load
    error             TEXT
);
-- the hot path: rolling 7-day SUM(credits) per user on every enqueue
CREATE INDEX IF NOT EXISTS ix_jobs_user_finished ON jobs(user_id, finished_at);
CREATE INDEX IF NOT EXISTS ix_jobs_state ON jobs(state);
CREATE INDEX IF NOT EXISTS ix_jobs_model_finished ON jobs(model_id, finished_at);

CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    started_at   REAL NOT NULL,
    expires_at   REAL NOT NULL,          -- started_at + SESSION_WINDOW_HOURS, fixed (no activity extension)
    credits_used REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_sessions_user ON sessions(user_id, expires_at DESC);

CREATE TABLE IF NOT EXISTS invoices (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    period_start    REAL NOT NULL,
    period_end      REAL NOT NULL,
    total_credits   REAL NOT NULL,
    total_cost_usd  REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'sent', 'paid')),
    created_at      REAL NOT NULL
);

-- learned per-model load times: EWMA over observed cold starts, seeded from bench
CREATE TABLE IF NOT EXISTS model_load_stats (
    model_id            TEXT PRIMARY KEY,
    ewma_load_seconds   REAL NOT NULL,
    samples             INTEGER NOT NULL DEFAULT 0,
    updated_at          REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO schema_meta(key, value) VALUES ('version', '1')
    ON CONFLICT(key) DO NOTHING;
