"""Regression tests for the background-task registry (POS-AI-23).

Every background LLM task in Odysseus used to be started as a bare
``asyncio.create_task(...)`` whose return value was thrown away. asyncio only
keeps a *weak* reference to a running task, so the garbage collector was free to
finalize one mid-``await`` — and a coroutine finalized that way cannot run async
cleanup, which is how ``httpx`` releases a connection. Holding a reference is
the documented fix; putting the task in the background LLM lane is what keeps
its model traffic off the interactive connection pool.
"""

import asyncio
import gc
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import background_tasks  # noqa: E402
from src.llm_lane import BACKGROUND, INTERACTIVE, current_lane  # noqa: E402


def test_spawned_task_survives_a_garbage_collection():
    async def scenario():
        started = asyncio.Event()
        finished = asyncio.Event()

        async def work():
            started.set()
            await asyncio.sleep(0.05)
            finished.set()

        background_tasks.spawn(work(), name="gc-probe")   # return value discarded
        await started.wait()
        # The only strong reference is the registry's. Without it the task is
        # collectable here and can be finalized mid-await.
        gc.collect()
        await asyncio.wait_for(finished.wait(), timeout=2)
        return True

    assert asyncio.run(scenario()) is True


def test_spawned_task_runs_in_the_background_lane():
    async def scenario():
        seen = {}

        async def work():
            seen["lane"] = current_lane()

        await background_tasks.spawn(work(), name="lane-probe")
        return seen["lane"]

    assert asyncio.run(scenario()) == BACKGROUND


def test_lane_is_inherited_by_nested_awaits():
    """A background task's model call is usually several frames deep — the lane
    has to reach it without every layer passing a parameter."""
    async def scenario():
        seen = {}

        async def inner():
            seen["lane"] = current_lane()

        async def outer():
            await inner()

        await background_tasks.spawn(outer())
        return seen["lane"]

    assert asyncio.run(scenario()) == BACKGROUND


def test_caller_lane_is_not_disturbed():
    async def scenario():
        async def work():
            await asyncio.sleep(0)

        await background_tasks.spawn(work())
        return current_lane()

    assert asyncio.run(scenario()) == INTERACTIVE


def test_registry_drains_on_completion():
    async def scenario():
        async def work():
            await asyncio.sleep(0)

        task = background_tasks.spawn(work())
        assert background_tasks.pending_count() >= 1
        await task
        await asyncio.sleep(0)  # let the done-callback run
        return background_tasks.pending_count()

    assert asyncio.run(scenario()) == 0


def test_failures_are_logged_not_swallowed(caplog):
    async def scenario():
        async def boom():
            raise RuntimeError("kaboom")

        task = background_tasks.spawn(boom(), name="boom")
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)

    with caplog.at_level("WARNING"):
        asyncio.run(scenario())
    assert any("[bg-task]" in r.getMessage() for r in caplog.records)
