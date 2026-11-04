"""Tests for shadow adaptive scheduler (OD-20 / Jira PS-644 / Epic TMOS-1).

Verifies:
1. Hard policy ordering: Ineligible candidates are NEVER recommended by any policy.
2. Low-N regularization: Small-sample arms do not acquire false precision over proven arms.
3. Fault taxonomy separation: Provider/infrastructure faults do not poison model quality estimates.
4. Stale capacity safety: Stale subscription capacity is never treated as free capacity.
5. Explainability: Decomposed utility scores and transparent narrative rationales.
6. Five-policy comparison: deterministic, cheapest, strongest, capacity_aware, empirical_adaptive.
7. Replay & earned authority gate: Phase 0/1 shadow evaluation and strict deferral criteria.
"""
from __future__ import annotations

import pytest
from src.shadow_scheduler import (
    CandidateRanking,
    PolicyComparison,
    ReplayEvaluationResult,
    ShadowRecommendation,
    ShadowScheduler,
    ShadowSchedulerError,
    evaluate_decision,
    is_eligible_for_authority,
    replay_evaluations,
)


def make_assessment(target_id: str, profile_id: str, *, eligible: bool = True,
                    rule: str = "eligible", cost_rank: int = 1,
                    preference_rank: int = 0, locality: str = "local"):
    return {
        "target_id": target_id,
        "profile_id": profile_id,
        "eligible": eligible,
        "rule": rule,
        "cost_rank": cost_rank,
        "preference_rank": preference_rank,
        "locality": locality,
    }


def make_outcome_fact(arm_key: str, *, n: int = 10, verified_n: int = 8,
                      accepted_n: int = 8, median_elapsed_s: float = 15.0,
                      infra_faults: int = 0, model_faults: int = 0,
                      operator_interventions: int = 0):
    return {
        "arm_key": arm_key,
        "distinct_execution_n": n,
        "verified_n": verified_n,
        "accepted_n": accepted_n,
        "median_elapsed_s": median_elapsed_s,
        "fault_counts": {
            "infrastructure": infra_faults,
            "model_quality": model_faults,
        },
        "operator_intervention_n": operator_interventions,
    }


def test_ineligible_candidate_is_never_recommended():
    """Negative control: Ineligible candidates are NEVER recommended, even if cheaper or higher-scoring."""
    scheduler = ShadowScheduler()
    candidates = [
        make_assessment("cheap-target", "cheap-ineligible-prof", eligible=False,
                        rule="policy_denied", cost_rank=0),
        make_assessment("expensive-target", "expensive-eligible-prof", eligible=True,
                        rule="eligible", cost_rank=3),
    ]
    outcomes = {
        "cheap-ineligible-prof": make_outcome_fact("cheap-ineligible-prof", n=100, verified_n=100),
        "expensive-eligible-prof": make_outcome_fact("expensive-eligible-prof", n=100, verified_n=90),
    }

    comparison = scheduler.evaluate(
        task_context={"task_class": "bugfix", "role": "implementer"},
        candidates=candidates,
        outcomes=outcomes,
    )

    # Every policy must recommend ONLY the eligible profile
    for policy_name in ("deterministic", "cheapest_eligible", "strongest_eligible",
                        "capacity_aware", "empirical_adaptive"):
        rec = comparison.get_policy_recommendation(policy_name)
        assert rec is not None
        assert rec.selected_profile_id == "expensive-eligible-prof"
        assert rec.selected_target_id == "expensive-target"

    # All candidate rankings in recommendation must only contain eligible candidates
    adaptive = comparison.get_policy_recommendation("empirical_adaptive")
    ranked_profiles = [r.profile_id for r in adaptive.candidate_rankings]
    assert "cheap-ineligible-prof" not in ranked_profiles
    assert ranked_profiles == ["expensive-eligible-prof"]


def test_no_eligible_candidates_fails_closed():
    """Fail-closed: When no candidate is eligible, scheduler raises typed error."""
    scheduler = ShadowScheduler()
    candidates = [
        make_assessment("target-1", "prof-1", eligible=False, rule="policy_denied"),
        make_assessment("target-2", "prof-2", eligible=False, rule="capacity_missing"),
    ]
    with pytest.raises(ShadowSchedulerError, match="no eligible candidates"):
        scheduler.evaluate(
            task_context={"task_class": "general"},
            candidates=candidates,
            outcomes={},
        )


def test_low_n_shrinkage_prevents_false_precision():
    """Negative control: 1/1 (100%) arm does not outrank a proven 95/100 (95%) arm."""
    scheduler = ShadowScheduler()
    candidates = [
        make_assessment("t-unproven", "prof-unproven", eligible=True, cost_rank=1),
        make_assessment("t-proven", "prof-proven", eligible=True, cost_rank=1),
    ]
    # prof-unproven: N=1, verified=1 (100% naive)
    # prof-proven: N=100, verified=95 (95% empirical)
    outcomes = {
        "prof-unproven": make_outcome_fact("prof-unproven", n=1, verified_n=1, accepted_n=1),
        "prof-proven": make_outcome_fact("prof-proven", n=100, verified_n=95, accepted_n=95),
    }

    comparison = scheduler.evaluate(
        task_context={"task_class": "general", "quality_floor": 0.70},
        candidates=candidates,
        outcomes=outcomes,
    )
    rec = comparison.get_policy_recommendation("empirical_adaptive")
    assert rec.selected_profile_id == "prof-proven"
    assert rec.score_decomposition["quality_uncertainty_penalty"] > 0


def test_infrastructure_faults_do_not_poison_quality_estimates():
    """Negative control: Infrastructure faults (timeouts, socket errors) do not degrade model quality."""
    scheduler = ShadowScheduler()
    candidates = [
        make_assessment("t-infra", "prof-infra-drop", eligible=True, cost_rank=1),
        make_assessment("t-broken", "prof-code-broken", eligible=True, cost_rank=1),
    ]
    # Both had 10 total attempts.
    # prof-infra-drop: 8 verified, 2 infra faults (model generated valid code when network answered)
    # prof-code-broken: 8 verified, 2 model code faults (syntax/assertion failures)
    outcomes = {
        "prof-infra-drop": make_outcome_fact("prof-infra-drop", n=10, verified_n=8, infra_faults=2, model_faults=0),
        "prof-code-broken": make_outcome_fact("prof-code-broken", n=10, verified_n=8, infra_faults=0, model_faults=2),
    }

    comparison = scheduler.evaluate(
        task_context={"task_class": "feature_implementation"},
        candidates=candidates,
        outcomes=outcomes,
    )
    rec = comparison.get_policy_recommendation("empirical_adaptive")
    rankings = {r.profile_id: r for r in rec.candidate_rankings}
    assert rankings["prof-infra-drop"].decomposition["model_quality_score"] > rankings["prof-code-broken"].decomposition["model_quality_score"]


def test_stale_subscription_capacity_not_treated_as_free():
    """Negative control: Expired/stale subscription facts cannot be treated as free capacity."""
    scheduler = ShadowScheduler()
    candidates = [
        make_assessment("t-stale", "prof-stale-sub", eligible=True, cost_rank=1),
        make_assessment("t-fresh", "prof-fresh-sub", eligible=True, cost_rank=1),
    ]
    capacity_facts = {
        "prof-stale-sub": {"subscription": True, "stale": True, "remaining_quota": 1000},
        "prof-fresh-sub": {"subscription": True, "stale": False, "remaining_quota": 1000, "reset_horizon_hours": 2.0},
    }
    outcomes = {
        "prof-stale-sub": make_outcome_fact("prof-stale-sub", n=50, verified_n=45),
        "prof-fresh-sub": make_outcome_fact("prof-fresh-sub", n=50, verified_n=45),
    }

    comparison = scheduler.evaluate(
        task_context={"task_class": "general"},
        candidates=candidates,
        outcomes=outcomes,
        capacity_facts=capacity_facts,
    )
    capacity_policy = comparison.get_policy_recommendation("capacity_aware")
    assert capacity_policy.selected_profile_id == "prof-fresh-sub"

    adaptive = comparison.get_policy_recommendation("empirical_adaptive")
    rankings = {r.profile_id: r for r in adaptive.candidate_rankings}
    assert rankings["prof-fresh-sub"].decomposition["capacity_opportunity_score"] > rankings["prof-stale-sub"].decomposition["capacity_opportunity_score"]


def test_explainable_decomposed_utility_scoring():
    """Explainability: Recommendation provides transparent decomposed subscores and rationale."""
    candidates = [
        make_assessment("t-1", "prof-1", eligible=True, cost_rank=1),
        make_assessment("t-2", "prof-2", eligible=True, cost_rank=2),
    ]
    outcomes = {
        "prof-1": make_outcome_fact("prof-1", n=20, verified_n=18, operator_interventions=0),
        "prof-2": make_outcome_fact("prof-2", n=20, verified_n=12, operator_interventions=5),
    }

    comparison = evaluate_decision(
        task_context={"task_class": "general"},
        candidates=candidates,
        outcomes=outcomes,
    )
    assert isinstance(comparison, PolicyComparison)
    rec = comparison.get_policy_recommendation("empirical_adaptive")
    assert isinstance(rec, ShadowRecommendation)
    assert all(isinstance(r, CandidateRanking) for r in rec.candidate_rankings)

    assert rec.schema == "shadow-routing-recommendation-v1"
    assert "quality_score" in rec.score_decomposition
    assert "cost_score" in rec.score_decomposition
    assert "latency_score" in rec.score_decomposition
    assert "intervention_score" in rec.score_decomposition
    assert "capacity_score" in rec.score_decomposition
    assert len(rec.explanation) > 20
    assert "prof-1" in rec.explanation
    assert rec.selected_profile_id == "prof-1"


def test_five_policy_baselines_comparable():
    """All 5 baselines are computed and compared simultaneously on the same candidate set."""
    scheduler = ShadowScheduler()
    candidates = [
        make_assessment("t-static", "prof-static-preferred", eligible=True, preference_rank=0, cost_rank=2),
        make_assessment("t-cheap", "prof-cheapest", eligible=True, preference_rank=1, cost_rank=0),
        make_assessment("t-capable", "prof-strongest", eligible=True, preference_rank=2, cost_rank=3),
    ]
    outcomes = {
        "prof-static-preferred": make_outcome_fact("prof-static-preferred", n=30, verified_n=20),
        "prof-cheapest": make_outcome_fact("prof-cheapest", n=30, verified_n=15),
        "prof-strongest": make_outcome_fact("prof-strongest", n=30, verified_n=29),
    }

    comparison = scheduler.evaluate(
        task_context={"task_class": "complex_refactor"},
        candidates=candidates,
        outcomes=outcomes,
    )

    det = comparison.get_policy_recommendation("deterministic")
    cheap = comparison.get_policy_recommendation("cheapest_eligible")
    strong = comparison.get_policy_recommendation("strongest_eligible")
    adaptive = comparison.get_policy_recommendation("empirical_adaptive")

    assert det.selected_profile_id == "prof-static-preferred"
    assert cheap.selected_profile_id == "prof-cheapest"
    assert strong.selected_profile_id == "prof-strongest"
    assert adaptive.selected_profile_id in {"prof-strongest", "prof-static-preferred"}


def test_counterfactual_disagreement_recorded():
    """Counterfactual tracking: Disagreement between adaptive and deterministic is flagged with reason."""
    scheduler = ShadowScheduler()
    candidates = [
        make_assessment("t-det", "prof-det-default", eligible=True, preference_rank=0, cost_rank=2),
        make_assessment("t-better", "prof-adaptive-winner", eligible=True, preference_rank=1, cost_rank=1),
    ]
    outcomes = {
        "prof-det-default": make_outcome_fact("prof-det-default", n=40, verified_n=20),
        "prof-adaptive-winner": make_outcome_fact("prof-adaptive-winner", n=40, verified_n=38),
    }

    comparison = scheduler.evaluate(
        task_context={"task_class": "bugfix"},
        candidates=candidates,
        outcomes=outcomes,
    )

    adaptive = comparison.get_policy_recommendation("empirical_adaptive")
    assert adaptive.selected_profile_id == "prof-adaptive-winner"
    assert adaptive.disagrees_with_deterministic is True
    assert adaptive.counterfactual_reason is not None
    assert "prof-adaptive-winner" in adaptive.counterfactual_reason


def test_replay_evaluations_and_earned_authority_verdict():
    """Replay corpus evaluations: Measures regret/benefit and applies earned authority gate."""
    decisions = [
        {
            "task_context": {"task_class": "bugfix", "quality_floor": 0.70},
            "candidates": [
                make_assessment("t-1", "prof-a", eligible=True, preference_rank=0, cost_rank=1),
                make_assessment("t-2", "prof-b", eligible=True, preference_rank=1, cost_rank=1),
            ],
            "outcomes": {
                "prof-a": make_outcome_fact("prof-a", n=20, verified_n=12),  # 60%
                "prof-b": make_outcome_fact("prof-b", n=20, verified_n=19),  # 95%
            },
            "actual_outcome": {"verified": True, "accepted": True, "cost": 0.05, "elapsed_s": 12.0}
        } for _ in range(15)
    ]

    report = replay_evaluations(decisions)
    assert isinstance(report, ReplayEvaluationResult)
    assert report.total_decisions == 15
    assert "deterministic" in report.policies
    assert "empirical_adaptive" in report.policies

    # Adaptive consistently picked prof-b (higher verified rate)
    adaptive_summary = report.policies["empirical_adaptive"]
    deterministic_summary = report.policies["deterministic"]
    assert adaptive_summary.verified_rate >= deterministic_summary.verified_rate

    # Authority gate check
    earned, verdict_reason = is_eligible_for_authority(report, min_sample_size=10, min_verification_margin=0.0)
    assert earned is True
    assert "earned" in verdict_reason.lower()

    # Negative control: if sample size is insufficient, authority is deferred
    earned_low_n, reason_low_n = is_eligible_for_authority(report, min_sample_size=100)
    assert earned_low_n is False
    assert "insufficient" in reason_low_n.lower()
