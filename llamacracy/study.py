# SPDX-License-Identifier: AGPL-3.0-or-later
"""Study: courses, each with its own materials and a tutor.

A course belongs to the person who made it and nobody else sees it, the
same as their conversations. It has a name, a few lines about it in their
own words (level, textbook, how the exams work), its materials (documents
they uploaded for it: syllabus, notes, slides, past papers) and its
sessions, which are ordinary conversations with `course_id` set.

A session is an agent turn on a model marked `tools`, always: the tutor
looks things up in the materials (doc_outline / doc_search / doc_read)
instead of being handed them whole, so a course can hold a whole semester
without filling the context window, and it can say where an answer came
from. A course marked quantitative also requires every number to come from
run_python, which the server then checks (verify.py).

Nothing here is configuration: what a course is and what it holds is data
in the instance's own database, so one install serves anyone's subjects.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import documents, threads
from .config import Settings, get_settings
from .db import Database, get_db, now
from .identity import Principal, get_principal
from .registry import ModelInfo, Registry, get_registry
from .tools import DocRef

router = APIRouter(prefix="/api/courses")

MAX_COURSES = 50
MAX_MATERIALS = 60
NAME_MAX = 80
NOTES_MAX = 4000


def study_models(reg: Registry) -> list[ModelInfo]:
    """The models a session can run on: those that use tools."""
    return [m for m in reg.chat_models() if m.tools]


def default_model(reg: Registry, settings: Settings) -> str | None:
    """Where a new course starts: STUDY_MODEL if it can run sessions, else the
    first heavy-tier tools model, else any tools model."""
    ms = study_models(reg)
    if settings.study_model and any(m.key == settings.study_model for m in ms):
        return settings.study_model
    heavy = [m for m in ms if m.tier == "heavy"]
    return (heavy or ms)[0].key if ms else None


def enabled(reg: Registry, settings: Settings) -> bool:
    return settings.study and bool(study_models(reg))


async def owned(db: Database, course_id: str, user_id: int):
    row = await db.fetch_one("SELECT * FROM courses WHERE id = ? AND user_id = ?",
                             (course_id, user_id))
    if row is None:
        raise HTTPException(404, "no such course")
    return row


async def material_ids(db: Database, course_id: str) -> list[str]:
    rows = await db.fetch_all(
        "SELECT upload_id FROM course_materials WHERE course_id = ? ORDER BY added_at, upload_id",
        (course_id,))
    return [r["upload_id"] for r in rows]


async def materials(db: Database, course_id: str, user_id: int) -> list[documents.Loaded]:
    """A course's materials with their text, in the order they were added."""
    ids = await material_ids(db, course_id)
    docs = await documents.load(db, ids, user_id)
    return [docs[i] for i in ids if i in docs]


def brief(course, refs: list[DocRef], tokens: dict[str, int], *, compute: bool) -> str:
    """The tutor's part of the system prompt, ahead of the tools' own."""
    lines = [
        f'You are tutoring the user in their course "{course["name"]}". The goal is that they '
        "can do this themselves on the exam, not just that this question gets answered.",
        "",
        "How to tutor:",
        "- Start from where they are. If it isn't clear what they already understand, ask.",
        "- Guide rather than tell: one question at a time, a hint before a solution, and let them "
        "take the next step. Keep each turn short.",
        "- When they ask outright for an explanation or a worked solution, give it clearly, step "
        "by step, then ask one short question that checks they followed.",
        "- When they show you their work, find the first thing that's wrong and ask about that; "
        "don't rewrite all of it.",
        "- Be honest about mistakes, theirs and yours. Encourage without flattering.",
        "",
        "Getting it right:",
        "- Their course materials come first: look things up in them before answering from "
        "general knowledge, and say where it came from (the file, and the page or section).",
        "- If the materials don't cover it, say so, and mark what follows as general knowledge.",
        "- Never make up a quotation, a page number, a definition \"from the textbook\" or a "
        "source. Quote only what you have just read, word for word.",
    ]
    if course["quantitative"] and compute:
        lines.append(
            "- This course has math in it. Every number, derivative, integral, solution and unit "
            "conversion you state comes from run_python, hints included, and you check a result "
            "a second way when that's cheap.")
    elif course["quantitative"]:
        lines.append(
            "- This course has math in it, but Python isn't available right now: work carefully, "
            "and say plainly that your numbers weren't computed.")
    notes = (course["notes"] or "").strip()
    if notes:
        lines += ["", "About this course, in their words:", notes]
    lines.append("")
    if refs:
        lines.append("Their course materials (look inside with doc_outline, doc_search and doc_read):")
        for r in refs:
            size = f"{r.units} {r.unit}s, " if r.units and r.unit else ""
            lines.append(f'- {r.ref}: "{r.name}" ({size}~{tokens.get(r.ref, 0):,} tokens)')
    else:
        lines.append(
            "They haven't added any course materials yet. Answer from general knowledge, say "
            "so, and when it would help, suggest adding the syllabus, notes or slides.")
    return "\n".join(lines)


def _course(row, **extra) -> dict:
    return {"id": row["id"], "name": row["name"], "notes": row["notes"],
            "quantitative": bool(row["quantitative"]), "model_id": row["model_id"],
            "created_at": row["created_at"], "updated_at": row["updated_at"], **extra}


async def _drop_unused_uploads(db: Database, upload_ids: list[str], user_id: int) -> None:
    """Delete these uploads unless another course or a message still uses them."""
    if not upload_ids:
        return
    marks = ",".join("?" * len(upload_ids))
    rows = await db.fetch_all(
        f"SELECT id, path, text_path FROM uploads WHERE user_id = ? AND id IN ({marks})"
        " AND id NOT IN (SELECT upload_id FROM course_materials)"
        " AND id NOT IN (SELECT j.value FROM messages m, json_each(m.doc_ids_json) j"
        "                WHERE json_valid(m.doc_ids_json))",
        (user_id, *upload_ids))
    if rows:
        await db.transaction([("DELETE FROM uploads WHERE id = ?", (r["id"],)) for r in rows])
        threads.remove_files(rows)


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
class CourseIn(BaseModel):
    name: str = Field(min_length=1, max_length=NAME_MAX)
    notes: str = Field("", max_length=NOTES_MAX)
    quantitative: bool = False


class CoursePatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=NAME_MAX)
    notes: str | None = Field(None, max_length=NOTES_MAX)
    quantitative: bool | None = None


class MaterialIn(BaseModel):
    upload_id: str


@router.get("")
async def list_courses(principal: Principal = Depends(get_principal),
                       db: Database = Depends(get_db)):
    rows = await db.fetch_all(
        "SELECT c.*,"
        " (SELECT COUNT(*) FROM course_materials m WHERE m.course_id = c.id) AS n_materials,"
        " (SELECT COUNT(*) FROM conversations v WHERE v.course_id = c.id) AS n_sessions"
        " FROM courses c WHERE c.user_id = ? ORDER BY c.updated_at DESC",
        (principal.user_id,))
    return {"courses": [_course(r, materials=r["n_materials"], sessions=r["n_sessions"])
                        for r in rows]}


@router.post("")
async def create_course(body: CourseIn,
                        principal: Principal = Depends(get_principal),
                        settings: Settings = Depends(get_settings),
                        db: Database = Depends(get_db),
                        reg: Registry = Depends(get_registry)):
    if not body.name.strip():
        raise HTTPException(400, "a course needs a name")
    n = await db.fetch_one("SELECT COUNT(*) AS n FROM courses WHERE user_id = ?",
                           (principal.user_id,))
    if n["n"] >= MAX_COURSES:
        raise HTTPException(400, f"at most {MAX_COURSES} courses each")
    cid, ts = uuid.uuid4().hex, now()
    await db.execute(
        "INSERT INTO courses (id, user_id, name, notes, quantitative, model_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (cid, principal.user_id, body.name.strip(), body.notes.strip(), int(body.quantitative),
         default_model(reg, settings), ts, ts))
    return _course(await owned(db, cid, principal.user_id), materials=[], sessions=[])


@router.get("/{course_id}")
async def course_detail(course_id: str,
                        principal: Principal = Depends(get_principal),
                        db: Database = Depends(get_db)):
    row = await owned(db, course_id, principal.user_id)
    ids = await material_ids(db, course_id)
    marks = ",".join("?" * len(ids))
    docs = {r["id"]: documents.describe(r) for r in await db.fetch_all(
        f"SELECT id, filename, bytes, meta_json FROM uploads WHERE user_id = ? AND id IN ({marks})",
        (principal.user_id, *ids))} if ids else {}
    sessions = await db.fetch_all(
        "SELECT id, title, model_id, created_at, updated_at FROM conversations"
        " WHERE user_id = ? AND course_id = ? ORDER BY updated_at DESC LIMIT 200",
        (principal.user_id, course_id))
    return _course(row, materials=[docs[i] for i in ids if i in docs],
                   sessions=[dict(s) for s in sessions])


@router.patch("/{course_id}")
async def update_course(course_id: str, body: CoursePatch,
                        principal: Principal = Depends(get_principal),
                        db: Database = Depends(get_db)):
    await owned(db, course_id, principal.user_id)
    fields = body.model_dump(exclude_none=True)     # only what was sent
    for k in ("name", "notes"):
        if k in fields:
            fields[k] = fields[k].strip()
    if fields.get("name") == "":
        raise HTTPException(400, "a course needs a name")
    if "quantitative" in fields:
        fields["quantitative"] = int(fields["quantitative"])
    if fields:
        # column names come from the model's fields, never from the request
        await db.execute(
            f"UPDATE courses SET {', '.join(f'{k} = ?' for k in fields)}, updated_at = ? WHERE id = ?",
            (*fields.values(), now(), course_id))
    return _course(await owned(db, course_id, principal.user_id))


@router.delete("/{course_id}")
async def delete_course(course_id: str,
                        principal: Principal = Depends(get_principal),
                        db: Database = Depends(get_db)):
    """The course, its sessions and its materials, files included."""
    await owned(db, course_id, principal.user_id)
    sessions = await db.fetch_all(
        "SELECT id FROM conversations WHERE user_id = ? AND course_id = ?",
        (principal.user_id, course_id))
    for s in sessions:
        await threads.delete(db, s["id"], principal.user_id)
    ids = await material_ids(db, course_id)
    await db.transaction([
        ("DELETE FROM course_materials WHERE course_id = ?", (course_id,)),
        ("DELETE FROM courses WHERE id = ?", (course_id,)),
    ])
    await _drop_unused_uploads(db, ids, principal.user_id)
    return {"deleted": course_id, "sessions": len(sessions)}


@router.post("/{course_id}/materials")
async def add_material(course_id: str, body: MaterialIn,
                       principal: Principal = Depends(get_principal),
                       db: Database = Depends(get_db)):
    """Add a document (already uploaded and read, via /api/uploads) to a course."""
    await owned(db, course_id, principal.user_id)
    up = await db.fetch_one(
        "SELECT id, filename, bytes, meta_json FROM uploads"
        " WHERE id = ? AND user_id = ? AND kind = 'document'",
        (body.upload_id, principal.user_id))
    if up is None:
        raise HTTPException(404, "no such document")
    ids = await material_ids(db, course_id)
    if body.upload_id not in ids:
        if len(ids) >= MAX_MATERIALS:
            raise HTTPException(400, f"at most {MAX_MATERIALS} materials in a course")
        await db.transaction([
            ("INSERT INTO course_materials (course_id, upload_id, added_at) VALUES (?, ?, ?)",
             (course_id, body.upload_id, now())),
            ("UPDATE courses SET updated_at = ? WHERE id = ?", (now(), course_id)),
        ])
    return documents.describe(up)


@router.delete("/{course_id}/materials/{upload_id}")
async def remove_material(course_id: str, upload_id: str,
                          principal: Principal = Depends(get_principal),
                          db: Database = Depends(get_db)):
    await owned(db, course_id, principal.user_id)
    if upload_id not in await material_ids(db, course_id):
        raise HTTPException(404, "not one of this course's materials")
    await db.execute("DELETE FROM course_materials WHERE course_id = ? AND upload_id = ?",
                     (course_id, upload_id))
    await _drop_unused_uploads(db, [upload_id], principal.user_id)
    return {"removed": upload_id}
