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
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .admin import router as admin_router
from .apikeys import generate as generate_api_key
from .config import Settings, get_settings
from .db import Database, get_db, now
from .identity import Principal, get_principal, get_principal_api_key
from .metering import Meter
from .queue import Job, JobState, QueueManager, get_queue, start_queue, stop_queue
from .registry import Registry, get_registry
from .search import get_search
from .uploads import UploadRejected
from .uploads import save as save_upload
from .uploads import validate as validate_upload
from .upstream import Upstream, get_upstream

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
            "compact_keep_recent": settings.compact_keep_recent,
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


# --------------------------------------------------------------------------- #
# API keys (Phase 8.2) -- self-service, for the OpenAI-compatible endpoint
# --------------------------------------------------------------------------- #
class CreateApiKey(BaseModel):
    label: str = ""


@app.get("/api/keys")
async def list_api_keys(principal: Principal = Depends(get_principal),
                        db: Database = Depends(get_db)):
    rows = await db.fetch_all(
        "SELECT id, label, created_at, last_used_at FROM api_keys "
        "WHERE user_id = ? ORDER BY id DESC",
        (principal.user_id,),
    )
    return {"keys": [dict(r) for r in rows]}


@app.post("/api/keys")
async def create_api_key(req: CreateApiKey,
                         principal: Principal = Depends(get_principal),
                         db: Database = Depends(get_db)):
    key, key_hash = generate_api_key()
    await db.execute(
        "INSERT INTO api_keys (user_id, key_hash, label, created_at) VALUES (?, ?, ?, ?)",
        (principal.user_id, key_hash, req.label.strip()[:80], now()),
    )
    # shown once -- only the hash is ever stored, so this is the one chance
    return {"key": key}


@app.delete("/api/keys/{key_id}")
async def revoke_api_key(key_id: int,
                         principal: Principal = Depends(get_principal),
                         db: Database = Depends(get_db)):
    row = await db.fetch_one(
        "SELECT id FROM api_keys WHERE id = ? AND user_id = ?",
        (key_id, principal.user_id),
    )
    if row is None:
        raise HTTPException(404, "no such key")
    await db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    return {"revoked": key_id}


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
# admission -- one gate for every door (web chat, compaction, /v1)
# --------------------------------------------------------------------------- #
async def _check_limits(job: Job, qm: QueueManager) -> None:
    """Enqueue-time limit check. Rejecting after a queue wait is hostile, so
    this runs before the job is submitted -- and before anything about the
    request is persisted, so a rejected send leaves no orphan rows. Opens the
    user's session if they have none. Overshoot is allowed: an admitted job
    runs to completion even if it finishes over the cap."""
    if qm.check_limits is None:
        return
    decision = await qm.check_limits(job.user_id, job.model_id)
    job.session_id = decision.session_id
    if not decision.allowed:
        job.state = JobState.LIMIT_EXCEEDED
        job.finished_at = now()
        await qm._persist_job(job)
        raise HTTPException(429, detail=decision.as_dict())


# --------------------------------------------------------------------------- #
# conversations
# --------------------------------------------------------------------------- #
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
        "SELECT id, role, content, model_id, prompt_tokens, completion_tokens, "
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
# compact context -- on demand, never automatic. Summarizes everything except
# the last COMPACT_KEEP_RECENT messages using the conversation's own model, so
# future turns send far less history. This is a real inference: it goes
# through the same FIFO queue and is billed the same as any other job -- not
# free like search. Nothing is deleted; _load_history (above) is what
# actually skips the folded-in messages when building a prompt.
# --------------------------------------------------------------------------- #
_COMPACT_SYSTEM_PROMPT = (
    "You summarize conversations concisely and factually. Preserve names, "
    "decisions, numbers, and code/config specifics needed to continue "
    "naturally. Write neutral third-person notes -- no markdown headers, no "
    "commentary about summarizing, no meta remarks. Plain prose or short "
    "bullet points only."
)


def _render_turns(msgs: list[dict]) -> str:
    who = {"user": "User", "assistant": "Assistant", "system": "Note"}
    return "\n".join(f"{who.get(m['role'], m['role'])}: {m['content']}" for m in msgs)


@app.post("/api/conversations/{conv_id}/compact")
async def compact_conversation(conv_id: str,
                               principal: Principal = Depends(get_principal),
                               settings: Settings = Depends(get_settings),
                               db: Database = Depends(get_db),
                               reg: Registry = Depends(get_registry),
                               qm: QueueManager = Depends(get_queue)):
    conv = await db.fetch_one(
        "SELECT * FROM conversations WHERE id = ? AND user_id = ?",
        (conv_id, principal.user_id),
    )
    if conv is None:
        raise HTTPException(404, "no such conversation")

    boundary = conv["compact_boundary_id"] or 0
    rows = await db.fetch_all(
        "SELECT id, role, content FROM messages "
        "WHERE conversation_id = ? AND id > ? ORDER BY id",
        (conv_id, boundary),
    )
    keep = settings.compact_keep_recent
    to_compact = rows[:-keep] if keep else list(rows)
    if len(to_compact) < 2:
        raise HTTPException(
            400, "not enough new conversation since last time to be worth compacting "
                f"(keeps the last {keep} messages either way)")

    model = reg.get(conv["model_id"])
    if model is None or model.kind != "chat":
        raise HTTPException(400, f"unknown model for this conversation: {conv['model_id']}")

    prior = conv["context_summary"]
    prompt = _render_turns([dict(r) for r in to_compact])
    if prior:
        prompt = f"Earlier summary:\n{prior}\n\nAdditional conversation since then:\n{prompt}"

    payload = {
        "messages": [
            {"role": "system", "content": _COMPACT_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": settings.compact_summary_max_tokens,
        **model.sampling,
    }
    job = Job(
        user_id=principal.user_id,
        owner_name=principal.display_name,
        owner_email=principal.email,
        model_id=conv["model_id"],
        payload=payload,
        conversation_id=None,   # the summary lives on conversations.context_summary, not as a message
    )

    # compacting is real GPU time and gets billed like anything else
    await _check_limits(job, qm)
    await qm.submit(job)
    await job._done.wait()
    if job.state != JobState.DONE or not job.content.strip():
        raise HTTPException(502, job.error or "compaction produced no summary")

    summary = job.content.strip()
    new_boundary = to_compact[-1]["id"]
    await db.execute(
        "UPDATE conversations SET compact_boundary_id = ?, context_summary = ?, updated_at = ? "
        "WHERE id = ?",
        (new_boundary, summary, now(), conv_id),
    )
    return {
        "compact_boundary_id": new_boundary,
        "context_summary": summary,
        "compacted_count": len(to_compact),
        "model": conv["model_id"],
        "credits": job.credits,
        "prompt_tokens": job.prompt_tokens,
        "completion_tokens": job.completion_tokens,
    }


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
    conv = await db.fetch_one(
        "SELECT compact_boundary_id, context_summary FROM conversations WHERE id = ?",
        (conv_id,),
    )
    # Compacted messages (id <= boundary) are never resent verbatim -- the
    # running summary stands in for them instead. Nothing is deleted; they're
    # still visible in the UI, just excluded from what actually gets sent.
    boundary = (conv["compact_boundary_id"] if conv else None) or 0
    rows = await db.fetch_all(
        "SELECT role, content FROM messages "
        "WHERE conversation_id = ? AND id > ? ORDER BY id",
        (conv_id, boundary),
    )
    history = [{"role": r["role"], "content": r["content"]} for r in rows]
    if conv and conv["context_summary"]:
        history.insert(0, {
            "role": "system",
            "content": "Summary of the earlier part of this conversation "
                      "(context only -- don't refer to this note explicitly):\n"
                      + conv["context_summary"],
        })
    return history


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

    conv_id = req.conversation_id
    if conv_id:
        owned = await db.fetch_one(
            "SELECT id FROM conversations WHERE id = ? AND user_id = ?",
            (conv_id, principal.user_id),
        )
        if owned is None:
            raise HTTPException(404, "no such conversation")
        history = await _load_history(db, conv_id)
    else:
        history = []

    # Search runs here, outside the FIFO queue -- it's a local HTTP call, not
    # GPU time. One query, injected into this turn's prompt only; the user's
    # own message is persisted clean (search_json carries the citations).
    search_outcome = None
    if req.search:
        search_outcome = await get_search().search(req.message, settings.search_max_results)

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
        conversation_id=None,   # attached below, once the row is guaranteed to exist
    )
    # Admission first: a 429 must not leave a half-written conversation or a
    # user message that never got a reply.
    await _check_limits(job, qm)

    ts = now()
    if not conv_id:
        conv_id = uuid.uuid4().hex
        title = req.message.strip().replace("\n", " ")[:50]
        await db.execute(
            "INSERT INTO conversations (id, user_id, title, model_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (conv_id, principal.user_id, title, req.model, ts, ts),
        )
    await db.execute(
        "INSERT INTO messages (conversation_id, role, content, search_json, "
        "  image_upload_id, created_at) "
        "VALUES (?, 'user', ?, ?, ?, ?)",
        (conv_id, req.message,
         json.dumps(search_outcome.as_dict()) if search_outcome else None,
         req.image_id, ts),
    )
    job.conversation_id = conv_id
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
# OpenAI-compatible API (Phase 8.2) -- for IDE tools (Continue.dev etc.), not
# the web UI. Bearer API-key auth (get_principal_api_key), not oauth2-proxy --
# these paths are in oauth2-proxy's skip_auth_routes (deploy/oauth2-proxy.cfg)
# since an IDE isn't a browser session. Same FIFO queue + metering as
# everything else -- a `jobs` row is still written for billing, but there's no
# conversation to persist: the client sends its full message list every call,
# exactly like the real OpenAI API, and we don't keep IDE chatter in the web
# UI's history.
#
# The chat completion is a raw passthrough: the body goes upstream as it
# arrived (plus the model's sampling and the token cap) and llama-swap's bytes
# come back untouched, so tool calling and anything else llama.cpp supports
# works without this file knowing it exists.
# --------------------------------------------------------------------------- #
@app.get("/v1/models")
async def v1_models(principal: Principal = Depends(get_principal_api_key),
                    reg: Registry = Depends(get_registry)):
    return {
        "object": "list",
        "data": [{"id": m.key, "object": "model", "created": 0, "owned_by": "llamacracy"}
                 for m in reg.chat_models()],
    }


# How much of a /v1 request we understand: the model (it is the billing key and
# decides which llama-swap entry runs) and that there are messages at all.
# Everything else -- tools, tool_choice, response_format, logprobs, n, whatever
# the client sends next year -- is forwarded verbatim and never modelled here.
# Declaring those fields is what used to drop them: a pydantic model with
# `extra="ignore"` silently ate `tools`, so the chat template never rendered a
# tool section and models answered with a description of the call instead.
_V1_KEEPALIVE_S = 15.0


async def _v1_drain(job: Job, qm: QueueManager, request: Request):
    """Yields the queue's own internal events until the job ends, cancelling on
    client disconnect -- same contract as /api/chat's stream. Emits a keepalive
    event while the job is still waiting its turn, so a long FIFO wait or a
    heavyweight cold start never looks like a dead connection to the client."""
    try:
        while True:
            try:
                chunk = await asyncio.wait_for(job._chunks.get(), timeout=_V1_KEEPALIVE_S)
            except TimeoutError:
                yield {"type": "keepalive"}
                continue
            if chunk is None:
                break
            yield chunk
    except asyncio.CancelledError:
        await qm.cancel(job.id, job.user_id)
        raise
    if await request.is_disconnected():
        await qm.cancel(job.id, job.user_id)


@app.post("/v1/chat/completions")
async def v1_chat_completions(request: Request,
                              principal: Principal = Depends(get_principal_api_key),
                              settings: Settings = Depends(get_settings),
                              reg: Registry = Depends(get_registry),
                              qm: QueueManager = Depends(get_queue)):
    """Queued raw passthrough to llama-swap.

    The job holds the same exclusive FIFO slot and is metered off the same
    clocks as a web chat; the only thing this endpoint does to the payload is
    merge in the model's sampling defaults and clamp max_tokens. The response
    is llama-swap's own bytes, unaltered.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "body must be JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")

    model_id = body.get("model")
    model = reg.get(model_id) if isinstance(model_id, str) else None
    if model is None or model.kind != "chat" or not model.in_picker:
        raise HTTPException(400, f"unknown model: {model_id}")
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        raise HTTPException(400, "messages must be a non-empty array")

    try:
        asked = int(body["max_tokens"]) if body.get("max_tokens") is not None else None
    except (TypeError, ValueError):
        raise HTTPException(400, "max_tokens must be an integer") from None
    cap = min(asked or model.max_tokens_default,
              model.max_tokens_default,
              settings.max_tokens_per_request)

    stream = bool(body.get("stream"))
    # sampling wins over the client, as it does for the web UI: these models are
    # tuned per entry in the inventory and an IDE has no idea what suits them
    payload = {**body, "max_tokens": cap, **model.sampling}
    payload.pop("model", None)          # the queue sets it from job.model_id
    if stream:
        # credits come off the real usage block, never an estimate, so ask for
        # it. The client sees one extra final chunk: ordinary OpenAI behaviour.
        payload["stream_options"] = {**(payload.get("stream_options") or {}),
                                     "include_usage": True}

    job = Job(
        user_id=principal.user_id,
        owner_name=principal.display_name,
        owner_email=principal.email,
        model_id=model_id,
        payload=payload,
        conversation_id=None,   # the client owns history; nothing to persist here
        raw_passthrough=True,
    )

    # one shared meter, one shared session/weekly cap regardless of which
    # door a job came in through
    await _check_limits(job, qm)
    await qm.submit(job)

    if stream:
        async def event_stream():
            async for chunk in _v1_drain(job, qm, request):
                t = chunk.get("type")
                if t == "raw":
                    yield chunk["data"]
                elif t == "keepalive":
                    # an SSE comment: every conformant parser ignores it, so it
                    # holds the connection open without entering the payload
                    yield b": llamacracy queued\n\n"
                elif t == "cancelled":
                    # upstream was cut mid-stream and never sent its own [DONE]
                    yield b"data: [DONE]\n\n"
                elif t == "error":
                    yield _sse({"error": {"message": chunk.get("detail", "generation error")}}).encode()
                    yield b"data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    parts: list[bytes] = []
    async for chunk in _v1_drain(job, qm, request):
        t = chunk.get("type")
        if t == "raw":
            parts.append(chunk["data"])
        elif t == "error":
            raise HTTPException(502, chunk.get("detail", "generation error"))
    if not parts:
        raise HTTPException(502, "upstream returned nothing")
    return Response(content=b"".join(parts), media_type="application/json")


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
