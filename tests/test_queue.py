"""Phase 2: the queue is strict FIFO, one job at a time, cancellable, and
records the timing fields metering will bill on."""

from __future__ import annotations

import asyncio

import pytest

from llamacracy.config import Settings
from llamacracy.db import Database
from llamacracy.queue import Job, JobState, QueueManager
from llamacracy.registry import ModelInfo, Registry

from tests.conftest import FakeUpstream


def _registry() -> Registry:
    models = {
        "small": ModelInfo("small", "Small", "chat", "fast", True, "", "off",
                           seed_cold_load_s=2.0, seed_tg_tok_s=40.0),
        "big": ModelInfo("big", "Big", "chat", "heavy", True, "", "off",
                         seed_cold_load_s=25.0, seed_tg_tok_s=30.0),
    }
    return Registry(models, "test")


async def _seed_users(db: Database) -> None:
    for uid in (1, 2, 99):
        await db.execute(
            "INSERT OR IGNORE INTO users (id, oidc_sub, email, created_at) VALUES (?,?,?,0)",
            (uid, f"sub{uid}", f"u{uid}@x"),
        )


async def _make_qm(db_path: str, up: FakeUpstream, cfg: Settings | None = None) -> QueueManager:
    db = Database(db_path)
    await _seed_users(db)
    qm = QueueManager(db, up, _registry(), cfg or Settings())
    await qm.start()
    return qm


def _job(model="small", user=1, **kw) -> Job:
    return Job(user_id=user, owner_name=f"u{user}", owner_email=f"u{user}@x",
               model_id=model, payload={"messages": [{"role": "user", "content": "hi"}]},
               **kw)


async def _drain(job: Job):
    out = []
    while True:
        c = await job._chunks.get()
        if c is None:
            break
        out.append(c)
    return out


@pytest.mark.asyncio
async def test_fifo_order_one_at_a_time(tmp_db_path):
    up = FakeUpstream()
    up.delay = 0.05
    up.script = {"big": ["a"] * 10, "small": ["b"] * 3}
    qm = await _make_qm(tmp_db_path, up)
    try:
        big = _job("big", user=1)
        small = _job("small", user=2)
        await qm.submit(big)
        await asyncio.sleep(0.01)
        await qm.submit(small)

        # Alice's big job is active; Bob's small job waits behind it (deliberate)
        await asyncio.sleep(0.02)
        assert qm._active is big
        assert qm.position_of(small.id) == 1

        await _drain(big)
        await _drain(small)
        rows = await qm.db.fetch_all(
            "SELECT id, state FROM jobs ORDER BY queued_at")
        assert [r["state"] for r in rows] == ["done", "done"]
        # big finished before small started
        j = {r["id"]: r for r in await qm.db.fetch_all(
            "SELECT id, gen_started_at, finished_at FROM jobs")}
        assert j[small.id]["gen_started_at"] >= j[big.id]["finished_at"]
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_cancel_while_queued_removes_without_running(tmp_db_path):
    up = FakeUpstream()
    up.delay = 0.05
    up.script = {"small": ["x"] * 8}
    qm = await _make_qm(tmp_db_path, up)
    try:
        a, b = _job(user=1), _job(user=2)
        await qm.submit(a)
        await qm.submit(b)
        await asyncio.sleep(0.01)
        assert await qm.cancel(b.id, user_id=2) is True
        await _drain(a)
        await _drain(b)
        row = await qm.db.fetch_one("SELECT state, occupancy_seconds FROM jobs WHERE id=?", (b.id,))
        assert row["state"] == "cancelled"
        assert row["occupancy_seconds"] == 0.0   # never held the box -> not billable
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_cancel_wrong_user_denied(tmp_db_path):
    up = FakeUpstream()
    qm = await _make_qm(tmp_db_path, up)
    try:
        a = _job(user=1)
        await qm.submit(a)
        assert await qm.cancel(a.id, user_id=99) is False
        await _drain(a)
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_cancel_while_generating_records_partial(tmp_db_path):
    up = FakeUpstream()
    up.delay = 0.05
    up.script = {"small": ["tok"] * 40}
    qm = await _make_qm(tmp_db_path, up)
    try:
        a = _job(user=1)
        await qm.submit(a)
        await asyncio.sleep(0.14)              # let a few tokens stream
        assert await qm.cancel(a.id, user_id=1) is True
        await _drain(a)
        row = await qm.db.fetch_one(
            "SELECT state, gen_seconds, occupancy_seconds, completion_tokens, usage_estimated "
            "FROM jobs WHERE id=?", (a.id,))
        assert row["state"] == "cancelled"
        assert row["occupancy_seconds"] > 0
        assert row["completion_tokens"] < 40   # stopped early
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_cold_start_detection_and_load_estimate_learns(tmp_db_path):
    up = FakeUpstream()
    up.loaded = None
    qm = await _make_qm(tmp_db_path, up)
    try:
        a = _job("small", user=1)
        await qm.submit(a)
        await _drain(a)
        r1 = await qm.db.fetch_one("SELECT cold_start FROM jobs WHERE id=?", (a.id,))
        assert r1["cold_start"] == 1

        b = _job("small", user=1)      # model now resident -> warm
        await qm.submit(b)
        await _drain(b)
        r2 = await qm.db.fetch_one("SELECT cold_start FROM jobs WHERE id=?", (b.id,))
        assert r2["cold_start"] == 0

        stat = await qm.db.fetch_one(
            "SELECT samples FROM model_load_stats WHERE model_id='small'")
        assert stat and stat["samples"] >= 1
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_idle_ttl_unloads(tmp_db_path):
    up = FakeUpstream()
    cfg = Settings()
    object.__setattr__(cfg, "idle_ttl_minutes", 0)  # unload as soon as idle
    qm = await _make_qm(tmp_db_path, up, cfg)
    qm._idle_poll_s = 0.05
    try:
        a = _job("small", user=1)
        await qm.submit(a)
        await _drain(a)
        assert up.loaded == "small"
        await asyncio.sleep(0.2)  # a few idle-monitor ticks
        assert up.unload_calls >= 1
    finally:
        await qm.stop()
