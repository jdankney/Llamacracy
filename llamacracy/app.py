"""FastAPI application. Binds 127.0.0.1 only; oauth2-proxy sits in front (see
deploy/). Serves the SPA and the JSON/SSE API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .db import Database, get_db, now
from .identity import Principal, get_principal
from .queue import Job, JobState, QueueManager, get_queue, start_queue, stop_queue
from .registry import Registry, get_registry
from .upstream import Upstream, UpstreamError, get_upstream

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("llamacracy")

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    get_db()  # applies schema
    if not settings.is_dev and not settings.admin_email_set:
        log.warning("ADMIN_EMAILS is empty -- nobody can reach the admin dashboard")
    up = get_upstream()
    if not await up.health():
        log.warning("llama-swap not reachable at %s -- will keep retrying", up.base_url)
    await start_queue()
    log.info("llamacracy up on %s:%s (dev_mode=%s)",
             settings.app_bind_host, settings.app_bind_port, settings.is_dev)
    try:
        yield
    finally:
        await stop_queue()
        await up.aclose()
        get_db().close()


app = FastAPI(title="Llamacracy", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# models / me
# --------------------------------------------------------------------------- #
@app.get("/healthz")
async def healthz(up: Upstream = Depends(get_upstream)):
    return {"ok": True, "upstream": await up.health()}


@app.get("/api/me")
async def me(principal: Principal = Depends(get_principal),
             settings: Settings = Depends(get_settings)):
    return {
        "email": principal.email,
        "display_name": principal.display_name,
        "is_admin": principal.is_admin,
        "limits": {
            "session_credit_limit": settings.session_credit_limit,
            "weekly_credit_limit": settings.weekly_credit_limit,
            "session_window_hours": settings.session_window_hours,
            "max_tokens_per_request": settings.max_tokens_per_request,
        },
    }


@app.get("/api/models")
async def list_models(principal: Principal = Depends(get_principal),
                      reg: Registry = Depends(get_registry),
                      qm: QueueManager = Depends(get_queue)):
    loaded = qm._loaded_model
    out = []
    for m in reg.chat_models():
        out.append({
            "id": m.key,
            "display": m.display,
            "tier": m.tier,
            "blurb": m.blurb,
            "ctx": m.ctx,
            "reasoning": m.reasoning,
            "tok_s": m.seed_tg_tok_s,
            "cold_load_s": qm.load_estimate(m.key),
            "resident": m.key == loaded,
            "max_tokens_default": min(m.max_tokens_default,
                                      get_settings().max_tokens_per_request),
        })
    return {"models": out, "loaded_model": loaded}


# --------------------------------------------------------------------------- #
# conversations
# --------------------------------------------------------------------------- #
class NewConversation(BaseModel):
    model: str
    title: str | None = None


@app.get("/api/conversations")
async def conversations(principal: Principal = Depends(get_principal),
                        db: Database = Depends(get_db)):
    rows = await db.fetch_all(
        "SELECT id, title, model_id, created_at, updated_at FROM conversations "
        "WHERE user_id = ? ORDER BY updated_at DESC LIMIT 200",
        (principal.user_id,),
    )
    return {"conversations": [dict(r) for r in rows]}


@app.get("/api/conversations/{conv_id}")
async def conversation_detail(conv_id: str,
                              principal: Principal = Depends(get_principal),
                              db: Database = Depends(get_db)):
    conv = await db.fetch_one(
        "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
        (conv_id, principal.user_id),
    )
    if conv is None:
        raise HTTPException(404, "no such conversation")
    msgs = await db.fetch_all(
        "SELECT role, content, model_id, prompt_tokens, completion_tokens, "
        "usage_estimated, created_at FROM messages WHERE conversation_id = ? ORDER BY id",
        (conv_id,),
    )
    return {"conversation": dict(conv), "messages": [dict(m) for m in msgs]}


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str,
                              principal: Principal = Depends(get_principal),
                              db: Database = Depends(get_db)):
    conv = await db.fetch_one(
        "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
        (conv_id, principal.user_id),
    )
    if conv is None:
        raise HTTPException(404, "no such conversation")
    await db.transaction([
        ("DELETE FROM messages WHERE conversation_id = ?", (conv_id,)),
        ("UPDATE jobs SET conversation_id = NULL WHERE conversation_id = ?", (conv_id,)),
        ("DELETE FROM conversations WHERE id = ?", (conv_id,)),
    ])
    return {"deleted": conv_id}


# --------------------------------------------------------------------------- #
# chat
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    model: str
    message: str = Field(min_length=1)
    conversation_id: str | None = None
    max_tokens: int | None = None


async def _load_history(db: Database, conv_id: str) -> list[dict]:
    rows = await db.fetch_all(
        "SELECT role, content FROM messages WHERE conversation_id = ? ORDER BY id",
        (conv_id,),
    )
    return [{"role": r["role"], "content": r["content"]} for r in rows]


@app.post("/api/chat")
async def chat(req: ChatRequest,
               request: Request,
               principal: Principal = Depends(get_principal),
               settings: Settings = Depends(get_settings),
               db: Database = Depends(get_db),
               reg: Registry = Depends(get_registry),
               qm: QueueManager = Depends(get_queue)):
    model = reg.get(req.model)
    if model is None or model.kind != "chat" or not model.in_picker:
        raise HTTPException(400, f"not a selectable chat model: {req.model}")

    ts = now()
    conv_id = req.conversation_id
    if conv_id:
        owned = await db.fetch_one(
            "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, principal.user_id),
        )
        if owned is None:
            raise HTTPException(404, "no such conversation")
    else:
        conv_id = uuid.uuid4().hex
        title = req.message.strip().replace("\n", " ")[:50]
        await db.execute(
            "INSERT INTO conversations (id, user_id, title, model_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conv_id, principal.user_id, title, req.model, ts, ts),
        )

    history = await _load_history(db, conv_id)
    await db.execute(
        "INSERT INTO messages (conversation_id, role, content, created_at) "
        "VALUES (?, 'user', ?, ?)",
        (conv_id, req.message, ts),
    )
    messages = history + [{"role": "user", "content": req.message}]

    cap = min(
        req.max_tokens or model.max_tokens_default,
        model.max_tokens_default,
        settings.max_tokens_per_request,
    )
    payload = {"messages": messages, "max_tokens": cap, **model.sampling}

    job = Job(
        user_id=principal.user_id,
        owner_name=principal.display_name,
        owner_email=principal.email,
        model_id=req.model,
        payload=payload,
        conversation_id=conv_id,
    )

    # Phase 3 hooks in here: qm.check_limits(...) before submit; on rejection
    # persist a limit_exceeded job and return the reset timestamp.
    if qm.check_limits is not None:
        decision = await qm.check_limits(principal.user_id, req.model)
        if decision is not None and not decision.allowed:
            job.state = JobState.LIMIT_EXCEEDED
            job.finished_at = now()
            await qm._persist_job(job)
            raise HTTPException(429, detail=decision.as_dict())

    await qm.submit(job)

    async def event_stream():
        yield _sse({"type": "accepted", "job_id": job.id,
                    "conversation_id": conv_id,
                    "position": qm.position_of(job.id)})
        try:
            while True:
                chunk = await job._chunks.get()
                if chunk is None:
                    break
                yield _sse(chunk)
        except asyncio.CancelledError:
            await qm.cancel(job.id, principal.user_id)
            raise
        if await request.is_disconnected():
            await qm.cancel(job.id, principal.user_id)

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str,
                     principal: Principal = Depends(get_principal),
                     qm: QueueManager = Depends(get_queue)):
    ok = await qm.cancel(job_id, principal.user_id)
    if not ok:
        raise HTTPException(404, "job not found, not yours, or already finished")
    return {"cancelled": job_id}


# --------------------------------------------------------------------------- #
# live queue (SSE)
# --------------------------------------------------------------------------- #
@app.get("/api/queue")
async def queue_now(principal: Principal = Depends(get_principal),
                    qm: QueueManager = Depends(get_queue)):
    return qm.snapshot()


@app.get("/api/queue/events")
async def queue_events(request: Request,
                       principal: Principal = Depends(get_principal),
                       qm: QueueManager = Depends(get_queue)):
    q = qm.subscribe()

    async def stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    snap = await asyncio.wait_for(q.get(), timeout=15)
                    yield _sse(snap)
                except TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            qm.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- #
# FIM / infill -- separate, clearly-labelled. Never a chat model.
# --------------------------------------------------------------------------- #
class InfillRequest(BaseModel):
    prefix: str
    suffix: str = ""
    n_predict: int = 64


@app.post("/api/infill")
async def infill(req: InfillRequest,
                 principal: Principal = Depends(get_principal),
                 reg: Registry = Depends(get_registry),
                 up: Upstream = Depends(get_upstream)):
    fim = reg.fim_model()
    if fim is None:
        raise HTTPException(503, "no FIM model configured")
    try:
        res = await up.infill({
            "model": fim.key,
            "input_prefix": req.prefix,
            "input_suffix": req.suffix,
            "n_predict": min(req.n_predict, 256),
        })
    except UpstreamError as e:
        raise HTTPException(502, str(e))
    return {"content": res.get("content", ""), "model": fim.display}


# --------------------------------------------------------------------------- #
# SPA
# --------------------------------------------------------------------------- #
def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets", check_dir=False),
              name="assets")

    @app.get("/")
    async def index():
        idx = STATIC_DIR / "index.html"
        if idx.exists():
            return FileResponse(idx)
        return {"service": "llamacracy", "ui": "not built yet (Phase 4)"}
