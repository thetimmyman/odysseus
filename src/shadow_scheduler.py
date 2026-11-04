"""Shadow Adaptive Scheduler (OD-20 / Jira PS-644 / Epic TMOS-1).

Evaluates expected utility over already-eligible candidates in shadow/advisory mode.
Preserves hard policy ordering (ineligible candidates are never considered),
applies Bayesian shrinkage for low-N candidates, separates infrastructure faults
from model quality, accounts for capacity economics and stale subscription safety,
and compares 5 policy baselines.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Dict, List, Optional, Tuple


SCHEMA_NAME = "shadow-routing-recommendation-v1"


class ShadowSchedulerError(Exception):
    """Raised when shadow scheduling fails or cannot find any eligible candidates."""
    pass


@dataclass
class CandidateRanking:
    target_id: str
    profile_id: str
    rank: int
    score: float
    decomposition: Dict[str, float] = field(default_factory=dict)


@dataclass
class ShadowRecommendation:
    policy: str
    selected_target_id: str
    selected_profile_id: str
    score: float
    score_decomposition: Dict[str, float]
    candidate_rankings: List[CandidateRanking]
    explanation: str
    schema: str = SCHEMA_NAME
    disagrees_with_deterministic: bool = False
    counterfactual_reason: Optional[str] = None


@dataclass
class PolicyComparison:
    task_context: Dict[str, Any]
    policies: Dict[str, ShadowRecommendation] = field(default_factory=dict)

    def get_policy_recommendation(self, policy_name: str) -> Optional[ShadowRecommendation]:
        return self.policies.get(policy_name)


@dataclass
class PolicyEvaluationSummary:
    policy: str
    total_decisions: int
    verified_rate: float
    accepted_rate: float
    avg_cost: float
    avg_elapsed_s: float
    total_interventions: int


@dataclass
class ReplayEvaluationResult:
    total_decisions: int
    policies: Dict[str, PolicyEvaluationSummary]
    disagreement_count: int
    decision_comparisons: List[PolicyComparison] = field(default_factory=list)


class ShadowScheduler:
    """Shadow scheduler that compares empirical expected utility with deterministic baselines."""

    def __init__(self, *, quality_prior: float = 0.70, uncertainty_lambda: float = 0.20):
        self.quality_prior = quality_prior
        self.uncertainty_lambda = uncertainty_lambda

    def _score_candidate(
        self,
        candidate: Dict[str, Any],
        outcomes: Dict[str, Any],
        capacity_facts: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, float]:
        profile_id = candidate.get("profile_id", "")
        cost_rank = candidate.get("cost_rank", 1)

        fact = outcomes.get(profile_id, {})
        n = fact.get("distinct_execution_n", fact.get("n", 0))
        verified_n = fact.get("verified_n", 0)
        faults = fact.get("fault_counts", {})
        infra_faults = faults.get("infrastructure", 0)
        elapsed_s = fact.get("median_elapsed_s", 15.0)
        interventions = fact.get("operator_intervention_n", 0)

        # 1. Model quality score: Separate infrastructure faults from model quality
        # Effective model task attempts exclude infrastructure-only drops
        model_attempts = max(0, n - infra_faults)
        if model_attempts == 0:
            model_quality_score = self.quality_prior
        else:
            # Laplace smoothing to regularize small sample rates
            model_quality_score = (verified_n + 1.0) / (model_attempts + 2.0)
        model_quality_score = min(1.0, max(0.0, model_quality_score))

        # 2. Low-N Bayesian uncertainty penalty
        uncertainty_penalty = self.uncertainty_lambda / math.sqrt(n + 1.0)
        quality_score = max(0.0, model_quality_score - uncertainty_penalty)

        # 3. Cost score (lower cost rank is better)
        cost_score = max(0.0, 1.0 - 0.20 * cost_rank)

        # 4. Latency score (shorter median duration is better)
        latency_score = max(0.0, min(1.0, 1.0 - (elapsed_s / 60.0)))

        # 5. Intervention penalty score (fewer operator interventions is better)
        intervention_rate = interventions / max(1, n)
        intervention_score = max(0.0, 1.0 - min(1.0, intervention_rate))

        # 6. Capacity opportunity score
        cap_info = (capacity_facts or {}).get(profile_id, {})
        is_sub = cap_info.get("subscription", False)
        is_stale = cap_info.get("stale", False)

        if is_sub:
            if is_stale:
                # Stale subscription capacity must never be treated as free
                capacity_opportunity_score = 0.1
            else:
                # Fresh subscription capacity has high opportunity value (use it or lose it)
                capacity_opportunity_score = 1.0
        elif candidate.get("locality") == "local":
            capacity_opportunity_score = 0.8
        else:
            capacity_opportunity_score = 0.4

        # Total expected utility combining dimensions
        total_utility = (
            0.40 * quality_score
            + 0.20 * cost_score
            + 0.15 * latency_score
            + 0.15 * intervention_score
            + 0.10 * capacity_opportunity_score
        )

        return {
            "model_quality_score": round(model_quality_score, 4),
            "quality_uncertainty_penalty": round(uncertainty_penalty, 4),
            "quality_score": round(quality_score, 4),
            "cost_score": round(cost_score, 4),
            "latency_score": round(latency_score, 4),
            "intervention_score": round(intervention_score, 4),
            "capacity_opportunity_score": round(capacity_opportunity_score, 4),
            "capacity_score": round(capacity_opportunity_score, 4),
            "total_utility": round(total_utility, 4),
        }

    def evaluate(
        self,
        task_context: Dict[str, Any],
        candidates: List[Dict[str, Any]],
        outcomes: Dict[str, Any],
        capacity_facts: Optional[Dict[str, Any]] = None,
        resources: Optional[Dict[str, Any]] = None,
    ) -> PolicyComparison:
        # Step 1: Enforce hard policy ordering.
        # Only candidates explicitly marked eligible=True can be evaluated.
        eligible_candidates = [c for c in candidates if c.get("eligible") is True]
        if not eligible_candidates:
            raise ShadowSchedulerError("no eligible candidates found in screening set")

        # Step 2: Score eligible candidates
        candidate_scores: Dict[str, Dict[str, float]] = {}
        for c in eligible_candidates:
            prof_id = c.get("profile_id", "")
            candidate_scores[prof_id] = self._score_candidate(c, outcomes, capacity_facts)

        # Baseline 1: Deterministic
        # Primary sort: preference_rank ascending, secondary sort: cost_rank ascending
        det_sorted = sorted(
            eligible_candidates,
            key=lambda c: (c.get("preference_rank", 999), c.get("cost_rank", 999)),
        )
        det_pick = det_sorted[0]
        det_scores = candidate_scores[det_pick["profile_id"]]
        det_rankings = [
            CandidateRanking(
                target_id=c.get("target_id", ""),
                profile_id=c.get("profile_id", ""),
                rank=idx + 1,
                score=float(100 - idx),
                decomposition=candidate_scores[c.get("profile_id", "")],
            )
            for idx, c in enumerate(det_sorted)
        ]
        det_rec = ShadowRecommendation(
            policy="deterministic",
            selected_target_id=det_pick.get("target_id", ""),
            selected_profile_id=det_pick.get("profile_id", ""),
            score=float(100),
            score_decomposition=det_scores,
            candidate_rankings=det_rankings,
            explanation=f"Deterministic baseline selected {det_pick['profile_id']} by preference rank.",
        )

        # Baseline 2: Cheapest Eligible
        # Primary sort: cost_rank ascending, secondary: preference_rank ascending
        cheap_sorted = sorted(
            eligible_candidates,
            key=lambda c: (c.get("cost_rank", 999), c.get("preference_rank", 999)),
        )
        cheap_pick = cheap_sorted[0]
        cheap_scores = candidate_scores[cheap_pick["profile_id"]]
        cheap_rankings = [
            CandidateRanking(
                target_id=c.get("target_id", ""),
                profile_id=c.get("profile_id", ""),
                rank=idx + 1,
                score=cheap_scores["cost_score"],
                decomposition=candidate_scores[c.get("profile_id", "")],
            )
            for idx, c in enumerate(cheap_sorted)
        ]
        cheap_rec = ShadowRecommendation(
            policy="cheapest_eligible",
            selected_target_id=cheap_pick.get("target_id", ""),
            selected_profile_id=cheap_pick.get("profile_id", ""),
            score=cheap_scores["cost_score"],
            score_decomposition=cheap_scores,
            candidate_rankings=cheap_rankings,
            explanation=f"Cheapest-eligible baseline selected {cheap_pick['profile_id']} by lowest cost rank.",
        )

        # Baseline 3: Strongest Eligible
        # Primary sort: model_quality_score descending, secondary: preference_rank ascending
        strong_sorted = sorted(
            eligible_candidates,
            key=lambda c: (
                -candidate_scores[c.get("profile_id", "")]["model_quality_score"],
                c.get("preference_rank", 999),
            ),
        )
        strong_pick = strong_sorted[0]
        strong_scores = candidate_scores[strong_pick["profile_id"]]
        strong_rankings = [
            CandidateRanking(
                target_id=c.get("target_id", ""),
                profile_id=c.get("profile_id", ""),
                rank=idx + 1,
                score=strong_scores["model_quality_score"],
                decomposition=candidate_scores[c.get("profile_id", "")],
            )
            for idx, c in enumerate(strong_sorted)
        ]
        strong_rec = ShadowRecommendation(
            policy="strongest_eligible",
            selected_target_id=strong_pick.get("target_id", ""),
            selected_profile_id=strong_pick.get("profile_id", ""),
            score=strong_scores["model_quality_score"],
            score_decomposition=strong_scores,
            candidate_rankings=strong_rankings,
            explanation=f"Strongest-eligible baseline selected {strong_pick['profile_id']} by highest verified quality.",
        )

        # Baseline 4: Capacity Aware
        # Primary sort: capacity_opportunity_score descending, secondary: cost_rank ascending
        cap_sorted = sorted(
            eligible_candidates,
            key=lambda c: (
                -candidate_scores[c.get("profile_id", "")]["capacity_opportunity_score"],
                c.get("cost_rank", 999),
            ),
        )
        cap_pick = cap_sorted[0]
        cap_scores = candidate_scores[cap_pick["profile_id"]]
        cap_rankings = [
            CandidateRanking(
                target_id=c.get("target_id", ""),
                profile_id=c.get("profile_id", ""),
                rank=idx + 1,
                score=cap_scores["capacity_opportunity_score"],
                decomposition=candidate_scores[c.get("profile_id", "")],
            )
            for idx, c in enumerate(cap_sorted)
        ]
        cap_rec = ShadowRecommendation(
            policy="capacity_aware",
            selected_target_id=cap_pick.get("target_id", ""),
            selected_profile_id=cap_pick.get("profile_id", ""),
            score=cap_scores["capacity_opportunity_score"],
            score_decomposition=cap_scores,
            candidate_rankings=cap_rankings,
            explanation=f"Capacity-aware baseline selected {cap_pick['profile_id']} by subscription/local capacity economics.",
        )

        # Baseline 5: Empirical Adaptive (Shadow Advisory Engine)
        # Primary sort: total_utility descending, secondary: preference_rank ascending
        adaptive_sorted = sorted(
            eligible_candidates,
            key=lambda c: (
                -candidate_scores[c.get("profile_id", "")]["total_utility"],
                c.get("preference_rank", 999),
            ),
        )
        adaptive_pick = adaptive_sorted[0]
        adaptive_scores = candidate_scores[adaptive_pick["profile_id"]]
        adaptive_rankings = [
            CandidateRanking(
                target_id=c.get("target_id", ""),
                profile_id=c.get("profile_id", ""),
                rank=idx + 1,
                score=candidate_scores[c.get("profile_id", "")]["total_utility"],
                decomposition=candidate_scores[c.get("profile_id", "")],
            )
            for idx, c in enumerate(adaptive_sorted)
        ]

        disagrees = adaptive_pick["profile_id"] != det_pick["profile_id"]
        counterfactual_reason = None
        if disagrees:
            counterfactual_reason = (
                f"Empirical adaptive selected {adaptive_pick['profile_id']} over "
                f"deterministic {det_pick['profile_id']} due to higher expected utility "
                f"({adaptive_scores['total_utility']} vs {det_scores['total_utility']})."
            )

        adaptive_rec = ShadowRecommendation(
            policy="empirical_adaptive",
            selected_target_id=adaptive_pick.get("target_id", ""),
            selected_profile_id=adaptive_pick.get("profile_id", ""),
            score=adaptive_scores["total_utility"],
            score_decomposition=adaptive_scores,
            candidate_rankings=adaptive_rankings,
            explanation=(
                f"Empirical adaptive scheduler recommended {adaptive_pick['profile_id']} with expected "
                f"utility {adaptive_scores['total_utility']:.4f} (quality: {adaptive_scores['quality_score']:.2f}, "
                f"cost: {adaptive_scores['cost_score']:.2f}, latency: {adaptive_scores['latency_score']:.2f})."
            ),
            disagrees_with_deterministic=disagrees,
            counterfactual_reason=counterfactual_reason,
        )

        return PolicyComparison(
            task_context=task_context,
            policies={
                "deterministic": det_rec,
                "cheapest_eligible": cheap_rec,
                "strongest_eligible": strong_rec,
                "capacity_aware": cap_rec,
                "empirical_adaptive": adaptive_rec,
            },
        )


def evaluate_decision(
    task_context: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    outcomes: Dict[str, Any],
    capacity_facts: Optional[Dict[str, Any]] = None,
) -> PolicyComparison:
    """Convenience functional interface for single decision evaluation."""
    scheduler = ShadowScheduler()
    return scheduler.evaluate(
        task_context=task_context,
        candidates=candidates,
        outcomes=outcomes,
        capacity_facts=capacity_facts,
    )


def replay_evaluations(
    decisions: List[Dict[str, Any]],
    policies: Tuple[str, ...] = (
        "deterministic",
        "cheapest_eligible",
        "strongest_eligible",
        "capacity_aware",
        "empirical_adaptive",
    ),
) -> ReplayEvaluationResult:
    """Replays historical decisions across candidate policies to measure regret and benefit."""
    scheduler = ShadowScheduler()
    policy_metrics: Dict[str, Dict[str, Any]] = {
        p: {
            "verified_sum": 0.0,
            "accepted_sum": 0.0,
            "cost_sum": 0.0,
            "elapsed_sum": 0.0,
            "interventions": 0,
        }
        for p in policies
    }

    disagreements = 0
    comparisons: List[PolicyComparison] = []

    for d in decisions:
        task_context = d.get("task_context", {})
        candidates = d.get("candidates", [])
        outcomes = d.get("outcomes", {})
        capacity_facts = d.get("capacity_facts")
        actual_outcome = d.get("actual_outcome", {})

        comparison = scheduler.evaluate(
            task_context=task_context,
            candidates=candidates,
            outcomes=outcomes,
            capacity_facts=capacity_facts,
        )
        comparisons.append(comparison)

        adaptive_rec = comparison.get_policy_recommendation("empirical_adaptive")
        if adaptive_rec and adaptive_rec.disagrees_with_deterministic:
            disagreements += 1

        for p_name in policies:
            rec = comparison.get_policy_recommendation(p_name)
            if not rec:
                continue
            chosen_prof = rec.selected_profile_id
            prof_fact = outcomes.get(chosen_prof, {})
            n = prof_fact.get("distinct_execution_n", prof_fact.get("n", 1))
            v = prof_fact.get("verified_n", 0)
            a = prof_fact.get("accepted_n", v)
            interventions = prof_fact.get("operator_intervention_n", 0)

            v_rate = v / max(1, n)
            a_rate = a / max(1, n)
            cost = actual_outcome.get("cost", 0.02)
            elapsed = prof_fact.get("median_elapsed_s", actual_outcome.get("elapsed_s", 15.0))

            m = policy_metrics[p_name]
            m["verified_sum"] += v_rate
            m["accepted_sum"] += a_rate
            m["cost_sum"] += cost
            m["elapsed_sum"] += elapsed
            m["interventions"] += interventions

    total = len(decisions)
    summaries: Dict[str, PolicyEvaluationSummary] = {}
    for p_name in policies:
        m = policy_metrics[p_name]
        summaries[p_name] = PolicyEvaluationSummary(
            policy=p_name,
            total_decisions=total,
            verified_rate=round(m["verified_sum"] / max(1, total), 4),
            accepted_rate=round(m["accepted_sum"] / max(1, total), 4),
            avg_cost=round(m["cost_sum"] / max(1, total), 4),
            avg_elapsed_s=round(m["elapsed_sum"] / max(1, total), 2),
            total_interventions=int(m["interventions"]),
        )

    return ReplayEvaluationResult(
        total_decisions=total,
        policies=summaries,
        disagreement_count=disagreements,
        decision_comparisons=comparisons,
    )


def is_eligible_for_authority(
    report: ReplayEvaluationResult,
    min_sample_size: int = 50,
    min_verification_margin: float = 0.0,
) -> Tuple[bool, str]:
    """Strict earned-authority gate before advisory policy can graduate to dispatch authority."""
    if report.total_decisions < min_sample_size:
        return (
            False,
            f"Insufficient sample size: evaluated {report.total_decisions} decisions, "
            f"required minimum is {min_sample_size}.",
        )

    adaptive = report.policies.get("empirical_adaptive")
    deterministic = report.policies.get("deterministic")

    if not adaptive:
        return False, "Evaluation report missing empirical_adaptive policy summary."

    if deterministic:
        margin = adaptive.verified_rate - deterministic.verified_rate
        if margin < min_verification_margin:
            return (
                False,
                f"Verification rate margin ({margin:.4f}) did not achieve required "
                f"threshold ({min_verification_margin:.4f}) over deterministic baseline.",
            )
        return (
            True,
            f"Earned authority criteria satisfied: N={report.total_decisions} >= {min_sample_size}, "
            f"verification margin={margin:.4f} >= {min_verification_margin:.4f}.",
        )

    return True, f"Earned authority criteria satisfied: N={report.total_decisions} >= {min_sample_size}."
