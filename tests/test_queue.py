# SPDX-License-Identifier: AGPL-3.0-or-later
"""The queue is strict FIFO, one job at a time, cancellable, and
records the timing fields metering will bill on."""

from __future__ import annotations

import asyncio

import pytest

from llamacracy.config import Settings
from llamacracy.db import Database
from llamacracy.queue import Job, QueueManager
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
    object.__setattr__(cfg, "idle_ttl_minutes", 0)   # unload as soon as idle
    object.__setattr__(cfg, "idle_ttl_seconds", 0.0)  # (wins over minutes; also ignore any .env value)
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


def _idle_cfg(seconds: float) -> Settings:
    cfg = Settings()
    object.__setattr__(cfg, "idle_ttl_seconds", seconds)
    return cfg


@pytest.mark.asyncio
async def test_a_locked_model_is_kept_loaded_and_alive(tmp_db_path):
    up = FakeUpstream()
    qm = await _make_qm(tmp_db_path, up, _idle_cfg(0.0))
    qm._idle_poll_s = 0.02
    qm._keepalive_s = 0.05
    try:
        a = _job("small", user=1)
        await qm.submit(a)
        await _drain(a)
        await qm.set_locked("small")
        await asyncio.sleep(0.3)
        assert up.unload_calls == 0             # the idle timer leaves it alone...
        assert up.loaded == "small"
        assert up.touches.count("small") >= 2   # ...and llama-swap's ttl is held off
        assert qm.snapshot()["locked_model"] == "small"
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_the_locked_model_comes_back_after_another_models_idle_time(tmp_db_path):
    up = FakeUpstream()
    qm = await _make_qm(tmp_db_path, up, _idle_cfg(0.1))
    qm._idle_poll_s = 0.02
    try:
        await qm.set_locked("small")
        await asyncio.sleep(0.1)
        assert up.loaded == "small"             # nothing was loaded: preloaded at once
        b = _job("big", user=2)                  # someone else's model swaps it out
        await qm.submit(b)
        await _drain(b)
        assert up.loaded == "big"
        await asyncio.sleep(0.03)
        assert up.loaded == "big"               # big gets its idle time first
        await asyncio.sleep(0.25)
        assert up.loaded == "small"
        assert up.unload_calls == 0
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_the_lock_survives_a_restart_and_force_unload_releases_it(tmp_db_path):
    up = FakeUpstream()
    qm = await _make_qm(tmp_db_path, up)
    await qm.set_locked("big")
    await qm.stop()
    qm = await _make_qm(tmp_db_path, up)
    try:
        assert qm.locked_model == "big"
        assert await qm.force_unload() is True
        assert qm.locked_model is None
        qm2 = QueueManager(qm.db, up, _registry(), Settings())
        await qm2.start()
        assert qm2.locked_model is None          # released for good
        await qm2.stop()
        with pytest.raises(ValueError):
            await qm.set_locked("nope")
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_a_preload_gives_way_to_a_job_that_arrives(tmp_db_path):
    up = FakeUpstream()
    hang = asyncio.Event()                      # a locked model that takes forever to load

    async def slow_touch(model_id, timeout=600.0):
        up.touches.append(model_id)
        await hang.wait()
        up.loaded = model_id
        return True

    up.touch = slow_touch
    qm = await _make_qm(tmp_db_path, up, _idle_cfg(30.0))
    qm._idle_poll_s = 0.01
    try:
        await qm.set_locked("small")
        await asyncio.sleep(0.05)
        assert up.touches == ["small"]           # preload under way, holding the slot
        b = _job("big", user=2)
        await qm.submit(b)
        await asyncio.wait_for(_drain(b), 2)     # ...but the job doesn't wait for it
        row = await qm.db.fetch_one("SELECT state FROM jobs WHERE id=?", (b.id,))
        assert row["state"] == "done"
        assert up.loaded == "big"
    finally:
        hang.set()
        await qm.stop()


@pytest.mark.asyncio
async def test_a_hung_preload_is_given_up(tmp_db_path):
    up = FakeUpstream()
    seen = []

    async def hung_touch(model_id, timeout=600.0):
        seen.append(timeout)
        await asyncio.sleep(timeout)
        return True

    up.touch = hung_touch
    qm = await _make_qm(tmp_db_path, up, _idle_cfg(0.0))
    qm._idle_poll_s = 0.01
    try:
        await qm.set_locked("big")                # seed load ~25 s
        await asyncio.sleep(0.05)
        assert seen and seen[0] == pytest.approx(max(60.0, 3 * qm.load_estimate("big")))
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_a_dead_running_endpoint_doesnt_stop_the_poller(tmp_db_path):
    up = FakeUpstream()
    calls = []

    async def garbled():
        calls.append(1)
        raise ValueError("not JSON")

    qm = await _make_qm(tmp_db_path, up)
    qm._running_poll_s = 0.01
    up.loaded_model = garbled
    try:
        await asyncio.sleep(0.1)
        assert len(calls) >= 3 and not qm._tasks[2].done()
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_a_failed_preload_backs_off(tmp_db_path):
    up = FakeUpstream()
    up.touch_ok = False
    qm = await _make_qm(tmp_db_path, up, _idle_cfg(0.0))
    qm._idle_poll_s = 0.01
    try:
        await qm.set_locked("small")
        await asyncio.sleep(0.15)
        assert up.touches == ["small"]           # one try, then it waits
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_locking_loads_the_model_now_and_unlocking_changes_nothing(tmp_db_path):
    up = FakeUpstream()
    qm = await _make_qm(tmp_db_path, up, _idle_cfg(30.0))
    qm._idle_poll_s = 0.02
    try:
        a = _job("big", user=1)
        await qm.submit(a)
        await _drain(a)
        await qm.set_locked("small")             # big is resident and not idle long
        await asyncio.sleep(0.1)
        assert up.loaded == "small"               # loaded straight away anyway
        await qm.set_locked("big")
        await qm.set_locked(None)                 # changed their mind at once
        await asyncio.sleep(0.1)
        assert up.loaded == "small" and up.unload_calls == 0   # idle clock untouched
    finally:
        await qm.stop()


def test_a_picture_counts_as_a_pictures_worth_of_prompt():
    from llamacracy.queue import IMAGE_TOKENS, _estimate_prompt
    photo = "data:image/jpeg;base64," + "A" * 3_000_000          # a ~2 MB phone photo
    msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": photo}},
                                         {"type": "text", "text": "what is this?"}]}]
    est = _estimate_prompt(msgs)
    assert IMAGE_TOKENS <= est < IMAGE_TOKENS + 100
    assert _estimate_prompt([{"role": "user", "content": "x" * 3600}]) >= 1000


async def _conversation(db: Database, user: int = 1) -> tuple[str, int]:
    await db.execute(
        "INSERT INTO conversations (id, user_id, title, model_id, created_at, updated_at) "
        "VALUES ('c1', ?, 't', 'small', 0, 0)", (user,))
    mid = await db.insert(
        "INSERT INTO messages (conversation_id, role, content, created_at) "
        "VALUES ('c1', 'user', 'hi', 0)")
    return "c1", mid


@pytest.mark.asyncio
async def test_a_stopped_reply_keeps_what_was_written(tmp_db_path):
    up = FakeUpstream()
    up.delay = 0.05
    up.script = {"small": ["tok "] * 40}
    qm = await _make_qm(tmp_db_path, up)
    try:
        conv, mid = await _conversation(qm.db)
        a = _job(user=1, conversation_id=conv, parent_message_id=mid)
        await qm.submit(a)
        await asyncio.sleep(0.2)
        assert await qm.cancel(a.id, user_id=1) is True
        events = await _drain(a)
        assert events[-1]["type"] == "done" and events[-1]["state"] == "cancelled"
        row = await qm.db.fetch_one(
            "SELECT content, stopped, parent_id FROM messages WHERE role = 'assistant'")
        assert row["content"].startswith("tok tok") and len(row["content"]) < 160
        assert row["stopped"] == 1 and row["parent_id"] == mid
        shown = await qm.db.fetch_one("SELECT active_child_id FROM messages WHERE id = ?", (mid,))
        assert shown["active_child_id"] is not None
    finally:
        await qm.stop()


@pytest.mark.asyncio
async def test_a_reply_stopped_before_its_first_word_saves_nothing(tmp_db_path):
    up = FakeUpstream()
    up.delay = 0.3
    qm = await _make_qm(tmp_db_path, up)
    try:
        conv, mid = await _conversation(qm.db)
        a = _job(user=1, conversation_id=conv, parent_message_id=mid)
        await qm.submit(a)
        await asyncio.sleep(0.05)
        assert await qm.cancel(a.id, user_id=1) is True
        await _drain(a)
        assert await qm.db.fetch_one("SELECT id FROM messages WHERE role = 'assistant'") is None
    finally:
        await qm.stop()
