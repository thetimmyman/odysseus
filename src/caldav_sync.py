"""CalDAV → local SQLite sync.

The Settings UI lets users save CalDAV credentials, but the original
sync path was removed when calendar storage was migrated to SQLite.
This module re-wires that gap as a one-way pull (remote → local),
called on calendar open and from a periodic scheduler loop.

Design notes:
- We use the `caldav` lib so PROPFIND discovery + REPORT XML work
  across Radicale / Nextcloud / Apple / Fastmail without us
  reinventing the protocol. It's pure Python.
- The lib is synchronous; we run it in a threadpool via
  `asyncio.to_thread` so the FastAPI event loop stays free.
- Each remote calendar maps to one local `CalendarCal` row with
  `source="caldav"` and `id` = a stable hash of the remote URL so
  re-syncs idempotently target the same row.
- Events upsert by VEVENT UID (kept as the local `uid`). Local
  CalDAV-sourced events not seen in the latest pull are deleted so
  remote deletions propagate.
- A recurring event is ONE resource holding a master VEVENT (RRULE, EXDATE)
  plus RECURRENCE-ID override VEVENTs under the same UID. The master keeps the
  UID row; each override gets its own row (see `src/recurrence.py`) so the
  series expansion in `routes/calendar_routes.py` can drop excluded and
  replaced occurrences instead of the last VEVENT overwriting the master.
- Datetimes are converted to UTC and the row is flagged `is_utc=True`
  so the serializer adds the Z suffix and the frontend renders in the
  user's local TZ correctly.
"""

import asyncio
import contextvars
import hashlib
import ipaddress
import logging
import math
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse, urlunparse

from src.recurrence import encode_keys, override_uid, recurrence_key, tzid_for

logger = logging.getLogger(__name__)

# Pull window: 90 days back, 1 year forward. Keeps the REPORT cheap and
# matches what the calendar UI typically renders. Far-future recurring
# events still come through via RRULE expansion on the frontend.
_LOOKBACK_DAYS = 90
_LOOKAHEAD_DAYS = 365

# python-caldav's DAVClient has no network timeout unless it is given one, so a
# server that accepts a connection and then says nothing would hold a sync (and
# the owner's lock, and a worker thread) for good. Every client is built with
# CALDAV_REQUEST_TIMEOUT_SECONDS, which the HTTP library applies to the connect
# and to each socket read. A read that keeps being answered, a byte at a time,
# never times out that way, so a request is also capped as a whole: it may use no
# more than what is left of the pull's budget, CALDAV_SYNC_TIMEOUT_SECONDS (see
# `_cap_request_time`), and the pull stops starting new work once that has passed.
# A slow server therefore holds a pull for at most the budget plus the time one
# blocked read needs to give up. Both must be positive numbers: anything else
# (zero, negative, infinite, not a number) means "no limit" and falls back to the
# default instead.
ENV_REQUEST_TIMEOUT = "CALDAV_REQUEST_TIMEOUT_SECONDS"
ENV_SYNC_TIMEOUT = "CALDAV_SYNC_TIMEOUT_SECONDS"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 30
DEFAULT_SYNC_TIMEOUT_SECONDS = 300

# Most CalDAV accounts one owner may keep, and so the most a pull walks. An
# owner's accounts are pulled one after another, each with a budget of its own,
# so the time a pull can hold the periodic loop (see `src/caldav_sync_loop.py`) is
# this many budgets at most. Without a limit that time grew with a number the
# owner types in, and one user adding accounts that pointed at slow servers could
# keep every later owner from being pulled for hours. Saving a further account is
# refused (`POST /api/calendar/config/accounts`); accounts saved beyond it before
# the limit existed are not pulled and the pull says so.
MAX_CALDAV_ACCOUNTS = 10

# Monotonic clock for the pull's time budget; a name of its own so tests can move it.
_clock = time.monotonic

# Where the previous pull of an account stopped. A pull that spends its budget
# leaves the calendars it did not reach for the next one, and walking the server's
# list from the top again would spend the budget on the same leading calendars
# every time, so the tail would never be pulled (nor pruned). Instead the next
# pull starts at the first calendar the last one never began, and wraps round, so
# each calendar gets its turn even when one is too slow to finish. Keyed by owner
# and account, valued by calendar URL (the order on the server may change); cleared
# when a pull reaches the end of its calendars. In memory only: a restart starts
# over at the top of the list. Each key is only written by that account's pull,
# which runs one at a time per owner (see `_owner_lock`).
_RESUME_AT: dict = {}


def _resume_key(owner: str, account_id: str, url: str) -> tuple:
    return (owner, account_id or url)


def _from_resume_point(calendars: list, resume_url) -> list:
    """``calendars`` rotated to begin at the one whose URL is ``resume_url``; as
    given when there is no such calendar (nothing to resume, or it is gone)."""
    calendars = list(calendars)
    if resume_url:
        for index, remote_cal in enumerate(calendars):
            if str(remote_cal.url) == resume_url:
                return calendars[index:] + calendars[:index]
    return calendars


def _positive_seconds(environ, name: str, default: float) -> float:
    env = os.environ if environ is None else environ
    raw = str(env.get(name, "")).strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        logger.warning("%s=%r is not a positive number of seconds; using %ss", name, raw, default)
        return float(default)
    return value


def request_timeout_seconds(environ=None) -> float:
    """Network timeout of one CalDAV request: the connect and each read, not the
    request as a whole (see `_cap_request_time` for that)."""
    return _positive_seconds(environ, ENV_REQUEST_TIMEOUT, DEFAULT_REQUEST_TIMEOUT_SECONDS)


def sync_timeout_seconds(environ=None) -> float:
    """Wall-clock budget of one account's pull. No single request outlasts it."""
    return _positive_seconds(environ, ENV_SYNC_TIMEOUT, DEFAULT_SYNC_TIMEOUT_SECONDS)


_BLOCKED_HOSTS = {
    "localhost",
    "localhost.",
    "ip6-localhost",
    "metadata.google.internal",
}


def _private_caldav_allowed() -> bool:
    return os.environ.get("ODYSSEUS_ALLOW_PRIVATE_CALDAV", "0").lower() in {"1", "true", "yes"}


def _validate_caldav_address(addr: ipaddress._BaseAddress) -> None:
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_unspecified
        or addr.is_reserved
    ):
        raise ValueError("CalDAV URL host is not allowed")
    if addr.is_private and not _private_caldav_allowed():
        raise ValueError("Private CalDAV IPs require ODYSSEUS_ALLOW_PRIVATE_CALDAV=1")


def _validate_caldav_ip(host: str) -> None:
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return
    _validate_caldav_address(ip)


def _resolve_caldav_host_ips(host: str) -> list[ipaddress._BaseAddress]:
    addrs: list[ipaddress._BaseAddress] = []
    for family, _, _, _, sockaddr in socket.getaddrinfo(host, None):
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        try:
            addrs.append(ipaddress.ip_address(sockaddr[0].split("%", 1)[0]))
        except ValueError:
            continue
    return addrs


def _validate_caldav_hostname(host: str) -> None:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return
    except ValueError:
        pass
    try:
        addrs = _resolve_caldav_host_ips(host)
    except OSError:
        raise ValueError("CalDAV URL host does not resolve")
    if not addrs:
        raise ValueError("CalDAV URL host does not resolve")
    for addr in addrs:
        _validate_caldav_address(addr)


def validate_caldav_url(raw_url: str) -> str:
    """Validate and normalize a user-provided CalDAV URL before server-side use."""
    url = (raw_url if isinstance(raw_url, str) else "").strip()
    if not url:
        raise ValueError("CalDAV URL is required")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("CalDAV URL must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("CalDAV URL must include a host")
    if parsed.username or parsed.password:
        raise ValueError("Put CalDAV credentials in the username/password fields, not the URL")
    if parsed.fragment:
        raise ValueError("CalDAV URL fragments are not allowed")
    try:
        parsed.port
    except ValueError:
        raise ValueError("CalDAV URL has an invalid port")
    host = (parsed.hostname or "").lower()
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        raise ValueError("CalDAV URL host is not allowed")
    _validate_caldav_ip(host)
    _validate_caldav_hostname(host)
    return urlunparse(parsed._replace(fragment="")).rstrip("/")


def _stable_cal_id(remote_url: str, owner: str = "", account_id: str = "") -> str:
    """Deterministic local id for a remote CalDAV calendar, scoped to owner
    and account so two users — or one user with two accounts — pointing at
    the same server URL get distinct local rows (avoids PK collision, #2765).
    The owner and account_id default to "" for the legacy/URL-only path so
    existing callers without those arguments keep working."""
    key = f"{owner}\n{account_id}\n{remote_url}"
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"caldav-{h}"


def _to_utc_naive(dt):
    """CalDAV datetimes can be tz-aware (with a TZID) or naive. The DB
    column is naive but we set is_utc=True so the serializer adds Z.
    All-day events stay as date and get widened to datetime here."""
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None), False
        return dt, False  # naive → treat as local
    # date-only (all-day)
    return datetime(dt.year, dt.month, dt.day), True


def _find_existing_event(db, pending, uid_val, calendar_id):
    """Find the event to update for THIS calendar.

    CalendarEvent.uid is the global primary key, so an unscoped lookup by uid
    returns whatever row holds that VEVENT uid — including another owner's.
    The old code then reassigned that row's calendar_id, moving (stealing)
    another user's event into the syncing calendar whenever the two share a
    uid (shared/subscribed/public calendars, or two accounts on one server).
    Scope the lookup to the calendar being synced; a genuine cross-user uid
    collision then fails the PK insert inside the per-calendar try/except
    instead of hijacking the row. (import_ics was already fixed this way.)
    """
    from core.database import CalendarEvent
    return pending.get(uid_val) or db.query(CalendarEvent).filter(
        CalendarEvent.uid == uid_val,
        CalendarEvent.calendar_id == calendar_id,
    ).first()


def _google_caldav_events_url(url: str) -> str | None:
    """Map a Google CalDAV *principal* URL to its event-collection URL.

    Google serves the principal at ``…/user`` but events live under ``…/events``
    — the ``/user`` resource holds no VEVENTs. The `caldav` library's
    principal→home-set discovery does not reliably enumerate calendars from
    Google's ``/user`` endpoint, so the sync falls into the "treat the URL as a
    single calendar" fallback below. Pointed at ``/user`` that fallback issues
    every calendar-query REPORT against the principal, which returns a clean but
    empty 200 for all date ranges — the calendar shows no events even though
    auth succeeded (issue #2507).

    Both Google CalDAV endpoint forms are handled, since some accounts only
    authenticate against one of them:
      - newer:  ``https://apidata.googleusercontent.com/caldav/v2/<id>/user``
      - legacy: ``https://www.google.com/calendar/dav/<id>/user``

    Returns the events URL for a recognised Google principal URL, else None so
    the caller keeps the original URL unchanged.
    """
    parts = urlparse(url)
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    if not path.endswith("/user"):
        return None
    is_google = (
        host.endswith("googleusercontent.com")                       # newer /caldav/v2 form
        or (host in ("www.google.com", "google.com") and "/calendar/dav/" in path)  # legacy form
    )
    if not is_google:
        return None
    new_path = path[: -len("/user")] + "/events"
    return urlunparse(parts._replace(path=new_path))


def _open_url_as_calendar(client, url: str):
    """Open ``url`` as a single calendar collection.

    Used when principal discovery yields no calendars. Google's principal URL
    is not an event collection, so map it to the events URL first
    (see ``_google_caldav_events_url``); other servers' URLs are used as-is.
    """
    target = _google_caldav_events_url(url) or url
    return client.calendar(url=target)


class _RequestOutOfTime(BaseException):
    """Raised inside a request thread whose time is up, to end it.

    A BaseException so that the ``except Exception`` clauses of the HTTP stack
    cannot swallow it half way. It is only ever raised in the thread that
    `_call_within` started for the request and never leaves that thread.
    """


_REQUEST_THREAD_NAME = "caldav-request"

# Request threads whose call ran out of time but have not exited yet (typically
# blocked in a C call such as DNS resolution, which no socket timeout bounds).
# They are kept so that a pull cannot pile new requests on top of a stuck
# resolver without limit: past MAX_OVERDUE_REQUESTS no further request starts
# until some of them have ended.
MAX_OVERDUE_REQUESTS = 4
_overdue_threads: "set[threading.Thread]" = set()
_overdue_lock = threading.Lock()


# Where the request threads left overdue by the current pull or write-back are
# reported, so that the caller can keep the owner's custody until they end. A
# caller sets it to a list around its work; contexts copied for its worker
# threads share that list.
_OVERDUE_SINK: "contextvars.ContextVar[Optional[list]]" = contextvars.ContextVar(
    "caldav_overdue_sink", default=None,
)


def _note_overdue(thread: threading.Thread) -> None:
    with _overdue_lock:
        _overdue_threads.add(thread)
    sink = _OVERDUE_SINK.get()
    if sink is not None:
        sink.append(thread)


async def wait_overdue_ended(threads, poll: float = 0.05) -> None:
    """Return once none of ``threads`` is running any more."""
    while any(t.is_alive() for t in threads):
        await asyncio.sleep(poll)


def _overdue_count() -> int:
    """How many abandoned request threads are still running (ended ones are dropped)."""
    with _overdue_lock:
        for thread in [t for t in _overdue_threads if not t.is_alive()]:
            _overdue_threads.discard(thread)
        return len(_overdue_threads)


def _stop_thread(thread: threading.Thread) -> bool:
    """Make ``thread`` raise `_RequestOutOfTime` the next time it runs Python code.

    A thread blocked in a socket read only gets there when the read returns, which
    the request timeout bounds, or when the next byte arrives. Returns whether the
    request was delivered; on an interpreter that cannot do it the thread simply
    runs on until its own read timeout ends it.
    """
    try:
        import ctypes

        return ctypes.pythonapi.PyThreadState_SetAsyncExc(
            ctypes.c_ulong(thread.ident), ctypes.py_object(_RequestOutOfTime),
        ) == 1
    except Exception:
        logger.warning("could not stop an overdue CalDAV request thread", exc_info=True)
        return False


def _call_within(seconds: float, grace: float, fn, /, *args, **kwargs):
    """Return ``fn(*args, **kwargs)``, or raise TimeoutError once ``seconds`` have passed.

    The call runs in a thread of its own so that it can be ended: HTTP timeouts
    apply to each socket operation, so a server that keeps trickling bytes in
    would otherwise keep the calling (sync) thread, and the owner's lock that
    thread stands for, for as long as it likes. When time is up the call's
    thread is told to stop (see `_stop_thread`) and given up to ``grace`` seconds
    to do so; the caller does not go on using whatever state the call touched.
    A thread that is still running after the grace stays on a bounded list of
    overdue requests (``MAX_OVERDUE_REQUESTS``), and no new request starts while
    that list is full.
    Nothing is started when no time is left. What the call raises is raised here
    unchanged, and it runs in a copy of the caller's context.
    """
    if seconds <= 0:
        raise TimeoutError("the time budget of the CalDAV pull is spent")
    if _overdue_count() >= MAX_OVERDUE_REQUESTS:
        raise TimeoutError("earlier CalDAV requests are still stuck; not starting another")
    box: dict = {}
    settled = threading.Lock()
    finished = threading.Event()
    context = contextvars.copy_context()

    def run():
        try:
            try:
                box["value"] = context.run(fn, *args, **kwargs)
            except BaseException as exc:  # handed to the caller below
                box["error"] = exc
            with settled:
                box["done"] = True
            finished.set()
        except _RequestOutOfTime:
            pass  # the caller gave up on this call and is not waiting for it

    thread = threading.Thread(target=run, name=_REQUEST_THREAD_NAME, daemon=True)
    thread.start()
    if not finished.wait(seconds):
        with settled:  # a call that finishes right now is a result, not an overrun
            overdue = not box.get("done")
            stopped = overdue and _stop_thread(thread)
        if overdue:
            if stopped:
                thread.join(grace)
            if thread.is_alive():
                # Still inside a blocking call: keep it counted so that later
                # pulls cannot pile more requests onto it without bound.
                _note_overdue(thread)
            raise TimeoutError(f"a CalDAV request did not finish within the {seconds:g}s left of its time budget")
    thread.join()
    if "error" in box:
        error = box["error"]
        if isinstance(error, _RequestOutOfTime):
            # The stop request landed just as the call ended: that is a timeout
            # like any other, not an internal BaseException for the callers.
            raise TimeoutError(
                f"a CalDAV request did not finish within the {seconds:g}s left of its time budget"
            ) from None
        raise error
    return box["value"]


def _cap_request_time(client, deadline) -> None:
    """Give every HTTP exchange of ``client`` a time cap of its own.

    The client's network timeout bounds the connect and each socket read, so a
    server that answers a byte at a time (or a link that is merely very slow)
    never trips it, and the request goes on for as long as bytes keep arriving.
    With a ``deadline`` (a `_clock` time) a request may use what is left of it,
    and none is started after it; without one each request gets the whole pull
    budget. Both end in TimeoutError, which every caller already treats like any
    other failed request.

    The cap sits on the session, the one place every request passes through (the
    client's own methods, the calendar objects, the write-back), and is skipped
    for a client without a session, which only a test's stand-in lacks.
    """
    session = getattr(client, "session", None)
    send = getattr(session, "request", None)
    if not callable(send):
        return
    budget = sync_timeout_seconds()
    grace = request_timeout_seconds()

    def capped(*args, **kwargs):
        left = budget if deadline is None else deadline - _clock()
        return _call_within(left, grace, send, *args, **kwargs)

    session.request = capped


def _build_dav_client(url: str, username: str, password: str, deadline=None):
    """Construct a CalDAV client with automatic redirects disabled.

    ``validate_caldav_url`` resolves and vets the *initial* host, but caldav's
    underlying HTTP session follows 3xx redirects by default. So a URL that
    passes validation can still be redirected — at request time — to
    loopback / link-local / private space, re-opening the SSRF the host check
    closes. Pin the session to zero redirects: any 3xx then raises instead of
    silently following an attacker-chosen ``Location``. This mirrors the
    test-connection path in ``routes/calendar_routes.py``, which already sets
    ``follow_redirects=False``.

    DAVClient exposes no per-request redirect flag, so we set it on the session
    after construction (the session is created in ``__init__``).

    The client also gets a finite network timeout (see ``request_timeout_seconds``):
    DAVClient defaults to none, which lets a silent server block a request for good.
    That timeout does not bound a server that keeps trickling data, so each request
    is capped as a whole too (see ``_cap_request_time``): by ``deadline``, the
    `_clock` time at which the caller's budget ends (the pull's, or the write-back's
    own), or by the whole pull budget when the caller has none.
    """
    import caldav

    client = caldav.DAVClient(
        url=url, username=username, password=password, timeout=request_timeout_seconds(),
    )
    # Unconditional: a redirect-disable that only sometimes applies is not a
    # control. The session exists right after __init__ on every real client;
    # test_build_dav_client_disables_redirects asserts it against installed
    # caldav in CI.
    client.session.max_redirects = 0
    _cap_request_time(client, deadline)
    return client


def _exdate_keys(comp, all_day: bool) -> set:
    """Occurrence keys excluded by every EXDATE line of a series master."""
    raw = comp.get("exdate")
    items = raw if isinstance(raw, list) else ([] if raw is None else [raw])
    keys = set()
    for item in items:
        for entry in getattr(item, "dts", None) or []:
            value, _ = _to_utc_naive(entry.dt)
            keys.add(recurrence_key(value, all_day))
    return keys


def _event_row_from_vevent(comp):
    """Turn one VEVENT into ``(row_uid, fields)``; ``fields`` is None when the
    VEVENT has no DTSTART (it is still reported as seen so it is never pruned).

    A master keeps the VEVENT UID. A RECURRENCE-ID override gets its own row uid
    so it cannot overwrite the master (see ``src/recurrence.py``).
    """
    uid_val = str(comp.get("uid", "")) or str(uuid.uuid4())

    rid_p = comp.get("recurrence-id")
    rid_key = None
    if rid_p is not None:
        rid_value, rid_all_day = _to_utc_naive(rid_p.dt)
        rid_key = recurrence_key(rid_value, rid_all_day)
    row_uid = override_uid(uid_val, rid_key) if rid_key else uid_val

    dtstart_p = comp.get("dtstart")
    if not dtstart_p:
        return row_uid, None
    start_dt, all_day = _to_utc_naive(dtstart_p.dt)

    dtend_p = comp.get("dtend")
    if dtend_p:
        end_dt, _ = _to_utc_naive(dtend_p.dt)
    elif all_day:
        end_dt = start_dt + timedelta(days=1)
    else:
        end_dt = start_dt + timedelta(hours=1)

    # is_utc reflects whether the source carried a TZ we converted from.
    # All-day = no TZ semantics.
    row_is_utc = (
        not all_day
        and isinstance(dtstart_p.dt, datetime)
        and dtstart_p.dt.tzinfo is not None
    )

    fields = {
        "summary": str(comp.get("summary", "")),
        "description": str(comp.get("description", "")),
        "location": str(comp.get("location", "")),
        "dtstart": start_dt,
        "dtend": end_dt,
        "all_day": all_day,
        "is_utc": row_is_utc,
        "status": "cancelled" if str(comp.get("status", "")).strip().upper() == "CANCELLED" else "confirmed",
        "rrule": "",
        "exdate": "",
        "tzid": None,
        "series_uid": None,
        "recurrence_id": None,
    }
    if rid_key:
        # An override is a single event; it never carries its own series rule.
        fields["series_uid"] = uid_val
        fields["recurrence_id"] = rid_key
    else:
        fields["rrule"] = comp.get("rrule").to_ical().decode() if comp.get("rrule") else ""
        if fields["rrule"]:
            fields["exdate"] = encode_keys(_exdate_keys(comp, all_day))
            if row_is_utc:
                fields["tzid"] = tzid_for(dtstart_p.dt, getattr(dtstart_p, "params", None))
    return row_uid, fields


def _should_prune_window(seen_uids: set, parse_failed: bool) -> bool:
    """Whether the post-sync prune of vanished CalDAV events is safe to run.

    The prune deletes local ``origin=="caldav"`` rows in the window whose UID the
    server did not just return. Any parse failure (total or partial) makes
    ``seen_uids`` an incomplete view of the server, so pruning against it can
    delete events that still exist upstream but could not be read: a total
    failure wipes the whole window, a partial failure deletes just the
    unreadable ones. Only prune on a clean read. An empty ``seen_uids`` after a
    clean read is a genuinely empty window, which is safe to prune.
    """
    return not parse_failed


def _sync_blocking(owner: str, url: str, username: str, password: str, account_id: str = "") -> dict:
    """The actual sync — synchronous, intended to run in a threadpool.
    Returns counts: {calendars, events, deleted, errors}."""
    # Lazy imports so a missing `caldav` dep doesn't break app startup —
    # the integrations form still works, sync just no-ops with an error.
    from caldav.lib.error import AuthorizationError, NotFoundError
    from core.database import CalendarCal, CalendarEvent, SessionLocal
    from sqlalchemy import and_, or_

    result = {"calendars": 0, "events": 0, "deleted": 0, "errors": []}
    deadline = _clock() + sync_timeout_seconds()

    client = _build_dav_client(url, username, password, deadline=deadline)

    # Discovery: try principal → calendars first; if the server doesn't
    # support discovery (or the URL points directly at a calendar), fall
    # back to treating the URL as a single calendar.
    calendars = []
    try:
        principal = client.principal()
        calendars = principal.calendars()
    except (AuthorizationError, NotFoundError) as e:
        result["errors"].append(f"Discovery failed: {e}")
        return result
    except Exception as e:
        logger.info(f"CalDAV principal discovery failed, trying URL as calendar: {e}")
        try:
            calendars = [_open_url_as_calendar(client, url)]
        except Exception as e2:
            result["errors"].append(f"Could not open URL as calendar: {e2}")
            return result

    if not calendars:
        try:
            calendars = [_open_url_as_calendar(client, url)]
        except Exception as e:
            result["errors"].append(f"No calendars and URL fallback failed: {e}")
            return result

    start = datetime.utcnow() - timedelta(days=_LOOKBACK_DAYS)
    end = datetime.utcnow() + timedelta(days=_LOOKAHEAD_DAYS)

    resume_key = _resume_key(owner, account_id, url)
    calendars = _from_resume_point(calendars, _RESUME_AT.get(resume_key))
    _RESUME_AT.pop(resume_key, None)

    db = SessionLocal()
    try:
        for remote_cal in calendars:
            if _clock() >= deadline:
                # The next pull picks up here, at the first calendar this one
                # never began, rather than starting over at the top.
                _RESUME_AT[resume_key] = str(remote_cal.url)
                result["errors"].append(
                    "time budget exhausted; the remaining calendars were not pulled "
                    "(the next pass starts with them)"
                )
                break
            try:
                remote_url = str(remote_cal.url)
                cal_id = _stable_cal_id(remote_url, owner=owner, account_id=account_id)
                display_name = (remote_cal.name or "").strip() or "CalDAV"

                local_cal = db.query(CalendarCal).filter(
                    CalendarCal.id == cal_id,
                    CalendarCal.owner == owner,
                ).first()
                if not local_cal:
                    local_cal = CalendarCal(
                        id=cal_id,
                        owner=owner,
                        name=display_name,
                        color="#5b8abf",
                        source="caldav",
                        account_id=account_id or None,
                    )
                    db.add(local_cal)
                    db.commit()
                else:
                    # Refresh display name and stamp account_id if missing.
                    changed = False
                    if local_cal.name != display_name:
                        local_cal.name = display_name
                        changed = True
                    if account_id and not local_cal.account_id:
                        local_cal.account_id = account_id
                        changed = True
                    if changed:
                        db.commit()
                result["calendars"] += 1

                # Fetch events in window. `date_search` returns CalendarObject
                # resources; each may contain one VEVENT (most servers) or
                # several (rare).
                from icalendar import Calendar as iCal

                seen_uids = set()
                # Track events added to the session but not yet committed so
                # duplicate UIDs within the same batch are updated, not re-inserted
                # (which would violate the UNIQUE constraint on commit).
                pending: dict = {}
                parse_failed = False
                try:
                    objs = remote_cal.date_search(start=start, end=end, expand=False)
                except Exception as e:
                    result["errors"].append(f"{display_name}: date_search failed ({e})")
                    continue

                for obj in objs:
                    try:
                        ical = iCal.from_ical(obj.data)
                    except Exception as e:
                        result["errors"].append(f"{display_name}: parse failed ({e})")
                        parse_failed = True
                        continue

                    for comp in ical.walk():
                        if comp.name != "VEVENT":
                            continue
                        try:
                            row_uid, fields = _event_row_from_vevent(comp)
                        except Exception as e:
                            # One unreadable VEVENT must not abort the calendar,
                            # and seen_uids is now incomplete, so skip the prune.
                            result["errors"].append(f"{display_name}: VEVENT parse failed ({e})")
                            parse_failed = True
                            continue
                        seen_uids.add(row_uid)
                        if fields is None:
                            continue

                        existing = _find_existing_event(db, pending, row_uid, local_cal.id)
                        if existing:
                            existing.calendar_id = local_cal.id
                            for name, value in fields.items():
                                setattr(existing, name, value)
                            existing.origin = "caldav"
                        else:
                            new_ev = CalendarEvent(
                                uid=row_uid,
                                calendar_id=local_cal.id,
                                origin="caldav",
                                **fields,
                            )
                            db.add(new_ev)
                            pending[row_uid] = new_ev
                        result["events"] += 1
                db.commit()

                # Prune locally-cached CalDAV events that vanished
                # upstream (only within our sync window — events outside
                # the window aren't in `objs`, so we'd false-delete them).
                # Only rows we previously pulled from the server (origin=="caldav")
                # are prunable; locally-created events (agent / email triage / a
                # UI event whose write-back failed) carry origin NULL and must
                # never be deleted just because the server didn't return them.
                # Skip the prune on any parse failure: seen_uids is then an
                # incomplete view of the server, so pruning against it would
                # delete events that still exist upstream but could not be read
                # (the empty-seen_uids case wipes the whole window; a partial
                # failure deletes just the unreadable rows).
                #
                # `seen_uids` holds ROW uids: a series master's UID and the
                # derived uid of each RECURRENCE-ID override, so an override
                # reverted upstream is pruned like any other vanished event.
                # A recurring master is also a deletion candidate, but a
                # bounded search may legitimately omit a series that still
                # exists: one that ended or is sparse, or whose occurrences in
                # the window are all excluded, wherever its DTSTART lies.
                # Confirm its remote absence by UID before deleting it.
                # An override of a series that WAS pulled is eligible wherever
                # it sits (it may have been moved far from the window): the
                # server sends every override with its master, so one that is
                # not in `seen_uids` was removed upstream.
                if _should_prune_window(seen_uids, parse_failed):
                    eligible = or_(
                        and_(CalendarEvent.dtstart >= start, CalendarEvent.dtstart <= end),
                        and_(
                            CalendarEvent.dtstart < start,
                            CalendarEvent.rrule.isnot(None),
                            CalendarEvent.rrule != "",
                        ),
                    )
                    if seen_uids:
                        eligible = or_(eligible, CalendarEvent.series_uid.in_(seen_uids))
                    stale = db.query(CalendarEvent).filter(
                        CalendarEvent.calendar_id == local_cal.id,
                        CalendarEvent.origin == "caldav",
                        eligible,
                        ~CalendarEvent.uid.in_(seen_uids) if seen_uids else CalendarEvent.uid.isnot(None),
                    ).all()
                    deleted = 0
                    masters = []
                    for ev in stale:
                        if ev.rrule:
                            masters.append(ev)
                        else:
                            db.delete(ev)
                            deleted += 1
                    # One lookup per candidate, so a slow server must not be
                    # allowed to stretch them past the pull's time budget; what
                    # is not confirmed stays and is judged on the next pull.
                    for ev in masters:
                        if _clock() >= deadline:
                            result["errors"].append(
                                f"{display_name}: time budget exhausted before every missing recurring resource was confirmed"
                            )
                            break
                        try:
                            remote_cal.event_by_uid(ev.uid)
                        except NotFoundError:
                            pass  # Authoritative absence, not merely outside the window.
                        except Exception as e:
                            result["errors"].append(
                                f"{display_name}: could not confirm missing recurring resource ({type(e).__name__})"
                            )
                            continue
                        else:
                            continue
                        db.delete(ev)
                        deleted += 1
                    result["deleted"] += deleted
                    db.commit()
            except Exception as e:
                logger.exception("CalDAV sync failed for one calendar")
                result["errors"].append(str(e)[:200])
                db.rollback()
    finally:
        db.close()

    return result


def _encrypt_account_passwords(accounts: list) -> tuple[list, bool]:
    """Return ``(accounts, changed)`` with every plaintext password encrypted.

    Every *interactive* write path (``POST/PUT /api/calendar/config*``) already
    calls ``secret_storage.encrypt``. Two paths did not, and both land plaintext
    in ``data/user_prefs.json``:

      1. the legacy ``caldav`` -> ``caldav_accounts`` migration below, which
         copied ``legacy["password"]`` verbatim; and
      2. any account already stored plaintext by (1) on an earlier release —
         it is only ever *read* afterwards, and ``decrypt()`` passes plaintext
         through unchanged, so nothing ever re-encrypted it.

    ``encrypt()`` is a no-op on an already-``enc:``-prefixed value, so this is
    idempotent and safe to run on every load.

    Fail-safe: a value is only swapped in once it has been proved to round-trip.
    If the Fernet key is unreadable or mismatched, ``encrypt`` would still
    produce a token but ``decrypt`` would later return "" — silently destroying
    a working credential. Verifying the round-trip first means a broken key
    leaves the plaintext in place (still readable, still working) rather than
    bricking calendar sync.
    """
    try:
        from src import secret_storage as _ss
        encrypt = _ss.encrypt
        decrypt = _ss.decrypt
        is_encrypted = _ss.is_encrypted
    except (ImportError, AttributeError):
        # secret_storage unavailable or partially stubbed (several CalDAV tests
        # inject a module exposing only `decrypt`). Encryption is an at-rest
        # hardening pass, never a precondition for sync — degrade to a no-op
        # rather than break the caller's read path.
        return list(accounts or []), False

    changed = False
    out = []
    for acc in accounts or []:
        acc = dict(acc or {})
        pw = acc.get("password") or ""
        if pw and not is_encrypted(pw):
            try:
                token = encrypt(pw)
                if is_encrypted(token) and decrypt(token) == pw:
                    acc["password"] = token
                    changed = True
                else:
                    logger.error(
                        "CalDAV password encryption round-trip failed for account %s — "
                        "leaving the existing value untouched",
                        acc.get("id") or acc.get("label") or "?",
                    )
            except Exception:
                logger.exception(
                    "CalDAV password encryption failed for account %s — "
                    "leaving the existing value untouched",
                    acc.get("id") or acc.get("label") or "?",
                )
        out.append(acc)
    return out, changed


def _load_caldav_accounts(owner: str) -> list:
    """Return the list of CalDAV accounts for *owner*, auto-migrating the legacy
    single-account ``caldav`` key to the new ``caldav_accounts`` list on first call,
    and encrypting any password still stored in plaintext.

    The save step is best-effort: if ``save_for_user`` is unavailable (e.g. in a
    test with a minimal prefs mock) the migrated accounts are still returned; the
    next real call will just re-run the cheap migration again.
    """
    import uuid as _uuid
    from src.user_preferences import load_for_user

    prefs = load_for_user(owner) or {}
    if "caldav_accounts" in prefs:
        accounts = list(prefs["caldav_accounts"] or [])
        accounts, changed = _encrypt_account_passwords(accounts)
        if changed:
            prefs["caldav_accounts"] = accounts
            try:
                from src.user_preferences import save_for_user
                save_for_user(owner, prefs)
                logger.info(
                    "Encrypted %d plaintext CalDAV password(s) at rest for owner %s",
                    sum(1 for a in accounts if a.get("password")), owner,
                )
            except (ImportError, AttributeError):
                pass  # best-effort; the next call re-runs the cheap upgrade
        return accounts
    # Migrate legacy single-account config to the list format.
    legacy = prefs.get("caldav", {}) or {}
    if legacy.get("url"):
        accounts = [{
            "id": str(_uuid.uuid4()),
            "label": "CalDAV",
            "url": legacy["url"],
            "username": legacy.get("username", ""),
            "password": legacy.get("password", ""),
        }]
        accounts, _ = _encrypt_account_passwords(accounts)
        prefs["caldav_accounts"] = accounts
        prefs.pop("caldav", None)
        try:
            from src.user_preferences import save_for_user
            save_for_user(owner, prefs)
        except (ImportError, AttributeError):
            pass  # best-effort; next call re-migrates from the still-present legacy key
        return accounts
    return []


# One sync per owner at a time. The periodic loop, the calendar route and the
# token route all land here; two pulls racing on the same rows only produce
# duplicate-key errors, so a second caller waits for the first and then pulls
# again (it gets data at least as fresh as when it asked).
_OWNER_LOCKS: dict = {}


def _owner_lock(owner: str) -> asyncio.Lock:
    lock = _OWNER_LOCKS.get(owner)
    if lock is None:
        lock = _OWNER_LOCKS[owner] = asyncio.Lock()
    return lock


async def sync_caldav(owner: str) -> dict:
    """Pull CalDAV state into local DB for `owner` across all configured accounts.
    Returns aggregated counts + per-account errors. Serialized per owner."""
    lock = _owner_lock(owner)
    await lock.acquire()
    overdue: list = []
    token = _OVERDUE_SINK.set(overdue)
    try:
        return await _sync_caldav_locked(owner)
    finally:
        _OVERDUE_SINK.reset(token)
        if any(t.is_alive() for t in overdue):
            # A request of this pull is still running in a call that cannot be
            # interrupted: the owner stays locked until it ends, so no second
            # pull piles on, but this one answers now.
            _hold_until_ended(lock, overdue)
        else:
            lock.release()


_HOLDERS: "set[asyncio.Task]" = set()


def _hold_until_ended(lock: asyncio.Lock, threads: list) -> None:
    async def release_later():
        try:
            await wait_overdue_ended(threads)
        finally:
            lock.release()

    task = asyncio.ensure_future(release_later())
    _HOLDERS.add(task)
    task.add_done_callback(_HOLDERS.discard)


async def _sync_caldav_locked(owner: str) -> dict:
    from src.secret_storage import decrypt

    accounts = _load_caldav_accounts(owner)
    if not accounts:
        return {
            "calendars": 0, "events": 0, "deleted": 0,
            "errors": ["CalDAV is not configured"],
        }

    totals: dict = {"calendars": 0, "events": 0, "deleted": 0, "errors": []}
    if len(accounts) > MAX_CALDAV_ACCOUNTS:
        totals["errors"].append(
            f"{len(accounts) - MAX_CALDAV_ACCOUNTS} CalDAV account(s) beyond the limit of "
            f"{MAX_CALDAV_ACCOUNTS} were not pulled; remove accounts to get back under the limit"
        )
        accounts = accounts[:MAX_CALDAV_ACCOUNTS]
    for acc in accounts:
        url = (acc.get("url") or "").strip()
        user = (acc.get("username") or "").strip()
        pw = acc.get("password") or ""
        account_id = acc.get("id") or ""
        label = acc.get("label") or url or account_id
        try:
            pw = decrypt(pw)
        except Exception:
            pass
        if not (url and user and pw):
            totals["errors"].append(f"{label}: missing URL, username, or password")
            continue
        try:
            url = validate_caldav_url(url)
            # Keep owner custody through the actual blocking writer's lifetime.
            # Cancelling an await does not stop a thread. Use an executor Future
            # (not a separately cancellable Task), preserving to_thread's context.
            worker = asyncio.get_running_loop().run_in_executor(
                None, contextvars.copy_context().run,
                _sync_blocking, owner, url, user, pw, account_id,
            )
            try:
                result = await asyncio.shield(worker)
            except asyncio.CancelledError:
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if not worker.cancelled():
                    worker.exception()  # Consume any failure before propagating cancellation.
                raise
        except ValueError as e:
            result = {"calendars": 0, "events": 0, "deleted": 0, "errors": [str(e)]}
        except Exception as e:
            logger.exception("CalDAV sync raised for account %s", label)
            result = {"calendars": 0, "events": 0, "deleted": 0, "errors": [str(e)[:200]]}
        totals["calendars"] += result.get("calendars", 0)
        totals["events"] += result.get("events", 0)
        totals["deleted"] += result.get("deleted", 0)
        for err in result.get("errors", []):
            totals["errors"].append(f"{label}: {err}")
    return totals
