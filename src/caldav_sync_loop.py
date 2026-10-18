"""Periodic CalDAV pull.

``src/caldav_sync.py`` documents a scheduler loop, but until now the pull only
ran when somebody opened the calendar or hit ``POST /api/calendar/sync``, so a
read model with nobody looking at it went stale (days, in one measured case).
This module is that loop: every ``CALDAV_SYNC_INTERVAL_SECONDS`` (default 600,
``0`` disables) it pulls each owner that has a CalDAV account configured.

* Owners are processed one after another and a pass finishes before the next
  sleep starts, so the loop never overlaps itself. ``sync_caldav`` also
  serializes per owner, so a pass cannot race a manual sync either.
* A failure (bad credentials, server down, a bug) is logged and the loop carries
  on; one owner's failure does not stop the others.
* The wait between passes is jittered by +/-10% so a restart does not line every
  instance up on the CalDAV server, and the first pass waits a short random time
  so application startup is not slowed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Awaitable, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

ENV_INTERVAL = "CALDAV_SYNC_INTERVAL_SECONDS"
DEFAULT_INTERVAL_SECONDS = 600
# Floor for the configured interval: below this a mistyped value would hammer
# the CalDAV server (Google rate-limits aggressively).
MIN_INTERVAL_SECONDS = 60
JITTER_FRACTION = 0.1
STARTUP_DELAY_SECONDS = (15.0, 45.0)


def sync_interval_seconds(environ=None) -> int:
    """Seconds between passes from the environment; 0 means disabled."""
    env = os.environ if environ is None else environ
    raw = str(env.get(ENV_INTERVAL, "")).strip()
    if not raw:
        return DEFAULT_INTERVAL_SECONDS
    try:
        value = int(float(raw))
    except ValueError:
        logger.warning("%s=%r is not a number; using %ss", ENV_INTERVAL, raw, DEFAULT_INTERVAL_SECONDS)
        return DEFAULT_INTERVAL_SECONDS
    if value <= 0:
        return 0
    if value < MIN_INTERVAL_SECONDS:
        logger.warning("%s=%s is below the %ss floor; using the floor", ENV_INTERVAL, value, MIN_INTERVAL_SECONDS)
        return MIN_INTERVAL_SECONDS
    return value


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


async def run_sync_pass(
    owners: Optional[Iterable[str]] = None,
    sync: Optional[Callable[[str], Awaitable[dict]]] = None,
) -> dict:
    """Pull every owner once. Returns ``{owner: result-or-None}``; ``None`` marks
    an owner whose sync raised. Never raises (except cancellation)."""
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
            result = await sync(owner)
        except asyncio.CancelledError:
            raise
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
) -> None:
    """Run passes forever, ``interval`` seconds (jittered) apart."""
    await sleep(rng.uniform(*STARTUP_DELAY_SECONDS))
    while True:
        try:
            owners = list(list_owners())
        except Exception:
            logger.exception("CalDAV sync loop: could not list owners")
            owners = []
        if owners:
            await run_sync_pass(owners, sync)
        await sleep(jittered(interval, rng))


def start_caldav_sync_loop() -> Optional[asyncio.Task]:
    """Start the loop on the running event loop; None when disabled."""
    interval = sync_interval_seconds()
    if interval <= 0:
        logger.info("Periodic CalDAV sync disabled (%s=0)", ENV_INTERVAL)
        return None
    logger.info("Periodic CalDAV sync every ~%ss", interval)
    return asyncio.create_task(caldav_sync_loop(interval), name="caldav-sync-loop")
