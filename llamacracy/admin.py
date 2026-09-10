"""Admin dashboard API. Every route is behind require_admin, which checks the
OIDC email against ADMIN_EMAILS in config -- never a database flag.
"""

from __future__ import annotations

import csv
import io
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .db import Database, get_db, now
from .identity import Principal, require_admin
from .metering import Meter
from .queue import QueueManager, get_queue
from .registry import Registry, get_registry

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])


def _meter(request: Request) -> Meter:
    return request.app.state.meter


# --------------------------------------------------------------------------- #
# Live
# --------------------------------------------------------------------------- #
@router.get("/live")
async def live(qm: QueueManager = Depends(get_queue),
               db: Database = Depends(get_db),
               reg: Registry = Depends(get_registry)):
    gpu = await _gpu_stats()
    active_sessions = await db.fetch_all(
        "SELECT s.id, u.email, u.display_name, s.started_at, s.expires_at, s.credits_used "
        "FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.expires_at > ? ORDER BY s.started_at DESC",
        (now(),),
    )
    return {
        "queue": qm.snapshot(),
        "loaded_model": qm._loaded_model,
        "load_estimates": {m.key: qm.load_estimate(m.key) for m in reg.all()},
        "gpu": gpu,
        "active_sessions": [dict(r) for r in active_sessions],
    }


async def _gpu_stats() -> dict:
    import asyncio
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total,temperature.gpu,power.draw,utilization.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        used, total, temp, power, util = (x.strip() for x in out.decode().split(","))
        return {"vram_used_mib": int(float(used)), "vram_total_mib": int(float(total)),
                "temp_c": float(temp), "power_w": float(power), "util_pct": float(util),
                "available": True}
    except Exception:  # noqa: BLE001
        return {"available": False}


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
@router.get("/users")
async def users(db: Database = Depends(get_db), meter: Meter = Depends(_meter)):
    rows = await db.fetch_all("SELECT * FROM users ORDER BY id")
    at = now()
    out = []
    for u in rows:
        s_cap, w_cap = await meter.effective_limits(u["id"])
        sess = await meter.active_session(u["id"], at)
        s_used = float(sess["credits_used"]) if sess else 0.0
        w_used = await meter.weekly_credits(u["id"], at)
        totals = await db.fetch_one(
            "SELECT COUNT(*) AS jobs, COALESCE(SUM(completion_tokens),0) AS out_tokens, "
            "COALESCE(SUM(prompt_tokens),0) AS in_tokens, COALESCE(SUM(credits),0) AS credits, "
            "COALESCE(SUM(cost_usd),0) AS cost FROM jobs WHERE user_id = ? AND finished_at IS NOT NULL",
            (u["id"],),
        )
        out.append({
            "id": u["id"], "email": u["email"], "display_name": u["display_name"],
            "is_admin": bool(u["is_admin"]), "disabled": bool(u["disabled"]),
            "uncapped": bool(u["uncapped"]),
            "last_active_at": u["last_active_at"],
            "session": {"used": round(s_used, 1), "cap": s_cap,
                        "pct": round(100 * s_used / s_cap, 1) if s_cap else 0},
            "weekly": {"used": round(w_used, 1), "cap": w_cap,
                       "pct": round(100 * w_used / w_cap, 1) if w_cap else 0},
            "session_override": u["session_credit_limit_override"],
            "weekly_override": u["weekly_credit_limit_override"],
            "all_time": {"jobs": totals["jobs"], "tokens": totals["out_tokens"] + totals["in_tokens"],
                         "credits": round(totals["credits"], 1), "cost_usd": round(totals["cost"], 4)},
        })
    return {"users": out}


# --------------------------------------------------------------------------- #
# Usage (time series + per-model performance)
# --------------------------------------------------------------------------- #
@router.get("/usage")
async def usage(days: int = 30, db: Database = Depends(get_db)):
    since = now() - days * 86400
    by_day_user = await db.fetch_all(
        "SELECT CAST(finished_at/86400 AS INT)*86400 AS day, u.email, "
        "  COALESCE(SUM(j.credits),0) AS credits "
        "FROM jobs j JOIN users u ON u.id = j.user_id "
        "WHERE j.finished_at >= ? GROUP BY day, u.email ORDER BY day",
        (since,),
    )
    by_model = await db.fetch_all(
        "SELECT model_id, COUNT(*) AS requests, "
        "  COALESCE(SUM(occupancy_seconds),0) AS occupancy, "
        "  COALESCE(SUM(credits),0) AS credits, COALESCE(SUM(cost_usd),0) AS cost, "
        "  COALESCE(AVG(completion_tokens),0) AS mean_out_tokens, "
        "  COALESCE(SUM(completion_tokens),0) AS out_tokens, "
        "  COALESCE(SUM(gen_seconds),0) AS gen_seconds, "
        "  COALESCE(AVG(cold_start),0) AS cold_rate "
        "FROM jobs WHERE finished_at >= ? AND state IN ('done','cancelled') "
        "GROUP BY model_id ORDER BY credits DESC",
        (since,),
    )
    models = []
    for r in by_model:
        gs = r["gen_seconds"] or 0
        models.append({
            "model_id": r["model_id"], "requests": r["requests"],
            "occupancy_seconds": round(r["occupancy"], 1),
            "credits": round(r["credits"], 1), "cost_usd": round(r["cost"], 4),
            "mean_tokens": round(r["mean_out_tokens"], 1),
            "mean_tok_s": round(r["out_tokens"] / gs, 1) if gs else None,
            "cold_rate": round(r["cold_rate"], 2),
        })
    return {"by_day_user": [dict(r) for r in by_day_user], "by_model": models}


# --------------------------------------------------------------------------- #
# Queue impact -- seconds of wait each user inflicted on OTHER users.
# Under strict FIFO this is the real fairness metric.
# --------------------------------------------------------------------------- #
@router.get("/queue-impact")
async def queue_impact(days: int = 30, db: Database = Depends(get_db)):
    since = now() - days * 86400
    rows = await db.fetch_all(
        """
        SELECT j.user_id, u.email, u.display_name,
               SUM(MAX(0, MIN(j.finished_at, k.picked_at) - MAX(j.picked_at, k.queued_at))) AS wait_inflicted,
               COUNT(DISTINCT k.id) AS jobs_delayed
        FROM jobs j
        JOIN jobs k ON k.user_id != j.user_id
          AND k.queued_at < j.finished_at
          AND k.picked_at  > j.picked_at
        JOIN users u ON u.id = j.user_id
        WHERE j.picked_at IS NOT NULL AND j.finished_at IS NOT NULL
          AND k.picked_at IS NOT NULL AND j.finished_at >= ?
        GROUP BY j.user_id
        ORDER BY wait_inflicted DESC
        """,
        (since,),
    )
    return {"impact": [
        {"email": r["email"], "display_name": r["display_name"],
         "wait_inflicted_s": round(r["wait_inflicted"] or 0, 1),
         "jobs_delayed": r["jobs_delayed"]}
        for r in rows
    ]}


# --------------------------------------------------------------------------- #
# Billing
# --------------------------------------------------------------------------- #
class InvoicePeriod(BaseModel):
    period_start: float
    period_end: float


@router.get("/billing")
async def billing(period_days: int = 30, db: Database = Depends(get_db)):
    end = now()
    start = end - period_days * 86400
    per_user = await db.fetch_all(
        "SELECT u.id AS user_id, u.email, u.display_name, "
        "  COALESCE(SUM(j.credits),0) AS credits, COALESCE(SUM(j.cost_usd),0) AS cost, "
        "  COUNT(j.id) AS jobs "
        "FROM users u LEFT JOIN jobs j ON j.user_id = u.id "
        "  AND j.finished_at >= ? AND j.finished_at < ? "
        "GROUP BY u.id ORDER BY cost DESC",
        (start, end),
    )
    invoices = await db.fetch_all(
        "SELECT i.*, u.email FROM invoices i JOIN users u ON u.id = i.user_id "
        "ORDER BY i.created_at DESC LIMIT 100")
    return {
        "period": {"start": start, "end": end},
        "per_user": [dict(r) for r in per_user],
        "invoices": [dict(r) for r in invoices],
    }


@router.post("/billing/invoice")
async def create_invoice(body: dict, db: Database = Depends(get_db)):
    user_id = int(body["user_id"])
    start = float(body["period_start"])
    end = float(body["period_end"])
    row = await db.fetch_one(
        "SELECT COALESCE(SUM(credits),0) AS c, COALESCE(SUM(cost_usd),0) AS cost "
        "FROM jobs WHERE user_id = ? AND finished_at >= ? AND finished_at < ?",
        (user_id, start, end),
    )
    inv_id = await db.insert(
        "INSERT INTO invoices (user_id, period_start, period_end, total_credits, "
        "  total_cost_usd, status, created_at) VALUES (?,?,?,?,?, 'draft', ?)",
        (user_id, start, end, round(row["c"], 2), round(row["cost"], 4), now()),
    )
    return {"invoice_id": inv_id}


@router.post("/billing/invoice/{inv_id}/status")
async def set_invoice_status(inv_id: int, body: dict, db: Database = Depends(get_db)):
    status = body.get("status")
    if status not in ("draft", "sent", "paid"):
        raise HTTPException(400, "bad status")
    await db.execute("UPDATE invoices SET status = ? WHERE id = ?", (status, inv_id))
    return {"ok": True}


@router.get("/billing/export.csv")
async def export_csv(period_days: int = 30, db: Database = Depends(get_db)):
    end = now()
    start = end - period_days * 86400
    rows = await db.fetch_all(
        "SELECT u.email, j.model_id, j.state, j.finished_at, j.occupancy_seconds, "
        "  j.credits, j.cost_usd, j.rate_used, j.prompt_tokens, j.completion_tokens, "
        "  j.usage_estimated, j.cold_start "
        "FROM jobs j JOIN users u ON u.id = j.user_id "
        "WHERE j.finished_at >= ? AND j.finished_at < ? ORDER BY j.finished_at",
        (start, end),
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["email", "model", "state", "finished_utc", "occupancy_s", "credits",
                "cost_usd", "rate_used", "prompt_tokens", "completion_tokens",
                "usage_estimated", "cold_start"])
    for r in rows:
        w.writerow([r["email"], r["model_id"], r["state"],
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r["finished_at"])),
                    round(r["occupancy_seconds"], 2), round(r["credits"], 3),
                    round(r["cost_usd"], 5), r["rate_used"], r["prompt_tokens"],
                    r["completion_tokens"], r["usage_estimated"], r["cold_start"]])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=llamacracy-usage.csv"})


# --------------------------------------------------------------------------- #
# Controls
# --------------------------------------------------------------------------- #
@router.post("/users/{user_id}/limits")
async def set_limits(user_id: int, body: dict, db: Database = Depends(get_db)):
    s = body.get("session_override")
    w = body.get("weekly_override")
    await db.execute(
        "UPDATE users SET session_credit_limit_override = ?, "
        "weekly_credit_limit_override = ? WHERE id = ?",
        (s if s not in ("", None) else None, w if w not in ("", None) else None, user_id),
    )
    return {"ok": True}


@router.post("/users/{user_id}/disabled")
async def set_disabled(user_id: int, body: dict,
                       principal: Principal = Depends(require_admin),
                       db: Database = Depends(get_db)):
    if user_id == principal.user_id:
        raise HTTPException(400, "cannot disable yourself")
    await db.execute("UPDATE users SET disabled = ? WHERE id = ?",
                     (1 if body.get("disabled") else 0, user_id))
    return {"ok": True}


@router.post("/users/{user_id}/uncapped")
async def set_uncapped(user_id: int, body: dict, db: Database = Depends(get_db)):
    """Uncapped users are never blocked at enqueue. Their session/weekly % is
    still computed and shown (and can sail past 100%)."""
    await db.execute("UPDATE users SET uncapped = ? WHERE id = ?",
                     (1 if body.get("uncapped") else 0, user_id))
    return {"ok": True}


@router.post("/unload")
async def force_unload(qm: QueueManager = Depends(get_queue)):
    ok = await qm.up.unload_all()
    if ok:
        qm._loaded_model = None
        await qm._broadcast()
    return {"ok": ok}


@router.post("/jobs/{job_id}/kill")
async def kill_job(job_id: str, qm: QueueManager = Depends(get_queue)):
    job = qm._by_id.get(job_id)
    if job is None:
        raise HTTPException(404, "job not active")
    if job is qm._active:
        job._cancel.set()
    elif job in qm._pending:
        qm._pending.remove(job)
        from .queue import JobState
        job.state = JobState.CANCELLED
        job.finished_at = now()
        await job._chunks.put(None)
        await qm._persist_job(job)
        await qm._broadcast()
    return {"ok": True}
