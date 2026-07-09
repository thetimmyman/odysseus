"""Probe #2: end-to-end SessionManager isolation on a temp DB — persistence
+ reload across two owners. Covers the path the RAM-only probe didn't."""
import core.database as db
db.Base.metadata.create_all(bind=db.engine)

from core.session_manager import SessionManager
from core.models import ChatMessage


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
