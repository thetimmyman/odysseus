"""End-to-end SessionManager isolation on a temp DB: persistence + reload
across two owners.

`sqlite:///:memory:` lives inside one DBAPI connection, so `core.database` must
use `StaticPool`; the default `SingletonThreadPool` gives each thread its own
empty database. `test_schema_is_visible_across_threads` guards that choice.
"""
import threading

import pytest
from sqlalchemy import inspect

import core.database as db
db.Base.metadata.create_all(bind=db.engine)

from core.session_manager import SessionManager
from core.models import ChatMessage


@pytest.fixture(autouse=True)
def _ensure_schema():
    """Create the schema on the engine `SessionLocal` is actually bound to.

    Resolving the bind from a live Session rather than from
    `core.database.engine` keeps this correct even if an earlier module
    re-points the global engine: whatever SessionManager is about to write
    through is what the tables get created on. `create_all` is idempotent and
    never drops, so the second test still reads the rows the first one wrote.
    """
    import core.session_manager as sm_mod

    session = sm_mod.SessionLocal()
    try:
        bind = session.get_bind()
        db.Base.metadata.create_all(bind=bind)
        assert "sessions" in inspect(bind).get_table_names(), (
            "schema missing from the bind SessionManager writes through — "
            "check the connection pool for in-memory SQLite (see module docstring)"
        )
        yield
    finally:
        session.close()


def _fresh_mgr():
    return SessionManager()


def test_schema_is_visible_across_threads():
    """Guards the in-memory SQLite pool choice.

    With SingletonThreadPool (the default for `sqlite:///:memory:`) each thread
    gets its own empty database and this fails. The app is multi-threaded —
    FastAPI runs sync endpoints in a threadpool — so this is a real property,
    not a test-only concern.
    """
    import core.session_manager as sm_mod

    session = sm_mod.SessionLocal()
    try:
        bind = session.get_bind()
    finally:
        session.close()

    seen = {}

    def _probe():
        seen["tables"] = "sessions" in inspect(bind).get_table_names()

    t = threading.Thread(target=_probe)
    t.start()
    t.join()

    assert seen["tables"], (
        "the sessions table is invisible from another thread — in-memory SQLite "
        "must use StaticPool, not SingletonThreadPool"
    )


def test_persist_and_ram_isolation():
    sm = _fresh_mgr()
    sm.create_session("chatA", "A", "http://x/v1", "m", owner="alice")
    sm.create_session("chatB", "B", "http://x/v1", "m", owner="bob")
    sm.add_message("chatA", ChatMessage("user", "alice-private"))
    sm.add_message("chatB", ChatMessage("user", "bob-private"))
    assert [m.content for m in sm.get_session("chatA").history] == ["alice-private"]
    assert [m.content for m in sm.get_session("chatB").history] == ["bob-private"]


def test_reload_from_db_keeps_sessions_isolated():
    # brand-new manager -> forces a DB hydrate, not the RAM cache
    sm2 = _fresh_mgr()
    a = sm2.get_session("chatA")
    b = sm2.get_session("chatB")
    a_ctx = [m["content"] for m in a.get_context_messages()]
    b_ctx = [m["content"] for m in b.get_context_messages()]
    assert a_ctx == ["alice-private"], f"chatA leaked/lost: {a_ctx}"
    assert b_ctx == ["bob-private"], f"chatB leaked/lost: {b_ctx}"
    assert all("bob" not in c for c in a_ctx), "bob's msg leaked into alice's chat"
    assert all("alice" not in c for c in b_ctx), "alice's msg leaked into bob's chat"
