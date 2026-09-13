r"""Regression test for a py/polynomial-redos sink in text_helpers.

Imported for PS-602 (the `<thought>` half of upstream #4704; our tree already
carries the tool_parsing half via the forward-only delimiter scanners).

`_THOUGHT_TAG_OPEN_RE` used `(\s+[^>]*)?` -- two OVERLAPPING quantifiers (both
match whitespace), so an unclosed `<thought` + whitespace flood in untrusted
model output backtracked O(n^2). Dropping the `+` (`\s` = one char) removes the
ambiguity while keeping the EXACT language and capture.

NOTE: upstream #4704 chose `([^>]*)`, which is NOT equivalent here -- it also
matches non-tags (`<thoughtx>`, `<thought/>`) and silently rewrites them. The
equivalence oracle below rejects that; this fork uses the exact form.
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
    # The DOCUMENTED adversary: an opener followed by a long whitespace run and
    # NO `>`. The old `(\s+[^>]*)?>` had `\s+` and `[^>]*` both consuming that
    # whitespace -> O(n^2) backtracking; the single `[^>]*` scan is linear.
    evil = "<thought" + " " * 60_000 + "x"
    start = time.perf_counter()
    out = _new_sub(evil)
    dt = time.perf_counter() - start
    assert out == evil                    # no `>` -> nothing matches
    assert dt < _BUDGET_S, f"took {dt:.2f}s (expected linear)"
