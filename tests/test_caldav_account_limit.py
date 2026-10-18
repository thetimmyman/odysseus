"""POST /api/calendar/config/accounts refuses an account past the limit.

An owner's CalDAV accounts are pulled one after another, each with a time budget
of its own, so how many an owner may keep bounds how long their pull can hold the
periodic sync loop (see ``src/caldav_sync_loop.py``). Calls the async route
handler directly with a minimal fake request, as ``test_caldav_writeback_route``
does, against an in-memory preference store; no network, no real preferences file.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes.calendar_routes as croutes
import src.caldav_sync as caldav_sync
import src.secret_storage as secrets
import src.user_preferences as prefs_mod


class _Request:
    def __init__(self, body):
        self.state = SimpleNamespace(current_user="tester")
        self._body = body

    async def json(self):
        return self._body


def _add_endpoint():
    router = croutes.setup_calendar_routes()
    for r in router.routes:
        if getattr(r, "path", "").endswith("/config/accounts") and "POST" in getattr(r, "methods", set()):
            return r.endpoint
    raise RuntimeError("POST */config/accounts not found")


@pytest.fixture
def store(monkeypatch):
    """An in-memory preference store for the one owner, holding no accounts."""
    prefs = {}

    monkeypatch.setattr(prefs_mod, "load_for_user", lambda owner: dict(prefs))
    monkeypatch.setattr(prefs_mod, "save_for_user", lambda owner, new: (prefs.clear(), prefs.update(new)))
    monkeypatch.setattr(caldav_sync, "validate_caldav_url", lambda url: url)
    monkeypatch.setattr(secrets, "encrypt", lambda value: "enc:" + value)
    monkeypatch.setattr(secrets, "decrypt", lambda value: value[4:] if value.startswith("enc:") else value)
    return prefs


def _body(n):
    return {"label": f"Acct {n}", "url": f"https://dav{n}.example.invalid/", "username": "u", "password": "p"}


async def test_accounts_can_be_added_up_to_the_limit_and_no_further(store):
    add = _add_endpoint()
    limit = caldav_sync.MAX_CALDAV_ACCOUNTS

    for n in range(limit):
        assert (await add(_Request(_body(n))))["ok"] is True
    assert len(store["caldav_accounts"]) == limit

    with pytest.raises(HTTPException) as refused:
        await add(_Request(_body(limit)))
    assert refused.value.status_code == 400
    assert str(limit) in refused.value.detail
    assert len(store["caldav_accounts"]) == limit, "a refused account must not be saved"


async def test_removing_an_account_makes_room_for_another(store):
    add = _add_endpoint()
    limit = caldav_sync.MAX_CALDAV_ACCOUNTS
    for n in range(limit):
        await add(_Request(_body(n)))

    router = croutes.setup_calendar_routes()
    delete = next(
        r.endpoint for r in router.routes
        if getattr(r, "path", "").endswith("/config/accounts/{account_id}") and "DELETE" in getattr(r, "methods", set())
    )
    await delete(store["caldav_accounts"][0]["id"], _Request({}))
    assert len(store["caldav_accounts"]) == limit - 1

    assert (await add(_Request(_body(limit))))["ok"] is True
    assert len(store["caldav_accounts"]) == limit


async def test_an_owner_already_over_the_limit_cannot_add_but_keeps_what_is_saved(store):
    limit = caldav_sync.MAX_CALDAV_ACCOUNTS
    store["caldav_accounts"] = [
        {"id": f"old-{n}", "label": "CalDAV", "url": "https://dav.example.invalid/", "username": "u", "password": "enc:p"}
        for n in range(limit + 4)
    ]
    with pytest.raises(HTTPException) as refused:
        await _add_endpoint()(_Request(_body(99)))
    assert refused.value.status_code == 400
    assert len(store["caldav_accounts"]) == limit + 4


async def test_validation_errors_still_come_before_the_limit(store):
    """A bad URL or a missing password is reported as before, not as 'too many'."""
    add = _add_endpoint()
    limit = caldav_sync.MAX_CALDAV_ACCOUNTS
    for n in range(limit):
        await add(_Request(_body(n)))

    with pytest.raises(HTTPException) as no_password:
        await add(_Request({"url": "https://dav.example.invalid/", "username": "u"}))
    assert no_password.value.detail == "Password is required"
