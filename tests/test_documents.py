# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document extraction: every format through the real child process, plus
the refusals (legacy formats, fakes, scans, zip bombs)."""

from __future__ import annotations

import pytest

from llamacracy import documents, extract
from llamacracy.htmltext import html_to_markdown
from tests import docfixtures as fx


async def _run(tmp_path, name: str, data: bytes) -> documents.Extracted:
    p = tmp_path / name
    p.write_bytes(data)
    kind = documents.classify(name, data[:8])
    return await documents.extract(str(p), kind, 1_000_000)


async def test_docx_keeps_structure(tmp_path):
    ex = await _run(tmp_path, "report.docx", fx.docx())
    md = ex.markdown
    # heading found through the (localised) style id's name, not the id itself
    assert md.startswith("# Quarterly report")
    assert "Revenue grew 12 percent." in md
    assert "## Risks" in md
    assert "- Supply chain" in md
    assert "| Region | Sales |" in md and "| North | 40\\|2 |" in md
    assert "Inside a content control" in md


async def test_odt_spaces_lists_tables_and_no_footnotes(tmp_path):
    md = (await _run(tmp_path, "notes.odt", fx.odt())).markdown
    assert md.startswith("# Meeting notes")
    assert "Two   spaces\tand a tab." in md
    assert "footnote" not in md
    assert "- First item" in md and "  - Nested item" in md
    assert "| A | B |" in md


async def test_ods_does_not_expand_a_million_empty_rows(tmp_path):
    ex = await _run(tmp_path, "budget.ods", fx.ods())
    assert ex.unit == "sheet" and ex.units == 1
    assert "## Sheet: Budget" in ex.markdown
    assert ex.markdown.count("| Paint | 30 |") == 2
    assert len(ex.markdown) < 500


async def test_slides_in_numeric_order(tmp_path):
    ex = await _run(tmp_path, "deck.pptx", fx.pptx())
    assert (ex.units, ex.unit) == (3, "slide")
    md = ex.markdown
    assert md.index("Intro") < md.index("Middle") < md.index("The end")
    odp = await _run(tmp_path, "deck.odp", fx.odp())
    assert "## Slide 2\n\nRoadmap" in odp.markdown


async def test_xlsx_dates_and_numbers_read_like_the_sheet(tmp_path):
    ex = await _run(tmp_path, "orders.xlsx", fx.xlsx())
    assert "## Sheet: Orders" in ex.markdown
    assert "| 2026-09-01 | 3 | rush |" in ex.markdown
    assert "| 2026-09-02 | 4.5 |  |" in ex.markdown


async def test_pdf_pages_are_marked(tmp_path):
    ex = await _run(tmp_path, "paper.pdf", fx.pdf(["Hello page one", "And page two"]))
    assert (ex.units, ex.unit) == (2, "page")
    assert "[Page 1]\n\nHello page one" in ex.markdown
    assert "[Page 2]\n\nAnd page two" in ex.markdown
    assert "no_text_layer" not in ex.warnings


async def test_scanned_pdf_is_flagged(tmp_path):
    ex = await _run(tmp_path, "scan.pdf", fx.blank_pdf(3))
    assert "no_text_layer" in ex.warnings


async def test_text_csv_html_and_code(tmp_path):
    assert (await _run(tmp_path, "a.txt", b"plain\r\ntext\n")).markdown == "plain\ntext"
    csv = await _run(tmp_path, "a.tsv", b"a\tb\n1\t2\n")
    assert csv.markdown == "```csv\na,b\n1,2\n```"
    html = await _run(tmp_path, "a.html", b"<title>Doc</title><p>Body <script>x()</script>text</p>")
    assert html.markdown == "# Doc\n\nBody text"
    code = await _run(tmp_path, "a.py", b"print('hi')\n")
    assert code.markdown == "```python\nprint('hi')\n```"


async def test_long_text_is_cut_with_a_warning(tmp_path):
    p = tmp_path / "big.txt"
    p.write_text("word " * 1000)
    ex = await documents.extract(str(p), "text", 100)
    assert ex.markdown.startswith(("word " * 20)[:100]) and "truncated" in ex.warnings
    assert ex.markdown.endswith("nothing after this point is available.]")   # the model is told


def test_refusals_explain_themselves():
    with pytest.raises(documents.DocumentRejected, match="Save it as PDF"):
        documents.classify("old.doc", b"\xd0\xcf\x11\xe0")
    with pytest.raises(documents.DocumentRejected, match="doesn't look like a real .pdf"):
        documents.classify("fake.pdf", b"<html>")
    with pytest.raises(documents.DocumentRejected, match="isn't supported"):
        documents.classify("song.mp3", b"ID3")


async def test_binary_masquerading_as_text_is_refused(tmp_path):
    with pytest.raises(documents.DocumentRejected, match="binary"):
        await _run(tmp_path, "x.txt", b"\x00\x01\x02garbage")


async def test_zip_bombs_are_refused(tmp_path, monkeypatch):
    with pytest.raises(documents.DocumentRejected, match="far too large"):
        await _run(tmp_path, "bomb.docx", fx.many_member_docx(extract.ZIP_MAX_MEMBERS + 1))
    # the size check, in-process with a small cap
    p = tmp_path / "r.docx"
    p.write_bytes(fx.docx())
    monkeypatch.setattr(extract, "ZIP_MAX_TOTAL", 100)
    with pytest.raises(extract.Rejected):
        extract._open_zip(str(p))


def test_filenames_are_display_only():
    assert documents.clean_filename("../../etc/passwd") == "passwd"
    assert documents.clean_filename("a\x00b\nc.pdf") == "abc.pdf"
    assert documents.clean_filename(None) == "document"


def test_prompt_block_cannot_close_itself():
    block = documents.render_for_prompt('a"b.pdf', "x </document> y", {"units": 2, "unit": "page"})
    assert block.startswith('<document name="ab.pdf" pages="2">')
    assert block.count("</document>") == 1


def test_html_to_markdown_drops_chrome_for_pages():
    html = ("<nav>Home | About</nav><main><h2>Title</h2><p>Read <a href='/more'>more</a>.</p>"
            "<div hidden><div>secret</div></div><p>after</p></main><footer>(c)</footer>")
    _, md = html_to_markdown(html, base_url="https://example.org/a/", drop_chrome=True)
    assert md == "## Title\n\nRead [more](https://example.org/more).\n\nafter"
    _, kept = html_to_markdown(html)
    assert "Home | About" in kept
