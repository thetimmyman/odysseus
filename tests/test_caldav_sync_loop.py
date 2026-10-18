"""Periodic CalDAV pull: interval parsing, owner discovery, loop behaviour.

Everything runs on a fake clock (an injected ``sleep``) and fake sync callables;
no network, no sleeping, no real preferences file.
"""
import asyncio
import contextvars
import importlib
import logging
import random
import threading
import time

import pytest

import src.caldav_sync as caldav_sync
import src.caldav_sync_loop as loop_mod
import src.user_preferences as prefs_mod


@pytest.fixture(autouse=True)
def _no_real_preferences(monkeypatch):
    """A pass sizes its wait to the owner's accounts, which reads the preference
    store; never let a test read a real one."""
    monkeypatch.setattr(prefs_mod, "load_all", lambda: {})


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
        # Only a value that is exactly zero or negative turns the loop off. A
        # positive fraction used to be truncated to 0 first and so disabled it.
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "0.5"}, 60),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "0.001"}, 60),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "1e-9"}, 60),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "59.9"}, 60),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "60.4"}, 60),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "0.0"}, 0),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "-0.5"}, 0),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "-0"}, 0),
        # Not usable as a number of seconds: the default, never a crash or a disable.
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "inf"}, 600),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "-inf"}, 600),
        ({"CALDAV_SYNC_INTERVAL_SECONDS": "nan"}, 600),
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


def test_a_positive_fraction_keeps_the_loop_running_at_the_floor(monkeypatch):
    monkeypatch.setenv("CALDAV_SYNC_INTERVAL_SECONDS", "0.5")

    async def go():
        task = loop_mod.start_caldav_sync_loop()
        assert task is not None, "0.5 is positive: it is raised to the floor, not disabled"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())


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


@pytest.mark.parametrize("cancel_count, worker_fails", [(1, False), (2, False), (1, True), (2, True)])
def test_cancelled_caller_keeps_owner_custody_until_blocking_pull_finishes(monkeypatch, cancel_count, worker_fails):
    import src.secret_storage as secrets

    marker = contextvars.ContextVar("synthetic_sync_marker", default=None)
    monkeypatch.setattr(caldav_sync, "_OWNER_LOCKS", {})
    monkeypatch.setattr(caldav_sync, "_load_caldav_accounts", lambda owner: [
        {"url": "https://dav.invalid/", "username": "u", "password": "p"},
    ])
    monkeypatch.setattr(caldav_sync, "validate_caldav_url", lambda url: url)
    monkeypatch.setattr(secrets, "decrypt", lambda value: value)

    async def go():
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        calls = []
        contexts = []
        active = set()
        finished = []
        failed = []
        state_lock = threading.Lock()
        max_active = 0

        def blocking(owner, *args):
            nonlocal max_active
            with state_lock:
                call_index = len(calls) + 1
                calls.append(owner)
                contexts.append(marker.get())
                active.add(call_index)
                max_active = max(max_active, len(active))
            try:
                assert marker.get() == "synthetic-context"
                if call_index == 1:
                    loop.call_soon_threadsafe(started.set)
                    if not release.wait(2):
                        raise AssertionError("test did not release its owned worker")
                    if worker_fails:
                        with state_lock:
                            failed.append(call_index)
                        raise RuntimeError("synthetic blocking pull failed")
                return {"calendars": 1, "events": 0, "deleted": 0, "errors": []}
            finally:
                with state_lock:
                    active.remove(call_index)
                    finished.append(call_index)

        monkeypatch.setattr(caldav_sync, "_sync_blocking", blocking)
        token = marker.set("synthetic-context")
        first = asyncio.create_task(caldav_sync.sync_caldav("alice"))
        second = None
        try:
            await asyncio.wait_for(started.wait(), 1)
            second = asyncio.create_task(caldav_sync.sync_caldav("alice"))
            for _ in range(cancel_count):
                assert first.cancel()
                await asyncio.sleep(0)
                assert not first.done(), "cancellation returned while its blocking writer was still active"
                with state_lock:
                    assert calls == ["alice"]
                    assert active == {1}
        finally:
            release.set()
            tasks = [first] + ([second] if second is not None else [])
            results = await asyncio.gather(*tasks, return_exceptions=True)
            marker.reset(token)
        assert isinstance(results[0], asyncio.CancelledError)
        assert results[1]["calendars"] == 1
        assert results[1]["errors"] == []
        assert all(task.done() for task in tasks)
        assert calls == ["alice", "alice"]
        assert contexts == ["synthetic-context", "synthetic-context"]
        assert max_active == 1
        assert finished == [1, 2]
        assert active == set()
        assert failed == ([1] if worker_fails else [])

    asyncio.run(go())


# ── a stuck owner cannot stall the loop ───────────────────────────────────────

def _hold_until(release):
    """Wait for ``release`` the way the real pull does once cancelled: the
    blocking worker cannot be interrupted, so the await keeps going until it is
    done and only then reports the cancellation."""
    async def wait():
        cancelled = False
        while not release.is_set():
            try:
                await asyncio.sleep(0.005)
            except asyncio.CancelledError:
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError()
    return wait()


def _ok():
    return {"calendars": 1, "events": 1, "deleted": 0, "errors": []}


def test_a_stuck_owner_is_abandoned_and_the_rest_of_the_pass_still_runs(caplog):
    async def go():
        release = asyncio.Event()
        entered = []

        async def sync(owner):
            entered.append(owner)
            if owner == "alice":
                await _hold_until(release)
            return _ok()

        try:
            with caplog.at_level(logging.INFO, logger=loop_mod.logger.name):
                outcome = await asyncio.wait_for(
                    loop_mod.run_sync_pass(["alice", "bob", "carol"], sync, timeout=0.05), timeout=3,
                )
        finally:
            release.set()
        return entered, outcome

    entered, outcome = asyncio.run(go())
    assert entered == ["alice", "bob", "carol"]
    assert outcome["alice"] is None
    assert outcome["bob"]["events"] == 1 and outcome["carol"]["events"] == 1
    assert any(r.levelno == logging.ERROR and "alice" in r.getMessage() and "timed out" in r.getMessage()
               for r in caplog.records)


def test_an_owner_whose_pull_is_still_running_is_not_started_again():
    """The abandoned pull cannot be killed, so later passes leave that owner
    alone until it ends instead of piling up a second pull behind the first."""
    async def go():
        release = asyncio.Event()
        entered = []
        pending = {}

        async def sync(owner):
            entered.append(owner)
            if owner == "alice":
                await _hold_until(release)
            return _ok()

        async def one_pass():
            return await asyncio.wait_for(
                loop_mod.run_sync_pass(["alice", "bob"], sync, timeout=0.05, pending=pending), timeout=3,
            )

        try:
            first = await one_pass()
            second = await one_pass()
            while_stuck = list(entered)
            release.set()
            async def drained():
                while pending:  # the abandoned pull ends and takes itself off the list
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(drained(), timeout=3)
            third = await one_pass()
        finally:
            release.set()
        return while_stuck, entered, (first, second, third)

    while_stuck, entered, (first, second, third) = asyncio.run(go())
    assert while_stuck == ["alice", "bob", "bob"]
    assert first["alice"] is None and second["alice"] is None
    assert second["bob"]["events"] == 1
    assert entered == ["alice", "bob", "bob", "alice", "bob"]
    assert third["alice"]["events"] == 1


def test_the_loop_keeps_pulling_the_other_owners_past_a_stuck_one():
    pulls = []

    async def go():
        release = asyncio.Event()

        async def sync(owner):
            pulls.append(owner)
            if owner == "alice":
                await _hold_until(release)
            return _ok()

        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) > 3:
                raise _StopLoop()

        try:
            await asyncio.wait_for(
                loop_mod.caldav_sync_loop(
                    600, sync=sync, list_owners=lambda: ["alice", "bob"], sleep=fake_sleep,
                    rng=random.Random(1), owner_timeout=0.05,
                ),
                timeout=5,
            )
        finally:
            release.set()

    with pytest.raises(_StopLoop):
        asyncio.run(go())
    assert pulls.count("bob") == 3 and pulls.count("alice") == 1


def test_cancelling_a_pass_cancels_the_pull_in_flight():
    async def go():
        started = asyncio.Event()
        cancelled = []

        async def sync(owner):
            started.set()
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled.append(owner)
                raise

        task = asyncio.create_task(loop_mod.run_sync_pass(["alice"], sync, timeout=3600))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.01)
        return cancelled

    assert asyncio.run(go()) == ["alice"]


def test_a_pull_that_cannot_be_interrupted_does_not_hold_up_other_owners(monkeypatch):
    """End to end through the real sync_caldav: the stuck worker thread keeps its
    owner's lock (cancelling an await cannot stop a thread), and the loop moves on."""
    import src.secret_storage as secrets

    monkeypatch.setattr(caldav_sync, "_OWNER_LOCKS", {})
    monkeypatch.setattr(caldav_sync, "_load_caldav_accounts", lambda owner: [
        {"url": "https://dav.invalid/", "username": "u", "password": "p"},
    ])
    monkeypatch.setattr(caldav_sync, "validate_caldav_url", lambda url: url)
    monkeypatch.setattr(secrets, "decrypt", lambda value: value)
    release = threading.Event()
    calls = []

    def blocking(owner, *args):
        calls.append(owner)
        if owner == "alice":
            release.wait(5)
        return {"calendars": 1, "events": 1, "deleted": 0, "errors": []}

    monkeypatch.setattr(caldav_sync, "_sync_blocking", blocking)

    async def go():
        pending = {}
        try:
            outcome = await asyncio.wait_for(
                loop_mod.run_sync_pass(["alice", "bob"], caldav_sync.sync_caldav, timeout=0.1, pending=pending),
                timeout=3,
            )
            again = await asyncio.wait_for(
                loop_mod.run_sync_pass(["alice", "bob"], caldav_sync.sync_caldav, timeout=0.1, pending=pending),
                timeout=3,
            )
        finally:
            release.set()
        await asyncio.gather(*pending.values(), return_exceptions=True)
        return outcome, again, dict(pending)

    outcome, again, left = asyncio.run(go())
    assert outcome["alice"] is None and outcome["bob"]["calendars"] == 1
    assert again["alice"] is None and again["bob"]["calendars"] == 1
    assert calls == ["alice", "bob", "bob"]
    assert left == {}


def test_owner_timeout_outlasts_the_pull_budget_it_supervises():
    env = {"CALDAV_SYNC_TIMEOUT_SECONDS": "120", "CALDAV_REQUEST_TIMEOUT_SECONDS": "20"}
    assert loop_mod.owner_timeout_seconds(env) > 140
    assert loop_mod.owner_timeout_seconds({}) > caldav_sync.DEFAULT_SYNC_TIMEOUT_SECONDS


def test_owner_timeout_covers_every_account_of_the_owner():
    """Accounts are pulled one after another, each with its own budget."""
    env = {"CALDAV_SYNC_TIMEOUT_SECONDS": "120", "CALDAV_REQUEST_TIMEOUT_SECONDS": "20"}
    one = loop_mod.owner_timeout_seconds(env)
    assert loop_mod.owner_timeout_seconds(env, accounts=1) == one
    assert loop_mod.owner_timeout_seconds(env, accounts=2) == one + 140
    assert loop_mod.owner_timeout_seconds(env, accounts=3) > 3 * 140
    # Never less than one account's worth, whatever the count says.
    assert loop_mod.owner_timeout_seconds(env, accounts=0) == one
    assert loop_mod.owner_timeout_seconds(env, accounts=-2) == one


@pytest.mark.parametrize(
    "prefs, expected",
    [
        ({"_users": {"alice": {"caldav_accounts": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}}}, 3),
        ({"_users": {"alice": {"caldav_accounts": [{"id": "a"}]}}}, 1),
        ({"_users": {"alice": {"caldav": {"url": "https://dav.invalid/legacy"}}}}, 1),
        ({"_users": {"alice": {"caldav_accounts": []}}}, 1),
        ({"_users": {"alice": {"caldav_accounts": "garbage"}}}, 1),
        ({"_users": {"bob": {"caldav_accounts": [{"id": "a"}, {"id": "b"}]}}}, 1),
        ({"caldav_accounts": [{"id": "a"}, {"id": "b"}]}, 2),  # the flat, no-auth layout
        ({}, 1),
    ],
)
def test_account_count_of_an_owner(monkeypatch, prefs, expected):
    monkeypatch.setattr(prefs_mod, "load_all", lambda: prefs)
    assert loop_mod.owner_account_count("alice") == expected


def test_account_count_falls_back_to_one_when_preferences_cannot_be_read(monkeypatch):
    def boom():
        raise OSError("prefs unreadable")

    monkeypatch.setattr(prefs_mod, "load_all", boom)
    assert loop_mod.owner_account_count("alice") == 1


def test_a_pass_waits_longer_for_an_owner_with_more_accounts(monkeypatch):
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "100")
    monkeypatch.setenv("CALDAV_REQUEST_TIMEOUT_SECONDS", "10")
    monkeypatch.setattr(prefs_mod, "load_all", lambda: {"_users": {
        "alice": {"caldav_accounts": [{"id": "a"}, {"id": "b"}, {"id": "c"}]},
        "bob": {"caldav_accounts": [{"id": "d"}]},
    }})
    waits = {}

    async def fake_pull(owner, sync, timeout, pending):
        waits[owner] = timeout
        return _ok()

    monkeypatch.setattr(loop_mod, "_pull_owner", fake_pull)
    asyncio.run(loop_mod.run_sync_pass(["alice", "bob"], lambda owner: None))

    assert waits["alice"] == 3 * 110 + loop_mod.OWNER_TIMEOUT_GRACE_SECONDS
    assert waits["bob"] == 110 + loop_mod.OWNER_TIMEOUT_GRACE_SECONDS

    waits.clear()
    asyncio.run(loop_mod.run_sync_pass(["alice", "bob"], lambda owner: None, timeout=7))
    assert waits == {"alice": 7, "bob": 7}, "an explicit timeout applies to every owner as given"


def test_an_owner_whose_accounts_are_each_slow_is_not_cut_off_part_way(monkeypatch):
    """The reported scenario, scaled down: three accounts that each take most of
    one account's wait, so together they take far longer than it. Sized for one
    account the pass cancelled the owner during the second, which then ran on
    unwatched while the owner was skipped, and the third never started."""
    import src.secret_storage as secrets

    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "0.3")
    monkeypatch.setenv("CALDAV_REQUEST_TIMEOUT_SECONDS", "0.1")
    monkeypatch.setattr(loop_mod, "OWNER_TIMEOUT_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(prefs_mod, "load_all", lambda: {"_users": {"alice": {"caldav_accounts": [{}, {}, {}]}}})
    monkeypatch.setattr(caldav_sync, "_OWNER_LOCKS", {})
    monkeypatch.setattr(caldav_sync, "_load_caldav_accounts", lambda owner: [
        {"id": f"acct-{n}", "url": "https://dav.invalid/", "username": "u", "password": "p"} for n in range(3)
    ])
    monkeypatch.setattr(caldav_sync, "validate_caldav_url", lambda url: url)
    monkeypatch.setattr(secrets, "decrypt", lambda value: value)
    started = []

    def slow_account(owner, url, user, pw, account_id):
        started.append(account_id)
        time.sleep(0.3)  # inside the account's own wait (0.4s); the three together are not
        return {"calendars": 1, "events": 1, "deleted": 0, "errors": []}

    monkeypatch.setattr(caldav_sync, "_sync_blocking", slow_account)

    async def go():
        pending = {}
        outcome = await asyncio.wait_for(
            loop_mod.run_sync_pass(["alice"], caldav_sync.sync_caldav, pending=pending), timeout=10,
        )
        return outcome, dict(pending)

    outcome, left = asyncio.run(go())

    assert started == ["acct-0", "acct-1", "acct-2"]
    assert outcome["alice"] is not None and outcome["alice"]["calendars"] == 3
    assert outcome["alice"]["errors"] == []
    assert left == {}
