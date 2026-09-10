"""Metering -- the part where a bug means someone gets billed wrong.

  1 credit = 1 second of exclusive box time.

Credits are always MEASURED from our own timers, never estimated. Only tokens
may be estimated (and such rows are flagged).

  credits (exclusive lane) = occupancy_seconds - load_seconds * (1 - LOAD_TIME_MULTIPLIER)
                           = active_seconds + load_seconds * LOAD_TIME_MULTIPLIER

Charging rules:
  - generation time      -> always charged to the requester (full rate)
  - model load time      -> charged to whoever triggered it, at LOAD_TIME_MULTIPLIER
  - failed load / error  -> 0 (not the user's fault)
  - cancelled            -> charged for the seconds actually consumed
  - queue waiting time   -> never charged (not in occupancy_seconds)
  - fast lane            -> not full price (FAST_LANE_CREDIT_RATE, default 0);
                            fast lane isn't implemented yet, everything is exclusive.

Limits are checked at ENQUEUE (rejecting after a queue wait is hostile), and
overshoot is allowed: a job admitted under the cap runs to completion even if
it finishes over. Overshoot is bounded by MAX_TOKENS_PER_REQUEST.

  cost_usd = credits * watts * rate * MARKUP   (per the spec's formula)
           = credits[s] * watts[W] / 3.6e6 * rate[$/kWh] * markup

Historical cost is never recomputed: rate_used and cost_usd are frozen on the
job row at finalisation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .config import Settings
from .db import Database, now
from .registry import Registry

log = logging.getLogger("llamacracy.metering")

LANE_CREDIT_RATE = {"exclusive": 1.0, "fast": 0.0}
WEEK_SECONDS = 7 * 24 * 3600


@dataclass
class LimitDecision:
    allowed: bool
    session_id: int | None = None
    limit: str | None = None            # "session" | "weekly"
    used: float = 0.0
    cap: float = 0.0
    reset_at: float | None = None       # UTC epoch

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "limit": self.limit,
            "used": round(self.used, 1),
            "cap": round(self.cap, 1),
            "reset_at": self.reset_at,
        }


@dataclass
class UsageView:
    session_used: float
    session_cap: float
    session_reset_at: float | None
    weekly_used: float
    weekly_cap: float
    weekly_reset_at: float | None
    uncapped: bool = False              # limits are informational only for this user

    def _pct(self, used: float, cap: float) -> float:
        return round(100 * used / cap, 1) if cap else 0.0

    def as_dict(self) -> dict:
        return {
            "session": {"used": round(self.session_used, 1), "cap": self.session_cap,
                        "pct": self._pct(self.session_used, self.session_cap),
                        "reset_at": self.session_reset_at, "uncapped": self.uncapped},
            "weekly": {"used": round(self.weekly_used, 1), "cap": self.weekly_cap,
                       "pct": self._pct(self.weekly_used, self.weekly_cap),
                       "reset_at": self.weekly_reset_at, "uncapped": self.uncapped},
            "uncapped": self.uncapped,
            "warn": (not self.uncapped) and max(
                self._pct(self.session_used, self.session_cap),
                self._pct(self.weekly_used, self.weekly_cap)) >= 75,
        }


class Meter:
    def __init__(self, db: Database, settings: Settings, registry: Registry):
        self.db = db
        self.cfg = settings
        self.reg = registry

    # -- credit computation -------------------------------------------------
    def credits_for_job(self, *, state: str, lane: str, occupancy_seconds: float,
                        load_seconds: float) -> float:
        if state in ("error", "limit_exceeded"):
            return 0.0
        lane_rate = LANE_CREDIT_RATE.get(lane, 1.0)
        if lane_rate == 0.0:
            return 0.0
        load_seconds = max(0.0, min(load_seconds, occupancy_seconds))
        active = occupancy_seconds - load_seconds
        return lane_rate * (active + load_seconds * self.cfg.load_time_multiplier)

    def cost_for_job(self, *, credits: float, gpu_watts: float | None,
                     model_id: str, ran_at: float | None) -> tuple[float, float, float]:
        """Returns (cost_usd, rate_used, watts). Never call this to recompute an
        already-billed job -- rate_used is frozen on the row."""
        if gpu_watts is None:
            info = self.reg.get(model_id)
            gpu_watts = (info.gpu_gen_watts if info and info.gpu_gen_watts
                         else 180.0)  # blended fallback if nvidia-smi was unavailable
        watts = gpu_watts + self.cfg.non_gpu_load_watts
        hour = time.localtime(ran_at or now()).tm_hour
        rate = self.cfg.rate_for_hour(hour)
        cost = credits * watts / 3.6e6 * rate * self.cfg.markup
        return round(cost, 6), rate, round(watts, 1)

    # -- limits -----------------------------------------------------------
    async def effective_limits(self, user_id: int) -> tuple[float, float]:
        row = await self.db.fetch_one(
            "SELECT session_credit_limit_override, weekly_credit_limit_override "
            "FROM users WHERE id = ?", (user_id,))
        s = (row["session_credit_limit_override"] if row and
             row["session_credit_limit_override"] is not None
             else self.cfg.session_credit_limit)
        w = (row["weekly_credit_limit_override"] if row and
             row["weekly_credit_limit_override"] is not None
             else self.cfg.weekly_credit_limit)
        return float(s), float(w)

    async def is_uncapped(self, user_id: int) -> bool:
        row = await self.db.fetch_one("SELECT uncapped FROM users WHERE id = ?", (user_id,))
        return bool(row["uncapped"]) if row else False

    async def active_session(self, user_id: int, at: float):
        return await self.db.fetch_one(
            "SELECT * FROM sessions WHERE user_id = ? AND started_at <= ? "
            "AND expires_at > ? ORDER BY started_at DESC LIMIT 1",
            (user_id, at, at))

    async def get_or_open_session(self, user_id: int, at: float):
        s = await self.active_session(user_id, at)
        if s is not None:
            return s
        sid = await self.db.insert(
            "INSERT INTO sessions (user_id, started_at, expires_at, credits_used) "
            "VALUES (?, ?, ?, 0)",
            (user_id, at, at + self.cfg.session_window_seconds))
        return await self.db.fetch_one("SELECT * FROM sessions WHERE id = ?", (sid,))

    async def weekly_credits(self, user_id: int, at: float) -> float:
        row = await self.db.fetch_one(
            "SELECT COALESCE(SUM(credits), 0) AS c FROM jobs "
            "WHERE user_id = ? AND finished_at IS NOT NULL AND finished_at >= ?",
            (user_id, at - WEEK_SECONDS))
        return float(row["c"] if row else 0.0)

    async def weekly_reset_at(self, user_id: int, cap: float, at: float) -> float:
        """The moment the rolling window first drops enough old credits to bring
        the user back under `cap` -- i.e. the Nth-oldest in-window job's
        finished_at + 7 days."""
        rows = await self.db.fetch_all(
            "SELECT finished_at, credits FROM jobs "
            "WHERE user_id = ? AND finished_at IS NOT NULL AND finished_at >= ? "
            "AND credits > 0 ORDER BY finished_at ASC",
            (user_id, at - WEEK_SECONDS))
        total = sum(r["credits"] for r in rows)
        removed = 0.0
        for r in rows:
            removed += r["credits"]
            if total - removed < cap:
                return r["finished_at"] + WEEK_SECONDS
        return at + WEEK_SECONDS

    async def check_limits(self, user_id: int, model_id: str) -> LimitDecision:
        """Enqueue-time gate. Opens a session on the user's first request.
        Rejects only if the user is ALREADY at/over a cap (overshoot allowed)."""
        at = now()
        session = await self.get_or_open_session(user_id, at)

        # uncapped users are never blocked -- their session still exists (and
        # their usage % still climbs), it just never gates.
        if await self.is_uncapped(user_id):
            return LimitDecision(True, session["id"])

        s_cap, w_cap = await self.effective_limits(user_id)

        s_used = float(session["credits_used"])
        if s_used >= s_cap:
            return LimitDecision(False, session["id"], "session", s_used, s_cap,
                                 reset_at=session["expires_at"])

        w_used = await self.weekly_credits(user_id, at)
        if w_used >= w_cap:
            return LimitDecision(False, session["id"], "weekly", w_used, w_cap,
                                 reset_at=await self.weekly_reset_at(user_id, w_cap, at))

        return LimitDecision(True, session["id"])

    # -- finalisation (called by the queue worker after a job ends) --------
    async def finalize_billing(self, job) -> None:
        job.credits = round(self.credits_for_job(
            state=job.state.value, lane=job.lane,
            occupancy_seconds=job.occupancy_seconds, load_seconds=job.load_seconds), 4)
        ran_at = job.gen_started_at or job.finished_at
        job.cost_usd, job.rate_used, watts = self.cost_for_job(
            credits=job.credits, gpu_watts=job.gpu_watts_mean,
            model_id=job.model_id, ran_at=ran_at)
        if job.gpu_watts_mean is None:
            job.gpu_watts_mean = round(watts - self.cfg.non_gpu_load_watts, 1)

        if job.session_id and job.credits > 0:
            await self.db.execute(
                "UPDATE sessions SET credits_used = credits_used + ? WHERE id = ?",
                (job.credits, job.session_id))
        log.info("job %s billed: %.1f credits, $%.4f (state=%s cold=%s)",
                 job.id, job.credits, job.cost_usd, job.state.value, job.cold_start)

    # -- usage view (for /api/usage and the dashboards) -------------------
    async def usage_view(self, user_id: int) -> UsageView:
        at = now()
        s_cap, w_cap = await self.effective_limits(user_id)
        uncapped = await self.is_uncapped(user_id)
        session = await self.active_session(user_id, at)
        s_used = float(session["credits_used"]) if session else 0.0
        s_reset = session["expires_at"] if session else None
        w_used = await self.weekly_credits(user_id, at)
        w_reset = (await self.weekly_reset_at(user_id, w_cap, at)
                   if not uncapped and w_used >= w_cap * 0.75 else None)
        return UsageView(s_used, s_cap, s_reset, w_used, w_cap, w_reset, uncapped)
