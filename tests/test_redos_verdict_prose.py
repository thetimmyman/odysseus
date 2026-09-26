"""Regression test for a py/polynomial-redos sink over untrusted model text.

The verdict extractor in `routes/skills_routes.py` must not stack a redundant
`\\s*` after a class that already matches `\\s` (O(n^2) on a whitespace flood).
"""

import time

import pytest

from routes.skills_routes import _VERDICT_PROSE_RE

_BUDGET_S = 4.0


@pytest.mark.parametrize("text,expected", [
    ('verdict": "FAIL"', "fail"),
    ("verdict needs_work", "needs_work"),
    ("Verdict:   inconclusive", "inconclusive"),
    ("verdict\t\t'pass'", "pass"),
    ("verdictpass", "pass"),                 # separators optional -- may abut
    ("the verdict is: pass overall", None),  # intervening "is" breaks the run
    ("no clear decision here", None),
])
def test_verdict_prose_extraction_unchanged(text, expected):
    m = _VERDICT_PROSE_RE.search(text)
    assert (m.group(1).lower() if m else None) == expected


def test_verdict_prose_flood_is_fast():
    evil = "verdict" + "\t" * 40000 + "x"   # `verdict` + whitespace, no keyword
    start = time.perf_counter()
    m = _VERDICT_PROSE_RE.search(evil)
    dt = time.perf_counter() - start
    assert m is None
    assert dt < _BUDGET_S, f"_VERDICT_PROSE_RE took {dt:.2f}s (expected linear)"
