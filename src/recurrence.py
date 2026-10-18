"""Recurring-series bookkeeping shared by the CalDAV pull and the event listing.

A CalDAV resource for a recurring event holds one master VEVENT (``RRULE``,
optional ``EXDATE``) plus any number of ``RECURRENCE-ID`` override VEVENTs, all
under one ``UID``. ``calendar_events.uid`` is the primary key, so the pull stores:

* the master under the VEVENT ``UID`` (``rrule``, ``exdate``, ``tzid`` set);
* each override under ``override_uid(UID, key)`` with ``series_uid`` and
  ``recurrence_id`` set, ``rrule`` empty. It is an ordinary single event as far
  as the overlap query is concerned; the series expansion consults its key to
  drop the occurrence it replaces. A ``STATUS:CANCELLED`` override is stored with
  ``status="cancelled"``, which hides it from listings while still suppressing
  the occurrence.

An occurrence is identified by its ORIGINAL start, as a *key*: ``YYYY-MM-DD``
for an all-day series, otherwise ``YYYY-MM-DDTHH:MM:SS`` as a naive UTC instant
(floating series stay floating). The same function builds the key for EXDATE,
RECURRENCE-ID and the expansion, so the three always agree.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

KEY_SEP = "::"
_UTC_NAMES = {"UTC", "ETC/UTC", "GMT", "Z"}


def recurrence_key(value, all_day: bool) -> str:
    """Canonical text identifying one occurrence start (see module docstring)."""
    if not isinstance(value, datetime):
        value = datetime(value.year, value.month, value.day)
    if all_day:
        return value.strftime("%Y-%m-%d")
    return value.strftime("%Y-%m-%dT%H:%M:%S")


def override_uid(series_uid: str, key: str) -> str:
    """Primary key for the row holding one RECURRENCE-ID override."""
    return f"{series_uid}{KEY_SEP}{key}"


def encode_keys(keys: Iterable[str]) -> str:
    """Stable text form of a key set, for the ``exdate`` column."""
    return ",".join(sorted({k for k in keys if k}))


def decode_keys(text: Optional[str]) -> frozenset:
    return frozenset(k for k in (text or "").split(",") if k)


def load_zone(tzid: Optional[str]) -> Optional[ZoneInfo]:
    """Resolve an IANA zone name; None when absent or unknown to this host."""
    if not tzid:
        return None
    try:
        return ZoneInfo(tzid)
    except Exception:
        return None


def tzid_for(value, params=None) -> Optional[str]:
    """IANA zone name behind a tz-aware DTSTART, or None.

    None for date-only and floating values, for UTC (a fixed instant needs no
    zone to expand), and for zone names this host cannot resolve (a custom
    VTIMEZONE). A series without a usable zone expands in fixed UTC, which
    drifts by an hour across a DST change; see ``routes/calendar_routes``.

    The ``TZID`` parameter is tried first, then the zone the parser already
    resolved on the value. They differ for a non-IANA name such as the Windows
    ``Eastern Standard Time`` that Exchange and Outlook servers send: icalendar
    maps it to ``America/New_York`` on the value, but ``ZoneInfo`` cannot load
    the parameter text.
    """
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    candidates = []
    if params is not None:
        try:
            candidates.append(params.get("TZID"))
        except Exception:
            pass
    candidates.append(getattr(value.tzinfo, "key", None))
    candidates.append(getattr(value.tzinfo, "zone", None))
    for candidate in candidates:
        name = str(candidate or "")
        if not name:
            continue
        if name.upper() in _UTC_NAMES:
            return None
        if load_zone(name) is not None:
            return name
    return None


def wall_to_utc_naive(wall: datetime, zone: ZoneInfo) -> datetime:
    """Naive local wall time to naive UTC.

    A time skipped by a spring-forward gap uses the offset before the gap and an
    ambiguous fall-back time takes its first occurrence (RFC 5545 section
    3.3.5), which is what ``fold=0`` gives.
    """
    return wall.replace(tzinfo=zone).astimezone(timezone.utc).replace(tzinfo=None)


def utc_naive_to_wall(utc: datetime, zone: ZoneInfo) -> datetime:
    return utc.replace(tzinfo=timezone.utc).astimezone(zone).replace(tzinfo=None)


# Slack added to a wall-time lower bound so a DST shift cannot hide an
# occurrence; the caller's overlap filter then trims the extras.
WALL_MARGIN = timedelta(days=1)
