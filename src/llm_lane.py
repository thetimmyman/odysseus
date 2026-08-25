"""LLM call lanes — keep background/utility model traffic separated from the
interactive chat turn a user is watching.

Odysseus fires a lot of *background* model work off the back of a normal chat
turn: memory extraction, skill extraction, the completion verifier, auto-naming,
teacher escalation, scheduled tasks. Every one of those calls used to share the
interactive path's process-wide state — one `httpx.AsyncClient` connection pool
and one response cache keyed only on (url, model, messages, temperature,
max_tokens). Nothing in that shared machinery knew which subsystem had asked, so
nothing could tell a background answer apart from the user's answer.

That mattered: on 2026-08-24 and 2026-08-25 two background completions were
persisted as the assistant's reply in a live user session (a memory-extractor
JSON array, and the skill extractor's literal `null` decline token — see
POS-AI-23). A lane makes the separation explicit and structural:

* each lane gets its own HTTP connection pool, so a background request that is
  abandoned mid-flight can never hand its socket to the stream the user is
  reading;
* the lane is part of the response-cache key, so a background completion can
  never be served out of cache to an interactive call (or vice versa).

The lane travels in a ``ContextVar``, so anything launched through
``src.background_tasks.spawn`` is automatically in the background lane without
each call site having to remember. Callers that make an in-turn utility call
(the completion verifier runs inside the user's request context) pass
``lane=BACKGROUND`` explicitly.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator, Optional

INTERACTIVE = "interactive"
BACKGROUND = "background"

_VALID_LANES = frozenset({INTERACTIVE, BACKGROUND})

_lane_var: ContextVar[str] = ContextVar("odysseus_llm_lane", default=INTERACTIVE)


def current_lane() -> str:
    """The lane the calling code is running in. Defaults to INTERACTIVE."""
    return _lane_var.get()


def normalize_lane(lane: Optional[str]) -> str:
    """Coerce a caller-supplied lane to a known value.

    ``None`` means "whatever lane I'm already in" — that is what lets the
    ContextVar set by ``background_tasks.spawn`` reach every nested call
    without threading a parameter through the whole stack. An unknown string
    is treated as background: an unrecognised caller is exactly the sort of
    traffic that should not share the user's connection pool.
    """
    if lane is None:
        return current_lane()
    if lane in _VALID_LANES:
        return lane
    return BACKGROUND


@contextmanager
def lane_scope(lane: str) -> Iterator[str]:
    """Run a block in ``lane``, restoring the previous lane on exit."""
    resolved = lane if lane in _VALID_LANES else BACKGROUND
    token = _lane_var.set(resolved)
    try:
        yield resolved
    finally:
        _lane_var.reset(token)
