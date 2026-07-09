"""Verifies the fork's #4335 port: MCP email owner-scoping in
mcp_servers/email_server.py. Focused unit test (monkeypatches the DB reader)
covering both directions — isolation AND legacy/single-user pass-through."""
import pytest
import mcp_servers.email_server as es

ROWS = [
    {"id": "a", "owner": "alice", "name": "A", "imap_user": "alice@x", "from_address": "alice@x"},
    {"id": "b", "owner": "bob",   "name": "B", "imap_user": "bob@x",   "from_address": "bob@x"},
    {"id": "c", "owner": "",      "name": "Legacy", "imap_user": "legacy@x", "from_address": "legacy@x"},
]


def test_account_visibility():
    assert es._account_visible_to_owner(ROWS[0], "alice") is True
    assert es._account_visible_to_owner(ROWS[1], "alice") is False          # bob's, hidden from alice
    assert es._account_visible_to_owner(ROWS[2], "legacy@x") is True        # ownerless visible on mailbox match
    assert es._account_visible_to_owner(ROWS[2], "alice") is False          # ownerless hidden otherwise


def test_filter_scoped_to_owner():
    tok = es._CURRENT_OWNER.set("alice")
    try:
        assert {r["id"] for r in es._filter_accounts_for_owner(ROWS)} == {"a"}
    finally:
        es._CURRENT_OWNER.reset(tok)


def test_filter_unscoped_multiowner_fails_closed():
    # no current owner + multiple distinct owners -> return nothing (fail closed)
    assert es._filter_accounts_for_owner(ROWS) == []


def test_filter_unscoped_single_or_legacy_passthrough():
    single = [ROWS[2]]  # only an ownerless legacy account -> legacy single-user mode still works
    assert es._filter_accounts_for_owner(single) == single


def test_mcp_owner_required():
    assert es._mcp_owner_required(ROWS) is True                 # multi-owner + no owner -> required
    tok = es._CURRENT_OWNER.set("alice")
    try:
        assert es._mcp_owner_required(ROWS) is False            # owner present -> not required
    finally:
        es._CURRENT_OWNER.reset(tok)
    assert es._mcp_owner_required([ROWS[2]]) is False           # single/legacy -> not required


@pytest.mark.asyncio
async def test_call_tool_extracts_owner_and_scopes_list(monkeypatch):
    monkeypatch.setattr(es, "_read_accounts_from_db", lambda: list(ROWS))
    # alice asks to list accounts, owner threaded via _odysseus_owner -> sees only her own
    out = await es.call_tool("list_email_accounts", {"_odysseus_owner": "alice"})
    text = out[0].text
    assert "alice@x" in text and "bob@x" not in text and "legacy@x" not in text
    # ContextVar must be reset after the call (no leak to the next caller)
    assert es._CURRENT_OWNER.get() is None


@pytest.mark.asyncio
async def test_call_tool_requires_owner_when_ambiguous(monkeypatch):
    monkeypatch.setattr(es, "_read_accounts_from_db", lambda: list(ROWS))
    out = await es.call_tool("list_email_accounts", {})   # no owner, multiple owners -> fail closed
    assert "requires an authenticated owner" in out[0].text
