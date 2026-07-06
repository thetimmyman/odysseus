"""src/routing_scoring.py — Phase 2: weighted scoring + historical performance
lookup for the model routing harness. See routing_engine.py (consumer) and
scripts/odysseus-score (writer of RoutingModelRun.scores)."""
import json
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Score-field weights by task type, ported from the routing harness spec
# Section 13. Falls back to a generic hallucination/convention/test/plan mix
# for any task_type not explicitly listed.
SCORE_WEIGHTS: Dict[str, List[Tuple[str, float]]] = {
    "known_bug_reproduction": [
        ("root_cause_accuracy", 0.40), ("patch_correctness", 0.25), ("minimality", 0.15),
        ("test_awareness", 0.10), ("hallucination_control", 0.10),
    ],
    "bug_debug": [  # RoutingTask.task_type spelling; same weights as known_bug_reproduction
        ("root_cause_accuracy", 0.40), ("patch_correctness", 0.25), ("minimality", 0.15),
        ("test_awareness", 0.10), ("hallucination_control", 0.10),
    ],
    "unknown_bug_debug": [
        ("root_cause_accuracy", 0.40), ("patch_correctness", 0.25), ("minimality", 0.15),
        ("test_awareness", 0.10), ("hallucination_control", 0.10),
    ],
    "feature_implementation": [
        ("patch_correctness", 0.35), ("repo_convention_fit", 0.20), ("minimality", 0.15),
        ("test_awareness", 0.15), ("hallucination_control", 0.15),
    ],
    "implementation": [
        ("patch_correctness", 0.35), ("repo_convention_fit", 0.20), ("minimality", 0.15),
        ("test_awareness", 0.15), ("hallucination_control", 0.15),
    ],
    "feature_plan": [
        ("plan_quality", 0.40), ("repo_convention_fit", 0.20), ("test_awareness", 0.15),
        ("hallucination_control", 0.15), ("minimality", 0.10),
    ],
    "feature_review": [
        ("adversarial_review_quality", 0.45), ("hallucination_control", 0.25),
        ("test_awareness", 0.15), ("repo_convention_fit", 0.15),
    ],
    "feature_plan_review": [
        ("adversarial_review_quality", 0.45), ("hallucination_control", 0.25),
        ("test_awareness", 0.15), ("repo_convention_fit", 0.15),
    ],
    "diff_review": [
        ("adversarial_review_quality", 0.45), ("hallucination_control", 0.25),
        ("test_awareness", 0.15), ("repo_convention_fit", 0.15),
    ],
}
_DEFAULT_WEIGHTS: List[Tuple[str, float]] = [
    ("hallucination_control", 0.30), ("repo_convention_fit", 0.25),
    ("test_awareness", 0.20), ("plan_quality", 0.25),
]

ALL_SCORE_FIELDS = [
    "root_cause_accuracy", "patch_correctness", "minimality", "test_awareness",
    "repo_convention_fit", "hallucination_control", "plan_quality",
    "adversarial_review_quality",
]


def _weighted_average(scores: Dict[str, Optional[float]], weights: List[Tuple[str, float]]) -> Optional[float]:
    """Weighted average over whatever score fields are actually present (not
    None) — renormalizes over the available subset rather than treating a
    missing field as 0, since Phase 2 scoring is often partial (a reviewer
    may not fill in every field). Returns None if nothing is scored yet."""
    present = [(scores.get(field), weight) for field, weight in weights if scores.get(field) is not None]
    if not present:
        return None
    total_weight = sum(w for _, w in present)
    if total_weight <= 0:
        return None
    return sum(v * w for v, w in present) / total_weight


def score_run(scores: Dict[str, Optional[float]], task_type: str) -> Optional[float]:
    """Weighted 0-5 score for one RoutingModelRun.scores dict, given the task
    type it was run against. Returns None if `scores` is empty/unscored."""
    if not scores:
        return None
    weights = SCORE_WEIGHTS.get(task_type, _DEFAULT_WEIGHTS)
    return _weighted_average(scores, weights)


def historical_score(db, model_profile_id: str, task_type: str) -> Optional[float]:
    """Average score_run() across this model's past scored runs for this task
    type. Returns None (not a neutral default) when there's no scored
    history yet — routing_engine.route_task() should skip the historical
    bonus term entirely in that case, matching the source spec's own
    `if (typeof historicalScore === "number")` conditional, rather than
    silently boosting or penalizing an untested model."""
    from core.database import RoutingModelRun, RoutingRun, RoutingTask

    rows = (
        db.query(RoutingModelRun.scores)
        .join(RoutingRun, RoutingModelRun.run_id == RoutingRun.id)
        .join(RoutingTask, RoutingRun.task_id == RoutingTask.id)
        .filter(RoutingModelRun.model_profile_id == model_profile_id)
        .filter(RoutingTask.task_type == task_type)
        .filter(RoutingModelRun.scores.isnot(None))
        .all()
    )
    values = []
    for (scores_json,) in rows:
        try:
            scores = json.loads(scores_json) if scores_json else None
        except Exception:
            continue
        if not scores:
            continue
        s = score_run(scores, task_type)
        if s is not None:
            values.append(s)
    if not values:
        return None
    return sum(values) / len(values)


def record_manual_score(db, model_run_id: str, new_scores: Dict[str, float]) -> dict:
    """Merge `new_scores` (any subset of ALL_SCORE_FIELDS) into a
    RoutingModelRun's existing scores and persist. Returns the updated
    scores dict."""
    from core.database import RoutingModelRun

    row = db.get(RoutingModelRun, model_run_id)
    if not row:
        raise ValueError(f"no RoutingModelRun with id {model_run_id!r}")
    try:
        existing = json.loads(row.scores) if row.scores else {}
    except Exception:
        existing = {}
    unknown = [k for k in new_scores if k not in ALL_SCORE_FIELDS]
    if unknown:
        raise ValueError(f"unknown score field(s): {', '.join(unknown)} (expected one of {ALL_SCORE_FIELDS})")
    existing.update(new_scores)
    row.scores = json.dumps(existing)
    db.commit()
    db.refresh(row)
    return existing
