# SPDX-License-Identifier: AGPL-3.0-or-later
"""HTML -> compact Markdown, stdlib only.

Used for uploaded .html documents and for web pages the model reads. The goal
is what a person would read, in as few tokens as possible: headings, prose,
lists, tables, code and links survive; scripts, styles, navigation, forms and
page chrome do not. It is a tolerant scanner over html.parser, not a DOM, so
broken markup degrades to slightly worse text instead of an exception.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

# never text a reader wants
_SKIP = {"script", "style", "noscript", "template", "svg", "canvas", "iframe",
         "head", "title", "select", "button", "object", "embed"}
# page chrome: dropped from web pages, kept for documents (where a <nav> is
# more likely to be a table of contents someone wrote on purpose)
_CHROME = {"nav", "footer", "aside", "form", "header"}
_BLOCK = {"p", "div", "section", "article", "main", "blockquote", "figure",
          "figcaption", "dl", "dt", "dd", "address", "details", "summary",
          "center", "fieldset", "hr"}
_HEADINGS = {f"h{i}": i for i in range(1, 7)}
_VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col",
         "embed", "source", "track", "wbr", "param"}


class _Converter(HTMLParser):
    def __init__(self, base_url: str | None, drop_chrome: bool):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.drop = _SKIP | (_CHROME if drop_chrome else set())
        self.out: list[str] = []
        self.skip_depth = 0
        self.skip_tag = ""
        self.pre = 0
        self.lists: list[list] = []        # [kind, counter] per open list
        self.link: list[str | None] = []   # href per open <a>
        self.link_start: list[int] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.table_rows: list[list[str]] | None = None
        self.title = ""
        self._in_title = False

    # -- output helpers ---------------------------------------------------
    def _emit(self, s: str) -> None:
        if self.cell is not None:
            self.cell.append(s)
        else:
            self.out.append(s)

    def _newline(self, n: int = 2) -> None:
        if self.cell is not None:
            self.cell.append(" ")
            return
        self.out.append("\n" * n)

    # -- parser callbacks ---------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        if self.skip_depth:
            # skipping a subtree: only count nesting of the tag that opened it
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        a = dict(attrs)
        if tag in self.drop or "hidden" in a or a.get("aria-hidden") == "true":
            if tag not in _VOID:
                self.skip_tag = tag
                self.skip_depth = 1
            return
        if tag in _HEADINGS:
            self._newline()
            self._emit("#" * _HEADINGS[tag] + " ")
        elif tag in _BLOCK:
            self._newline()
            if tag == "blockquote":
                self._emit("> ")
            elif tag == "hr":
                self._emit("---")
                self._newline()
        elif tag == "br":
            self._newline(1)
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
            self._newline(1)
        elif tag == "li":
            self._newline(1)
            depth = max(0, len(self.lists) - 1)
            if self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                self._emit("  " * depth + f"{self.lists[-1][1]}. ")
            else:
                self._emit("  " * depth + "- ")
        elif tag == "pre":
            self.pre += 1
            self._newline()
            self._emit("```\n")
        elif tag == "code" and not self.pre:
            self._emit("`")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "a":
            href = a.get("href") or ""
            if self.base_url:
                href = urljoin(self.base_url, href)
            self.link.append(href if href.startswith(("http://", "https://")) else None)
            self.link_start.append(len(self.cell if self.cell is not None else self.out))
        elif tag == "img":
            alt = (a.get("alt") or "").strip()
            if alt:
                self._emit(f"[image: {alt}]")
        elif tag == "table":
            self.table_rows = []
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.cell = []

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if self.skip_depth:
            if tag == self.skip_tag:
                self.skip_depth -= 1
            return
        if tag in _HEADINGS or tag in _BLOCK:
            self._newline()
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._newline(1)
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self._emit("\n```")
            self._newline()
        elif tag == "code" and not self.pre:
            self._emit("`")
        elif tag in ("strong", "b"):
            self._emit("**")
        elif tag in ("em", "i"):
            self._emit("*")
        elif tag == "a" and self.link:
            href = self.link.pop()
            start = self.link_start.pop()
            buf = self.cell if self.cell is not None else self.out
            text = "".join(buf[start:]).strip()
            if href and text and text != href:
                del buf[start:]
                buf.append(f"[{text}]({href})")
        elif tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip().replace("|", "\\|"))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.table_rows is not None and any(self.row):
                self.table_rows.append(self.row)
            self.row = None
        elif tag == "table" and self.table_rows is not None:
            rows, self.table_rows = self.table_rows, None
            self.out.append("\n\n" + markdown_table(rows) + "\n\n")

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()
        if self.skip_depth:
            return
        if self.pre:
            self._emit(data)
        else:
            self._emit(re.sub(r"\s+", " ", data))


def markdown_table(rows: list[list[str]]) -> str:
    """Rows of cell strings -> a Markdown table, the first row as the header."""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def tidy(text: str) -> str:
    """Trim trailing spaces and collapse runs of blank lines."""
    lines = [ln.rstrip() for ln in text.splitlines()]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # lines that are only markup left behind by emptied elements
    text = re.sub(r"(?m)^(?:\*\*|\*|`|>|- )\s*$\n?", "", text)
    return text.strip()


def html_to_markdown(html: str, *, base_url: str | None = None,
                     drop_chrome: bool = False) -> tuple[str, str]:
    """Returns (title, markdown)."""
    conv = _Converter(base_url, drop_chrome)
    try:
        conv.feed(html)
        conv.close()
    except Exception:  # noqa: BLE001 -- html.parser is tolerant; this is belt and braces
        pass
    body = "".join(conv.out)
    return conv.title, tidy(body)


_REGION_RE = re.compile(r"<(main|article)\b[^>]*>(.*?)</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def page_to_markdown(html: str, url: str) -> tuple[str, str]:
    """A web page as (title, markdown) for a model to read: page chrome
    dropped, and when the page marks its main content (<main>, <article>)
    and that is most of the page's text, just that."""
    title, full = html_to_markdown(html, base_url=url, drop_chrome=True)
    regions = [m.group(0) for m in _REGION_RE.finditer(html)]
    if regions:
        best = max(regions, key=lambda r: len(_TAG_RE.sub("", r)))
        _, main = html_to_markdown(best, base_url=url, drop_chrome=True)
        # a <main> that holds a fraction of the text is usually a wrapper
        # around one widget, not the article: keep the whole page then
        if len(main) >= 0.3 * len(full):
            full = main
    # linked icons and badges ("[[image: Edit]](...)") are noise to a reader
    full = re.sub(r"\[\[image: [^\]]*\]\]\([^)]*\)", "", full)
    return title, tidy(full)
