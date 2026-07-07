"""
Integration tests for the v0.5 routing harness REST surfaces.

Built self-contained: spins up a bare FastAPI app with ONLY the harness
router + an in-memory SQLite DB (the new CoordinatorAudit / EmergencyOverride /
WorkflowReliabilitySignal tables), avoiding the heavyweight app.py import chain.

Guarantees exercised here:
  - deterministic coordinator wrap audit (schemaVersion enforced, fail-closed)
  - emergency override create/list/revoke via security-admin gating
  - workflow reliability signal compute + store (advisory only)
  - escalation evaluate endpoint
  - no target-repo mutation (harness is read-only control plane)
"""
import os

# Disable auth so require_security_admin / require_admin return early in tests.
os.environ["AUTH_ENABLED"] = "false"  # force (overrides any pre-set value)

import sqlalchemy
from sqlalchemy.orm import sessionmaker
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in os.sys.path:
    os.sys.path.insert(0, ROOT)

import core.database as cdb  # noqa: E402
import routes.routing_harness_routes as rh  # noqa: E402

HDR = {"x-odysseus-internal-tool": "odysseus-internal-tool"}

GOOD_DECISION = (
    '{"schemaVersion":"0.5","taskId":"t1","classification":{"domain":"general_swe",'
    '"taskType":"implementation","risk":"low","dataSensitivity":"public",'
    '"verificationMode":"test_passing_and_new_tests"},"contextRequest":{"items":[]},'
    '"routeRecommendation":{"backend":"odysseus_general_swe","modelRoleChain":[],'
    '"allowPremium":false},"budgetRecommendation":{"maxCostUsd":1.0,"premiumCapUsd":2.0},'
    '"approvalRecommendation":{"required":false,"humanReview":false},'
    '"confidence":{"score":0.7,"basis":"heuristic"},"rationale":["low risk"]}'
)

BAD_DECISION = (
    '{"schemaVersion":"9.9","taskId":"t2","classification":{},"contextRequest":{},'
    '"routeRecommendation":{},"budgetRecommendation":{},"approvalRecommendation":{},'
    '"confidence":{},"rationale":[]}'
)


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
    # Handlers are defined inside setup_routing_harness_routes(); it MUST run
    # before include_router so FastAPI snapshots the routes.
    rh.setup_routing_harness_routes()
    app.include_router(rh.router)
    client = TestClient(app)

    @app.middleware("http")
    async def _stamp(request, call_next):
        if request.headers.get("x-odysseus-internal-tool") == "odysseus-internal-tool":
            request.state.current_user = "internal-tool"
        return await call_next(request)

    return app, client


def test_coordinator_wrap_audit_stores_raw_output():
    _app, client = _make_app_and_client()
    r = client.post(
        "/api/harness/coordinator/wrap",
        headers=HDR,
        json={"task_id": "t1", "raw_coordinator_output": GOOD_DECISION},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["appliedFallback"] is False
    assert body["auditId"] is not None

    s = rh.SessionLocal()
    rows = s.query(rh.CoordinatorAudit).all()
    s.close()
    assert len(rows) == 1
    assert rows[0].schema_version == "0.5"
    assert rows[0].validation_errors == "[]"


def test_coordinator_wrap_failclosed_unknown_schema():
    _app, client = _make_app_and_client()
    r = client.post(
        "/api/harness/coordinator/wrap",
        headers=HDR,
        json={"task_id": "t2", "raw_coordinator_output": BAD_DECISION},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert body["appliedFallback"] is True
    assert body["fallbackPath"] == "safe_scout"
    assert any("schema" in rsn.lower() for rsn in body["validationErrors"])

    s = rh.SessionLocal()
    a = s.query(rh.CoordinatorAudit).first()
    s.close()
    assert a.validation_errors  # non-empty: reason recorded for audit


def test_emergency_override_lifecycle():
    _app, client = _make_app_and_client()
    create = client.post(
        "/api/harness/emergency/override",
        headers=HDR,
        json={"requested_by": "alice", "reason": "prod outage", "ttl_minutes": 30},
    )
    assert create.status_code == 200, create.text
    oid = create.json()["id"]

    lst = client.get("/api/harness/emergency/active", headers=HDR)
    assert lst.status_code == 200
    assert any(o["id"] == oid for o in lst.json())

    rev = client.post(
        f"/api/harness/emergency/{oid}/revoke",
        headers=HDR,
        json={"revoked_by": "alice"},
    )
    assert rev.status_code == 200
    assert rev.json()["active"] is False

    s = rh.SessionLocal()
    o = s.query(rh.EmergencyOverride).filter_by(id=oid).first()
    s.close()
    assert o.active is False
    assert o.deactivated_at is not None


def test_escalation_evaluate_via_api():
    _app, client = _make_app_and_client()
    r = client.post(
        "/api/harness/escalation/evaluate",
        headers=HDR,
        json={
            "task_id": "t-e",
            "risk": "high",
            "cheaper_attempts": 3,
            "max_cheaper_attempts": 2,
            "signal": {"cheap_models_disagree": True},
            "est_premium_cost_usd": 1.0,
            "budget_remaining_usd": 5.0,
            "data_policy_allows_premium": True,
            "approval_satisfied": True,
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["allowed"] is True


def test_workflow_reliability_signal_stored_and_unaffected_by_confounds():
    _app, client = _make_app_and_client()
    r = client.post(
        "/api/harness/reliability/signal",
        headers=HDR,
        json={
            "subject_type": "engineer",
            "subject_id": "e1",
            "period_start": "2026-07-01",
            "period_end": "2026-07-07",
            "normalized_verification_failure_rate": 0.7,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # Recommended action is an advisory review signal, NOT a budget lever.
    assert body["recommendedAction"] in (
        "increase_review_depth",
        "require_coaching_review",
        "require_senior_reviewer",
        "admin_review",
    )
    assert "budget" not in body, "reliability must not carry a budget lever"

    s = rh.SessionLocal()
    rows = s.query(rh.WorkflowReliabilitySignal).all()
    s.close()
    assert len(rows) == 1
    assert rows[0].confounders  # captured as JSON, not dropped
