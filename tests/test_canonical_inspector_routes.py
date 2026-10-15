"""Real cookie gate and default unavailable source boundary; no live app import."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect

from core.auth import AuthManager
from src.canonical_inspector_service import InspectorService, acknowledge_retention
from tests.test_canonical_inspector_admission import (
    native_store as native_store, prepared, principal, publication,
)


def retained_projection():
    return {"schema": "routing-inspection-projection-v1", "authoritative": False,
            "execution_package": {"package_hash": "1" * 64},
            "dispatch": {"receipt_hash": "d" * 64},
            "retained_evidence": {"evidence_package_hash": "e" * 64,
                                  "attempt_receipt_hash": "1" * 64,
                                  "validation": {"state": "UNVERIFIED"}}}


def retained_payload():
    return {"evidence_package_hash": "e" * 64,
            "execution_package": {"package_hash": "1" * 64, "run_id": "run-1",
                                  "packet_id": "synthetic-uppercase"},
            "attempt_receipts": [{"receipt_hash": "1" * 64, "dispatch_receipt_hash": "d" * 64}]}


def client_for(tmp_path, monkeypatch, service=None):
    from routes.routing_harness_routes import setup_routing_harness_routes

    monkeypatch.setenv("AUTH_ENABLED", "true")
    manager = AuthManager(str(tmp_path / "auth.json"))
    manager._config = {"users": {"admin": {"is_admin": True, "created": 1.5},
                                 "other": {"is_admin": True, "created": 2.0}}}
    app = FastAPI()
    app.state.auth_manager = manager
    if service is not None:
        app.state.canonical_inspector_service = service

    @app.middleware("http")
    async def stamp(request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        request.state.api_token = request.headers.get("x-test-bearer")
        return await call_next(request)

    app.include_router(setup_routing_harness_routes())
    return app, manager, TestClient(app)


def test_default_boundary_stays_unavailable_without_creating_feature_schema(tmp_path, monkeypatch):
    monkeypatch.setenv("CANONICAL_INSPECTOR_ENABLED", "true")
    monkeypatch.setenv("HALOGEN_LEASE", "not-authority")
    app, _, client = client_for(tmp_path, monkeypatch)
    response = client.post("/api/harness/inspector/pilots/synthetic-uppercase",
                           headers={"x-test-user": "admin"}, json={"idempotency_key": "one"})
    assert response.status_code == 503
    assert len(response.content) <= 512
    import core.database as database
    assert "canonical_inspector_admission" not in inspect(database.engine).get_table_names()
    app.state.auth_manager = None
    assert client.get("/api/harness/inspector/attempts/unknown",
                      headers={"x-test-user": "admin"}).status_code == 403


@pytest.mark.parametrize("headers", [{}, {"x-test-user": "internal-tool"},
                                     {"x-test-user": "admin", "x-test-bearer": "yes"}])
def test_real_cookie_gate_precedes_disabled_feature(tmp_path, monkeypatch, headers):
    _, _, client = client_for(tmp_path, monkeypatch)
    assert client.post("/api/harness/inspector/pilots/synthetic-uppercase", headers=headers,
                       json={"idempotency_key": "one"}).status_code == 403


def test_owned_inspection_authorizes_before_io_and_rechecks_before_emit(native_store, tmp_path, monkeypatch):
    store, _ = native_store
    owner = principal()
    claim = store.admit(owner, "one", prepared())
    store.consume(claim, owner)
    handle = store.publish(claim, owner, publication())
    calls = []
    service = InspectorService(store, retained_root=tmp_path,
                               projector=lambda *_a, **_k: retained_projection())

    def fake_reader(*_args, **_kwargs):
        calls.append("read")
        return retained_payload(), {}

    monkeypatch.setattr(service, "_read", fake_reader)
    _, manager, client = client_for(tmp_path, monkeypatch, service)
    path = "/api/harness/inspector/attempts/" + handle
    foreign = client.get(path, headers={"x-test-user": "other"})
    missing = client.get("/api/harness/inspector/attempts/absent", headers={"x-test-user": "admin"})
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert calls == []
    good = client.get(path, headers={"x-test-user": "admin"})
    assert good.status_code == 200
    assert good.json()["verification_state"] == "UNVERIFIED"
    assert "admin" not in good.text and str(tmp_path) not in good.text

    def changed_identity(*_a, **_k):
        manager._config["users"]["admin"]["created"] = 3.0
        return retained_payload(), {}

    monkeypatch.setattr(service, "_read", changed_identity)
    assert client.get(path, headers={"x-test-user": "admin"}).status_code == 404


def test_retention_ack_fsyncs_existing_objects_and_every_directory(tmp_path, monkeypatch):
    import os
    from src.evidence_io import retain_evidence_bundle
    from tests.test_execution_outcomes import make_evidence

    # Reuse the bounded-reader's synthetic canonical fixture, not a live run.
    payload, artifacts, *_ = make_evidence(tmp_path / "synthetic-source", exit_code=1)
    root = tmp_path / "retained"
    bundle_hash = retain_evidence_bundle(root, payload, artifacts=artifacts).stem
    calls = []
    real_fsync = os.fsync

    def observe(fd):
        calls.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", observe)
    ack = acknowledge_retention(root, bundle_hash, parent_directories=(tmp_path,))
    assert ack.bundle_hash == bundle_hash
    count = len(calls)
    assert count >= 3
    calls.clear()
    acknowledge_retention(root, bundle_hash, parent_directories=(tmp_path,))
    assert len(calls) == count
    for failure_index in range(count):
        index = [0]

        def fail_one(fd):
            value = index[0]
            index[0] += 1
            if value == failure_index:
                raise OSError("injected sync uncertainty")
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", fail_one)
        with pytest.raises(Exception):
            acknowledge_retention(root, bundle_hash, parent_directories=(tmp_path,))


def test_inspection_refuses_locator_evidence_mismatch_before_emission(native_store, tmp_path):
    from src.canonical_inspector_admission import UnknownInspection

    store, _ = native_store
    owner = principal()
    claim = store.admit(owner, "one", prepared())
    store.consume(claim, owner)
    handle = store.publish(claim, owner, publication())
    service = InspectorService(store, retained_root=tmp_path,
                               projector=lambda *_a, **_k: {"verification_state": "VERIFIED"})
    service._read = lambda *_a, **_k: ({"evidence_package_hash": "f" * 64}, {})
    with pytest.raises(UnknownInspection):
        service.inspect(owner, handle, lambda: owner)


def test_revocation_during_reader_is_seen_by_fresh_final_transaction(native_store, tmp_path):
    from src.canonical_inspector_admission import UnknownInspection

    store, _ = native_store
    owner = principal()
    claim = store.admit(owner, "one", prepared())
    store.consume(claim, owner)
    handle = store.publish(claim, owner, publication())
    calls = []
    service = InspectorService(store, retained_root=tmp_path,
                               projector=lambda *_a, **_k: retained_projection())

    def revoke_during_read(*_a, **_k):
        calls.append("read")
        store.revoke(owner, handle)
        return retained_payload(), {}

    service._read = revoke_during_read
    with pytest.raises(UnknownInspection):
        service.inspect(owner, handle, lambda: owner)
    assert calls == ["read"]
    with pytest.raises(UnknownInspection):
        service.inspect(owner, handle, lambda: owner)
    assert calls == ["read"]


@pytest.mark.parametrize("change", ["disabled", "demoted", "deleted", "renamed"])
def test_auth_configuration_change_during_reader_denies_emission(native_store, tmp_path, monkeypatch, change):
    store, _ = native_store
    owner = principal()
    claim = store.admit(owner, "one", prepared())
    store.consume(claim, owner)
    handle = store.publish(claim, owner, publication())
    service = InspectorService(store, retained_root=tmp_path,
                               projector=lambda *_a, **_k: retained_projection())
    _, manager, client = client_for(tmp_path, monkeypatch, service)

    def change_auth(*_a, **_k):
        with manager._config_lock:
            if change == "disabled":
                monkeypatch.setenv("AUTH_ENABLED", "false")
            elif change == "demoted":
                manager._config["users"]["admin"]["is_admin"] = False
            elif change == "renamed":
                manager._config["users"]["renamed"] = manager._config["users"].pop("admin")
            else:
                manager._config["users"].pop("admin")
        return retained_payload(), {}

    service._read = change_auth
    assert client.get("/api/harness/inspector/attempts/" + handle,
                      headers={"x-test-user": "admin"}).status_code == 404


def test_projection_nested_allowlist_keeps_routing_facts_without_private_fields(native_store, tmp_path):
    store, _ = native_store
    owner = principal()
    claim = store.admit(owner, "one", prepared())
    store.consume(claim, owner)
    handle = store.publish(claim, owner, publication())
    projected = retained_projection()
    projected["dispatch"]["candidates_considered"] = [{
        "target_id": "public-target", "profile_id": "public-profile", "eligible": False,
        "selected": False, "rule": "capability_receipt_stale", "reason": "PRIVATE REASON",
        "host": "PRIVATE HOST", "account": "PRIVATE ACCOUNT"}]
    projected["raw_content"] = "PRIVATE CONTENT"
    service = InspectorService(store, retained_root=tmp_path, projector=lambda *_a, **_k: projected)
    service._read = lambda *_a, **_k: (retained_payload(), {})
    result = service.inspect(owner, handle, lambda: owner)
    assert result["routing"]["candidates"] == [{"target_id": "public-target", "profile_id": "public-profile",
                                               "eligible": False, "selected": False,
                                               "rule": "capability_receipt_stale"}]
    assert "PRIVATE" not in str(result)


def test_retention_ack_requires_explicit_adjacent_private_parent_chain(tmp_path):
    from src.canonical_inspector_admission import InspectorUnavailable

    for parents in ((), (tmp_path / "unrelated",), (tmp_path.parent,)):
        with pytest.raises(InspectorUnavailable):
            acknowledge_retention(tmp_path / "retained", "b" * 64, parent_directories=parents)


def test_feature_import_and_disabled_route_do_not_register_shared_base_tables(tmp_path):
    import os
    import subprocess
    import sys

    program = '''
import os
from sqlalchemy import inspect
import core.database as db
before = set(inspect(db.engine).get_table_names())
from src.canonical_inspector_admission import metadata
from routes.routing_harness_routes import setup_routing_harness_routes
setup_routing_harness_routes()
after = set(inspect(db.engine).get_table_names())
assert before == after
assert not (set(metadata.tables) & set(db.Base.metadata.tables))
assert not os.path.exists(os.path.join(os.environ['ODYSSEUS_DATA_DIR'], 'canonical_inspector'))
'''
    result = subprocess.run([sys.executable, "-c", program], capture_output=True, text=True,
                            timeout=30, env={**os.environ,
                                "DATABASE_URL": "sqlite:///" + str(tmp_path / "isolated-native.db"),
                                "ODYSSEUS_DATA_DIR": str(tmp_path),
                                "CANONICAL_INSPECTOR_ENABLED": "true", "HALOGEN_LEASE": "not-authority"})
    assert result.returncode == 0, result.stderr


def test_repeated_normal_router_setup_uses_only_each_apps_trusted_service(tmp_path, monkeypatch):
    from routes.routing_harness_routes import setup_routing_harness_routes

    class ReadOnlyService:
        def inspect(self, *_args):
            return {"marker": "only-app-A"}

    _, manager, _ = client_for(tmp_path, monkeypatch)

    def app_client(service):
        app = FastAPI()
        app.state.auth_manager = manager
        if service:
            app.state.canonical_inspector_service = service

        @app.middleware("http")
        async def stamp(request, call_next):
            request.state.current_user = "admin"
            return await call_next(request)

        app.include_router(setup_routing_harness_routes())
        return TestClient(app)

    enabled = app_client(ReadOnlyService())
    disabled = app_client(None)
    assert enabled.get("/api/harness/inspector/attempts/unknown").json() == {"marker": "only-app-A"}
    assert disabled.get("/api/harness/inspector/attempts/unknown").status_code == 503


def source_projection(payload, *, attempt_receipt_hash, **_kwargs):
    """Explicit projector seam until composition with its separately merged API.

    This shape fixture makes no canonical projector/lifecycle acceptance claim.
    The composed-base integration test uses the actual public projector.
    """
    selected = next(item for item in payload["attempt_receipts"]
                    if item["receipt_hash"] == attempt_receipt_hash)
    return {"schema": "routing-inspection-projection-v1", "authoritative": False,
            "execution_package": {"package_hash": payload["execution_package"]["package_hash"]},
            "dispatch": {"receipt_hash": selected["dispatch_receipt_hash"]},
            "retained_evidence": {"evidence_package_hash": payload["evidence_package_hash"],
                                  "attempt_receipt_hash": attempt_receipt_hash,
                                  "validation": {"state": "UNVERIFIED"}}}


def test_fixed_pilot_service_retains_publishes_and_replay_never_executes(native_store, tmp_path, monkeypatch):
    from tests.test_canonical_pilot_adapter import synthetic_pilot

    store, engine = native_store
    pilot, state = synthetic_pilot(tmp_path)
    service = InspectorService(store, retained_root=tmp_path / "retained", projector=source_projection,
                               pilot_adapter=pilot, retention_parents=(tmp_path,))
    _, _, client = client_for(tmp_path, monkeypatch, service)
    route = "/api/harness/inspector/pilots/synthetic-uppercase"
    response = client.post(route, headers={"x-test-user": "admin"}, json={"idempotency_key": "one"})
    assert response.status_code == 200, response.text
    handle = response.json()["handle"]
    assert client.get("/api/harness/inspector/attempts/" + handle,
                      headers={"x-test-user": "admin"}).status_code == 200
    assert client.post(route, headers={"x-test-user": "admin"},
                       json={"idempotency_key": "one"}).status_code == 503
    assert state["workers"] == state["released"] == 1
    assert pilot._prepared == {}
    assert store._claims == store._consumed == {}
    from sqlalchemy import text
    with engine.connect() as connection:
        assert connection.execute(text("SELECT reserved_slot FROM canonical_inspector_admission")).scalar() is None


def test_retention_failure_does_not_publish_or_redispatch(native_store, tmp_path, monkeypatch):
    import src.canonical_inspector_service as service_module
    from sqlalchemy import text
    from src.canonical_inspector_admission import InspectorUnavailable
    from tests.test_canonical_pilot_adapter import synthetic_pilot

    store, engine = native_store
    pilot, state = synthetic_pilot(tmp_path)
    service = InspectorService(store, retained_root=tmp_path / "retained", projector=source_projection,
                               pilot_adapter=pilot, retention_parents=(tmp_path,))

    def failed_ack(*_args, **_kwargs):
        raise InspectorUnavailable("injected acknowledgement uncertainty")

    monkeypatch.setattr(service_module, "acknowledge_retention", failed_ack)
    _, _, client = client_for(tmp_path, monkeypatch, service)
    for _ in range(2):
        assert client.post("/api/harness/inspector/pilots/synthetic-uppercase",
                           headers={"x-test-user": "admin"}, json={"idempotency_key": "one"}).status_code == 503
    assert state["workers"] == 1
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM canonical_inspector_event")).scalar() == 0
        assert connection.execute(text("SELECT reserved_slot FROM canonical_inspector_admission")).scalar() == 1


def test_unconfirmed_cleanup_holds_slot_and_never_releases_lease(native_store, tmp_path, monkeypatch):
    from sqlalchemy import text
    from tests.test_canonical_pilot_adapter import synthetic_pilot

    store, engine = native_store
    pilot, state = synthetic_pilot(tmp_path)
    state["cleanup"] = False
    service = InspectorService(store, retained_root=tmp_path / "retained", projector=source_projection,
                               pilot_adapter=pilot, retention_parents=(tmp_path,))
    _, _, client = client_for(tmp_path, monkeypatch, service)
    for key in ("one", "one", "two"):
        assert client.post("/api/harness/inspector/pilots/synthetic-uppercase",
                           headers={"x-test-user": "admin"}, json={"idempotency_key": key}).status_code == 503
    assert state["workers"] == 1 and state.get("released", 0) == 0
    assert pilot._prepared == {}
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM canonical_inspector_event")).scalar() == 0
        assert connection.execute(text("SELECT reserved_slot FROM canonical_inspector_admission")).scalar() == 1


@pytest.mark.parametrize("failed", [False, True])
def test_composed_public_retained_projector_owned_read_and_revocation(native_store, tmp_path, monkeypatch, failed):
    from dataclasses import replace
    from src.attempt_receipt import make_verification_receipt
    from src.routing_inspector import project_retained_routing_inspection
    from tests.test_canonical_pilot_adapter import synthetic_pilot

    store, _ = native_store
    pilot, state = synthetic_pilot(tmp_path)
    if failed:
        original_verifier = pilot._owners.verifier

        def failed_verifier(*args):
            result = original_verifier(*args)
            receipt = make_verification_receipt(**{**result.receipt.__dict__, "exit_code": 1,
                                                   "tests_passed": 1, "tests_failed": 1})
            return replace(result, receipt=receipt)

        pilot._owners = replace(pilot._owners, verifier=failed_verifier)
    service = InspectorService(store, retained_root=tmp_path / "retained",
                               projector=project_retained_routing_inspection,
                               pilot_adapter=pilot, retention_parents=(tmp_path,))
    _, _, client = client_for(tmp_path, monkeypatch, service)
    admitted = client.post("/api/harness/inspector/pilots/synthetic-uppercase",
                           headers={"x-test-user": "admin"}, json={"idempotency_key": "actual-public-projector"})
    assert admitted.status_code == 200, admitted.text
    handle = admitted.json()["handle"]
    path = "/api/harness/inspector/attempts/" + handle
    loaded = client.get(path, headers={"x-test-user": "admin"})
    assert loaded.status_code == 200, loaded.text
    body = loaded.json()
    assert body["authoritative"] is False and body["semantic_acceptance"] == "UNKNOWN"
    assert body["verification_state"] in {"UNKNOWN", "UNVERIFIED", "VERIFIED", "FAILED"}
    if failed:
        assert body["verification_state"] != "VERIFIED"
    assert body["routing"]["capability_state"] == "OBSERVED"
    assert body["routing"]["capacity_state"] == "UNKNOWN"
    assert body["routing"]["candidates"][0]["profile_id"] == pilot._owners.estate.profiles[0].profile_id
    original_read = service._read
    reader_calls = []

    def watched_read(*args, **kwargs):
        reader_calls.append(True)
        return original_read(*args, **kwargs)

    service._read = watched_read
    foreign = client.get(path, headers={"x-test-user": "other"})
    missing = client.get("/api/harness/inspector/attempts/absent", headers={"x-test-user": "admin"})
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    assert reader_calls == []
    assert client.post(path + "/revoke", headers={"x-test-user": "admin"}).status_code == 200
    revoked = client.get(path, headers={"x-test-user": "admin"})
    assert revoked.status_code == 404 and revoked.json() == missing.json()
    assert reader_calls == [] and state["workers"] == 1
