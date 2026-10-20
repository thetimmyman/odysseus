"""TMOS-154: the agent's manage_research tool must honour the session owner.

Saved research sessions (``data/deep_research/<id>.json``) carry an ``owner``
field and the HTTP routes already answer 404 — not 403 — when it does not match
the caller. ``do_manage_research(content, owner)`` used to ignore its ``owner``
argument, so in a multi-user deployment any user's agent could read, delete
(including the file's blobs) and list another user's research.

These tests pin the same route rule on the tool for every action. Synthetic
owners and tmp dirs only: nothing here reads real user data.
"""
import asyncio
import json
import os
import time

import pytest

from src.tool_implementations import do_manage_research

ALICE = "rp-alice"
BOB = "rp-bob"
LEGACY = "rp-legacy"
MISSING = "rp-missing"

ALICE_QUERY = "alice private topic alpha"
BOB_QUERY = "bob private topic beta"
LEGACY_QUERY = "legacy topic gamma"

_MISSING_OWNER = object()  # sentinel: write the session without an owner key


def _session(root, session_id, query, owner=_MISSING_OWNER, *blob_names):
    """Write a session JSON shaped like the real one, with blob references."""
    data = {
        "query": query,
        "result": f"Report body for {query}",
        "sources": [],
        "completed_at": 1_000 + len(session_id),
        "run_record": {"sources": [
            {"source_id": f"S{i}", "blob": f"blobs/{name}"}
            for i, name in enumerate(blob_names, 1)
        ]},
    }
    if owner is not _MISSING_OWNER:
        data["owner"] = owner
    (root / f"{session_id}.json").write_text(json.dumps(data), encoding="utf-8")


def _blob(root, name, age=7200):
    """A source blob old enough (past the sweep grace window) to be reclaimed."""
    (root / "blobs").mkdir(exist_ok=True)
    path = root / "blobs" / name
    path.write_text("text", encoding="utf-8")
    old = time.time() - age
    os.utime(path, (old, old))
    return path


def _manage(content, owner=None):
    return asyncio.run(do_manage_research(json.dumps(content), owner=owner))


@pytest.fixture
def research_dir(tmp_path, monkeypatch):
    root = tmp_path / "deep_research"
    root.mkdir()
    monkeypatch.setattr("src.tool_implementations.DEEP_RESEARCH_DIR", str(root))
    return root


@pytest.fixture
def library(research_dir):
    """alice and bob each own one session and one blob; one owner-less legacy file."""
    _blob(research_dir, "alice.txt")
    _blob(research_dir, "bob.txt")
    _session(research_dir, ALICE, ALICE_QUERY, "alice", "alice.txt")
    _session(research_dir, BOB, BOB_QUERY, "bob", "bob.txt")
    _session(research_dir, LEGACY, LEGACY_QUERY)
    return research_dir



# --- read ----------------------------------------------------------------------

def test_reading_your_own_session_returns_the_report(library):
    res = _manage({"action": "read", "id": ALICE}, owner="alice")

    assert res.get("exit_code") == 0
    assert ALICE_QUERY in res["output"]
    assert BOB_QUERY not in res["output"]


@pytest.mark.parametrize("action", ["read", "open", "view", "get"])
def test_reading_another_owners_session_looks_exactly_like_a_missing_id(library, action):
    res = _manage({"action": action, "id": BOB}, owner="alice")
    missing = _manage({"action": action, "id": MISSING}, owner="alice")

    assert res == {"error": f"Research '{BOB}' not found."}
    assert missing == {"error": f"Research '{MISSING}' not found."}
    # Nothing from bob's file leaks through the error response.
    assert BOB_QUERY not in json.dumps(res)


def test_reading_a_legacy_session_is_only_for_the_empty_owner(library):
    assert LEGACY_QUERY in _manage({"action": "read", "id": LEGACY}, owner=None)["output"]
    assert LEGACY_QUERY in _manage({"action": "read", "id": LEGACY}, owner="")["output"]
    assert _manage({"action": "read", "id": LEGACY}, owner="alice") == {
        "error": f"Research '{LEGACY}' not found."}


# --- delete --------------------------------------------------------------------

def test_deleting_another_owners_session_changes_nothing(library):
    """Negative control: the pre-fix code unlinked the file and swept its blob."""
    alice_json = library / f"{ALICE}.json"
    alice_blob = library / "blobs" / "alice.txt"

    res = _manage({"action": "delete", "id": ALICE}, owner="bob")

    assert res == {"error": f"Research '{ALICE}' not found."}
    assert alice_json.exists() and alice_blob.exists()


def test_deleting_your_own_session_removes_the_file_and_its_blob(library):
    alice_json = library / f"{ALICE}.json"
    alice_blob = library / "blobs" / "alice.txt"
    bob_json = library / f"{BOB}.json"
    bob_blob = library / "blobs" / "bob.txt"

    res = _manage({"action": "delete", "id": ALICE}, owner="alice")

    assert res.get("exit_code") == 0
    assert not alice_json.exists() and not alice_blob.exists()
    assert bob_json.exists() and bob_blob.exists()


# --- list ----------------------------------------------------------------------

def test_list_shows_only_your_own_sessions(library):
    alice = _manage({"action": "list"}, owner="alice")
    bob = _manage({"action": "list"}, owner="bob")

    assert "(1 item)" in alice["output"]
    assert ALICE_QUERY in alice["output"]
    assert BOB_QUERY not in alice["output"] and LEGACY_QUERY not in alice["output"]

    assert "(1 item)" in bob["output"]
    assert BOB_QUERY in bob["output"]
    assert ALICE_QUERY not in bob["output"] and LEGACY_QUERY not in bob["output"]


def test_list_search_filters_inside_the_visible_set(library):
    own = _manage({"action": "list", "search": "alpha"}, owner="alice")
    other = _manage({"action": "list", "search": "beta"}, owner="alice")

    assert ALICE_QUERY in own["output"] and "(1 item)" in own["output"]
    assert other["output"].startswith("No research found in the library.")
    assert BOB_QUERY not in other["output"]


@pytest.mark.parametrize("owner", [None, ""])
def test_anonymous_callers_see_only_ownerless_sessions(library, owner):
    res = _manage({"action": "list"}, owner=owner)

    assert "(1 item)" in res["output"]
    assert LEGACY_QUERY in res["output"]
    assert ALICE_QUERY not in res["output"] and BOB_QUERY not in res["output"]


# --- malformed input -----------------------------------------------------------

@pytest.mark.parametrize("owner", [None, "", "alice"])
def test_a_corrupt_session_is_neither_readable_nor_deletable(research_dir, owner):
    corrupt = research_dir / "rp-broken.json"
    corrupt.write_text("{not json", encoding="utf-8")

    assert _manage({"action": "read", "id": "rp-broken"}, owner=owner) == {
        "error": "Research 'rp-broken' not found."}
    assert _manage({"action": "delete", "id": "rp-broken"}, owner=owner) == {
        "error": "Research 'rp-broken' not found."}
    assert corrupt.exists()


def test_invalid_id_is_still_rejected(library):
    assert _manage({"action": "read", "id": "../x"}, owner="alice") == {
        "error": "Invalid research id."}
    assert _manage({"action": "delete", "id": "../x"}, owner="alice") == {
        "error": "Invalid research id."}

