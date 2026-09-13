"""Regression test for a py/polynomial-redos sink over untrusted model text.

Imported for PS-602 (the applicable half of upstream #4943; our fork's
`agent_loop.py` does not carry the continuation matcher).

`routes/skills_routes.py` extracted a verdict from a teacher/verifier model's
PROSE with `["\\'\\s:]*\\s*` -- the class already matches `\\s`, so the trailing
`\\s*` was a redundant second quantifier that backtracked O(n^2) when the
keyword failed to match after a whitespace flood. Dropping it keeps the exact
match set and makes the scan linear.
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
