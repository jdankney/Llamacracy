# SPDX-License-Identifier: AGPL-3.0-or-later
"""Did the numbers in an answer come from the code the model ran?

After a turn that used run_python, every number in the answer is looked up
in what the code printed (and in what the user wrote, since repeating a
given isn't a claim). A number that appears in neither was worked out some
other way, most likely in the model's head, and the reply says so. This is
the server's check, not the model's say-so: the model can't mark its own
answer as verified.

It is deliberately modest. It checks that numbers were computed, not that
the code computed the right thing (a misread problem is solved correctly
and wrongly at once), and it doesn't read symbolic answers (π/4 is two
numbers to it). Small whole numbers (0-10) and years are left out, neither
counted nor flagged: they are step numbers, exponents, list items and dates
as often as they are results.
"""

from __future__ import annotations

import re

_FENCE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_CITE = re.compile(r"\[\d{1,3}\](?![(:])")
_LIST_MARK = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
# "Step 2", "Day 3", "page 14": labels, not results
_LABEL = re.compile(r"\b(?:step|part|day|section|chapter|page|problem|question|example|exercise|"
                    r"figure|table|equation|eq\.|case|method|way|round|option|item|line|hw|lab)"
                    r"\s*#?\s*\d+(?:\.\d+)*[a-z]?\b", re.IGNORECASE)
# 1.2 × 10^{-3}, 1.2\times10^{-3}, 1.2 x 10^-3, 1.2·10^-3
_SCI = re.compile(r"(?<![\w.])([-−]?\d+(?:\.\d+)?)\s*(?:×|\\times|\\cdot|·|x|\*)\s*10\s*\^\s*\{?\s*([-−]?\d+)\s*\}?")
_NUM = re.compile(r"(?<![\w.^])[-−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?(?![\w])")
_OUT_NUM = re.compile(r"[-−]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _value(s: str) -> float:
    return float(s.replace("−", "-").replace(",", ""))


def _decimals(s: str) -> int:
    s = s.split("e")[0].split("E")[0]
    return len(s.split(".", 1)[1]) if "." in s else 0


def answer_numbers(text: str) -> list[tuple[str, float, float]]:
    """(as written, value, tolerance) for each number an answer states."""
    text = _FENCE.sub(" ", text)
    text = _CITE.sub(" ", text)
    text = _LIST_MARK.sub(" ", text)
    text = _LABEL.sub(" ", text)
    found = []
    for m in _SCI.finditer(text):
        mant, exp = m.group(1), int(m.group(2).replace("−", "-"))
        found.append((m.group(0), _value(mant) * 10 ** exp, 0.5 * 10 ** (exp - _decimals(mant))))
    text = _SCI.sub(" ", text)
    for m in _NUM.finditer(text):
        s = m.group(0)
        found.append((s, _value(s), 0.5 * 10 ** -_decimals(s)))
    return found


def text_numbers(text: str) -> list[float]:
    """Every number in some output or message, loosely."""
    out = []
    for m in _OUT_NUM.finditer(text.replace(",", "")):
        try:
            out.append(_value(m.group(0)))
        except ValueError:
            continue
    return out


def _matches(value: float, tol: float, pool: list[float]) -> bool:
    a = abs(value)
    for o in pool:
        o = abs(o)
        # written rounded from o, or o itself; or a percentage of a fraction
        if abs(o - a) <= tol * 1.000001 or abs(o * 100 - a) <= tol * 1.000001:
            return True
        # rounded to significant figures ("about 1,230" from 1234.5)
        if o and a and abs(o - a) / max(o, a) < 0.005 and _sig_rounded(a):
            return True
    return False


def _sig_rounded(a: float) -> bool:
    """Whether a looks like a rounded figure (trailing zeros, e.g. 1,200)."""
    return a >= 100 and float(a).is_integer() and int(a) % 10 == 0


def check(answer: str, outputs: list[str], givens: list[str]) -> dict | None:
    """{"matched": n, "unmatched": [...]} for an answer after run_python ran,
    or None when there's nothing to say (no numbers)."""
    computed = [v for o in outputs for v in text_numbers(o)]
    given = [v for g in givens for v in text_numbers(g)]
    matched, unmatched = 0, []
    for shown, value, tol in answer_numbers(answer):
        small = float(value).is_integer() and (abs(value) <= 10 or   # or a year:
                                               (1900 <= value <= 2100 and "," not in shown))
        if small:
            continue            # neither a claim to credit nor one to flag
        if _matches(value, tol, computed):
            matched += 1
        elif _matches(value, tol, given):
            continue
        elif shown not in unmatched:
            unmatched.append(shown)
    if not matched and not unmatched:
        return None
    return {"matched": matched, "unmatched": unmatched[:8]}
