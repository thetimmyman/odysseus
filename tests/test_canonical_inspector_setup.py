"""Explicit authenticated schema maintenance on isolated file-backed engines.

These are source/interface checks, not production migration or pilot evidence.
"""
import pytest

from tests.helpers.docker_cli import block_native_transports
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from src.canonical_inspector_admission import AdmissionStore, InspectorUnavailable
from tests.test_canonical_inspector_routes import client_for


@pytest.fixture
def setup_client(tmp_path, monkeypatch):
    # A deliberately different filename exercises the configured engine rather
    # than an observer's assumed DATA_DIR/app.db path.
    engine = create_engine("sqlite:///" + str(tmp_path / "existing-application.db"),
                           connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def native_connection(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")

    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE application_data (value TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO application_data VALUES ('retained')"))
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr("routes.routing_harness_routes.SessionLocal", sessions)
    from src import settings
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings.save_settings({})
    app, manager, client = client_for(tmp_path, monkeypatch)
    yield app, manager, client, sessions, engine
    engine.dispose()


def test_admin_can_inspect_configured_native_schema_without_initializing_it(setup_client):
    _, _, client, sessions, _ = setup_client
    response = client.get("/api/harness/inspector/setup", headers={"x-test-user": "admin"})
    assert response.status_code == 200
    data = response.json()
    assert data["schema_state"] == "ABSENT"
    assert data["table_count"] == data["trigger_count"] == 0
    assert data["connection"] == {"dialect": "sqlite", "file_backed": True,
                                   "foreign_keys": 1, "synchronous": 2, "journal_mode": "delete"}
    assert len(data["expected_schema_hash"]) == 64
    assert data["execution_enabled"] is False
    assert "existing-application.db" not in response.text
    with pytest.raises(InspectorUnavailable):
        AdmissionStore(sessions).check_schema()


def test_transport_setup_refuses_unauthenticated_request_before_any_process(setup_client, monkeypatch):
    _, _, client, _, _ = setup_client

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("unauthorized transport must not start a process")

    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden)
    response = client.post("/api/harness/inspector/setup/transport",
                           json={"expected_manifest_digest": "a" * 64})
    assert response.status_code == 403


@pytest.mark.parametrize("fault", ["none", "digest", "principal", "actor-renamed"])
def test_transport_setup_runs_only_fixed_owned_proofs(setup_client, tmp_path, monkeypatch, fault):
    import asyncio
    import json
    from src.canonical_inspector_workspace import manifest_digest
    from src.constants import CANONICAL_TMOS_TRANSPORT_GUARD_FILENAME
    from tests.test_canonical_inspector_admission import principal
    from tests.test_owned_measurement_transport import transport_manifest, SyntheticExecutor

    _, manager, client, _, _ = setup_client
    manifest = transport_manifest()
    manifest["principal"] = principal().__dict__
    if fault == "principal":
        manifest["principal"]["auth_key"] = "other"
    fixed = tmp_path / "owner.json"
    fixed.write_text(json.dumps(manifest)); fixed.chmod(0o600)
    monkeypatch.setattr("src.canonical_inspector_setup.CANONICAL_TMOS_MANIFEST_FILE", fixed)
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    monkeypatch.setattr("src.canonical_inspector_setup.CANONICAL_INSPECTOR_WORK_ROOT", work)
    monkeypatch.setattr("src.local_measurement_transport.CANONICAL_INSPECTOR_WORK_ROOT", work)
    peer = SyntheticExecutor(manifest["runtime"]["execution_transport"]["executor"])
    if fault == "actor-renamed":
        respond = peer.respond

        def rename_after_normal_unit(process, value):
            respond(process, value)
            if value["operation"] == "launch" and value["kind"] == "proof-normal":
                assert manager.rename_user("admin", "renamed", requesting_user="other")

        peer.respond = rename_after_normal_unit
    monkeypatch.setattr(asyncio, "create_subprocess_exec", peer.start)
    response = client.post("/api/harness/inspector/setup/transport", headers={"x-test-user": "admin"},
                           json={"expected_manifest_digest": "b" * 64 if fault == "digest" else manifest_digest(manifest)})
    if fault != "none":
        assert response.status_code == (409 if fault == "digest" else 503)
        if fault == "actor-renamed":
            assert [call.envelope["kind"] for call in peer.calls if call.envelope["operation"] == "launch"] == ["proof-normal"]
            status = client.get("/api/harness/inspector/setup", headers={"x-test-user": "other"}).json()
            assert status["maintenance_refusal"] == "UNRESOLVED_TRANSPORT"
            assert status["unresolved_transport"] is True
        else:
            assert peer.calls == []
        return
    assert response.status_code == 200, response.text
    assert response.json() == {"schema": "inspector-transport-proof-v1", "status": "OBSERVED_THIS_CALL",
        "proofs_completed": 2, "app_client_stop_confirmed": True, "executor_descendant_stop_confirmed": True,
        "provider_calls": 0, "broker_calls": 0, "execution_qualification": "NOT_GRANTED"}
    assert [call.envelope["kind"] for call in peer.calls if call.envelope["operation"] == "launch"] == ["proof-normal", "proof-descendant"]
    assert all(call.returncode is not None for call in peer.calls)
    assert not (work / CANONICAL_TMOS_TRANSPORT_GUARD_FILENAME).exists()
    assert client.get("/api/harness/inspector/setup", headers={"x-test-user": "admin"}).json()["schema_state"] == "ABSENT"


@pytest.mark.parametrize("headers,body,expected", [
    ({"x-test-user": "admin", "x-test-bearer": "yes"}, {"expected_manifest_digest": "a" * 64}, 403),
    ({"x-test-user": "internal-tool"}, {"expected_manifest_digest": "a" * 64}, 403),
    ({"x-test-user": "admin", "origin": "https://foreign.invalid"}, {"expected_manifest_digest": "a" * 64}, 403),
    ({"x-test-user": "admin", "origin": "http://[broken"}, {"expected_manifest_digest": "a" * 64}, 403),
    ({"x-test-user": "admin", "sec-fetch-site": "cross-site"}, {"expected_manifest_digest": "a" * 64}, 403),
    ({"x-test-user": "admin", "sec-fetch-site": "same-site"}, {"expected_manifest_digest": "a" * 64}, 403),
    ({"x-test-user": "admin"}, {"expected_manifest_digest": "a" * 64, "command": "refused"}, 400),
    ({"x-test-user": "admin"}, {"expected_manifest_digest": False}, 400),
    ({"x-test-user": "admin"}, {"expected_manifest_digest": "A" * 64}, 400),
    ({"x-test-user": "admin"}, {"expected_manifest_digest": "a" * 63}, 400),
    ({"x-test-user": "admin"}, {}, 400),
])
def test_transport_auth_csrf_and_shape_refuse_before_backend(setup_client, monkeypatch, headers, body, expected):
    _, _, client, _, _ = setup_client

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("refused transport must not start a process")

    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden)
    response = client.post("/api/harness/inspector/setup/transport", headers=headers, json=body)
    assert response.status_code == expected


@pytest.mark.parametrize("state", ["enabled", "owner-present", "unresolved-transport"])
def test_transport_maintenance_requires_disabled_idle_state(setup_client, tmp_path, monkeypatch, state):
    from src import settings
    from src.constants import CANONICAL_TMOS_TRANSPORT_GUARD_FILENAME
    app, _, client, _, _ = setup_client
    if state == "enabled":
        settings.save_settings({"canonicalInspector": {"enabled": True}})
    elif state == "owner-present":
        app.state.canonical_inspector_runtime = object()
    else:
        work = tmp_path / "work"
        work.mkdir(mode=0o700)
        (work / CANONICAL_TMOS_TRANSPORT_GUARD_FILENAME).write_bytes(b"unknown prior ownership")
        monkeypatch.setattr("src.canonical_inspector_setup.CANONICAL_INSPECTOR_WORK_ROOT", work)

    async def forbidden(*_args, **_kwargs):
        raise AssertionError("non-idle transport must not start a process")

    monkeypatch.setattr("asyncio.create_subprocess_exec", forbidden)
    response = client.post("/api/harness/inspector/setup/transport", headers={"x-test-user": "admin"},
                           json={"expected_manifest_digest": "a" * 64})
    assert response.status_code == 409


def test_explicit_admin_addition_is_compatible_and_preserves_application_data(setup_client):
    _, _, client, sessions, engine = setup_client
    path = "/api/harness/inspector/setup"
    headers = {"x-test-user": "admin"}
    before = client.get(path, headers=headers).json()
    response = client.post(path + "/schema", headers=headers,
                           json={"expected_schema_hash": before["expected_schema_hash"]})
    assert response.status_code == 200
    assert response.json()["changed"] is True
    after = client.get(path, headers=headers).json()
    assert (after["schema_state"], after["table_count"], after["trigger_count"]) == ("COMPATIBLE", 3, 12)
    AdmissionStore(sessions).check_schema()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT value FROM application_data")).scalar_one() == "retained"


def test_failed_mid_addition_rolls_back_and_excludes_private_failure_text(setup_client):
    _, _, client, sessions, engine = setup_client
    path = "/api/harness/inspector/setup"
    headers = {"x-test-user": "admin"}
    expected = client.get(path, headers=headers).json()["expected_schema_hash"]

    def fail_after_partial_ddl(_connection, _cursor, statement, *_args):
        if statement.lstrip().startswith("CREATE TABLE canonical_inspector_event"):
            raise RuntimeError("PRIVATE-FAULT-FIXTURE must never reach the operator")

    event.listen(engine, "after_cursor_execute", fail_after_partial_ddl)
    try:
        response = client.post(path + "/schema", headers=headers, json={"expected_schema_hash": expected})
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_partial_ddl)
    assert response.status_code == 503
    assert "PRIVATE-FAULT-FIXTURE" not in response.text
    assert client.get(path, headers=headers).json()["schema_state"] == "ABSENT"
    with pytest.raises(InspectorUnavailable):
        AdmissionStore(sessions).check_schema()


def test_missing_settings_after_preview_cannot_grant_disabled_setup(setup_client, tmp_path):
    _, _, client, sessions, _ = setup_client
    path = "/api/harness/inspector/setup"
    headers = {"x-test-user": "admin"}
    expected = client.get(path, headers=headers).json()["expected_schema_hash"]
    (tmp_path / "settings.json").unlink()
    response = client.post(path + "/schema", headers=headers, json={"expected_schema_hash": expected})
    assert response.status_code == 503
    with pytest.raises(InspectorUnavailable):
        AdmissionStore(sessions).check_schema()


def test_changed_account_epoch_during_addition_rolls_back_only_the_schema(setup_client):
    _, manager, client, sessions, engine = setup_client
    path = "/api/harness/inspector/setup"
    headers = {"x-test-user": "admin"}
    expected = client.get(path, headers=headers).json()["expected_schema_hash"]

    def change_account(_connection, _cursor, statement, *_args):
        if statement.lstrip().startswith("CREATE TABLE canonical_inspector_event"):
            manager._config["users"]["admin"]["created"] = 3.0

    event.listen(engine, "after_cursor_execute", change_account)
    try:
        response = client.post(path + "/schema", headers=headers, json={"expected_schema_hash": expected})
    finally:
        event.remove(engine, "after_cursor_execute", change_account)
    assert response.status_code == 503
    assert manager.snapshot_admin_account_epoch("admin")[1] == "float64_hex:" + (3.0).hex()
    assert client.get(path, headers=headers).json()["schema_state"] == "ABSENT"
    with pytest.raises(InspectorUnavailable):
        AdmissionStore(sessions).check_schema()


@pytest.mark.parametrize("owner_attribute", ["canonical_inspector_runtime", "canonical_inspector_service"])
def test_present_owner_refuses_addition_even_when_setting_is_disabled(setup_client, owner_attribute):
    app, _, client, sessions, _ = setup_client
    path = "/api/harness/inspector/setup"
    headers = {"x-test-user": "admin"}
    expected = client.get(path, headers=headers).json()["expected_schema_hash"]
    setattr(app.state, owner_attribute, object())
    response = client.post(path + "/schema", headers=headers, json={"expected_schema_hash": expected})
    assert response.status_code == 409
    with pytest.raises(InspectorUnavailable):
        AdmissionStore(sessions).check_schema()


def test_changed_enabled_setting_cannot_hide_behind_cached_preview(setup_client, tmp_path):
    from src import settings
    _, _, client, sessions, _ = setup_client
    path = "/api/harness/inspector/setup"
    headers = {"x-test-user": "admin"}
    settings.load_settings()  # Existing normal caller may have cached disabled.
    expected = client.get(path, headers=headers).json()["expected_schema_hash"]
    (tmp_path / "settings.json").write_text('{"canonicalInspector":{"enabled":true}}')
    response = client.post(path + "/schema", headers=headers, json={"expected_schema_hash": expected})
    assert response.status_code == 409
    assert client.get(path, headers=headers).json()["execution_enabled"] is True
    with pytest.raises(InspectorUnavailable):
        AdmissionStore(sessions).check_schema()


def test_unresolved_admission_blocks_setup_without_releasing_it(setup_client):
    from tests.test_canonical_inspector_admission import prepared, principal
    _, _, client, sessions, _ = setup_client
    path = "/api/harness/inspector/setup"
    headers = {"x-test-user": "admin"}
    expected = client.get(path, headers=headers).json()["expected_schema_hash"]
    assert client.post(path + "/schema", headers=headers,
                       json={"expected_schema_hash": expected}).status_code == 200
    store = AdmissionStore(sessions)
    claim = store.admit(principal(), "uncertain", prepared())
    observed = client.get(path, headers=headers).json()
    assert observed["maintenance_available"] is False
    assert observed["maintenance_refusal"] == "UNRESOLVED_ADMISSION"
    response = client.post(path + "/schema", headers=headers, json={"expected_schema_hash": expected})
    assert response.status_code == 409
    assert store.consume(claim, principal()).run_id == "run-1"


@pytest.mark.parametrize("headers", [
    {}, {"x-test-user": "member"}, {"x-test-user": "api"}, {"x-test-user": "internal-tool"},
    {"x-test-user": "admin", "x-test-bearer": "fixture-bearer"},
])
def test_cookie_admin_authority_precedes_status_and_request_body(setup_client, headers):
    _, _, client, sessions, _ = setup_client
    path = "/api/harness/inspector/setup"
    assert client.get(path, headers=headers).status_code == 403
    assert client.post(path + "/schema", headers=headers, content="not JSON").status_code == 403
    with pytest.raises(InspectorUnavailable):
        AdmissionStore(sessions).check_schema()


def test_loopback_marker_cannot_borrow_an_admin_cookie_for_schema_setup(setup_client):
    from core.middleware import INTERNAL_TOOL_HEADER
    _, _, client, sessions, _ = setup_client
    headers = {"x-test-user": "admin", INTERNAL_TOOL_HEADER: "fixture-marker"}
    path = "/api/harness/inspector/setup"
    assert client.get(path, headers=headers).status_code == 403
    assert client.post(path + "/schema", headers=headers, content="not JSON").status_code == 403
    with pytest.raises(InspectorUnavailable):
        AdmissionStore(sessions).check_schema()


@pytest.mark.parametrize("body", [
    {}, [], {"expected_schema_hash": "0" * 64, "database_url": "sqlite:///another.db"},
    {"expected_schema_hash": "0" * 64, "sql": "DROP TABLE application_data"},
])
def test_setup_cannot_select_a_database_or_supply_ddl(setup_client, body):
    _, _, client, sessions, _ = setup_client
    response = client.post("/api/harness/inspector/setup/schema", headers={"x-test-user": "admin"}, json=body)
    assert response.status_code == 400
    with pytest.raises(InspectorUnavailable):
        AdmissionStore(sessions).check_schema()


@pytest.mark.parametrize("collision", ["partial_table", "trigger_on_application_table"])
def test_partial_or_conflicting_schema_is_refused_without_repair(setup_client, collision):
    _, _, client, sessions, engine = setup_client
    with engine.begin() as connection:
        if collision == "partial_table":
            connection.execute(text("CREATE TABLE canonical_inspector_admission (id TEXT)"))
        else:
            connection.execute(text("CREATE TRIGGER canonical_admission_immutable "
                "BEFORE UPDATE ON application_data BEGIN SELECT RAISE(ABORT, 'fixture conflict'); END"))
    path = "/api/harness/inspector/setup"
    headers = {"x-test-user": "admin"}
    before = client.get(path, headers=headers).json()
    assert before["schema_state"] == "MISMATCH"
    response = client.post(path + "/schema", headers=headers,
                           json={"expected_schema_hash": before["expected_schema_hash"]})
    assert response.status_code == 409
    assert client.get(path, headers=headers).json() == before
    with pytest.raises(InspectorUnavailable):
        AdmissionStore(sessions).check_schema()


def test_changed_schema_expectation_refuses_before_addition(setup_client):
    _, _, client, sessions, _ = setup_client
    response = client.post("/api/harness/inspector/setup/schema", headers={"x-test-user": "admin"},
                           json={"expected_schema_hash": "0" * 64})
    assert response.status_code == 409
    with pytest.raises(InspectorUnavailable):
        AdmissionStore(sessions).check_schema()


def test_compatible_schema_is_observed_without_reapplying_or_minting_an_owner(setup_client):
    app, _, client, sessions, _ = setup_client
    path = "/api/harness/inspector/setup"
    headers = {"x-test-user": "admin"}
    expected = client.get(path, headers=headers).json()["expected_schema_hash"]
    first = client.post(path + "/schema", headers=headers, json={"expected_schema_hash": expected})
    assert first.json()["changed"] is True
    second = client.post(path + "/schema", headers=headers, json={"expected_schema_hash": expected})
    assert second.status_code == 200
    assert second.json()["changed"] is False
    assert second.json()["execution_qualification"] == "NOT_RUN"
    assert getattr(app.state, "canonical_inspector_runtime", None) is None
    assert getattr(app.state, "canonical_inspector_service", None) is None
    AdmissionStore(sessions).check_schema()


def test_memory_engine_cannot_be_initialized_via_the_setup_route(setup_client, monkeypatch):
    _, _, client, _, _ = setup_client
    engine = create_engine("sqlite:///:memory:")
    monkeypatch.setattr("routes.routing_harness_routes.SessionLocal", sessionmaker(bind=engine))
    try:
        path = "/api/harness/inspector/setup"
        headers = {"x-test-user": "admin"}
        assert client.get(path, headers=headers).status_code == 503
        assert client.post(path + "/schema", headers=headers,
                           json={"expected_schema_hash": "0" * 64}).status_code == 503
    finally:
        engine.dispose()


@pytest.mark.parametrize("condition", ["foreign_keys_off", "synchronous_off", "memory_journal", "autocommit"])
def test_unqualified_native_connection_is_refused_without_adjusting_it(setup_client, condition):
    _, _, client, sessions, engine = setup_client

    def unsafe_connection(connection, _):
        if condition == "autocommit":
            connection.isolation_level = None
        else:
            connection.execute({"foreign_keys_off": "PRAGMA foreign_keys=OFF",
                "synchronous_off": "PRAGMA synchronous=OFF",
                "memory_journal": "PRAGMA journal_mode=MEMORY"}[condition])

    engine.dispose()
    event.listen(engine, "connect", unsafe_connection)
    try:
        path = "/api/harness/inspector/setup"
        headers = {"x-test-user": "admin"}
        assert client.get(path, headers=headers).status_code == 503
        assert client.post(path + "/schema", headers=headers,
                           json={"expected_schema_hash": "0" * 64}).status_code == 503
    finally:
        event.remove(engine, "connect", unsafe_connection)
        engine.dispose()
    with pytest.raises(InspectorUnavailable):
        AdmissionStore(sessions).check_schema()


@pytest.mark.parametrize("source", ["{broken", "[]", '{"canonicalInspector":{"enabled":"false"}}'])
def test_malformed_settings_are_unavailable_instead_of_disabled_defaults(setup_client, tmp_path, source):
    _, _, client, sessions, _ = setup_client
    (tmp_path / "settings.json").write_text(source)
    path = "/api/harness/inspector/setup"
    headers = {"x-test-user": "admin"}
    assert client.get(path, headers=headers).status_code == 503
    assert client.post(path + "/schema", headers=headers,
                       json={"expected_schema_hash": "0" * 64}).status_code == 503
    with pytest.raises(InspectorUnavailable):
        AdmissionStore(sessions).check_schema()


def test_setting_change_during_addition_rolls_back_schema_without_reverting_setting(setup_client):
    from src import settings
    _, _, client, sessions, engine = setup_client
    path = "/api/harness/inspector/setup"
    headers = {"x-test-user": "admin"}
    expected = client.get(path, headers=headers).json()["expected_schema_hash"]

    def enable_during_addition(_connection, _cursor, statement, *_args):
        if statement.lstrip().startswith("CREATE TABLE canonical_inspector_event"):
            settings.save_settings({"canonicalInspector": {"enabled": True}})

    event.listen(engine, "after_cursor_execute", enable_during_addition)
    try:
        response = client.post(path + "/schema", headers=headers, json={"expected_schema_hash": expected})
    finally:
        event.remove(engine, "after_cursor_execute", enable_during_addition)
    assert response.status_code == 409
    after = client.get(path, headers=headers).json()
    assert after["schema_state"] == "ABSENT"
    assert after["execution_enabled"] is True
    with pytest.raises(InspectorUnavailable):
        AdmissionStore(sessions).check_schema()


def test_completed_schema_survives_connection_reopen_without_owner_setup(setup_client, monkeypatch):
    _, _, client, _, engine = setup_client
    path = "/api/harness/inspector/setup"
    headers = {"x-test-user": "admin"}
    expected = client.get(path, headers=headers).json()["expected_schema_hash"]
    assert client.post(path + "/schema", headers=headers,
                       json={"expected_schema_hash": expected}).status_code == 200
    engine.dispose()
    reopened = create_engine(engine.url, connect_args={"check_same_thread": False})

    @event.listens_for(reopened, "connect")
    def qualify_reopened(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")

    sessions = sessionmaker(bind=reopened)
    monkeypatch.setattr("routes.routing_harness_routes.SessionLocal", sessions)
    try:
        data = client.get(path, headers=headers).json()
        assert (data["schema_state"], data["table_count"], data["trigger_count"]) == ("COMPATIBLE", 3, 12)
        assert data["execution_qualification"] == "NOT_RUN"
        AdmissionStore(sessions).check_schema()
        assert client.get("/api/harness/inspector/attempts/00000000-0000-4000-8000-000000000001",
                          headers=headers).status_code == 503
    finally:
        reopened.dispose()


@pytest.mark.parametrize("payload", [
    '{"expected_schema_hash":NaN}',
    '{"expected_schema_hash":"' + "0" * 64 + '","expected_schema_hash":"' + "1" * 64 + '"}',
    '{"expected_schema_hash":"' + "x" * 5000 + '"}',
])
def test_malformed_duplicate_or_oversized_request_cannot_initialize_schema(setup_client, payload):
    _, _, client, sessions, _ = setup_client
    response = client.post("/api/harness/inspector/setup/schema", headers={"x-test-user": "admin"},
                           content=payload)
    assert response.status_code == 400
    with pytest.raises(InspectorUnavailable):
        AdmissionStore(sessions).check_schema()


@pytest.fixture(autouse=True)
def no_native_transports(monkeypatch):
    block_native_transports(monkeypatch)
