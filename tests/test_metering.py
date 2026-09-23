# SPDX-License-Identifier: AGPL-3.0-or-later
"""The money math. A bug here bills a friend wrong, so:
window boundaries, overshoot, cancellation partial billing, rolling-weekly edges.
"""

from __future__ import annotations

import time
import uuid

import pytest

from llamacracy.config import Settings
from llamacracy.db import Database
from llamacracy.metering import WEEK_SECONDS, Meter
from llamacracy.registry import ModelInfo, Registry

DAY = 86400


def _settings(**over) -> Settings:
    s = Settings()
    defaults = dict(session_credit_limit=3600.0, weekly_credit_limit=12000.0,
                    session_window_hours=5.0, load_time_multiplier=0.5,
                    non_gpu_load_watts=110.0, markup=1.0, electricity_rate=0.456,
                    tou_schedule="")
    for k, v in {**defaults, **over}.items():
        object.__setattr__(s, k, v)
    return s


def _registry() -> Registry:
    return Registry({"m": ModelInfo("m", "M", "chat", "fast", True, "", "off",
                                    gpu_gen_watts=140.0, seed_tg_tok_s=40.0)}, "t")


@pytest.fixture
def meter(tmp_db_path):
    db = Database(tmp_db_path)
    db._conn.execute(
        "INSERT INTO users (id, oidc_sub, email, created_at) VALUES (1, 's1', 'a@x', 0)")
    return Meter(db, _settings(), _registry())


async def _add_job(db: Database, *, user_id=1, credits=0.0, finished_at=None,
                   state="done", usage_estimated=0, cost_usd=0.0):
    await db.execute(
        "INSERT INTO jobs (id, user_id, model_id, state, queued_at, finished_at, "
        "credits, cost_usd, usage_estimated) VALUES (?,?,?,?,?,?,?,?,?)",
        (uuid.uuid4().hex, user_id, "m", state, (finished_at or time.time()) - 10,
         finished_at, credits, cost_usd, usage_estimated))


# --------------------------------------------------------------------------- #
# credit formula
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("state,lane,occ,load,expected", [
    ("done", "exclusive", 100.0, 20.0, 90.0),      # 80 active + 20*0.5
    ("done", "exclusive", 50.0, 0.0, 50.0),        # warm: full price
    ("cancelled", "exclusive", 30.0, 0.0, 30.0),   # seconds actually consumed
    ("cancelled", "exclusive", 10.0, 3.0, 8.5),    # partial, with a bit of load
    ("error", "exclusive", 25.0, 25.0, 0.0),       # failed load -> free
    ("limit_exceeded", "exclusive", 0.0, 0.0, 0.0),
    ("done", "fast", 100.0, 0.0, 0.0),             # fast lane not full price
    ("done", "exclusive", 100.0, 500.0, 50.0),     # load clamped to occupancy
])
def test_credits_formula(meter, state, lane, occ, load, expected):
    got = meter.credits_for_job(state=state, lane=lane,
                                occupancy_seconds=occ, load_seconds=load)
    assert got == pytest.approx(expected)


def test_credits_never_estimated_only_from_timers(meter):
    # occupancy is a wall-clock measurement; there is no token-based path in
    # credits_for_job at all
    a = meter.credits_for_job(state="done", lane="exclusive",
                              occupancy_seconds=42.0, load_seconds=0.0)
    assert a == 42.0


# --------------------------------------------------------------------------- #
# cost model
# --------------------------------------------------------------------------- #
def test_cost_uses_measured_gpu_watts_plus_constant(meter):
    # 3600 credit-seconds at (140 + 110) W, flat rate 0.456
    cost, rate, watts = meter.cost_for_job(
        credits=3600.0, gpu_watts=140.0, model_id="m", ran_at=0)
    assert watts == 250.0
    assert rate == 0.456
    assert cost == pytest.approx(3600 * 250 / 3.6e6 * 0.456)  # ~0.114


def test_cost_falls_back_to_registry_watts_when_unmeasured(meter):
    cost, rate, watts = meter.cost_for_job(
        credits=100.0, gpu_watts=None, model_id="m", ran_at=0)
    assert watts == 250.0   # 140 (registry gpu_gen_watts) + 110


def test_cost_honours_tou_schedule_by_hour(tmp_db_path):
    db = Database(tmp_db_path)
    s = _settings(tou_schedule='{"3": 0.20, "17": 0.85}')
    m = Meter(db, s, _registry())
    # 3am local -> super-off-peak
    t_3am = time.mktime(time.struct_time((2026, 7, 1, 3, 0, 0, 0, 0, -1)))
    _, rate_night, _ = m.cost_for_job(credits=10, gpu_watts=100, model_id="m", ran_at=t_3am)
    t_5pm = time.mktime(time.struct_time((2026, 7, 1, 17, 0, 0, 0, 0, -1)))
    _, rate_peak, _ = m.cost_for_job(credits=10, gpu_watts=100, model_id="m", ran_at=t_5pm)
    assert rate_night == 0.20
    assert rate_peak == 0.85


# --------------------------------------------------------------------------- #
# session window
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_session_opens_on_first_request_and_is_fixed_5h(meter):
    d = await meter.check_limits(1, "m")
    assert d.allowed and d.session_id
    s = await meter.db.fetch_one("SELECT * FROM sessions WHERE id = ?", (d.session_id,))
    assert s["expires_at"] - s["started_at"] == pytest.approx(5 * 3600)


@pytest.mark.asyncio
async def test_session_does_not_extend_on_activity(meter):
    d1 = await meter.check_limits(1, "m")
    s1 = await meter.db.fetch_one("SELECT expires_at FROM sessions WHERE id=?", (d1.session_id,))
    d2 = await meter.check_limits(1, "m")
    s2 = await meter.db.fetch_one("SELECT expires_at FROM sessions WHERE id=?", (d2.session_id,))
    assert d2.session_id == d1.session_id
    assert s2["expires_at"] == s1["expires_at"]


@pytest.mark.asyncio
async def test_expired_session_opens_a_fresh_one(meter):
    now = time.time()
    old = await meter.db.insert(
        "INSERT INTO sessions (user_id, started_at, expires_at, credits_used) "
        "VALUES (1, ?, ?, 500)", (now - 6 * 3600, now - 3600))
    d = await meter.check_limits(1, "m")
    assert d.session_id != old
    fresh = await meter.db.fetch_one("SELECT credits_used FROM sessions WHERE id=?", (d.session_id,))
    assert fresh["credits_used"] == 0


# --------------------------------------------------------------------------- #
# session cap + overshoot
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_session_admits_just_under_cap_then_denies_at_cap(meter):
    now = time.time()
    sid = await meter.db.insert(
        "INSERT INTO sessions (user_id, started_at, expires_at, credits_used) "
        "VALUES (1, ?, ?, 3599.9)", (now - 60, now + 3600))
    d = await meter.check_limits(1, "m")
    assert d.allowed is True and d.session_id == sid

    await meter.db.execute("UPDATE sessions SET credits_used = 3600 WHERE id = ?", (sid,))
    d2 = await meter.check_limits(1, "m")
    assert d2.allowed is False
    assert d2.limit == "session"
    assert d2.reset_at == pytest.approx(now + 3600)


@pytest.mark.asyncio
async def test_overshoot_recorded_not_truncated(meter):
    """A job admitted under the cap is billed in full even if it ends over."""
    now = time.time()
    sid = await meter.db.insert(
        "INSERT INTO sessions (user_id, started_at, expires_at, credits_used) "
        "VALUES (1, ?, ?, 3500)", (now - 60, now + 3600))

    class J:
        state = type("S", (), {"value": "done"})()
        lane = "exclusive"
        occupancy_seconds = 400.0
        load_seconds = 0.0
        gen_started_at = now
        finished_at = now
        gpu_watts_mean = 140.0
        model_id = "m"
        session_id = sid
        id = "j1"
        cold_start = False
        credits = cost_usd = rate_used = 0

    j = J()
    await meter.finalize_billing(j)
    assert j.credits == 400.0
    s = await meter.db.fetch_one("SELECT credits_used FROM sessions WHERE id=?", (sid,))
    assert s["credits_used"] == pytest.approx(3900.0)   # 3500 + 400, over the 3600 cap


# --------------------------------------------------------------------------- #
# rolling weekly
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_weekly_window_is_rolling_not_calendar(meter):
    now = time.time()
    await _add_job(meter.db, credits=1000, finished_at=now - (WEEK_SECONDS - 60))   # in
    await _add_job(meter.db, credits=1000, finished_at=now - (WEEK_SECONDS + 60))   # out
    used = await meter.weekly_credits(1, now)
    assert used == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_weekly_denies_at_cap_with_correct_reset(meter):
    now = time.time()
    t0 = now - 6 * DAY
    t1 = now - 5 * DAY
    t2 = now - 4 * DAY
    for t in (t0, t1, t2):
        await _add_job(meter.db, credits=5000, finished_at=t)   # 15000 total, cap 12000
    d = await meter.check_limits(1, "m")
    assert d.allowed is False and d.limit == "weekly"
    # removing the oldest (t0, 5000) drops total to 10000 < 12000
    assert d.reset_at == pytest.approx(t0 + WEEK_SECONDS, abs=1)


@pytest.mark.asyncio
async def test_weekly_just_under_cap_is_allowed(meter):
    now = time.time()
    await _add_job(meter.db, credits=11999, finished_at=now - DAY)
    d = await meter.check_limits(1, "m")
    assert d.allowed is True


# --------------------------------------------------------------------------- #
# per-user overrides
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_per_user_limit_override(meter):
    await meter.db.execute(
        "UPDATE users SET session_credit_limit_override = 1000, "
        "weekly_credit_limit_override = 2000 WHERE id = 1")
    s_cap, w_cap = await meter.effective_limits(1)
    assert (s_cap, w_cap) == (1000.0, 2000.0)


@pytest.mark.asyncio
async def test_uncapped_user_is_never_blocked(meter):
    now = time.time()
    # way over both caps: full session + double the weekly
    await meter.db.insert(
        "INSERT INTO sessions (user_id, started_at, expires_at, credits_used) "
        "VALUES (1, ?, ?, ?)", (now - 60, now + 3600, 9999))
    await _add_job(meter.db, credits=25000, finished_at=now - DAY)
    assert (await meter.check_limits(1, "m")).allowed is False   # normal user: blocked

    await meter.db.execute("UPDATE users SET uncapped = 1 WHERE id = 1")
    d = await meter.check_limits(1, "m")
    assert d.allowed is True and d.session_id is not None

    v = (await meter.usage_view(1)).as_dict()
    assert v["uncapped"] is True
    assert v["warn"] is False                    # no nagging when uncapped
    assert v["session"]["pct"] > 100 and v["weekly"]["pct"] > 100


# --------------------------------------------------------------------------- #
# estimated-fraction guard
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_usage_view_flags_warn_at_75pct(meter):
    now = time.time()
    await meter.db.insert(
        "INSERT INTO sessions (user_id, started_at, expires_at, credits_used) "
        "VALUES (1, ?, ?, ?)", (now - 60, now + 3600, 2700))  # 75% of 3600
    v = await meter.usage_view(1)
    assert v.as_dict()["warn"] is True
