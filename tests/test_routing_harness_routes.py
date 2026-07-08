"""
Integration tests for the v0.5 routing harness REST surfaces.

Built self-contained: spins up a bare FastAPI app with ONLY the harness
router + an in-memory SQLite DB, avoiding the heavyweight app.py import
chain. A stub auth_manager + a header-driven middleware simulate the auth
middleware's request.state stamping, so require_admin_cookie /
require_security_admin run their REAL logic in every test.

Guarantees exercised here:
  - deterministic coordinator wrap audit (schemaVersion enforced, fail-closed,
    redaction + HMAC + policy versions on the audit row, 413 size ceiling)
  - route/budget previews are read-only (no rows written)
  - policy publish/versions/rollback lifecycle (archived + logged)
  - registry CRUD (delete refused once runs reference a profile)
  - admin-cookie gating on every endpoint; security_admin on break-glass
"""
import json
import os
import sys

import pytest
import sqlalchemy
from sqlalchemy.orm import sessionmaker
from fastapi import FastAPI
from fastapi.testclient import TestClient


# Functional tests run with auth "disabled" (mirrors single-user deployments);
# require_admin_cookie still demands a real admin cookie user regardless.
# Scoped to THIS module's tests via an autouse fixture: core.middleware reads
# AUTH_ENABLED per request, so nothing needs it at import time — and a bare
# module-level `os.environ["AUTH_ENABLED"] = "false"` executes during pytest
# COLLECTION and leaks into the whole suite, silently disabling auth for every
# other test (8 unrelated auth-rejection tests failed with DID NOT RAISE /
# 200-instead-of-401). TestAuthEnabledGating's setup_method still overrides to
# "true" per test (setup_method runs after autouse fixtures); the monkeypatch
# teardown restores whatever was set before.
@pytest.fixture(autouse=True)
def _auth_disabled_env(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.database as cdb  # noqa: E402
import routes.routing_harness_routes as rh  # noqa: E402
import src.routing_policy as rp  # noqa: E402

ADMIN = {"x-test-user": "admin"}
NON_ADMIN = {"x-test-user": "bob"}
SEC_ADMIN = {"x-test-user": "sec"}
BEARER = {"x-test-user": "admin", "x-test-bearer": "1"}

GOOD_DECISION = json.dumps({
    "schemaVersion": "0.5",
    "taskId": "t1",
    "classification": {
        "domain": "general_swe", "taskType": "implementation",
        "risk": "low", "dataSensitivity": "public", "verificationMode": "feature_addition",
    },
    "contextRequest": {"sources": [], "includeTests": True, "includeLogs": False},
    "routeRecommendation": {
        "backend": "odysseus_general_swe",
        "modelRoleChain": [{"role": "implementer", "reason": "bounded change"}],
        "allowPremium": False,
    },
    "budgetRecommendation": {"maxCostUsd": 1.0, "preferFree": True},
    "approvalRecommendation": {"required": False, "level": "none"},
    "confidence": {"score": 0.7, "basis": "metadata"},
    "rationale": ["low risk"],
})

BAD_DECISION = json.dumps({"schemaVersion": "9.9", "taskId": "t2"})

INLINE_TASK = {
    "id": "preview-task-1",
    "title": "preview",
    "objective": "look around",
    "type": "diff_review",
    "repoPath": ".",
}


class _StubAuthManager:
    """Minimal shape require_admin_cookie / require_security_admin consume:
    admin + sec are admins; only sec holds the security_admin privilege."""
    is_configured = True

    def is_admin(self, user):
        return user in ("admin", "sec")

    def get_privileges(self, user):
        return {"security_admin": user == "sec"}


def _make_app_and_client():
    engine = sqlalchemy.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    cdb.Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False)
    rh.SessionLocal = TestSession

    app = FastAPI()
    app.state.auth_manager = _StubAuthManager()
    # Handlers are defined inside setup_routing_harness_routes(); it MUST run
    # before include_router so FastAPI snapshots the routes.
    rh.setup_routing_harness_routes()
    app.include_router(rh.router)

    @app.middleware("http")
    async def _stamp(request, call_next):
        # Simulates the real auth middleware's request.state stamping.
        user = request.headers.get("x-test-user")
        if user:
            request.state.current_user = user
        if request.headers.get("x-test-bearer"):
            request.state.api_token = True
        return await call_next(request)

    client = TestClient(app)
    return app, client


# ---------- coordinator wrap + audit ----------
def test_coordinator_wrap_happy_path_audits_with_hmac():
    _app, client = _make_app_and_client()
    r = client.post(
        "/api/harness/coordinator/wrap",
        headers=ADMIN,
        json={"task_id": "t1", "raw_coordinator_output": GOOD_DECISION},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["appliedFallback"] is False
    assert body["auditId"]
    assert "routingPolicyVersion" in body["policyVersions"]
    assert "budgetPolicyVersion" in body["policyVersions"]

    s = rh.SessionLocal()
    rows = s.query(rh.CoordinatorAudit).all()
    s.close()
    assert len(rows) == 1
    assert rows[0].schema_version == "0.5"
    assert rows[0].validation_errors == "[]"
    assert rows[0].hmac and len(rows[0].hmac) == 64
    assert rows[0].redaction_applied is False
    assert json.loads(rows[0].policy_versions)["budgetPolicyVersion"]


def test_coordinator_wrap_failclosed_unknown_schema():
    _app, client = _make_app_and_client()
    r = client.post(
        "/api/harness/coordinator/wrap",
        headers=ADMIN,
        json={"task_id": "t2", "raw_coordinator_output": BAD_DECISION},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["appliedFallback"] is True
    assert body["fallbackPath"] == "safe_scout"
    assert any("schema" in rsn.lower() for rsn in body["validationErrors"])


def test_coordinator_wrap_redacts_secrets_before_storage():
    _app, client = _make_app_and_client()
    leaky = 'not json but has sk-abcdefghij0123456789 and password="hunter2hunter2"'
    r = client.post(
        "/api/harness/coordinator/wrap",
        headers=ADMIN,
        json={"task_id": "t-leak", "raw_coordinator_output": leaky},
    )
    assert r.status_code == 200, r.text
    s = rh.SessionLocal()
    row = s.query(rh.CoordinatorAudit).first()
    s.close()
    assert "sk-abcdefghij0123456789" not in row.raw_output
    assert "hunter2hunter2" not in row.raw_output
    assert row.redaction_applied is True


def test_coordinator_wrap_413_on_oversize_output():
    _app, client = _make_app_and_client()
    r = client.post(
        "/api/harness/coordinator/wrap",
        headers=ADMIN,
        json={"task_id": "t-big", "raw_coordinator_output": "x" * 300_000},
    )
    assert r.status_code == 413, r.text


def test_coordinator_audit_list_and_get_roundtrip():
    _app, client = _make_app_and_client()
    w = client.post(
        "/api/harness/coordinator/wrap",
        headers=ADMIN,
        json={"task_id": "t-audit", "raw_coordinator_output": GOOD_DECISION},
    )
    audit_id = w.json()["auditId"]

    lst = client.get("/api/harness/coordinator/audit?task_id=t-audit", headers=ADMIN)
    assert lst.status_code == 200
    items = lst.json()
    assert len(items) == 1
    assert items[0]["id"] == audit_id
    assert items[0]["parsed_ok"] is True
    assert items[0]["fallback_path"] == "none"

    det = client.get(f"/api/harness/coordinator/audit/{audit_id}", headers=ADMIN)
    assert det.status_code == 200
    full = det.json()
    assert full["hmac"] and len(full["hmac"]) == 64
    assert full["policy_versions"] and "routingPolicyVersion" in full["policy_versions"]
    assert full["validation_errors"] == []
    assert full["raw_output"]

    missing = client.get("/api/harness/coordinator/audit/nope", headers=ADMIN)
    assert missing.status_code == 404


# ---------- previews ----------
def test_route_preview_inline_task_returns_candidates_without_persisting():
    _app, client = _make_app_and_client()
    r = client.post("/api/harness/route/preview", headers=ADMIN, json={"task": INLINE_TASK})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "candidates" in body
    assert "context_token_estimate" in body
    assert body["task_id"] == "preview-task-1"

    s = rh.SessionLocal()
    assert s.query(rh.RoutingTask).count() == 0  # preview never persists
    s.close()


def test_route_preview_requires_task_or_task_id():
    _app, client = _make_app_and_client()
    r = client.post("/api/harness/route/preview", headers=ADMIN, json={})
    assert r.status_code == 400


def test_budget_preview_exposes_checks_and_spend():
    _app, client = _make_app_and_client()
    r = client.post("/api/harness/budget/preview", headers=ADMIN, json={"task": INLINE_TASK})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["allowed"] is True
    assert body["general"]["allowed"] is True
    assert body["premium"]["allowed"] is True
    assert "daily" in body["spend"] and "cap" in body["spend"]["daily"]
    assert "policyVersions" in body


def test_budget_summary_shapes_periods_with_caps():
    _app, client = _make_app_and_client()
    r = client.get("/api/harness/budget/summary", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    for period in ("daily", "weekly", "monthly"):
        assert "spend_usd" in body["periods"][period]
        assert "cap_usd" in body["periods"][period]
    assert body["periods"]["daily"]["premium_cap_usd"] is not None


# ---------- policy lifecycle ----------
def _policy(version):
    return {
        "routingPolicyVersion": version,
        "verificationPolicyVersion": "1.0",
        "uiConfigVersion": "1.0",
        "coordinator": {"provider": "external", "endpointName": None, "model": None,
                        "temperature": 0.1, "maxTokens": 2048},
        "maxUntrustedTokens": 256,
        "rawOutputMaxBytes": 262144,
        "remoteSensitivityCeiling": "confidential",
    }


def test_policy_publish_versions_rollback_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "POLICY_PATH", str(tmp_path / "routing_policy.json"))
    monkeypatch.setattr(rp, "POLICY_VERSIONS_DIR", str(tmp_path / "policy_versions"))
    monkeypatch.setattr(rp, "_cache", None)
    _app, client = _make_app_and_client()

    # No file yet -> defaults served.
    g = client.get("/api/harness/policy", headers=ADMIN)
    assert g.status_code == 200
    assert g.json()["policy"]["routingPolicyVersion"] == "1.0"

    p1 = client.post("/api/harness/policy/publish", headers=ADMIN,
                     json={"policy": _policy("1.1")})
    assert p1.status_code == 200, p1.text
    assert p1.json()["policyVersions"]["routingPolicyVersion"] == "1.1"

    p2 = client.post("/api/harness/policy/publish", headers=ADMIN,
                     json={"policy": _policy("1.2")})
    assert p2.status_code == 200
    assert p2.json()["policy"]["routingPolicyVersion"] == "1.2"

    v = client.get("/api/harness/policy/versions", headers=ADMIN)
    assert v.status_code == 200
    versions = v.json()["versions"]
    # Publishing 1.1 archived nothing (no prior file); publishing 1.2 archived 1.1.
    assert len(versions) == 1
    assert versions[0]["routingPolicyVersion"] == "1.1"

    rb = client.post("/api/harness/policy/rollback", headers=ADMIN,
                     json={"archive": versions[0]["archive"]})
    assert rb.status_code == 200, rb.text
    assert rb.json()["policy"]["routingPolicyVersion"] == "1.1"

    # Rollback archived the 1.2 file and logged both publishes + the rollback.
    v2 = client.get("/api/harness/policy/versions", headers=ADMIN).json()["versions"]
    assert len(v2) == 2
    log = (tmp_path / "policy_versions" / "publish_log.jsonl").read_text().strip().splitlines()
    assert len(log) == 3
    assert json.loads(log[-1])["actor"] == "admin"


def test_policy_publish_rejects_missing_version_keys():
    _app, client = _make_app_and_client()
    r = client.post("/api/harness/policy/publish", headers=ADMIN,
                    json={"policy": {"routingPolicyVersion": "2.0"}})
    assert r.status_code == 400


def test_policy_rollback_rejects_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "POLICY_PATH", str(tmp_path / "routing_policy.json"))
    monkeypatch.setattr(rp, "POLICY_VERSIONS_DIR", str(tmp_path / "policy_versions"))
    monkeypatch.setattr(rp, "_cache", None)
    _app, client = _make_app_and_client()
    r = client.post("/api/harness/policy/rollback", headers=ADMIN,
                    json={"archive": "../routing_policy.json"})
    assert r.status_code == 400


# ---------- registry CRUD ----------
def test_registry_crud_roundtrip():
    _app, client = _make_app_and_client()

    create = client.post("/api/harness/registry", headers=ADMIN, json={
        "id": "p1", "model": "test/model-a", "roles": ["scout", "reviewer"],
        "context_window": 128000, "is_free": True,
    })
    assert create.status_code == 200, create.text
    assert create.json()["roles"] == ["scout", "reviewer"]
    assert create.json()["endpoint"] is None

    dup = client.post("/api/harness/registry", headers=ADMIN,
                      json={"id": "p1", "model": "test/model-a"})
    assert dup.status_code == 409

    lst = client.get("/api/harness/registry", headers=ADMIN)
    assert lst.status_code == 200
    assert [p["id"] for p in lst.json()] == ["p1"]

    patch = client.patch("/api/harness/registry/p1", headers=ADMIN,
                         json={"enabled": False, "roles": ["implementer"], "notes": "bench loser"})
    assert patch.status_code == 200, patch.text
    assert patch.json()["enabled"] is False
    assert patch.json()["roles"] == ["implementer"]
    assert patch.json()["notes"] == "bench loser"

    delete = client.delete("/api/harness/registry/p1", headers=ADMIN)
    assert delete.status_code == 200
    assert client.get("/api/harness/registry", headers=ADMIN).json() == []


def test_registry_delete_refused_when_runs_reference_profile():
    _app, client = _make_app_and_client()
    client.post("/api/harness/registry", headers=ADMIN,
                json={"id": "p-used", "model": "test/model-b", "roles": ["scout"]})

    s = rh.SessionLocal()
    # Sequential commits: these models define no relationship()s, so a single
    # flush won't order the INSERTs FK-parent-first.
    s.add(cdb.RoutingTask(id="task-x", title="t", objective="o",
                          task_type="diff_review", repo_path="."))
    s.commit()
    s.add(cdb.RoutingRun(id="run-x", task_id="task-x", status="succeeded"))
    s.commit()
    s.add(cdb.RoutingModelRun(id="mr-x", run_id="run-x", model_profile_id="p-used"))
    s.commit()
    s.close()

    delete = client.delete("/api/harness/registry/p-used", headers=ADMIN)
    assert delete.status_code == 400
    assert "disable" in delete.json()["detail"]


# ---------- emergency + escalation + reliability (existing surface) ----------
def test_emergency_override_lifecycle():
    # AUTH_ENABLED=false: require_security_admin no-ops, but the admin-cookie
    # gate still applies (exercised for real in TestAuthEnabledGating).
    _app, client = _make_app_and_client()
    create = client.post(
        "/api/harness/emergency/override",
        headers=ADMIN,
        json={"requested_by": "alice", "reason": "prod outage", "ttl_minutes": 30},
    )
    assert create.status_code == 200, create.text
    oid = create.json()["id"]
    assert create.json()["approvedBy"] == "admin"

    lst = client.get("/api/harness/emergency/active", headers=ADMIN)
    assert lst.status_code == 200
    assert any(o["id"] == oid for o in lst.json())

    rev = client.post(f"/api/harness/emergency/{oid}/revoke", headers=ADMIN, json={})
    assert rev.status_code == 200
    assert rev.json()["active"] is False

    s = rh.SessionLocal()
    o = s.query(rh.EmergencyOverride).filter_by(id=oid).first()
    s.close()
    assert o.active is False
    assert o.deactivated_by == "admin"


def test_escalation_evaluate_via_api():
    _app, client = _make_app_and_client()
    r = client.post(
        "/api/harness/escalation/evaluate",
        headers=ADMIN,
        json={
            "task_id": "t-e", "risk": "high", "cheaper_attempts": 3,
            "max_cheaper_attempts": 2, "signal": {"cheap_models_disagree": True},
            "est_premium_cost_usd": 1.0, "budget_remaining_usd": 5.0,
            "data_policy_allows_premium": True, "approval_satisfied": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["allowed"] is True


def test_workflow_reliability_signal_stored():
    _app, client = _make_app_and_client()
    r = client.post(
        "/api/harness/reliability/signal",
        headers=ADMIN,
        json={
            "subject_type": "engineer", "subject_id": "e1",
            "period_start": "2026-07-01", "period_end": "2026-07-07",
            "normalized_verification_failure_rate": 0.7,
        },
    )
    assert r.status_code == 200, r.text
    assert "budget" not in r.json(), "reliability must not carry a budget lever"

    s = rh.SessionLocal()
    rows = s.query(rh.WorkflowReliabilitySignal).all()
    s.close()
    assert len(rows) == 1


# ---------- auth gating (AUTH_ENABLED=true, real gate code paths) ----------
class TestAuthEnabledGating:
    """require_admin_cookie + require_security_admin run their REAL logic here:
    AUTH_ENABLED=true, stub auth_manager, header-stamped request.state."""

    def setup_method(self):
        os.environ["AUTH_ENABLED"] = "true"

    def teardown_method(self):
        os.environ["AUTH_ENABLED"] = "false"

    GATED = [
        ("post", "/api/harness/coordinator/wrap",
         {"task_id": "t", "raw_coordinator_output": "{}"}),
        ("get", "/api/harness/registry", None),
        ("get", "/api/harness/policy", None),
        ("get", "/api/harness/emergency/active", None),
    ]

    def _call(self, client, method, path, body, headers):
        kwargs = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        return getattr(client, method)(path, **kwargs)

    def test_unauthenticated_rejected_403(self):
        _app, client = _make_app_and_client()
        for method, path, body in self.GATED:
            r = self._call(client, method, path, body, headers={})
            assert r.status_code == 403, f"{path}: {r.status_code}"

    def test_bearer_token_rejected_403(self):
        _app, client = _make_app_and_client()
        for method, path, body in self.GATED:
            r = self._call(client, method, path, body, headers=BEARER)
            assert r.status_code == 403, f"{path}: {r.status_code}"

    def test_cookie_non_admin_rejected_403(self):
        _app, client = _make_app_and_client()
        for method, path, body in self.GATED:
            r = self._call(client, method, path, body, headers=NON_ADMIN)
            assert r.status_code == 403, f"{path}: {r.status_code}"

    def test_admin_cookie_allowed(self):
        _app, client = _make_app_and_client()
        r = self._call(client, "post", "/api/harness/coordinator/wrap",
                       {"task_id": "t", "raw_coordinator_output": GOOD_DECISION},
                       headers=ADMIN)
        assert r.status_code == 200, r.text
        assert client.get("/api/harness/registry", headers=ADMIN).status_code == 200
        assert client.get("/api/harness/policy", headers=ADMIN).status_code == 200
        assert client.get("/api/harness/emergency/active", headers=ADMIN).status_code == 200

    def test_emergency_override_needs_security_admin(self):
        _app, client = _make_app_and_client()
        body = {"requested_by": "alice", "reason": "outage", "ttl_minutes": 5}
        # Admin but not security_admin: passes the cookie gate, fails the privilege.
        r = client.post("/api/harness/emergency/override", headers=ADMIN, json=body)
        assert r.status_code == 403
        # security_admin (also an admin) passes both gates.
        r2 = client.post("/api/harness/emergency/override", headers=SEC_ADMIN, json=body)
        assert r2.status_code == 200, r2.text
        assert r2.json()["approvedBy"] == "sec"
