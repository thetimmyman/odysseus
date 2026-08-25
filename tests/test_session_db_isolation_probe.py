"""Probe #2: end-to-end SessionManager isolation on a temp DB — persistence
+ reload across two owners. Covers the path the RAM-only probe didn't."""
import pytest

import core.database as db
db.Base.metadata.create_all(bind=db.engine)

from core.session_manager import SessionManager
from core.models import ChatMessage


@pytest.fixture(autouse=True)
def _ensure_schema():
    """Create the schema on the engine `SessionLocal` is actually bound to.

    conftest points DATABASE_URL at `sqlite:///:memory:`. `SessionLocal` is
    bound to an engine once, at `core.database` import; an earlier test module
    in a full-suite run can rebind or dispose `core.database.engine`, after
    which `create_all(bind=db.engine)` targets a *different* engine than the
    one SessionManager writes through — so the tables land in the wrong
    database and the test dies on "no such table: sessions".

    Resolving the bind from a live Session removes the guesswork: whatever
    SessionManager is about to use is what we create the tables on. Keeping the
    Session open until after `create_all` matters too — returning the only
    connection to the pool is what discards an in-memory database.

    `create_all` is idempotent and never drops, so the second test still reads
    the rows the first one wrote.

    Latent until now because the suite could not run end-to-end: pytest was
    aborting during collection on an unpinned mcp 2.x. Running this file alone
    always passed, which is why it was never caught.
    """
    import core.session_manager as sm_mod

    # Bind to SessionManager's OWN SessionLocal, not core.database's. It does
    # `from .database import ... SessionLocal` at import, so it holds a fixed
    # reference; rebinding core.database.SessionLocal later does not follow.
    # Creating tables via core.database can therefore target a different engine
    # than the code under test writes through.
    session = sm_mod.SessionLocal()
    try:
        db.Base.metadata.create_all(bind=session.get_bind())
        yield
    finally:
        session.close()


def _fresh_mgr():
    return SessionManager()


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
