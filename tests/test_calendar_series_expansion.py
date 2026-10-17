"""Server-side expansion of recurring series: EXDATE, overrides, DST.

Covers ``_expand_rrule`` directly and ``GET /api/calendar/events`` against a real
SQLite database, with rows produced by the same ``_event_row_from_vevent`` the
CalDAV pull uses, so the key written by the pull and the key read by the
expansion are proven to agree. Names and times are synthetic.
"""
import asyncio
import importlib
import tempfile
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import CalendarCal, CalendarEvent
from src.caldav_sync import _event_row_from_vevent
from src.recurrence import (
    decode_keys,
    encode_keys,
    load_zone,
    recurrence_key,
    tzid_for,
    utc_naive_to_wall,
    wall_to_utc_naive,
)

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}", connect_args={"check_same_thread": False}, poolclass=NullPool
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)

NY = ZoneInfo("America/New_York")


@pytest.fixture
def cal(monkeypatch):
    mod = importlib.import_module("routes.calendar_routes")

    monkeypatch.setattr(mod, "SessionLocal", _TS)
    monkeypatch.setattr(mod, "CalendarEvent", CalendarEvent)
    monkeypatch.setattr(mod, "CalendarCal", CalendarCal)
    db = _TS()
    try:
        db.query(CalendarEvent).delete()
        db.query(CalendarCal).delete()
        db.add(CalendarCal(id="cal-a", owner="alice", name="Family", source="caldav", color="#112233"))
        db.add(CalendarCal(id="cal-b", owner="bob", name="Other", source="caldav"))
        db.commit()
    finally:
        db.close()
    return mod


def _wrap(*vevents):
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//t//EN", *vevents, "END:VCALENDAR", ""])


def _vevent(uid, *lines):
    return "\r\n".join(["BEGIN:VEVENT", f"UID:{uid}", *lines, "END:VEVENT"])


def _load(ics, calendar_id="cal-a"):
    """Store the rows the CalDAV pull would store for this iCalendar text."""
    db = _TS()
    try:
        for comp in Calendar.from_ical(ics).walk():
            if comp.name != "VEVENT":
                continue
            row_uid, fields = _event_row_from_vevent(comp)
            db.add(CalendarEvent(uid=row_uid, calendar_id=calendar_id, origin="caldav", **fields))
        db.commit()
    finally:
        db.close()


def _list(cal, start, end, user="alice"):
    endpoint = next(
        r.endpoint for r in cal.setup_calendar_routes().routes
        if r.path == "/api/calendar/events" and "GET" in r.methods
    )
    request = SimpleNamespace(state=SimpleNamespace(current_user=user))
    return asyncio.run(endpoint(request, start=start, end=end))["events"]


# Weekly Tuesday 15:30 America/New_York from 2026-10-13. US clocks go back on
# 2026-11-01, so 19:30Z becomes 20:30Z for the same wall time.
def _series_ics(*extra, rrule="RRULE:FREQ=WEEKLY;BYDAY=TU", exdates=()):
    master = _vevent(
        "series-a@test",
        "DTSTART;TZID=America/New_York:20261013T153000",
        "DTEND;TZID=America/New_York:20261013T163000",
        rrule,
        *[f"EXDATE;TZID=America/New_York:{d}" for d in exdates],
        "SUMMARY:Series A",
    )
    return _wrap(master, *extra)


def _override(rid, start=None, end=None, status=None, summary="Series A (moved)"):
    start = start or rid
    lines = [
        f"RECURRENCE-ID;TZID=America/New_York:{rid}",
        f"DTSTART;TZID=America/New_York:{start}",
        f"DTEND;TZID=America/New_York:{end or start}",
        f"SUMMARY:{summary}",
    ]
    if status:
        lines.append(f"STATUS:{status}")
    return _vevent("series-a@test", *lines)


# ── DST ───────────────────────────────────────────────────────────────────────

def test_series_in_a_zone_keeps_its_wall_clock_across_a_dst_change(cal):
    _load(_series_ics())
    events = _list(cal, "2026-10-26T00:00:00", "2026-11-17T00:00:00")
    assert [e["dtstart"] for e in events] == [
        "2026-10-27T19:30:00Z",   # 15:30 EDT
        "2026-11-03T20:30:00Z",   # 15:30 EST
        "2026-11-10T20:30:00Z",   # 15:30 EST
    ]
    assert all(e["dtend"][11:13] == str(int(e["dtstart"][11:13]) + 1) for e in events)
    assert all(e["is_recurrence"] and e["series_uid"] == "series-a@test" for e in events)


def test_series_without_a_zone_expands_in_fixed_utc_as_before(cal):
    """No usable TZID (custom VTIMEZONE): behaviour is unchanged, fixed UTC."""
    _load(_series_ics())
    db = _TS()
    try:
        db.query(CalendarEvent).filter_by(uid="series-a@test").update({"tzid": None})
        db.commit()
    finally:
        db.close()
    events = _list(cal, "2026-10-26T00:00:00", "2026-11-17T00:00:00")
    assert [e["dtstart"] for e in events] == [
        "2026-10-27T19:30:00Z", "2026-11-03T19:30:00Z", "2026-11-10T19:30:00Z",
    ]


def test_until_is_compared_on_the_series_wall_clock(cal):
    # 19:30Z on 2026-11-10 is 14:30 EST, before that day's 15:30 occurrence.
    _load(_series_ics(rrule="RRULE:FREQ=WEEKLY;BYDAY=TU;UNTIL=20261110T193000Z"))
    events = _list(cal, "2026-10-26T00:00:00", "2026-11-17T00:00:00")
    assert [e["dtstart"] for e in events] == ["2026-10-27T19:30:00Z", "2026-11-03T20:30:00Z"]


def test_nonexistent_and_ambiguous_wall_times_follow_rfc_5545():
    # 02:30 on 2027-03-14 does not exist: the offset before the gap (EST) applies.
    assert wall_to_utc_naive(datetime(2027, 3, 14, 2, 30), NY) == datetime(2027, 3, 14, 7, 30)
    # 01:30 on 2026-11-01 happens twice: the first (EDT) wins.
    assert wall_to_utc_naive(datetime(2026, 11, 1, 1, 30), NY) == datetime(2026, 11, 1, 5, 30)
    assert utc_naive_to_wall(datetime(2026, 11, 3, 20, 30), NY) == datetime(2026, 11, 3, 15, 30)


def test_tzid_for_ignores_utc_floating_and_unknown_zones():
    from icalendar import vDDDTypes

    def prop(text, tzid=None):
        return vDDDTypes.from_ical(text, tzid) if tzid else vDDDTypes.from_ical(text)

    assert tzid_for(prop("20261013T153000", "America/New_York")) == "America/New_York"
    assert tzid_for(prop("20261013T153000Z")) is None
    assert tzid_for(prop("20261013T153000")) is None
    assert tzid_for(datetime(2026, 10, 13, 15, 30, tzinfo=NY), {"TZID": "Not/AZone"}) is None
    assert load_zone("Not/AZone") is None and load_zone(None) is None


# ── EXDATE and overrides ──────────────────────────────────────────────────────

def test_exdate_occurrence_is_left_out(cal):
    _load(_series_ics(exdates=["20261103T153000"]))
    events = _list(cal, "2026-10-26T00:00:00", "2026-11-17T00:00:00")
    assert [e["dtstart"] for e in events] == ["2026-10-27T19:30:00Z", "2026-11-10T20:30:00Z"]


def test_moved_instance_replaces_its_occurrence(cal):
    moved = _override("20261103T153000", start="20261104T170000", end="20261104T180000")
    _load(_series_ics(moved))
    events = _list(cal, "2026-10-26T00:00:00", "2026-11-17T00:00:00")
    assert [(e["dtstart"], e["summary"]) for e in events] == [
        ("2026-10-27T19:30:00Z", "Series A"),
        ("2026-11-04T22:00:00Z", "Series A (moved)"),   # 17:00 EST
        ("2026-11-10T20:30:00Z", "Series A"),
    ]
    moved_event = events[1]
    assert moved_event["series_uid"] == "series-a@test"
    assert moved_event["is_recurrence"] is True
    assert moved_event["uid"] != "series-a@test"
    assert "20261103" not in "".join(e["dtstart"] for e in events)


def test_cancelled_instance_hides_its_occurrence_and_itself(cal):
    gone = _override("20261103T153000", status="CANCELLED")
    _load(_series_ics(gone))
    events = _list(cal, "2026-10-26T00:00:00", "2026-11-17T00:00:00")
    assert [e["dtstart"] for e in events] == ["2026-10-27T19:30:00Z", "2026-11-10T20:30:00Z"]


def test_instance_moved_out_of_the_window_still_hides_its_slot(cal):
    far = _override("20261103T153000", start="20270105T170000", end="20270105T180000")
    _load(_series_ics(far))
    events = _list(cal, "2026-10-26T00:00:00", "2026-11-17T00:00:00")
    assert [e["dtstart"] for e in events] == ["2026-10-27T19:30:00Z", "2026-11-10T20:30:00Z"]


def test_instance_moved_into_the_window_shows_up(cal):
    early = _override("20261201T153000", start="20261104T170000", end="20261104T180000")
    _load(_series_ics(early))
    events = _list(cal, "2026-11-01T00:00:00", "2026-11-08T00:00:00")
    assert [(e["dtstart"], e["summary"]) for e in events] == [
        ("2026-11-03T20:30:00Z", "Series A"),
        ("2026-11-04T22:00:00Z", "Series A (moved)"),
    ]


def test_all_day_series_with_exdate_and_cancelled_instance(cal):
    master = _vevent(
        "allday@test",
        "DTSTART;VALUE=DATE:20261012", "DTEND;VALUE=DATE:20261013",
        "RRULE:FREQ=WEEKLY;BYDAY=MO", "EXDATE;VALUE=DATE:20261026", "SUMMARY:Weekly all day",
    )
    gone = _vevent(
        "allday@test",
        "RECURRENCE-ID;VALUE=DATE:20261102", "DTSTART;VALUE=DATE:20261102",
        "DTEND;VALUE=DATE:20261103", "STATUS:CANCELLED", "SUMMARY:Weekly all day",
    )
    _load(_wrap(master, gone))
    events = _list(cal, "2026-10-19", "2026-11-17")
    assert [(e["dtstart"], e["dtend"], e["all_day"]) for e in events] == [
        ("2026-10-19", "2026-10-20", True),
        ("2026-11-09", "2026-11-10", True),
        ("2026-11-16", "2026-11-17", True),
    ]


def test_another_owners_overrides_do_not_touch_this_owners_series(cal):
    _load(_series_ics())
    # Bob has a cancelled override that names the same series uid in his calendar.
    db = _TS()
    try:
        db.add(CalendarEvent(
            uid="series-a@test::2026-11-03T20:30:00", calendar_id="cal-b", summary="x", origin="caldav",
            dtstart=datetime(2026, 11, 3, 20, 30), dtend=datetime(2026, 11, 3, 21, 30), status="cancelled",
            series_uid="series-a@test", recurrence_id="2026-11-03T20:30:00",
        ))
        db.commit()
    finally:
        db.close()
    events = _list(cal, "2026-10-26T00:00:00", "2026-11-17T00:00:00")
    assert len(events) == 3
    assert _list(cal, "2026-10-26T00:00:00", "2026-11-17T00:00:00", user="bob") == []


def test_plain_event_is_not_a_recurrence(cal):
    plain = _vevent("plain@test", "DTSTART:20261104T140000Z", "DTEND:20261104T150000Z", "SUMMARY:Once")
    _load(_wrap(plain))
    (event,) = _list(cal, "2026-11-01T00:00:00", "2026-11-08T00:00:00")
    assert event["is_recurrence"] is False and event["series_uid"] == "plain@test"


def test_expand_rrule_accepts_events_without_the_new_columns():
    """Callers (and older tests) pass objects that predate exdate/tzid/series_uid."""
    mod = importlib.import_module("routes.calendar_routes")

    ev = SimpleNamespace(
        uid="old", summary="s", dtstart=datetime(2026, 6, 1, 9), dtend=datetime(2026, 6, 1, 10),
        all_day=False, is_utc=False, rrule="FREQ=DAILY;COUNT=3", calendar=SimpleNamespace(name="c", color="#fff"),
        calendar_id="c1", color=None, description="", location="", event_type=None, importance="normal",
    )
    out = mod._expand_rrule(ev, datetime(2026, 6, 1), datetime(2026, 6, 10))
    assert len(out) == 3


# ── key helpers ───────────────────────────────────────────────────────────────

def test_key_round_trip_and_format():
    assert recurrence_key(datetime(2026, 11, 3, 20, 30), False) == "2026-11-03T20:30:00"
    assert recurrence_key(datetime(2026, 11, 3, 20, 30), True) == "2026-11-03"
    keys = {"2026-11-03T20:30:00", "2026-11-10T20:30:00"}
    assert decode_keys(encode_keys(keys)) == keys
    assert decode_keys(None) == frozenset() and decode_keys("") == frozenset()


# ── edits, deletes and export of synced series ────────────────────────────────

def _route(cal, method, path):
    return next(
        r.endpoint for r in cal.setup_calendar_routes().routes
        if r.path == path and method in r.methods
    )


def _request(user="alice"):
    return SimpleNamespace(state=SimpleNamespace(current_user=user))


@pytest.fixture
def pushed(monkeypatch):
    """Record CalDAV write-backs instead of talking to a server."""
    import src.caldav_writeback as wb

    calls = []

    async def fake_writeback(owner, source, calendar_id, event, delete=False):
        calls.append({"owner": owner, "calendar_id": calendar_id, "uid": event.get("uid"), "delete": delete})
        return True

    monkeypatch.setattr(wb, "writeback_event", fake_writeback)
    return calls


def _rows(*uids):
    db = _TS()
    try:
        return {r.uid: r for r in db.query(CalendarEvent).filter(CalendarEvent.uid.in_(uids)).all()}
    finally:
        db.close()


def _row_uids():
    db = _TS()
    try:
        return sorted(r.uid for r in db.query(CalendarEvent).all())
    finally:
        db.close()


OVERRIDE_UID = "series-a@test::2026-11-03T20:30:00"


def _series_with_overrides():
    _load(_series_ics(
        _override("20261103T153000", start="20261104T170000", end="20261104T180000"),
        _override("20261110T153000", status="CANCELLED"),
        exdates=["20261117T153000"],
    ))


def test_editing_a_moved_instance_changes_only_that_row(cal, pushed):
    from routes.calendar_routes import EventUpdate

    _series_with_overrides()
    update = _route(cal, "PUT", "/api/calendar/events/{uid}")
    out = asyncio.run(update(_request(), OVERRIDE_UID, EventUpdate(summary="Renamed instance")))
    assert out["ok"] is True and out["writeback"] == "skipped"
    rows = _rows("series-a@test", OVERRIDE_UID)
    assert rows[OVERRIDE_UID].summary == "Renamed instance"
    assert rows["series-a@test"].summary == "Series A"
    assert rows["series-a@test"].rrule
    assert pushed == []


def test_editing_a_series_with_exceptions_stays_local(cal, pushed):
    from routes.calendar_routes import EventUpdate

    _series_with_overrides()
    update = _route(cal, "PUT", "/api/calendar/events/{uid}")
    out = asyncio.run(update(_request(), "series-a@test", EventUpdate(summary="Retitled series")))
    assert out["writeback"] == "skipped"
    assert _rows("series-a@test")["series-a@test"].summary == "Retitled series"
    assert pushed == []


@pytest.mark.parametrize("bookkeeping", ["zone", "exdate", "override"])
def test_clearing_rrule_does_not_bypass_the_synced_series_writeback_guard(cal, pushed, bookkeeping):
    from routes.calendar_routes import EventUpdate

    if bookkeeping == "zone":
        _load(_series_ics())
    else:
        master = _vevent(
            "series-a@test", "DTSTART:20261013T193000Z", "DTEND:20261013T203000Z",
            "RRULE:FREQ=WEEKLY", "SUMMARY:Series A",
            *(["EXDATE:20261103T193000Z"] if bookkeeping == "exdate" else []),
        )
        override = [] if bookkeeping == "exdate" else [_vevent(
            "series-a@test", "RECURRENCE-ID:20261103T193000Z",
            "DTSTART:20261104T193000Z", "DTEND:20261104T203000Z", "SUMMARY:Moved",
        )]
        _load(_wrap(master, *override))

    update = _route(cal, "PUT", "/api/calendar/events/{uid}")
    out = asyncio.run(update(_request(), "series-a@test", EventUpdate(rrule="")))
    assert out.get("writeback") == "skipped"
    assert _rows("series-a@test")["series-a@test"].rrule == ""
    assert pushed == []


def test_editing_an_expanded_occurrence_still_targets_the_series(cal, pushed):
    """Existing behaviour, kept: an expanded occurrence id edits its series."""
    from routes.calendar_routes import EventUpdate

    _load(_series_ics())
    update = _route(cal, "PUT", "/api/calendar/events/{uid}")
    asyncio.run(update(_request(), "series-a@test::2026-11-10T20:30", EventUpdate(location="Room 9")))
    assert _rows("series-a@test")["series-a@test"].location == "Room 9"


def test_editing_a_plain_synced_event_is_still_pushed(cal, pushed):
    from routes.calendar_routes import EventUpdate

    _load(_wrap(_vevent("plain@test", "DTSTART:20261104T140000Z", "DTEND:20261104T150000Z", "SUMMARY:Once")))
    update = _route(cal, "PUT", "/api/calendar/events/{uid}")
    out = asyncio.run(update(_request(), "plain@test", EventUpdate(summary="Once, renamed")))
    assert out == {"ok": True}
    assert pushed == [{"owner": "alice", "calendar_id": "cal-a", "uid": "plain@test", "delete": False}]


def test_editing_a_simple_utc_series_is_still_pushed(cal, pushed):
    """No zone, no EXDATE, no overrides: the push loses nothing, so it happens."""
    from routes.calendar_routes import EventUpdate

    _load(_wrap(_vevent(
        "utc-series@test", "DTSTART:20261104T140000Z", "DTEND:20261104T150000Z",
        "RRULE:FREQ=DAILY;COUNT=5", "SUMMARY:Daily",
    )))
    update = _route(cal, "PUT", "/api/calendar/events/{uid}")
    asyncio.run(update(_request(), "utc-series@test", EventUpdate(summary="Daily, renamed")))
    assert [c["uid"] for c in pushed] == ["utc-series@test"]


def test_deleting_a_moved_instance_leaves_the_series_alone(cal, pushed):
    _series_with_overrides()
    delete = _route(cal, "DELETE", "/api/calendar/events/{uid}")
    out = asyncio.run(delete(_request(), OVERRIDE_UID))
    assert out["writeback"] == "skipped"
    # Neither the moved event nor its original slot may reappear after DELETE.
    assert _list(cal, "2026-11-01T00:00:00", "2026-11-08T00:00:00") == []
    # The master and unaffected occurrences remain usable.
    assert _list(cal, "2026-10-26T00:00:00", "2026-11-01T00:00:00")[0]["series_uid"] == "series-a@test"
    assert pushed == []


def test_deleting_a_series_removes_its_overrides_and_deletes_remotely_once(cal, pushed):
    _series_with_overrides()
    _load(_wrap(_vevent("plain@test", "DTSTART:20261104T140000Z", "DTEND:20261104T150000Z", "SUMMARY:Once")))
    delete = _route(cal, "DELETE", "/api/calendar/events/{uid}")
    assert asyncio.run(delete(_request(), "series-a@test")) == {"ok": True}
    assert _row_uids() == ["plain@test"]
    assert pushed == [{"owner": "alice", "calendar_id": "cal-a", "uid": "series-a@test", "delete": True}]


def test_deleting_a_plain_synced_event_is_still_pushed(cal, pushed):
    _load(_wrap(_vevent("plain@test", "DTSTART:20261104T140000Z", "DTEND:20261104T150000Z", "SUMMARY:Once")))
    delete = _route(cal, "DELETE", "/api/calendar/events/{uid}")
    asyncio.run(delete(_request(), "plain@test"))
    assert _row_uids() == []
    assert [(c["uid"], c["delete"]) for c in pushed] == [("plain@test", True)]


def test_another_owner_cannot_edit_or_delete_an_override_row(cal, pushed):
    from fastapi import HTTPException
    from routes.calendar_routes import EventUpdate

    _series_with_overrides()
    update = _route(cal, "PUT", "/api/calendar/events/{uid}")
    delete = _route(cal, "DELETE", "/api/calendar/events/{uid}")
    for call in (
        lambda: update(_request("bob"), OVERRIDE_UID, EventUpdate(summary="x")),
        lambda: delete(_request("bob"), OVERRIDE_UID),
    ):
        with pytest.raises(HTTPException) as err:
            asyncio.run(call())
        assert err.value.status_code == 404
    assert OVERRIDE_UID in _row_uids()
    assert pushed == []


def test_export_keeps_exdates_and_recurrence_ids(cal):
    _series_with_overrides()
    export = _route(cal, "GET", "/api/calendar/export/{cal_id}")
    text = asyncio.run(export(_request(), "cal-a")).body.decode().replace("\r\n", "\n")
    lines = text.split("\n")
    master_block = text.split("BEGIN:VEVENT")[1:]
    # Every VEVENT is under the series UID; no internal row uid leaks out.
    assert [l for l in lines if l.startswith("UID:")] == ["UID:series-a@test"] * 2
    assert "::" not in text
    # The moved instance is exported with its original slot as RECURRENCE-ID.
    assert "RECURRENCE-ID:20261103T203000Z" in lines
    assert "DTSTART:20261104T220000Z" in lines
    # The master excludes its EXDATE and the cancelled instance; the cancelled
    # override itself is not exported as an event.
    assert "EXDATE:20261117T203000Z" in lines
    assert "EXDATE:20261110T203000Z" in lines
    assert len(master_block) == 2
    assert sum(1 for l in lines if l.startswith("RRULE:")) == 1
