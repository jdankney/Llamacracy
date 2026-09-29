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


def test_exact_forms_and_other_units_match():
    # sympy printed exact values; the answer restates them (from a live run)
    out = ["Final result: 16/5\nVolume: 8*pi\nPeriod: 1.874758162762797e-07 s"]
    ans = r"It's **16/5** (or **3.2**), the volume is $8\pi$, and T is 187 ns, or $\frac{16}{5}$ again."
    assert check(ans, out, []) == {"matched": 5, "unmatched": []}
    # but a fraction the code never produced is still a claim
    assert check(r"so $\frac{17}{5}$", out, [])["unmatched"] == [r"\frac{17}{5}"]


def test_working_toward_a_computed_result_is_not_flagged():
    out = ["T(1024) = 11264"]
    ans = "$$T(1024) = 1024(1 + 10) = 1024 \\times 11 = \\boxed{11264}$$"
    assert check(ans, out, []) == {"matched": 2, "unmatched": []}
    # a wrong result isn't excused by its working (which is right: 1024 × 11
    # is the 11264 the code printed), and working doesn't excuse a number on
    # a line that ends somewhere unchecked
    assert check("$$1024 \\times 11 = 11265$$", out, [])["unmatched"] == ["11265"]
    assert check("r = 11264 and T = 999", out, [])["unmatched"] == ["999"]
    # working whose result is on the next line: 1024 · 11 is exactly 11264
    ans = "$$T(1024) = 1024 \\cdot (10 + 1) = 1024 \\cdot 11$$\n$$T(1024) = 11264$$"
    assert check(ans, out, []) == {"matched": 2, "unmatched": []}
    assert check("$$1024 \\cdot 12$$", out, [])["unmatched"] == ["12"]


def test_formula_constants_are_not_claims():
    ans = r"$$T = \frac{2\pi m}{qB}$$ over $\int_0^{2\pi}$, with $\frac{1}{2}$ and **42.5**"
    assert check(ans, ["42.5"], []) == {"matched": 1, "unmatched": []}


def test_reciprocals_in_exact_working():
    # series capacitors: 1/C = 1/4 + 1/6 = 5/12, and the code printed C = 2.4
    ans = r"$\frac{1}{C} = \frac{1}{4} + \frac{1}{6} = \frac{5}{12}$, so $C = \mathbf{2.4}$ µF"
    assert check(ans, ["C = 2.4"], ["4 µF and 6 µF in series"]) == {"matched": 2, "unmatched": []}
    # decimals aren't read as reciprocals: 0.25 isn't 4
    assert check("about 0.25", ["4.0"], [])["unmatched"] == ["0.25"]
