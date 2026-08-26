"""Regression tests for POS-AI-23 — background-call output reaching a user chat.

Two background completions were persisted as the assistant's reply in live
sessions on 2026-08-24/25:

* ``8670f5ae`` — the memory extractor's ``[{"text": ..., "category": ...}]``
  array, answering "yes install whatever we need to make this work";
* ``2c490607`` — the skill extractor's literal ``null`` decline token,
  answering a FizzBuzz coding request.

Both were written by the ordinary chat finaliser with ordinary chat metadata,
so nothing downstream could tell them apart from a real reply. These tests lock
in the three things that now make that impossible to happen silently:

1. the two observed payloads are recognised;
2. ordinary replies are NOT (the guard must not eat real answers);
3. background and interactive model calls are structurally separated — different
   connection pools, different response-cache namespaces.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import bg_crossover  # noqa: E402


# ── The two payloads actually observed in production ──────────────────────

# services/memory/memory_extractor.py::EXTRACT_SYSTEM_PROMPT contract.
OBSERVED_MEMORY_EXTRACTION = (
    '[\n'
    '  {"text": "The user\'s name is Timothy DeFreest.", "category": "identity"},\n'
    '  {"text": "Career in defense business development, proposals, and pricing.", '
    '"category": "fact"}\n'
    ']'
)

# services/memory/skill_extractor.py: "Return null (the bare word, no JSON)".
OBSERVED_SKILL_DECLINE = "null"


def test_detects_memory_extractor_payload():
    assert bg_crossover.detect(OBSERVED_MEMORY_EXTRACTION) == "memory-extractor"


def test_detects_skill_extractor_decline_token():
    assert bg_crossover.detect(OBSERVED_SKILL_DECLINE) == "bare-sentinel"


def test_detects_skill_extractor_json_payload():
    payload = (
        '{"title": "Fix an off-by-one", "problem": "loop stops early", '
        '"solution": "widen the range", "steps": ["read", "edit", "run"], '
        '"tags": ["python"], "confidence": 0.8}'
    )
    assert bg_crossover.detect(payload) == "skill-extractor"


def test_detects_completion_verifier_scaffolding():
    payload = (
        "<user_request>\nwrite fizzbuzz\n</user_request>\n"
        "<actions_taken>\n[bash] python fizz.py\n</actions_taken>\n"
        "VERIFICATION: FAIL: the file was never created"
    )
    assert bg_crossover.detect(payload) == "completion-verifier"


def test_detects_decline_token_behind_reasoning():
    # The observed turn spent 6065 tokens reasoning and emitted 4 chars of
    # content. Thinking markup must not hide the sentinel from the guard.
    assert bg_crossover.detect("<think>lots of deliberation</think>\nnull") == "bare-sentinel"


# ── Real replies must survive untouched ───────────────────────────────────

def test_ignores_ordinary_reply():
    assert bg_crossover.detect("Done — fizzbuzz.py is fixed and prints 1..15.") is None


def test_ignores_reply_that_merely_mentions_null():
    text = (
        "The function returns null when the lookup misses, which is why the "
        "caller crashes. I changed it to return an empty list instead."
    )
    assert bg_crossover.detect(text) is None


def test_ignores_long_json_answer_the_user_asked_for():
    # A user CAN legitimately ask for JSON. Only the exact background contracts
    # trip the guard, and only for bodies short enough to BE one.
    items = ", ".join('{"text": "fact %d", "category": "fact"}' % i for i in range(200))
    payload = "[" + items + "]"
    assert len(payload) > bg_crossover.MAX_INSPECT_CHARS
    assert bg_crossover.detect(payload) is None


def test_ignores_json_array_of_unrelated_objects():
    assert bg_crossover.detect('[{"name": "a", "qty": 2}]') is None


# ── The guard replaces, logs and reports ──────────────────────────────────

def test_guard_replaces_crossover_with_a_user_facing_notice():
    content, reason = bg_crossover.guard_user_reply(
        OBSERVED_SKILL_DECLINE, session_id="2c490607", where="chat-finalize",
    )
    assert reason == "bare-sentinel"
    assert content == bg_crossover.USER_NOTICE
    assert content != OBSERVED_SKILL_DECLINE


def test_guard_passes_real_replies_through_unchanged():
    original = "Here's the fix: change range(1, 15) to range(1, 16)."
    content, reason = bg_crossover.guard_user_reply(original, session_id="x")
    assert reason is None
    assert content == original


def test_guard_logs_an_alertable_marker(caplog):
    with caplog.at_level("ERROR"):
        bg_crossover.guard_user_reply(OBSERVED_MEMORY_EXTRACTION, session_id="8670f5ae")
    assert any("[bg-crossover]" in r.getMessage() for r in caplog.records)


# ── Structural isolation: lanes ───────────────────────────────────────────

def test_background_and_interactive_calls_never_share_a_cache_entry():
    """The response cache is process-wide and keyed on the request. Identical
    requests from the two lanes must still land in different slots, so a
    background completion can never be handed to the user's turn."""
    from src.llm_core import _get_cache_key
    from src.llm_lane import BACKGROUND, INTERACTIVE

    msgs = [{"role": "user", "content": "same prompt"}]
    interactive = _get_cache_key("http://x/v1", "m", msgs, 0.0, 0, INTERACTIVE)
    background = _get_cache_key("http://x/v1", "m", msgs, 0.0, 0, BACKGROUND)
    assert interactive != background


def test_cache_key_follows_the_ambient_lane():
    from src.llm_core import _get_cache_key
    from src.llm_lane import BACKGROUND, INTERACTIVE, lane_scope

    msgs = [{"role": "user", "content": "same prompt"}]
    with lane_scope(INTERACTIVE):
        a = _get_cache_key("http://x/v1", "m", msgs, 0.0, 0)
    with lane_scope(BACKGROUND):
        b = _get_cache_key("http://x/v1", "m", msgs, 0.0, 0)
    assert a != b


def test_lanes_get_separate_connection_pools():
    """An abandoned background request must not be able to hand its socket to
    the SSE stream the user is reading."""
    from src import llm_core
    from src.llm_lane import BACKGROUND, INTERACTIVE

    interactive = llm_core._get_http_client(INTERACTIVE)
    background = llm_core._get_http_client(BACKGROUND)
    try:
        assert interactive is not background
        # Same lane keeps reusing its own client (warm connections preserved).
        assert llm_core._get_http_client(INTERACTIVE) is interactive
    finally:
        llm_core._http_clients.clear()


def test_unknown_lane_is_treated_as_background():
    """Fail closed: an unrecognised caller does not get the user's pool."""
    from src.llm_lane import BACKGROUND, normalize_lane

    assert normalize_lane("something-else") == BACKGROUND


def test_lane_scope_restores_the_previous_lane():
    from src.llm_lane import BACKGROUND, INTERACTIVE, current_lane, lane_scope

    assert current_lane() == INTERACTIVE
    with lane_scope(BACKGROUND):
        assert current_lane() == BACKGROUND
    assert current_lane() == INTERACTIVE
