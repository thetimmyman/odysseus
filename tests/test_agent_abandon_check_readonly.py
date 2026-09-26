"""The abandoned-task auto-continue must not fire on a finished read-only task.

`_effectful_used` never flips on a read-only task, so the abandon check must not
read that as unfinished work and nudge a completed answer into restating itself.
"""

import pytest

from src.agent_loop import SUBSTANTIVE_ANSWER_CHARS, is_substantive_answer

# Three tool-free rounds: the real answer, then two redundant restatements.
_ROUND_3_ANSWER = 910
_ROUND_4_RESTATEMENT = 948
_ROUND_5_RESTATEMENT = 601


@pytest.mark.parametrize(
    "length", [_ROUND_3_ANSWER, _ROUND_4_RESTATEMENT, _ROUND_5_RESTATEMENT]
)
def test_real_answers_from_the_observed_turn_suppress_the_nudge(length):
    assert is_substantive_answer("x" * length) is True


def test_empty_round_is_not_an_answer():
    assert is_substantive_answer("") is False
    assert is_substantive_answer("   \n  ") is False


def test_short_promise_still_reads_as_abandoned():
    # The case the auto-continue exists to catch — must keep firing.
    assert is_substantive_answer("Let me check the logs to see what went wrong.") is False


def test_short_answer_with_a_code_block_counts():
    # A terse but real answer: under the char threshold, but it shipped output.
    assert is_substantive_answer("Here it is:\n```py\nprint(1)\n```") is True


def test_threshold_boundary():
    assert is_substantive_answer("x" * (SUBSTANTIVE_ANSWER_CHARS - 1)) is False
    assert is_substantive_answer("x" * SUBSTANTIVE_ANSWER_CHARS) is True


def test_whitespace_padding_does_not_manufacture_an_answer():
    assert is_substantive_answer("done." + " " * SUBSTANTIVE_ANSWER_CHARS) is False


def test_abandon_check_requires_a_non_substantive_answer():
    """The auto-continue condition must actually consult the predicate."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "src" / "agent_loop.py").read_text()
    # Anchor on the code block's box-drawing header, not the plain phrase — the
    # latter also appears in the constants comment far above the condition.
    marker = "── Abandoned-task auto-continue"
    assert marker in src, "abandon-check block header moved — update this test"
    block = src.split(marker)[1][:1500]
    assert "not _substantive_answer" in block, (
        "abandon check no longer consults is_substantive_answer — a finished "
        "read-only turn will be nudged into restating itself again"
    )
