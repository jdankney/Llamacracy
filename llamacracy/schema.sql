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
    -- Compact context: messages with id <= compact_boundary_id are folded into
    -- context_summary and no longer resent verbatim (still visible in the UI,
    -- never deleted -- just excluded from the prompt going forward).
    compact_boundary_id  INTEGER,
    context_summary      TEXT,
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
    created_at        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_messages_conversation ON messages(conversation_id, id);

-- uploaded images (Phase 8.4) -- on disk under UPLOAD_DIR, never base64-in-DB.
-- Scoped to the uploader; served back only to them (see /api/uploads/{id}).
CREATE TABLE IF NOT EXISTS uploads (
    id           TEXT PRIMARY KEY,       -- uuid hex, also the on-disk filename stem
    user_id      INTEGER NOT NULL REFERENCES users(id),
    path         TEXT NOT NULL,
    mime         TEXT NOT NULL,
    bytes        INTEGER NOT NULL,
    created_at   REAL NOT NULL
);

-- Per-user API keys for the OpenAI-compatible endpoint (Phase 8.2, Continue.dev
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
