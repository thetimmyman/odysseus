"""PS-635 live control (experiment L1) — the hidden acceptance test.

Harness-owned. The worker NEVER sees this file. It asserts BEHAVIOUR, not key
strings: a module that reads the wrong keys renders NONE, which is what fails.

The decisive case is test_invented_keys_are_ignored: a worker that guessed
synonyms instead of reading the declared interface keys passes every other test
here and fails that one.
"""
from src.ledger_note import render_note

DECLARED = {
    "subject_name": "Reconcile live state",
    "verbatim_lines": ["alpha", "beta"],
    "block_reason": "write scope unclear",
}


def test_reads_the_declared_keys():
    out = render_note(DECLARED)
    assert "SUBJECT: Reconcile live state" in out
    assert "alpha" in out and "beta" in out
    assert "BLOCK: write scope unclear" in out


def test_sections_appear_in_order():
    out = render_note(DECLARED)
    order = [out.index("SUBJECT:"), out.index("LINES:"), out.index("BLOCK:")]
    assert order == sorted(order)


def test_absent_keys_render_none():
    out = render_note({})
    assert "SUBJECT: NONE" in out
    assert "LINES: NONE" in out
    assert "BLOCK: NONE" in out


def test_optional_block_reason_may_be_absent():
    out = render_note({"subject_name": "S", "verbatim_lines": ["x"]})
    assert "SUBJECT: S" in out
    assert "BLOCK: NONE" in out


def test_invented_keys_are_ignored():
    """NEGATIVE CONTROL for guessing: synonyms must NOT be read."""
    out = render_note({"subject": "S", "lines": ["a"], "block": "b"})
    assert "SUBJECT: NONE" in out
    assert "LINES: NONE" in out
    assert "BLOCK: NONE" in out


def test_lines_render_one_bullet_each_indented_two_spaces():
    out = render_note(DECLARED)
    lines_section = out.split("LINES:", 1)[1]
    assert "  - alpha" in lines_section
    assert "  - beta" in lines_section


def test_output_never_exceeds_max_chars():
    out = render_note({"subject_name": "x" * 5000}, max_chars=200)
    assert len(out) <= 200


def test_truncation_is_visible():
    out = render_note({"subject_name": "x" * 5000}, max_chars=200)
    assert "...[TRUNCATED]" in out
