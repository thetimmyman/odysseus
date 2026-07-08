
"""Unit tests for the v0.5 harness policy modules: strict coordinator parser +
deterministic wrapper fallback chain, redaction, audit HMAC, and the
(unchanged) escalation / reliability policy modules."""
import json

import pytest

from src.routing_coordinator import (
    CoordinatorDecision,
    DecisionValidationError,
    GateContext,
    parse_decision,
    SchemaVersionError,
    SCHEMA_VERSION,
    wrap_coordinator_output,
)
from src.routing_escalation import (
    build_emergency_override,
    EscalationContext,
    EscalationSignal,
    evaluate_escalation,
    Risk,
)
from src.routing_redaction import redact_text
from src.routing_reliability import (
    budget_affecting_policy_allowed,
    compute_signal,
    Confounders,
    ReliabilityInput,
)

VALID_DECISION = {
    "schemaVersion": "0.5",
    "taskId": "t1",
    "classification": {
        "domain": "general_swe", "taskType": "bug_debug",
        "risk": "low", "dataSensitivity": "internal", "verificationMode": "bug_fix",
    },
    "contextRequest": {"sources": ["repo"], "includeTests": True, "includeLogs": False,
                       "maxUntrustedTokens": 256},
    "routeRecommendation": {
        "backend": "odysseus_general_swe",
        "modelRoleChain": [{"role": "scout", "reason": "cheap first pass"}],
        "allowPremium": False,
    },
    "budgetRecommendation": {"maxCostUsd": 0.01, "preferFree": True},
    "approvalRecommendation": {"required": False, "level": "none"},
    "confidence": {"score": 0.6, "basis": "metadata"},
    "rationale": ["low risk", "cheap model can scout"],
}

DET_ROUTE = {
    "backend": "odysseus_general_swe",
    "modelRoleChain": [{"role": "scout", "reason": "ranked candidate", "modelPreference": "m1"}],
    "allowPremium": False,
    "verificationMode": "analysis_only",
    "dataSensitivity": "internal",
    "approvalRequired": False,
    "approved": False,
    "rationale": ["deterministic router fallback"],
    "schemaVersion": SCHEMA_VERSION,
}


# ---------- strict parser ----------
def test_parse_decision_valid_full():
    d = parse_decision(VALID_DECISION)
    assert isinstance(d, CoordinatorDecision)
    assert d.schemaVersion == SCHEMA_VERSION
    assert d.classification.risk.value == "low"
    assert d.classification.verificationMode.value == "bug_fix"


def test_unknown_schema_version_fails_closed():
    with pytest.raises(SchemaVersionError):
        parse_decision(dict(VALID_DECISION, schemaVersion="9.9"))


def test_invalid_enum_lists_field():
    bad = dict(VALID_DECISION)
    bad["classification"] = dict(bad["classification"], risk="wild")
    with pytest.raises(DecisionValidationError) as exc:
        parse_decision(bad)
    assert any(e.startswith("invalid_enum_value:classification.risk") for e in exc.value.errors)


def test_missing_classification_rejected():
    bad = dict(VALID_DECISION)
    del bad["classification"]
    with pytest.raises(DecisionValidationError) as exc:
        parse_decision(bad)
    assert any("missing_required_field:classification" in e for e in exc.value.errors)


def test_unknown_field_in_budget_rejected():
    bad = dict(VALID_DECISION)
    bad["budgetRecommendation"] = dict(bad["budgetRecommendation"], premiumCapUsd=2.0)
    with pytest.raises(DecisionValidationError) as exc:
        parse_decision(bad)
    assert any("unknown_field:budgetRecommendation.premiumCapUsd" in e for e in exc.value.errors)


def test_model_role_chain_without_reason_rejected():
    bad = dict(VALID_DECISION)
    bad["routeRecommendation"] = dict(
        bad["routeRecommendation"], modelRoleChain=[{"role": "scout"}],
    )
    with pytest.raises(DecisionValidationError) as exc:
        parse_decision(bad)
    assert any("missing_required_field:modelRoleChain[0].reason" in e for e in exc.value.errors)


def test_confidence_score_out_of_range_rejected():
    bad = dict(VALID_DECISION)
    bad["confidence"] = {"score": 1.5, "basis": "metadata"}
    with pytest.raises(DecisionValidationError) as exc:
        parse_decision(bad)
    assert any("invalid_value:confidence.score" in e for e in exc.value.errors)


# ---------- wrapper fallback chain ----------
def test_wrapper_passes_clean_decision():
    res = wrap_coordinator_output(json.dumps(VALID_DECISION), GateContext(task_id="t1"))
    assert res.ok and res.decision is not None
    assert res.appliedFallback is False and res.fallbackPath == "none"
    assert res.route["backend"] == "odysseus_general_swe"


def test_garbage_json_no_fns_hits_safe_scout():
    res = wrap_coordinator_output("not json at all {{{", GateContext(task_id="t1"))
    assert res.appliedFallback and res.fallbackPath == "safe_scout"
    assert res.route["backend"] == "odysseus_general_swe"
    assert res.route["verificationMode"] == "analysis_only"


def test_garbage_json_with_deterministic_fn_passes_route_through():
    res = wrap_coordinator_output(
        "not json {{{", GateContext(task_id="t1"),
        deterministic_fn=lambda task_id: dict(DET_ROUTE),
    )
    assert res.fallbackPath == "deterministic"
    assert res.route == DET_ROUTE


def test_field_error_repaired_by_repair_fn():
    bad = dict(VALID_DECISION)
    bad["classification"] = dict(bad["classification"], risk="wild")

    def repair_fn(raw_text, errors):
        assert any("invalid_enum_value:classification.risk" in e for e in errors)
        return json.dumps(VALID_DECISION)

    res = wrap_coordinator_output(json.dumps(bad), GateContext(task_id="t1"), repair_fn=repair_fn)
    assert res.ok is True
    assert res.fallbackPath == "repair"


def test_unknown_version_never_repaired():
    calls = []

    def repair_fn(raw_text, errors):
        calls.append(1)
        return json.dumps(VALID_DECISION)

    res = wrap_coordinator_output(
        json.dumps(dict(VALID_DECISION, schemaVersion="9.9")),
        GateContext(task_id="t1"),
        repair_fn=repair_fn,
        deterministic_fn=lambda task_id: dict(DET_ROUTE),
    )
    assert calls == [], "repair must not be attempted for an unknown schemaVersion"
    assert res.fallbackPath == "deterministic"


def _restricted_remote_decision():
    d = dict(VALID_DECISION)
    d["classification"] = dict(d["classification"], dataSensitivity="restricted")
    d["routeRecommendation"] = dict(d["routeRecommendation"], backend="openrouter")
    return d


def test_restricted_remote_blocked_without_exception():
    res = wrap_coordinator_output(
        json.dumps(_restricted_remote_decision()),
        GateContext(task_id="t2", remote_exception_approved=False),
    )
    assert res.appliedFallback
    assert res.fallbackPath in ("deterministic", "safe_scout")
    assert any("restricted_data_remote_blocked" in e for e in res.validationErrors)


def test_restricted_remote_allowed_with_recorded_exception():
    res = wrap_coordinator_output(
        json.dumps(_restricted_remote_decision()),
        GateContext(task_id="t2", remote_exception_approved=True),
    )
    assert res.ok is True and res.fallbackPath == "none"


def test_approval_required_unsatisfied_blocks():
    d = dict(VALID_DECISION)
    d["approvalRecommendation"] = {"required": True, "level": "reviewer"}
    res = wrap_coordinator_output(json.dumps(d), GateContext(task_id="t3", approval_satisfied=False))
    assert res.appliedFallback
    assert any("approval_gate_unsatisfied" in e for e in res.validationErrors)


def test_approval_required_satisfied_marks_approved():
    d = dict(VALID_DECISION)
    d["approvalRecommendation"] = {"required": True, "level": "reviewer"}
    res = wrap_coordinator_output(json.dumps(d), GateContext(task_id="t3", approval_satisfied=True))
    assert res.ok is True
    assert res.route["approvalRequired"] is True
    assert res.route["approved"] is True


# ---------- redaction ----------
def test_redact_masks_api_keys_and_passwords():
    text = 'key is sk-abcdefghij0123456789 and password="hunter2hunter2" done'
    red, applied = redact_text(text)
    assert applied is True
    assert "sk-abcdefghij0123456789" not in red
    assert "hunter2hunter2" not in red
    assert "[REDACTED]" in red
    # Generic assignment keeps the key NAME so audits show what leaked.
    assert "password" in red


def test_redact_clean_text_untouched():
    text = "the route is odysseus_general_swe with a scout role"
    red, applied = redact_text(text)
    assert red == text
    assert applied is False


# ---------- audit hmac ----------
def test_hmac_sign_deterministic_and_input_sensitive(tmp_path, monkeypatch):
    import src.secret_storage as ss
    # Point the key at a throwaway path so the test never touches data/.app_key.
    monkeypatch.setattr(ss, "_KEY_PATH", tmp_path / "test_key")
    monkeypatch.setattr(ss, "_fernet", None)
    a1 = ss.hmac_sign("payload one")
    a2 = ss.hmac_sign("payload one")
    b = ss.hmac_sign("payload two")
    assert a1 == a2
    assert a1 != b
    assert len(a1) == 64 and all(c in "0123456789abcdef" for c in a1)


# ---------- escalation (unchanged module, kept for coverage) ----------
def test_escalation_all_conditions_met():
    ctx = EscalationContext(
        task_id="t", risk=Risk.HIGH, cheaper_attempts=3,
        signal=EscalationSignal(tests_still_fail=True),
        est_premium_cost_usd=0.5, budget_remaining_usd=1.0,
        data_policy_allows_premium=True, approval_satisfied=True,
    )
    assert evaluate_escalation(ctx).allowed is True


def test_escalation_missing_signal_blocks():
    ctx = EscalationContext(
        task_id="t", risk=Risk.RELEASE_BLOCKING, cheaper_attempts=5,
        signal=EscalationSignal(), est_premium_cost_usd=0.1,
        budget_remaining_usd=1.0, data_policy_allows_premium=True,
        approval_satisfied=True,
    )
    v = evaluate_escalation(ctx)
    assert v.allowed is False
    assert any("condition2_unmet" in r for r in v.reasons)


def test_escalation_over_budget_blocks_without_approval():
    ctx = EscalationContext(
        task_id="t", risk=Risk.HIGH, cheaper_attempts=3,
        signal=EscalationSignal(reviewer_requested_escalation=True),
        est_premium_cost_usd=5.0, budget_remaining_usd=1.0,
        data_policy_allows_premium=True, approval_satisfied=False,
    )
    assert evaluate_escalation(ctx).allowed is False


def test_emergency_override_ttl_capped_and_postmortem():
    o = build_emergency_override("alice", "bob", "prod down", ttl_minutes=999)
    delta = (o.expires_at - o.created_at).total_seconds() / 60.0
    assert delta <= 60.0
    assert o.forced_backend.value == "human_only_emergency"
    assert o.post_mortem_required is True


# ---------- reliability (unchanged module, kept for coverage) ----------
def test_reliability_signal_advisory_only():
    sig = compute_signal(ReliabilityInput(
        subject_type="engineer", subject_id="e1",
        period_start="2026-07-01", period_end="2026-07-07",
        normalized_verification_failure_rate=0.6,
    ))
    assert sig.recommended_action in ("require_senior_reviewer", "admin_review")
    # Policy guarantee: core Odysseus must not budget-affect from reliability.
    assert budget_affecting_policy_allowed() is False
    assert not any("budget" in k.lower() for k in sig.to_dict().keys())


def test_reliability_confounders_soften_action():
    sig_hard = compute_signal(ReliabilityInput(
        subject_type="engineer", subject_id="e1",
        period_start="2026-07-01", period_end="2026-07-07",
        normalized_verification_failure_rate=0.8,
    ))
    sig_confounded = compute_signal(ReliabilityInput(
        subject_type="engineer", subject_id="e1",
        period_start="2026-07-01", period_end="2026-07-07",
        normalized_verification_failure_rate=0.8,
        confounders=Confounders(flaky_tests_observed=True, high_risk_task_mix=True),
    ))
    assert sig_hard.recommended_action == "admin_review"
    assert sig_confounded.recommended_action != "admin_review"
