"""App-owned real-work composition with inert I/O; no runtime qualification."""
import pytest
from sqlalchemy.orm import sessionmaker

from tests.test_canonical_inspector_admission import native_store as native_store
from tests.test_canonical_inspector_routes import client_for
from tests.test_canonical_tmos_publication import retained_work


class InertProcesses:
    """Source adapter only. Construction and inspection perform no captures."""

    def __init__(self, **configuration):
        self.configuration = configuration
        self.calls = []

    async def terminate_all(self):
        self.calls.append("terminate")

    async def confirm_stopped(self):
        return True

    def request_stop(self):
        self.calls.append("stop requested")


def test_failed_alternate_is_durable_unresolved_and_never_applied_or_replayed(native_store, tmp_path):
    from src.canonical_inspector_admission import AdmissionStore, InspectorUnavailable
    from tests.test_inspector_alternate import two_target_work, execute_alternate_source

    store, engine = native_store
    work = two_target_work(store, tmp_path)
    alternate = execute_alternate_source(store, work)
    failed = store.fail_tmos_alternate(alternate.claim, work.owner, stage="invoke")
    assert failed["status"] == "UNRESOLVED"
    assert failed["failed_stage"] == "invoke"
    assert failed["resulting_attempt_receipt_hash"] is None
    assert failed["resulting_dispatch_receipt_hash"] is None
    assert failed["operator_request_receipt_hash"] == alternate.preference["receipt_hash"]
    assert store.fail_tmos_alternate(alternate.claim, work.owner, stage="invoke") == failed
    reopened = AdmissionStore(sessionmaker(bind=engine, expire_on_commit=False))
    rows = reopened.interventions(work.owner, work.published["handle"], "tmos-routing-inspector-v1")
    assert failed in rows
    assert not any(row["status"] == "APPLIED" for row in rows)
    with pytest.raises(InspectorUnavailable):
        work.service.publish_tmos_alternate(work.owner, work.published["handle"],
            alternate.preference["receipt_hash"], alternate.claim, alternate.execution, lambda: work.owner)


@pytest.mark.parametrize("field,value", [("contract", "A different contract"),
                                         ("allowed_tools", ()), ("objective", "A different objective")])
def test_alternate_cannot_retask_the_original_owned_work(native_store, tmp_path, field, value):
    from dataclasses import replace
    from src.canonical_inspector_admission import InspectorUnavailable
    from tests.test_inspector_alternate import two_target_work, execute_alternate_source

    store, _ = native_store
    work = two_target_work(store, tmp_path)
    work.package = replace(work.package, **{field: value})
    alternate = execute_alternate_source(store, work)
    with pytest.raises(InspectorUnavailable):
        work.service.publish_tmos_alternate(work.owner, work.published["handle"],
            alternate.preference["receipt_hash"], alternate.claim, alternate.execution, lambda: work.owner)


@pytest.mark.asyncio
async def test_default_lifecycle_never_constructs_owner_or_installs_service(tmp_path, monkeypatch):
    from src.canonical_inspector_runtime import start_inspector_runtime, stop_inspector_runtime

    app, _, client = client_for(tmp_path, monkeypatch)
    calls = []

    def forbidden(**_kwargs):
        calls.append("unexpected")
        raise AssertionError("disabled setup must not construct an executor")

    await start_inspector_runtime(app, settings={}, process_factory=forbidden)
    assert calls == []
    assert client.get("/api/harness/inspector/attempts/unknown",
        headers={"x-test-user": "admin"}).status_code == 503
    await stop_inspector_runtime(app)
    assert calls == []


@pytest.mark.asyncio
async def test_configured_lifecycle_composes_the_owned_retained_interface_without_provider_io(native_store, tmp_path, monkeypatch):
    from src.canonical_inspector_runtime import start_inspector_runtime, stop_inspector_runtime

    store, engine = native_store
    work = retained_work(store, tmp_path)
    published = work.service.publish_tmos_work(work.owner, work.claim, work.execution, lambda: work.owner)
    app, _, client = client_for(tmp_path, monkeypatch)
    processes = []

    def inert(**configuration):
        process = InertProcesses(**configuration)
        processes.append(process)
        return process

    await start_inspector_runtime(app, settings={"canonicalInspector": {"enabled": True}},
        sessions=sessionmaker(bind=engine, expire_on_commit=False),
        manifest={"schema": "canonical-tmos-owner-v1", "packages": {work.package.packet_id: work.package.to_dict()}},
        retained_root=work.path.parent, process_factory=inert)
    path = "/api/harness/inspector/attempts/" + published["handle"]
    response = client.get(path, headers={"x-test-user": "admin"})
    assert response.status_code == 200
    assert response.json()["execution_package_hash"] == work.package.package_hash
    assert response.json()["attempt_receipt_hash"] == work.execution.attempt_hash
    assert response.json()["mission_key"] == "TMOS-11"
    assert client.get(path, headers={"x-test-user": "other"}).status_code == 404
    assert all(process.calls == [] for process in processes)
    await stop_inspector_runtime(app)
    assert client.get(path, headers={"x-test-user": "admin"}).status_code == 503


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["foreign-owner", "extra-input", "other-packet", "pilot", "no-cookie"])
async def test_real_admission_refuses_unowned_or_unadmitted_input_before_any_capture(native_store, tmp_path, monkeypatch, scenario):
    import httpx
    from src.canonical_inspector_runtime import start_inspector_runtime, stop_inspector_runtime
    from tests.test_canonical_inspector_admission import principal
    from tests.test_canonical_tmos_publication import tmos_package

    _, engine = native_store
    package = tmos_package()
    retained = tmp_path / "retained"
    retained.mkdir(mode=0o700)
    app, _, _ = client_for(tmp_path, monkeypatch)
    calls = []

    def forbidden(**_configuration):
        calls.append("unexpected capture")
        raise AssertionError("refused input must not construct an executor")

    await start_inspector_runtime(app, settings={"canonicalInspector": {"enabled": True}},
        sessions=sessionmaker(bind=engine, expire_on_commit=False), retained_root=retained,
        manifest={"schema": "canonical-tmos-owner-v1", "principal": principal().__dict__,
                  "packages": {package.packet_id: package.to_dict()}}, process_factory=forbidden)
    path = "/api/harness/inspector/tmos/" + package.packet_id
    body = {"idempotency_key": "one"}
    headers = {"x-test-user": "other" if scenario == "foreign-owner" else "admin"}
    if scenario == "extra-input":
        body["package"] = package.to_dict()
    elif scenario == "other-packet":
        path = "/api/harness/inspector/tmos/arbitrary-task"
    elif scenario == "pilot":
        path = "/api/harness/inspector/pilots/synthetic-uppercase"
    elif scenario == "no-cookie":
        headers = {}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(path, json=body, headers=headers)
    assert response.status_code == (403 if scenario == "no-cookie" else 503)
    assert calls == []
    assert await stop_inspector_runtime(app) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing-schema", "missing-trigger", "other-project", "bad-package-hash"])
async def test_unqualified_configured_composition_stays_unavailable_before_executor_construction(native_store, tmp_path, monkeypatch, change):
    from sqlalchemy import create_engine, text
    from src.canonical_inspector_runtime import start_inspector_runtime
    from src.execution_package import compute_package_hash
    from tests.test_canonical_tmos_publication import tmos_package

    _, engine = native_store
    package = tmos_package().to_dict()
    if change == "missing-schema":
        engine = create_engine("sqlite:///" + str(tmp_path / "uninitialized.db"))
    elif change == "missing-trigger":
        with engine.begin() as connection:
            connection.execute(text("DROP TRIGGER canonical_event_no_update"))
    elif change == "other-project":
        package["ticket_key"] = "OTHER-1"
        package["package_hash"] = compute_package_hash({key: value for key, value in package.items() if key != "package_hash"})
    else:
        package["package_hash"] = "f" * 64
    retained = tmp_path / "retained"
    retained.mkdir(mode=0o700)
    app, _, client = client_for(tmp_path, monkeypatch)

    def forbidden(**_configuration):
        raise AssertionError("unqualified setup must not construct an executor")

    await start_inspector_runtime(app, settings={"canonicalInspector": {"enabled": True}},
        sessions=sessionmaker(bind=engine, expire_on_commit=False),
        manifest={"schema": "canonical-tmos-owner-v1", "packages": {package["packet_id"]: package}},
        retained_root=retained, process_factory=forbidden)
    assert client.get("/api/harness/inspector/attempts/unknown",
        headers={"x-test-user": "admin"}).status_code == 503
    if change == "missing-schema":
        from sqlalchemy import inspect
        assert inspect(engine).get_table_names() == []


def test_workspace_capture_cannot_start_without_the_same_operational_process_proof():
    from src.local_measurement_owner import MeasurementUnresolved
    from src.local_measurement_process import SystemdProcesses

    processes = SystemdProcesses(worker_command=["unused"], broker_worker=["unused"],
                                workspace_worker=["unused"])
    with pytest.raises(MeasurementUnresolved):
        processes.workspace({"schema": "tmos-workspace-operation-v1", "operation": "prepare",
            "packet_id": "routing-inspector-command", "run_id": "00000000-0000-4000-8000-000000000001",
            "manifest_digest": "a" * 64}, lease_valid=lambda: True)


def test_fixed_workspace_cli_uses_only_its_private_admitted_manifest(tmp_path, monkeypatch):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    from src.constants import LOCAL_MEASUREMENT_COMMAND

    path, _, operation = workspace_fixture(tmp_path, monkeypatch)
    root = Path(os.environ["ODYSSEUS_DATA_DIR"]) / "canonical_inspector"
    root.mkdir(parents=True, mode=0o700)
    fixed = root / "tmos-owner.json"
    fixed.write_bytes(path.read_bytes())
    fixed.chmod(0o600)
    command = [sys.executable, "-I", LOCAL_MEASUREMENT_COMMAND, "--worker-workspace", "--tmos-manifest", str(fixed)]
    response = subprocess.run(command, input=json.dumps(operation).encode(), capture_output=True, timeout=5)
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout)["source"]["detached"] is True
    refused = subprocess.run([*command[:-1], str(path)], input=json.dumps(operation).encode(),
                             capture_output=True, timeout=5)
    assert refused.returncode == 2 and refused.stdout == b""


@pytest.mark.asyncio
async def test_shutdown_cannot_claim_stop_while_a_start_request_is_unresolved(monkeypatch):
    import asyncio
    import threading
    from src.local_measurement_owner import MeasurementUnresolved
    from src.local_measurement_process import SystemdProcesses

    started, release = threading.Event(), threading.Event()

    async def uncertain_start(*_args, **_kwargs):
        started.set()
        while not release.is_set():
            await asyncio.sleep(0.01)
        raise OSError("source fixture: manager acknowledgement missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", uncertain_start)
    processes = SystemdProcesses(worker_command=["unused"], broker_worker=["unused"])
    proof = asyncio.create_task(asyncio.to_thread(processes.qualify_no_provider))
    assert await asyncio.to_thread(started.wait, 2)
    try:
        assert await processes.confirm_stopped() is False
    finally:
        release.set()
        with pytest.raises(MeasurementUnresolved):
            await proof
    assert await processes.confirm_stopped() is False


def workspace_fixture(tmp_path, monkeypatch):
    import hashlib
    import json
    import subprocess
    from dataclasses import replace
    from src.execution_package import compute_package_hash
    from src.source_snapshot import take_source_snapshot
    from tests.test_canonical_tmos_publication import tmos_package

    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr("src.canonical_inspector_runtime.CANONICAL_INSPECTOR_WORK_ROOT", tmp_path / "captures")
    repo = tmp_path / "source"
    repo.mkdir()
    for command in (
            ["git", "init", "-q", "--initial-branch=main"],
            ["git", "config", "user.name", "Synthetic"],
            ["git", "config", "user.email", "synthetic@example.invalid"],
            ["git", "config", "commit.gpgsign", "false"],
            ["git", "remote", "add", "origin", "https://example.invalid/tmos.git"]):
        subprocess.run(command, cwd=repo, check=True, capture_output=True)
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src/thing.py").write_text("value = 1\n")
    verifier = b"# synthetic source-only verifier\n"
    (repo / "tests/test_thing.py").write_bytes(verifier)
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "synthetic source"], cwd=repo, check=True, capture_output=True)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    source = take_source_snapshot(str(repo), base_sha=head, relevant_paths=("src/thing.py", "tests/test_thing.py"))
    template = tmos_package()
    template = replace(template, budgets=replace(template.budgets, max_attempts=1,
        context_tokens=4096, output_tokens=512, time_seconds=180))
    verification = replace(template.verification,
        verifier_digests=(("tests/test_thing.py", hashlib.sha256(verifier).hexdigest()),))
    package = replace(template, source=source, verification=verification)
    package = replace(package, package_hash=compute_package_hash(package.core()))
    manifest = {"schema": "canonical-tmos-owner-v1", "packages": {package.packet_id: package.to_dict()}}
    path = tmp_path / "owner.json"
    path.write_text(json.dumps(manifest))
    path.chmod(0o600)
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    operation = {"schema": "tmos-workspace-operation-v1", "operation": "prepare", "packet_id": package.packet_id,
        "run_id": "00000000-0000-4000-8000-000000000001", "manifest_digest": digest}
    return path, package, operation


def test_workspace_prepares_exact_clean_base_and_refuses_reuse_or_arbitrary_paths(tmp_path, monkeypatch):
    from src.canonical_inspector_admission import InspectorUnavailable
    from src.canonical_inspector_runtime import workspace_operation
    from src.source_snapshot import source_snapshot_from_dict

    path, package, operation = workspace_fixture(tmp_path, monkeypatch)
    prepared = workspace_operation(operation, manifest_path=path)
    source = source_snapshot_from_dict(prepared["source"])
    assert source.head_sha == package.source.head_sha
    assert source.is_clean and source.is_complete and source.detached
    assert source.worktree != package.source.worktree
    assert prepared["cleanup_confirmed"] is True
    with pytest.raises(InspectorUnavailable):
        workspace_operation(operation, manifest_path=path)
    with pytest.raises(InspectorUnavailable):
        workspace_operation({**operation, "repo": "/arbitrary"}, manifest_path=path)


def test_workspace_applies_only_admitted_source_paths_including_git_metadata(tmp_path, monkeypatch):
    import copy
    from pathlib import Path
    from src.canonical_inspector_admission import InspectorUnavailable
    from src.canonical_inspector_runtime import workspace_operation
    from src.execution_package import compute_package_hash

    path, template, operation = workspace_fixture(tmp_path, monkeypatch)
    prepared = workspace_operation(operation, manifest_path=path)
    package = copy.deepcopy(template.to_dict())
    package.update(run_id=operation["run_id"], source=prepared["source"])
    package["package_hash"] = compute_package_hash({key: value for key, value in package.items() if key != "package_hash"})
    patch = "diff --git a/src/thing.py b/src/thing.py\n--- a/src/thing.py\n+++ b/src/thing.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    worktree = Path(package["source"]["worktree"])
    renamed_verifier = "diff --git a/tests/test_thing.py b/tests/moved.py\nsimilarity index 100%\nrename from tests/test_thing.py\nrename to tests/moved.py\n"
    with pytest.raises(InspectorUnavailable):
        workspace_operation({**operation, "operation": "apply", "package": package,
                             "patch": patch + renamed_verifier}, manifest_path=path)
    assert (worktree / "src/thing.py").read_text() == "value = 1\n"
    assert (worktree / "tests/test_thing.py").exists()
    assert not (worktree / "tests/moved.py").exists()
    applied = workspace_operation({**operation, "operation": "apply", "package": package,
                                   "patch": patch}, manifest_path=path)
    assert applied["changed_files"] == ["src/thing.py"]
    assert (worktree / "src/thing.py").read_text() == "value = 2\n"
    assert (Path(template.source.worktree) / "src/thing.py").read_text() == "value = 1\n"


def test_workspace_snapshot_refuses_retasking_the_immutable_package(tmp_path, monkeypatch):
    import copy
    from src.canonical_inspector_admission import InspectorUnavailable
    from src.canonical_inspector_runtime import workspace_operation
    from src.execution_package import compute_package_hash

    path, template, operation = workspace_fixture(tmp_path, monkeypatch)
    prepared = workspace_operation(operation, manifest_path=path)
    package = copy.deepcopy(template.to_dict())
    package.update(run_id=operation["run_id"], source=prepared["source"])
    package["package_hash"] = compute_package_hash({key: value for key, value in package.items() if key != "package_hash"})
    observed = workspace_operation({**operation, "operation": "snapshot", "package": package}, manifest_path=path)
    assert observed["source"] == prepared["source"]
    for changes in ({"ticket_key": "TMOS-99"}, {"allowed_tools": ["shell"]}, {"write_scope": ["tests/test_thing.py"]}):
        changed = {**package, **changes}
        changed["package_hash"] = compute_package_hash({key: value for key, value in changed.items() if key != "package_hash"})
        with pytest.raises(InspectorUnavailable):
            workspace_operation({**operation, "operation": "snapshot", "package": changed}, manifest_path=path)


def test_workspace_verifier_retains_both_streams_and_requires_container_end_acknowledgement(tmp_path, monkeypatch):
    import base64
    import copy
    from pathlib import Path
    import subprocess
    from src.canonical_inspector_admission import InspectorUnavailable
    from src.canonical_inspector_runtime import workspace_operation
    from src.dispatch_routing import policy_snapshot
    from src.execution_package import compute_package_hash

    path, template, operation = workspace_fixture(tmp_path, monkeypatch)
    prepared = workspace_operation(operation, manifest_path=path)
    package = copy.deepcopy(template.to_dict())
    package.update(run_id=operation["run_id"], source=prepared["source"])
    package["package_hash"] = compute_package_hash({key: value for key, value in package.items() if key != "package_hash"})
    policy = {"routingPolicyVersion": "synthetic-workspace-v1"}
    monkeypatch.setattr("src.routing_policy.load_policy", lambda **_kwargs: policy)
    stdout = b'{"schema":"tmos-work-verification-v1","tests_passed":2,"tests_failed":0}\n'
    seen = []

    def inert_verifier(worktree, command, **options):
        seen.append((worktree, command))
        directory = Path(options["artifacts_dir"])
        directory.mkdir(parents=True, exist_ok=True)
        out, err = directory / "stdout", directory / "stderr"
        out.write_bytes(stdout)
        err.write_bytes(b"")
        out.chmod(0o600)
        err.chmod(0o600)
        return {"allowed": True, "exit_code": 0, "timed_out": False, "error": None,
            "stdout_path": str(out), "stderr_path": str(err), "stdout_truncated": False,
            "stderr_truncated": False, "container_name": "routing-sbx-" + "a" * 12,
            "timeout_s": options["timeout_s"]}

    original_run = subprocess.run

    def inert_docker(argv, **options):
        if argv[:3] == ["docker", "container", "ls"]:
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        return original_run(argv, **options)

    monkeypatch.setattr("src.routing_sandbox.run_in_sandbox", inert_verifier)
    monkeypatch.setattr(subprocess, "run", inert_docker)
    request = {**operation, "operation": "verify", "package": package,
        "policy_hash": policy_snapshot(policy=policy).policy_hash, "timeout_s": 3}
    captured = workspace_operation(request, manifest_path=path)
    assert seen == [(prepared["source"]["worktree"], template.verification.command)]
    assert base64.b64decode(captured["stdout_b64"]) == stdout
    assert base64.b64decode(captured["stderr_b64"]) == b""
    assert captured["source"] == prepared["source"]
    assert captured["cleanup_confirmed"] is True
    assert captured["timeout_s"] == 3
    with pytest.raises(InspectorUnavailable):
        workspace_operation(request, manifest_path=path)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["initial", "alternate", "alternate-stale", "alternate-identity-mismatch",
                                      "initial-identity-mismatch", "initial-container-unknown", "initial-revoked",
                                      "initial-permission"])
async def test_app_owned_execution_runs_normal_loop_and_publishes_exact_inert_attempt(native_store, tmp_path, monkeypatch, scenario):
    """Real composition/loop/retention with synthetic external I/O only."""
    from datetime import datetime, timezone
    import json
    from pathlib import Path
    from types import SimpleNamespace
    import subprocess
    from src.canonical_inspector_runtime import start_inspector_runtime, stop_inspector_runtime, workspace_operation
    from tests.test_canonical_inspector_admission import principal
    from tests.test_inspector_alternate import routing_inputs

    _, engine = native_store
    manifest_path, template, _ = workspace_fixture(tmp_path, monkeypatch)
    estate, capabilities, _, profiles = routing_inputs(tmp_path,
        runtime_options={"num_ctx": 4096, "num_predict": 512, "temperature": 0, "think": False})
    manifest = json.loads(manifest_path.read_text())
    if scenario == "initial-permission":
        from src.execution_package import compute_package_hash
        package = manifest["packages"][template.packet_id]
        package["allowed_tools"] = []
        package["package_hash"] = compute_package_hash({key: value for key, value in package.items() if key != "package_hash"})
    manifest["principal"] = principal().__dict__
    manifest["runtime"] = {"lease_client": "/synthetic/lease-client", "lease_client_sha256": "a" * 64,
        "identity_commands": {p.target_id: [p.target_id] for p in profiles},
        "preferred_profiles": [profiles[0].profile_id]}
    manifest_path.write_text(json.dumps(manifest))
    policy = {"routingPolicyVersion": "synthetic-owned-loop-v1"}
    monkeypatch.setattr("src.routing_policy.load_policy", lambda **_kwargs: policy)
    patch = "diff --git a/src/thing.py b/src/thing.py\n--- a/src/thing.py\n+++ b/src/thing.py\n@@ -1 +1 @@\n-value = 1\n+value = 2\n"
    calls = []
    instances = []
    fault = {"mode": scenario if scenario.startswith("initial-") else "none"}

    class Processes(InertProcesses):
        qualified = False

        def __init__(self, **configuration):
            super().__init__(**configuration)
            self.profile = profiles[min(len(instances), 1)]
            instances.append(self)

        def qualify_no_provider(self):
            calls.append("local-proof-source-fixture")
            self.qualified = True

        def workspace(self, value, *, lease_valid):
            assert self.qualified and lease_valid()
            calls.append(value["operation"])
            return workspace_operation(value, manifest_path=manifest_path)

        def identity(self, command, *, lease_valid):
            assert self.qualified and lease_valid()
            calls.append("identity-source-fixture")
            profile = next(p for p in profiles if p.target_id == command[0])
            receipt = capabilities.current(profile.profile_id)
            if fault["mode"] == "initial-revoked":
                with app.state.auth_manager._config_lock:
                    app.state.auth_manager._config["users"]["admin"]["is_admin"] = False
            if fault["mode"].endswith("identity-mismatch"):
                return {"profile_id": profile.profile_id, "checked_at": datetime.now(timezone.utc).isoformat(),
                    "current_material_identity": receipt.material_identity(), "comparison": {"equal": False}}
            return {"profile_id": profile.profile_id, "checked_at": datetime.now(timezone.utc).isoformat(),
                "current_material_identity": receipt.material_identity(), "comparison": {"equal": True}}

        def post(self, route, body):
            assert self.qualified
            calls.append(route)
            if route == "/acquire":
                return 200, {"lease": "a" * 12, "pool": "rtx4500", "url": self.profile.endpoint_url, "ttl": 600}
            return 200, {"ok": True}

        def request(self, url, body, *, lease_valid):
            assert self.qualified and lease_valid()
            calls.append("model-source-fixture")
            assert url == self.profile.endpoint_url + "/api/chat"
            return {"model": self.profile.model, "done": True, "done_reason": "stop",
                "prompt_eval_count": 1000, "eval_count": 100,
                "message": {"content": "", "tool_calls": [{"function": {"name": "write_file",
                    "arguments": {"path": "candidate.patch", "search": "PENDING_PATCH", "replacement": patch}}}]}}

        def finish_capture(self):
            calls.append("capture-cleanup-source-fixture")
            return True

    def verifier(worktree, command, **options):
        directory = Path(options["artifacts_dir"])
        out, err = directory / "stdout", directory / "stderr"
        report = {"schema": "tmos-work-verification-v1", "tests_passed": 2, "tests_failed": 0,
            "positive_control": {"expected": "PASS", "observed": "PASS"},
            "negative_control": {"expected": "FAIL", "observed": "FAIL"}}
        out.write_text(json.dumps(report)); err.write_bytes(b"")
        out.chmod(0o600); err.chmod(0o600)
        return {"allowed": True, "exit_code": 0, "timed_out": False, "error": None,
            "stdout_path": str(out), "stderr_path": str(err), "stdout_truncated": False,
            "stderr_truncated": False, "container_name": "routing-sbx-" + "a" * 12,
            "timeout_s": options["timeout_s"]}

    original_run = subprocess.run

    def docker(argv, **options):
        if argv[:3] == ["docker", "container", "ls"]:
            output = b"routing-sbx-" + b"a" * 12 if fault["mode"] == "initial-container-unknown" else b""
            return subprocess.CompletedProcess(argv, 0, stdout=output, stderr=b"")
        return original_run(argv, **options)

    monkeypatch.setattr("src.routing_sandbox.run_in_sandbox", verifier)
    monkeypatch.setattr(subprocess, "run", docker)
    retained = tmp_path / "retained"
    retained.mkdir(mode=0o700)
    app, _, _ = client_for(tmp_path, monkeypatch)
    reader = lambda **_kwargs: SimpleNamespace(profiles=estate.profiles, receipts=(), skipped=(),
        network_classes={}, capability_store=capabilities)
    runtime = await start_inspector_runtime(app, settings={"canonicalInspector": {"enabled": True}},
        sessions=sessionmaker(bind=engine, expire_on_commit=False), manifest=manifest,
        retained_root=retained, process_factory=Processes, routing_reader=reader)
    if scenario.startswith("initial-"):
        from src.canonical_inspector_admission import InspectorUnavailable
        with pytest.raises(InspectorUnavailable):
            await runtime.service.admit_tmos(principal(), template.packet_id, "owned-one", principal)
        assert "/release" not in calls
        assert calls.count("model-source-fixture") == (1 if scenario.endswith("container-unknown") else 0)
        assert await stop_inspector_runtime(app) is False
        return
    import httpx
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/harness/inspector/tmos/" + template.packet_id,
            headers={"x-test-user": "admin"}, json={"idempotency_key": "owned-one"})
    assert response.status_code == 200
    result = response.json()
    view = runtime.service.inspect(principal(), result["handle"], principal)
    assert view["mission_key"] == "TMOS-11"
    assert view["verification_state"] == "VERIFIED"
    assert view["semantic_acceptance"] == "UNKNOWN"
    assert view["routing"]["decision"]["selected"]["profile_id"] == profiles[0].profile_id
    assert view["routing"]["decision"]["selected"]["provider"] == "ollama"
    health = view["routing"]["capabilities"]["supplements"][0]
    assert health["health_state"] == "live"
    assert health["health_ttl_s"] == 300
    assert health["health_checked_at"] == capabilities.current(profiles[0].profile_id).health_checked_at
    assert calls.count("model-source-fixture") == 1
    assert calls[0] == "local-proof-source-fixture"
    assert calls.index("capture-cleanup-source-fixture") < calls.index("/release")
    if scenario.startswith("alternate"):
        import asyncio
        from datetime import timedelta
        from src.local_targets import make_target_capability_receipt
        fault["mode"] = scenario
        if scenario == "alternate-stale":
            previous = capabilities.current(profiles[1].profile_id)
            changed = make_target_capability_receipt(**{**previous.to_dict(),
                "health_checked_at": (datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()})
            capabilities.append(changed, supersedes=previous.receipt_hash)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/api/harness/inspector/attempts/" + result["handle"] + "/alternate",
                headers={"x-test-user": "admin"}, json={"dispatch_receipt_hash": view["dispatch_receipt_hash"],
                    "profile_id": profiles[1].profile_id, "reason": "Compare eligible alternate", "idempotency_key": "prefer-one"})
        assert response.status_code == 200
        requested = response.json()
        assert requested["status"] == "REQUESTED"
        for _ in range(100):
            current = runtime.service.inspect(principal(), result["handle"], principal)
            applied = [row for row in current["interventions"] if row["status"] in {"APPLIED", "UNRESOLVED"}]
            if applied:
                break
            await asyncio.sleep(0.01)
        assert len(applied) == 1
        if scenario != "alternate":
            assert applied[0]["status"] == "UNRESOLVED"
            assert applied[0]["resulting_handle"] is None
            assert applied[0]["failed_stage"] in {"routing", "lease"}
            assert calls.count("model-source-fixture") == 1
            assert calls.count("/release") == 1
            assert await stop_inspector_runtime(app) is False
            return
        changed = runtime.service.inspect(principal(), applied[0]["resulting_handle"], principal)
        assert changed["routing"]["decision"]["selected"]["profile_id"] == profiles[1].profile_id
        assert changed["attempt_receipt_hash"] != view["attempt_receipt_hash"]
        assert changed["dispatch_receipt_hash"] != view["dispatch_receipt_hash"]
        assert applied[0]["operator_request_receipt_hash"] == requested["receipt"]["receipt_hash"]
        await runtime.service.submit_alternate(principal(), result["handle"], view["dispatch_receipt_hash"],
            profiles[1].profile_id, "Compare eligible alternate", "prefer-one", principal)
        await asyncio.sleep(0.02)
        assert calls.count("model-source-fixture") == 2
    assert await stop_inspector_runtime(app) is True
