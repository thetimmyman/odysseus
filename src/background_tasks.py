"""Fire-and-forget background task registry.

1. Keep a strong reference: asyncio holds only a weak one, so a discarded task
   can be garbage-collected mid-await and skip its async cleanup (e.g. httpx
   connection teardown).
2. Run the task in the background LLM lane (``src.llm_lane``) so its model calls
   use the background connection pool and cache namespace.

Exceptions are logged rather than surfacing only as a GC-time warning.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Optional, Set

from src.llm_lane import BACKGROUND, lane_scope

logger = logging.getLogger(__name__)

# Removed by the done-callback, so bounded by concurrency, not history.
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
    """Drop-in for ``asyncio.create_task`` at fire-and-forget sites; returns the task."""
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
