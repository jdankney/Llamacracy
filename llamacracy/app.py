# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI application. Binds 127.0.0.1 only; oauth2-proxy sits in front (see
deploy/). Serves the SPA and the JSON/SSE API.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from . import documents, sandbox, study, threads
from .admin import router as admin_router
from .apikeys import generate as generate_api_key
from .config import Settings, get_settings
from .db import Database, get_db, now
from .identity import Principal, get_principal, get_principal_api_key
from .metering import Meter
from .queue import Job, JobState, QueueManager, get_queue, start_queue, stop_queue
from .registry import Registry, get_registry
from .search import get_search
from .tools import (
    DocRef,
    ImageRef,
    Toolbox,
    document_tools,
    image_search_tool,
    python_tool,
    read_page_tool,
    view_image_tool,
    web_search_tool,
    workspace_tools,
)
from .uploads import UploadRejected
from .uploads import save as save_upload
from .uploads import validate as validate_upload
from .upstream import Upstream, get_upstream
from .workspace import Workspace, branch_files

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("llamacracy")

STATIC_DIR = Path(__file__).parent / "static"
_HEX = r"^#[0-9a-fA-F]{6}$"


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
app.include_router(study.router)


# --------------------------------------------------------------------------- #
# models / me
# --------------------------------------------------------------------------- #
@app.get("/healthz")
async def healthz(up: Upstream = Depends(get_upstream)):
    return {"ok": True, "upstream": await up.health()}


class Appearance(BaseModel):
    """What a user can change about how the UI looks, for their account only.

    Colours end up in CSS custom properties, so they are held to a strict
    `#rrggbb` shape: nothing else can reach a style attribute. `preset` names
    a built-in palette; "custom" means the four colours below are in charge.
    """
    model_config = ConfigDict(extra="forbid")
    preset: str = Field(default="llamacracy", pattern=r"^[a-z][a-z0-9-]{0,31}$")
    bg: str | None = Field(default=None, pattern=_HEX)
    surface: str | None = Field(default=None, pattern=_HEX)
    text: str | None = Field(default=None, pattern=_HEX)
    accent: str | None = Field(default=None, pattern=_HEX)
    chat_text: Literal["sm", "md", "lg", "xl"] = "md"


class Prefs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    appearance: Appearance = Field(default_factory=Appearance)


async def _load_prefs(db: Database, user_id: int) -> Prefs:
    row = await db.fetch_one("SELECT prefs_json FROM users WHERE id = ?", (user_id,))
    if row is None or not row["prefs_json"]:
        return Prefs()
    try:
        return Prefs.model_validate_json(row["prefs_json"])
    except ValueError:
        # stored by an older build or hand-edited: fall back rather than
        # locking the user out of their own page
        log.warning("ignoring unreadable prefs for user %s", user_id)
        return Prefs()


@app.get("/api/me")
async def me(principal: Principal = Depends(get_principal),
             settings: Settings = Depends(get_settings),
             db: Database = Depends(get_db),
             reg: Registry = Depends(get_registry)):
    prefs = await _load_prefs(db, principal.user_id)
    return {
        "prefs": prefs.model_dump(),
        "email": principal.email,
        "display_name": principal.display_name,
        "is_admin": principal.is_admin,
        "source_url": settings.source_url,
        # tools models compute with run_python (the Help page says so only then)
        "compute": settings.python_tool and sandbox.available(settings.sandbox_python) is None,
        # /study: on when some model can run a session
        "study": {"enabled": study.enabled(reg, settings),
                  "default_model": study.default_model(reg, settings)},
        "limits": {
            "session_credit_limit": settings.session_credit_limit,
            "weekly_credit_limit": settings.weekly_credit_limit,
            "session_window_hours": settings.session_window_hours,
            "max_tokens_per_request": settings.max_tokens_per_request,
            "thinking_max_tokens": settings.thinking_max_tokens,
            "upload_max_mb": settings.upload_max_mb,
            "document_max_mb": settings.document_max_mb,
            "max_docs_per_message": documents.MAX_DOCS_PER_MESSAGE,
            "doc_inline_fraction": settings.doc_inline_fraction,
            "compact_keep_recent": settings.compact_keep_recent,
        },
    }


@app.put("/api/me/prefs")
async def put_prefs(prefs: Prefs,
                    principal: Principal = Depends(get_principal),
                    db: Database = Depends(get_db)):
    """Replace the caller's preferences. Scoped to the signed-in user: there
    is no way to read or write anyone else's through this route."""
    await db.execute("UPDATE users SET prefs_json = ? WHERE id = ?",
                     (prefs.model_dump_json(), principal.user_id))
    return {"prefs": prefs.model_dump()}


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
# API keys -- self-service, for the OpenAI-compatible endpoint
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
            "thinking": m.thinking,
            "tools": m.tools,
            "tok_s": m.seed_tg_tok_s,
            "pp_tok_s": m.seed_pp_tok_s,     # how fast it reads: attached documents
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
    """The sidebar's chats. Study sessions are listed under their course."""
    rows = await db.fetch_all(
        "SELECT id, title, model_id, created_at, updated_at FROM conversations "
        "WHERE user_id = ? AND course_id IS NULL ORDER BY updated_at DESC LIMIT 200",
        (principal.user_id,),
    )
    return {"conversations": [dict(r) for r in rows]}


async def _owned_conversation(db: Database, conv_id: str, user_id: int):
    conv = await db.fetch_one(
        "SELECT * FROM conversations WHERE id = ? AND user_id = ?", (conv_id, user_id))
    if conv is None:
        raise HTTPException(404, "no such conversation")
    return conv


async def _conversation_payload(db: Database, conv) -> dict:
    """The branch the user is looking at, as the UI renders it. Each message
    carries `siblings` (its alternatives, oldest first, itself included) so
    the UI can show "2 / 3" and switch between them; the conversation carries
    the compaction that applies to *this* branch."""
    rows = await threads.load(db, conv["id"])
    path = threads.active_path(conv["active_root_id"], rows)
    sibs = threads.siblings(rows)
    boundary, summary = threads.summary_point(path)
    docs = await _describe_documents(db, threads.document_ids(path), conv["user_id"])
    files = await branch_files(db, conv["id"], [r["id"] for r in path])
    messages = []
    for r in path:
        m = {k: v for k, v in r.items()
             if k not in ("active_child_id", "context_summary", "doc_ids_json")}
        m["siblings"] = sibs.get(r["parent_id"], [r["id"]])
        m["documents"] = [docs[i] for i in documents.doc_ids(r) if i in docs]
        messages.append(m)
    conversation = {k: conv[k] for k in conv.keys() if k != "active_root_id"}
    conversation.update(compact_boundary_id=boundary, context_summary=summary)
    conversation["files"] = [
        {"id": f.id, "path": f.path, "chars": len(f.content), "updated_at": f.created_at}
        for f in sorted(files.values(), key=lambda f: f.path) if f.content is not None]
    return {"conversation": conversation, "messages": messages}


async def _describe_documents(db: Database, ids: list[str], user_id: int) -> dict[str, dict]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = await db.fetch_all(
        f"SELECT id, filename, bytes, meta_json FROM uploads "
        f"WHERE user_id = ? AND kind = 'document' AND id IN ({marks})", (user_id, *ids))
    return {r["id"]: documents.describe(r) for r in rows}


@app.get("/api/conversations/{conv_id}")
async def conversation_detail(conv_id: str,
                              principal: Principal = Depends(get_principal),
                              db: Database = Depends(get_db)):
    conv = await _owned_conversation(db, conv_id, principal.user_id)
    return await _conversation_payload(db, conv)


@app.get("/api/conversations/{conv_id}/files/{file_id}")
async def workspace_file(conv_id: str, file_id: int, download: bool = False,
                         principal: Principal = Depends(get_principal),
                         db: Database = Depends(get_db)):
    """One version of a workspace file. Downloads are plain text attachments:
    a file the model wrote is never rendered as this site."""
    await _owned_conversation(db, conv_id, principal.user_id)
    row = await db.fetch_one(
        "SELECT id, path, content, created_at FROM workspace_files "
        "WHERE id = ? AND conversation_id = ? AND content IS NOT NULL", (file_id, conv_id))
    if row is None:
        raise HTTPException(404, "no such file")
    if download:
        return Response(row["content"].encode("utf-8"), media_type="text/plain; charset=utf-8",
                        headers={"Content-Disposition": _attachment(row["path"].split("/")[-1]),
                                 "X-Content-Type-Options": "nosniff"})
    return {"id": row["id"], "path": row["path"], "content": row["content"],
            "updated_at": row["created_at"]}


def _attachment(name: str) -> str:
    from urllib.parse import quote
    safe = re.sub(r'[^\w.\- ]', "_", name) or "file.txt"
    return f"attachment; filename=\"{safe}\"; filename*=UTF-8''{quote(name)}"


class SwitchRequest(BaseModel):
    message_id: int


@app.post("/api/conversations/{conv_id}/switch")
async def switch_branch(conv_id: str, req: SwitchRequest,
                        principal: Principal = Depends(get_principal),
                        db: Database = Depends(get_db)):
    """Show a different version of a message (the "< 2 / 3 >" controls). Only
    a pointer moves: every branch, and everything below it, is kept, and
    each remembers which of its own replies was last shown."""
    conv = await _owned_conversation(db, conv_id, principal.user_id)
    row = await db.fetch_one(
        "SELECT id, parent_id FROM messages WHERE id = ? AND conversation_id = ?",
        (req.message_id, conv_id))
    if row is None:
        raise HTTPException(404, "no such message in this conversation")
    if row["parent_id"] is None:
        await db.execute("UPDATE conversations SET active_root_id = ? WHERE id = ?",
                         (row["id"], conv_id))
    else:
        await db.execute("UPDATE messages SET active_child_id = ? WHERE id = ?",
                         (row["id"], row["parent_id"]))
    conv = await _owned_conversation(db, conv_id, principal.user_id)
    return await _conversation_payload(db, conv)


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
    await threads.delete(db, conv_id, principal.user_id)
    return {"deleted": conv_id}


# --------------------------------------------------------------------------- #
# compact context -- on demand, never automatic. Summarizes everything except
# the last COMPACT_KEEP_RECENT messages using the conversation's own model, so
# future turns send far less history. This is a real inference: it goes
# through the same FIFO queue and is billed the same as any other job -- not
# free like search. Nothing is deleted; threads.history() is what actually
# skips the folded-in messages when building a prompt. Compaction applies to
# the branch being shown: the summary is stored on its boundary message, so
# it covers exactly the paths that run through that message.
# --------------------------------------------------------------------------- #
_COMPACT_SYSTEM_PROMPT = (
    "You summarize conversations concisely and factually. Preserve names, "
    "decisions, numbers, and code/config specifics needed to continue "
    "naturally. Write neutral third-person notes -- no markdown headers, no "
    "commentary about summarizing, no meta remarks. Plain prose or short "
    "bullet points only."
)


def _render_turns(msgs: list[dict], doc_names: dict[str, str] | None = None) -> str:
    """The transcript a summary is written from. Attached documents are named,
    not included: the summary can say what was shared without re-reading it."""
    who = {"user": "User", "assistant": "Assistant", "system": "Note"}
    lines = []
    for m in msgs:
        names = [doc_names[i] for i in documents.doc_ids(m) if doc_names and i in doc_names]
        att = f"[attached: {', '.join(names)}] " if names else ""
        lines.append(f"{who.get(m['role'], m['role'])}: {att}{m['content']}")
    return "\n".join(lines)


@app.post("/api/conversations/{conv_id}/compact")
async def compact_conversation(conv_id: str,
                               principal: Principal = Depends(get_principal),
                               settings: Settings = Depends(get_settings),
                               db: Database = Depends(get_db),
                               reg: Registry = Depends(get_registry),
                               qm: QueueManager = Depends(get_queue)):
    conv = await _owned_conversation(db, conv_id, principal.user_id)
    path = threads.active_path(conv["active_root_id"], await threads.load(db, conv_id))
    boundary, prior = threads.summary_point(path)
    ids = [r["id"] for r in path]
    rows = path if boundary is None else path[ids.index(boundary) + 1:]
    keep = settings.compact_keep_recent
    to_compact = rows[:-keep] if keep else list(rows)
    if len(to_compact) < 2:
        raise HTTPException(
            400, "not enough new conversation since last time to be worth compacting "
                f"(keeps the last {keep} messages either way)")

    model = reg.get(conv["model_id"])
    if model is None or model.kind != "chat":
        raise HTTPException(400, f"unknown model for this conversation: {conv['model_id']}")

    doc_names = {i: d["filename"] for i, d in (await _describe_documents(
        db, threads.document_ids(to_compact), principal.user_id)).items()}
    prompt = _render_turns(to_compact, doc_names)
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
        conversation_id=None,   # the summary lives on its boundary message, not as a message of its own
    )

    # compacting is real GPU time and gets billed like anything else
    await _check_limits(job, qm)
    await qm.submit(job)
    await job._done.wait()
    if job.state != JobState.DONE or not job.content.strip():
        raise HTTPException(502, job.error or "compaction produced no summary")

    summary = job.content.strip()
    new_boundary = to_compact[-1]["id"]
    await db.transaction([
        ("UPDATE messages SET context_summary = ? WHERE id = ?", (summary, new_boundary)),
        ("UPDATE conversations SET updated_at = ? WHERE id = ?", (now(), conv_id)),
    ])
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
# uploads -- images (vision) and documents, on disk, scoped to the uploader
# --------------------------------------------------------------------------- #
@app.post("/api/uploads")
async def upload(file: UploadFile = File(...),
                 principal: Principal = Depends(get_principal),
                 settings: Settings = Depends(get_settings),
                 db: Database = Depends(get_db)):
    ctype = (file.content_type or "").split(";")[0].strip().lower()
    if ctype.startswith("image/"):
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
        return {"id": file_id, "kind": "image", "mime": file.content_type, "bytes": len(data)}
    return await _upload_document(file, principal, settings, db)


async def _upload_document(file: UploadFile, principal: Principal,
                           settings: Settings, db: Database) -> dict:
    """Extract now, outside the queue: a document is text by the time anyone
    sends it, so a send never waits on a parser and never holds the GPU."""
    limit = int(settings.document_max_mb * 1024 * 1024)
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(400, f"file too large (max {settings.document_max_mb:.0f} MB)")
    if not data:
        raise HTTPException(400, "empty file")
    filename = documents.clean_filename(file.filename)
    try:
        kind = documents.classify(filename, data[:8])
    except documents.DocumentRejected as e:
        raise HTTPException(400, str(e)) from e

    # the extractor reads from disk: park the bytes where they'll live anyway
    # (keeping the extension, which some readers insist on)
    ext = re.sub(r"[^a-z0-9.]", "", Path(filename).suffix.lower())[:12]
    tmp = Path(settings.upload_dir) / f".incoming-{uuid.uuid4().hex}{ext}"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(data)
    try:
        ex = await documents.extract(str(tmp), kind, settings.document_max_chars)
    except documents.DocumentRejected as e:
        raise HTTPException(400, f"{filename}: {e}") from e
    finally:
        tmp.unlink(missing_ok=True)
    if "no_text_layer" in ex.warnings and len(re.sub(r"\[Page \d+\]", "", ex.markdown).strip()) < 200:
        raise HTTPException(400, f"{filename} looks like a scan: its pages are pictures with "
                                 "no text in them. Attach a photo of the page to a vision "
                                 "model instead.")
    if not ex.markdown.strip():
        raise HTTPException(400, f"{filename} has no text in it to read")

    file_id, path, text_path = documents.save(settings.upload_dir, data, filename, ex.markdown)
    meta = {"tokens": ex.tokens, "units": ex.units, "unit": ex.unit, "warnings": ex.warnings}
    await db.execute(
        "INSERT INTO uploads (id, user_id, path, mime, bytes, kind, filename, text_path, "
        "  meta_json, created_at) VALUES (?, ?, ?, ?, ?, 'document', ?, ?, ?, ?)",
        (file_id, principal.user_id, path, "application/octet-stream", len(data),
         filename, text_path, json.dumps(meta), now()),
    )
    return {"kind": "document", "id": file_id, "filename": filename, "bytes": len(data), **meta}


@app.get("/api/uploads/{upload_id}")
async def get_upload(upload_id: str,
                     principal: Principal = Depends(get_principal),
                     db: Database = Depends(get_db)):
    row = await db.fetch_one(
        "SELECT path, mime, kind, filename FROM uploads WHERE id = ? AND user_id = ?",
        (upload_id, principal.user_id),
    )
    if row is None:
        raise HTTPException(404, "no such upload")
    if row["kind"] == "document":
        # always a download, never rendered: an uploaded .html or .svg shown
        # inline would run as this site
        return FileResponse(row["path"], media_type="application/octet-stream",
                            filename=row["filename"],
                            headers={"X-Content-Type-Options": "nosniff"})
    return FileResponse(row["path"], media_type=row["mime"])


@app.get("/api/uploads/{upload_id}/text")
async def get_upload_text(upload_id: str,
                          principal: Principal = Depends(get_principal),
                          db: Database = Depends(get_db)):
    """The text the model is given for a document: what the UI previews."""
    row = await db.fetch_one(
        "SELECT id, filename, bytes, text_path, meta_json FROM uploads "
        "WHERE id = ? AND user_id = ? AND kind = 'document'",
        (upload_id, principal.user_id),
    )
    if row is None:
        raise HTTPException(404, "no such document")
    try:
        text = Path(row["text_path"]).read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(410, "this document's text is no longer available") from None
    return {**documents.describe(row), "text": text}


# --------------------------------------------------------------------------- #
# chat
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    model: str
    message: str = Field(min_length=1)
    conversation_id: str | None = None
    max_tokens: int | None = None
    search: bool = False
    think: bool = False          # the Think toggle; ignored for models that can't
    image_id: str | None = None
    doc_ids: list[str] | None = None   # attached documents (upload ids)
    files: bool = False          # let the model write files to the conversation's workspace
    # Editing: the id of an earlier user message this one replaces. The
    # original is kept; this becomes its sibling (a new branch) and gets a
    # fresh reply. Omitted, the message continues the branch being shown.
    edit_of: int | None = None
    # Study: start a session in this course (a new conversation only; an
    # existing one keeps the course it was started in)
    course_id: str | None = None


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

    # Where the new message hangs in the conversation tree, and so what history
    # the model sees: the branch up to its parent. A plain send continues the
    # branch on screen; an edit becomes a sibling of the message it edits.
    conv_id = req.conversation_id
    parent_id: int | None = None
    image_id = req.image_id
    doc_ids = list(dict.fromkeys(req.doc_ids or []))
    prior: list[dict] = []
    course = None
    if conv_id:
        conv = await _owned_conversation(db, conv_id, principal.user_id)
        if conv["course_id"]:
            course = await study.owned(db, conv["course_id"], principal.user_id)
        rows = await threads.load(db, conv_id)
        if req.edit_of is not None:
            orig = next((r for r in rows if r["id"] == req.edit_of), None)
            if orig is None or orig["role"] != "user":
                raise HTTPException(404, "no such message of yours in this conversation")
            parent_id = orig["parent_id"]
            # editing the words shouldn't silently drop the photo or the
            # documents they were about
            image_id = image_id or orig["image_upload_id"]
            if req.doc_ids is None:
                doc_ids = documents.doc_ids(orig)
        else:
            path = threads.active_path(conv["active_root_id"], rows)
            parent_id = path[-1]["id"] if path else None
        prior = threads.path_to(parent_id, rows)
    elif req.edit_of is not None:
        raise HTTPException(400, "edit_of needs the conversation_id it belongs to")
    elif req.course_id:
        course = await study.owned(db, req.course_id, principal.user_id)
    if course is not None and not settings.study:
        raise HTTPException(400, "study sessions are turned off here")
    if course is not None and not model.tools:
        raise HTTPException(400, f"{model.display} can't run a study session: pick a model "
                                 "that looks things up")

    if len(doc_ids) > documents.MAX_DOCS_PER_MESSAGE:
        raise HTTPException(400, f"at most {documents.MAX_DOCS_PER_MESSAGE} documents per message")
    # Every document on this branch, fetched in one go and owned by this user
    # only: those still in the verbatim part of the history plus the new ones
    # ("in play"), and those a compaction has folded away.
    in_play = threads.document_ids(threads.after_summary(prior)) + doc_ids
    folded = [i for i in threads.document_ids(prior) if i not in in_play]
    docs = await documents.load(db, in_play + folded, principal.user_id)
    if any(i not in docs for i in doc_ids):
        raise HTTPException(404, "no such document")
    # Documents go in whole while they fit comfortably. Past that, a model
    # that is good with tools gets a short stand-in for the biggest ones and
    # looks things up in them (outline / search / read) instead; so does any
    # document a compaction folded away. Other models get everything whole,
    # or a clear refusal below if that can't fit.
    playing = [docs[i] for i in dict.fromkeys(in_play) if i in docs]
    lookup: list[documents.Loaded] = []
    if model.tools:
        playing, lookup = documents.split_inline(
            playing, int(model.ctx * settings.doc_inline_fraction))
    refs = [DocRef(f"d{n}", d.filename, d.text, d.meta.get("units"), d.meta.get("unit"))
            for n, d in enumerate(lookup + ([docs[i] for i in folded if i in docs]
                                            if model.tools else []), 1)]
    by_id = {d.id: r for d, r in zip(lookup, refs, strict=False)}
    # a course's materials: always looked up, never sent whole, so a semester
    # of notes doesn't fill the window on every turn
    course_refs: list[DocRef] = []
    course_tokens: dict[str, int] = {}
    if course is not None:
        for d in await study.materials(db, course["id"], principal.user_id):
            r = DocRef(f"d{len(refs) + len(course_refs) + 1}", d.filename, d.text,
                       d.meta.get("units"), d.meta.get("unit"))
            course_refs.append(r)
            course_tokens[r.ref] = d.tokens
    doc_blocks = {d.id: d.block() for d in playing}
    doc_blocks.update({i: r.stub(docs[i].tokens) for i, r in by_id.items()})
    history = threads.history(prior, doc_blocks)

    # An attached image routes this turn to the paired -vision llama-swap
    # entry (same weights + --mmproj) instead of the picked text model. The
    # extra VRAM/cold-load only happens on turns that actually carry an
    # image; every other turn behaves exactly as before.
    dispatch_model_id = req.model
    image_row = None
    if image_id:
        image_row = await db.fetch_one(
            "SELECT * FROM uploads WHERE id = ? AND user_id = ?",
            (image_id, principal.user_id),
        )
        if image_row is None:
            raise HTTPException(404, "no such upload")
        vision = reg.vision_variant(req.model)
        if vision is None:
            raise HTTPException(400, f"{model.display} doesn't support images yet")
        dispatch_model_id = vision.key

    # Search, two ways. A model that is good with tools gets the agent loop:
    # it writes its own queries, as many as it needs, inside its queued turn.
    # Any other model gets one search, run here outside the FIFO queue (a
    # local HTTP call, not GPU time) and injected into this turn's prompt
    # only; the user's own message is persisted clean (search_json carries
    # the citations).
    search_outcome = None
    agent = None
    web = req.search and model.tools
    # the workspace: on when asked, and from then on whenever this branch has
    # files, so a follow-up can always reach what an earlier turn wrote
    ws = await Workspace.open(db, conv_id, [r["id"] for r in prior])
    use_files = model.tools and (req.files or bool(ws.listing()))
    # Pictures: a model with a vision variant can look again at the photos
    # earlier in this branch, and with Search on find and view web images.
    # That needs the -vision entry (same weights + mmproj) for the turn.
    vision = reg.vision_variant(req.model)
    pictures: list[ImageRef] = []
    use_pictures = False
    if model.tools and vision is not None and settings.agent_images:
        earlier = [r for r in prior if r["image_upload_id"]]
        rows = await db.fetch_all(
            "SELECT id, path, mime FROM uploads WHERE user_id = ? AND kind = 'image' AND id IN ("
            + ",".join("?" * len(earlier)) + ")",
            (principal.user_id, *[r["image_upload_id"] for r in earlier])) if earlier else []
        found = {r["id"]: r for r in rows}
        pictures = [ImageRef(f"img{n}", r["image_upload_id"], found[r["image_upload_id"]]["path"],
                             found[r["image_upload_id"]]["mime"],
                             r["content"].strip().replace("\n", " ")[:80])
                    for n, r in enumerate((r for r in earlier if r["image_upload_id"] in found), 1)]
        use_pictures = bool(pictures) or web
        if use_pictures:
            dispatch_model_id = vision.key
    if (dispatch_model_id == req.model and vision is not None
            and qm._loaded_model == vision.key and reg.require(vision.key).ctx >= model.ctx):
        # already resident: the vision entry does everything the text one
        # does, so use it rather than swap models for a turn without pictures
        dispatch_model_id = vision.key
    # run_python: a tools model always has a calculator, when the sandbox is built
    compute = (model.tools and settings.python_tool
               and sandbox.available(settings.sandbox_python) is None)
    if web or refs or course_refs or use_files or use_pictures or compute or course is not None:
        tools = []
        if use_pictures:
            tools.append(view_image_tool(pictures, allow_web=web,
                                         max_bytes=int(settings.agent_image_max_mb * 1024 * 1024)))
            if web:
                tools.append(image_search_tool(get_search(), settings.agent_search_results))
        if use_files:
            tools += workspace_tools(ws, read_chars=settings.agent_read_chars)
        if web:
            tools += [web_search_tool(get_search(), settings.agent_search_results),
                      read_page_tool(read_chars=settings.agent_read_chars,
                                     max_bytes=int(settings.agent_page_max_mb * 1024 * 1024),
                                     scratch_dir=settings.upload_dir,
                                     pdf_max_chars=settings.document_max_chars)]
        if refs or course_refs:
            tools += document_tools(refs + course_refs, read_chars=settings.agent_read_chars)
        if compute:
            async def save_figure(png: bytes) -> str:
                file_id, path = save_upload(settings.upload_dir, png, ".png")
                await db.execute(
                    "INSERT INTO uploads (id, user_id, path, mime, bytes, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (file_id, principal.user_id, path, "image/png", len(png), now()))
                return file_id
            tools.append(python_tool(python=settings.sandbox_python,
                                     timeout_s=settings.sandbox_timeout_seconds,
                                     memory_mb=settings.sandbox_memory_mb,
                                     output_chars=8000, save_figure=save_figure))
        folded_refs = [r for r in refs if r not in by_id.values()]
        notes = []
        if pictures:
            notes.append("Earlier photos in this chat: " + "; ".join(
                f'{p.ref} (sent with "{p.caption}")' for p in pictures) + ".")
        if folded_refs:
            notes.append("Documents shared earlier in this conversation (before the summary):\n"
                         + "".join(r.stub(documents.estimate_tokens(r.text)) for r in folded_refs).strip())
        agent = Toolbox(tools, max_steps=settings.agent_max_steps,
                        deadline_s=settings.agent_max_seconds,
                        result_chars=settings.agent_tool_result_chars, notes=notes)
        if use_files:
            agent.workspace = ws
    elif req.search:
        search_outcome = await get_search().search(req.message, settings.search_max_results)

    prompt_content = req.message
    if search_outcome and search_outcome.ok and search_outcome.results:
        prompt_content = search_outcome.to_prompt_block() + req.message
    prompt_content = "".join(doc_blocks[i] for i in doc_ids) + prompt_content

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
    if agent is not None:
        # one system message, first: several chat templates reject a second
        # one, and a compacted history already starts with its summary
        brief = agent.system_prompt()
        if course is not None:
            brief = study.brief(course, course_refs, course_tokens, compute=compute) + "\n\n" + brief
        if messages[0]["role"] == "system":
            messages[0] = {"role": "system", "content": brief + "\n\n" + messages[0]["content"]}
        else:
            messages.insert(0, {"role": "system", "content": brief})

    # Thinking and the answer share one output budget, so a thinking turn gets
    # the larger THINKING_MAX_TOKENS instead of the everyday cap.
    think = req.think and model.thinking
    budget = (settings.thinking_max_tokens if think
              else min(model.max_tokens_default, settings.max_tokens_per_request))
    cap = min(req.max_tokens or budget, budget)
    if doc_blocks:
        # Documents are the one way a single send can blow straight past the
        # context window. Say so now, in words, instead of letting llama.cpp
        # fail the job after a queue wait.
        est = sum(documents.estimate_tokens(_text_of(m["content"])) + 4 for m in messages)
        if est + min(cap, 1024) > model.ctx:
            raise HTTPException(400, (
                f"With its documents this chat is about {est:,} tokens, more than "
                f"{model.display} can hold at once ({model.ctx:,}). Pick a model that "
                "holds more, or one that looks things up in long files (Help lists "
                "both), compact the chat, or attach less."))
    payload = {"messages": messages, "max_tokens": cap, **model.sampling}
    if model.thinking:
        payload["chat_template_kwargs"] = {"enable_thinking": think}

    job = Job(
        user_id=principal.user_id,
        owner_name=principal.display_name,
        owner_email=principal.email,
        model_id=dispatch_model_id,
        payload=payload,
        conversation_id=None,   # attached below, once the row is guaranteed to exist
        agent=agent,
    )
    # Admission first: a 429 must not leave a half-written conversation or a
    # user message that never got a reply.
    await _check_limits(job, qm)

    ts = now()
    if not conv_id:
        conv_id = uuid.uuid4().hex
        title = req.message.strip().replace("\n", " ")[:50]
        await db.execute(
            "INSERT INTO conversations (id, user_id, title, model_id, course_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (conv_id, principal.user_id, title, req.model, course["id"] if course else None, ts, ts),
        )
    if course is not None:
        # the course moves up its list, and its next session starts on this model
        await db.execute("UPDATE courses SET updated_at = ?, model_id = ? WHERE id = ?",
                         (ts, req.model, course["id"]))
    user_msg_id = await db.insert(
        "INSERT INTO messages (conversation_id, role, content, search_json, "
        "  image_upload_id, doc_ids_json, parent_id, created_at) "
        "VALUES (?, 'user', ?, ?, ?, ?, ?, ?)",
        (conv_id, req.message,
         json.dumps(search_outcome.as_dict()) if search_outcome else None,
         image_id, json.dumps(doc_ids) if doc_ids else None, parent_id, ts),
    )
    # ...and make it the version on screen: its parent (or, for a first
    # message, the conversation) now points at it
    if parent_id is None:
        await db.execute("UPDATE conversations SET active_root_id = ? WHERE id = ?",
                         (user_msg_id, conv_id))
    else:
        await db.execute("UPDATE messages SET active_child_id = ? WHERE id = ?",
                         (user_msg_id, parent_id))
    job.conversation_id = conv_id
    job.parent_message_id = user_msg_id
    # this turn's writes hang off its own message, on this branch only
    ws.conversation_id, ws.anchor = conv_id, user_msg_id
    await qm.submit(job)

    async def event_stream():
        yield _sse({"type": "accepted", "job_id": job.id,
                    "conversation_id": conv_id, "message_id": user_msg_id,
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


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    return "".join(p.get("text", "") for p in content if isinstance(p, dict))


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
# OpenAI-compatible API -- for IDE tools (Continue.dev etc.), not
# the web UI. Bearer API-key auth (get_principal_api_key), not oauth2-proxy --
# these paths are in oauth2-proxy's skip_auth_routes (deploy/oauth2-proxy.cfg)
# since an IDE isn't a browser session. Same FIFO queue + metering as
# everything else -- a `jobs` row is still written for billing, but there's no
# conversation to persist: the client sends its full message list every call,
# exactly like the real OpenAI API, and we don't keep IDE chatter in the web
# UI's history.
#
# The chat completion is a raw passthrough: the body goes upstream as it
# arrived (plus sampling defaults for anything it left out, and the token cap) and llama-swap's bytes
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


def _has_image(messages: list) -> bool:
    """True if any OpenAI-style message carries an image_url content part."""
    return any(
        isinstance(m, dict) and isinstance(m.get("content"), list)
        and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
        for m in messages
    )


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
    # An image anywhere in the history routes the job to the paired -vision
    # llama-swap entry, as /api/chat does: the text-only entry has no --mmproj
    # and llama.cpp rejects the whole request.
    dispatch_model_id = model_id
    if _has_image(body["messages"]):
        vision = reg.vision_variant(model_id)
        if vision is None:
            raise HTTPException(400, f"{model.display} doesn't support images yet")
        dispatch_model_id = vision.key

    try:
        asked = int(body["max_tokens"]) if body.get("max_tokens") is not None else None
    except (TypeError, ValueError):
        raise HTTPException(400, "max_tokens must be an integer") from None
    # Thinking is off unless the client asks (chat_template_kwargs.enable_thinking,
    # as llama.cpp and vLLM take it); the queue fills in the "off". A client
    # that does ask gets the thinking budget, since reasoning and answer share it.
    ctk = body.get("chat_template_kwargs")
    think = model.thinking and isinstance(ctk, dict) and ctk.get("enable_thinking") is True
    budget = max(settings.api_max_tokens_per_request,
                 settings.thinking_max_tokens if think else 0)
    cap = min(asked or budget, budget)

    stream = bool(body.get("stream"))
    # The inventory's sampling fills in what the client leaves out; what it
    # does send wins. Unlike the web UI, an IDE sets these on purpose (a low
    # temperature for its apply model, say).
    payload = {**model.sampling, **body, "max_tokens": cap}
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
        model_id=dispatch_model_id,
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
    @app.get("/study")
    @app.get("/study/{rest:path}")
    async def index():
        idx = STATIC_DIR / "index.html"
        if idx.exists():
            return FileResponse(idx)
        return {"service": "llamacracy", "ui": "static/index.html is missing"}
