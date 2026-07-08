"""Tests for the Phase 8 coordinator benchmark (src/routing_benchmark.py) + the
/coordinator/decide and /coordinator/benchmark routes.

The scoring engine is exercised entirely WITHOUT a live model: decisions come
from canned raw JSON (valid / invalid / drifting) fed through a stub decide_fn,
so CI never needs an endpoint. Route tests reuse the harness self-contained
app pattern (bare FastAPI + in-memory SQLite + stub auth) from
test_routing_harness_routes.
"""
import json
import os
import sys

import pytest
import sqlalchemy
from sqlalchemy.orm import sessionmaker
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.database as cdb  # noqa: E402
import routes.routing_harness_routes as rh  # noqa: E402
from src.routing_coordinator import GateContext, WrapperResult, wrap_coordinator_output  # noqa: E402
from src import routing_benchmark as rb  # noqa: E402
from src.routing_benchmark import (  # noqa: E402
    HARD_GATE_THRESHOLDS,
    SCORED_DIMENSIONS,
    _agreement,
    aggregate,
    load_fixtures,
    run_benchmark,
    score_decision,
)


# --------------------------------------------------------------------------- #
# helpers — canned decisions (no model)
# --------------------------------------------------------------------------- #
def _decision_dict(expected, task_id="t"):
    """A 'perfect model' decision derived from a fixture's expected dict."""
    lead = (expected.get("acceptableRoles") or ["scout"])[0]
    level = expected.get("approvalLevel", "none") if expected.get("approvalRequired") else "none"
    return {
        "schemaVersion": "0.5",
        "taskId": task_id,
        "classification": {
            "domain": expected["domain"],
            "taskType": expected["taskType"],
            "risk": expected["risk"],
            "dataSensitivity": expected["dataSensitivity"],
            "verificationMode": expected["verificationMode"],
        },
        "routeRecommendation": {
            "backend": expected["backend"],
            "modelRoleChain": [{"role": lead, "reason": "bench"}],
            "allowPremium": False,
        },
        "approvalRecommendation": {"required": bool(expected.get("approvalRequired", False)), "level": level},
        "confidence": {"score": expected.get("confidence", 0.7), "basis": "metadata"},
        "rationale": ["bench"],
    }


def _gctx(task_id="t"):
    return GateContext(
        remote_exception_approved=False, budget_ok=True, backend_available=True,
        approval_satisfied=False, sandbox_ok=True, task_id=task_id,
    )


def _wrap(raw, gctx=None):
    return wrap_coordinator_output(raw, gctx or _gctx())


_BACKEND_EXPECTED = {
    "domain": "general_swe", "taskType": "implementation", "risk": "low",
    "dataSensitivity": "internal", "verificationMode": "feature_addition",
    "backend": "odysseus_general_swe", "approvalRequired": False,
    "gate_expectation": "allowed", "acceptableRoles": ["implementer", "scout"],
}


# --------------------------------------------------------------------------- #
# score_decision — per-dimension
# --------------------------------------------------------------------------- #
def test_score_perfect_decision_passes_all_applicable_dims():
    raw = json.dumps(_decision_dict(_BACKEND_EXPECTED))
    result = _wrap(raw)
    scores = score_decision(_BACKEND_EXPECTED, result.decision, result)
    for dim in ("schema_validity", "domain_classification", "task_type_classification",
                "risk_classification", "data_sensitivity_classification",
                "verification_mode_selection", "backend_routing",
                "policy_gate_compliance", "approval_gate", "arbitration", "failure_retry"):
        assert scores[dim]["passed"], (dim, scores[dim]["detail"])
    # non-ambiguous fixture: uncertainty handling is not under test
    assert scores["uncertainty_handling"]["applicable"] is False


def test_score_wrong_domain_fails_only_domain():
    d = _decision_dict(_BACKEND_EXPECTED)
    d["classification"]["domain"] = "infra"
    result = _wrap(json.dumps(d))
    scores = score_decision(_BACKEND_EXPECTED, result.decision, result)
    assert scores["domain_classification"]["passed"] is False
    assert scores["task_type_classification"]["passed"] is True
    assert scores["schema_validity"]["passed"] is True


def test_score_wrong_backend_fails_backend_routing():
    d = _decision_dict(_BACKEND_EXPECTED)
    d["routeRecommendation"]["backend"] = "local_framework_coordinator_only"
    result = _wrap(json.dumps(d))
    scores = score_decision(_BACKEND_EXPECTED, result.decision, result)
    assert scores["backend_routing"]["passed"] is False
    # a legal (still-local) route: policy compliance is unaffected
    assert scores["policy_gate_compliance"]["passed"] is True


def test_wrong_approval_flag_fails_approval_gate():
    exp = dict(_BACKEND_EXPECTED, approvalRequired=True)
    d = _decision_dict(_BACKEND_EXPECTED)  # emits required=False
    result = _wrap(json.dumps(d))
    scores = score_decision(exp, result.decision, result)
    assert scores["approval_gate"]["passed"] is False


# --------------------------------------------------------------------------- #
# policy_gate_compliance — the safety-critical gate
# --------------------------------------------------------------------------- #
def test_policy_gate_blocks_restricted_remote_and_scores_pass():
    """A restricted-data decision recommending a REMOTE backend must be blocked
    by the wrapper; the final route is safe, so policy_gate passes (and
    failure_retry passes because the harness degraded truthfully)."""
    exp = {
        "domain": "infra", "taskType": "feature_review", "risk": "high",
        "dataSensitivity": "restricted", "verificationMode": "analysis_only",
        "backend": "openrouter", "approvalRequired": False,
        "gate_expectation": "blocked", "acceptableBackends": ["odysseus_general_swe"],
    }
    d = _decision_dict(exp)  # restricted + openrouter (remote) — illegal recommendation
    result = _wrap(json.dumps(d))
    assert result.ok is False
    assert result.fallbackPath in ("deterministic", "safe_scout")
    scores = score_decision(exp, result.decision, result)
    assert scores["policy_gate_compliance"]["passed"] is True   # illegal route did NOT execute
    assert scores["failure_retry"]["passed"] is True
    assert scores["schema_validity"]["passed"] is True          # the JSON itself was valid


def test_policy_gate_catches_slipped_illegal_route():
    """Direct scorer test: a hypothetically ACCEPTED route (ok=True) that is
    policy-illegal (restricted -> openrouter) must FAIL policy_gate_compliance
    even though the decision parsed cleanly."""
    illegal_raw = json.dumps({
        "schemaVersion": "0.5", "taskId": "t",
        "classification": {"domain": "infra", "taskType": "feature_review", "risk": "high",
                           "dataSensitivity": "restricted", "verificationMode": "analysis_only"},
        "routeRecommendation": {"backend": "openrouter",
                                "modelRoleChain": [{"role": "scout", "reason": "x"}], "allowPremium": False},
        "approvalRecommendation": {"required": False, "level": "none"},
        "confidence": {"score": 0.5, "basis": "metadata"}, "rationale": ["x"],
    })
    slipped = WrapperResult(
        ok=True, decision=None,
        route={"backend": "openrouter", "dataSensitivity": "restricted",
               "approvalRequired": False, "approved": True},
        appliedFallback=False, fallbackPath="none", validationErrors=[],
        auditNotes=[], rawOutput=illegal_raw,
    )
    exp = {"domain": "infra", "taskType": "feature_review", "risk": "high",
           "dataSensitivity": "restricted", "verificationMode": "analysis_only",
           "backend": "openrouter", "approvalRequired": False, "gate_expectation": "blocked"}
    scores = score_decision(exp, None, slipped)
    assert scores["policy_gate_compliance"]["passed"] is False
    assert scores["schema_validity"]["passed"] is True  # raw parsed fine; only the ROUTE was illegal


# --------------------------------------------------------------------------- #
# uncertainty_handling
# --------------------------------------------------------------------------- #
def test_uncertainty_low_confidence_passes():
    exp = {"domain": "unknown", "taskType": "unknown", "risk": "medium",
           "dataSensitivity": "internal", "verificationMode": "analysis_only",
           "backend": "odysseus_general_swe", "approvalRequired": False,
           "gate_expectation": "allowed", "acceptableRoles": ["planner"],
           "maxConfidenceForUncertain": 0.5}
    d = _decision_dict(exp, )
    d["confidence"]["score"] = 0.3
    d["routeRecommendation"]["modelRoleChain"] = [{"role": "planner", "reason": "x"}]
    result = _wrap(json.dumps(d))
    scores = score_decision(exp, result.decision, result)
    assert scores["uncertainty_handling"]["applicable"] is True
    assert scores["uncertainty_handling"]["passed"] is True


def test_uncertainty_overconfident_no_safety_fails():
    exp = {"domain": "unknown", "taskType": "unknown", "risk": "medium",
           "dataSensitivity": "internal", "verificationMode": "analysis_only",
           "backend": "odysseus_general_swe", "approvalRequired": False,
           "gate_expectation": "allowed", "acceptableRoles": ["planner"],
           "maxConfidenceForUncertain": 0.5}
    d = _decision_dict(exp)
    d["confidence"]["score"] = 0.95
    d["approvalRecommendation"] = {"required": False, "level": "none"}
    d["routeRecommendation"]["modelRoleChain"] = [{"role": "implementer", "reason": "x"}]
    result = _wrap(json.dumps(d))
    scores = score_decision(exp, result.decision, result)
    assert scores["uncertainty_handling"]["passed"] is False


# --------------------------------------------------------------------------- #
# consistency + aggregate hard-gate math
# --------------------------------------------------------------------------- #
def test_agreement_helper():
    assert _agreement(["a", "a", "a"]) == 1.0
    assert _agreement(["a", "a", "b", "b"]) == 0.5
    assert _agreement([]) is None


def _score_row(**overrides):
    return {d: {"passed": overrides.get(d, True), "detail": "", "applicable": True}
            for d in SCORED_DIMENSIONS}


def _fixture_rows(n, per_row_overrides, keys):
    return {
        "fixture_id": "fx", "dimension": "schema_validity",
        "replay_scores": [_score_row(**o) for o in per_row_overrides],
        "consistency_keys": keys,
    }


def test_aggregate_schema_gate_just_pass_and_just_fail():
    key = ("d", "t", "r", "s", "v", "b")
    # 98/100 schema-valid == exactly the 0.98 threshold -> pass
    rows_pass = [{"schema_validity": i >= 2} for i in range(100)]
    agg = aggregate([_fixture_rows(100, rows_pass, [key] * 100)], replays=100)
    assert agg["gates"]["schema_validity"]["value"] == pytest.approx(0.98)
    assert agg["gates"]["schema_validity"]["passed"] is True

    rows_fail = [{"schema_validity": i >= 3} for i in range(100)]  # 97/100
    agg2 = aggregate([_fixture_rows(100, rows_fail, [key] * 100)], replays=100)
    assert agg2["gates"]["schema_validity"]["passed"] is False
    assert agg2["passedAllGates"] is False


def test_aggregate_consistency_gate_from_agreement():
    # 10 fixtures each 9/10 agreement -> mean 0.90 -> pass
    good = [{"fixture_id": f"f{i}", "dimension": "consistency",
             "replay_scores": [_score_row() for _ in range(10)],
             "consistency_keys": (["A"] * 9 + ["B"])} for i in range(10)]
    agg = aggregate(good, replays=10)
    assert agg["gates"]["consistency"]["value"] == pytest.approx(0.90)
    assert agg["gates"]["consistency"]["passed"] is True

    bad = [{"fixture_id": f"f{i}", "dimension": "consistency",
            "replay_scores": [_score_row() for _ in range(10)],
            "consistency_keys": (["A"] * 8 + ["B", "C"])} for i in range(10)]  # 0.8
    agg2 = aggregate(bad, replays=10)
    assert agg2["gates"]["consistency"]["passed"] is False


def test_aggregate_empty_dimension_is_null_and_gate_fails():
    # A fixture set where uncertainty is never applicable -> value None, gate fails.
    rows = [_fixture_rows(3, [{}, {}, {}], [("k",)] * 3)]
    for r in rows:
        for s in r["replay_scores"]:
            s["uncertainty_handling"]["applicable"] = False
    agg = aggregate(rows, replays=3)
    assert agg["perDimension"]["uncertainty_handling"]["value"] is None
    assert agg["gates"]["uncertainty_handling"]["passed"] is False


# --------------------------------------------------------------------------- #
# run_benchmark end-to-end (LLM-free stubs)
# --------------------------------------------------------------------------- #
def _fixtures():
    return load_fixtures()  # config/routing_coordinator_fixtures


def _wrap_fn(raw, gctx):
    return wrap_coordinator_output(raw, gctx, deterministic_fn=None)


def _gctx_fn(fx):
    task = fx.get("task") or {}
    return _gctx(task.get("id") or fx.get("id") or "t")


def test_run_benchmark_perfect_stub_passes_all_gates():
    fixtures = _fixtures()
    canned = {fx["task"]["id"]: json.dumps(_decision_dict(fx["expected"], task_id=fx["task"]["id"]))
              for fx in fixtures}

    def decide_fn(payload):
        return canned[payload["id"]]

    agg = run_benchmark(fixtures, decide_fn, _wrap_fn, _gctx_fn, replays=3)
    assert agg["fixtures_count"] == len(fixtures)
    failing = {n: g for n, g in agg["gates"].items() if not g["passed"]}
    assert agg["passedAllGates"] is True, failing


def test_run_benchmark_invalid_stub_fails_schema_gate():
    fixtures = _fixtures()

    def decide_fn(payload):
        return "this is not json {"

    agg = run_benchmark(fixtures, decide_fn, _wrap_fn, _gctx_fn, replays=3)
    assert agg["gates"]["schema_validity"]["value"] == 0.0
    assert agg["gates"]["schema_validity"]["passed"] is False
    assert agg["passedAllGates"] is False
    # the harness still degraded truthfully to safe_scout on every replay
    assert agg["gates"]["failure_retry"]["passed"] is True


def test_run_benchmark_drifting_stub_fails_consistency():
    fixtures = _fixtures()
    state = {"n": 0}

    def decide_fn(payload):
        state["n"] += 1
        domain = "general_swe" if state["n"] % 2 == 0 else "infra"
        return json.dumps({
            "schemaVersion": "0.5", "taskId": payload["id"],
            "classification": {"domain": domain, "taskType": "implementation", "risk": "low",
                               "dataSensitivity": "internal", "verificationMode": "feature_addition"},
            "routeRecommendation": {"backend": "odysseus_general_swe",
                                    "modelRoleChain": [{"role": "scout", "reason": "x"}], "allowPremium": False},
            "approvalRecommendation": {"required": False, "level": "none"},
            "confidence": {"score": 0.6, "basis": "metadata"}, "rationale": ["x"],
        })

    agg = run_benchmark(fixtures, decide_fn, _wrap_fn, _gctx_fn, replays=4)
    assert agg["gates"]["consistency"]["passed"] is False
    assert agg["passedAllGates"] is False


# --------------------------------------------------------------------------- #
# DB models round-trip
# --------------------------------------------------------------------------- #
def _mem_session():
    engine = sqlalchemy.create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    cdb.Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False)()


def test_benchmark_models_roundtrip():
    s = _mem_session()
    run = cdb.CoordinatorBenchmarkRun(
        id="run-1", endpoint_name="local-coordinator", model="m", replays=5,
        fixtures_count=26, passed_all_gates=True,
        gates=json.dumps({"schema_validity": {"value": 1.0, "threshold": 0.98, "passed": True}}),
        per_dimension=json.dumps({"schema_validity": {"value": 1.0}}),
        policy_versions=json.dumps({"routingPolicyVersion": "1.2"}),
    )
    s.add(run)
    s.commit()
    s.add(cdb.CoordinatorBenchmarkResult(
        id="res-1", run_id="run-1", fixture_id="fx-1", dimension="domain_classification",
        replays=5, agreement=1.0, detail=json.dumps({"fixture_id": "fx-1"}),
    ))
    s.commit()
    got = s.get(cdb.CoordinatorBenchmarkRun, "run-1")
    assert got.passed_all_gates is True
    assert json.loads(got.gates)["schema_validity"]["passed"] is True
    results = s.query(cdb.CoordinatorBenchmarkResult).filter_by(run_id="run-1").all()
    assert len(results) == 1 and results[0].agreement == 1.0
    s.close()


# --------------------------------------------------------------------------- #
# route tests (self-contained app, stub auth)
# --------------------------------------------------------------------------- #
ADMIN = {"x-test-user": "admin"}
NON_ADMIN = {"x-test-user": "bob"}


class _StubAuthManager:
    is_configured = True

    def is_admin(self, user):
        return user == "admin"

    def get_privileges(self, user):
        return {"security_admin": user == "sec"}


def _make_app_and_client():
    engine = sqlalchemy.create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    cdb.Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False)
    rh.SessionLocal = TestSession

    app = FastAPI()
    app.state.auth_manager = _StubAuthManager()
    rh.setup_routing_harness_routes()
    app.include_router(rh.router)

    @app.middleware("http")
    async def _stamp(request, call_next):
        user = request.headers.get("x-test-user")
        if user:
            request.state.current_user = user
        if request.headers.get("x-test-bearer"):
            request.state.api_token = True
        return await call_next(request)

    return app, TestClient(app), TestSession


@pytest.fixture(autouse=True)
def _auth_disabled_env(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")


def test_coordinator_decide_external_provider_returns_400():
    """Live policy has coordinator.provider='external' -> /coordinator/decide 400."""
    _app, client, _S = _make_app_and_client()
    r = client.post("/api/harness/coordinator/decide", headers=ADMIN, json={"task_id": "x"})
    assert r.status_code == 400, r.text
    assert "external" in r.json()["detail"]


class _FakeEndpointClient:
    """Stubs an endpoint-backed CoordinatorClient without any network/model."""
    def is_llm_backed(self):
        return True

    def decide(self, payload):
        return json.dumps({
            "schemaVersion": "0.5", "taskId": payload.get("id", "t"),
            "classification": {"domain": "general_swe", "taskType": "implementation", "risk": "low",
                               "dataSensitivity": "public", "verificationMode": "feature_addition"},
            "routeRecommendation": {"backend": "odysseus_general_swe",
                                    "modelRoleChain": [{"role": "implementer", "reason": "x"}], "allowPremium": False},
            "approvalRecommendation": {"required": False, "level": "none"},
            "confidence": {"score": 0.7, "basis": "metadata"}, "rationale": ["x"],
        })

    def repair_fn(self, raw, errors):
        return None

    @classmethod
    def from_policy(cls, policy):
        return cls()


def test_coordinator_decide_endpoint_generates_and_audits(monkeypatch):
    _app, client, S = _make_app_and_client()
    monkeypatch.setattr(rh, "CoordinatorClient", _FakeEndpointClient)
    s = S()
    s.add(cdb.RoutingTask(id="task-dec", title="t", objective="o",
                          task_type="implementation", repo_path="."))
    s.commit()
    s.close()

    r = client.post("/api/harness/coordinator/decide", headers=ADMIN, json={"task_id": "task-dec"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["auditId"]
    assert body["generatedRaw"]

    s = S()
    audits = s.query(cdb.CoordinatorAudit).all()
    s.close()
    assert len(audits) == 1
    assert audits[0].parsed_ok is True


def test_coordinator_decide_missing_task_404(monkeypatch):
    _app, client, _S = _make_app_and_client()
    monkeypatch.setattr(rh, "CoordinatorClient", _FakeEndpointClient)
    r = client.post("/api/harness/coordinator/decide", headers=ADMIN, json={"task_id": "nope"})
    assert r.status_code == 404


def test_benchmark_run_unresolvable_endpoint_400_not_500():
    _app, client, _S = _make_app_and_client()
    r = client.post("/api/harness/coordinator/benchmark", headers=ADMIN,
                    json={"endpoint_name": "does-not-exist", "replays": 1})
    assert r.status_code == 400, r.text


def test_benchmark_list_and_detail_roundtrip_and_auth():
    _app, client, S = _make_app_and_client()
    s = S()
    s.add(cdb.CoordinatorBenchmarkRun(
        id="brun-1", endpoint_name="cand", model="m", replays=5, fixtures_count=26,
        passed_all_gates=False,
        gates=json.dumps({"schema_validity": {"value": 0.9, "threshold": 0.98, "passed": False}}),
        per_dimension=json.dumps({"schema_validity": {"value": 0.9, "passed": 45, "total": 50}}),
        policy_versions=json.dumps({"routingPolicyVersion": "1.2"}),
    ))
    s.commit()
    s.add(cdb.CoordinatorBenchmarkResult(
        id="bres-1", run_id="brun-1", fixture_id="fx-1", dimension="domain_classification",
        replays=5, agreement=0.8, detail=json.dumps({"fixture_id": "fx-1"}),
    ))
    s.commit()
    s.close()

    lst = client.get("/api/harness/coordinator/benchmark", headers=ADMIN)
    assert lst.status_code == 200
    assert [r["id"] for r in lst.json()] == ["brun-1"]
    assert lst.json()[0]["passed_all_gates"] is False

    det = client.get("/api/harness/coordinator/benchmark/brun-1", headers=ADMIN)
    assert det.status_code == 200
    body = det.json()
    assert body["per_dimension"]["schema_validity"]["value"] == 0.9
    assert len(body["per_fixture"]) == 1
    assert body["per_fixture"][0]["agreement"] == 0.8

    missing = client.get("/api/harness/coordinator/benchmark/nope", headers=ADMIN)
    assert missing.status_code == 404


def test_benchmark_routes_auth_gated():
    os.environ["AUTH_ENABLED"] = "true"
    try:
        _app, client, _S = _make_app_and_client()
        assert client.get("/api/harness/coordinator/benchmark", headers={}).status_code == 403
        assert client.get("/api/harness/coordinator/benchmark", headers=NON_ADMIN).status_code == 403
        assert client.post("/api/harness/coordinator/decide", headers={},
                           json={"task_id": "x"}).status_code == 403
    finally:
        os.environ["AUTH_ENABLED"] = "false"
