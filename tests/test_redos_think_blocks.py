"""Regression tests for ReDoS in agent_loop's `<think>...</think>` stripping.

Imported for PS-602 (security subset of PS-595) from upstream Odysseus #4877,
which fixed CodeQL `py/polynomial-redos` on the lazy `<think>.*?</think>`
pattern in `src/agent_loop.py`. Our fork carried one compiled `_THINK_RE` plus
an inline copy, applied with `re.sub` over a whole model response. When the
closing delimiter is missing, the engine rescans to end-of-string from every
`<think>` opener -> O(n^2) on attacker-influenced input.

The fix replaces the regex with `_strip_think_blocks`, a forward-only linear
scan that is byte-for-byte equivalent to the original
`re.sub(r'<think>.*?</think>', '', text, flags=DOTALL|IGNORECASE)`.
"""

import re
import time

from src.agent_loop import _strip_think_blocks

_REFERENCE_RE = re.compile("<think>" + ".*?" + "</think>", re.DOTALL | re.IGNORECASE)


def _reference(text: str) -> str:
    return _REFERENCE_RE.sub("", text or "")


_BUDGET_S = 4.0

EQUIV_CASES = [
    "",
    "no tags here at all",
    "<think>hidden</think>visible",
    "before<think>cot</think>after",
    "a<think>one</think>b<think>two</think>c",
    "<think>only</think>",
    "<think></think>tail",
    "<think>a<think>nested</think>rest",
    "leading</think>orphan<think>x</think>",
    "trailing<think>no closer for this one",
    "CASE <THINK>UP</THINK> mix <Think>x</Think>",
    "multi\nline\n<think>a\nb\nc</think>\nkeep",
    "<thinking>not matched by narrow regex</thinking>",
    "<think >space-in-tag not matched</think >",
]


def test_strip_think_blocks_matches_reference_regex():
    for case in EQUIV_CASES:
        assert _strip_think_blocks(case) == _reference(case), repr(case)


def test_empty_and_none_safe():
    assert _strip_think_blocks("") == ""
    assert _strip_think_blocks(None) in (None, "")


def test_many_openers_no_closer_is_linear():
    hostile = "<think>" * 60_000 + "x"
    start = time.perf_counter()
    out = _strip_think_blocks(hostile)
    elapsed = time.perf_counter() - start
    assert out == hostile
    assert elapsed < _BUDGET_S, f"took {elapsed:.2f}s (expected linear)"


def test_openers_then_one_far_closer_is_linear():
    hostile = "<think>" * 60_000 + "</think>" + "tail"
    start = time.perf_counter()
    out = _strip_think_blocks(hostile)
    elapsed = time.perf_counter() - start
    assert out == "tail"
    assert elapsed < _BUDGET_S, f"took {elapsed:.2f}s (expected linear)"
