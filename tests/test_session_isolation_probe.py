"""Probe: does the fork leak messages between chats (#135/#267)?
Tests RAM-level isolation of the fork's real Session model — no DB needed
(Session.add_message only persists when a manager is set)."""
from core.models import Session, ChatMessage


def _s(sid):
    return Session(id=sid, name=sid, endpoint_url="http://x/v1", model="m")


def test_distinct_sessions_have_distinct_history_lists():
    a, b = _s("a"), _s("b")
    assert a.history is not b.history, "two sessions SHARE one history list -> LEAK"


def test_message_added_to_A_does_not_appear_in_B():
    a, b = _s("a"), _s("b")
    a.add_message(ChatMessage("user", "secret-from-A"))
    assert b.history == [], "B saw A's message -> cross-chat LEAK"
    assert all("secret-from-A" not in m["content"] for m in b.get_context_messages())
    assert [m["content"] for m in a.get_context_messages()] == ["secret-from-A"]


def test_many_sessions_no_shared_list_identity():
    sessions = [_s(f"s{i}") for i in range(20)]
    ids = {id(s.history) for s in sessions}
    assert len(ids) == 20, "some sessions alias the same history list -> LEAK"


def test_history_slicing_reassignment_isolated():
    # mirrors chat_handler.py:295 / session_manager.py:287 truncation pattern
    a, b = _s("a"), _s("b")
    for i in range(5):
        a.add_message(ChatMessage("user", f"a{i}"))
        b.add_message(ChatMessage("user", f"b{i}"))
    a.history = a.history[-2:]          # truncate A
    assert [m.content for m in b.history] == [f"b{i}" for i in range(5)], "truncating A disturbed B"
    assert all(m.content.startswith("a") for m in a.history)
