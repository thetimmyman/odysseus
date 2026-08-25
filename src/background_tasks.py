"""Fire-and-forget background task registry.

Two jobs:

1. **Keep a strong reference.** ``asyncio`` only holds a *weak* reference to a
   running task (see the ``asyncio.create_task`` docs: "Save a reference to the
   result of this function, to avoid a task disappearing mid-execution"). Every
   background LLM task in Odysseus was started as a bare
   ``asyncio.create_task(...)`` with the return value discarded, so the garbage
   collector was free to finalize it mid-``await``. A task finalized that way
   cannot run its async cleanup — ``httpx``'s connection teardown included —
   which leaves the shared connection pool in a state nobody reasoned about.
   Registering the task here removes that whole failure mode.

2. **Put the task in the background LLM lane** (``src.llm_lane``) so every model
   call it makes, however deeply nested, uses the background connection pool and
   the background response-cache namespace instead of the interactive ones.

Exceptions are logged rather than swallowed silently — an un-awaited task that
raises used to produce only a "Task exception was never retrieved" warning at
GC time, long after the context that would explain it was gone.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Optional, Set

from src.llm_lane import BACKGROUND, lane_scope

logger = logging.getLogger(__name__)

# Strong references to in-flight background tasks. Entries are removed by the
# done-callback, so this stays bounded by actual concurrency, not by history.
_TASKS: Set[asyncio.Task] = set()


def _on_done(task: asyncio.Task) -> None:
    _TASKS.discard(task)
    if task.cancelled():
        logger.debug("[bg-task] %s cancelled", task.get_name())
        return
    exc = task.exception()
    if exc is not None:
        logger.warning(
            "[bg-task] %s failed: %s", task.get_name(), exc, exc_info=exc,
        )


async def _run_in_lane(coro: Awaitable[Any]) -> Any:
    with lane_scope(BACKGROUND):
        return await coro


def spawn(coro: Awaitable[Any], *, name: Optional[str] = None) -> asyncio.Task:
    """Start ``coro`` as a tracked background task in the background LLM lane.

    Drop-in replacement for ``asyncio.create_task`` at fire-and-forget call
    sites. Returns the task so tests (and callers that want to await it) can.
    """
    task = asyncio.ensure_future(_run_in_lane(coro))
    if name:
        try:
            task.set_name(name)
        except AttributeError:  # pragma: no cover - very old event loops
            pass
    _TASKS.add(task)
    task.add_done_callback(_on_done)
    return task


def pending_count() -> int:
    """Number of tracked background tasks still running (diagnostics/tests)."""
    return len(_TASKS)
