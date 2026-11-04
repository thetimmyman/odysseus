"""Profile calibration and accepted engineering work measurement (OD-2 / Jira PS-579 / Epic TMOS-1).

Calibrates Qwen-first and local worker profiles across:
1. Three orchestration arms: G0 (raw baseline), G1 (bounded repair), G2 (bounded replan).
2. Honest reporting of unobserved repair/replan cells without synthetic failures.
3. Reviewer authority evaluation and preregistered held-out auto-approval rules.
4. Fault taxonomy: runtime/host stability faults decoupled from task quality defects.
5. Accepted work per wall-clock hour under deterministic gates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Tuple, Union


SCHEMA_REPORT = "calib-profile-report-v1"
SCHEMA_COMPARISON = "calib-orchestration-comparison-v1"


class OrchestrationArm(str, Enum):
    G0 = "G0"  # Historically representative raw/local worker baseline
    G1 = "G1"  # Bounded execution loop: WorkPacket -> context -> verifier -> repair retry
    G2 = "G2"  # Frozen G1 + bounded validated replanner at eligible boundaries


class CorpusLayer(str, Enum):
    INTERNAL_SOURCE_BOUND = "internal_source_bound"
    TERMINAL_BENCH_MINI = "terminal_bench_mini"
    PI_LOCAL_CODING_BENCH = "pi_local_coding_bench"


class ReviewVerdict(str, Enum):
    FLAG_FOR_HUMAN_READ_ONLY = "FLAG_FOR_HUMAN_READ_ONLY"
    AUTO_APPROVE_QUALIFIED = "AUTO_APPROVE_QUALIFIED"


@dataclass(frozen=True)
class HistoricalReviewerBaseline:
    total_prs: int = 19
    closed_unmerged_n: int = 17
    merged_positive_n: int = 2
    false_approve_rate: float = 7.0 / 17.0
    false_reject_rate: float = 2.0 / 2.0
    pair_separation: float = 0.0


@dataclass(frozen=True)
class ProfileArm:
    profile_id: str
    hardware_target: str
    model_name: str
    backend: str = "vllm"
    role: str = "worker"


@dataclass
class CalibrationReport:
    schema: str
    profile: ProfileArm
    arm: OrchestrationArm
    total_tasks: int
    passed_tasks: int
    first_attempt_passed_tasks: int
    post_repair_passed_tasks: int
    pass_rate: float
    natural_repair_uplift: Union[str, float]
    validated_replan_uplift: Union[str, float]
    challenger_state: str
    total_elapsed_s: float
    accepted_work_per_hour: float
    stability_fault_count: int = 0
    model_defect_count: int = 0
    operational_stability_rate: float = 1.0


@dataclass
class OrchestrationComparison:
    schema: str
    profile: ProfileArm
    corpus: CorpusLayer
    arm_reports: Dict[OrchestrationArm, CalibrationReport] = field(default_factory=dict)


class ReviewerCalibration:
    """Manages semantic reviewer authority and baseline governance."""

    def __init__(self):
        self._baseline = HistoricalReviewerBaseline()

    def get_historical_baseline(self) -> HistoricalReviewerBaseline:
        return self._baseline

    def evaluate_profile_reviewer_authority(self, profile_id: str) -> Tuple[ReviewVerdict, str]:
        # Under the authoritative historical baseline, local Qwen has 41% false approve rate
        return (
            ReviewVerdict.FLAG_FOR_HUMAN_READ_ONLY,
            f"Reviewer authority unearned for {profile_id}: historical baseline has "
            f"false approve rate {self._baseline.false_approve_rate:.1%} and "
            f"false reject rate {self._baseline.false_reject_rate:.1%}. "
            "Remains strictly flag-for-human-read only.",
        )

    def is_auto_approve_authorized(self, profile_id: str) -> bool:
        verdict, _ = self.evaluate_profile_reviewer_authority(profile_id)
        return verdict == ReviewVerdict.AUTO_APPROVE_QUALIFIED


def evaluate_auto_approve_rule(
    sample: List[Dict[str, Any]],
    held_out: List[Dict[str, Any]],
) -> Tuple[bool, str]:
    """Strict preregistered held-out auto-approve rule for semantic reviewers.

    Requirements:
    1. Sample size >= 10.
    2. Zero false approves in sample.
    3. At least one known defective member correctly rejected.
    4. Exact same criteria independently satisfied on a held-out second sample.
    """
    if len(sample) < 10:
        return False, f"Insufficient sample size: {len(sample)} < 10 required."

    defective_sample = [p for p in sample if p.get("is_defective")]
    if not defective_sample:
        return False, "Sample lacks at least one known defective PR."

    false_approves_sample = [p for p in defective_sample if p.get("approved")]
    if false_approves_sample:
        return (
            False,
            f"Primary sample contains {len(false_approves_sample)} false approves (0 allowed).",
        )

    correct_rejections_sample = [p for p in defective_sample if not p.get("approved")]
    if not correct_rejections_sample:
        return False, "No known defective PR was correctly rejected in primary sample."

    # Evaluate held-out sample
    if len(held_out) < 10:
        return False, f"Insufficient held-out sample size: {len(held_out)} < 10 required."

    defective_ho = [p for p in held_out if p.get("is_defective")]
    if not defective_ho:
        return False, "Held-out sample lacks at least one known defective PR."

    false_approves_ho = [p for p in defective_ho if p.get("approved")]
    if false_approves_ho:
        return (
            False,
            f"Held-out sample failed with {len(false_approves_ho)} false approves.",
        )

    return True, "Earned auto-approve criteria satisfied on primary and held-out samples."


class ProfileCalibrationRunner:
    """Orchestrates engineering work calibration runs across profiles and arms."""

    def calibrate_profile(
        self,
        profile: ProfileArm,
        arm: OrchestrationArm,
        tasks: List[Dict[str, Any]],
    ) -> CalibrationReport:
        total = len(tasks)
        if total == 0:
            return CalibrationReport(
                schema=SCHEMA_REPORT,
                profile=profile,
                arm=arm,
                total_tasks=0,
                passed_tasks=0,
                first_attempt_passed_tasks=0,
                post_repair_passed_tasks=0,
                pass_rate=0.0,
                natural_repair_uplift="UNOBSERVED",
                validated_replan_uplift="UNOBSERVED",
                challenger_state="DEFERRED",
                total_elapsed_s=0.0,
                accepted_work_per_hour=0.0,
            )

        first_passed = sum(1 for t in tasks if t.get("first_attempt_pass"))
        post_repaired = sum(
            1 for t in tasks if not t.get("first_attempt_pass") and t.get("post_repair_pass")
        )
        replanned = sum(
            1
            for t in tasks
            if not t.get("first_attempt_pass")
            and not t.get("post_repair_pass")
            and t.get("replan_pass")
        )
        passed = first_passed + post_repaired + replanned
        first_failed = total - first_passed

        # Honest unobserved cell reporting
        if first_failed == 0:
            natural_repair_uplift: Union[str, float] = "UNOBSERVED"
        else:
            natural_repair_uplift = round(post_repaired / total, 4)

        if (first_failed - post_repaired) == 0:
            validated_replan_uplift: Union[str, float] = "UNOBSERVED"
        else:
            validated_replan_uplift = round(replanned / total, 4)

        total_elapsed_s = sum(float(t.get("elapsed_s", 0.0)) for t in tasks)
        if total_elapsed_s > 0 and passed > 0:
            accepted_work_per_hour = round(passed / (total_elapsed_s / 3600.0), 4)
        else:
            accepted_work_per_hour = 0.0

        stability_faults = sum(1 for t in tasks if t.get("stability_fault"))
        model_defects = sum(
            1
            for t in tasks
            if not t.get("first_attempt_pass")
            and not t.get("post_repair_pass")
            and not t.get("replan_pass")
            and not t.get("stability_fault")
        )
        operational_stability = round((total - stability_faults) / max(1, total), 4)

        return CalibrationReport(
            schema=SCHEMA_REPORT,
            profile=profile,
            arm=arm,
            total_tasks=total,
            passed_tasks=passed,
            first_attempt_passed_tasks=first_passed,
            post_repair_passed_tasks=post_repaired,
            pass_rate=round(passed / total, 4),
            natural_repair_uplift=natural_repair_uplift,
            validated_replan_uplift=validated_replan_uplift,
            challenger_state="DEFERRED",
            total_elapsed_s=total_elapsed_s,
            accepted_work_per_hour=accepted_work_per_hour,
            stability_fault_count=stability_faults,
            model_defect_count=model_defects,
            operational_stability_rate=operational_stability,
        )

    def compare_orchestration_arms(
        self,
        profile: ProfileArm,
        corpus: CorpusLayer,
        arm_tasks: Dict[OrchestrationArm, List[Dict[str, Any]]],
    ) -> OrchestrationComparison:
        reports: Dict[OrchestrationArm, CalibrationReport] = {}
        for arm, tasks in arm_tasks.items():
            reports[arm] = self.calibrate_profile(profile, arm, tasks)

        return OrchestrationComparison(
            schema=SCHEMA_COMPARISON,
            profile=profile,
            corpus=corpus,
            arm_reports=reports,
        )
