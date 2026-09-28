"""chat.js must close the ``<think>`` block at each agent round boundary.

A round ending on a tool call emits reasoning but no answer delta, so without a
boundary close ``_thinkOpen`` leaks into the next round and raw deliberation
(including guarded system-prompt content) renders as the answer.
"""

import os
import re

CHAT_JS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "static", "js", "chat.js",
)


def _source():
    with open(CHAT_JS, "r", encoding="utf-8") as f:
        return f.read()


def _handler_body(src, marker, length=2600):
    idx = src.index(marker)
    return src[idx:idx + length]


def test_agent_step_closes_an_open_think_block():
    body = _handler_body(_source(), "json.type === 'agent_step'")
    assert "_thinkOpen" in body, (
        "the agent_step round boundary must reset the thinking state; leaving it "
        "open leaks round 2+ reasoning into the answer body"
    )
    assert "</think>" in body
    assert re.search(r"_thinkOpen\s*=\s*false", body)


def test_teacher_takeover_closes_an_open_think_block():
    body = _handler_body(_source(), "json.type === 'teacher_takeover'")
    assert "_thinkOpen" in body
    assert re.search(r"_thinkOpen\s*=\s*false", body)


def test_think_state_is_still_opened_lazily_on_a_thinking_delta():
    src = _source()
    # The wrap itself must stay as-is: open on the first thinking delta, close
    # on the first answer delta.
    assert "if (!_thinkOpen) { _delta = '<think>' + _delta; _thinkOpen = true; }" in src
    assert "_delta = '</think>' + _delta; _thinkOpen = false;" in src


def _simulate(events):
    """Mirror of chat.js's wrapping state machine, including the boundary close."""
    accumulated = ""
    think_open = False
    for ev in events:
        if ev.get("type") == "agent_step":
            if think_open:
                accumulated += "</think>"
                think_open = False
            continue
        delta = ev["delta"]
        if ev.get("thinking"):
            if not think_open:
                delta = "<think>" + delta
                think_open = True
        elif think_open:
            delta = "</think>" + delta
            think_open = False
        accumulated += delta
    if think_open:
        accumulated += "</think>"
    return accumulated


def test_every_round_gets_its_own_balanced_think_block():
    # Round 1: reasoning then a tool call (no answer delta) -> agent_step.
    # Round 2: more reasoning, then the real answer.
    out = _simulate([
        {"delta": "r1 reasoning", "thinking": True},
        {"type": "agent_step"},
        {"delta": "r2 reasoning", "thinking": True},
        {"delta": "Here is the answer."},
    ])
    assert out.count("<think>") == 2
    assert out.count("</think>") == 2
    assert out == "<think>r1 reasoning</think><think>r2 reasoning</think>Here is the answer."
    # The visible reply is exactly the answer — no deliberation, no orphan tag.
    visible = re.sub(r"<think>.*?</think>", "", out, flags=re.S)
    assert visible == "Here is the answer."


def test_reasoning_does_not_leak_when_several_rounds_end_on_tool_calls():
    out = _simulate([
        {"delta": "r1 thinks", "thinking": True},
        {"type": "agent_step"},
        {"delta": "r2 thinks", "thinking": True},
        {"type": "agent_step"},
        {"delta": "r3 thinks", "thinking": True},
        {"delta": "Done."},
    ])
    visible = re.sub(r"<think>.*?</think>", "", out, flags=re.S)
    assert visible == "Done."
    assert "</think>" not in visible
