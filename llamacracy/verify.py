# SPDX-License-Identifier: AGPL-3.0-or-later
"""Did the numbers in an answer come from the code the model ran?

After a turn that used run_python, every number in the answer is looked up
in what the code printed (and in what the user wrote and the constants the
code was written with, since repeating a given isn't a claim). A number
that appears in none of these was worked out some other way, most likely in
the model's head, and the reply says so. This is the server's check, not
the model's say-so: the model can't mark its own answer as verified.

It is deliberately modest. It checks that numbers were computed, not that
the code computed the right thing (a misread problem is solved correctly
and wrongly at once). It reads exact fractions (16/5, \\frac{10}{3}), as
values or as reciprocals (1/C = 5/12 for C = 2.4), and multiples of π, and
a result restated in another SI prefix (1.87e-7 s as 187 ns), but no other
symbolic forms. What it leaves alone:

- small whole numbers (0-10), small fractions (both parts at most 10),
  small multiples of π (2π) and years, which are step numbers, exponents,
  list items, formula constants and dates as often as they are results:
  never flagged, and only the fractions count when the code printed them;
- hand working on an equation line whose result the code did print
  (`1024(1 + 10) = 1024 × 11 = 11264`): the 11 isn't a claim of its own;
  nor is it when it multiplies or adds exactly into a printed result.
"""

from __future__ import annotations

import math
import re

_FENCE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_CITE = re.compile(r"\[\d{1,3}\](?![(:])")
_LIST_MARK = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
# "Step 2", "Day 3", "page 14": labels, not results
# (and their plurals and ranges: "Sections 15.1–15.3", "pages 4-7")
_LABEL = re.compile(r"\b(?:step|part|day|section|chapter|page|problem|question|example|exercise|"
                    r"figure|table|equation|eq\.|case|method|way|round|option|item|line|hw|lab|week|unit)s?"
                    r"\s*#?\s*\d+(?:\.\d+)*[a-z]?(?:\s*(?:[–-]|to)\s*\d+(?:\.\d+)*[a-z]?)?\b",
                    re.IGNORECASE)
# 1.2 × 10^{-3}, 1.2\times10^{-3}, 1.2 x 10^-3, 1.2·10^-3
_SCI = re.compile(r"(?<![\w.])([-−]?\d+(?:\.\d+)?)\s*(?:×|\\times|\\cdot|·|x|\*)\s*10\s*\^\s*\{?\s*([-−]?\d+)\s*\}?")
# \frac{10}{3}, \dfrac{10}{3}, 16/5
_FRAC = re.compile(r"(?<![\w.])([-−]?)\\[dt]?frac\s*\{\s*(\d+)\s*\}\s*\{\s*(\d+)\s*\}"
                   r"|(?<![\w./])([-−]?\d+)\s*/\s*(\d+)(?![\w.]|\s*[/^])")
# 8π, 8\pi
_PI = re.compile(r"(?<![\w.])([-−]?\d+(?:\.\d+)?)\s*(?:\\pi\b|π)")
_NUM = re.compile(r"(?<![\w.^])[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w])")
_OUT_NUM = re.compile(r"[-−]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
# what sympy prints: 16/5, 8*pi, 3*pi/4
_OUT_FRAC = re.compile(r"(?<![\w.*/])(-?\d+)/(\d+)(?![\d.])")
_OUT_PI = re.compile(r"(?<![\w.])(-?\d+(?:\.\d+)?)\*pi\b(?:/(\d+))?")
# a line of working: split on newlines and LaTeX row breaks
_ROW = re.compile(r"\n|\\\\")
_EXACT = 1e-9


def _value(s: str) -> float:
    return float(s.replace("−", "-").replace(",", ""))


def _decimals(s: str) -> int:
    s = s.split("e")[0].split("E")[0]
    return len(s.split(".", 1)[1]) if "." in s else 0


def _blank(m: re.Match) -> str:
    """Remove a match but keep every later position where it was."""
    return " " * len(m.group(0))


_SUP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺", "0123456789-+")
_SUP_RUN = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺]+")
# 11 264 with a thin or narrow no-break space between the groups (SI style)
_SPACED = re.compile(r"(?<=\d)[\u2009\u202f\u00a0](?=\d{3}(?!\d))")


def _normalize(text: str) -> str:
    """Numbers written the typographic way, as the patterns read them:
    10²³ as 10^23 (else 6.02 × 10²³ reads as 6.02 and 10), and SI thousands
    groups joined (11 264 is one number, not 11 and 264)."""
    text = _SUP_RUN.sub(lambda m: "^" + m.group(0).translate(_SUP), text)
    return _SPACED.sub("", text)


def _numbers(text: str) -> list[tuple[str, float, float, bool, int]]:
    """(as written, value, tolerance, small, position) for each number."""
    text = _normalize(text)
    text = _FENCE.sub(_blank, text)
    text = _CITE.sub(_blank, text)
    text = _LIST_MARK.sub(_blank, text)
    text = _LABEL.sub(_blank, text)
    found = []
    for m in _SCI.finditer(text):
        mant, exp = m.group(1), int(m.group(2).replace("−", "-"))
        found.append((m.group(0), _value(mant) * 10 ** exp, 0.5 * 10 ** (exp - _decimals(mant)),
                      False, m.start()))
    text = _SCI.sub(_blank, text)
    for m in _FRAC.finditer(text):
        sign, a, b = (m.group(1), m.group(2), m.group(3)) if m.group(2) else ("", m.group(4), m.group(5))
        num, den = int(a.replace("−", "-")), int(b)
        if den == 0:
            continue
        v = (-1 if sign else 1) * num / den
        found.append((m.group(0), v, _EXACT * max(1.0, abs(v)), abs(num) <= 10 and den <= 10,
                      m.start()))
    text = _FRAC.sub(_blank, text)
    for m in _PI.finditer(text):
        k = m.group(1)
        v = _value(k) * math.pi
        tol = _EXACT * max(1.0, abs(v)) if "." not in k else 0.5 * 10 ** -_decimals(k) * math.pi
        small = "." not in k and abs(int(k.replace("−", "-"))) <= 10    # 2π in a formula
        found.append((m.group(0), v, tol, small, m.start()))
    text = _PI.sub(_blank, text)
    for m in _NUM.finditer(text):
        s = m.group(0)
        v = _value(s)
        small = float(v).is_integer() and (abs(v) <= 10 or (1900 <= v <= 2100 and "," not in s))
        found.append((s, v, 0.5 * 10 ** -_decimals(s), small, m.start()))
    return sorted(found, key=lambda f: f[4])


def answer_numbers(text: str) -> list[tuple[str, float, float]]:
    """(as written, value, tolerance) for each number an answer states."""
    return [(s, v, t) for s, v, t, _, _ in _numbers(text)]


def text_numbers(text: str) -> list[float]:
    """Every number in some output or message, loosely, with the exact
    fractions and multiples of π that sympy prints as values too."""
    text = text.replace(",", "")
    out = []
    for m in _OUT_NUM.finditer(text):
        try:
            out.append(_value(m.group(0)))
        except ValueError:
            continue
    for m in _OUT_FRAC.finditer(text):
        if int(m.group(2)):
            out.append(int(m.group(1)) / int(m.group(2)))
    for m in _OUT_PI.finditer(text):
        out.append(float(m.group(1)) * math.pi / (int(m.group(2)) if m.group(2) else 1))
    if re.search(r"(?<![\w*])pi\b", text):
        out.append(math.pi)
    return out


# a result restated in another unit: an SI prefix (187 ns for 1.87e-7 s)
_SI = tuple(10.0 ** (3 * k) for k in (-4, -3, -2, -1, 1, 2, 3, 4))


def _matches(value: float, tol: float, pool: list[float]) -> bool:
    a = abs(value)
    for o in pool:
        # "fell by 4.2" from a printed -4.2 is fine; a "-4.2" the code never
        # printed with that sign is a sign error, not a match
        if value < 0 < o:
            continue
        o = abs(o)
        # written rounded from o, or o itself; or as a percentage (or in cm)
        if abs(o - a) <= tol * 1.000001 or abs(o * 100 - a) <= tol * 1.000001:
            return True
        # in another prefix, and close too: 11 isn't 11264 in thousands
        if any(abs(o * s - a) <= tol * 1.000001 and abs(o * s - a) <= 0.005 * a for s in _SI):
            return True
        # rounded to significant figures ("about 1,230" from 1234.5)
        if o and a and abs(o - a) / max(o, a) < 0.005 and _sig_rounded(a):
            return True
    return False


def _sig_rounded(a: float) -> bool:
    """Whether a looks like a rounded figure (trailing zeros, e.g. 1,200)."""
    return a >= 100 and float(a).is_integer() and int(a) % 10 == 0


def _working(answer: str, found: list, verdicts: list[tuple[int, bool | None]],
             computed: list[float]) -> set[int]:
    """Positions of numbers that are working toward a checked result: on an
    equation line whose last side holds only numbers the code printed, or
    combining exactly with another number on their line into one it printed
    (the 11 in `1024 · 11`, with 11264 on the next line)."""
    rows = [0] + [m.end() for m in _ROW.finditer(answer)] + [len(answer) + 1]
    results = [abs(c) for c in computed if abs(c) > 10]
    exempt = set()
    for lo, hi in zip(rows, rows[1:], strict=False):
        last_eq = answer.rfind("=", lo, hi)
        if last_eq >= 0:
            tail = [ok for pos, ok in verdicts if last_eq < pos < hi and ok is not None]
            if tail and all(tail):
                exempt |= {pos for pos, ok in verdicts if lo <= pos < last_eq and ok is False}
        row = [(f[1], f[4], ok) for f, (_, ok) in zip(found, verdicts, strict=True) if lo <= f[4] < hi]
        for x, pos, ok in row:
            if ok is False and pos not in exempt and any(
                    abs(abs(r) - c) <= _EXACT * c
                    for y, p, _ in row if p != pos for r in (x * y, x + y) for c in results):
                exempt.add(pos)
    return exempt


def check(answer: str, outputs: list[str], givens: list[str]) -> dict | None:
    """{"matched": n, "unmatched": [...]} for an answer after run_python ran
    (n counts distinct numbers, as written), or None when there's nothing to
    say (no numbers)."""
    computed = [v for o in outputs for v in text_numbers(o)]
    given = [v for g in givens for v in text_numbers(g)]
    found = _numbers(answer)
    # an exact fraction may be a reciprocal: 1/C = 5/12 for C = 2.4
    inv_computed = [1 / v for v in computed if v]
    inv_given = [1 / v for v in given if v]
    # per number: True came from the code, False from nowhere, None not a claim
    verdicts = []
    for shown, value, tol, small, pos in found:
        exact = "/" in shown or "frac" in shown
        if _matches(value, tol, computed) or exact and _matches(value, tol, inv_computed):
            verdicts.append((pos, True))
        elif small or _matches(value, tol, given) or exact and _matches(value, tol, inv_given):
            verdicts.append((pos, None))
        else:
            verdicts.append((pos, False))
    working = _working(answer, found, verdicts, computed)
    matched, unmatched = set(), []
    for (shown, value, _tol, small, pos), (_, ok) in zip(found, verdicts, strict=True):
        if ok and not (small and float(value).is_integer()):
            matched.add(shown)
        elif ok is False and pos not in working and shown not in unmatched:
            unmatched.append(shown)
    if not matched and not unmatched:
        return None
    return {"matched": len(matched), "unmatched": unmatched[:8]}
