# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tiny but real documents, built in code so the tests need no office suite.

Each is the smallest file its format's readers accept: the same XML parts an
editor writes, zipped the same way, just with nothing optional in them.
"""

from __future__ import annotations

import io
import zipfile


def _zip(parts: dict[str, str], first: tuple[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if first:     # ODF wants its mimetype first and stored
            z.writestr(zipfile.ZipInfo(first[0]), first[1])
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def docx() -> bytes:
    styles = f"""<w:styles {W}>
      <w:style w:type="paragraph" w:styleId="Berschrift1"><w:name w:val="heading 1"/></w:style>
      <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>
    </w:styles>"""
    body = f"""<w:document {W}><w:body>
      <w:p><w:pPr><w:pStyle w:val="Berschrift1"/></w:pPr><w:r><w:t>Quarterly report</w:t></w:r></w:p>
      <w:p><w:r><w:t xml:space="preserve">Revenue grew </w:t></w:r><w:r><w:t>12 percent.</w:t></w:r></w:p>
      <w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>Risks</w:t></w:r></w:p>
      <w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>Supply chain</w:t></w:r></w:p>
      <w:tbl>
        <w:tr><w:tc><w:p><w:r><w:t>Region</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Sales</w:t></w:r></w:p></w:tc></w:tr>
        <w:tr><w:tc><w:p><w:r><w:t>North</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>40|2</w:t></w:r></w:p></w:tc></w:tr>
      </w:tbl>
      <w:sdt><w:sdtContent><w:p><w:r><w:t>Inside a content control</w:t></w:r></w:p></w:sdtContent></w:sdt>
    </w:body></w:document>"""
    return _zip({"[Content_Types].xml": "<Types/>", "word/document.xml": body,
                 "word/styles.xml": styles})


ODF_NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
          'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
          'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
          'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"')


def _odf(body: str, mimetype: str) -> bytes:
    content = f"<office:document-content {ODF_NS}><office:body>{body}</office:body></office:document-content>"
    return _zip({"content.xml": content}, first=("mimetype", mimetype))


def odt() -> bytes:
    return _odf("""<office:text>
      <text:h text:outline-level="1">Meeting notes</text:h>
      <text:p>Two<text:s text:c="3"/>spaces<text:tab/>and a tab.<text:note><text:note-body><text:p>footnote</text:p></text:note-body></text:note></text:p>
      <text:list><text:list-item><text:p>First item</text:p>
        <text:list><text:list-item><text:p>Nested item</text:p></text:list-item></text:list>
      </text:list-item></text:list>
      <table:table><table:table-row><table:table-cell><text:p>A</text:p></table:table-cell><table:table-cell><text:p>B</text:p></table:table-cell></table:table-row>
        <table:table-row><table:table-cell><text:p>1</text:p></table:table-cell><table:table-cell><text:p>2</text:p></table:table-cell></table:table-row></table:table>
    </office:text>""", "application/vnd.oasis.opendocument.text")


def ods() -> bytes:
    # the trailing row/column "repeated" a million times is what real files do
    return _odf("""<office:spreadsheet><table:table table:name="Budget">
      <table:table-row><table:table-cell><text:p>Item</text:p></table:table-cell><table:table-cell><text:p>Cost</text:p></table:table-cell><table:table-cell table:number-columns-repeated="16382"/></table:table-row>
      <table:table-row table:number-rows-repeated="2"><table:table-cell><text:p>Paint</text:p></table:table-cell><table:table-cell><text:p>30</text:p></table:table-cell></table:table-row>
      <table:table-row table:number-rows-repeated="1048573"><table:table-cell table:number-columns-repeated="16384"/></table:table-row>
    </table:table></office:spreadsheet>""", "application/vnd.oasis.opendocument.spreadsheet")


def odp() -> bytes:
    return _odf("""<office:presentation>
      <draw:page draw:name="p1"><draw:frame><draw:text-box><text:p>Welcome</text:p></draw:text-box></draw:frame></draw:page>
      <draw:page draw:name="p2"><draw:frame><draw:text-box><text:p>Roadmap</text:p></draw:text-box></draw:frame></draw:page>
    </office:presentation>""", "application/vnd.oasis.opendocument.presentation")


def pptx() -> bytes:
    a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    slide = f'<p:sld xmlns:p="x" {a}><a:p><a:r><a:t>{{}}</a:t></a:r></a:p></p:sld>'
    # slide10 sorts before slide2 as a string: order has to be numeric
    return _zip({"[Content_Types].xml": "<Types/>",
                 "ppt/slides/slide1.xml": slide.format("Intro"),
                 "ppt/slides/slide2.xml": slide.format("Middle"),
                 "ppt/slides/slide10.xml": slide.format("The end")})


def xlsx() -> bytes:
    import datetime as dt

    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Orders"
    ws.append(["Date", "Qty", "Note"])
    ws.append([dt.date(2026, 9, 1), 3.0, "rush"])
    ws.append([dt.date(2026, 9, 2), 4.5, None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def pdf(pages: list[str]) -> bytes:
    """A PDF with one line of real (extractable) text per page."""
    objs: list[bytes] = []
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    font = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {4 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % n + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref))
    return out.getvalue()


def blank_pdf(n: int) -> bytes:
    """Pages with no text layer at all: what a scan looks like to a parser."""
    return pdf([""] * n)


def many_member_docx(n: int) -> bytes:
    """A .docx with an absurd number of parts: one shape of zip bomb."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("word/document.xml", "<w:document/>")
        for i in range(n):
            z.writestr(f"p/{i}", "")
    return buf.getvalue()
