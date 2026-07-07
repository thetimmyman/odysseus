
"""
routing_reliability.py — WorkflowReliabilityMonitor (Section 13).

Workflow reliability is a REVIEW-READINESS signal only. It MUST NOT reduce
per-engineer or per-task budgets in core Odysseus (Section 13 policy). The
signal may increase review depth / coaching surfaces but never touches budget.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import List, Optional

logger = logging.getLogger("odysseus.routing.reliability")


class ReviewAction(str, Enum):  # noqa: F811
    NONE = "none"
    INCREASE_REVIEW_DEPTH = "increase_review_depth"
    REQUIRE_COACHING_REVIEW = "require_coaching_review"
    REQUIRE_SENIOR_REVIEWER = "require_senior_reviewer"
    ADMIN_REVIEW = "admin_review"


@dataclass
class Confounders:
    flaky_tests_observed: bool = False
    high_risk_task_mix: bool = False
    model_failure_spike: bool = False
    legacy_hotspot_touched: bool = False


@dataclass
class ReliabilityInput:
    subject_type: str  # engineer | team | repo | task_type
    subject_id: str
    period_start: str
    period_end: str
    normalized_verification_failure_rate: float  # 0..1
    lesson_review_participation_rate: float = 1.0
    avg_validated_lesson_quality: float = 0.0
    confounders: Confounders = field(default_factory=Confounders)


@dataclass
class ReviewReadinessSignal:
    subject_type: str
    subject_id: str
    period_start: str
    period_end: str
    normalized_verification_failure_rate: float
    lesson_review_participation_rate: float
    avg_validated_lesson_quality: float
    confounders: dict
    recommended_action: str

    def to_dict(self) -> dict:
        return {
            "subjectType": self.subject_type,
            "subjectId": self.subject_id,
            "periodStart": self.period_start,
            "periodEnd": self.period_end,
            "normalizedVerificationFailureRate": self.normalized_verification_failure_rate,
            "lessonReviewParticipationRate": self.lesson_review_participation_rate,
            "avgValidatedLessonQuality": self.avg_validated_lesson_quality,
            "confounders": self.confounders,
            "recommendedAction": self.recommended_action,
        }


def compute_signal(inp: ReliabilityInput) -> ReviewReadinessSignal:
    """
    Produce a review-readiness signal. This is purely advisory; callers must
    NOT use the output to reduce budgets in core Odysseus.
    """
    rate = max(0.0, min(1.0, inp.normalized_verification_failure_rate))
    action = ReviewAction.NONE.value

    # Confounders soften the recommendation.
    has_confounder = any(vars(inp.confounders).values())

    if rate >= 0.5:
        action = (
            ReviewAction.ADMIN_REVIEW.value
            if (rate >= 0.75 and not has_confounder)
            else ReviewAction.REQUIRE_SENIOR_REVIEWER.value
        )
    elif rate >= 0.3:
        action = ReviewAction.REQUIRE_COACHING_REVIEW.value
    elif rate >= 0.15:
        action = ReviewAction.INCREASE_REVIEW_DEPTH.value

    return ReviewReadinessSignal(
        subject_type=inp.subject_type,
        subject_id=inp.subject_id,
        period_start=inp.period_start,
        period_end=inp.period_end,
        normalized_verification_failure_rate=rate,
        lesson_review_participation_rate=inp.lesson_review_participation_rate,
        avg_validated_lesson_quality=inp.avg_validated_lesson_quality,
        confounders=vars(inp.confounders),
        recommended_action=action,
    )


def budget_affecting_policy_allowed() -> bool:
    """
    Section 13 policy: core Odysseus MUST NOT reduce per-engineer/per-task
    budgets from reliability signals. An organization that wants a budget-
    affecting policy must implement it as an EXTERNAL layer, not default Odysseus.
    This helper exists so callers can assert the constraint explicitly.
    """
    return False
