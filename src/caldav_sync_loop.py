"""Periodic CalDAV pull.

``src/caldav_sync.py`` documents a scheduler loop, but until now the pull only
ran when somebody opened the calendar or hit ``POST /api/calendar/sync``, so a
read model with nobody looking at it went stale (days, in one measured case).
This module is that loop: every ``CALDAV_SYNC_INTERVAL_SECONDS`` (default 600,
``0`` disables) it pulls each owner that has a CalDAV account configured.

* Owners are processed one after another and a pass finishes before the next
  sleep starts, so the loop never overlaps itself. ``sync_caldav`` also
  serializes per owner, so a pass cannot race a manual sync either.
* One owner cannot stall the rest. The CalDAV client has a finite network
  timeout, a pull of one account stops itself when its time budget is spent and
  no single request outlasts that budget, however slowly the server answers (see
  ``src/caldav_sync.py``), and the loop does not wait for an owner beyond
  ``owner_timeout_seconds()`` either: it cancels that pull, moves on, and leaves
  the owner alone in later passes until the abandoned pull has really ended. An
  owner's accounts are pulled one after another, each with a budget of its own,
  so that wait grows with the number of accounts, up to ``MAX_CALDAV_ACCOUNTS``:
  an owner can neither save nor have pulled more than that, so the longest an
  owner can hold the pass is a fixed time that no preference setting changes (about
  55 minutes with the default budgets, and only for an owner whose every account
  is served as slowly as the budget allows). A blocking worker thread cannot be
  interrupted, so it may outlive its timeout; skipping the owner keeps such a
  straggler from being joined by a second pull.
* A failure (bad credentials, server down, a bug) is logged and the loop carries
  on; one owner's failure does not stop the others.
* The wait between passes is jittered by +/-10% so a restart does not line every
  instance up on the CalDAV server, and the first pass waits a short random time
  so application startup is not slowed.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import os
import random
from typing import Awaitable, Callable, Iterable, MutableMapping, Optional

logger = logging.getLogger(__name__)

ENV_INTERVAL = "CALDAV_SYNC_INTERVAL_SECONDS"
DEFAULT_INTERVAL_SECONDS = 600
# Floor for the configured interval: below this a mistyped value would hammer
# the CalDAV server (Google rate-limits aggressively).
MIN_INTERVAL_SECONDS = 60
JITTER_FRACTION = 0.1
STARTUP_DELAY_SECONDS = (15.0, 45.0)
# Extra time a pass waits for an owner on top of the pull's own limits, so a pull
# that merely runs long stops itself and reports what it did before the loop
# gives up on it.
OWNER_TIMEOUT_GRACE_SECONDS = 5.0


def sync_interval_seconds(environ=None) -> int:
    """Seconds between passes from the environment; 0 means disabled."""
    env = os.environ if environ is None else environ
    raw = str(env.get(ENV_INTERVAL, "")).strip()
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        logger.warning("%s=%r is not a number; using %ss", ENV_INTERVAL, raw, DEFAULT_INTERVAL_SECONDS)
        return DEFAULT_INTERVAL_SECONDS
    # Decide on the number as written: truncating first would turn 0.5 into 0
    # and so switch the loop off, when only a value <= 0 means "disabled".
    if value <= 0:
        return 0
    if value < MIN_INTERVAL_SECONDS:
        logger.warning("%s=%s is below the %ss floor; using the floor", ENV_INTERVAL, raw, MIN_INTERVAL_SECONDS)
        return MIN_INTERVAL_SECONDS
    return int(value)


def _has_caldav(prefs) -> bool:
    if not isinstance(prefs, dict):
        return False
    if prefs.get("caldav_accounts"):
        return True
    legacy = prefs.get("caldav")
    return isinstance(legacy, dict) and bool(legacy.get("url"))


def caldav_owners() -> list:
    """Owners with at least one CalDAV account configured, sorted.

    Reads the preference store directly. In the multi-user layout each key under
    ``_users`` is an owner. In the legacy flat layout (no auth) the one set of
    preferences belongs to the calendar routes' fallback owner.
    """
    from src.user_preferences import load_all

    prefs = load_all()
    users = prefs.get("_users") if isinstance(prefs, dict) else None
    if isinstance(users, dict):
        return sorted(name for name, p in users.items() if name and _has_caldav(p))
    if _has_caldav(prefs):
        from routes.calendar_routes import FALLBACK_OWNER
        return [FALLBACK_OWNER]
    return []


def owner_account_count(owner: str) -> int:
    """How many CalDAV accounts a pull of ``owner`` walks: at least 1, at most
    ``MAX_CALDAV_ACCOUNTS`` (a pull walks no more, however many are saved).

    A read of the preference store only (unlike ``_load_caldav_accounts`` it
    migrates and re-encrypts nothing); any trouble reading it counts as one.
    """
    try:
        from src.caldav_sync import MAX_CALDAV_ACCOUNTS
        from src.user_preferences import load_for_user

        prefs = load_for_user(owner)
    except Exception:
        return 1
    accounts = prefs.get("caldav_accounts") if isinstance(prefs, dict) else None
    return min(max(1, len(accounts)), MAX_CALDAV_ACCOUNTS) if isinstance(accounts, list) else 1


def owner_timeout_seconds(environ=None, accounts: int = 1) -> float:
    """How long a pass waits for one owner's pull before moving on.

    The pull walks the owner's accounts one after another and each gets a time
    budget of its own, so the wait is, per account, that budget and one more
    request (the read blocked when the budget ran out), plus a little slack once.
    Sized for one account it would cut off an owner whose second account is merely
    slow, and the accounts after it would never get a turn. ``accounts`` counts
    only up to ``MAX_CALDAV_ACCOUNTS``, the most a pull walks, so what the owner
    configures cannot stretch the wait past a fixed bound.
    """
    from src.caldav_sync import MAX_CALDAV_ACCOUNTS, request_timeout_seconds, sync_timeout_seconds

    per_account = sync_timeout_seconds(environ) + request_timeout_seconds(environ)
    counted = min(max(1, int(accounts)), MAX_CALDAV_ACCOUNTS)
    return counted * per_account + OWNER_TIMEOUT_GRACE_SECONDS


class _NotPulled(Exception):
    """An owner the pass gave up on or did not start; already logged."""


def _retire(pending: MutableMapping, owner: str, task: asyncio.Task) -> None:
    if pending.get(owner) is task:
        del pending[owner]
    if not task.cancelled():
        task.exception()  # mark it retrieved; whatever it raised was reported when awaited


async def _await_ended(task: asyncio.Task, timeout: float) -> None:
    """Wait up to ``timeout`` for a cancelled pull to end, ignoring further cancels."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not task.done():
        left = deadline - loop.time()
        if left <= 0:
            logger.error("A cancelled CalDAV pull is still running after %gs; not waiting longer", timeout)
            return
        try:
            await asyncio.wait({task}, timeout=left)
        except asyncio.CancelledError:
            continue


async def _pull_owner(owner: str, sync, timeout: float, pending: MutableMapping) -> dict:
    previous = pending.get(owner)
    if previous is not None and not previous.done():
        logger.warning("Periodic CalDAV sync for %s skipped: its previous pull is still running", owner)
        raise _NotPulled(owner)
    task = asyncio.ensure_future(sync(owner))
    pending[owner] = task
    task.add_done_callback(functools.partial(_retire, pending, owner))
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        # The pull suppresses cancellation until its worker thread has finished,
        # so wait for it (bounded) before letting shutdown go on; a further
        # cancellation of this wait is still honoured once the pull has ended.
        await _await_ended(task, timeout)
        raise
    if not done:
        # Cancelling asks the pull to stop; the worker thread behind it cannot be
        # interrupted, so the task may stay pending a while (see `pending`).
        task.cancel()
        logger.error(
            "Periodic CalDAV sync for %s timed out after %gs; moving on to the next owner", owner, timeout,
        )
        raise _NotPulled(owner)
    return task.result()


async def run_sync_pass(
    owners: Optional[Iterable[str]] = None,
    sync: Optional[Callable[[str], Awaitable[dict]]] = None,
    *,
    timeout: Optional[float] = None,
    pending: Optional[MutableMapping] = None,
) -> dict:
    """Pull every owner once. Returns ``{owner: result-or-None}``; ``None`` marks
    an owner whose sync raised, timed out or was skipped because its previous
    pull (abandoned after a timeout) is still running. ``pending`` carries those
    unfinished pulls from pass to pass. ``timeout`` is how long to wait for each
    owner, in seconds; by default it is sized to the owner's number of accounts
    (see ``owner_timeout_seconds``). Never raises (except cancellation)."""
    if pending is None:
        pending = {}
    if sync is None:
        from src.caldav_sync import sync_caldav as sync
    if owners is None:
        try:
            owners = caldav_owners()
        except Exception:
            logger.exception("CalDAV sync pass: could not list owners")
            return {}

    outcome: dict = {}
    for owner in owners:
        try:
            wait = timeout if timeout is not None else owner_timeout_seconds(accounts=owner_account_count(owner))
            result = await _pull_owner(owner, sync, wait, pending)
        except asyncio.CancelledError:
            raise
        except _NotPulled:
            outcome[owner] = None
            continue
        except Exception:
            logger.exception("Periodic CalDAV sync failed for owner %s", owner)
            outcome[owner] = None
            continue
        outcome[owner] = result
        errors = (result or {}).get("errors") or []
        if errors:
            logger.warning(
                "Periodic CalDAV sync for %s finished with %d error(s); first: %s",
                owner, len(errors), str(errors[0])[:200],
            )
        else:
            logger.info(
                "Periodic CalDAV sync for %s: %s calendar(s), %s event(s), %s removed",
                owner, result.get("calendars", 0), result.get("events", 0), result.get("deleted", 0),
            )
    return outcome


def jittered(interval: float, rng=random) -> float:
    return interval * (1.0 + rng.uniform(-JITTER_FRACTION, JITTER_FRACTION))


async def caldav_sync_loop(
    interval: float,
    *,
    sync: Optional[Callable[[str], Awaitable[dict]]] = None,
    list_owners: Callable[[], Iterable[str]] = caldav_owners,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rng=random,
    owner_timeout: Optional[float] = None,
) -> None:
    """Run passes forever, ``interval`` seconds (jittered) apart."""
    pending: dict = {}
    await sleep(rng.uniform(*STARTUP_DELAY_SECONDS))
    while True:
        try:
            owners = list(list_owners())
        except Exception:
            logger.exception("CalDAV sync loop: could not list owners")
            owners = []
        if owners:
            await run_sync_pass(owners, sync, timeout=owner_timeout, pending=pending)
        await sleep(jittered(interval, rng))


def start_caldav_sync_loop() -> Optional[asyncio.Task]:
    """Start the loop on the running event loop; None when disabled."""
    interval = sync_interval_seconds()
    if interval <= 0:
        logger.info("Periodic CalDAV sync disabled (%s=0)", ENV_INTERVAL)
        return None
    logger.info("Periodic CalDAV sync every ~%ss", interval)
    return asyncio.create_task(caldav_sync_loop(interval), name="caldav-sync-loop")
