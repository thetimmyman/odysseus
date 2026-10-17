"""Observable ownership and one-use dispatch contracts on isolated file SQLite."""
import math
import pickle
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from core.auth import AuthManager
from src.canonical_inspector_admission import (
    AdmissionStore, InspectorUnavailable, PreparedPilot, Principal,
    Publication, UnknownInspection, metadata,
)


@pytest.fixture
def native_store(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "native.db"),
                           connect_args={"check_same_thread": False, "timeout": 5})

    @event.listens_for(engine, "connect")
    def qualify_fixture(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
        dbapi_connection.execute("PRAGMA synchronous=FULL")

    metadata.create_all(engine)
    store = AdmissionStore(sessionmaker(bind=engine, expire_on_commit=False))
    yield store, engine
    engine.dispose()


def prepared(number=1):
    return PreparedPilot(run_id=f"run-{number}", packet_id="synthetic-uppercase",
                         package_hash=str(number) * 64, config_digest="c" * 64)


def principal(name="admin", created=1.5):
    return Principal(name, "float64_hex:" + created.hex())


def publication(number=1):
    return Publication(bundle_hash="b" * 64, evidence_hash="e" * 64,
                       attempt_hash=str(number) * 64, dispatch_hash="d" * 64,
                       disclosure_generation="public-uppercase-v1")


@pytest.mark.parametrize("value", [True, 1, "1.5", None, math.inf, math.nan, 0.0, -1.0])
def test_account_snapshot_refuses_noncanonical_epoch(tmp_path, monkeypatch, value):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    manager = AuthManager(str(tmp_path / "auth.json"))
    manager._config = {"users": {"admin": {"is_admin": True, "created": value}}}
    assert manager.snapshot_admin_account_epoch("admin") is None


def test_snapshot_preserves_exact_key_and_float_bits(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    manager = AuthManager(str(tmp_path / "auth.json"))
    first, adjacent = 1.5, math.nextafter(1.5, math.inf)
    manager._config = {"users": {"Admin": {"is_admin": True, "created": first},
                                 "admin": {"is_admin": True, "created": adjacent}}}
    assert manager.snapshot_admin_account_epoch(" Admin") is None
    assert manager.snapshot_admin_account_epoch("Admin") == ("Admin", "float64_hex:" + first.hex())
    assert manager.snapshot_admin_account_epoch("admin") != manager.snapshot_admin_account_epoch("Admin")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    assert manager.snapshot_admin_account_epoch("admin") is None


def test_committed_claim_is_one_use_and_not_serializable(native_store):
    store, _ = native_store
    claim = store.admit(principal(), "one", prepared())
    with pytest.raises(TypeError):
        pickle.dumps(claim)
    assert store.consume(claim, principal()).run_id == "run-1"
    with pytest.raises(InspectorUnavailable):
        store.consume(claim, principal())
    with pytest.raises(InspectorUnavailable):
        store.admit(principal(), "one", prepared())


def test_duplicate_and_global_slot_contention_have_one_winner(native_store):
    store, _ = native_store

    def attempt(index):
        try:
            return store.admit(principal(str(index)), str(index), prepared(index + 1))
        except InspectorUnavailable:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(attempt, range(4)))
    assert sum(claim is not None for claim in claims) == 1


def test_terminal_release_does_not_recreate_old_dispatch_authority(native_store):
    store, _ = native_store
    owner = principal()
    claim = store.admit(owner, "one", prepared())
    store.consume(claim, owner)
    with pytest.raises(InspectorUnavailable):
        store.release_terminal(claim, cleanup_confirmed=False)
    store.release_terminal(claim, cleanup_confirmed=True)
    with pytest.raises(InspectorUnavailable):
        store.admit(owner, "one", prepared())
    new_claim = store.admit(owner, "two", prepared(2))
    assert store.consume(new_claim, owner).run_id == "run-2"


def test_publication_owner_revocation_and_correction_are_immutable(native_store):
    store, engine = native_store
    owner = principal()
    claim = store.admit(owner, "one", prepared())
    store.consume(claim, owner)
    handle = store.publish(claim, owner, publication())
    for foreign in (principal("other"), principal(created=2.0)):
        with pytest.raises(UnknownInspection):
            store.resolve(foreign, handle, "public-uppercase-v1")
        with pytest.raises(UnknownInspection):
            store.revoke(foreign, handle)
    assert store.resolve(owner, handle, "public-uppercase-v1").attempt_hash == "1" * 64
    replacement = store.correct(owner, handle, publication())
    with pytest.raises(UnknownInspection):
        store.resolve(owner, handle, "public-uppercase-v1")
    assert replacement != handle
    assert store.resolve(owner, replacement, "public-uppercase-v1").attempt_hash == "1" * 64
    with engine.begin() as connection:
        with pytest.raises(Exception, match="immutable"):
            connection.execute(text("UPDATE canonical_inspector_admission SET principal_key='other'"))
        with pytest.raises(Exception, match="append-only"):
            connection.execute(text("DELETE FROM canonical_inspector_event"))
    store.revoke(owner, replacement)
    with pytest.raises(UnknownInspection):
        store.resolve(owner, replacement, "public-uppercase-v1")


def test_missing_schema_and_unqualified_sqlite_never_mint_claim(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "unqualified.db"))
    store = AdmissionStore(sessionmaker(bind=engine))
    with pytest.raises(InspectorUnavailable):
        store.admit(principal(), "one", prepared())
    assert inspect(engine).get_table_names() == []
    engine.dispose()


def test_uncertain_commit_never_returns_claim_or_permits_retry(native_store):
    store, engine = native_store

    class PersistThenFail(Session):
        def commit(self):
            super().commit()
            raise OSError("commit acknowledgement lost")

    uncertain = AdmissionStore(sessionmaker(bind=engine, class_=PersistThenFail))
    with pytest.raises(InspectorUnavailable):
        uncertain.admit(principal(), "one", prepared())
    with pytest.raises(InspectorUnavailable):
        store.admit(principal(), "one", prepared())
    with pytest.raises(InspectorUnavailable):
        store.admit(principal("other"), "new", prepared(2))
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM canonical_inspector_admission")).scalar() == 1
        assert connection.execute(text("SELECT count(*) FROM canonical_inspector_event")).scalar() == 0


def test_same_key_race_mints_one_claim_and_crashed_creator_never_replays(native_store):
    store, _ = native_store

    def admit_once(_):
        try:
            return store.admit(principal(), "same", prepared())
        except InspectorUnavailable:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(admit_once, range(4)))
    assert sum(result is not None for result in results) == 1
    # Simulate a new process/store after the creator disappeared before work.
    restarted = AdmissionStore(store._sessions)
    with pytest.raises(InspectorUnavailable):
        restarted.admit(principal(), "same", prepared())
    with pytest.raises(InspectorUnavailable):
        restarted.consume(next(result for result in results if result), principal())


def test_raw_sql_cannot_substitute_event_actor_run_or_revoke_another_origin(native_store):
    from sqlalchemy import insert, select
    from src.canonical_inspector_admission import events

    store, engine = native_store
    owner = principal()
    claim = store.admit(owner, "one", prepared())
    store.consume(claim, owner)
    handle = store.publish(claim, owner, publication())
    with engine.connect() as connection:
        original = dict(connection.execute(select(events)).mappings().one())
    for change in ({"actor_key": "other"}, {"actor_epoch": principal(created=2.0).account_epoch},
                   {"run_id": "another-run"}):
        forged = {**original, **change, "id": "new-event", "handle": "new-handle"}
        with engine.begin() as connection:
            with pytest.raises(Exception):
                connection.execute(insert(events).values(**forged))
    store.revoke(owner, handle)
    with pytest.raises(UnknownInspection):
        store.correct(owner, handle, publication())


@pytest.mark.parametrize("setting", ["PRAGMA foreign_keys=OFF", "PRAGMA synchronous=NORMAL"])
def test_unqualified_connection_controls_refuse_without_new_row(native_store, setting):
    store, engine = native_store
    with engine.connect() as connection:
        connection.exec_driver_sql(setting)
    with pytest.raises(InspectorUnavailable):
        store.admit(principal(), "one", prepared())
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM canonical_inspector_admission")).scalar() == 0


def test_raw_replace_cannot_rewrite_committed_origin_or_publication(native_store):
    from sqlalchemy import insert, select
    from src.canonical_inspector_admission import admissions, events

    store, engine = native_store
    owner = principal()
    claim = store.admit(owner, "one", prepared())
    store.consume(claim, owner)
    handle = store.publish(claim, owner, publication())
    for table, substitution in ((admissions, {"principal_key": "other"}),
                                 (events, {"bundle_hash": "f" * 64})):
        with engine.connect() as connection:
            original = dict(connection.execute(select(table)).mappings().one())
        with engine.begin() as connection:
            with pytest.raises(Exception):
                connection.execute(insert(table).prefix_with("OR REPLACE").values(**{**original, **substitution}))
    assert store.resolve(owner, handle, "public-uppercase-v1").bundle_hash == "b" * 64


def test_two_initial_publications_for_one_claim_have_one_durable_winner(native_store):
    store, engine = native_store
    owner = principal()
    claim = store.admit(owner, "one", prepared())
    store.consume(claim, owner)

    def publish_once(_):
        try:
            return store.publish(claim, owner, publication())
        except InspectorUnavailable:
            return None

    barrier = threading.Barrier(4)

    def align_concurrent_empty_reads(_conn, _cursor, statement, _params, _ctx, _many):
        if statement.startswith("SELECT canonical_inspector_event.id") and "admission_id" in statement:
            barrier.wait(timeout=5)

    event.listen(engine, "after_cursor_execute", align_concurrent_empty_reads)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            handles = list(pool.map(publish_once, range(4)))
    finally:
        event.remove(engine, "after_cursor_execute", align_concurrent_empty_reads)
    assert sum(value is not None for value in handles) == 1
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM canonical_inspector_event WHERE kind='published'")).scalar() == 1
