import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.routing_outcomes import export_task, meaningful_verification


def verification(**overrides):
    value = {"mode": "regression_guard", "passed": True, "patch_applied": True,
             "patch_sha256": hashlib.sha256(b"patch").hexdigest(),
             "layers": [{"layer": "existing_tests", "blocking": True, "passed": True, "skipped": False,
                         "commands": [{"exit_code": 0, "tool_call_record_id": "tool1"}]}]}
    value.update(overrides)
    return value


@pytest.mark.parametrize("change", [
    {"mode": "analysis_only"}, {"layers": []}, {"passed": False},
    {"infrastructure_error": "docker_unavailable"},
    {"layers": [{"blocking": True, "passed": True, "skipped": True, "commands": []}]},
    {"layers": [{"blocking": True, "passed": True, "commands": [{"exit_code": 0}]}]},
])
def test_meaningless_verification_rejected(change):
    assert not meaningful_verification(verification(**change))


class Query:
    def __init__(self, rows): self.rows = rows
    def filter(self, *args): return self
    def all(self): return self.rows


@pytest.fixture
def setup(tmp_path, monkeypatch):
    from core.database import RoutingTask, RoutingRun, ToolCallRecord
    root = tmp_path / "export"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"directory": str(root), "cohort": "coding",
                                  "profiles": {"p": "codex", "other": "claude-code"}}))
    monkeypatch.setenv("ODYSSEUS_USAGE_EXPORT_CONFIG", str(config))
    task = SimpleNamespace(id="task1", created_at="2026-09-24T00:00:00Z")
    row = SimpleNamespace(id="attempt1", run_id="run1", model_profile_id="p",
        created_at="2026-09-24T01:00:00Z", latency_ms=100, errored=False, rate_limited=False,
        scores=json.dumps({"verification": verification()}),
        artifacts=json.dumps({"inference_attempted": True, "upstream_error": False}))
    rows = [row]
    check = SimpleNamespace(run_id="run1", allowed=True, exit_code=0, completed_at="2026-09-24T02:00:00Z")
    db = SimpleNamespace(get=lambda model, id: check if model is ToolCallRecord else task,
        query=lambda model: Query([SimpleNamespace(id="run1")] if model is RoutingRun else rows))
    monkeypatch.setattr("src.routing_verification.load_patch_text", lambda r: "patch")
    return db, rows, root


def events(root):
    return [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]


def test_response_is_not_validated_and_replay_is_idempotent(setup):
    db, rows, root = setup
    export_task(db, "task1")
    export_task(db, "task1")
    assert [e["type"] for e in events(root)] == ["task_registered", "turns_recorded"]
    assert os.stat(root / "events.jsonl").st_mode & 0o777 == 0o600


def test_acceptance_requires_checks_and_reviewer_then_evidence_hash_matches(setup):
    db, rows, root = setup
    attestation = {"model_run_id": "attempt1", "reviewer": "human", "reason": "reviewed change"}
    export_task(db, "task1", acceptance=attestation)
    export_task(db, "task1", acceptance=attestation)
    finals = [e for e in events(root) if e["type"] == "task_finalized"]
    assert len(finals) == 1 and finals[0]["status"] == "validated"
    assert hashlib.sha256(Path(finals[0]["evidence_path"]).read_bytes()).hexdigest() == finals[0]["evidence_sha256"]
    rows[0].scores = json.dumps({"verification": verification(layers=[])})
    with pytest.raises(ValueError, match="meaningful"):
        export_task(db, "task1", acceptance=attestation)


def test_mixed_provider_revokes_prior_credit(setup):
    db, rows, root = setup
    export_task(db, "task1", acceptance={"model_run_id": "attempt1", "reviewer": "human", "reason": "ok"})
    rows.append(SimpleNamespace(**{**vars(rows[0]), "id": "attempt2", "model_profile_id": "other"}))
    export_task(db, "task1")
    assert [e["type"] for e in events(root)][-2:] == ["task_reopened", "task_finalized"]
    assert events(root)[-1]["status"] == "abandoned"
    assert export_task(db, "task1")["task"]["excluded"]


def test_failed_checks_then_retry_records_rework_and_no_double_count(setup):
    db, rows, root = setup
    rows[0].scores = json.dumps({"verification": verification(passed=False,
        layers=[{"layer": "existing_tests", "blocking": True, "passed": False,
                 "commands": [{"exit_code": 1, "tool_call_record_id": "tool1"}]}])})
    export_task(db, "task1")
    rows.append(SimpleNamespace(**{**vars(rows[0]), "id": "attempt2", "errored": False,
                                 "scores": "{}", "artifacts": json.dumps({"inference_attempted": True})}))
    export_task(db, "task1")
    result = events(root)
    assert sum(e.get("turns", 0) for e in result) == 2
    assert sum(e.get("errors", 0) for e in result) == 0
    assert sum(e["type"] == "task_reopened" for e in result) == 1


def test_transport_failures_are_errors_not_failed_tasks(setup):
    db, rows, root = setup
    rows[0].errored = True
    rows[0].artifacts = json.dumps({"inference_attempted": True, "upstream_error": True})
    export_task(db, "task1")
    assert sum(e.get("errors", 0) for e in events(root)) == 1
    assert not any(e["type"] == "task_finalized" for e in events(root))


def test_partial_required_blocking_coverage_is_not_meaningful():
    assert not meaningful_verification(verification(mode="feature_addition"))


def test_nonprivate_existing_directory_rejected(setup):
    db, rows, root = setup
    root.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private"):
        export_task(db, "task1")


def test_whitespace_reviewer_is_not_acceptance(setup):
    db, rows, root = setup
    with pytest.raises(ValueError, match="reviewer"):
        export_task(db, "task1", acceptance={"model_run_id": "attempt1", "reviewer": " ", "reason": "ok"})


def test_changed_verified_patch_rejected(setup, monkeypatch):
    db, rows, root = setup
    monkeypatch.setattr("src.routing_verification.load_patch_text", lambda r: "different")
    with pytest.raises(ValueError, match="differs"):
        export_task(db, "task1", acceptance={"model_run_id": "attempt1", "reviewer": "human", "reason": "ok"})
