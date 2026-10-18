"""Token (``ody_``) calendar routes: sync, list calendars, update an event.

The codex router wraps the cookie-session calendar routes, so these tests build
both real routers and check the scope gate, the owner scoping and the wiring.
Synthetic owners and events; the CalDAV pull itself is replaced by a recorder.
"""
import asyncio
import importlib
import tempfile
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from core.database import CalendarCal, CalendarEvent

_TMPDB = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_ENGINE = create_engine(
    f"sqlite:///{_TMPDB.name}", connect_args={"check_same_thread": False}, poolclass=NullPool
)
cdb.Base.metadata.create_all(_ENGINE)
_TS = sessionmaker(bind=_ENGINE, autoflush=False, autocommit=False)


@pytest.fixture
def routers(monkeypatch):
    cal_mod = importlib.import_module("routes.calendar_routes")
    from routes.codex_routes import setup_codex_routes

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(cal_mod, "SessionLocal", _TS)
    # Other test modules swap fakes into these module globals; pin the real models.
    monkeypatch.setattr(cal_mod, "CalendarEvent", CalendarEvent)
    monkeypatch.setattr(cal_mod, "CalendarCal", CalendarCal)
    db = _TS()
    try:
        db.query(CalendarEvent).delete()
        db.query(CalendarCal).delete()
        db.add(CalendarCal(id="cal-a", owner="alice", name="Alice cal", source="local", color="#112233"))
        db.add(CalendarCal(id="cal-b", owner="bob", name="Bob cal", source="local", color="#445566"))
        db.add(CalendarEvent(
            uid="ev-a", calendar_id="cal-a", summary="Alice event", origin="local",
            dtstart=datetime(2026, 11, 4, 14), dtend=datetime(2026, 11, 4, 15),
        ))
        db.commit()
    finally:
        db.close()
    calendar_router = cal_mod.setup_calendar_routes()
    codex = setup_codex_routes(calendar_router=calendar_router)
    return SimpleNamespace(calendar=calendar_router, codex=codex)


def _endpoint(router, method, path):
    return next(r.endpoint for r in router.routes if r.path == path and method in r.methods)


def _token_request(scopes, owner="alice"):
    return SimpleNamespace(
        state=SimpleNamespace(
            current_user="api", api_token=True, api_token_owner=owner, api_token_scopes=list(scopes),
        ),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=SimpleNamespace(is_configured=True))),
        client=SimpleNamespace(host="203.0.113.10"),
    )


@pytest.fixture
def pulls(monkeypatch):
    import src.caldav_sync as caldav_sync

    calls = []

    async def fake_sync(owner):
        calls.append(owner)
        return {"calendars": 2, "events": 5, "deleted": 1, "errors": []}

    monkeypatch.setattr(caldav_sync, "sync_caldav", fake_sync)
    return calls


# ── wiring ────────────────────────────────────────────────────────────────────

def test_the_three_routes_are_registered_with_the_expected_methods(routers):
    paths = {(m, r.path) for r in routers.codex.routes for m in r.methods}
    assert ("POST", "/api/codex/calendar/sync") in paths
    assert ("GET", "/api/codex/calendar/calendars") in paths
    assert ("PUT", "/api/codex/calendar/events/{uid}") in paths


def test_capabilities_list_the_new_calendar_actions(routers):
    caps = _endpoint(routers.codex, "GET", "/api/codex/capabilities")
    out = caps(_token_request({"calendar:write"}))
    actions = out["tools"]["calendar"]["actions"]
    assert {"update_event", "list_calendars", "sync"} <= set(actions)
    assert out["tools"]["calendar"]["write"] is True


# ── sync ──────────────────────────────────────────────────────────────────────

def test_sync_runs_for_the_token_owner_and_returns_the_counts(routers, pulls):
    sync = _endpoint(routers.codex, "POST", "/api/codex/calendar/sync")
    request = _token_request({"calendar:write"}, owner="alice")
    out = asyncio.run(sync(request))
    assert out == {"calendars": 2, "events": 5, "deleted": 1, "errors": []}
    assert pulls == ["alice"]
    # The bearer-token markers are put back once the nested handler returns.
    assert request.state.current_user == "api" and request.state.api_token is True


@pytest.mark.parametrize("scopes", [["calendar:read"], ["todos:write", "memory:write"], []])
def test_sync_needs_the_calendar_write_scope(routers, pulls, scopes):
    sync = _endpoint(routers.codex, "POST", "/api/codex/calendar/sync")
    with pytest.raises(HTTPException) as err:
        asyncio.run(sync(_token_request(scopes)))
    assert err.value.status_code == 403
    assert pulls == []


def test_sync_refuses_a_token_without_an_owner(routers, pulls):
    sync = _endpoint(routers.codex, "POST", "/api/codex/calendar/sync")
    with pytest.raises(HTTPException) as err:
        asyncio.run(sync(_token_request({"calendar:write"}, owner=None)))
    assert err.value.status_code == 403
    assert pulls == []


def test_sync_is_unavailable_without_a_calendar_router(pulls):
    from routes.codex_routes import setup_codex_routes

    sync = _endpoint(setup_codex_routes(), "POST", "/api/codex/calendar/sync")
    with pytest.raises(HTTPException) as err:
        asyncio.run(sync(_token_request({"calendar:write"})))
    assert err.value.status_code == 503
    assert pulls == []


# ── list calendars ────────────────────────────────────────────────────────────

def test_calendars_lists_only_the_token_owners(routers):
    listing = _endpoint(routers.codex, "GET", "/api/codex/calendar/calendars")
    out = asyncio.run(listing(_token_request({"calendar:read"}, owner="alice")))
    hrefs = {c["href"] for c in out["calendars"]}
    assert "cal-a" in hrefs and "cal-b" not in hrefs
    assert all(set(c) == {"name", "href", "color", "source"} for c in out["calendars"])

    out_bob = asyncio.run(listing(_token_request({"calendar:read"}, owner="bob")))
    assert {c["href"] for c in out_bob["calendars"]} >= {"cal-b"}
    assert "cal-a" not in {c["href"] for c in out_bob["calendars"]}


def test_a_write_token_may_also_list_calendars(routers):
    listing = _endpoint(routers.codex, "GET", "/api/codex/calendar/calendars")
    assert asyncio.run(listing(_token_request({"calendar:write"})))["calendars"]


def test_calendars_needs_a_calendar_scope(routers):
    listing = _endpoint(routers.codex, "GET", "/api/codex/calendar/calendars")
    with pytest.raises(HTTPException) as err:
        asyncio.run(listing(_token_request({"documents:read"})))
    assert err.value.status_code == 403


def _owner_rows(owner):
    db = _TS()
    try:
        return [(c.name, c.source) for c in db.query(CalendarCal).filter_by(owner=owner).all()]
    finally:
        db.close()


def test_a_read_token_lists_without_creating_the_default_calendar(routers):
    """A calendar:read token must stay side-effect free: an owner with no calendar
    gets an empty list and no row is written."""
    listing = _endpoint(routers.codex, "GET", "/api/codex/calendar/calendars")
    assert _owner_rows("carol") == []
    out = asyncio.run(listing(_token_request({"calendar:read"}, owner="carol")))
    assert out == {"calendars": []}
    assert _owner_rows("carol") == []


def test_a_write_token_gets_the_default_calendar_made_on_first_list(routers):
    listing = _endpoint(routers.codex, "GET", "/api/codex/calendar/calendars")
    out = asyncio.run(listing(_token_request({"calendar:write"}, owner="dave")))
    assert [c["name"] for c in out["calendars"]] == ["Personal"]
    assert _owner_rows("dave") == [("Personal", "local")]


def test_a_cookie_session_listing_still_creates_the_default_calendar(routers):
    listing = _endpoint(routers.codex, "GET", "/api/codex/calendar/calendars")
    request = SimpleNamespace(
        state=SimpleNamespace(current_user="erin"),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=SimpleNamespace(is_configured=True))),
        client=SimpleNamespace(host="203.0.113.10"),
    )
    out = asyncio.run(listing(request))
    assert [c["name"] for c in out["calendars"]] == ["Personal"]
    assert _owner_rows("erin") == [("Personal", "local")]


def test_a_read_token_after_the_default_exists_sees_it(routers):
    listing = _endpoint(routers.codex, "GET", "/api/codex/calendar/calendars")
    asyncio.run(listing(_token_request({"calendar:write"}, owner="dave")))
    out = asyncio.run(listing(_token_request({"calendar:read"}, owner="dave")))
    assert [c["name"] for c in out["calendars"]] == ["Personal"]
    assert len(_owner_rows("dave")) == 1


def test_calendars_is_unavailable_without_a_calendar_router():
    from routes.codex_routes import setup_codex_routes

    listing = _endpoint(setup_codex_routes(), "GET", "/api/codex/calendar/calendars")
    with pytest.raises(HTTPException) as err:
        asyncio.run(listing(_token_request({"calendar:read"})))
    assert err.value.status_code == 503


# ── update ────────────────────────────────────────────────────────────────────

def _summary(uid="ev-a"):
    db = _TS()
    try:
        return db.query(CalendarEvent).filter_by(uid=uid).one().summary
    finally:
        db.close()


def test_update_changes_the_owners_event(routers):
    update = _endpoint(routers.codex, "PUT", "/api/codex/calendar/events/{uid}")
    out = asyncio.run(update(_token_request({"calendar:write"}), "ev-a", {"summary": "Renamed"}))
    assert out == {"ok": True}
    assert _summary() == "Renamed"


def _synced_series(**fields):
    db = _TS()
    try:
        db.add(CalendarCal(id="cal-dav", owner="alice", name="Synced", source="caldav"))
        db.add(CalendarEvent(
            uid="series-s@test", calendar_id="cal-dav", summary="Series", origin="caldav",
            dtstart=datetime(2026, 11, 4, 19, 30), dtend=datetime(2026, 11, 4, 20, 30),
            is_utc=True, rrule="FREQ=WEEKLY", **fields,
        ))
        db.commit()
    finally:
        db.close()


def _rrule_of(uid):
    db = _TS()
    try:
        return db.query(CalendarEvent).filter_by(uid=uid).one().rrule
    finally:
        db.close()


@pytest.mark.parametrize("fields", [{"tzid": "America/New_York"}, {"exdate": "20261111T193000Z"}])
def test_clearing_the_rule_of_a_synced_series_through_a_token_stays_local(routers, monkeypatch, fields):
    """The write-back would replace the remote resource with one UTC, non-recurring
    event and lose its zone and exclusions, so a token edit is held back as well."""
    import src.caldav_writeback as writeback

    pushed = []

    async def record(*args, **kwargs):
        pushed.append((args, kwargs))

    monkeypatch.setattr(writeback, "writeback_event", record)
    _synced_series(**fields)
    update = _endpoint(routers.codex, "PUT", "/api/codex/calendar/events/{uid}")

    out = asyncio.run(update(_token_request({"calendar:write"}), "series-s@test", {"rrule": ""}))

    assert out["writeback"] == "skipped"
    assert pushed == []
    assert _rrule_of("series-s@test") == ""


def test_clearing_the_rule_of_a_plain_synced_event_is_still_pushed(routers, monkeypatch):
    """Control for the test above: with nothing a push would lose, it goes through."""
    import src.caldav_writeback as writeback

    pushed = []

    async def record(owner, source, cal_id, payload, delete=False):
        pushed.append(payload)

    monkeypatch.setattr(writeback, "writeback_event", record)
    _synced_series()
    update = _endpoint(routers.codex, "PUT", "/api/codex/calendar/events/{uid}")

    out = asyncio.run(update(_token_request({"calendar:write"}), "series-s@test", {"rrule": ""}))

    assert out == {"ok": True}
    assert [p["rrule"] for p in pushed] == [""]


def test_update_needs_the_calendar_write_scope(routers):
    update = _endpoint(routers.codex, "PUT", "/api/codex/calendar/events/{uid}")
    with pytest.raises(HTTPException) as err:
        asyncio.run(update(_token_request({"calendar:read"}), "ev-a", {"summary": "Nope"}))
    assert err.value.status_code == 403
    assert _summary() == "Alice event"


def test_update_cannot_reach_another_owners_event(routers):
    update = _endpoint(routers.codex, "PUT", "/api/codex/calendar/events/{uid}")
    with pytest.raises(HTTPException) as err:
        asyncio.run(update(_token_request({"calendar:write"}, owner="bob"), "ev-a", {"summary": "Mine now"}))
    assert err.value.status_code == 404
    assert _summary() == "Alice event"


def test_update_rejects_a_malformed_payload(routers):
    update = _endpoint(routers.codex, "PUT", "/api/codex/calendar/events/{uid}")
    with pytest.raises(HTTPException) as err:
        asyncio.run(update(_token_request({"calendar:write"}), "ev-a", {"summary": ["not", "text"]}))
    assert err.value.status_code == 400
    assert _summary() == "Alice event"


def test_update_is_unavailable_without_a_calendar_router():
    from routes.codex_routes import setup_codex_routes

    update = _endpoint(setup_codex_routes(), "PUT", "/api/codex/calendar/events/{uid}")
    with pytest.raises(HTTPException) as err:
        asyncio.run(update(_token_request({"calendar:write"}), "ev-a", {"summary": "x"}))
    assert err.value.status_code == 503
