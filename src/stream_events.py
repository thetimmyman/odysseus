"""One place that decides what counts as *answer* text in an agent SSE stream.

``stream_llm`` tags reasoning deltas with ``thinking: true`` and leaves answer
deltas untagged (src/llm_core.py). The interactive path honours that: the agent
loop accumulates only untagged deltas into ``full_response``, so reasoning never
reaches the persisted assistant message.

Every *background* consumer of ``stream_agent_loop`` was reimplementing the
accumulation by hand as ``if "delta" in data: text += data["delta"]`` — with no
``thinking`` check — so each one folded pages of raw model deliberation into what
it treated as the model's answer:

* ``src/bg_monitor.py`` persisted it as an assistant message in the user's
  session;
* ``src/task_scheduler.py`` returned it as the task's output (the text that goes
  out in reminders/notifications);
* ``src/teacher_escalation.py`` graded the teacher's turn on it;
* ``routes/skills_routes.py`` fed it to the skill-run evaluator;
* ``src/crew_orchestrator.py`` handed it to the next worker.

That is POS-AI-24 on the background side. Sharing one helper means the contract
is defined once and a future consumer inherits it instead of re-deriving it.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def is_thinking_event(data: Dict[str, Any]) -> bool:
    """True when an SSE payload carries reasoning rather than answer text."""
    return bool(isinstance(data, dict) and data.get("thinking"))


def answer_delta(data: Dict[str, Any]) -> Optional[str]:
    """The answer text in an SSE payload, or ``None``.

    Returns ``None`` for reasoning deltas, non-delta events, and non-string
    deltas — so callers can write ``if (d := answer_delta(data)) is not None``
    and be sure they are only accumulating what the user would see as the reply.
    """
    if not isinstance(data, dict):
        return None
    if is_thinking_event(data):
        return None
    delta = data.get("delta")
    if isinstance(delta, str):
        return delta
    return None
