"""One place that decides what counts as *answer* text in an agent SSE stream.

``stream_llm`` tags reasoning deltas with ``thinking: true``. Every consumer of
``stream_agent_loop`` must use this helper so reasoning never leaks into what it
treats as the model's answer.
"""

from __future__ import annotations

from typing import Any, Dict, Optional


def is_thinking_event(data: Dict[str, Any]) -> bool:
    """True when an SSE payload carries reasoning rather than answer text."""
    return bool(isinstance(data, dict) and data.get("thinking"))


def answer_delta(data: Dict[str, Any]) -> Optional[str]:
    """The answer text in an SSE payload, or ``None`` for reasoning and non-delta events."""
    if not isinstance(data, dict):
        return None
    if is_thinking_event(data):
        return None
    delta = data.get("delta")
    if isinstance(delta, str):
        return delta
    return None
