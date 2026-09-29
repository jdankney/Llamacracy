# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reading tools' pieces, and the fetcher's refusal to go anywhere
inside the network."""

from __future__ import annotations

import httpx
import pytest

from llamacracy import fetch as fetchmod
from llamacracy.tools import DocRef, Toolbox, document_tools, page_at, text_window


def test_text_window_ends_on_a_line_break():
    text = "\n".join(f"line {i}" for i in range(100))
    chunk, a, b = text_window(text, 0, 50)
    assert a == 0 and chunk.endswith("\n") and b <= 50
    chunk2, a2, _ = text_window(text, b, 50)
    assert a2 == b and chunk2.startswith("line")


def test_page_at_follows_page_marks():
    text = "[Page 1]\n\nalpha\n\n[Page 2]\n\nbeta"
    assert page_at(text, text.index("alpha")) == 1
    assert page_at(text, text.index("beta")) == 2
    assert page_at(text, text.index("[Page 2]")) == 2     # a read that starts on the mark


async def test_reading_on_through_a_document_counts_parts_and_names_sections():
    text = "\n\n".join(f"## Section {s}\n\n" + "\n".join(f"filler line {s}.{i} here" for i in range(40))
                       for s in range(1, 31))
    (_, search, read) = document_tools([DocRef("d1", "book.md", text)], read_chars=2000)
    box = Toolbox([search, read], max_steps=1, deadline_s=10, result_chars=100_000)
    labels, start = [], 0
    for _ in range(3):
        out = await box.run("doc_read", {"ref": "d1", "start": start})
        labels.append(out.summary)
        start = int(out.text.rsplit("start ", 1)[1].rstrip("]").replace(",", ""))
    # each window ends a little short of 2000 chars: still parts 1, 2, 3
    assert [lb.split(" of ")[0] for lb in labels] == ["part 1", "part 2", "part 3"]
    assert 'from inside "Section 5"' in out.text    # ~900 chars a section
    hits = await box.run("doc_search", {"ref": "d1", "query": "filler 17.5"})
    assert 'under "Section 17"' in hits.text


async def test_outline_samples_long_documents_evenly():
    text = "\n\n".join(f"[Page {i}]\n\nPage {i} begins here" for i in range(1, 501))
    (outline, _, _) = document_tools([DocRef("d1", "big.pdf", text, 500, "page")], read_chars=5000)
    box = Toolbox([outline], max_steps=1, deadline_s=10, result_chars=100_000)
    out = await box.run("doc_outline", {"ref": "d1"})
    assert out.text.startswith('d1 "big.pdf": 500 pages')
    assert "[Page 1] Page 1 begins here" in out.text and "[Page 497]" in out.text
    assert "(… 500 entries in all)" in out.text


async def test_unknown_document_ref_lists_the_real_ones():
    tools = document_tools([DocRef("d1", "a.pdf", "x")], read_chars=100)
    box = Toolbox(tools, max_steps=1, deadline_s=10, result_chars=1000)
    out = await box.run("doc_read", {"ref": "d9"})
    assert not out.ok and 'd1 ("a.pdf")' in out.text
    # the file name works as a ref too: models do that
    assert (await box.run("doc_read", {"ref": "a.pdf"})).ok


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://10.0.0.5/", "http://192.168.0.1/", "http://172.16.3.4/",
    "http://169.254.169.254/latest/meta-data", "http://100.64.1.1/", "http://[::1]/",
    "http://[fd00::1]/", "http://[::ffff:127.0.0.1]/", "http://0.0.0.0/", "http://224.0.0.1/",
])
async def test_fetch_refuses_internal_addresses(url):
    with pytest.raises(fetchmod.FetchError, match="not a public website"):
        await fetchmod.fetch(url, max_bytes=1000)


@pytest.mark.parametrize("url,why", [
    ("file:///etc/passwd", "only http and https"),
    ("gopher://example.org/", "only http and https"),
    ("http://user:pw@example.org/", "username or password"),
])
async def test_fetch_refuses_odd_links(url, why):
    with pytest.raises(fetchmod.FetchError, match=why):
        await fetchmod.fetch(url, max_bytes=1000)


async def test_fetch_checks_every_redirect_and_pins_the_address():
    seen = []

    def handler(request: httpx.Request):
        seen.append((str(request.url), request.headers["host"], request.extensions.get("sni_hostname")))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
    with pytest.raises(fetchmod.FetchError, match="not a public website"):
        await fetchmod.fetch("http://93.184.215.14/start", max_bytes=1000, client=client)
    # the first hop went to the checked address; the redirect never left
    assert seen == [("http://93.184.215.14/start", "93.184.215.14", "93.184.215.14")]


async def test_fetch_caps_the_body(monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"content-type": "text/html; charset=latin-1"},
                                 content=b"x" * 5000)))
    got = await fetchmod.fetch("http://93.184.215.14/", max_bytes=1000, client=client)
    assert got.truncated and len(got.body) == 1000 and got.charset == "latin-1"
