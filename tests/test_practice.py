# SPDX-License-Identifier: AGPL-3.0-or-later
"""Practice problems: keys checked before anyone sees them, answers graded by the server."""

from __future__ import annotations

import math

from llamacracy.practice import number_matches, parse_number
from tests.test_api import _chat, _upload_doc, agent_client, client, compute_client  # noqa: F401
from tests.test_study import SYLLABUS, _as, _course


def test_numbers_are_read_the_way_people_type_them():
    cases = {"16/5": 3.2, "3π/4": 3 * math.pi / 4, "3*pi/4": 3 * math.pi / 4, "√2": math.sqrt(2),
             "2^10": 1024, "1.2 × 10^-3": 1.2e-3, "1.2e-3": 1.2e-3, "1,450": 1450, "−2.5": -2.5,
             "2sqrt(2)": 2 * math.sqrt(2), "$0.5$": 0.5, "(1/2)(3)": 1.5}
    for text, want in cases.items():
        assert math.isclose(parse_number(text), want), text
    assert parse_number("0.2 m", "m") == 0.2
    for bad in ("about 3", "__import__('os')", "x + 1", "9**9999", "", "2**100"):
        assert parse_number(bad) is None, bad


def test_answers_match_exactly_or_to_the_figures_written():
    third = 1 / 3
    assert number_matches("1/3", third) and number_matches("0.333", third) and number_matches("0.33", third)
    assert number_matches("0.3", third) is False            # one figure isn't enough
    assert number_matches("3.14", math.pi) and not number_matches("3.15", math.pi)
    assert number_matches("16/5", 3.2) and number_matches("3.2", 3.2) and not number_matches("3.3", 3.2)
    assert number_matches("hello", 3.2) is None


def test_a_practice_set_with_checked_keys(compute_client, monkeypatch):
    c, up = compute_client, compute_client.fake_upstream
    course = _course(c, quantitative=True)
    doc = _upload_doc(c, "syllabus.md", SYLLABUS)
    c.post(f"/api/courses/{course['id']}/materials", json={"upload_id": doc["id"]})
    up.agent_steps = [
        {"tool_calls": [("doc_search", {"ref": "d1", "query": "grading"})]},
        {"tool_calls": [
            # the fake sandbox prints 2.6666666666666665: 8/3 is checked, 3 is sent back
            ("add_problem", {"question": "Integrate x from 0 to 4, then divide by 3.", "kind": "number",
                             "topic": "Integrals", "answer": "8/3", "code": "print(8/3)",
                             "solution": "x^2/2 from 0 to 4 is 8; over 3."}),
            ("add_problem", {"question": "What is 1 + 2?", "kind": "number", "topic": "Integrals",
                             "answer": "3", "code": "print(1 + 2)"}),
            ("add_problem", {"question": "How much is each midterm worth?", "kind": "choice",
                             "topic": "Course rules", "choices": ["10%", "25%", "50%"], "answer": "B",
                             "source": "syllabus.md", "quote": "Two midterms (25% each)"}),
            ("add_problem", {"question": "Why grade this way?", "kind": "short", "topic": "Course rules",
                             "answer": "To spread the risk.", "quote": "a sentence the syllabus never had"}),
        ]},
        {"content": ["Two problems on integrals and course rules."]},
    ]
    ev = _chat(c, model="small", message="Give me practice problems", course_id=course["id"], mode="practice")
    first = up.payloads[0]
    assert first["tool_choice"] == "required" and "add_problem" in [t["function"]["name"] for t in first["tools"]]
    assert "Practice mode" in first["messages"][0]["content"]
    results = [m["content"] for m in up.payloads[2]["messages"] if m["role"] == "tool"][1:]
    assert results[0].startswith("Added problem 1. Key checked: your code printed it.")
    assert results[1].startswith("Not added: your answer 3 isn't what your code printed")
    assert results[2].startswith("Added problem 2. Key checked: the quote is in the materials.")
    assert results[3].startswith("Not added: `quote` isn't word for word")

    ids = ev[-1]["tools"]["practice"]
    assert len(ids) == 2
    cards = c.get("/api/practice", params={"ids": ",".join(ids)}).json()["problems"]
    assert [p["verified"] for p in cards] == ["code", "quote"]
    assert all("answer" not in p and "solution" not in p for p in cards)       # keys stay hidden
    num, choice = ids

    r = c.post(f"/api/practice/{num}/answer", json={"answer": "2.5"}).json()
    assert r["status"] == "wrong" and "answer" not in r                        # try again
    assert c.post(f"/api/practice/{num}/answer", json={"answer": "lots"}).status_code == 400
    r = c.post(f"/api/practice/{num}/answer", json={"answer": "2.67"}).json()
    assert r["status"] == "right" and r["answer"] == "8/3" and r["tries"] == 2 and r["code"] == "print(8/3)"
    r = c.post(f"/api/practice/{choice}/reveal").json()
    assert r["status"] == "revealed" and r["answer"] == "25%" and r["quote"] == "Two midterms (25% each)"

    prog = c.get("/api/practice/progress", params={"course_id": course["id"]}).json()
    assert prog["topics"] == [{"topic": "Course rules", "tried": 1, "first_try": 0},
                              {"topic": "Integrals", "tried": 1, "first_try": 0}]
    assert [m["id"] for m in prog["missed"]] == [choice]

    # yours alone, and gone with the session
    _as(monkeypatch, "someone")
    assert c.get("/api/practice", params={"ids": num}).json()["problems"] == []
    assert c.post(f"/api/practice/{num}/reveal").status_code == 404
    # and back to the owner
    monkeypatch.setenv("DEV_SUB", "dev-local-sub")
    monkeypatch.setenv("DEV_EMAIL", "dev@localhost")
    from llamacracy.config import get_settings
    get_settings.cache_clear()
    assert c.delete(f"/api/conversations/{ev[0]['conversation_id']}").status_code == 200
    for table in ("practice_problems", "practice_attempts"):
        assert c.db._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_a_written_answer_is_marked_by_the_student(agent_client):
    c, up = agent_client, agent_client.fake_upstream
    course = _course(c)
    up.agent_steps = [
        {"tool_calls": [("add_problem", {"question": "Define a colophon.", "kind": "short",
                                         "answer": "A closing note naming printer, place and date."})]},
        {"content": ["One for you."]},
    ]
    ev = _chat(c, model="small", message="practice", course_id=course["id"], mode="practice")
    pid = ev[-1]["tools"]["practice"][0]
    r = c.post(f"/api/practice/{pid}/answer", json={"answer": "an end note"}).json()
    assert r["mark_yourself"] and r["answer"].startswith("A closing note")
    assert c.post(f"/api/practice/{pid}/mark", json={"right": True}).json()["status"] == "self_right"


def test_a_numeric_choice_needs_code_and_facts_need_none(compute_client):
    c, up = compute_client, compute_client.fake_upstream
    math_course = _course(c, quantitative=True)
    up.agent_steps = [
        {"tool_calls": [
            ("add_problem", {"question": "Which is 8/3?", "kind": "choice", "answer": "A",
                             "choices": ["2.6666666666666665", "3"]}),
            ("add_problem", {"question": "Which is 8/3?", "kind": "choice", "answer": "A",
                             "choices": ["2.6666666666666665", "3"], "code": "print(8/3)"}),
        ]},
        {"content": ["One."]},
    ]
    ev = _chat(c, model="small", message="practice", course_id=math_course["id"], mode="practice")
    results = [m["content"] for m in up.payloads[1]["messages"] if m["role"] == "tool"]
    assert results[0].startswith("Not added: give the Python") and results[1].startswith("Added problem 1. Key checked")
    assert len(ev[-1]["tools"]["practice"]) == 1

    # a history course: a year needs no code
    hist = _course(c, name="Medieval History")
    up.agent_steps = [{"tool_calls": [("add_problem", {"question": "When?", "kind": "number", "answer": "1469"})]},
                      {"content": ["One."]}]
    ev = _chat(c, model="small", message="practice", course_id=hist["id"], mode="practice")
    assert c.get("/api/practice", params={"ids": ev[-1]["tools"]["practice"][0]}).json()["problems"][0]["verified"] is None


def test_exact_expressions_sympy_prints_are_read():
    from llamacracy.practice import printed_values
    out = "inner: x**4/2\nresult = 1/2 - cos(1)/2\n-2 + pi\n4*sqrt(2)/3"
    vals = printed_values(out)
    for want in (0.5 - math.cos(1) / 2, math.pi - 2, 4 * math.sqrt(2) / 3):
        assert any(math.isclose(v, want) for v in vals), want


def test_the_key_never_reaches_the_browser_before_they_try(compute_client):
    import json
    c, up = compute_client, compute_client.fake_upstream
    course = _course(c, quantitative=True)
    up.agent_steps = [
        {"tool_calls": [("add_problem", {"question": "Q?", "kind": "number", "topic": "Fractions",
                                         "answer": "8/3", "code": "print(8/3)",
                                         "solution": "SECRET-SOLUTION", "quote": "SECRET-QUOTE"})]},
        {"content": ["Here is a problem."]},
    ]
    ev = _chat(c, model="small", message="practice", course_id=course["id"], mode="practice")
    shown = json.dumps([e for e in ev if e["type"] in ("tool_draft", "tool_start", "tool_end")])
    conv = ev[0]["conversation_id"]
    stored = json.dumps(c.get(f"/api/conversations/{conv}").json())
    for secret in ("8/3", "print(8/3)", "SECRET-SOLUTION", "SECRET-QUOTE"):
        assert secret not in shown and secret not in stored, secret
    assert "Fractions" in shown                     # what's safe to show still is


def test_a_choice_is_graded_by_its_text_not_its_position(agent_client):
    c, up = agent_client, agent_client.fake_upstream
    course = _course(c, name="Arithmetic")
    up.agent_steps = [{"tool_calls": [("add_problem", {
        "question": "1 + 1?", "kind": "choice", "answer": "2", "choices": ["1", "2", "3", "4"]})]},
        {"content": ["One."]}]
    ev = _chat(c, model="small", message="practice", course_id=course["id"], mode="practice")
    pid = ev[-1]["tools"]["practice"][0]
    assert c.post(f"/api/practice/{pid}/answer", json={"answer": "1"}).json()["status"] == "wrong"
    assert c.post(f"/api/practice/{pid}/answer", json={"answer": "2"}).json()["status"] == "right"


def test_halves_round_up_as_people_are_taught():
    assert number_matches("13", 12.5) and not number_matches("12", 12.5)
    assert number_matches("0.13", 0.125) and number_matches("2.68", 2.675)
    assert number_matches("-13", -12.5) and number_matches("1.0e3", 1000.4)
