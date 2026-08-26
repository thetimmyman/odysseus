"""Regression tests for POS-AI-24 on the background side.

``stream_llm`` tags reasoning deltas ``thinking: true``. The interactive agent
loop honours that; every background consumer of ``stream_agent_loop`` did not —
each one accumulated ``data["delta"]`` unconditionally, so raw model
deliberation was folded into what it treated as the model's answer. In
``bg_monitor`` that answer is persisted into a user's session as an assistant
message; in ``task_scheduler`` it goes out in reminders.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.stream_events import answer_delta, is_thinking_event  # noqa: E402


def test_reasoning_delta_is_not_answer_text():
    assert answer_delta({"delta": "Let me think about this…", "thinking": True}) is None


def test_plain_delta_is_answer_text():
    assert answer_delta({"delta": "Done."}) == "Done."


def test_empty_answer_delta_is_preserved_not_dropped():
    # "" is a real (empty) answer delta, distinct from "no answer here".
    assert answer_delta({"delta": ""}) == ""


def test_non_delta_events_are_not_answer_text():
    assert answer_delta({"type": "tool_output", "output": "hi"}) is None
    assert answer_delta({"type": "agent_step", "round": 2}) is None


def test_non_string_delta_is_ignored():
    assert answer_delta({"delta": None}) is None
    assert answer_delta({"delta": 42}) is None


def test_is_thinking_event():
    assert is_thinking_event({"delta": "x", "thinking": True}) is True
    assert is_thinking_event({"delta": "x"}) is False
    assert is_thinking_event("not a dict") is False


def _sse(payload):
    return "data: " + json.dumps(payload) + "\n\n"


def test_bg_monitor_does_not_persist_reasoning_as_the_answer(monkeypatch):
    """The concrete path that writes into a live user session."""
    import asyncio

    from src import bg_monitor

    events = [
        _sse({"delta": "First I should check the memory block…", "thinking": True}),
        _sse({"delta": "The user's full name is on file, but I won't use it.",
              "thinking": True}),
        _sse({"type": "agent_step", "round": 2}),
        _sse({"delta": "The transcription finished; the text is in out.txt."}),
        "data: [DONE]\n\n",
    ]

    async def fake_stream_agent_loop(*_a, **_kw):
        for e in events:
            yield e

    import src.agent_loop as agent_loop
    monkeypatch.setattr(agent_loop, "stream_agent_loop", fake_stream_agent_loop)

    class _Sess:
        id = "sess-1"
        endpoint_url = "http://x/v1"
        model = "m"
        headers = None
        owner = None

    full, tool_events = asyncio.run(bg_monitor._drain_agent(_Sess(), []))
    assert full == "The transcription finished; the text is in out.txt."
    assert "won't use it" not in full
    assert tool_events == []
