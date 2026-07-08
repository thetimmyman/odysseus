"""WP6 observability endpoint (spec Section 20 metrics), the persisted-
verification viewer endpoint, and the dataPolicy pass-through on route
previews. Same self-contained harness-app pattern as
tests/test_routing_harness_routes.py: bare FastAPI app + in-memory SQLite +
stub auth manager so require_admin_cookie runs its REAL logic."""
import json
import os
import sys
import uuid
from datetime import datetime, timedelta

import pytest
import sqlalchemy
from sqlalchemy.orm import sessionmaker
from fastapi import FastAPI
from fastapi.testclient import TestClient


# Scoped AUTH_ENABLED=false (see test_routing_harness_routes.py for why a
# module-level os.environ assignment must be avoided).
@pytest.fixture(autouse=True)
def _auth_disabled_env(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.database as cdb  # noqa: E402
import routes.routing_harness_routes as rh  # noqa: E402

ADMIN = {"x-test-user": "admin"}
NON_ADMIN = {"x-test-user": "bob"}


class _StubAuthManager:
    is_configured = True

    def is_admin(self, user):
        return user == "admin"

    def get_privileges(self, user):
        return {"security_admin": False}


def _make_app_and_client():
    engine = sqlalchemy.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    cdb.Base.metadata.create_all(bind=engine)
    rh.SessionLocal = sessionmaker(bind=engine, autoflush=False)

    app = FastAPI()
    app.state.auth_manager = _StubAuthManager()
    rh.setup_routing_harness_routes()
    app.include_router(rh.router)

    @app.middleware("http")
    async def _stamp(request, call_next):
        user = request.headers.get("x-test-user")
        if user:
            request.state.current_user = user
        return await call_next(request)

    return app, TestClient(app)


# ---------- seeding helpers ----------
def _seed_audit(parsed_ok=True, fallback_path="none", validation_errors=(),
                raw_output="{}", created_at=None, task_id="t"):
    s = rh.SessionLocal()
    row = cdb.CoordinatorAudit(
        id=str(uuid.uuid4()), task_id=task_id, schema_version="0.5",
        raw_output=raw_output, validation_errors=json.dumps(list(validation_errors)),
        fallback_path=fallback_path, applied_fallback=fallback_path != "none",
        audit_notes="[]", parsed_ok=parsed_ok,
    )
    if created_at is not None:
        row.created_at = created_at
    s.add(row)
    s.commit()
    s.close()


def _seed_model_run(scores=None, cost_usd=0.0, created_at=None,
                    task_type="bug_debug"):
    s = rh.SessionLocal()
    tid, rid, mid = (str(uuid.uuid4()) for _ in range(3))
    s.add(cdb.RoutingTask(id=tid, title="t", objective="o",
                          task_type=task_type, repo_path="."))
    s.commit()
    s.add(cdb.RoutingRun(id=rid, task_id=tid, status="succeeded"))
    s.commit()
    row = cdb.RoutingModelRun(
        id=mid, run_id=rid, cost_usd=cost_usd,
        scores=json.dumps(scores) if scores is not None else None,
    )
    if created_at is not None:
        row.created_at = created_at
    s.add(row)
    s.commit()
    s.close()
    return mid, rid, tid


def _metrics(client, days=None):
    q = f"?days={days}" if days is not None else ""
    r = client.get(f"/api/harness/observability{q}", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["metrics"]


# ---------- observability metrics ----------
def test_observability_empty_is_null_not_zero():
    _app, client = _make_app_and_client()
    m = _metrics(client)
    for key in ("costPerSuccessfulPatchUsd", "coordinatorSchemaValidityRate",
                "coordinatorFallbackRate", "policyViolationRate",
                "approvalGateMissRate"):
        assert m[key]["value"] is None, key
        assert m[key]["denominator"] in (0, None), key
    assert m["flakyTestRate"]["value"] is None
    assert "insufficient data model" in m["flakyTestRate"]["note"]


def test_schema_validity_and_fallback_rates():
    _app, client = _make_app_and_client()
    _seed_audit(parsed_ok=True, fallback_path="none")
    _seed_audit(parsed_ok=True, fallback_path="none")
    _seed_audit(parsed_ok=True, fallback_path="repair")
    _seed_audit(parsed_ok=False, fallback_path="safe_scout",
                validation_errors=["schema_version_error:bad"])
    m = _metrics(client)
    assert m["coordinatorSchemaValidityRate"] == {
        "value": 0.75, "numerator": 3, "denominator": 4,
        "note": m["coordinatorSchemaValidityRate"]["note"],
    }
    assert m["coordinatorFallbackRate"]["numerator"] == 2
    assert m["coordinatorFallbackRate"]["denominator"] == 4
    assert m["coordinatorFallbackRate"]["value"] == 0.5


def test_policy_violation_rate_counts_gate_errors_only():
    _app, client = _make_app_and_client()
    # gate errors (run_hard_gates strings) -> violations
    _seed_audit(parsed_ok=False, fallback_path="deterministic",
                validation_errors=["approval_gate_unsatisfied"])
    _seed_audit(parsed_ok=False, fallback_path="safe_scout",
                validation_errors=["restricted_data_remote_blocked",
                                   "backend_unavailable:openrouter"])
    # schema problems are NOT policy violations
    _seed_audit(parsed_ok=False, fallback_path="safe_scout",
                validation_errors=["decision_validation_error:missing_required_field:taskId"])
    _seed_audit(parsed_ok=True, fallback_path="none")
    m = _metrics(client)
    assert m["policyViolationRate"]["numerator"] == 2
    assert m["policyViolationRate"]["denominator"] == 4
    assert m["policyViolationRate"]["value"] == 0.5


def _decision_raw(approval_required):
    return json.dumps({
        "schemaVersion": "0.5", "taskId": "t",
        "approvalRecommendation": {"required": approval_required, "level": "admin"},
    })


def test_approval_gate_miss_rate_derivation():
    _app, client = _make_app_and_client()
    # Accepted decision requiring approval, gates clean -> denominator, no miss.
    _seed_audit(parsed_ok=True, fallback_path="none",
                raw_output=_decision_raw(True))
    # Accepted decision NOT requiring approval -> excluded from denominator.
    _seed_audit(parsed_ok=True, fallback_path="none",
                raw_output=_decision_raw(False))
    # Blocked (fallback) approval failure -> NOT a miss: it never executed.
    _seed_audit(parsed_ok=False, fallback_path="deterministic",
                validation_errors=["approval_gate_unsatisfied"],
                raw_output=_decision_raw(True))
    # Accepted row whose archived raw output no longer parses -> excluded + noted.
    _seed_audit(parsed_ok=True, fallback_path="repair", raw_output="not json")
    m = _metrics(client)
    appr = m["approvalGateMissRate"]
    assert appr["numerator"] == 0
    assert appr["denominator"] == 1
    assert appr["value"] == 0.0
    assert "excluded" in appr["note"]


def test_cost_per_successful_patch():
    _app, client = _make_app_and_client()
    _seed_model_run({"verification": {"passed": True, "patch_accepted": True}},
                    cost_usd=2.0)
    _seed_model_run({"verification": {"passed": False, "patch_accepted": False}},
                    cost_usd=1.0)
    _seed_model_run(None, cost_usd=0.5)  # never verified: cost still counts
    m = _metrics(client)
    cost = m["costPerSuccessfulPatchUsd"]
    assert cost["numerator"] == 3.5
    assert cost["denominator"] == 1
    assert cost["value"] == 3.5


def test_days_window_excludes_old_rows():
    _app, client = _make_app_and_client()
    _seed_audit(parsed_ok=True, fallback_path="none",
                created_at=datetime.utcnow() - timedelta(days=60))
    assert _metrics(client, days=30)["coordinatorSchemaValidityRate"]["denominator"] == 0
    assert _metrics(client, days=90)["coordinatorSchemaValidityRate"]["denominator"] == 1


# ---------- persisted-verification viewer ----------
def test_verification_viewer_returns_stored_block():
    _app, client = _make_app_and_client()
    verification = {
        "mode": "bug_fix", "passed": True, "patch_accepted": True,
        "patch_applied": True,
        "layers": [{"layer": "existing_tests", "source": "existing_tests",
                    "blocking": True, "passed": True, "skipped": False,
                    "commands": [{"cmd": "pytest -q", "exit_code": 0,
                                  "advisory": False, "origin": "task.inputs.test_commands",
                                  "tool_call_record_id": "tc1"}],
                    "notes": []}],
        "notes": [], "completed_at": "2026-07-08T00:00:00+00:00",
    }
    mid, rid, tid = _seed_model_run(
        {"patch_correctness": 4.0, "verification": verification})
    r = client.get(f"/api/harness/model-runs/{mid}/verification", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model_run_id"] == mid
    assert body["run_id"] == rid
    assert body["task_id"] == tid
    assert body["verification"] == verification


def test_verification_viewer_404s():
    _app, client = _make_app_and_client()
    # Unknown model run.
    r = client.get("/api/harness/model-runs/nope/verification", headers=ADMIN)
    assert r.status_code == 404
    # Known run, never verified (scores present but no verification block).
    mid, _rid, _tid = _seed_model_run({"patch_correctness": 3.0})
    r2 = client.get(f"/api/harness/model-runs/{mid}/verification", headers=ADMIN)
    assert r2.status_code == 404
    assert "verification" in r2.json()["detail"]


# ---------- dataPolicy pass-through (WP3 known gap) ----------
def test_route_preview_passes_data_policy_through():
    _app, client = _make_app_and_client()
    r = client.post("/api/harness/route/preview", headers=ADMIN, json={"task": {
        "id": "dp-task", "title": "t", "objective": "o",
        "type": "diff_review", "repoPath": ".", "dataSensitivity": "restricted",
    }})
    assert r.status_code == 200, r.text
    dp = r.json().get("dataPolicy")
    assert dp is not None, "route/preview must not drop route_task's dataPolicy block"
    assert dp["sensitivity"] == "restricted"
    assert dp["localOnly"] is True
    assert "remoteCandidatesExcluded" in dp


# ---------- auth gating (AUTH_ENABLED=true, real gate code paths) ----------
class TestObservabilityAuthGating:
    def setup_method(self):
        os.environ["AUTH_ENABLED"] = "true"

    def teardown_method(self):
        os.environ["AUTH_ENABLED"] = "false"

    def test_admin_gate_enforced(self):
        _app, client = _make_app_and_client()
        for headers, expected in ((({}), 403), (NON_ADMIN, 403)):
            assert client.get("/api/harness/observability",
                              headers=headers).status_code == expected
            assert client.get("/api/harness/model-runs/x/verification",
                              headers=headers).status_code == expected
        assert client.get("/api/harness/observability", headers=ADMIN).status_code == 200
        # Admin on the viewer hits the 404 (row missing), not a 403.
        assert client.get("/api/harness/model-runs/x/verification",
                          headers=ADMIN).status_code == 404
