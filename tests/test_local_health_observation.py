"""The maintained collector produces observations for the normal refresh CLI."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src import local_target_identity as identity
from src.local_targets import ContextProfile, HostBaseline, ModelIdentity, RuntimeIdentity
from src.target_capability_store import TargetCapabilityStore
from test_local_target_identity import fixture as native_fixture
from test_ps632_profile_registry import measured


def prepare(tmp_path, monkeypatch, *, expired=False):
    raw, material, request = native_fixture()
    checked = datetime.now(timezone.utc) - timedelta(seconds=2)
    raw["observed_at"] = (checked + timedelta(seconds=1)).isoformat()
    raw["provider_health"] = {"healthy": True, "checked_at": checked.isoformat()}
    receipt = measured(
        host_id=material["host_id"],
        runtime=RuntimeIdentity(**material["runtime"]),
        model=ModelIdentity(**material["model"]),
        context=ContextProfile(**{**material["context"], "safe_working_context": 4096,
                                 "semantic_verified_context": 4096,
                                 "safe_context_source": "synthetic-measurement"}),
        host=HostBaseline(**material["host"]),
        observed_at=(checked - timedelta(seconds=90000 if expired else 600)).isoformat(),
        health_checked_at=(checked - timedelta(seconds=600)).isoformat(),
        ttl_s=86400,
    )
    receipt_file, config_file = tmp_path / "receipt.json", tmp_path / "config.json"
    receipt_file.write_text(json.dumps(receipt.to_dict()))
    config_file.write_text(json.dumps({"request_options": request,
                                      "endpoint_url": raw["endpoint_url"]}))
    monkeypatch.setattr(identity, "capture", lambda config: copy.deepcopy(raw))
    return raw, receipt, ["--receipt", str(receipt_file), "--config", str(config_file),
                          "--health-observation"]


@pytest.mark.parametrize("expired", [False, True])
def test_collector_observation_refreshes_liveness_without_renewing_qualification(
        tmp_path, monkeypatch, capsys, expired):
    raw, receipt, args = prepare(tmp_path, monkeypatch, expired=expired)
    store = TargetCapabilityStore(str(tmp_path / "store"))
    store.append(receipt)
    active_before = Path(store.active_path).read_bytes()
    ledger_before = Path(store.receipts_path).read_bytes()

    assert identity.main(args) == 0
    observation = json.loads(capsys.readouterr().out)
    assert observation["health"] == "healthy"
    assert observation["checked_at"] == raw["provider_health"]["checked_at"]
    observation_file = tmp_path / "observation.json"
    observation_file.write_text(json.dumps(observation))
    cli = Path(__file__).resolve().parents[1] / "scripts" / "odysseus-capability"
    result = subprocess.run([sys.executable, str(cli), "--store", store.directory,
                             "refresh", str(observation_file)],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    refreshed = store.current(receipt.profile_id)
    assert refreshed.health_state() == "live"
    assert refreshed.health_checked_at == raw["provider_health"]["checked_at"]
    assert refreshed.qualification_material() == receipt.qualification_material()
    assert refreshed.profile_id == receipt.profile_id
    assert refreshed.qualification_state() == ("qualification_expired" if expired else "valid")
    assert Path(store.active_path).read_bytes() == active_before
    assert Path(store.receipts_path).read_bytes().startswith(ledger_before)
    assert store.verify()["ok"]


@pytest.mark.parametrize("clock", ["stale", "future", "invalid", "missing", "after_capture"])
def test_health_mode_refuses_unusable_response_clock_without_output(
        tmp_path, monkeypatch, capsys, clock):
    raw, _, args = prepare(tmp_path, monkeypatch)
    if clock == "stale":
        raw["provider_health"]["checked_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=301)).isoformat()
    elif clock == "future":
        raw["provider_health"]["checked_at"] = (
            datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    elif clock == "invalid":
        raw["provider_health"]["checked_at"] = "not-a-timestamp"
    elif clock == "missing":
        del raw["provider_health"]["checked_at"]
    else:
        raw["provider_health"]["checked_at"] = (
            datetime.fromisoformat(raw["observed_at"]) + timedelta(milliseconds=500)).isoformat()
    with pytest.raises(ValueError):
        identity.main(args)
    assert capsys.readouterr().out == ""


def test_health_mode_refuses_drift_that_default_identity_mode_can_still_explain(
        tmp_path, monkeypatch, capsys):
    raw, _, args = prepare(tmp_path, monkeypatch)
    raw["host_baseline"]["kernel"] = "changed-kernel"
    assert identity.main(args[:-1]) == 0
    assert json.loads(capsys.readouterr().out)["comparison"]["equal"] is False
    with pytest.raises(ValueError):
        identity.main(args)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("proof", [None, {}, {"healthy": False}, {"healthy": "true"}])
def test_health_mode_requires_explicit_successful_health_evidence(
        tmp_path, monkeypatch, capsys, proof):
    raw, _, args = prepare(tmp_path, monkeypatch)
    if proof is None:
        del raw["provider_health"]
    else:
        raw["provider_health"] = proof
    with pytest.raises(ValueError):
        identity.main(args)
    assert capsys.readouterr().out == ""


def test_capture_failure_emits_no_health_observation(tmp_path, monkeypatch, capsys):
    _, _, args = prepare(tmp_path, monkeypatch)

    def failed_capture(config):
        raise ValueError("synthetic provider failure")

    monkeypatch.setattr(identity, "capture", failed_capture)
    with pytest.raises(ValueError):
        identity.main(args)
    assert capsys.readouterr().out == ""


def test_health_mode_respects_a_stricter_receipt_liveness_window(tmp_path, monkeypatch, capsys):
    raw, _, args = prepare(tmp_path, monkeypatch)
    receipt_file = Path(args[1])
    receipt = json.loads(receipt_file.read_text())
    receipt["health_ttl_s"] = 1
    receipt_file.write_text(json.dumps(receipt))
    assert raw["provider_health"]["checked_at"] < raw["observed_at"]
    with pytest.raises(ValueError):
        identity.main(args)
    assert capsys.readouterr().out == ""


def test_native_capture_keeps_response_time_before_final_artifact_checks(monkeypatch):
    raw, _, _ = native_fixture("halogen-flash")
    health = {**raw["health"], "chat_template": {"path": "/fixture/template", "sha256": "template"}}
    start = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)

    class Clock(datetime):
        current = start

        @classmethod
        def now(cls, zone):
            return cls.current

    container = {"Id": "fixture-container", "Image": "image", "ImageDigest": "image",
                 "State": {"Running": True, "Pid": 1, "StartedAt": "fixture-start"},
                 "Config": {"Env": ["CHECKPOINT=/fixture/checkpoint"]}, "Mounts": [],
                 "NetworkSettings": {"Ports": {"9/tcp": [{"HostPort": "9", "HostIp": "0.0.0.0"}]}}}
    responses = []

    def metadata(base, path="", body=None):
        assert base in ("http://127.0.0.1:9/health", "http://127.0.0.1:9")
        assert path in ("", "/health") and body is None
        responses.append(Clock.current)
        return copy.deepcopy(health)

    def command(argv, *, binary=False):
        if argv == ["ip", "-j", "addr", "show"]:
            return "[]"
        if argv == ["fixture-gpu"]:
            return "gpu"
        if argv == ["fixture-engine", "inspect", "fixture-container"]:
            return json.dumps([container])
        assert argv[:3] == ["fixture-engine", "exec", "fixture-container"]
        action = argv[3:]
        if action[:2] == ["stat", "-c"]:
            if len(responses) == 2:
                Clock.current = start + timedelta(seconds=10)
            return "10:1:2:3"
        assert action[0] == "sha256sum"
        return ("template" if action[-1].endswith("template") else "model-digest") + "  fixture"

    monkeypatch.setattr(identity, "datetime", Clock)
    monkeypatch.setattr(identity, "_run", command)
    monkeypatch.setattr(identity, "_api", metadata)
    monkeypatch.setattr(identity, "_host", lambda: {"cpu_arch": "arch", "kernel": "kernel",
                                                   "boot_cmdline_digest": "boot"})
    monkeypatch.setattr(identity.socket, "getaddrinfo",
                        lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 9))])
    observation = identity.capture({"kind": "halogen-flash", "engine_argv": ["fixture-engine"],
        "container": "fixture-container", "health_url": "http://127.0.0.1:9/health",
        "endpoint_url": "http://127.0.0.1:9/v1", "checkpoint_env_key": "CHECKPOINT",
        "checkpoint": "/fixture/checkpoint", "template": "/fixture/template",
        "gpu_query_argv": ["fixture-gpu"]})
    assert observation["provider_health"] == {"healthy": True, "checked_at": start.isoformat()}
    assert datetime.fromisoformat(observation["observed_at"]) == start + timedelta(seconds=10)
