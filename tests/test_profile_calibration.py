"""Tests for profile calibration and engineering work measurement (OD-2 / Jira PS-579 / Epic TMOS-1).

Verifies:
1. Reviewer authority remains unearned: Qwen local semantic review authority is strictly
   flag-for-human-read only under historical baseline (19 PRs, 41% false approve).
2. Preregistered held-out auto-approve rule: enforces N>=10, zero false approves, at least
   one known defective member correctly rejected, and verified reproducibility on a held-out sample.
3. Honest reporting of unobserved repair/replan cells: when no natural attempt-1 failure occurs,
   repair uplift is reported as UNOBSERVED; failures are never manufactured.
4. Natural failure capture: genuine technical failures that recover under G1 are quantified truthfully.
5. Fault taxonomy separation: host/runtime stability faults are isolated from model task failures.
6. Acceptance metric: accepted work per hour under correctness gates governs, not raw tok/s.
7. Three-layer corpus and orchestration arms: G0 (raw), G1 (bounded repair), G2 (bounded replan)
   across internal and external corpora.
"""
from __future__ import annotations

import pytest
from src.profile_calibration import (
    CorpusLayer,
    OrchestrationArm,
    ProfileArm,
    ProfileCalibrationRunner,
    ReviewerCalibration,
    ReviewVerdict,
    evaluate_auto_approve_rule,
)


def test_reviewer_authority_remains_unearned():
    """Negative control: Local Qwen review authority remains strictly flag-for-human-read."""
    reviewer = ReviewerCalibration()
    baseline = reviewer.get_historical_baseline()

    assert baseline.total_prs == 19
    assert baseline.closed_unmerged_n == 17
    assert baseline.merged_positive_n == 2
    assert baseline.false_approve_rate == pytest.approx(7.0 / 17.0, rel=1e-3)
    assert baseline.false_reject_rate == pytest.approx(2.0 / 2.0, rel=1e-3)

    verdict, rationale = reviewer.evaluate_profile_reviewer_authority("rtx4500-qwen27b")
    assert verdict == ReviewVerdict.FLAG_FOR_HUMAN_READ_ONLY
    assert "unearned" in rationale.lower()
    assert reviewer.is_auto_approve_authorized("rtx4500-qwen27b") is False


def test_preregistered_auto_approve_rule_rejects_insufficient_or_imperfect_sample():
    """Negative control: Auto-approve rule rejects low-N, false approves, or lack of defect rejection."""
    # Case 1: Low sample size (N=9 < 10)
    sample_low_n = [
        {"pr_id": f"pr-{i}", "is_defective": False, "approved": True} for i in range(8)
    ] + [{"pr_id": "pr-bad", "is_defective": True, "approved": False}]
    held_out = sample_low_n.copy()

    passed, reason = evaluate_auto_approve_rule(sample_low_n, held_out)
    assert passed is False
    assert "sample size" in reason.lower()

    # Case 2: False approve present (1 false approve in 10 items)
    sample_false_approve = [
        {"pr_id": f"pr-{i}", "is_defective": False, "approved": True} for i in range(8)
    ] + [
        {"pr_id": "pr-bad-1", "is_defective": True, "approved": False},
        {"pr_id": "pr-bad-2", "is_defective": True, "approved": True},  # False approve!
    ]
    passed_fa, reason_fa = evaluate_auto_approve_rule(sample_false_approve, held_out)
    assert passed_fa is False
    assert "false approve" in reason_fa.lower()

    # Case 3: No known defective member tested (all 10 were good PRs)
    sample_no_defect = [
        {"pr_id": f"pr-{i}", "is_defective": False, "approved": True} for i in range(12)
    ]
    passed_nd, reason_nd = evaluate_auto_approve_rule(sample_no_defect, sample_no_defect)
    assert passed_nd is False
    assert "known defective" in reason_nd.lower()

    # Case 4: Primary sample passes but held-out fails
    sample_good = [
        {"pr_id": f"pr-{i}", "is_defective": False, "approved": True} for i in range(10)
    ] + [{"pr_id": "pr-bad", "is_defective": True, "approved": False}]
    held_out_bad = [
        {"pr_id": f"pr-h-{i}", "is_defective": False, "approved": True} for i in range(10)
    ] + [{"pr_id": "pr-h-bad", "is_defective": True, "approved": True}]  # False approve on held-out

    passed_ho, reason_ho = evaluate_auto_approve_rule(sample_good, held_out_bad)
    assert passed_ho is False
    assert "held-out" in reason_ho.lower()

    # Case 5: Fully compliant primary + held-out samples earn approval
    held_out_good = [
        {"pr_id": f"pr-h-{i}", "is_defective": False, "approved": True} for i in range(10)
    ] + [{"pr_id": "pr-h-bad", "is_defective": True, "approved": False}]
    passed_earn, reason_earn = evaluate_auto_approve_rule(sample_good, held_out_good)
    assert passed_earn is True
    assert "earned" in reason_earn.lower()


def test_honest_reporting_of_unobserved_repair_and_replan_cells():
    """Negative control: When all packets pass on attempt 1, repair and replan are UNOBSERVED."""
    runner = ProfileCalibrationRunner()
    tasks = [
        {"task_id": f"t-{i}", "corpus": CorpusLayer.INTERNAL_SOURCE_BOUND,
         "first_attempt_pass": True, "elapsed_s": 25.0, "output_tokens": 400}
        for i in range(5)
    ]

    report = runner.calibrate_profile(
        profile=ProfileArm(profile_id="rtx4500-qwen27b", hardware_target="rtx4500",
                           model_name="qwen3.8:27b", backend="vllm"),
        arm=OrchestrationArm.G1,
        tasks=tasks,
    )

    # When all 5 tasks succeeded on attempt 1, repair uplift must be honestly reported as UNOBSERVED
    assert report.total_tasks == 5
    assert report.passed_tasks == 5
    assert report.natural_repair_uplift == "UNOBSERVED"
    assert report.validated_replan_uplift == "UNOBSERVED"
    assert report.challenger_state == "DEFERRED"


def test_natural_technical_failure_repair_uplift_captured():
    """Natural failure capture: A genuine first-attempt failure recovered by G1 repair is quantified."""
    runner = ProfileCalibrationRunner()
    tasks = [
        {"task_id": "t-1", "corpus": CorpusLayer.INTERNAL_SOURCE_BOUND,
         "first_attempt_pass": True, "elapsed_s": 20.0, "output_tokens": 300},
        {"task_id": "t-2", "corpus": CorpusLayer.INTERNAL_SOURCE_BOUND,
         "first_attempt_pass": False, "post_repair_pass": True, "attempts": 2,
         "elapsed_s": 45.0, "output_tokens": 650},
        {"task_id": "t-3", "corpus": CorpusLayer.INTERNAL_SOURCE_BOUND,
         "first_attempt_pass": True, "elapsed_s": 22.0, "output_tokens": 310},
    ]

    report = runner.calibrate_profile(
        profile=ProfileArm(profile_id="rtx4500-qwen27b", hardware_target="rtx4500",
                           model_name="qwen3.8:27b", backend="vllm"),
        arm=OrchestrationArm.G1,
        tasks=tasks,
    )

    assert report.total_tasks == 3
    assert report.passed_tasks == 3
    assert report.first_attempt_passed_tasks == 2
    assert report.post_repair_passed_tasks == 1
    # Uplift is measured: 1 task recovered / 1 failed attempt 1 = 100% recovery (+33.3% net pass rate)
    assert isinstance(report.natural_repair_uplift, float)
    assert report.natural_repair_uplift > 0.0


def test_stability_fault_taxonomy_isolation():
    """Negative control: Infrastructure/runtime crashes do not poison model implementation quality."""
    runner = ProfileCalibrationRunner()
    tasks = [
        {"task_id": "t-ok", "corpus": CorpusLayer.INTERNAL_SOURCE_BOUND,
         "first_attempt_pass": True, "elapsed_s": 30.0},
        {"task_id": "t-cuda-crash", "corpus": CorpusLayer.INTERNAL_SOURCE_BOUND,
         "first_attempt_pass": False, "stability_fault": True, "elapsed_s": 5.0},
        {"task_id": "t-code-defect", "corpus": CorpusLayer.INTERNAL_SOURCE_BOUND,
         "first_attempt_pass": False, "stability_fault": False, "elapsed_s": 25.0},
    ]

    report = runner.calibrate_profile(
        profile=ProfileArm(profile_id="framework-strix-hip", hardware_target="framework",
                           model_name="qwen3.8:27b", backend="hip"),
        arm=OrchestrationArm.G1,
        tasks=tasks,
    )

    assert report.stability_fault_count == 1
    assert report.model_defect_count == 1
    # Operational stability rate: 2 non-crashed / 3 total = 66.7%
    assert report.operational_stability_rate == pytest.approx(2.0 / 3.0, rel=1e-3)


def test_accepted_work_per_hour_governs_over_raw_throughput():
    """Negative control: Fast throughput that fails verifiers produces zero accepted work per hour."""
    runner = ProfileCalibrationRunner()
    # Fast profile: 200 tok/s, but fails verification on all tasks
    fast_failing_tasks = [
        {"task_id": f"t-f-{i}", "corpus": CorpusLayer.INTERNAL_SOURCE_BOUND,
         "first_attempt_pass": False, "post_repair_pass": False,
         "elapsed_s": 10.0, "output_tokens": 2000}
        for i in range(5)
    ]
    fast_report = runner.calibrate_profile(
        profile=ProfileArm(profile_id="fast-failing", hardware_target="test",
                           model_name="fast-model", backend="api"),
        arm=OrchestrationArm.G1,
        tasks=fast_failing_tasks,
    )

    # Slow profile: 25 tok/s, but passes verification on all tasks
    slow_passing_tasks = [
        {"task_id": f"t-s-{i}", "corpus": CorpusLayer.INTERNAL_SOURCE_BOUND,
         "first_attempt_pass": True, "elapsed_s": 120.0, "output_tokens": 3000}
        for i in range(5)
    ]
    slow_report = runner.calibrate_profile(
        profile=ProfileArm(profile_id="slow-passing", hardware_target="test",
                           model_name="slow-model", backend="vllm"),
        arm=OrchestrationArm.G1,
        tasks=slow_passing_tasks,
    )

    # Fast failing arm produces ZERO accepted work per hour despite emitting 10,000 tokens
    assert fast_report.passed_tasks == 0
    assert fast_report.accepted_work_per_hour == 0.0

    # Slow passing arm produces positive accepted work per hour
    # 5 tasks in 600s (0.1667 hours) -> 30 tasks / hour
    assert slow_report.passed_tasks == 5
    assert slow_report.accepted_work_per_hour == pytest.approx(30.0, rel=1e-2)
    assert slow_report.accepted_work_per_hour > fast_report.accepted_work_per_hour


def test_three_orchestration_arms_comparison():
    """G0 (raw), G1 (bounded repair), and G2 (bounded replan) compared on same corpus."""
    runner = ProfileCalibrationRunner()
    # Task 1 passes G0, G1, G2
    # Task 2 fails G0, but G1 repairs it
    # Task 3 fails G0 and G1, but G2 replan succeeds
    tasks_g0 = [
        {"task_id": "t-1", "first_attempt_pass": True, "elapsed_s": 30.0},
        {"task_id": "t-2", "first_attempt_pass": False, "elapsed_s": 30.0},
        {"task_id": "t-3", "first_attempt_pass": False, "elapsed_s": 30.0},
    ]
    tasks_g1 = [
        {"task_id": "t-1", "first_attempt_pass": True, "elapsed_s": 30.0},
        {"task_id": "t-2", "first_attempt_pass": False, "post_repair_pass": True, "attempts": 2, "elapsed_s": 60.0},
        {"task_id": "t-3", "first_attempt_pass": False, "post_repair_pass": False, "attempts": 3, "elapsed_s": 90.0},
    ]
    tasks_g2 = [
        {"task_id": "t-1", "first_attempt_pass": True, "elapsed_s": 30.0},
        {"task_id": "t-2", "first_attempt_pass": False, "post_repair_pass": True, "attempts": 2, "elapsed_s": 60.0},
        {"task_id": "t-3", "first_attempt_pass": False, "post_repair_pass": False, "replan_pass": True, "attempts": 4, "elapsed_s": 120.0},
    ]

    profile = ProfileArm(profile_id="rtx4500-qwen27b", hardware_target="rtx4500",
                         model_name="qwen3.8:27b", backend="vllm")

    comparison = runner.compare_orchestration_arms(
        profile=profile,
        corpus=CorpusLayer.INTERNAL_SOURCE_BOUND,
        arm_tasks={
            OrchestrationArm.G0: tasks_g0,
            OrchestrationArm.G1: tasks_g1,
            OrchestrationArm.G2: tasks_g2,
        },
    )

    assert comparison.arm_reports[OrchestrationArm.G0].passed_tasks == 1
    assert comparison.arm_reports[OrchestrationArm.G1].passed_tasks == 2
    assert comparison.arm_reports[OrchestrationArm.G2].passed_tasks == 3

    # Progression: G2 pass rate > G1 pass rate > G0 pass rate
    assert comparison.arm_reports[OrchestrationArm.G2].pass_rate > comparison.arm_reports[OrchestrationArm.G1].pass_rate
    assert comparison.arm_reports[OrchestrationArm.G1].pass_rate > comparison.arm_reports[OrchestrationArm.G0].pass_rate
    assert comparison.schema == "calib-orchestration-comparison-v1"
