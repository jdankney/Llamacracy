"""FastAPI application. Binds 127.0.0.1 only; oauth2-proxy sits in front (see
deploy/). Serves the SPA and the JSON/SSE API.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .admin import router as admin_router
from .config import Settings, get_settings
from .db import Database, get_db, now
from .identity import Principal, get_principal
from .metering import Meter
from .queue import Job, JobState, QueueManager, get_queue, start_queue, stop_queue
from .registry import Registry, get_registry
from .search import get_search
from .uploads import UploadRejected
from .uploads import save as save_upload
from .uploads import validate as validate_upload
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
    qm = await start_queue()
    meter = Meter(get_db(), settings, get_registry())
    qm.check_limits = meter.check_limits
    qm.finalize_billing = meter.finalize_billing
    app.state.meter = meter
    log.info("llamacracy up on %s:%s (dev_mode=%s)",
             settings.app_bind_host, settings.app_bind_port, settings.is_dev)
    try:
        yield
    finally:
        await stop_queue()
        await up.aclose()
        await get_search().aclose()
        get_db().close()


app = FastAPI(title="Llamacracy", lifespan=lifespan)
app.include_router(admin_router)


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
            "upload_max_mb": settings.upload_max_mb,
        },
    }


def get_meter(request: Request) -> Meter:
    return request.app.state.meter


@app.get("/api/usage")
async def usage(principal: Principal = Depends(get_principal),
                meter: Meter = Depends(get_meter),
                db: Database = Depends(get_db)):
    view = await meter.usage_view(principal.user_id)
    per_model = await db.fetch_all(
        "SELECT model_id, COUNT(*) AS requests, "
        "  COALESCE(SUM(credits), 0) AS credits, "
        "  COALESCE(SUM(cost_usd), 0) AS cost_usd, "
        "  COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
        "  COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens "
        "FROM jobs WHERE user_id = ? AND finished_at IS NOT NULL "
        "GROUP BY model_id ORDER BY credits DESC",
        (principal.user_id,),
    )
    daily = await db.fetch_all(
        "SELECT CAST(finished_at / 86400 AS INT) * 86400 AS day, "
        "  COALESCE(SUM(credits), 0) AS credits, COALESCE(SUM(cost_usd), 0) AS cost_usd "
        "FROM jobs WHERE user_id = ? AND finished_at IS NOT NULL "
        "AND finished_at >= ? GROUP BY day ORDER BY day",
        (principal.user_id, now() - 30 * 86400),
    )
    est = await db.fetch_one(
        "SELECT COALESCE(SUM(credits),0) AS total, "
        "  COALESCE(SUM(CASE WHEN usage_estimated THEN credits ELSE 0 END),0) AS estimated "
        "FROM jobs WHERE user_id = ? AND finished_at IS NOT NULL "
        "AND finished_at >= ?",
        (principal.user_id, now() - 30 * 86400),
    )
    frac = (est["estimated"] / est["total"]) if est and est["total"] else 0.0
    return {
        **view.as_dict(),
        "per_model": [dict(r) for r in per_model],
        "daily": [dict(r) for r in daily],
        "estimated_fraction": round(frac, 4),
    }


@app.get("/api/models")
async def list_models(principal: Principal = Depends(get_principal),
                      reg: Registry = Depends(get_registry),
                      qm: QueueManager = Depends(get_queue)):
    loaded = qm._loaded_model
    out = []
    # All chat-kind models, including unlisted -vision variants -- the picker
    # only shows in_picker ones, but the app needs the rest resolvable (e.g.
    # to show a friendly name for whatever a vision turn actually dispatched to).
    for m in reg.all():
        if m.kind != "chat":
            continue
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
            "vision": bool(m.vision_key),
            "in_picker": m.in_picker,
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
        "usage_estimated, search_json, image_upload_id, created_at FROM messages "
        "WHERE conversation_id = ? ORDER BY id",
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
    # any attached images belong only to this conversation's messages -- clean
    # up their rows *and* the on-disk files so deleted chats don't leave orphans
    uploads = await db.fetch_all(
        "SELECT DISTINCT u.id, u.path FROM uploads u "
        "JOIN messages m ON m.image_upload_id = u.id WHERE m.conversation_id = ?",
        (conv_id,),
    )
    await db.transaction([
        ("DELETE FROM messages WHERE conversation_id = ?", (conv_id,)),
        ("UPDATE jobs SET conversation_id = NULL WHERE conversation_id = ?", (conv_id,)),
        ("DELETE FROM conversations WHERE id = ?", (conv_id,)),
        *[("DELETE FROM uploads WHERE id = ?", (u["id"],)) for u in uploads],
    ])
    for u in uploads:
        try:
            Path(u["path"]).unlink(missing_ok=True)
        except OSError:
            log.warning("failed to remove upload file %s", u["path"])
    return {"deleted": conv_id}


# --------------------------------------------------------------------------- #
# vision uploads (Phase 8.4) -- on disk, scoped to the uploader
# --------------------------------------------------------------------------- #
@app.post("/api/uploads")
async def upload_image(file: UploadFile = File(...),
                       principal: Principal = Depends(get_principal),
                       settings: Settings = Depends(get_settings),
                       db: Database = Depends(get_db)):
    data = await file.read()
    try:
        ext = validate_upload(file.content_type, len(data), settings.upload_max_mb)
    except UploadRejected as e:
        raise HTTPException(400, str(e)) from e
    file_id, path = save_upload(settings.upload_dir, data, ext)
    await db.execute(
        "INSERT INTO uploads (id, user_id, path, mime, bytes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (file_id, principal.user_id, path, file.content_type, len(data), now()),
    )
    return {"id": file_id, "mime": file.content_type, "bytes": len(data)}


@app.get("/api/uploads/{upload_id}")
async def get_upload(upload_id: str,
                     principal: Principal = Depends(get_principal),
                     db: Database = Depends(get_db)):
    row = await db.fetch_one(
        "SELECT path, mime FROM uploads WHERE id = ? AND user_id = ?",
        (upload_id, principal.user_id),
    )
    if row is None:
        raise HTTPException(404, "no such upload")
    return FileResponse(row["path"], media_type=row["mime"])


# --------------------------------------------------------------------------- #
# chat
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    model: str
    message: str = Field(min_length=1)
    conversation_id: str | None = None
    max_tokens: int | None = None
    search: bool = False
    image_id: str | None = None


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

    # An attached image routes this turn to the paired -vision llama-swap
    # entry (same weights + --mmproj) instead of the picked text model. The
    # extra VRAM/cold-load only happens on turns that actually carry an
    # image; every other turn behaves exactly as before.
    dispatch_model_id = req.model
    image_row = None
    if req.image_id:
        image_row = await db.fetch_one(
            "SELECT * FROM uploads WHERE id = ? AND user_id = ?",
            (req.image_id, principal.user_id),
        )
        if image_row is None:
            raise HTTPException(404, "no such upload")
        vision = reg.vision_variant(req.model)
        if vision is None:
            raise HTTPException(400, f"{model.display} doesn't support images yet")
        dispatch_model_id = vision.key

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

    # Search runs here, outside the FIFO queue -- it's a local HTTP call, not
    # GPU time. One query, injected into this turn's prompt only; the user's
    # own message is persisted clean (search_json carries the citations).
    search_outcome = None
    if req.search:
        search_outcome = await get_search().search(req.message, settings.search_max_results)

    await db.execute(
        "INSERT INTO messages (conversation_id, role, content, search_json, "
        "  image_upload_id, created_at) "
        "VALUES (?, 'user', ?, ?, ?, ?)",
        (conv_id, req.message,
         json.dumps(search_outcome.as_dict()) if search_outcome else None,
         req.image_id, ts),
    )

    prompt_content = req.message
    if search_outcome and search_outcome.ok and search_outcome.results:
        prompt_content = search_outcome.to_prompt_block() + req.message

    # The image is sent for THIS turn only -- past turns' images aren't
    # resent on every follow-up (that would re-pay their full prompt-eval
    # cost, in GPU seconds, every single message). The model still has the
    # text of what it said about them; it just can't re-look.
    if image_row is not None:
        b64 = base64.b64encode(Path(image_row["path"]).read_bytes()).decode("ascii")
        user_content = [
            {"type": "image_url", "image_url": {"url": f"data:{image_row['mime']};base64,{b64}"}},
            {"type": "text", "text": prompt_content},
        ]
    else:
        user_content = prompt_content
    messages = history + [{"role": "user", "content": user_content}]

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
        model_id=dispatch_model_id,
        payload=payload,
        conversation_id=conv_id,
    )

    # Limits are checked at enqueue (rejecting after a queue wait is hostile).
    # Opens the user's session if they have none. Overshoot is allowed: an
    # admitted job runs to completion even if it finishes over the cap.
    if qm.check_limits is not None:
        decision = await qm.check_limits(principal.user_id, dispatch_model_id)
        job.session_id = decision.session_id
        if not decision.allowed:
            job.state = JobState.LIMIT_EXCEEDED
            job.finished_at = now()
            await qm._persist_job(job)
            raise HTTPException(429, detail=decision.as_dict())

    await qm.submit(job)

    async def event_stream():
        yield _sse({"type": "accepted", "job_id": job.id,
                    "conversation_id": conv_id,
                    "position": qm.position_of(job.id)})
        if search_outcome is not None:
            yield _sse(search_outcome.as_dict())
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
