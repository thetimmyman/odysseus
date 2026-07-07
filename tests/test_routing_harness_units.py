
"""Unit tests for the v0.5 harness policy modules (coordinator / escalation / reliability)."""
from src.routing_coordinator import (
    CoordinatorDecision,
    GateContext,
    parse_decision,
    SchemaVersionError,
    wrap_coordinator_output,
    SCHEMA_VERSION,
    DataSensitivity,
)
from src.routing_escalation import (
    build_emergency_override,
    EscalationContext,
    EscalationSignal,
    evaluate_escalation,
    Risk,
)
from src.routing_reliability import (
    compute_signal,
    Confounders,
    ReliabilityInput,
    budget_affecting_policy_allowed,
)

VALID_DECISION = {
    "schemaVersion": "0.5",
    "taskId": "t1",
    "classification": {
        "domain": "general_swe", "taskType": "bug_debug",
        "risk": "low", "dataSensitivity": "internal", "verificationMode": "advisory",
    },
    "contextRequest": {"sources": ["repo"], "includeTests": True, "includeLogs": False},
    "routeRecommendation": {
        "backend": "odysseus_general_swe",
        "modelRoleChain": [{"role": "scout", "reason": "cheap"}],
        "allowPremium": False,
    },
    "budgetRecommendation": {"maxCostUsd": 0.01, "preferFree": True},
    "approvalRecommendation": {"required": False, "level": "none"},
    "confidence": {"score": 0.6, "basis": "metadata"},
    "rationale": ["low risk", "cheap model can scout"],
}


def test_parse_decision_roundtrip():
    d = parse_decision(VALID_DECISION)
    assert isinstance(d, CoordinatorDecision)
    assert d.schemaVersion == SCHEMA_VERSION
    assert d.classification.risk.value == "low"


def test_unknown_schema_version_fails_closed():
    bad = dict(VALID_DECISION, schemaVersion="9.9")
    try:
        parse_decision(bad)
        assert False, "expected SchemaVersionError"
    except SchemaVersionError:
        pass


def test_wrapper_passes_clean_decision():
    g = GateContext(task_id="t1", backend_available=True, budget_ok=True, approval_satisfied=False)
    res = wrap_coordinator_output(__import__("json").dumps(VALID_DECISION), g)
    assert res.ok and res.decision is not None
    assert res.appliedFallback is False
    assert res.route["backend"] == "odysseus_general_swe"


def test_wrapper_falls_back_on_bad_json():
    g = GateContext(task_id="t1")
    res = wrap_coordinator_output("not json at all {{{", g)
    assert res.appliedFallback and res.route["backend"] == "odysseus_general_swe"
    assert res.fallbackPath == "safe_scout"


def test_restricted_data_remote_blocked_when_not_approved():
    d = dict(VALID_DECISION)
    d["classification"] = dict(d["classification"], dataSensitivity="restricted")
    d["routeRecommendation"] = dict(d["routeRecommendation"], backend="openrouter", allowPremium=True)
    g = GateContext(task_id="t2", data_policy_allows_remote=False, budget_ok=True)
    # parse still ok; gate blocks -> fallback
    res = wrap_coordinator_output(__import__("json").dumps(d), g)
    assert res.appliedFallback
    assert any("restricted_data_remote_blocked" in e for e in res.validationErrors)


def test_approval_gate_unsatisfied_blocks():
    d = dict(VALID_DECISION)
    d["approvalRecommendation"] = {"required": True, "level": "reviewer"}
    g = GateContext(task_id="t3", approval_satisfied=False)
    res = wrap_coordinator_output(__import__("json").dumps(d), g)
    assert res.appliedFallback
    assert any("approval_gate_unsatisfied" in e for e in res.validationErrors)


def test_escalation_all_conditions_met():
    ctx = EscalationContext(
        task_id="t", risk=Risk.HIGH, cheaper_attempts=3,
        signal=EscalationSignal(tests_still_fail=True),
        est_premium_cost_usd=0.5, budget_remaining_usd=1.0,
        data_policy_allows_premium=True, approval_satisfied=True,
    )
    v = evaluate_escalation(ctx)
    assert v.allowed is True


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
    v = evaluate_escalation(ctx)
    assert v.allowed is False


def test_emergency_override_ttl_capped():
    o = build_emergency_override("alice", "bob", "prod down", ttl_minutes=999)
    from datetime import datetime, timezone, timedelta
    delta = (o.expires_at - o.created_at).total_seconds() / 60.0
    assert delta <= 60.0
    assert o.forced_backend.value == "human_only_emergency"
    assert o.post_mortem_required is True


def test_emergency_override_expiry():
    from datetime import datetime, timezone, timedelta
    o = build_emergency_override("alice", "bob", "x", ttl_minutes=1)
    future = o.expires_at + timedelta(minutes=5)
    assert o.expired_or_inactive(now=future) is True


def test_reliability_signal_advisory_only():
    sig = compute_signal(ReliabilityInput(
        subject_type="engineer", subject_id="e1",
        period_start="2026-07-01", period_end="2026-07-07",
        normalized_verification_failure_rate=0.6,
    ))
    assert sig.recommended_action in ("require_senior_reviewer", "admin_review")
    # Policy guarantee: core Odysseus must not budget-affect from reliability.
    assert budget_affecting_policy_allowed() is False


def test_reliability_signal_low_rate_none():
    sig = compute_signal(ReliabilityInput(
        subject_type="team", subject_id="t1",
        period_start="2026-07-01", period_end="2026-07-07",
        normalized_verification_failure_rate=0.02,
    ))
    assert sig.recommended_action == "none"


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
    # With confounders present, the strongest "admin_review" is softened.
    assert sig_hard.recommended_action == "admin_review"
    assert sig_confounded.recommended_action != "admin_review"



def test_escalation_allowed_when_exhausted_and_approved():
    from src.routing_escalation import (
        EscalationContext, EscalationSignal, Risk, evaluate_escalation,
    )
    # HIGH risk + exhausted cheap attempts (condition 1) + a real §11.2 signal
    # (cheap models disagree on root cause) + budget ok + approved => allowed.
    ctx = EscalationContext(
        task_id="t-e",
        risk=Risk.HIGH,
        cheaper_attempts=3,
        max_cheaper_attempts=2,
        signal=EscalationSignal(cheap_models_disagree=True),
        est_premium_cost_usd=1.0,
        budget_remaining_usd=5.0,
        data_policy_allows_premium=True,
        approval_satisfied=True,
    )
    v = evaluate_escalation(ctx)
    assert v.allowed is True, v.reasons


def test_escalation_denied_when_low_risk_fresh_attempts():
    from src.routing_escalation import (
        EscalationContext, EscalationSignal, Risk, evaluate_escalation,
    )
    ctx = EscalationContext(
        task_id="t-e2", risk=Risk.LOW, cheaper_attempts=0, max_cheaper_attempts=2,
        signal=EscalationSignal(), approval_satisfied=False,
    )
    v = evaluate_escalation(ctx)
    assert v.allowed is False


def test_emergency_build_includes_ttl_and_postmortem():
    from src.routing_escalation import build_emergency_override, DEFAULT_EMERGENCY_TTL_MINUTES
    o = build_emergency_override("alice", "bob", "prod down", ttl_minutes=30)
    assert o.requested_by == "alice"
    assert o.approved_by == "bob"
    assert o.post_mortem_required is True
    assert o.forced_backend.value == "human_only_emergency"
    # TTL capped at default max even when a larger value is requested.
    from datetime import timedelta
    o2 = build_emergency_override("bob", "carol", "x", ttl_minutes=9999)
    ttl_min = (o2.expires_at - o2.created_at).total_seconds() / 60.0
    assert 0 < ttl_min <= DEFAULT_EMERGENCY_TTL_MINUTES


def test_reliability_no_budget_signal():
    from src.routing_reliability import (
        ReliabilityInput, Confounders, compute_signal, ReviewAction,
    )
    sig = compute_signal(ReliabilityInput(
        subject_type="repo", subject_id="svc", period_start="2026-07-01",
        period_end="2026-07-07", normalized_verification_failure_rate=0.9,
        lesson_review_participation_rate=0.1, avg_validated_lesson_quality=0.2,
        confounders=Confounders(flaky_tests_observed=True),
    ))
    # High failure rate + no lesson participation => senior reviewer / admin.
    assert sig.recommended_action in (
        ReviewAction.REQUIRE_SENIOR_REVIEWER, ReviewAction.ADMIN_REVIEW,
        ReviewAction.REQUIRE_COACHING_REVIEW, ReviewAction.INCREASE_REVIEW_DEPTH,
    )
    # CRITICAL: reliability monitor must NEVER carry a budget lever. The signal
    # type has no budget attribute at all, and to_dict() must not expose one.
    # The ReviewReadinessSignal type has NO budget attribute by construction, and
    # to_dict() must not expose any budget lever.
    assert not hasattr(sig, "budget_delta_usd")
    assert not hasattr(sig, "can_reduce_budget")
    assert not any("budget" in k.lower() for k in sig.to_dict().keys())
