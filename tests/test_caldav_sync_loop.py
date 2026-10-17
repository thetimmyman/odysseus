"""Periodic CalDAV pull: interval parsing, owner discovery, loop behaviour.

Everything runs on a fake clock (an injected ``sleep``) and fake sync callables;
no network, no sleeping, no real preferences file.
"""
import asyncio
import importlib
import logging
import random

import pytest

import src.caldav_sync as caldav_sync
import src.caldav_sync_loop as loop_mod
import src.user_preferences as prefs_mod


# ── interval ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "env, expected",
    [
        ({}, 600),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": ""}, 600),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "900"}, 900),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": " 120 "}, 120),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "0"}, 0),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "-5"}, 0),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "5"}, 60),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "59"}, 60),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "60"}, 60),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "soon"}, 600),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "300.7"}, 300),
    ],
)
def test_interval_from_environment(env, expected):
    assert loop_mod.sync_interval_seconds(env) == expected


def test_interval_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("CALDAV_SYNC_INTERVAL_SECONDS", "1200")
    assert loop_mod.sync_interval_seconds() == 1200
    monkeypatch.delenv("CALDAV_SYNC_INTERVAL_SECONDS")
    assert loop_mod.sync_interval_seconds() == 600


def test_disabled_loop_starts_nothing(monkeypatch):
    monkeypatch.setenv("CALDAV_SYNC_INTERVAL_SECONDS", "0")

    async def go():
        return loop_mod.start_caldav_sync_loop()

    assert asyncio.run(go()) is None


def test_enabled_loop_is_a_named_cancellable_task(monkeypatch):
    monkeypatch.setenv("CALDAV_SYNC_INTERVAL_SECONDS", "600")

    async def go():
        task = loop_mod.start_caldav_sync_loop()
        assert task is not None and task.get_name() == "caldav-sync-loop"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())


# ── owners ────────────────────────────────────────────────────────────────────

def test_owners_are_those_with_a_caldav_account(monkeypatch):
    monkeypatch.setattr(prefs_mod, "load_all", lambda: {
        "_users": {
            "zoe": {"caldav_accounts": [{"id": "a", "url": "https://dav.invalid/"}]},
            "alice": {"caldav": {"url": "https://dav.invalid/legacy"}},
            "bob": {"theme": "dark"},
            "carol": {"caldav": {"url": ""}},
            "dave": {"caldav_accounts": []},
            "erin": "not a dict",
        },
    })
    assert loop_mod.caldav_owners() == ["alice", "zoe"]


def test_flat_preferences_map_to_the_fallback_owner(monkeypatch):
    cal = importlib.import_module("routes.calendar_routes")

    monkeypatch.setattr(prefs_mod, "load_all", lambda: {"caldav": {"url": "https://dav.invalid/"}})
    assert loop_mod.caldav_owners() == [cal.FALLBACK_OWNER]
    monkeypatch.setattr(prefs_mod, "load_all", lambda: {"theme": "dark"})
    assert loop_mod.caldav_owners() == []


# ── one pass ──────────────────────────────────────────────────────────────────

def test_a_failing_owner_does_not_stop_the_pass(caplog):
    seen = []

    async def sync(owner):
        seen.append(owner)
        if owner == "bob":
            raise RuntimeError("server on fire")
        return {"calendars": 1, "events": 2, "deleted": 0, "errors": []}

    with caplog.at_level(logging.INFO, logger=loop_mod.logger.name):
        outcome = asyncio.run(loop_mod.run_sync_pass(["alice", "bob", "carol"], sync))

    assert seen == ["alice", "bob", "carol"]
    assert outcome["bob"] is None
    assert outcome["alice"]["events"] == 2 and outcome["carol"]["events"] == 2
    assert any("bob" in r.getMessage() and r.levelno == logging.ERROR for r in caplog.records)


def test_reported_errors_are_logged_as_a_warning_and_not_raised(caplog):
    async def sync(owner):
        return {"calendars": 0, "events": 0, "deleted": 0, "errors": ["Home: 401 Unauthorized"]}

    with caplog.at_level(logging.INFO, logger=loop_mod.logger.name):
        outcome = asyncio.run(loop_mod.run_sync_pass(["alice"], sync))

    assert outcome["alice"]["errors"] == ["Home: 401 Unauthorized"]
    assert any(r.levelno == logging.WARNING and "401" in r.getMessage() for r in caplog.records)


def test_pass_survives_owner_listing_failure(monkeypatch):
    def boom():
        raise OSError("prefs unreadable")

    monkeypatch.setattr(loop_mod, "caldav_owners", boom)
    assert asyncio.run(loop_mod.run_sync_pass(None, lambda owner: None)) == {}


def test_cancellation_is_not_swallowed_by_a_pass():
    async def sync(owner):
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(loop_mod.run_sync_pass(["alice"], sync))


# ── the loop ──────────────────────────────────────────────────────────────────

class _StopLoop(BaseException):
    """Ends the fake-clock loop; not an Exception, so the loop cannot swallow it."""


def _run_loop(interval, *, sync, owners, passes, rng=None):
    """Drive caldav_sync_loop on a fake clock for ``passes`` passes.

    Returns the list of requested sleeps, in order.
    """
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > passes:
            raise _StopLoop()

    async def go():
        await loop_mod.caldav_sync_loop(
            interval, sync=sync, list_owners=owners, sleep=fake_sleep, rng=rng or random.Random(7),
        )

    with pytest.raises(_StopLoop):
        asyncio.run(go())
    return sleeps


def test_loop_waits_a_short_random_time_then_pulls_every_interval():
    calls = []

    async def sync(owner):
        calls.append(owner)
        return {"calendars": 0, "events": 0, "deleted": 0, "errors": []}

    sleeps = _run_loop(600, sync=sync, owners=lambda: ["alice", "bob"], passes=3)
    low, high = loop_mod.STARTUP_DELAY_SECONDS
    assert low <= sleeps[0] <= high
    assert calls == ["alice", "bob"] * 3
    for wait in sleeps[1:]:
        assert 540 <= wait <= 660


def test_waits_are_jittered_not_constant():
    async def sync(owner):
        return {}

    sleeps = _run_loop(600, sync=sync, owners=lambda: ["alice"], passes=6)
    assert len(set(round(s, 3) for s in sleeps[1:])) > 1


def test_loop_keeps_going_after_a_failing_pass_and_a_failing_listing():
    attempts = {"list": 0, "sync": 0}

    def owners():
        attempts["list"] += 1
        if attempts["list"] == 1:
            raise OSError("prefs unreadable")
        return ["alice"]

    async def sync(owner):
        attempts["sync"] += 1
        raise RuntimeError("server on fire")

    _run_loop(600, sync=sync, owners=owners, passes=3)
    assert attempts["list"] == 3
    assert attempts["sync"] == 2


def test_loop_never_overlaps_itself():
    """A pass finishes before the next wait begins, even when a pull is slow."""
    events = []

    async def sync(owner):
        events.append(("start", owner))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        events.append(("end", owner))
        return {}

    sleeps_seen = []

    async def fake_sleep(seconds):
        sleeps_seen.append(len([e for e in events if e[0] == "end"]))
        if len(sleeps_seen) > 3:
            raise _StopLoop()

    async def go():
        await loop_mod.caldav_sync_loop(
            600, sync=sync, list_owners=lambda: ["alice", "bob"], sleep=fake_sleep, rng=random.Random(1),
        )

    with pytest.raises(_StopLoop):
        asyncio.run(go())
    # Strictly start/end pairs, in owner order: never two pulls in flight.
    assert events == [
        ("start", "alice"), ("end", "alice"), ("start", "bob"), ("end", "bob"),
    ] * 3
    # Each wait begins only once all pulls so far have ended.
    assert sleeps_seen == [0, 2, 4, 6]


def test_loop_does_nothing_while_no_owner_has_an_account():
    pulls = []

    async def sync(owner):
        pulls.append(owner)
        return {}

    sleeps = _run_loop(600, sync=sync, owners=lambda: [], passes=3)
    assert pulls == [] and len(sleeps) == 4


def test_jittered_stays_within_ten_percent():
    rng = random.Random(3)
    values = [loop_mod.jittered(1000, rng) for _ in range(500)]
    assert min(values) >= 900 and max(values) <= 1100
    assert max(values) - min(values) > 100


# ── per-owner serialization of the pull itself ────────────────────────────────

def test_syncs_for_one_owner_never_run_together(monkeypatch):
    """Loop, calendar route and token route all call sync_caldav; they queue."""
    monkeypatch.setattr(caldav_sync, "_OWNER_LOCKS", {})
    active = {"alice": 0, "bob": 0}
    peak = {"alice": 0, "bob": 0}
    both_running = []

    async def fake_locked(owner):
        active[owner] += 1
        peak[owner] = max(peak[owner], active[owner])
        both_running.append(active["alice"] > 0 and active["bob"] > 0)
        await asyncio.sleep(0.01)
        active[owner] -= 1
        return {"calendars": 0, "events": 0, "deleted": 0, "errors": []}

    monkeypatch.setattr(caldav_sync, "_sync_caldav_locked", fake_locked)

    async def go():
        await asyncio.gather(
            *[caldav_sync.sync_caldav("alice") for _ in range(4)],
            *[caldav_sync.sync_caldav("bob") for _ in range(2)],
        )

    asyncio.run(go())
    assert peak == {"alice": 1, "bob": 1}
    # Different owners are independent of each other.
    assert any(both_running)


def test_a_failed_sync_releases_the_owner_lock(monkeypatch):
    monkeypatch.setattr(caldav_sync, "_OWNER_LOCKS", {})
    calls = []

    async def flaky(owner):
        calls.append(owner)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return {"calendars": 0, "events": 0, "deleted": 0, "errors": []}

    monkeypatch.setattr(caldav_sync, "_sync_caldav_locked", flaky)

    async def go():
        with pytest.raises(RuntimeError):
            await caldav_sync.sync_caldav("alice")
        return await asyncio.wait_for(caldav_sync.sync_caldav("alice"), timeout=2)

    assert asyncio.run(go())["errors"] == []
