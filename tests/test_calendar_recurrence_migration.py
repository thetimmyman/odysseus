"""Startup migration that adds the recurring-series columns to calendar_events."""
import inspect
import sqlite3

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb

NEW_COLUMNS = ("exdate", "tzid", "series_uid", "recurrence_id")


def _columns(path):
    conn = sqlite3.connect(path)
    try:
        return [r[1] for r in conn.execute("PRAGMA table_info(calendar_events)")]
    finally:
        conn.close()


def _indexes(path):
    conn = sqlite3.connect(path)
    try:
        return {r[1] for r in conn.execute("PRAGMA index_list(calendar_events)")}
    finally:
        conn.close()


@pytest.fixture
def old_database(tmp_path, monkeypatch):
    """A database laid out as before this change: no series columns, one row."""
    path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{path}", poolclass=NullPool)
    cdb.Base.metadata.create_all(engine)
    engine.dispose()
    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX IF EXISTS ix_calendar_events_series_uid")
    for name in NEW_COLUMNS:
        conn.execute(f"ALTER TABLE calendar_events DROP COLUMN {name}")
    stamp = "2026-10-01 00:00:00"
    conn.execute(
        "INSERT INTO calendars (id, owner, name, source, created_at, updated_at) "
        "VALUES ('c1', 'alice', 'Family', 'caldav', ?, ?)", (stamp, stamp),
    )
    conn.execute(
        "INSERT INTO calendar_events (uid, calendar_id, summary, dtstart, dtend, is_utc, rrule, origin, "
        "created_at, updated_at) VALUES ('old-series', 'c1', 'Old series', '2026-10-13 19:30:00', "
        "'2026-10-13 20:30:00', 1, 'FREQ=WEEKLY;BYDAY=TU', 'caldav', ?, ?)", (stamp, stamp),
    )
    conn.commit()
    conn.close()
    assert not set(NEW_COLUMNS) & set(_columns(path))
    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{path}")
    return path


def test_migration_adds_the_columns_and_the_index(old_database):
    cdb._migrate_add_calendar_recurrence()
    assert set(NEW_COLUMNS) <= set(_columns(old_database))
    assert "ix_calendar_events_series_uid" in _indexes(old_database)


def test_migration_is_idempotent(old_database):
    cdb._migrate_add_calendar_recurrence()
    first = _columns(old_database)
    cdb._migrate_add_calendar_recurrence()
    assert _columns(old_database) == first
    assert first.count("exdate") == 1


def test_existing_rows_survive_and_read_back_through_the_model(old_database):
    cdb._migrate_add_calendar_recurrence()
    engine = create_engine(f"sqlite:///{old_database}", poolclass=NullPool)
    db = sessionmaker(bind=engine)()
    try:
        row = db.query(cdb.CalendarEvent).filter_by(uid="old-series").one()
        assert row.summary == "Old series" and row.rrule == "FREQ=WEEKLY;BYDAY=TU"
        assert (row.exdate or "") == ""
        assert row.tzid is None and row.series_uid is None and row.recurrence_id is None
    finally:
        db.close()
        engine.dispose()


def test_init_db_calls_the_migration():
    # init_db binds to the process-wide engine (the real data file), so it is
    # not run here; check that the startup path includes the migration, after
    # create_all so a fresh database gets the columns from the model instead.
    source = inspect.getsource(cdb.init_db)
    assert "_migrate_add_calendar_recurrence()" in source
    assert source.index("create_all") < source.index("_migrate_add_calendar_recurrence()")


def test_model_declares_the_same_columns_the_migration_adds(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}", poolclass=NullPool)
    cdb.Base.metadata.create_all(engine)
    engine.dispose()
    assert set(NEW_COLUMNS) <= set(_columns(tmp_path / "fresh.db"))
    assert "ix_calendar_events_series_uid" in _indexes(tmp_path / "fresh.db")


def test_missing_database_file_is_a_no_op(tmp_path, monkeypatch):
    path = tmp_path / "absent.db"
    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{path}")
    cdb._migrate_add_calendar_recurrence()
    assert not path.exists()


def test_database_without_the_table_is_left_alone(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    sqlite3.connect(path).close()
    monkeypatch.setattr(cdb, "DATABASE_URL", f"sqlite:///{path}")
    cdb._migrate_add_calendar_recurrence()
    assert _columns(path) == []
