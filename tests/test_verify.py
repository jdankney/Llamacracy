# SPDX-License-Identifier: AGPL-3.0-or-later
"""The number check: which numbers in an answer came out of the code."""

from __future__ import annotations

from llamacracy.verify import answer_numbers, check


def test_rounded_numbers_match_what_the_code_printed():
    out = ["0.7853981633974483\n1234.5678\n0.0012345"]
    ans = "About 0.785, or 0.79, and 1,234.57; also 1.23 × 10^{-3} and 1.2\\times10^{-3}."
    assert check(ans, out, []) == {"matched": 5, "unmatched": []}


def test_a_number_from_nowhere_is_flagged():
    assert check("The answer is 0.5 and 17.3", ["0.5"], []) == {"matched": 1, "unmatched": ["17.3"]}
    # not rounded from the output: 0.79 isn't 0.7853... to two places either
    assert check("roughly 0.80", ["0.7853981633974483"], [])["unmatched"] == ["0.80"]


def test_givens_labels_and_small_numbers_are_not_claims():
    ans = ("Step 12: with the 37.5 kg mass from the question, see page 214 and [3].\n"
           "1. first\n2. second\nIn 2025 the result was x = 4.")
    # nothing here is a computed claim, and nothing is suspicious either
    assert check(ans, ["99"], ["a 37.5 kg mass"]) is None


def test_percentages_and_code_blocks():
    assert check("That's 42.9% of it.", ["0.42857142857142855"], [])["matched"] == 1
    # numbers inside the code the answer shows aren't claims about results
    assert check("```python\nx = 123.456\n```\nSo 3.14.", ["3.14159"], []) == {"matched": 1, "unmatched": []}


def test_answer_numbers_reads_negatives_and_thousands():
    got = [(s, v) for s, v, _ in answer_numbers("−2.5 then 1,000,000 and 6.02e23")]
    assert got == [("−2.5", -2.5), ("1,000,000", 1_000_000.0), ("6.02e23", 6.02e23)]
