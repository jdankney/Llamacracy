# SPDX-License-Identifier: AGPL-3.0-or-later
"""Are the quotations in an answer really in the sources?

The counterpart of verify.py for words. After a turn that had documents or
read anything, every quotation in the answer is looked for, word for word,
in what the model could see: the documents (a course's materials, files
attached to the chat) and whatever its tools returned (passages it read,
pages it fetched, search results), plus what the user wrote. A quotation
that isn't there was paraphrased at best and invented at worst, and the
reply says so. When a quotation cites a page ("p. 7") and the words are
on a different page of that document, the reply says that too.

What counts as a quotation: words in double quotes (straight or curly)
running to MIN_WORDS or more, and Markdown blockquotes. Shorter quoted bits
are terms and titles, not claims about what a source says. Matching
ignores case, punctuation, Markdown emphasis and line breaks, joins words a
PDF split with a hyphen, and lets an ellipsis or a [bracketed insertion]
stand for left-out words, the way quotations are normally edited. It is
still word for word: a changed or added word is a miss.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MIN_WORDS = 5
MAX_REPORTED = 6

_FENCE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_CODE = re.compile(r"`[^`\n]*`")
_MATH = re.compile(r"\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)", re.DOTALL)
_QUOTED = re.compile(r"“([^”\n]{3,1200})”|\"([^\"\n]{3,1200})\"")
_BLOCK = re.compile(r"(?:^[ \t]*>[^\n]*(?:\n|$))+", re.MULTILINE)
_ATTRIB = re.compile(r"^\s*(?:[—–]|--?)\s*\S")          # "> — syllabus.pdf, p. 3"
_GAP = re.compile(r"\.\.\.|…|\[[^\]\n]{0,40}\]")         # ellipses and [insertions]
_PAGE_REF = re.compile(r"\b(?:pp?|pg|page)\.?\s*(\d{1,4})\b", re.IGNORECASE)
# before a quotation, only "on page 7" / "from p. 7": a bare "(p. 7)" there
# belongs to the quotation before it
_PAGE_LEAD = re.compile(r"\b(?:on|from|at|in)\s+(?:pp?|pg|page)\.?\s*(\d{1,4})\b", re.IGNORECASE)
_PAGE_MARK = re.compile(r"\[Page (\d+)\]")
_SOFT = re.compile(r"(\w)-[ \t]*\n[ \t]*(\w)")


@dataclass
class Quote:
    text: str
    page: int | None        # the page the answer says it's from, if it says


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower().replace("\u00ad", "")   # soft hyphens
    return " " + " ".join(re.sub(r"[\W_]+", " ", s).split()) + " "


def _words(s: str) -> int:
    return len(_norm(s).split())


def _cited_page(text: str, start: int, end: int) -> int | None:
    """A page the answer gives for the quotation that spans text[start:end]:
    just after it on the same line ("…" (p. 7)), else just before it ("on
    page 7 it says …")."""
    after = text[end:end + 80].split("\n", 1)[0]
    after = re.split(r"[“\"]", after, maxsplit=1)[0]        # not the next quotation's
    m = _PAGE_REF.search(after)
    if m:
        return int(m.group(1))
    before = text[max(0, start - 60):start].rsplit("\n", 1)[-1]
    before = re.split(r"[”\"]", before)[-1]
    found = list(_PAGE_LEAD.finditer(before))
    return int(found[-1].group(1)) if found else None


def extract(answer: str) -> list[Quote]:
    """The quotations an answer makes, with the page it cites for each."""
    blank = lambda m: " " * len(m.group(0))  # noqa: E731 -- keep every position
    text = _MATH.sub(blank, _CODE.sub(blank, _FENCE.sub(blank, answer)))
    quotes: list[Quote] = []
    for m in _BLOCK.finditer(text):
        lines = [re.sub(r"^[ \t]*>[ \t]?", "", ln) for ln in m.group(0).splitlines()]
        body = [ln for ln in lines if not _ATTRIB.match(ln)]
        attrib = " ".join(ln for ln in lines if _ATTRIB.match(ln))
        q = " ".join(body).strip()
        # a blockquote that is itself one quoted sentence: drop its quote marks
        q = re.sub(r'^[“"](.*)[”"]$', r"\1", q)
        if _words(re.sub(_GAP, " ", q)) >= MIN_WORDS:
            page = _PAGE_REF.search(attrib)
            quotes.append(Quote(q, int(page.group(1)) if page else _cited_page(text, m.start(), m.end() - 1)))
    text = _BLOCK.sub(blank, text)
    for m in _QUOTED.finditer(text):
        q = m.group(1) or m.group(2)
        if _words(re.sub(_GAP, " ", q)) >= MIN_WORDS:
            quotes.append(Quote(q.strip(), _cited_page(text, m.start(), m.end())))
    return quotes


@dataclass
class _Source:
    name: str
    pages: list[tuple[int | None, list[str]]]     # (page number, its normalized forms)


def _forms(text: str) -> list[str]:
    """A text normalized two ways: with words a line-end hyphen split joined
    back up (exam-/ple), and as printed (well-/known)."""
    joined = _norm(_SOFT.sub(r"\1\2", text))
    plain = _norm(text)
    return [joined] if joined == plain else [joined, plain]


def _source(name: str, text: str) -> _Source:
    parts = _PAGE_MARK.split(text)
    pages: list[tuple[int | None, list[str]]] = []
    if len(parts) > 1:
        pages = [(int(parts[i]), _forms(parts[i + 1])) for i in range(1, len(parts) - 1, 2)]
    # the whole text too: for text before the first mark, and quotations
    # running across a page break
    pages.append((None, _forms(text)))
    return _Source(name, pages)


def _in(fragments: list[str], hay: str) -> bool:
    pos = 0
    for f in fragments:
        i = hay.find(f, pos)
        if i < 0:
            return False
        pos = i + len(f) - 1
    return True


def _fragments(quote: str) -> list[str]:
    frags = [_norm(f) for f in _GAP.split(quote)]
    return [f for f in frags if f.strip()]


def _short(q: str, n: int = 90) -> str:
    q = " ".join(q.split())
    return q if len(q) <= n else q[:n - 1].rstrip() + "…"


def check(answer: str, documents: list[tuple[str, str]], seen: list[str]) -> dict | None:
    """{"found": n, "missing": [...], "wrong_page": [...]} for the answer's
    quotations, looked for in `documents` ((name, text), page-marked where
    the format has pages) and in `seen` (everything else the model was
    shown), or None when it quotes nothing."""
    quotes = extract(answer)
    if not quotes:
        return None
    docs = [_source(n, t) for n, t in documents if t]
    other = [form for s in seen if s for form in _forms(s)]
    found, missing, wrong = 0, [], []
    for q in quotes:
        frags = _fragments(q.text)
        pages_hit: set[int] = set()
        whole_hit = False
        for d in docs:
            for page, forms in d.pages:
                if any(_in(frags, f) for f in forms):
                    if page is None:
                        whole_hit = True
                    else:
                        pages_hit.add(page)
        if not (pages_hit or whole_hit or any(_in(frags, f) for f in other)):
            missing.append(_short(q.text))
            continue
        found += 1
        # only a quotation found on particular pages can be on the wrong one
        if q.page is not None and pages_hit and q.page not in pages_hit:
            wrong.append({"quote": _short(q.text, 60), "cited": q.page, "pages": sorted(pages_hit)})
    return {"found": found, "missing": missing[:MAX_REPORTED], "wrong_page": wrong[:MAX_REPORTED]}
