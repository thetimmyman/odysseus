"""Phase 7 ABSIS integration (src/routing_absis.py + scripts/odysseus-absis).

Everything here runs with the transport stubbed (subprocess.run monkeypatched
or a fake transport object) — no test ever actually sshs anywhere. The wire
format is pinned against absis_infra.schemas.Job's real serialization
(json.dumps(asdict(job), sort_keys=True), enums as .value), because a drifted
field name (timeout vs timeout_s) would enqueue jobs no worker can parse.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import shlex
from pathlib import Path
from types import SimpleNamespace

import pytest

import src.routing_absis as ra
from src.routing_absis import (
    AbsisJobSpec,
    AbsisTransport,
    AbsisTransportError,
    AbsisValidationError,
    build_enqueue_script,
    build_get_status_script,
    build_scan_workers_script,
    check_availability,
    enqueue,
    get_status,
    map_job_to_model_run,
)

ROOT = Path(__file__).resolve().parents[1]

KUBECTL_PREFIX = "sudo kubectl exec -n tacticus deploy/absis-orchestrator --"


# --- helpers -----------------------------------------------------------------

class FakeTransport:
    """Scripted transport: returns canned results per call, records scripts."""

    def __init__(self, results):
        self.results = list(results)
        self.scripts = []

    def run_remote_python(self, script):
        self.scripts.append(script)
        return self.results.pop(0)


def _worker(worker_id="w1", worker_class="llm_inference", capabilities=()):
    return {"worker_id": worker_id, "worker_class": worker_class,
            "capabilities": list(capabilities)}


# --- wire format -------------------------------------------------------------

EXPECTED_WIRE_FIELDS = {
    "scenario_id", "required_worker_class", "required_capabilities",
    "priority", "timeout_s", "job_id", "created_at", "payload",
    "status", "assigned_worker_id", "attempts", "last_error",
}


def test_wire_format_exact_fields_and_defaults():
    spec = AbsisJobSpec(scenario_id="scenario-042",
                        required_worker_class="llm_inference",
                        required_capabilities=["gpu"],
                        timeout_s=120,
                        payload={"prompt": "hi"})
    wire = spec.to_job_json()
    job = json.loads(wire)
    # Exact field set — a missing or extra key breaks the real Job asdict shape.
    assert set(job) == EXPECTED_WIRE_FIELDS
    # timeout_s, NOT timeout — the historical drift this test exists to catch.
    assert "timeout" not in job and job["timeout_s"] == 120
    # Enum-valued fields serialize as their .value strings.
    assert job["required_worker_class"] == "llm_inference"
    assert job["status"] == "queued"
    # Defaults exactly as the real dataclass fills them.
    assert job["priority"] == 0
    assert job["attempts"] == 0
    assert job["assigned_worker_id"] is None
    assert job["last_error"] is None
    assert isinstance(job["created_at"], float)
    assert job["payload"] == {"prompt": "hi"}
    assert job["required_capabilities"] == ["gpu"]
    # job_id is str(uuid.uuid4()) — hex WITH dashes.
    assert len(job["job_id"]) == 36 and job["job_id"].count("-") == 4


def test_wire_format_is_sort_keys_serialization():
    spec = AbsisJobSpec(scenario_id="s1", required_worker_class="oracle_runner")
    wire = spec.to_job_json()
    assert wire == json.dumps(json.loads(wire), sort_keys=True)
    keys = list(json.loads(wire, object_pairs_hook=lambda p: [k for k, _ in p]))
    assert keys == sorted(keys)


def test_each_serialization_mints_fresh_job_id():
    spec = AbsisJobSpec(scenario_id="s1", required_worker_class="llm_inference")
    assert json.loads(spec.to_job_json())["job_id"] != json.loads(spec.to_job_json())["job_id"]


def test_spec_defaults_match_job_defaults():
    spec = AbsisJobSpec(scenario_id="s1", required_worker_class="llm_inference")
    job = spec.to_job_dict()
    assert job["timeout_s"] == 600
    assert job["required_capabilities"] == []
    assert job["payload"] == {}


# --- input validation (reject, never escape) ----------------------------------

@pytest.mark.parametrize("bad", [
    'scen"ario', "scen'ario", "scen\\ario", "scen\nario", "scen\rario",
    "scenario with space", "", "scen`ario", "scen$ario",
])
def test_scenario_id_with_dangerous_chars_rejected(bad):
    with pytest.raises(AbsisValidationError):
        AbsisJobSpec(scenario_id=bad, required_worker_class="llm_inference")


@pytest.mark.parametrize("bad", ['g"pu', "g'pu", "g\\pu", "g\npu", "g\rpu", ""])
def test_capability_with_dangerous_chars_rejected(bad):
    with pytest.raises(AbsisValidationError):
        AbsisJobSpec(scenario_id="s1", required_worker_class="llm_inference",
                     required_capabilities=[bad])


def test_bad_worker_class_rejected():
    with pytest.raises(AbsisValidationError):
        AbsisJobSpec(scenario_id="s1", required_worker_class="grid_compute")
    with pytest.raises(AbsisValidationError):
        check_availability(FakeTransport([]), "grid_compute")


def test_bad_timeout_and_payload_rejected():
    with pytest.raises(AbsisValidationError):
        AbsisJobSpec(scenario_id="s1", required_worker_class="llm_inference", timeout_s=0)
    with pytest.raises(AbsisValidationError):
        AbsisJobSpec(scenario_id="s1", required_worker_class="llm_inference",
                     payload=["not", "a", "dict"])


@pytest.mark.parametrize("bad", ['abc"def', "abc'def", "x\ny", "$(reboot)", "a;b", ""])
def test_job_id_injection_rejected(bad):
    with pytest.raises(AbsisValidationError):
        build_get_status_script(bad)
    with pytest.raises(AbsisValidationError):
        get_status(FakeTransport([]), bad)


# --- ssh argv / remote-script construction ------------------------------------

def test_ssh_argv_construction(monkeypatch):
    calls = []

    def fake_run(argv, capture_output, text, timeout):
        calls.append(SimpleNamespace(argv=argv, timeout=timeout))
        return SimpleNamespace(returncode=0, stdout='{"ok": true}\n', stderr="")

    monkeypatch.setattr(ra.subprocess, "run", fake_run)
    t = AbsisTransport(ssh_target="minipc", kubectl_exec_prefix=KUBECTL_PREFIX, timeout_s=30)
    script = 'print("{}")'
    result = t.run_remote_python(script)
    assert result == {"ok": True}
    assert len(calls) == 1
    argv = calls[0].argv
    # argv list, never a shell string: ["ssh", "minipc", <remote command>].
    assert isinstance(argv, list) and len(argv) == 3
    assert argv[:2] == ["ssh", "minipc"]
    # Remote command = kubectl exec prefix + python -c + the shlex-quoted script.
    assert argv[2] == f"{KUBECTL_PREFIX} python -c {shlex.quote(script)}"
    assert calls[0].timeout == 30


def test_transport_never_uses_shell(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr(ra.subprocess, "run", fake_run)
    AbsisTransport().run_remote_python("print(1)")
    assert isinstance(seen["argv"], list)
    assert "shell" not in seen["kwargs"] or seen["kwargs"]["shell"] is False


def test_transport_error_paths(monkeypatch):
    def fail_run(argv, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(ra.subprocess, "run", fail_run)
    with pytest.raises(AbsisTransportError, match="exited 1"):
        AbsisTransport().run_remote_python("print(1)")

    monkeypatch.setattr(ra.subprocess, "run",
                        lambda argv, **kw: SimpleNamespace(returncode=0, stdout="not json",
                                                           stderr=""))
    with pytest.raises(AbsisTransportError, match="non-JSON"):
        AbsisTransport().run_remote_python("print(1)")

    def timeout_run(argv, **kwargs):
        raise ra.subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout"))

    monkeypatch.setattr(ra.subprocess, "run", timeout_run)
    with pytest.raises(AbsisTransportError, match="timed out"):
        AbsisTransport(timeout_s=1).run_remote_python("print(1)")


def test_enqueue_script_embeds_wire_as_json_literal_no_raw_concat():
    spec = AbsisJobSpec(scenario_id="scenario-042",
                        required_worker_class="llm_inference",
                        required_capabilities=["gpu"],
                        payload={"note": "tricky \" quote and \\ backslash\nnewline"})
    wire = json.dumps(spec.to_job_dict(), sort_keys=True)
    script = build_enqueue_script(wire)
    # The wire string is embedded exactly as a json.dumps literal (which is a
    # valid Python string literal), then json.loads'd remotely.
    assert f"wire = {json.dumps(wire)}" in script
    assert "json.loads(wire)" in script
    # No raw (unescaped) user text concatenated into the script: the payload's
    # quote/backslash/newline arrive only inside the escaped literal.
    embedded_literal = json.dumps(wire)
    assert embedded_literal in script
    stripped = script.replace(embedded_literal, "")
    assert "tricky" not in stripped  # payload text exists ONLY inside the literal
    # All three enqueue ops against the verified key names.
    assert 'r.lpush("conformance:jobs", wire)' in script
    assert 'r.set("conformance:job:" + job["job_id"], wire)' in script
    assert 'r.publish("conformance:status", wire)' in script
    # Round-trip: the embedded literal evaluates back to the exact wire JSON.
    assert json.loads(embedded_literal) == wire


def test_enqueue_script_is_shell_quoted_end_to_end(monkeypatch):
    """The full remote command must be a single shlex-quoted argument even
    when the script body contains quotes/newlines (it always does)."""
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        return SimpleNamespace(returncode=0, stdout='{"enqueued": true, "job_id": "x"}',
                               stderr="")

    monkeypatch.setattr(ra.subprocess, "run", fake_run)
    spec = AbsisJobSpec(scenario_id="s1", required_worker_class="llm_inference")
    t = AbsisTransport(ssh_target="minipc", kubectl_exec_prefix=KUBECTL_PREFIX)
    enqueue(t, spec, force=True)
    remote_cmd = captured["argv"][2]
    prefix = f"{KUBECTL_PREFIX} python -c "
    assert remote_cmd.startswith(prefix)
    quoted = remote_cmd[len(prefix):]
    # shlex round-trip: the quoted blob parses back to exactly one shell word
    # (the script), even though the script body contains quotes and newlines.
    words = shlex.split(quoted)
    assert len(words) == 1
    assert "json.loads(wire)" in words[0]


def test_get_status_script_embeds_job_id_via_json():
    job_id = "0f8fad5b-d9cb-469f-a165-70867728950e"
    script = build_get_status_script(job_id)
    assert f"job_id = {json.dumps(job_id)}" in script
    assert 'r.get("conformance:job:" + job_id)' in script


def test_scan_script_targets_worker_keys():
    script = build_scan_workers_script()
    assert 'match="conformance:worker:*"' in script
    assert "registered_workers" in script
    assert 'os.environ["REDIS_URL"]' in script


# --- check_availability -------------------------------------------------------

def test_availability_no_workers():
    t = FakeTransport([{"registered_workers": []}])
    out = check_availability(t, "llm_inference")
    assert out == {"available": False, "workers": [], "reason": "no_workers_registered"}


def test_availability_capability_superset_matches():
    t = FakeTransport([{"registered_workers": [
        _worker("w1", "llm_inference", ["gpu", "cuda", "fp8"]),
        _worker("w2", "oracle_runner", ["gpu"]),
    ]}])
    out = check_availability(t, "llm_inference", ["gpu", "cuda"])
    assert out["available"] is True
    assert out["reason"] is None
    assert {w["worker_id"] for w in out["workers"]} == {"w1", "w2"}


def test_availability_capability_subset_does_not_match():
    t = FakeTransport([{"registered_workers": [
        _worker("w1", "llm_inference", ["gpu"]),
    ]}])
    out = check_availability(t, "llm_inference", ["gpu", "cuda"])
    assert out["available"] is False
    assert out["reason"] == "no_matching_worker"


def test_availability_class_mismatch():
    t = FakeTransport([{"registered_workers": [_worker("w1", "oracle_runner")]}])
    out = check_availability(t, "llm_inference")
    assert out["available"] is False
    assert out["reason"] == "no_matching_worker"


def test_availability_no_required_capabilities_matches_bare_worker():
    t = FakeTransport([{"registered_workers": [_worker("w1", "llm_inference", [])]}])
    out = check_availability(t, "llm_inference")
    assert out["available"] is True


def test_availability_tolerates_malformed_worker_records():
    t = FakeTransport([{"registered_workers": [
        "garbage", {"worker_class": "llm_inference"},  # no id / no caps
    ]}])
    out = check_availability(t, "llm_inference")
    assert out["available"] is True  # class matches, no caps required
    assert len(out["workers"]) == 1


# --- enqueue gating -----------------------------------------------------------

def test_enqueue_refuses_without_matching_worker():
    t = FakeTransport([{"registered_workers": []}])
    spec = AbsisJobSpec(scenario_id="s1", required_worker_class="llm_inference")
    out = enqueue(t, spec)
    assert out["enqueued"] is False
    assert out["job_id"] is None
    assert out["error"] == "no_matching_worker"
    # Only the scan ran — the enqueue one-shot must never fire.
    assert len(t.scripts) == 1
    assert "conformance:worker:*" in t.scripts[0]
    assert "lpush" not in t.scripts[0]


def test_enqueue_force_bypasses_availability_gate():
    t = FakeTransport([{"enqueued": True, "job_id": "ignored"}])
    spec = AbsisJobSpec(scenario_id="s1", required_worker_class="llm_inference")
    out = enqueue(t, spec, force=True)
    assert out["enqueued"] is True and out["error"] is None
    assert len(t.scripts) == 1  # went straight to the enqueue script, no scan
    assert 'r.lpush("conformance:jobs", wire)' in t.scripts[0]
    # The job_id returned is the one embedded in the wire we shipped.
    embedded = json.loads(json.loads(t.scripts[0].split("wire = ", 1)[1].splitlines()[0]))
    assert out["job_id"] == embedded["job_id"]


def test_enqueue_proceeds_when_worker_available():
    t = FakeTransport([
        {"registered_workers": [_worker("w1", "llm_inference", ["gpu"])]},
        {"enqueued": True, "job_id": "ignored"},
    ])
    spec = AbsisJobSpec(scenario_id="s1", required_worker_class="llm_inference",
                        required_capabilities=["gpu"])
    out = enqueue(t, spec)
    assert out["enqueued"] is True
    assert len(t.scripts) == 2


# --- status / wait ------------------------------------------------------------

def test_get_status_found_and_not_found():
    job = {"job_id": "0f8fad5b-d9cb-469f-a165-70867728950e", "status": "running"}
    t = FakeTransport([{"found": True, "job": job}])
    assert get_status(t, job["job_id"]) == job

    t = FakeTransport([{"found": False, "job": None}])
    out = get_status(t, job["job_id"])
    assert out["error"] == "not_found"


def test_wait_for_terminal_polls_until_completed(monkeypatch):
    monkeypatch.setattr(ra.time, "sleep", lambda s: None)
    job_id = "0f8fad5b-d9cb-469f-a165-70867728950e"
    t = FakeTransport([
        {"found": True, "job": {"job_id": job_id, "status": "queued"}},
        {"found": True, "job": {"job_id": job_id, "status": "running"}},
        {"found": True, "job": {"job_id": job_id, "status": "completed", "payload": {"r": 1}}},
    ])
    out = ra.wait_for_terminal(t, job_id, timeout_s=60, poll_interval=0)
    assert out["status"] == "completed"
    assert len(t.scripts) == 3


def test_wait_for_terminal_times_out(monkeypatch):
    monkeypatch.setattr(ra.time, "sleep", lambda s: None)
    clock = {"now": 0.0}
    monkeypatch.setattr(ra.time, "monotonic", lambda: clock.__setitem__("now", clock["now"] + 3) or clock["now"])
    job_id = "0f8fad5b-d9cb-469f-a165-70867728950e"
    t = FakeTransport([{"found": True, "job": {"job_id": job_id, "status": "running"}}] * 50)
    out = ra.wait_for_terminal(t, job_id, timeout_s=10, poll_interval=0)
    assert out["error"] == "timeout"
    assert out["last_status"] == "running"


# --- map_job_to_model_run -----------------------------------------------------

def test_map_completed_job():
    out = map_job_to_model_run({
        "job_id": "j1", "status": "completed", "attempts": 1,
        "assigned_worker_id": "w1", "last_error": None,
        "payload": {"result": 42},
    })
    assert out["completed"] is True and out["errored"] is False
    assert out["error_message"] is None
    assert out["artifacts"] == {"absis_payload": {"result": 42}}
    assert out["notes"].startswith("absis job j1")


def test_map_failed_job():
    out = map_job_to_model_run({
        "job_id": "j2", "status": "failed", "attempts": 5,
        "assigned_worker_id": None, "last_error": "no worker claimed job",
        "payload": {},
    })
    assert out["completed"] is False and out["errored"] is True
    assert out["error_message"] == "no worker claimed job"
    assert "j2" in out["notes"] and "failed" in out["notes"]


def test_map_running_job_is_neither_completed_nor_errored():
    out = map_job_to_model_run({"job_id": "j3", "status": "running", "payload": None})
    assert out["completed"] is False and out["errored"] is False
    assert out["artifacts"] == {"absis_payload": {}}


# --- CLI ----------------------------------------------------------------------

def _load_cli():
    path = ROOT / "scripts" / "odysseus-absis"
    loader = importlib.machinery.SourceFileLoader("odysseus_absis_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_cli_disabled_by_policy_message(monkeypatch, capsys):
    cli = _load_cli()
    monkeypatch.setattr(cli, "load_absis_policy", lambda: {
        "enabled": False, "sshTarget": "minipc",
        "kubectlExecPrefix": KUBECTL_PREFIX, "transportTimeoutSeconds": 30,
        "note": "no workers deployed",
    })
    parser = cli._build_parser()
    args = parser.parse_args(["workers"])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 3
    err = capsys.readouterr().err
    assert "DISABLED by policy" in err
    assert "--force-enabled" in err
    assert "no workers deployed" in err


def test_cli_force_enabled_overrides_policy_gate(monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr(cli, "load_absis_policy", lambda: dict(
        ra.DEFAULT_ABSIS_POLICY, enabled=False, sshTarget="elsewhere",
        transportTimeoutSeconds=7))
    parser = cli._build_parser()
    args = parser.parse_args(["workers", "--force-enabled"])
    t = cli._transport(args)
    assert isinstance(t, AbsisTransport)
    assert t.ssh_target == "elsewhere"
    assert t.timeout_s == 7


def test_cli_workers_emits_availability(monkeypatch, capsys):
    cli = _load_cli()
    monkeypatch.setattr(cli, "load_absis_policy",
                        lambda: dict(ra.DEFAULT_ABSIS_POLICY, enabled=True))
    fake = FakeTransport([{"registered_workers": [_worker("w1", "llm_inference", ["gpu"])]}])
    monkeypatch.setattr(cli.AbsisTransport, "from_policy", classmethod(lambda c, cfg=None: fake))
    emitted = []
    monkeypatch.setattr(cli, "emit", lambda obj, args: emitted.append(obj))
    parser = cli._build_parser()
    args = parser.parse_args(["workers", "--worker-class", "llm_inference"])
    args.func(args)
    assert emitted[0]["available"] is True


def test_cli_workers_no_class_summarizes_both_classes(monkeypatch):
    cli = _load_cli()
    monkeypatch.setattr(cli, "load_absis_policy",
                        lambda: dict(ra.DEFAULT_ABSIS_POLICY, enabled=True))
    fake = FakeTransport([{"registered_workers": [_worker("w1", "oracle_runner")]}])
    monkeypatch.setattr(cli.AbsisTransport, "from_policy", classmethod(lambda c, cfg=None: fake))
    emitted = []
    monkeypatch.setattr(cli, "emit", lambda obj, args: emitted.append(obj))
    parser = cli._build_parser()
    args = parser.parse_args(["workers"])
    args.func(args)
    assert emitted[0]["available_by_class"] == {
        "llm_inference": False, "oracle_runner": True}
    assert len(fake.scripts) == 1  # one scan for the whole summary


def test_default_absis_policy_matches_routing_policy_defaults():
    """DEFAULT_ABSIS_POLICY (module fallback) and routing_policy.DEFAULT_POLICY
    ["absis"] must not drift — the module fallback exists only for policy
    files that predate the absis section."""
    from src.routing_policy import DEFAULT_POLICY
    assert DEFAULT_POLICY["absis"] == ra.DEFAULT_ABSIS_POLICY
    assert DEFAULT_POLICY["absis"]["enabled"] is False
