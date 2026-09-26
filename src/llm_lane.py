"""LLM call lanes: keep background model traffic separate from the interactive
chat turn.

Background work (memory/skill extraction, the completion verifier, naming,
scheduled tasks) must never cross into the user's reply. Each lane gets its own
HTTP connection pool, so an abandoned background request can't hand its socket
to the user's stream, and the lane is part of the response-cache key.

The lane travels in a ``ContextVar``, so ``src.background_tasks.spawn`` puts
everything nested in the background lane; in-turn utility calls (e.g. the
verifier) pass ``lane=BACKGROUND`` explicitly.
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
    """Coerce a caller-supplied lane. ``None`` means the current ContextVar lane;
    an unknown string is treated as background, keeping it off the user's pool.
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
