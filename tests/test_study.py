# SPDX-License-Identifier: AGPL-3.0-or-later
"""Study: courses, their materials, sessions with the tutor, and privacy."""

from __future__ import annotations

from pathlib import Path

from llamacracy.config import get_settings
from tests.test_api import _chat, _upload_doc, agent_client, client, compute_client  # noqa: F401

SYLLABUS = (b"# Linear Algebra syllabus\n\n## Week 5\n\nEigenvalues and eigenvectors.\n\n"
            b"## Grading\n\nTwo midterms (25% each) and a final (50%).\n")


def _as(monkeypatch, who: str) -> None:
    """Make the next requests come from someone else (DEV_MODE identity)."""
    monkeypatch.setenv("DEV_SUB", f"sub-{who}")
    monkeypatch.setenv("DEV_EMAIL", f"{who}@localhost")
    get_settings.cache_clear()


def _course(c, **body) -> dict:
    r = c.post("/api/courses", json={"name": "Linear Algebra", **body})
    assert r.status_code == 200, r.text
    return r.json()


def test_courses_are_made_listed_changed_and_private(agent_client, monkeypatch):
    c = agent_client
    course = _course(c, notes="Textbook chapters 1 to 7", quantitative=True)
    assert course["quantitative"] and course["model_id"] == "small"   # the only tools model
    assert [x["name"] for x in c.get("/api/courses").json()["courses"]] == ["Linear Algebra"]
    r = c.patch(f"/api/courses/{course['id']}", json={"notes": "  Exam is open-note.  "})
    assert r.json()["notes"] == "Exam is open-note." and r.json()["name"] == "Linear Algebra"
    assert c.patch(f"/api/courses/{course['id']}", json={"name": "   "}).status_code == 400

    # nobody else can see it, change it, or use it
    _as(monkeypatch, "someone")
    assert c.get("/api/courses").json()["courses"] == []
    assert c.get(f"/api/courses/{course['id']}").status_code == 404
    assert c.patch(f"/api/courses/{course['id']}", json={"name": "mine"}).status_code == 404
    assert c.delete(f"/api/courses/{course['id']}").status_code == 404
    r = c.post("/api/chat", json={"model": "small", "message": "hi", "course_id": course["id"]})
    assert r.status_code == 404


def test_materials_are_the_owners_documents_only(agent_client, monkeypatch):
    c = agent_client
    course = _course(c)
    doc = _upload_doc(c, "syllabus.md", SYLLABUS)
    assert c.post(f"/api/courses/{course['id']}/materials", json={"upload_id": doc["id"]}).status_code == 200
    # adding it twice is a no-op
    assert c.post(f"/api/courses/{course['id']}/materials", json={"upload_id": doc["id"]}).status_code == 200
    detail = c.get(f"/api/courses/{course['id']}").json()
    assert [m["filename"] for m in detail["materials"]] == ["syllabus.md"]

    _as(monkeypatch, "someone")
    theirs = _course(c, name="Music Theory")
    # someone else's document can't be added to your course
    r = c.post(f"/api/courses/{theirs['id']}/materials", json={"upload_id": doc["id"]})
    assert r.status_code == 404


def test_a_session_tutors_from_the_course_materials(agent_client):
    c, up = agent_client, agent_client.fake_upstream
    course = _course(c, notes="Exams are closed-book.")
    doc = _upload_doc(c, "syllabus.md", SYLLABUS)
    c.post(f"/api/courses/{course['id']}/materials", json={"upload_id": doc["id"]})
    up.agent_steps = [
        {"tool_calls": [("doc_search", {"ref": "d1", "query": "midterms"})]},
        {"content": ["The final is 50% of the grade (syllabus.md, Grading)."]},
    ]
    ev = _chat(c, model="small", message="How is the course graded?", course_id=course["id"])
    assert ev[-1]["state"] == "done"

    first = up.payloads[0]
    system = first["messages"][0]["content"]
    assert system.startswith('You are tutoring the user in their course "Linear Algebra"')
    assert "Exams are closed-book." in system and '- d1: "syllabus.md"' in system
    assert "doc_search" in [t["function"]["name"] for t in first["tools"]]
    # the materials are looked up, not pasted into the message
    assert "Two midterms" not in first["messages"][-1]["content"]
    found = next(m for m in up.payloads[1]["messages"] if m["role"] == "tool")["content"]
    assert "Two midterms (25% each)" in found

    # the session is listed under its course, not among the chats
    conv = ev[0]["conversation_id"]
    assert c.get("/api/conversations").json()["conversations"] == []
    detail = c.get(f"/api/courses/{course['id']}").json()
    assert [s["id"] for s in detail["sessions"]] == [conv]
    assert c.get(f"/api/conversations/{conv}").json()["conversation"]["course_id"] == course["id"]

    # a follow-up stays in the course without saying so again
    up.agent_steps = [{"content": ["Yes."]}]
    _chat(c, model="small", message="And the midterms?", conversation_id=conv)
    assert up.payloads[-1]["messages"][0]["content"].startswith("You are tutoring")


def test_a_course_with_no_materials_says_so(agent_client):
    c, up = agent_client, agent_client.fake_upstream
    course = _course(c)
    up.agent_steps = [{"content": ["Let's start with what you know."]}]
    _chat(c, model="small", message="Teach me diagonalization", course_id=course["id"])
    system = up.payloads[0]["messages"][0]["content"]
    assert "haven't added any course materials yet" in system
    assert "doc_search" not in [t["function"]["name"] for t in up.payloads[0].get("tools", [])]


def test_a_quantitative_course_insists_on_computing(compute_client):
    c, up = compute_client, compute_client.fake_upstream
    course = _course(c, quantitative=True)
    up.agent_steps = [{"content": ["Try expanding along the first row."]}]
    _chat(c, model="small", message="Help with a determinant", course_id=course["id"])
    system = up.payloads[0]["messages"][0]["content"]
    assert "comes from run_python, hints included" in system
    assert "run_python" in [t["function"]["name"] for t in up.payloads[0]["tools"]]


def test_sessions_need_a_model_that_looks_things_up(client):
    course = client.post("/api/courses", json={"name": "Medieval History"}).json()
    r = client.post("/api/chat", json={"model": "small", "message": "hi", "course_id": course["id"]})
    assert r.status_code == 400 and "can't run a study session" in r.json()["detail"]
    assert client.db._conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
    assert client.get("/api/me").json()["study"] == {"enabled": False, "default_model": None}


def test_deleting_a_course_deletes_its_sessions_and_materials(agent_client):
    c, up = agent_client, agent_client.fake_upstream
    course = _course(c)
    doc = _upload_doc(c, "notes.md", SYLLABUS)
    c.post(f"/api/courses/{course['id']}/materials", json={"upload_id": doc["id"]})
    up.agent_steps = [{"content": ["ok"]}]
    _chat(c, model="small", message="hi", course_id=course["id"])
    paths = c.db._conn.execute("SELECT path, text_path FROM uploads WHERE id = ?", (doc["id"],)).fetchone()

    r = c.delete(f"/api/courses/{course['id']}")
    assert r.json() == {"deleted": course["id"], "sessions": 1}
    for table in ("courses", "course_materials", "conversations", "messages", "uploads"):
        assert c.db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    assert not Path(paths["path"]).exists() and not Path(paths["text_path"]).exists()


def test_removing_a_material_keeps_a_document_still_in_use(agent_client):
    c, up = agent_client, agent_client.fake_upstream
    course = _course(c)
    doc = _upload_doc(c, "notes.md", SYLLABUS)
    c.post(f"/api/courses/{course['id']}/materials", json={"upload_id": doc["id"]})
    # the same file is also attached to a chat message
    up.agent_steps = [{"content": ["ok"]}]
    _chat(c, model="small", message="summarise", doc_ids=[doc["id"]])
    assert c.delete(f"/api/courses/{course['id']}/materials/{doc['id']}").status_code == 200
    assert c.get(f"/api/courses/{course['id']}").json()["materials"] == []
    assert c.get(f"/api/uploads/{doc['id']}").status_code == 200       # the chat still has it


def test_study_pages_serve_the_app(client):
    for path in ("/study", "/study/abc123"):
        r = client.get(path)
        assert r.status_code == 200 and "<div id=\"app\">" in r.text
