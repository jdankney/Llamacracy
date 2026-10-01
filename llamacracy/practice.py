# SPDX-License-Identifier: AGPL-3.0-or-later
"""Practice problems: sets the tutor writes, with answer keys the server checks.

In Practice mode a study session's tutor gets one more tool, add_problem,
and builds a set by calling it once per problem. A problem is stored as it
is added and shown as a card the student answers; its key stays on the
server until they get it right or ask to see it.

The keys are checked before anyone sees them, not trusted:

- a "number" problem in a course with Python must come with the code that
  computes its answer. The server runs it in the sandbox, and a problem
  whose stated answer isn't among what its code printed is sent back.
- any problem may carry `quote`, the words in the materials its answer
  rests on; they must be there word for word (quotes.py), or it's sent back.

Grading is the server's too, and costs no GPU time: a number answer is
read as a number or a simple exact expression (16/5, 3*pi/4, sqrt(2),
1.2e-3, with or without its unit) and accepted when it matches the key
exactly or to the significant figures the student wrote (0.33 for 1/3);
a multiple-choice answer is compared with the right option. A short
written answer can't be graded fairly by a rule, so the student sees the
model answer and marks it themselves.

Each attempt is kept, which is what the course page's progress (first-try
results by topic, and what keeps going wrong) is made from.
"""

from __future__ import annotations

import ast
import json
import logging
import math
import operator
import re
import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import quotes, verify
from .db import Database, get_db, now
from .identity import Principal, get_principal
from .tools import Tool, Toolbox, ToolOutcome

log = logging.getLogger("llamacracy.practice")
router = APIRouter(prefix="/api/practice")

KINDS = ("number", "choice", "short")
MAX_PER_SET = 10
MAX_CHOICES = 6
QUESTION_MAX = 4000


# --------------------------------------------------------------------------- #
# reading a number answer
# --------------------------------------------------------------------------- #
_BIN = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.Pow: operator.pow}
_FUN = {"sqrt": math.sqrt, "ln": math.log, "log": math.log, "log10": math.log10, "exp": math.exp,
        "sin": math.sin, "cos": math.cos, "tan": math.tan, "asin": math.asin,
        "acos": math.acos, "atan": math.atan, "abs": abs}
_CONST = {"pi": math.pi, "e": math.e}


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, int | float) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.Name) and node.id in _CONST:
        return _CONST[node.id]
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        v = _eval(node.operand)
        return -v if isinstance(node.op, ast.USub) else v
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN:
        a, b = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(b) > 64:
            raise ValueError("exponent too large")
        return _BIN[type(node.op)](a, b)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _FUN
            and len(node.args) == 1 and not node.keywords):
        return float(_FUN[node.func.id](_eval(node.args[0])))
    raise ValueError("not a number")


def parse_number(text: str, unit: str | None = None) -> float | None:
    """A number, or a simple exact expression, as people type them: 16/5,
    3π/4, √2, 2^10, 1.2 × 10^-3, 1,450, with or without the unit."""
    s = (text or "").strip().strip("$").strip()
    if unit and s.lower().endswith(unit.lower()):
        s = s[:-len(unit)].strip()
    if not s or len(s) > 120:
        return None
    s = s.replace("−", "-").replace("×", "*").replace("·", "*").replace("^", "**")
    s = re.sub(r"(\d),(?=\d{3}\b)", r"\1", s)                       # 1,450
    s = re.sub(r"(?<=\d)\s*[xX]\s*(?=10\b)", "*", s)                 # 1.2 x 10**-3
    s = s.replace("π", "pi").replace("√", "sqrt")
    s = re.sub(r"sqrt\s*(\d+(?:\.\d+)?)", r"sqrt(\1)", s)            # √2
    s = re.sub(r"(\d)\s*(pi|sqrt|e(?![-+]?\d)\b|\()", r"\1*\2", s)   # 3pi, 2sqrt(2), 2e, 2(3); not 1.2e-3
    s = re.sub(r"\)\s*(?=[\w(])", ")*", s)                           # (1/2)(3)
    try:
        v = _eval(ast.parse(s, mode="eval"))
    except (SyntaxError, ValueError, ZeroDivisionError, OverflowError, TypeError, RecursionError):
        return None
    return v if math.isfinite(v) else None


def _sig_figs(text: str) -> int | None:
    """Significant figures in a plain decimal answer (0.33 -> 2), else None."""
    m = re.fullmatch(r"\s*[-−]?(\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?\s*", text or "")
    if not m:
        return None
    digits = m.group(1).replace(".", "").lstrip("0")
    return len(digits) or 1


def _round_sig(x: float, n: int) -> float:
    return 0.0 if x == 0 else round(x, n - 1 - math.floor(math.log10(abs(x))))


def number_matches(answer: str, key: float, unit: str | None = None) -> bool | None:
    """Whether an answer is the key: exactly, or rounded to the significant
    figures it was written with (at least two). None when it isn't a number."""
    v = parse_number(answer, unit)
    if v is None:
        return None
    if abs(v - key) <= 1e-9 * max(1.0, abs(key)):
        return True
    figs = _sig_figs((answer or "").strip().strip("$").removesuffix(unit or "\0").strip())
    if figs and figs >= 2 and key != 0:
        return _round_sig(v, figs) == _round_sig(key, figs)
    return False


def printed_values(output: str) -> list[float]:
    """The values some code printed: plain numbers, and the exact expressions
    sympy prints (1/2 - cos(1)/2, -2 + pi, 4*sqrt(2)/3), a line at a time or
    after an "=" or ":"."""
    values = verify.text_numbers(output)
    for line in output.splitlines()[-40:]:
        for part in {line, line.rsplit("=", 1)[-1], line.rsplit(":", 1)[-1]}:
            v = parse_number(part.replace("**", "^"))
            if v is not None:
                values.append(v)
    return values


# --------------------------------------------------------------------------- #
# the tutor's tool
# --------------------------------------------------------------------------- #
def add_problem_tool(*, save: Callable[[dict], Awaitable[str]], compute: bool, quantitative: bool,
                     run_code: Callable[[str], Awaitable[tuple[bool, str]]] | None) -> Tool:
    """add_problem, for Practice mode. `save` stores a checked problem and
    returns its id; `run_code` runs Python in the sandbox (ok, output). In a
    quantitative course with Python, every numeric key must come with code;
    elsewhere code is optional, and checked when given."""
    added: list[str] = []

    async def check_code(code: str, value: float, answer: str, row: dict, box: Toolbox) -> str | None:
        """None when the code prints the key, else why it was sent back."""
        if not (compute and run_code is not None):
            return None
        if not code:
            return ("give the Python that computes the answer in `code`: it is run to check "
                    "your key before the student sees the problem." if quantitative else None)
        ok, output = await run_code(code)
        box.python_outputs.append(output)        # computed: the number check counts it
        box.python_code.append(code)
        tail = output[-1500:]
        if not ok:
            return f"your code failed:\n{tail}"
        if not any(abs(p - value) <= 1e-6 * max(1.0, abs(value)) for p in printed_values(output)):
            return (f"your answer {answer} isn't what your code printed:\n{tail}\n"
                    "Fix the code or the answer (printing float(result) too helps), and add the "
                    "problem again.")
        row.update(code=code, code_output=output[-2000:], verified="code")
        return None

    async def run(args: dict, box: Toolbox) -> ToolOutcome:
        def back(why: str) -> ToolOutcome:
            log.info("add_problem sent back: %s", why[:300])
            return ToolOutcome(f"Not added: {why}", ok=False, summary="sent back",
                               extra={"why": why.split("\n", 1)[0][:200]})
        if len(added) >= MAX_PER_SET:
            return back(f"a set has at most {MAX_PER_SET} problems. Finish with your reply.")
        question = str(args.get("question") or "").strip()
        kind = str(args.get("kind") or "").strip().lower()
        answer = str(args.get("answer") if args.get("answer") is not None else "").strip()
        if not question or len(question) > QUESTION_MAX:
            return back("give the problem in `question`.")
        if kind not in KINDS:
            return back(f"`kind` is one of {', '.join(KINDS)}.")
        if not answer:
            return back("give the correct answer in `answer`.")
        unit = str(args.get("unit") or "").strip()[:20] or None
        code = str(args.get("code") or "").strip()
        quote = str(args.get("quote") or "").strip()
        row = {"kind": kind, "question": question, "answer": answer, "unit": unit,
               "topic": str(args.get("topic") or "").strip()[:60],
               "solution": str(args.get("solution") or "").strip()[:8000],
               "source": str(args.get("source") or "").strip()[:200] or None,
               "quote": quote[:2000] or None, "choices": None, "value": None,
               "code": None, "code_output": None, "verified": None}
        if kind == "number":
            value = parse_number(answer, unit)
            if value is None:
                return back("a number problem's `answer` is a number or an exact expression "
                            "(16/5, 3*pi/4, sqrt(2), 1.2e-3), without words.")
            row["value"] = value
            why = await check_code(code, value, answer, row, box)
            if why:
                return back(why)
        elif kind == "choice":
            raw = args.get("choices")
            choices = [str(c).strip() for c in raw] if isinstance(raw, list) else []
            choices = [c for c in choices if c]
            if not 2 <= len(choices) <= MAX_CHOICES:
                return back(f"a choice problem has 2 to {MAX_CHOICES} options in `choices`.")
            letter = re.fullmatch(r"\(?([A-Fa-f])\)?\.?", answer)
            if letter and ord(letter.group(1).upper()) - 65 < len(choices):
                index = ord(letter.group(1).upper()) - 65
            else:
                index = next((i for i, c in enumerate(choices) if c.lower() == answer.lower()), None)
            if index is None:
                return back("`answer` must be one of the choices (or its letter, A for the first).")
            row.update(choices=choices, answer=choices[index], value=float(index))
            # a numeric right option is a computed key like any other
            numeric = parse_number(choices[index].strip("$ "))
            if numeric is not None:
                why = await check_code(code, numeric, choices[index], row, box)
                if why:
                    return back(why)
        if quote:
            found, _pages = quotes.find(quote, box.documents, box.seen)
            if not found:
                return back("`quote` isn't word for word in the materials. Copy the exact words "
                            "the answer rests on, or leave `quote` out.")
            # in a course without math, the materials are the check that matters
            row["verified"] = row["verified"] if quantitative and row["verified"] else "quote"
        row["n"] = len(added) + 1
        pid = await save(row)
        added.append(pid)
        box.practice.append(pid)
        checked = {"code": " Key checked: your code printed it.",
                   "quote": " Key checked: the quote is in the materials."}.get(row["verified"] or "", "")
        return ToolOutcome(f"Added problem {row['n']}.{checked}", summary=f"problem {row['n']}"
                           + (" · key checked" if row["verified"] else ""))

    return Tool(
        "add_problem",
        "Add one practice problem to the set the student will answer. Its answer stays hidden "
        "from them until they've tried. Call once per problem.",
        {"type": "object", "properties": {
            "question": {"type": "string", "description": "The problem, in Markdown (LaTeX math allowed)."},
            "topic": {"type": "string", "description": "A short topic label, e.g. 'Lagrange multipliers'."},
            "kind": {"type": "string", "enum": list(KINDS),
                     "description": "number: a numeric answer, graded automatically. choice: "
                                    "multiple choice. short: a written answer the student marks."},
            "answer": {"type": "string", "description": "number: the value or exact expression (16/5, "
                       "3*pi/4). choice: the correct option, or its letter. short: a model answer."},
            "choices": {"type": "array", "items": {"type": "string"}, "description": "choice only: the options."},
            "unit": {"type": "string", "description": "number only, optional: the unit, e.g. 'm/s'."},
            "solution": {"type": "string", "description": "The worked solution, shown after they answer."},
            "code": {"type": "string", "description": "number: Python that prints the answer. It is run."},
            "source": {"type": "string", "description": "Where in the materials, e.g. 'reader.pdf, p. 2'."},
            "quote": {"type": "string", "description": "The exact words in the materials the answer rests on."},
        }, "required": ["question", "kind", "answer"]},
        run,
        lambda a: "Writing problem" + (f": {a['topic']}" if a.get("topic") else ""),
    )


# --------------------------------------------------------------------------- #
# answering, from the cards
# --------------------------------------------------------------------------- #
_DONE = ("right", "revealed", "self_right", "self_wrong")


def _public(row, attempts: list) -> dict:
    """What the student sees: the key only once they're done with it."""
    outcomes = [a["outcome"] for a in attempts]
    status = next((o for o in reversed(outcomes) if o in _DONE), "wrong" if outcomes else "new")
    p = {"id": row["id"], "n": row["n"], "topic": row["topic"], "kind": row["kind"],
         "question": row["question"], "unit": row["unit"], "verified": row["verified"],
         "choices": json.loads(row["choices_json"]) if row["choices_json"] else None,
         "status": status, "tries": sum(1 for o in outcomes if o in ("right", "wrong"))}
    if status in _DONE:
        p.update(answer=row["answer"], solution=row["solution"], source=row["source"],
                 quote=row["quote"], code=row["code"], code_output=row["code_output"])
    return p


async def _owned(db: Database, pid: str, user_id: int):
    row = await db.fetch_one("SELECT * FROM practice_problems WHERE id = ? AND user_id = ?", (pid, user_id))
    if row is None:
        raise HTTPException(404, "no such problem")
    return row


async def _attempts(db: Database, pid: str) -> list:
    return await db.fetch_all("SELECT outcome, answer FROM practice_attempts WHERE problem_id = ? "
                              "ORDER BY id", (pid,))


async def _record(db: Database, row, user_id: int, outcome: str, answer: str | None = None) -> dict:
    await db.execute("INSERT INTO practice_attempts (problem_id, user_id, answer, outcome, created_at) "
                     "VALUES (?, ?, ?, ?, ?)", (row["id"], user_id, answer, outcome, now()))
    return _public(row, await _attempts(db, row["id"]))


class AnswerIn(BaseModel):
    answer: str = Field(max_length=2000)


class SelfMark(BaseModel):
    right: bool


@router.get("")
async def problems(ids: str, principal: Principal = Depends(get_principal),
                   db: Database = Depends(get_db)):
    want = [i for i in ids.split(",") if i][:50]
    if not want:
        return {"problems": []}
    rows = await db.fetch_all(
        f"SELECT * FROM practice_problems WHERE user_id = ? AND id IN ({','.join('?' * len(want))})",
        (principal.user_id, *want))
    out = {r["id"]: _public(r, await _attempts(db, r["id"])) for r in rows}
    return {"problems": [out[i] for i in want if i in out]}


@router.post("/{pid}/answer")
async def answer(pid: str, body: AnswerIn, principal: Principal = Depends(get_principal),
                 db: Database = Depends(get_db)):
    row = await _owned(db, pid, principal.user_id)
    given = body.answer.strip()
    if not given:
        raise HTTPException(400, "type an answer first")
    if row["kind"] == "number":
        ok = number_matches(given, row["answer_value"], row["unit"])
        if ok is None:
            raise HTTPException(400, "that isn't a number: type a value like 3.2, 16/5 or 3*pi/4")
    elif row["kind"] == "choice":
        choices = json.loads(row["choices_json"])
        ok = given == row["answer"] or given == str(choices.index(row["answer"]))
    else:
        # a written answer: shown the model answer, they mark it themselves
        return {**_public(row, await _attempts(db, pid)), "mark_yourself": True,
                "answer": row["answer"], "solution": row["solution"], "source": row["source"],
                "quote": row["quote"]}
    return await _record(db, row, principal.user_id, "right" if ok else "wrong", given)


@router.post("/{pid}/mark")
async def mark(pid: str, body: SelfMark, principal: Principal = Depends(get_principal),
               db: Database = Depends(get_db)):
    row = await _owned(db, pid, principal.user_id)
    if row["kind"] != "short":
        raise HTTPException(400, "only written answers are marked by hand")
    return await _record(db, row, principal.user_id, "self_right" if body.right else "self_wrong")


@router.post("/{pid}/reveal")
async def reveal(pid: str, principal: Principal = Depends(get_principal),
                 db: Database = Depends(get_db)):
    row = await _owned(db, pid, principal.user_id)
    return await _record(db, row, principal.user_id, "revealed")


@router.get("/progress")
async def progress(course_id: str, principal: Principal = Depends(get_principal),
                   db: Database = Depends(get_db)):
    """First-try results by topic for a course, and the problems still missed."""
    rows = await db.fetch_all(
        "SELECT p.id, p.topic, p.question, p.created_at,"
        " (SELECT outcome FROM practice_attempts a WHERE a.problem_id = p.id ORDER BY a.id LIMIT 1) AS first,"
        " (SELECT outcome FROM practice_attempts a WHERE a.problem_id = p.id ORDER BY a.id DESC LIMIT 1) AS last"
        " FROM practice_problems p WHERE p.user_id = ? AND p.course_id = ? ORDER BY p.created_at DESC",
        (principal.user_id, course_id))
    topics: dict[str, dict] = {}
    for r in rows:
        if r["first"] is None:
            continue
        t = topics.setdefault(r["topic"] or "Other", {"topic": r["topic"] or "Other", "tried": 0, "first_try": 0})
        t["tried"] += 1
        t["first_try"] += r["first"] in ("right", "self_right")
    missed = [{"id": r["id"], "topic": r["topic"], "question": r["question"][:140]}
              for r in rows if r["last"] in ("wrong", "revealed", "self_wrong")][:5]
    return {"topics": sorted(topics.values(), key=lambda t: t["first_try"] / t["tried"]),
            "missed": missed, "unanswered": sum(1 for r in rows if r["first"] is None)}


def new_id() -> str:
    return uuid.uuid4().hex
