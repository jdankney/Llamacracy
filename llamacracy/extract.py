# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document -> Markdown, run in a child process: `python -m llamacracy.extract`.

Parsing someone else's file is the riskiest thing the server does with an
upload. PDFs and zip-based office files can be built to explode (decompression
bombs, entity expansion, pathological page trees), so this never runs inside
the web process. The parent (documents.py) starts it with a wall-clock
timeout; the child caps its own memory and CPU before it imports a single
parser, reads XML only through defusedxml, and refuses zip archives whose
contents would inflate past a fixed size. If anything goes wrong the child
dies alone and the upload is rejected with a plain message.

Protocol: argv = [path, kind, max_chars]; one JSON object on stdout:
  {"ok": true, "markdown": str, "units": int|null, "unit": "page"|..., "warnings": [...]}
  {"ok": false, "error": str}
"""

from __future__ import annotations

import csv
import io
import json
import re
import resource
import sys
import zipfile
from pathlib import PurePosixPath

MEM_LIMIT = 1024 * 1024 * 1024          # address space, bytes
CPU_LIMIT_S = 60
ZIP_MAX_TOTAL = 300 * 1024 * 1024        # sum of uncompressed member sizes
ZIP_MAX_MEMBERS = 20_000
MAX_PAGES = 3000                         # PDF pages / slides read at most
MAX_SHEET_ROWS = 5000
MAX_SHEET_COLS = 60


class Rejected(Exception):
    """A readable reason the file can't be used, shown to the user as is."""


def _limit_self() -> None:
    for res, val in ((resource.RLIMIT_AS, MEM_LIMIT), (resource.RLIMIT_CPU, CPU_LIMIT_S)):
        try:
            soft, hard = resource.getrlimit(res)
            cap = val if hard == resource.RLIM_INFINITY else min(val, hard)
            resource.setrlimit(res, (cap, hard))
        except (ValueError, OSError):
            pass


# --------------------------------------------------------------------------- #
# zip containers (docx, xlsx, pptx, odt, ods, odp)
# --------------------------------------------------------------------------- #
def _open_zip(path: str) -> zipfile.ZipFile:
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise Rejected("the file is damaged or isn't really that format") from None
    infos = z.infolist()
    if len(infos) > ZIP_MAX_MEMBERS or sum(i.file_size for i in infos) > ZIP_MAX_TOTAL:
        raise Rejected("the file unpacks to something far too large to read")
    return z


def _xml(z: zipfile.ZipFile, name: str):
    from defusedxml import ElementTree as DET
    try:
        data = z.read(name)
    except KeyError:
        return None
    return DET.fromstring(data)


def _cell(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().replace("|", "\\|")


def _table(rows: list[list[str]]) -> str:
    from llamacracy.htmltext import markdown_table
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    # drop columns that are empty in every row (merged cells, trailing blanks)
    keep = [i for i in range(width) if any(i < len(r) and r[i] for r in rows)]
    return markdown_table([[r[i] if i < len(r) else "" for i in keep] for r in rows])


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_heading_styles(z) -> dict[str, int]:
    """styleId -> heading level. Style ids are localised ("berschrift1"), the
    style *names* are not ("heading 1"), so map through styles.xml."""
    out: dict[str, int] = {}
    root = _xml(z, "word/styles.xml")
    if root is None:
        return out
    for st in root.iter(_W + "style"):
        sid = st.get(_W + "styleId") or ""
        name_el = st.find(_W + "name")
        name = (name_el.get(_W + "val") if name_el is not None else "") or ""
        m = re.fullmatch(r"heading (\d)", name.lower())
        if m:
            out[sid] = int(m.group(1))
        elif name.lower() == "title":
            out[sid] = 1
        else:
            lvl = st.find(f"{_W}pPr/{_W}outlineLvl")
            if lvl is not None and (lvl.get(_W + "val") or "").isdigit():
                out[sid] = int(lvl.get(_W + "val")) + 1
    return out


def _docx_par_text(p) -> str:
    parts = []
    for el in p.iter():
        if el.tag == _W + "t" and el.text:
            parts.append(el.text)
        elif el.tag == _W + "tab":
            parts.append("\t")
        elif el.tag in (_W + "br", _W + "cr"):
            parts.append("\n")
    return "".join(parts)


def _docx_block(el, headings, out: list[str]) -> None:
    if el.tag == _W + "p":
        text = _docx_par_text(el).strip()
        if not text:
            return
        ppr = el.find(_W + "pPr")
        style = ppr.find(_W + "pStyle") if ppr is not None else None
        level = headings.get(style.get(_W + "val")) if style is not None else None
        if level:
            out.append("#" * min(level, 6) + " " + text.replace("\n", " "))
        elif ppr is not None and ppr.find(_W + "numPr") is not None:
            ilvl = ppr.find(f"{_W}numPr/{_W}ilvl")
            depth = int(ilvl.get(_W + "val") or 0) if ilvl is not None else 0
            out.append("  " * depth + "- " + text)
        else:
            out.append(text)
    elif el.tag == _W + "tbl":
        rows = []
        for tr in el.iter(_W + "tr"):
            rows.append([_cell(" ".join(_docx_par_text(p) for p in tc.iter(_W + "p")))
                         for tc in tr.findall(_W + "tc")])
        t = _table(rows)
        if t:
            out.append(t)
    elif el.tag == _W + "sdt":           # content controls wrap ordinary blocks
        content = el.find(_W + "sdtContent")
        for child in (content if content is not None else []):
            _docx_block(child, headings, out)


def _docx(path: str) -> tuple[str, int | None, str | None]:
    z = _open_zip(path)
    root = _xml(z, "word/document.xml")
    if root is None:
        raise Rejected("this .docx has no document body")
    body = root.find(_W + "body")
    headings = _docx_heading_styles(z)
    out: list[str] = []
    for el in (body if body is not None else []):
        _docx_block(el, headings, out)
    return "\n\n".join(out), None, None


# --------------------------------------------------------------------------- #
# XLSX (openpyxl reads through defusedxml once it is installed)
# --------------------------------------------------------------------------- #
def _fmt_value(v) -> str:
    import datetime as dt
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, dt.datetime):
        return v.isoformat(sep=" ", timespec="minutes") if (v.hour or v.minute) else v.date().isoformat()
    if isinstance(v, (dt.date, dt.time)):
        return v.isoformat()
    return _cell(str(v))


def _xlsx(path: str) -> tuple[str, int | None, str | None]:
    import openpyxl
    _open_zip(path).close()              # size check before openpyxl unpacks anything
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        raise Rejected(f"couldn't open the workbook ({type(e).__name__})") from None
    out, warnings = [], []
    for ws in wb.worksheets:
        rows = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i >= MAX_SHEET_ROWS:
                warnings.append(f"sheet {ws.title!r} cut at {MAX_SHEET_ROWS} rows")
                break
            rows.append([_fmt_value(v) for v in row[:MAX_SHEET_COLS]])
        t = _table(rows)
        out.append(f"## Sheet: {ws.title}\n\n{t or '(empty)'}")
    wb.close()
    _WARN.extend(warnings)
    return "\n\n".join(out), len(wb.worksheets), "sheet"


# --------------------------------------------------------------------------- #
# PPTX
# --------------------------------------------------------------------------- #
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _pptx(path: str) -> tuple[str, int | None, str | None]:
    z = _open_zip(path)
    slides = sorted(
        (n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
        key=lambda n: int(re.search(r"(\d+)", PurePosixPath(n).name).group(1)))
    out = []
    for i, name in enumerate(slides[:MAX_PAGES], 1):
        root = _xml(z, name)
        paras = []
        for p in root.iter(_A + "p"):
            text = "".join(t.text or "" for t in p.iter(_A + "t")).strip()
            if text:
                paras.append(text)
        out.append(f"## Slide {i}\n\n" + ("\n\n".join(paras) or "(no text)"))
    return "\n\n".join(out), len(slides), "slide"


# --------------------------------------------------------------------------- #
# OpenDocument (odt, ods, odp)
# --------------------------------------------------------------------------- #
_ODF = "urn:oasis:names:tc:opendocument:xmlns"
_T = "{" + _ODF + ":text:1.0}"
_TB = "{" + _ODF + ":table:1.0}"
_O = "{" + _ODF + ":office:1.0}"
_D = "{" + _ODF + ":drawing:1.0}"


def _odf_text(el) -> str:
    """Text of an ODF paragraph-ish element: spaces (<text:s c=N>), tabs and
    line breaks are elements in ODF, not characters."""
    parts = [el.text or ""]
    for child in el:
        tag = child.tag
        if tag == _T + "s":
            parts.append(" " * int(child.get(_T + "c") or 1))
        elif tag == _T + "tab":
            parts.append("\t")
        elif tag == _T + "line-break":
            parts.append("\n")
        elif tag in (_T + "note", _O + "annotation"):
            pass                          # footnotes and comments: not the body
        else:
            parts.append(_odf_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _odf_rows(table, max_rows: int) -> tuple[list[list[str]], bool]:
    rows: list[list[str]] = []
    cut = False
    for row in table.iter(_TB + "table-row"):
        cells: list[str] = []
        for c in row:
            if c.tag not in (_TB + "table-cell", _TB + "covered-table-cell"):
                continue
            text = _cell(" ".join(_odf_text(p) for p in c.iter(_T + "p")))
            rep = int(c.get(_TB + "number-columns-repeated") or 1)
            # a sheet ends in one cell "repeated" to column 16384: only expand
            # repeats that carry something
            cells.extend([text] * (min(rep, MAX_SHEET_COLS) if text else min(rep, 1)))
            if len(cells) >= MAX_SHEET_COLS:
                break
        rep = int(row.get(_TB + "number-rows-repeated") or 1)
        for _ in range(min(rep, max_rows) if any(cells) else 1):
            rows.append(cells[:MAX_SHEET_COLS])
        if len(rows) >= max_rows:
            cut = True
            break
    while rows and not any(rows[-1]):
        rows.pop()
    return rows, cut


_S = "{" + _ODF + ":style:1.0}"


def _odf_heading_styles(z, content) -> dict[str, int]:
    """Paragraph style name -> heading level. Editors often write a heading as
    a plain <text:p> whose (automatic) style descends from "Heading 1" or
    "Title", so follow each style's parents until one of them says."""
    styles: dict[str, tuple[str | None, int | None]] = {}
    roots = [content, _xml(z, "styles.xml")]
    for root in roots:
        if root is None:
            continue
        for st in root.iter(_S + "style"):
            name = st.get(_S + "name")
            if not name:
                continue
            shown = (st.get(_S + "display-name") or name.replace("_20_", " ")).lower()
            lvl = st.get(_S + "default-outline-level")
            m = re.fullmatch(r"heading (\d)", shown)
            level = int(lvl) if (lvl or "").isdigit() else (
                int(m.group(1)) if m else 1 if shown == "title" else None)
            styles[name] = (st.get(_S + "parent-style-name"), level)

    def resolve(name: str | None, hops: int = 0) -> int | None:
        if not name or name not in styles or hops > 10:
            return None
        parent, level = styles[name]
        return level if level else resolve(parent, hops + 1)
    return {n: lv for n in styles if (lv := resolve(n))}


def _odf_block(el, out: list[str], depth: int = 0, headings: dict | None = None) -> None:
    tag = el.tag
    headings = headings or {}
    if tag == _T + "h" or (tag == _T + "p" and el.get(_T + "style-name") in headings):
        lvl = int(el.get(_T + "outline-level") or headings.get(el.get(_T + "style-name")) or 1)
        text = _odf_text(el).strip()
        if text:
            out.append("#" * min(lvl, 6) + " " + text.replace("\n", " "))
    elif tag == _T + "p":
        text = _odf_text(el).strip()
        if text:
            out.append(text)
    elif tag == _T + "list":
        for item in el.findall(_T + "list-item"):
            first = True
            for child in item:
                if child.tag == _T + "list":
                    _odf_block(child, out, depth + 1, headings)
                elif child.tag in (_T + "p", _T + "h"):
                    text = _odf_text(child).strip()
                    if text:
                        out.append("  " * depth + ("- " if first else "  ") + text)
                        first = False
    elif tag == _TB + "table":
        rows, _ = _odf_rows(el, MAX_SHEET_ROWS)
        t = _table(rows)
        if t:
            out.append(t)
    else:                                  # sections, frames, ...: walk into them
        for child in el:
            _odf_block(child, out, depth, headings)


def _odf(path: str, kind: str) -> tuple[str, int | None, str | None]:
    z = _open_zip(path)
    root = _xml(z, "content.xml")
    if root is None:
        raise Rejected("this file has no content.xml, so it isn't an OpenDocument file")
    body = root.find(_O + "body")
    if body is None:
        return "", None, None
    if kind == "ods":
        sheets = list(body.iter(_TB + "table"))
        out = []
        for sh in sheets:
            rows, cut = _odf_rows(sh, MAX_SHEET_ROWS)
            name = sh.get(_TB + "name") or "Sheet"
            if cut:
                _WARN.append(f"sheet {name!r} cut at {MAX_SHEET_ROWS} rows")
            out.append(f"## Sheet: {name}\n\n{_table(rows) or '(empty)'}")
        return "\n\n".join(out), len(sheets), "sheet"
    if kind == "odp":
        pages = list(body.iter(_D + "page"))
        out = []
        for i, pg in enumerate(pages[:MAX_PAGES], 1):
            paras = [_odf_text(p).strip() for p in pg.iter() if p.tag in (_T + "p", _T + "h")]
            out.append(f"## Slide {i}\n\n" + ("\n\n".join(p for p in paras if p) or "(no text)"))
        return "\n\n".join(out), len(pages), "slide"
    out: list[str] = []
    headings = _odf_heading_styles(z, root)
    for child in body:
        _odf_block(child, out, headings=headings)
    return "\n\n".join(out), None, None


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def _pdf(path: str) -> tuple[str, int | None, str | None]:
    import logging

    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
    logging.getLogger("pypdf").setLevel(logging.ERROR)   # malformed-but-readable noise
    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise Rejected("this PDF is password-protected")
        n = len(reader.pages)
    except (PdfReadError, ValueError, KeyError) as e:
        raise Rejected(f"couldn't read this PDF ({type(e).__name__})") from None
    out, empty = [], 0
    for i, page in enumerate(reader.pages):
        if i >= MAX_PAGES:
            _WARN.append(f"only the first {MAX_PAGES} pages were read")
            out.append(f"[The document goes on to page {n}; only the first {MAX_PAGES} "
                       "pages were read, so nothing after this is available.]")
            n = MAX_PAGES       # the pages there is text for
            break
        try:
            text = page.extract_text() or ""
        except Exception:  # noqa: BLE001 -- one bad page shouldn't sink the rest
            text = ""
            _WARN.append(f"page {i + 1} couldn't be read")
        text = re.sub(r"[ \t]+\n", "\n", text).strip()
        empty += len(text) < 10
        out.append(f"[Page {i + 1}]\n\n{text}" if text else f"[Page {i + 1}]")
    if out and empty > len(out) / 2:
        # mostly a scan: pictures of pages with no text layer behind them
        _WARN.append("no_text_layer")
    return "\n\n".join(out), n, "page"


# --------------------------------------------------------------------------- #
# plain text, HTML
# --------------------------------------------------------------------------- #
def _decode(path: str) -> str:
    data = open(path, "rb").read()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if b"\x00" in data[:8192]:
        raise Rejected("this looks like a binary file, not text")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _text(path: str, kind: str) -> tuple[str, int | None, str | None]:
    text = _decode(path).replace("\r\n", "\n")
    if kind in ("csv", "tsv"):
        # keep it as CSV: models read it fine and a Markdown table of the same
        # data costs half as many tokens again. Just normalise the dialect.
        try:
            rows = list(csv.reader(io.StringIO(text), delimiter="\t" if kind == "tsv" else ","))
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\n").writerows(rows)
            text = buf.getvalue()
        except csv.Error:
            pass
        return f"```csv\n{text.strip()}\n```", None, None
    if kind == "html":
        from llamacracy.htmltext import html_to_markdown
        title, md = html_to_markdown(text)
        return (f"# {title}\n\n{md}" if title and not md.startswith("# ") else md), None, None
    if kind.startswith("code:"):
        return f"```{kind[5:]}\n{text.strip()}\n```", None, None
    return text.strip(), None, None


# --------------------------------------------------------------------------- #
_WARN: list[str] = []


def extract(path: str, kind: str) -> tuple[str, int | None, str | None]:
    if kind == "pdf":
        return _pdf(path)
    if kind == "docx":
        return _docx(path)
    if kind == "xlsx":
        return _xlsx(path)
    if kind == "pptx":
        return _pptx(path)
    if kind in ("odt", "ods", "odp"):
        return _odf(path, kind)
    return _text(path, kind)


def main() -> None:
    _limit_self()
    path, kind, max_chars = sys.argv[1], sys.argv[2], int(sys.argv[3])
    try:
        md, units, unit = extract(path, kind)
    except Rejected as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        return
    except MemoryError:
        print(json.dumps({"ok": False, "error": "the file needs too much memory to read"}))
        return
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"couldn't read this file ({type(e).__name__})"}))
        return
    md = re.sub(r"\n{3,}", "\n\n", md).strip()
    if len(md) > max_chars:
        # said in the text itself: the model reads this, not the warnings
        md = (md[:max_chars] + "\n\n[The document is cut off here: it was too long to read "
              "in full, so nothing after this point is available.]")
        _WARN.append("truncated")
    print(json.dumps({"ok": True, "markdown": md, "units": units, "unit": unit,
                      "warnings": _WARN}))


if __name__ == "__main__":
    main()
