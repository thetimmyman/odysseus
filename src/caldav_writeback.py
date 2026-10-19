"""CalDAV write-back: push local create/update/delete out to the remote (#800).

``src/caldav_sync.py`` is a one-way pull (remote → local). So events created,
edited, or deleted in Odysseus on a CalDAV-backed calendar only changed the local
SQLite copy and never reached the server (iCloud/Nextcloud/Radicale/Fastmail) —
they'd silently disappear on the next pull and never show on the user's phone.

This adds the missing write half. The remote calendar URL isn't stored locally
(the local calendar id is a one-way hash of it), so we re-discover the remote
calendar by matching that same hash, then PUT/DELETE the VEVENT by its UID via
the `caldav` lib. Writes are best-effort: the local DB stays the source of truth,
and a remote failure is reported, never fatal to the local operation.

A write-back is awaited inside the event routes, on a worker thread of the loop's
default executor that every other ``asyncio.to_thread`` caller shares, and one
write-back makes several HTTP requests (principal, calendars, lookup, save). So it
is bounded like a pull is, and the bound covers the whole write-back, not each
request:

* One deadline, ``CALDAV_WRITEBACK_TIMEOUT_SECONDS`` (default 30), starts when the
  write-back is asked for. Waiting for a turn, discovery, the lookup and the save
  all draw on it, each request is capped by what is left of it (see
  ``caldav_sync._cap_request_time``), and the route gets its answer by then. A
  server that trickles bytes can no longer stretch a write-back to a budget per
  request.
* An owner has one write-back running at a time, and at most
  ``MAX_CONCURRENT_WRITEBACKS`` run across all owners. Both limits are held until
  the worker thread has really ended, not only until the caller stopped waiting
  (a blocked thread cannot be interrupted, and a route that is cancelled or cut
  off by the request timeout leaves its thread running). A write-back that cannot
  get a turn within its deadline is not run and says so; the shared executor
  therefore never has more than ``MAX_CONCURRENT_WRITEBACKS`` of its threads
  taken by write-backs, whoever saved a CalDAV account and whatever it points at.

The pure pieces (``build_event_ical``, ``find_remote_calendar``, ``push_event``)
take their inputs by argument so they unit-test against a fake client with no
network.
"""

import asyncio
import logging
import weakref
from datetime import timezone

logger = logging.getLogger(__name__)

# One write-back as a whole: waiting for a turn, discovery, lookup and save. Every
# value that is not a positive number falls back to the default, as for the pull's
# timeouts. Keep it under the HTTP request timeout (``REQUEST_HARD_TIMEOUT``, 45 s
# by default) so the route answers with the result instead of being cut off.
ENV_WRITEBACK_TIMEOUT = "CALDAV_WRITEBACK_TIMEOUT_SECONDS"
DEFAULT_WRITEBACK_TIMEOUT_SECONDS = 30

# Most write-backs running at once across all owners, each on a thread of the
# shared default executor. An owner also has only one at a time (see `_Gate`).
MAX_CONCURRENT_WRITEBACKS = 4


def writeback_timeout_seconds(environ=None) -> float:
    """Wall-clock budget of one write-back, from the moment it is asked for."""
    from src.caldav_sync import _positive_seconds

    return _positive_seconds(environ, ENV_WRITEBACK_TIMEOUT, DEFAULT_WRITEBACK_TIMEOUT_SECONDS)


def _stable_cal_id(remote_url: str, owner: str = "", account_id: str = "") -> str:
    # Reuse the sync module's hashing so owner+account_id scoping stays consistent.
    from src.caldav_sync import _stable_cal_id as _sync_id
    return _sync_id(remote_url, owner=owner, account_id=account_id)


def build_event_ical(ev: dict) -> str:
    """Serialize a local event dict to a VCALENDAR/VEVENT iCalendar string.

    ``ev`` keys: uid, summary, description, location, dtstart (datetime),
    dtend (datetime), all_day (bool), is_utc (bool), rrule (str).
    Mirrors how the pull path interprets is_utc/all_day so a round-trip is stable.
    """
    from icalendar import Calendar, Event as iEvent
    from icalendar.prop import vRecur

    cal = Calendar()
    cal.add("prodid", "-//Odysseus//CalDAV write-back//EN")
    cal.add("version", "2.0")

    ve = iEvent()
    ve.add("uid", ev["uid"])
    ve.add("summary", ev.get("summary") or "")
    if ev.get("description"):
        ve.add("description", ev["description"])
    if ev.get("location"):
        ve.add("location", ev["location"])

    dtstart = ev["dtstart"]
    dtend = ev["dtend"]
    if ev.get("all_day"):
        ve.add("dtstart", dtstart.date())
        ve.add("dtend", dtend.date())
    elif ev.get("is_utc"):
        # Stored as naive-UTC instants — re-attach UTC so the server gets a Z time.
        ve.add("dtstart", dtstart.replace(tzinfo=timezone.utc))
        ve.add("dtend", dtend.replace(tzinfo=timezone.utc))
    else:
        # Legacy naive-local ("floating") time — emit without a TZ.
        ve.add("dtstart", dtstart)
        ve.add("dtend", dtend)

    if ev.get("rrule"):
        try:
            ve.add("rrule", vRecur.from_ical(ev["rrule"]))
        except Exception:
            logger.debug("CalDAV write-back: skipping unparseable rrule %r", ev.get("rrule"))

    cal.add_component(ve)
    return cal.to_ical().decode("utf-8")


def find_remote_calendar(calendars, local_cal_id: str, owner: str = "", account_id: str = ""):
    """Find the remote calendar whose URL hashes to ``local_cal_id``, or None.

    ``owner`` and ``account_id`` must match what was used when the local calendar
    id was originally computed in ``_sync_blocking`` so the hash round-trips."""
    for cal in calendars:
        try:
            if _stable_cal_id(str(cal.url), owner=owner, account_id=account_id) == local_cal_id:
                return cal
        except Exception:
            continue
    return None


def push_event(calendars, local_cal_id: str, ev: dict, *, delete: bool = False,
               owner: str = "", account_id: str = "") -> dict:
    """Create/update (or delete) ``ev`` on the matching remote calendar.

    Returns ``{"ok": bool, ...}``. ``calendars`` is the discovered caldav
    calendar list (injected so this is unit-testable with fakes).
    ``owner`` and ``account_id`` are forwarded to ``find_remote_calendar``
    so the URL hash round-trips correctly (#2765).
    """
    uid = (ev or {}).get("uid") if isinstance(ev, dict) else None
    if not uid:
        return {"ok": False, "error": "event uid is required"}

    remote = find_remote_calendar(calendars, local_cal_id, owner=owner, account_id=account_id)
    if remote is None:
        return {"ok": False, "error": "remote calendar not found"}

    try:
        existing = remote.event_by_uid(uid)
    except TimeoutError:
        # The write-back's time is spent (see `_writeback_blocking`). That is not
        # "no such event": a delete would report the event already gone, and a save
        # would go on to create a second copy of it.
        raise
    except Exception:
        existing = None

    if delete:
        if existing is None:
            return {"ok": True, "note": "already absent on remote"}
        existing.delete()
        return {"ok": True}

    ical = build_event_ical(ev)
    if existing is not None:
        existing.data = ical
        existing.save()
        return {"ok": True, "updated": True}
    remote.save_event(ical)
    return {"ok": True, "created": True}


def _discover_calendars(client):
    """Discover the principal's calendars, falling back to the URL itself —
    same strategy as the pull path."""
    from caldav.lib.error import AuthorizationError, NotFoundError
    try:
        return client.principal().calendars()
    except (AuthorizationError, NotFoundError, TimeoutError):
        # TimeoutError: the write-back's time is spent, so nothing is left to try.
        raise
    except Exception:
        try:
            return [client.calendar(url=str(client.url))]
        except Exception:
            return []


def _writeback_blocking(local_cal_id, ev, delete, url, username, password,
                        owner="", account_id="", deadline=None) -> dict:
    """The write-back itself, synchronous, for a worker thread.

    ``deadline`` is the `caldav_sync._clock` time at which the whole write-back
    ends (`writeback_event` sets it when the write-back is asked for); without one
    the write-back gets a full ``writeback_timeout_seconds()`` from now. Every
    request of the client is capped by it, so the lot (discovery, lookup, save)
    cannot take longer, and a server that trickles bytes cannot hold this thread
    for one budget per request. Raises TimeoutError once it is spent.
    """
    from src import caldav_sync
    from src.caldav_sync import _build_dav_client

    if deadline is None:
        deadline = caldav_sync._clock() + writeback_timeout_seconds()
    # Redirects disabled here too: the write-back path opens its own DAVClient,
    # so it needs the same SSRF-via-redirect protection as the pull path.
    client = _build_dav_client(url, username, password, deadline=deadline)
    calendars = _discover_calendars(client)
    if not calendars:
        return {"ok": False, "error": "no remote calendars discovered"}
    return push_event(calendars, local_cal_id, ev, delete=delete,
                      owner=owner, account_id=account_id)


class _Permit:
    """Turns of the write-back gate that are given back together, once."""

    def __init__(self):
        self._held: list = []

    def take(self, primitive) -> None:
        self._held.append(primitive)

    def release(self) -> None:
        held, self._held = self._held, []
        for primitive in reversed(held):
            primitive.release()


class _Gate:
    """Who may run a write-back now, for one event loop: one per owner, and no more
    than ``MAX_CONCURRENT_WRITEBACKS`` in all. Primitives belong to a loop, hence
    one gate per loop (a process has one; every ``asyncio.run`` of a test has its own).
    """

    def __init__(self):
        self.slots = asyncio.Semaphore(MAX_CONCURRENT_WRITEBACKS)
        self.owners: dict = {}

    def owner_lock(self, owner: str) -> asyncio.Lock:
        lock = self.owners.get(owner)
        if lock is None:
            lock = self.owners[owner] = asyncio.Lock()
        return lock


_GATES: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _gate() -> _Gate:
    loop = asyncio.get_running_loop()
    gate = _GATES.get(loop)
    if gate is None:
        gate = _GATES[loop] = _Gate()
    return gate


async def _take_within(primitive, seconds: float) -> bool:
    """Acquire a lock or semaphore, giving up after ``seconds``. A cancelled or
    timed-out wait holds nothing."""
    if seconds <= 0:
        return False
    try:
        async with asyncio.timeout(seconds):
            await primitive.acquire()
    except TimeoutError:
        return False
    return True


def _run_writeback(deadline, *args) -> dict:
    """What the worker thread runs: the write-back, with every failure as a result."""
    from src import caldav_sync

    try:
        if caldav_sync._clock() >= deadline:
            # It waited for a thread of the pool (or for the loop) until its time
            # was up, which is the moment to stop, not to start.
            return {"ok": False, "error": "CalDAV write-back timed out before it started"}
        return _writeback_blocking(*args, deadline=deadline)
    except TimeoutError as e:
        logger.warning("CalDAV write-back ran out of time: %s", e)
        return {"ok": False, "error": "CalDAV write-back timed out"}
    except Exception as e:
        logger.exception("CalDAV write-back raised")
        return {"ok": False, "error": str(e)[:200]}


async def _run_holding_overdue(deadline: float, overdue: list, *args) -> dict:
    """Run the write-back on a worker thread and, if one of its requests was left
    running past its time, keep the task (and so the owner's lock and the pool
    slot, which are given back when it ends) until that request has ended."""
    from src import caldav_sync

    try:
        return await asyncio.to_thread(_run_writeback, deadline, *args)
    finally:
        await caldav_sync.wait_overdue_ended(overdue)


def _retrieve(task: "asyncio.Future") -> None:
    # Nobody may be awaiting the task any more (the caller gave up on it); take its
    # outcome so the loop does not complain about a result nobody read.
    if not task.cancelled():
        task.exception()


async def _run_bounded(owner: str, deadline: float, *args) -> dict:
    """Run ``_writeback_blocking(*args)`` on a worker thread, when the owner and the
    pool allow it, so that the caller has its answer by ``deadline`` and no more
    than the gate allows are ever running.

    The owner's lock and the pool slot are given back when the worker task ends, not
    when this coroutine does: a caller that is cancelled, or stops waiting at the
    deadline, leaves a thread that is still running, and that thread must go on
    counting against the limits.
    """
    from src import caldav_sync

    clock = caldav_sync._clock
    gate = _gate()
    permit = _Permit()
    handed_over = False
    try:
        lock = gate.owner_lock(owner)
        if not await _take_within(lock, deadline - clock()):
            return {"ok": False, "error": "an earlier CalDAV write-back for this user is still running"}
        permit.take(lock)
        if not await _take_within(gate.slots, deadline - clock()):
            return {"ok": False, "error": "too many CalDAV write-backs are running; try again shortly"}
        permit.take(gate.slots)

        overdue: list = []
        token = caldav_sync._OVERDUE_SINK.set(overdue)
        try:
            task = asyncio.ensure_future(_run_holding_overdue(deadline, overdue, *args))
        finally:
            caldav_sync._OVERDUE_SINK.reset(token)
        handed_over = True
        task.add_done_callback(lambda _task: permit.release())
        task.add_done_callback(_retrieve)
    finally:
        if not handed_over:
            permit.release()

    try:
        # Shielded: the wait may end (deadline, or the route is cancelled) while the
        # thread goes on, and cancelling a task that has not started would drop its
        # work item and with it the only thing that gives the turns back.
        async with asyncio.timeout(max(deadline - clock(), 0)):
            return await asyncio.shield(task)
    except TimeoutError:
        logger.warning("CalDAV write-back did not finish within its time budget")
        return {"ok": False, "error": "CalDAV write-back timed out"}


async def writeback_event(owner: str, calendar_source: str, calendar_id: str,
                          ev: dict, *, delete: bool = False) -> dict:
    """Best-effort push of a local change to the remote CalDAV server.

    No-ops (``{"skipped": ...}``) when the calendar isn't CalDAV-backed or no
    credentials are configured. Never raises — a remote failure is logged and
    returned, the local DB remaining the source of truth. Returns within
    ``writeback_timeout_seconds()`` of being called, however slow the server is
    (see the module docstring for what is bounded and how).
    """
    if calendar_source != "caldav":
        return {"skipped": "not a caldav calendar"}
    try:
        from src import caldav_sync
        from src.caldav_sync import _load_caldav_accounts
        from src.secret_storage import decrypt
        from core.database import CalendarCal, SessionLocal

        deadline = caldav_sync._clock() + writeback_timeout_seconds()
        accounts = _load_caldav_accounts(owner)
        if not accounts:
            return {"skipped": "caldav not configured"}

        # Find which account owns this calendar.
        acc = None
        if len(accounts) > 1:
            db = SessionLocal()
            try:
                cal_row = db.query(CalendarCal).filter(CalendarCal.id == calendar_id).first()
                cal_account_id = cal_row.account_id if cal_row else None
            finally:
                db.close()
            if cal_account_id:
                acc = next((a for a in accounts if a.get("id") == cal_account_id), None)
        # Fall back to first account (covers single-account and legacy rows with
        # no account_id stamped).
        if acc is None:
            acc = accounts[0]

        url = (acc.get("url") or "").strip()
        user = (acc.get("username") or "").strip()
        pw = decrypt(acc.get("password") or "")
        if not (url and user and pw):
            return {"skipped": "caldav account credentials incomplete"}
        from src.caldav_sync import validate_caldav_url
        try:
            url = validate_caldav_url(url)
        except ValueError as e:
            logger.warning("CalDAV write-back URL rejected: %s", e)
            return {"ok": False, "error": str(e)[:200]}
        acc_id = acc.get("id") or ""
        result = await _run_bounded(
            owner, deadline, calendar_id, ev, delete, url, user, pw, owner, acc_id,
        )
        if not result.get("ok"):
            logger.warning("CalDAV write-back did not apply: %s", result.get("error") or result)
        return result
    except Exception as e:
        logger.exception("CalDAV write-back raised")
        return {"ok": False, "error": str(e)[:200]}
