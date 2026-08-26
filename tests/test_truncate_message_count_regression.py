"""Regression: truncate_messages must not set message_count above the real
number of messages when keep_count exceeds the message total.

The AI tool layer (src/ai_interaction.py manage_session action='truncate')
defaults keep_count=10, so a short session (say 3 messages) gets truncated
with keep_count=10. The DB has only 3 rows left, but truncate_messages used to
write db_session.message_count = keep_count (=10), leaving the persisted count
inconsistent with the actual rows. get_session relies on message_count>0 to
decide whether to lazily hydrate from the DB, so an inflated count is a latent
correctness hazard.
"""
import importlib
import os
import tempfile

import pytest


@pytest.fixture
def temp_db_manager():
    """A SessionManager bound to a throwaway file DB — with the global state restored.

    This rebinds process-wide state: it sets ``os.environ["DATABASE_URL"]`` and
    ``importlib.reload``s both ``core.database`` (new ``engine`` + ``SessionLocal``)
    and ``core.session_manager`` (which holds its own ``SessionLocal`` reference
    from import time). Leaving that in place leaks into every module that runs
    afterwards, because the rest of the suite shares this interpreter — later
    tests would silently read and write this temp file instead of the in-memory
    DB conftest configured.

    Restoring on teardown is what keeps the suite order-independent.
    """
    import core.database as database
    import core.session_manager as sm_mod

    original_url = os.environ.get("DATABASE_URL")

    db_fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(db_fd)
    os.environ["DATABASE_URL"] = f"sqlite:///{db_path}"

    # Reload after DATABASE_URL is set so the engine binds to the temp DB.
    importlib.reload(database)
    database.Base.metadata.create_all(bind=database.engine)
    importlib.reload(sm_mod)

    try:
        yield sm_mod.SessionManager(), database, sm_mod
    finally:
        if original_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_url
        # Re-point the shared modules back at the original database.
        importlib.reload(database)
        importlib.reload(sm_mod)
        try:
            os.unlink(db_path)
        except OSError:
            pass


def test_truncate_keep_count_exceeds_total_does_not_inflate_count(temp_db_manager):
    from core.models import ChatMessage

    sm, database, sm_mod = temp_db_manager
    sid = "short-session"
    sm.create_session(session_id=sid, name="t", endpoint_url="x",
                      model="m", rag=False, owner="u")
    for i in range(3):
        sm.add_message(sid, ChatMessage("user", f"msg{i}"))

    # AI default keep_count is 10 — larger than the 3 real messages.
    assert sm.truncate_messages(sid, 10) is True

    db = database.SessionLocal()
    try:
        DbSession = database.Session
        DbChatMessage = database.ChatMessage
        rows = db.query(DbChatMessage).filter(
            DbChatMessage.session_id == sid).count()
        db_session = db.query(DbSession).filter(DbSession.id == sid).first()
        # Nothing should have been deleted (only 3 messages exist).
        assert rows == 3
        # message_count must reflect the real number of rows, not keep_count.
        assert db_session.message_count == 3, (
            f"message_count={db_session.message_count} but only {rows} rows exist"
        )
    finally:
        db.close()
