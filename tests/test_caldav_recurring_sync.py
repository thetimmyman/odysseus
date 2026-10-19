"""CalDAV pull must keep a recurring series intact (master + EXDATE + overrides).

One CalDAV resource holds a recurring master VEVENT plus RECURRENCE-ID override
VEVENTs, all under one UID. ``calendar_events.uid`` is the primary key and the
pull used to write every VEVENT of the resource to the same row, so the last
override won, the RRULE was lost and EXDATEs were ignored: the series showed up
as a single stale instance. These tests drive ``_sync_blocking`` against a fake
CalDAV server that serves real iCalendar text (parsed by ``icalendar``) and
assert the rows it leaves behind. Names and times are synthetic.
"""
import asyncio
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import CalendarCal, CalendarEvent
from src import caldav_sync
from src.recurrence import override_uid, recurrence_key

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)

NY = ZoneInfo("America/New_York")
CAL_URL = "https://caldav.example.test/dav/me/events"


# ── fake CalDAV server ────────────────────────────────────────────────────────

class _Obj:
    def __init__(self, data):
        self.data = data


class _Remote:
    """Mutable server state: tests change ``resources`` between syncs."""
    resources: list = []
    fail_date_search = False


class _FakeCalendar:
    def __init__(self, url):
        self.url = url
        self.name = "Family"

    def date_search(self, start, end, expand=False):
        assert expand is False, "series must be pulled unexpanded"
        return [_Obj(r) for r in _Remote.resources]

    def event_by_uid(self, uid):
        from icalendar import Calendar
        from caldav.lib.error import NotFoundError

        for resource in _Remote.resources:
            if any(str(comp.get("uid", "")) == uid
                   for comp in Calendar.from_ical(resource).walk()
                   if comp.name == "VEVENT"):
                return _Obj(resource)
        raise NotFoundError("resource absent")


class _FakePrincipal:
    def calendars(self):
        return [_FakeCalendar(CAL_URL)]


class _FakeClient:
    def __init__(self, url=None, username=None, password=None, timeout=None):
        self.url = url
        self.timeout = timeout
        self.session = types.SimpleNamespace(max_redirects=30)

    def principal(self):
        return _FakePrincipal()

    def calendar(self, url=None):
        return _FakeCalendar(url)


@pytest.fixture(autouse=True)
def _server(monkeypatch):
    fake = types.ModuleType("caldav")
    fake.DAVClient = _FakeClient
    err = types.ModuleType("caldav.lib.error")
    err.AuthorizationError = type("AuthorizationError", (Exception,), {})
    err.NotFoundError = type("NotFoundError", (Exception,), {})
    lib = types.ModuleType("caldav.lib")
    lib.error = err
    fake.lib = lib
    monkeypatch.setitem(sys.modules, "caldav", fake)
    monkeypatch.setitem(sys.modules, "caldav.lib", lib)
    monkeypatch.setitem(sys.modules, "caldav.lib.error", err)
    monkeypatch.setattr(cdb, "SessionLocal", _TS, raising=False)
    monkeypatch.setattr(caldav_sync, "_RESUME_AT", {})
    _Remote.resources = []
    db = _TS()
    try:
        db.query(CalendarEvent).delete()
        db.query(CalendarCal).delete()
        db.commit()
    finally:
        db.close()
    yield


# ── iCalendar builders ────────────────────────────────────────────────────────

def _ics(*vevents):
    return "\r\n".join(["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//test//EN", *vevents, "END:VCALENDAR", ""])


def _tz(dt):  # 20261013T153000 for a NY wall time
    return dt.strftime("%Y%m%dT%H%M%S")


def _vevent(uid, *lines):
    return "\r\n".join(["BEGIN:VEVENT", f"UID:{uid}", *lines, "END:VEVENT"])


def _ny(d, hour, minute=0):
    return datetime(d.year, d.month, d.day, hour, minute)


def _tuesday(days_ago):
    d = (datetime.utcnow() - timedelta(days=days_ago)).date()
    while d.weekday() != 1:
        d -= timedelta(days=1)
    return d


def _utc_of(wall_ny):
    return wall_ny.replace(tzinfo=NY).astimezone(timezone.utc).replace(tzinfo=None)


def _sync():
    return caldav_sync._sync_blocking("alice", "https://caldav.example.test", "u", "p", "acct-1")


def _public_sync(monkeypatch):
    import src.secret_storage as secrets

    monkeypatch.setattr(caldav_sync, "_OWNER_LOCKS", {})
    monkeypatch.setattr(caldav_sync, "_load_caldav_accounts", lambda owner: [
        {"id": "acct-1", "url": "https://caldav.example.test", "username": "u", "password": "p"},
    ])
    monkeypatch.setattr(caldav_sync, "validate_caldav_url", lambda url: url)
    monkeypatch.setattr(secrets, "decrypt", lambda value: value)
    return asyncio.run(caldav_sync.sync_caldav("alice"))


def _rows():
    db = _TS()
    try:
        return {e.uid: e for e in db.query(CalendarEvent).all()}
    finally:
        db.close()


def _row_fingerprints():
    return {
        uid: tuple((column.name, getattr(row, column.name))
                   for column in CalendarEvent.__table__.columns)
        for uid, row in _rows().items()
    }


def _weekly_with_exceptions():
    """A weekly Tuesday 15:30 series (zone-based) with one EXDATE, one moved
    instance and one cancelled instance. The overrides come AFTER the master,
    the order that made the last override clobber the master."""
    first = _tuesday(35)
    ex = first + timedelta(days=7)
    moved = first + timedelta(days=14)
    cancelled = first + timedelta(days=21)
    master = _vevent(
        "series-a@test",
        f"DTSTART;TZID=America/New_York:{_tz(_ny(first, 15, 30))}",
        f"DTEND;TZID=America/New_York:{_tz(_ny(first, 16, 30))}",
        "RRULE:FREQ=WEEKLY;BYDAY=TU",
        f"EXDATE;TZID=America/New_York:{_tz(_ny(ex, 15, 30))}",
        "SUMMARY:Series A",
    )
    override_moved = _vevent(
        "series-a@test",
        f"RECURRENCE-ID;TZID=America/New_York:{_tz(_ny(moved, 15, 30))}",
        f"DTSTART;TZID=America/New_York:{_tz(_ny(moved + timedelta(days=1), 17, 0))}",
        f"DTEND;TZID=America/New_York:{_tz(_ny(moved + timedelta(days=1), 18, 0))}",
        "SUMMARY:Series A (moved)",
    )
    override_cancelled = _vevent(
        "series-a@test",
        f"RECURRENCE-ID;TZID=America/New_York:{_tz(_ny(cancelled, 15, 30))}",
        f"DTSTART;TZID=America/New_York:{_tz(_ny(cancelled, 15, 30))}",
        f"DTEND;TZID=America/New_York:{_tz(_ny(cancelled, 16, 30))}",
        "STATUS:CANCELLED",
        "SUMMARY:Series A",
    )
    return _ics(master, override_moved, override_cancelled), first, ex, moved, cancelled


# ── tests ─────────────────────────────────────────────────────────────────────

def test_series_master_keeps_rrule_exdate_and_zone_and_overrides_get_own_rows():
    ics, first, ex, moved, cancelled = _weekly_with_exceptions()
    _Remote.resources = [ics]

    result = _sync()
    assert result["errors"] == []
    rows = _rows()
    assert len(rows) == 3

    master = rows["series-a@test"]
    assert master.rrule == "FREQ=WEEKLY;BYDAY=TU"
    assert master.dtstart == _utc_of(_ny(first, 15, 30))
    assert master.dtend - master.dtstart == timedelta(hours=1)
    assert master.is_utc is True
    assert master.tzid == "America/New_York"
    assert master.series_uid is None and master.recurrence_id is None
    assert master.status == "confirmed"
    assert master.exdate == recurrence_key(_utc_of(_ny(ex, 15, 30)), False)

    moved_key = recurrence_key(_utc_of(_ny(moved, 15, 30)), False)
    mrow = rows[override_uid("series-a@test", moved_key)]
    assert mrow.series_uid == "series-a@test"
    assert mrow.recurrence_id == moved_key
    assert mrow.rrule == "" and mrow.exdate == "" and mrow.tzid is None
    assert mrow.summary == "Series A (moved)"
    assert mrow.dtstart == _utc_of(_ny(moved + timedelta(days=1), 17, 0))

    cancel_key = recurrence_key(_utc_of(_ny(cancelled, 15, 30)), False)
    crow = rows[override_uid("series-a@test", cancel_key)]
    assert crow.status == "cancelled"
    assert crow.series_uid == "series-a@test"


def test_resync_is_idempotent():
    ics, *_ = _weekly_with_exceptions()
    _Remote.resources = [ics]
    _sync()
    before = {u: (e.summary, e.dtstart, e.dtend, e.rrule, e.exdate, e.tzid, e.series_uid, e.recurrence_id, e.status)
              for u, e in _rows().items()}
    result = _sync()
    after = {u: (e.summary, e.dtstart, e.dtend, e.rrule, e.exdate, e.tzid, e.series_uid, e.recurrence_id, e.status)
             for u, e in _rows().items()}
    assert result["errors"] == [] and result["deleted"] == 0
    assert after == before


def test_row_clobbered_by_the_old_sync_is_repaired():
    """The old pull left one row under the series UID holding the LAST override
    (no RRULE). The next pull must restore the master in place."""
    ics, first, _, moved, _ = _weekly_with_exceptions()
    db = _TS()
    try:
        db.add(CalendarCal(id=caldav_sync._stable_cal_id(CAL_URL, owner="alice", account_id="acct-1"),
                           owner="alice", name="Family", source="caldav", account_id="acct-1"))
        db.commit()
        cal_id = db.query(CalendarCal).first().id
        db.add(CalendarEvent(
            uid="series-a@test", calendar_id=cal_id, summary="Series A", origin="caldav",
            dtstart=_utc_of(_ny(moved, 15, 30)), dtend=_utc_of(_ny(moved, 16, 30)), rrule="",
        ))
        db.commit()
    finally:
        db.close()
    _Remote.resources = [ics]
    _sync()
    master = _rows()["series-a@test"]
    assert master.rrule == "FREQ=WEEKLY;BYDAY=TU"
    assert master.dtstart == _utc_of(_ny(first, 15, 30))


def test_override_removed_upstream_is_pruned_and_survivors_kept():
    ics, _, _, moved, cancelled = _weekly_with_exceptions()
    _Remote.resources = [ics]
    _sync()
    moved_uid = override_uid("series-a@test", recurrence_key(_utc_of(_ny(moved, 15, 30)), False))
    cancel_uid = override_uid("series-a@test", recurrence_key(_utc_of(_ny(cancelled, 15, 30)), False))
    assert {moved_uid, cancel_uid} <= set(_rows())

    # Upstream reverts the moved instance: only the master and the cancelled override remain.
    vevents = ics.split("BEGIN:VEVENT")
    trimmed = "BEGIN:VEVENT".join([vevents[0], vevents[1], vevents[3]])
    _Remote.resources = [trimmed]
    result = _sync()
    rows = _rows()
    assert result["deleted"] == 1
    assert moved_uid not in rows
    assert cancel_uid in rows and "series-a@test" in rows


def test_override_moved_far_outside_the_window_is_still_pruned_when_removed():
    """The server sends every override with its master, so an override that is
    missing from a pull whose master was seen is gone, wherever it was moved to."""
    first = _tuesday(35)
    target = first + timedelta(days=14)
    far = target + timedelta(days=800)
    master = _vevent(
        "series-a@test",
        f"DTSTART;TZID=America/New_York:{_tz(_ny(first, 15, 30))}",
        f"DTEND;TZID=America/New_York:{_tz(_ny(first, 16, 30))}",
        "RRULE:FREQ=WEEKLY;BYDAY=TU",
        "SUMMARY:Series A",
    )
    moved = _vevent(
        "series-a@test",
        f"RECURRENCE-ID;TZID=America/New_York:{_tz(_ny(target, 15, 30))}",
        f"DTSTART;TZID=America/New_York:{_tz(_ny(far, 15, 30))}",
        f"DTEND;TZID=America/New_York:{_tz(_ny(far, 16, 30))}",
        "SUMMARY:Series A (moved far)",
    )
    _Remote.resources = [_ics(master, moved)]
    _sync()
    moved_uid = override_uid("series-a@test", recurrence_key(_utc_of(_ny(target, 15, 30)), False))
    assert moved_uid in _rows()

    _Remote.resources = [_ics(master)]
    result = _sync()
    assert result["deleted"] == 1
    assert moved_uid not in _rows() and "series-a@test" in _rows()


def test_series_deleted_upstream_is_pruned_even_though_its_dtstart_is_old():
    """A master keeps its original DTSTART, which may be older than the sync
    window; a series that vanished from the server must still go."""
    db = _TS()
    try:
        cal_id = caldav_sync._stable_cal_id(CAL_URL, owner="alice", account_id="acct-1")
        db.add(CalendarCal(id=cal_id, owner="alice", name="Family", source="caldav", account_id="acct-1"))
        old = datetime.utcnow() - timedelta(days=700)
        db.add(CalendarEvent(uid="gone-series@test", calendar_id=cal_id, summary="Gone", origin="caldav",
                             dtstart=old, dtend=old + timedelta(hours=1), rrule="FREQ=WEEKLY"))
        # Must survive: a local recurring event, and an old non-recurring pulled event.
        db.add(CalendarEvent(uid="local-series", calendar_id=cal_id, summary="Mine", origin=None,
                             dtstart=old, dtend=old + timedelta(hours=1), rrule="FREQ=WEEKLY"))
        db.add(CalendarEvent(uid="old-single@test", calendar_id=cal_id, summary="Old", origin="caldav",
                             dtstart=old, dtend=old + timedelta(hours=1), rrule=""))
        db.commit()
    finally:
        db.close()
    _Remote.resources = []
    result = _sync()
    rows = _rows()
    assert "gone-series@test" not in rows
    assert "local-series" in rows
    assert "old-single@test" in rows
    assert result["deleted"] == 1


@pytest.mark.parametrize("rule", ["FREQ=WEEKLY;COUNT=2", "FREQ=YEARLY;INTERVAL=5"])
def test_bounded_pull_does_not_delete_an_existing_out_of_window_series(monkeypatch, rule):
    """An ended or sparse series can exist remotely without matching this window."""
    old = datetime.utcnow() - timedelta(days=700)
    resource = _ics(_vevent(
        "outside-window@test", f"DTSTART:{old:%Y%m%dT%H%M%SZ}",
        f"DTEND:{old + timedelta(hours=1):%Y%m%dT%H%M%SZ}",
        "RRULE:" + rule, "SUMMARY:Retained history",
    ))
    _Remote.resources = [resource]
    _sync()  # Seed the cache as if this resource had been observed previously.
    before = _row_fingerprints()
    assert set(before) == {"outside-window@test"}
    monkeypatch.setattr(_FakeCalendar, "date_search", lambda self, start, end, expand=False: [])

    result = _public_sync(monkeypatch)

    assert result["errors"] == []
    assert result["deleted"] == 0
    assert _row_fingerprints() == before


def test_old_series_is_retained_when_its_remote_absence_cannot_be_confirmed(monkeypatch):
    old = datetime.utcnow() - timedelta(days=700)
    _Remote.resources = [_ics(_vevent(
        "uncertain-series@test", f"DTSTART:{old:%Y%m%dT%H%M%SZ}",
        "RRULE:FREQ=WEEKLY", "SUMMARY:Uncertain remote state",
    ))]
    _sync()
    before = _row_fingerprints()
    assert set(before) == {"uncertain-series@test"}
    monkeypatch.setattr(_FakeCalendar, "date_search", lambda self, start, end, expand=False: [])

    def unavailable(self, uid):
        raise RuntimeError("synthetic lookup unavailable")

    monkeypatch.setattr(_FakeCalendar, "event_by_uid", unavailable)
    result = _public_sync(monkeypatch)
    assert result["deleted"] == 0
    assert any("could not confirm" in error for error in result["errors"])
    assert _row_fingerprints() == before


def test_series_inside_the_window_is_confirmed_before_it_is_deleted(monkeypatch):
    """A time-range query also omits a series whose every occurrence in the window
    is excluded, so a master that starts inside the window needs the same proof of
    absence as an old one."""
    first = datetime.utcnow() + timedelta(days=10)
    days = [first + timedelta(days=n) for n in range(2)]
    _Remote.resources = [_ics(_vevent(
        "all-excluded@test", f"DTSTART:{first:%Y%m%dT%H%M%S}Z",
        f"DTEND:{first + timedelta(hours=1):%Y%m%dT%H%M%S}Z", "RRULE:FREQ=DAILY;COUNT=2",
        *[f"EXDATE:{d:%Y%m%dT%H%M%S}Z" for d in days], "SUMMARY:All excluded",
    ))]
    _sync()
    before = _row_fingerprints()
    assert set(before) == {"all-excluded@test"}
    monkeypatch.setattr(_FakeCalendar, "date_search", lambda self, start, end, expand=False: [])

    result = _public_sync(monkeypatch)

    assert result["errors"] == [] and result["deleted"] == 0
    assert _row_fingerprints() == before

    _Remote.resources = []
    assert _public_sync(monkeypatch)["deleted"] == 1
    assert _rows() == {}


# ── a pull has a time budget ──────────────────────────────────────────────────

def _fake_clock(monkeypatch, seconds_per_step=0.0):
    """Replace the pull's clock; every call to ``tick()`` moves it on."""
    clock = {"now": 0.0}
    monkeypatch.setattr(caldav_sync, "_clock", lambda: clock["now"])

    def tick(seconds):
        clock["now"] += seconds

    return tick


def test_a_pull_that_runs_out_of_time_stops_confirming_and_keeps_what_is_left(monkeypatch):
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "150")
    old = datetime.utcnow() - timedelta(days=700)
    _Remote.resources = [
        _ics(_vevent(f"slow-{n}@test", f"DTSTART:{old:%Y%m%dT%H%M%S}Z", "RRULE:FREQ=WEEKLY;COUNT=3", "SUMMARY:Slow"))
        for n in range(3)
    ]
    _sync()
    assert len(_rows()) == 3
    tick = _fake_clock(monkeypatch)
    lookups = []

    def slow_absent(self, uid):
        from caldav.lib.error import NotFoundError
        lookups.append(uid)
        tick(100)  # each lookup takes 100s; the budget is 150s
        raise NotFoundError("absent")

    monkeypatch.setattr(_FakeCalendar, "date_search", lambda self, start, end, expand=False: [])
    monkeypatch.setattr(_FakeCalendar, "event_by_uid", slow_absent)

    result = _sync()

    assert len(lookups) == 2, "no lookup may start once the budget is spent"
    assert result["deleted"] == 2 and len(_rows()) == 1
    assert any("time budget" in error for error in result["errors"])
    assert not any(uid in _rows() for uid in lookups)


def test_a_pull_that_runs_out_of_time_does_not_start_the_next_calendar(monkeypatch):
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "150")
    tick = _fake_clock(monkeypatch)
    pulled = []

    def date_search(self, start, end, expand=False):
        pulled.append(self.url)
        tick(200)
        return []

    monkeypatch.setattr(_FakeCalendar, "date_search", date_search)
    monkeypatch.setattr(
        _FakePrincipal, "calendars",
        lambda self: [_FakeCalendar(CAL_URL), _FakeCalendar(CAL_URL + "-second")],
    )

    result = _sync()

    assert pulled == [CAL_URL]
    assert result["calendars"] == 1
    assert any("time budget" in error for error in result["errors"])


class _SlowServer:
    """Two calendars, one fake clock. Each calendar's ``date_search`` costs
    ``seconds`` on the clock and serves ``events[url]``; ``pulled`` records which
    calendars a pass reached, in order."""

    SECOND = CAL_URL + "-second"
    THIRD = CAL_URL + "-third"

    def __init__(self, monkeypatch, urls, seconds=200.0):
        self.tick = _fake_clock(monkeypatch)
        self.seconds = seconds
        self.events = {url: [] for url in urls}
        self.pulled = []
        server = self

        def date_search(cal, start, end, expand=False):
            server.pulled.append(cal.url)
            server.tick(server.seconds)
            return [_Obj(r) for r in server.events[cal.url]]

        monkeypatch.setattr(_FakeCalendar, "date_search", date_search)
        monkeypatch.setattr(
            _FakePrincipal, "calendars", lambda self: [_FakeCalendar(url) for url in urls],
        )

    def pass_(self, owner="alice", account_id="acct-1"):
        """One pull of the account; the calendars it reached, in order."""
        self.pulled.clear()
        result = caldav_sync._sync_blocking(owner, "https://caldav.example.test", "u", "p", account_id)
        return list(self.pulled), result


def test_the_next_pass_starts_with_the_calendar_the_last_one_did_not_reach(monkeypatch):
    """A slow server used up the budget on the first calendar in every pass, so the
    tail was never pulled; the pass has to pick up where the previous one stopped."""
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "150")
    server = _SlowServer(monkeypatch, [CAL_URL, _SlowServer.SECOND, _SlowServer.THIRD])

    reached = [server.pass_()[0] for _ in range(6)]

    assert reached == [
        [CAL_URL], [_SlowServer.SECOND], [_SlowServer.THIRD],
        [CAL_URL], [_SlowServer.SECOND], [_SlowServer.THIRD],
    ]


def test_a_calendar_the_budget_never_reached_is_pruned_on_a_later_pass(monkeypatch):
    """The skipped tail kept events deleted upstream: its stale-row prune is part
    of its pull, so it has to get a pull."""
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "150")
    server = _SlowServer(monkeypatch, [CAL_URL, _SlowServer.SECOND], seconds=0.0)
    soon = datetime.utcnow() + timedelta(days=3)
    server.events[_SlowServer.SECOND] = [_ics(_vevent(
        "tail@test", f"DTSTART:{soon:%Y%m%dT%H%M%S}Z", "SUMMARY:Tail",
    ))]
    reached, _ = server.pass_()
    assert reached == [CAL_URL, _SlowServer.SECOND] and "tail@test" in _rows()

    server.events[_SlowServer.SECOND] = []  # deleted upstream
    server.seconds = 200.0  # and the server turns slow
    first, first_result = server.pass_()
    assert first == [CAL_URL] and first_result["deleted"] == 0 and "tail@test" in _rows()

    second, second_result = server.pass_()
    assert second == [_SlowServer.SECOND] and second_result["deleted"] == 1
    assert "tail@test" not in _rows()
    assert any("time budget" in error for error in second_result["errors"])


def test_a_pass_that_gets_through_every_calendar_starts_at_the_top_again(monkeypatch):
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "150")
    server = _SlowServer(monkeypatch, [CAL_URL, _SlowServer.SECOND])

    assert server.pass_()[0] == [CAL_URL]  # budget spent after the first
    server.seconds = 1.0
    finished, result = server.pass_()  # resumes at the second, wraps round to the first
    assert finished == [_SlowServer.SECOND, CAL_URL]
    assert result["errors"] == []

    assert server.pass_()[0] == [CAL_URL, _SlowServer.SECOND]  # back in server order


def test_a_pass_that_ends_on_its_last_calendar_has_nothing_to_resume(monkeypatch):
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "150")
    server = _SlowServer(monkeypatch, [CAL_URL, _SlowServer.SECOND], seconds=100.0)

    first, first_result = server.pass_()  # 100s each against 150s: both begin, the last one ends over budget

    assert first == [CAL_URL, _SlowServer.SECOND]
    assert first_result["errors"] == []
    assert caldav_sync._RESUME_AT == {}
    assert server.pass_()[0] == [CAL_URL, _SlowServer.SECOND]


def test_the_resume_point_follows_the_calendar_not_its_position(monkeypatch):
    """The server may reorder or drop calendars between passes."""
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "150")
    urls = [CAL_URL, _SlowServer.SECOND, _SlowServer.THIRD]
    server = _SlowServer(monkeypatch, urls)
    assert server.pass_()[0] == [CAL_URL]  # next up: the second

    urls[:] = [_SlowServer.THIRD, _SlowServer.SECOND, CAL_URL]  # reordered
    assert server.pass_()[0] == [_SlowServer.SECOND]

    urls[:] = [CAL_URL, _SlowServer.THIRD]  # the calendar it was heading for is gone
    assert server.pass_()[0] == [CAL_URL]


def test_the_resume_point_belongs_to_one_account(monkeypatch):
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "150")
    server = _SlowServer(monkeypatch, [CAL_URL, _SlowServer.SECOND])

    assert server.pass_("alice", "acct-1")[0] == [CAL_URL]
    assert server.pass_("alice", "acct-2")[0] == [CAL_URL]  # its own account: starts at the top
    assert server.pass_("bob", "acct-1")[0] == [CAL_URL]  # another owner's: starts at the top
    assert server.pass_("alice", "acct-1")[0] == [_SlowServer.SECOND]  # the first one resumes


def test_a_failed_discovery_keeps_the_resume_point(monkeypatch):
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "150")
    server = _SlowServer(monkeypatch, [CAL_URL, _SlowServer.SECOND])
    assert server.pass_()[0] == [CAL_URL]

    def down(*args, **kwargs):
        raise OSError("server down")

    with monkeypatch.context() as outage:
        outage.setattr(_FakeClient, "principal", down)
        outage.setattr(_FakeClient, "calendar", down)
        reached, result = server.pass_()
    assert reached == [] and result["errors"]

    assert server.pass_()[0] == [_SlowServer.SECOND]


def test_an_unspent_budget_changes_nothing(monkeypatch):
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "150")
    _fake_clock(monkeypatch)
    old = datetime.utcnow() - timedelta(days=700)
    _Remote.resources = [_ics(_vevent(
        "fast@test", f"DTSTART:{old:%Y%m%dT%H%M%S}Z", "RRULE:FREQ=WEEKLY;COUNT=3", "SUMMARY:Fast",
    ))]
    assert _sync()["errors"] == []
    _Remote.resources = []
    result = _sync()
    assert result["errors"] == [] and result["deleted"] == 1


def test_override_without_master_is_a_single_event_row():
    """Invited to one instance of someone else's series: the resource holds only
    a RECURRENCE-ID VEVENT."""
    day = _tuesday(3)
    only = _vevent(
        "their-series@test",
        f"RECURRENCE-ID;TZID=America/New_York:{_tz(_ny(day, 15, 30))}",
        f"DTSTART;TZID=America/New_York:{_tz(_ny(day, 15, 30))}",
        f"DTEND;TZID=America/New_York:{_tz(_ny(day, 16, 30))}",
        "SUMMARY:Their series",
    )
    _Remote.resources = [_ics(only)]
    _sync()
    rows = _rows()
    assert list(rows) == [override_uid("their-series@test", recurrence_key(_utc_of(_ny(day, 15, 30)), False))]
    row = next(iter(rows.values()))
    assert row.series_uid == "their-series@test" and row.rrule == ""


def test_all_day_series_uses_date_keys():
    monday = (datetime.utcnow() - timedelta(days=30)).date()
    while monday.weekday() != 0:
        monday -= timedelta(days=1)
    skip = monday + timedelta(days=14)
    cancelled = monday + timedelta(days=7)
    master = _vevent(
        "allday@test",
        f"DTSTART;VALUE=DATE:{monday:%Y%m%d}",
        f"DTEND;VALUE=DATE:{monday + timedelta(days=1):%Y%m%d}",
        "RRULE:FREQ=WEEKLY;BYDAY=MO",
        f"EXDATE;VALUE=DATE:{skip:%Y%m%d}",
        "SUMMARY:Weekly all day",
    )
    gone = _vevent(
        "allday@test",
        f"RECURRENCE-ID;VALUE=DATE:{cancelled:%Y%m%d}",
        f"DTSTART;VALUE=DATE:{cancelled:%Y%m%d}",
        f"DTEND;VALUE=DATE:{cancelled + timedelta(days=1):%Y%m%d}",
        "STATUS:CANCELLED",
        "SUMMARY:Weekly all day",
    )
    _Remote.resources = [_ics(master, gone)]
    _sync()
    rows = _rows()
    m = rows["allday@test"]
    assert m.all_day is True and m.is_utc is False and m.tzid is None
    assert m.exdate == skip.isoformat()
    o = rows[override_uid("allday@test", cancelled.isoformat())]
    assert o.recurrence_id == cancelled.isoformat() and o.status == "cancelled"


def test_cancelled_single_event_is_stored_as_cancelled():
    day = _tuesday(2)
    ev = _vevent(
        "single@test",
        f"DTSTART:{day:%Y%m%d}T140000Z",
        f"DTEND:{day:%Y%m%d}T150000Z",
        "STATUS:CANCELLED",
        "SUMMARY:Called off",
    )
    _Remote.resources = [_ics(ev)]
    _sync()
    assert _rows()["single@test"].status == "cancelled"


def test_utc_series_has_no_zone():
    day = _tuesday(10)
    ev = _vevent(
        "utc-series@test",
        f"DTSTART:{day:%Y%m%d}T140000Z",
        f"DTEND:{day:%Y%m%d}T150000Z",
        "RRULE:FREQ=DAILY;COUNT=5",
        "SUMMARY:UTC series",
    )
    _Remote.resources = [_ics(ev)]
    _sync()
    row = _rows()["utc-series@test"]
    assert row.rrule == "FREQ=DAILY;COUNT=5" and row.is_utc is True and row.tzid is None


def test_one_unreadable_vevent_does_not_abort_or_prune(monkeypatch):
    ics, *_ = _weekly_with_exceptions()
    _Remote.resources = [ics]
    _sync()
    rows_before = set(_rows())

    real = caldav_sync._event_row_from_vevent
    calls = {"n": 0}

    def flaky(comp):
        calls["n"] += 1
        if calls["n"] == 2:
            raise ValueError("boom")
        return real(comp)

    monkeypatch.setattr(caldav_sync, "_event_row_from_vevent", flaky)
    result = _sync()
    assert any("VEVENT parse failed" in e for e in result["errors"])
    # The unreadable VEVENT's row is not pruned: seen_uids was incomplete.
    assert result["deleted"] == 0
    assert set(_rows()) == rows_before
