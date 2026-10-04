r"""Regression test for a py/polynomial-redos sink in text_helpers.

`_THOUGHT_TAG_OPEN_RE` must not use overlapping whitespace quantifiers (O(n^2)
on an unclosed `<thought` + whitespace flood) and must keep the exact language
and capture. A bare `([^>]*)` is NOT equivalent: it also matches `<thoughtx>`.
"""

import re
import time

from src.text_helpers import _THOUGHT_TAG_OPEN_RE

_BUDGET_S = 4.0

# The pre-fix pattern, used ONLY as an equivalence oracle on well-formed tags.
_OLD = re.compile(r"<thought(\s+[^>]*)?>", re.IGNORECASE)


def _old_sub(text: str) -> str:
    return _OLD.sub(lambda m: "<think" + (m.group(1) or "") + ">", text)


def _new_sub(text: str) -> str:
    return _THOUGHT_TAG_OPEN_RE.sub(lambda m: "<think" + (m.group(1) or "") + ">", text)


EQUIV_CASES = [
    "",
    "plain text",
    "<thought>cot</thought>",
    "<thought id='x'>cot</thought>",
    "<thought  spaced attrs>tail",
    "<thought>",
    "<thoughtx> not a tag",
    "a<thought/>b",            # self-closing: group is "/"
    "mixed <thought>a</thought> and <thought id='y'>b</thought>",
    "CASE <THOUGHT>UP</THOUGHT>",
]


def test_thought_open_tag_substitution_matches_old_pattern():
    for case in EQUIV_CASES:
        assert _new_sub(case) == _old_sub(case), repr(case)


def test_thought_flood_is_linear():
    # An opener, a long whitespace run and NO `>`: must scan linearly.
    evil = "<thought" + " " * 60_000 + "x"
    start = time.perf_counter()
    out = _new_sub(evil)
    dt = time.perf_counter() - start
    assert out == evil                    # no `>` -> nothing matches
    assert dt < _BUDGET_S, f"took {dt:.2f}s (expected linear)"
