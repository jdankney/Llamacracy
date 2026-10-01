# SPDX-License-Identifier: AGPL-3.0-or-later
"""The quote check: are an answer's quotations really in what it read?"""

from __future__ import annotations

from llamacracy.quotes import check, extract

READER = ("[Page 1]\n\nIntroduction to the course.\n\n"
          "[Page 2]\n\nThe printed page is not merely a vessel for text; it is a docu-\n"
          "ment of the printer's craft, set by hand one letter at a time.\n\n"
          "[Page 3]\n\nLibraries began collecting early printed books as art only in the 1870s, "
          "after the Caxton exhibition.")


def test_exact_words_match_however_they_are_typed():
    ans = ('The reader calls the page “a document of the printer’s craft, set by hand” (p. 2), '
           'and notes that libraries "began collecting early printed books as art only in the 1870s".')
    assert check(ans, [("reader.pdf", READER)], []) == {"found": 2, "missing": [], "wrong_page": []}


def test_a_changed_word_or_an_invented_quote_is_flagged():
    ans = ('It says the page is "a record of the printer\'s craft, set by hand" and that '
           '"printed books were first shown in libraries in 1920".')
    r = check(ans, [("reader.pdf", READER)], [])
    assert r["found"] == 0 and len(r["missing"]) == 2


def test_ellipses_and_insertions_stand_for_left_out_words():
    ans = '> The printed page is not merely a vessel for text; … it is a document of the printer\'s craft\n> — reader.pdf, p. 2'
    assert check(ans, [("reader.pdf", READER)], [])["found"] == 1
    ans = 'They write that "[early books were collected] as art only in the 1870s, after the Caxton exhibition."'
    assert check(ans, [("reader.pdf", READER)], [])["found"] == 1


def test_the_wrong_page_is_flagged():
    ans = 'As the reader puts it, "libraries began collecting early printed books as art" (p. 2).'
    r = check(ans, [("reader.pdf", READER)], [])
    assert r["found"] == 1 and r["wrong_page"] == [
        {"quote": "libraries began collecting early printed books as art", "cited": 2, "pages": [3]}]
    # a page given before the quotation counts too
    ans = 'On page 2 they say "libraries began collecting early printed books as art".'
    assert check(ans, [("reader.pdf", READER)], [])["wrong_page"][0]["cited"] == 2


def test_short_quotes_code_and_math_are_not_quotations():
    ans = ('The word "document" matters, as does "Week 9". In code: `s = "a b c d e f"`.\n\n'
           '```\nprint("this is not a quote at all")\n```\n$$ \\text{"also not a quote here"} $$')
    assert extract(ans) == []
    assert check(ans, [("reader.pdf", READER)], []) is None


def test_what_the_tools_returned_and_the_user_wrote_count_too():
    ans = 'The page says "llamas were domesticated in the Andes about 5,000 years ago".'
    seen = ["Llamas were domesticated in the Andes about 5,000 years ago, most likely..."]
    assert check(ans, [], seen) == {"found": 1, "missing": [], "wrong_page": []}
