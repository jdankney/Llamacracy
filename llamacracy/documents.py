# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document uploads: PDF, Word, OpenDocument, spreadsheets, slides, text.

Text is extracted once, at upload, and kept on disk as Markdown next to the
original. That work is CPU, not GPU, so like search it happens outside the
FIFO queue and costs no credits; the model only pays (in prompt-processing
seconds) for what it is actually sent.

Extraction runs in a child process (extract.py) with a wall-clock timeout
and its own memory/CPU caps: the web process never parses an untrusted file.

A document attached to a message stays part of that message. Every later turn
on the same branch sends it again, the way the message text itself is sent
again, so a follow-up question about page 3 still has page 3. (Images are
different: they're sent only with their own turn, see app.py.) Compaction
folds a document away along with the messages around it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("llamacracy.documents")

EXTRACT_TIMEOUT_S = 90.0
MAX_DOCS_PER_MESSAGE = 5
CHARS_PER_TOKEN = 3.6                   # the UI's estimate too (app.js estTok)

_ZIP = b"PK\x03\x04"
# extension -> (kind, magic bytes it must start with, if any)
_FORMATS: dict[str, tuple[str, bytes | None]] = {
    ".pdf": ("pdf", b"%PDF-"),
    ".docx": ("docx", _ZIP),
    ".xlsx": ("xlsx", _ZIP),
    ".xlsm": ("xlsx", _ZIP),
    ".pptx": ("pptx", _ZIP),
    ".odt": ("odt", _ZIP),
    ".ods": ("ods", _ZIP),
    ".odp": ("odp", _ZIP),
    ".txt": ("text", None),
    ".text": ("text", None),
    ".md": ("text", None),
    ".markdown": ("text", None),
    ".rst": ("text", None),
    ".log": ("text", None),
    ".csv": ("csv", None),
    ".tsv": ("tsv", None),
    ".html": ("html", None),
    ".htm": ("html", None),
}
# source code and config: kept verbatim in a fenced block
_CODE = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "jsx", ".json": "json", ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".conf": "", ".xml": "xml",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".fish": "fish", ".ps1": "powershell",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".java": "java", ".kt": "kotlin", ".go": "go", ".rs": "rust", ".rb": "ruby",
    ".php": "php", ".swift": "swift", ".lua": "lua", ".sql": "sql", ".r": "r",
    ".css": "css", ".scss": "scss", ".tex": "latex", ".bib": "bibtex",
    ".dockerfile": "dockerfile", ".env": "", ".gitignore": "",
}
# formats people will try that we can't read: say what to do instead
_LEGACY = {
    ".doc": "an old Word .doc", ".xls": "an old Excel .xls", ".ppt": "an old PowerPoint .ppt",
    ".rtf": "an .rtf", ".pages": "a Pages", ".numbers": "a Numbers", ".key": "a Keynote",
    ".epub": "an .epub",
}


class DocumentRejected(Exception):
    pass


@dataclass
class Extracted:
    markdown: str
    units: int | None          # pages / slides / sheets, where the format has them
    unit: str | None
    warnings: list[str]

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.markdown)


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def clean_filename(name: str | None) -> str:
    name = Path(name or "document").name
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip() or "document"
    return name[:120]


def classify(filename: str, head: bytes) -> str:
    """The extractor kind for an upload, from its name and first bytes. Raises
    DocumentRejected with a message meant for the person uploading."""
    ext = Path(filename).suffix.lower()
    if not ext and filename.lower() in ("dockerfile", "makefile"):
        return "code:" + ("dockerfile" if filename.lower() == "dockerfile" else "make")
    if ext in _LEGACY:
        raise DocumentRejected(
            f"{_LEGACY[ext]} file can't be read directly. Save it as PDF, .docx or .odt and attach that.")
    if ext in _FORMATS:
        kind, magic = _FORMATS[ext]
        if magic and not head.startswith(magic):
            raise DocumentRejected(f"this doesn't look like a real {ext} file")
        return kind
    if ext in _CODE:
        return "code:" + _CODE[ext]
    raise DocumentRejected(
        f"{ext or 'this kind of'} file isn't supported. Documents: PDF, Word (.docx), "
        "OpenDocument (.odt, .ods, .odp), Excel (.xlsx), PowerPoint (.pptx), "
        "text, Markdown, CSV, HTML and source code.")


async def extract(path: str, kind: str, max_chars: int) -> Extracted:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "llamacracy.extract", path, kind, str(max_chars),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(Path(__file__).resolve().parent.parent),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), EXTRACT_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise DocumentRejected("reading this file took too long, so it was stopped") from None
    try:
        res = json.loads(out.decode("utf-8", "replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        # killed by its own memory/CPU cap, or crashed outright
        log.warning("extractor died (rc=%s): %s", proc.returncode, err[-500:])
        raise DocumentRejected("this file couldn't be read (it needed too much memory or time)") from None
    if not res.get("ok"):
        raise DocumentRejected(res.get("error") or "this file couldn't be read")
    return Extracted(res["markdown"], res.get("units"), res.get("unit"), res.get("warnings") or [])


def save(upload_dir: str, data: bytes, filename: str, md: str) -> tuple[str, str, str]:
    """Original + extracted text under upload_dir. Returns (id, path, text_path).
    The original keeps only its extension: the name people gave it lives in
    the database and never touches the filesystem."""
    Path(upload_dir).mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex
    ext = re.sub(r"[^a-z0-9.]", "", Path(filename).suffix.lower())[:12]
    path = Path(upload_dir) / f"{file_id}{ext or '.bin'}"
    text_path = Path(upload_dir) / f"{file_id}.extracted.md"
    path.write_bytes(data)
    text_path.write_text(md, encoding="utf-8")
    return file_id, str(path), str(text_path)


def describe(row) -> dict:
    """What the UI shows about an attached document (never its text)."""
    meta = json.loads(row["meta_json"] or "{}")
    return {
        "id": row["id"],
        "filename": row["filename"],
        "bytes": row["bytes"],
        "tokens": meta.get("tokens"),
        "units": meta.get("units"),
        "unit": meta.get("unit"),
        "warnings": meta.get("warnings", []),
    }


def render_for_prompt(filename: str, text: str, meta: dict) -> str:
    """One document as the model sees it, ahead of the message it came with."""
    attrs = f'name="{filename.replace(chr(34), "")}"'
    if meta.get("units") and meta.get("unit"):
        attrs += f' {meta["unit"]}s="{meta["units"]}"'
    # a document that contains our closing tag must not be able to end itself
    body = text.replace("</document>", "</ document>")
    return f"<document {attrs}>\n{body}\n</document>\n\n"


async def load_prompt_blocks(db, upload_ids: list[str], user_id: int) -> dict[str, str]:
    """upload id -> its rendered block, for the documents this user owns."""
    if not upload_ids:
        return {}
    marks = ",".join("?" * len(upload_ids))
    rows = await db.fetch_all(
        f"SELECT id, filename, text_path, meta_json FROM uploads "
        f"WHERE user_id = ? AND kind = 'document' AND id IN ({marks})",
        (user_id, *upload_ids))
    out = {}
    for r in rows:
        try:
            text = Path(r["text_path"]).read_text(encoding="utf-8")
        except OSError:
            log.warning("extracted text missing for upload %s", r["id"])
            text = "(this document's text is no longer available)"
        out[r["id"]] = render_for_prompt(r["filename"], text, json.loads(r["meta_json"] or "{}"))
    return out


def doc_ids(message: dict) -> list[str]:
    raw = message.get("doc_ids_json")
    if not raw:
        return []
    try:
        ids = json.loads(raw)
    except ValueError:
        return []
    return [i for i in ids if isinstance(i, str)] if isinstance(ids, list) else []
