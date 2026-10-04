"""Weighted run scoring and historical performance lookup for routing."""
import json
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Unlisted task types fall back to a generic weight mix.
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

# Task-performance and lesson-generation fields partition ALL_SCORE_FIELDS
# (unit-tested). Routing fitness may only consume task-performance fields.
TASK_PERF_SCORE_FIELDS = [
    "root_cause_accuracy", "patch_correctness", "minimality", "test_awareness",
    "repo_convention_fit", "hallucination_control",
]
LESSON_GEN_SCORE_FIELDS = ["plan_quality", "adversarial_review_quality"]


def _weighted_average(scores: Dict[str, Optional[float]], weights: List[Tuple[str, float]]) -> Optional[float]:
    """Weighted average renormalized over present fields (missing is not 0); None if unscored."""
    present = [(scores.get(field), weight) for field, weight in weights if scores.get(field) is not None]
    if not present:
        return None
    total_weight = sum(w for _, w in present)
    if total_weight <= 0:
        return None
    return sum(v * w for v, w in present) / total_weight


def score_run(scores: Dict[str, Optional[float]], task_type: str) -> Optional[float]:
    """Display score over all fields; routing fitness must not use it (it mixes in lesson-gen)."""
    if not scores:
        return None
    weights = SCORE_WEIGHTS.get(task_type, _DEFAULT_WEIGHTS)
    return _weighted_average(scores, weights)


def task_perf_score_run(scores: Dict[str, Optional[float]], task_type: str) -> Optional[float]:
    """Task-performance-only score, the only human signal routing fitness may use.

    Lesson-gen fields are excluded so good prose can't lift bad patches."""
    if not scores:
        return None
    weights = [(f, w) for f, w in SCORE_WEIGHTS.get(task_type, _DEFAULT_WEIGHTS)
               if f in TASK_PERF_SCORE_FIELDS]
    return _weighted_average(scores, weights)


def lesson_gen_score_run(scores: Dict[str, Optional[float]]) -> Optional[float]:
    """Plain mean of present lesson-gen fields; never consumed by routing fitness."""
    if not scores:
        return None
    values = [scores[f] for f in LESSON_GEN_SCORE_FIELDS if scores.get(f) is not None]
    if not values:
        return None
    return sum(values) / len(values)


def historical_score(db, model_profile_id: str, task_type: str) -> Optional[float]:
    """Mean task_perf_score_run() over past runs for this task type.

    None without history, so route_task skips the bonus instead of biasing an
    untested model."""
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
        s = task_perf_score_run(scores, task_type)
        if s is not None:
            values.append(s)
    if not values:
        return None
    return sum(values) / len(values)


# Split aggregates are computed on the fly, never materialized.
def _iter_model_runs(db, model_profile_id: Optional[str] = None,
                     task_type: Optional[str] = None):
    """(RoutingModelRun, task_type, model_label) rows, shared so both aggregates use one population."""
    from core.database import RoutingModelProfile, RoutingModelRun, RoutingRun, RoutingTask

    q = (
        db.query(RoutingModelRun, RoutingTask.task_type, RoutingModelProfile.model)
        .join(RoutingRun, RoutingModelRun.run_id == RoutingRun.id)
        .join(RoutingTask, RoutingRun.task_id == RoutingTask.id)
        .outerjoin(RoutingModelProfile,
                   RoutingModelRun.model_profile_id == RoutingModelProfile.id)
    )
    if model_profile_id:
        q = q.filter(RoutingModelRun.model_profile_id == model_profile_id)
    if task_type:
        q = q.filter(RoutingTask.task_type == task_type)
    for model_run, tt, model in q.all():
        yield model_run, tt, model or (model_run.model_profile_id or "unknown")


def _parse_scores(model_run) -> Optional[dict]:
    if not model_run.scores:
        return None
    try:
        scores = json.loads(model_run.scores)
    except Exception:
        return None
    return scores if isinstance(scores, dict) else None


def model_task_perf_by_task(db, model_profile_id: Optional[str] = None,
                            task_type: Optional[str] = None) -> List[dict]:
    """Task-performance aggregate per (model_profile, task_type).

    The only aggregate routing fitness may consume; no lesson-gen field enters it."""
    from collections import defaultdict

    buckets: Dict[tuple, dict] = defaultdict(lambda: {
        "attempts": 0, "completed": 0, "errored": 0, "rate_limited": 0,
        "total_cost_usd": 0.0, "latencies": [], "task_perf_scores": [],
        "verified_runs": 0, "verification_passed": 0, "patch_accepted": 0,
        "model": None,
    })
    for model_run, tt, model in _iter_model_runs(db, model_profile_id, task_type):
        b = buckets[(model_run.model_profile_id, tt)]
        b["model"] = model
        b["attempts"] += 1
        b["completed"] += int(bool(model_run.completed))
        b["errored"] += int(bool(model_run.errored))
        b["rate_limited"] += int(bool(model_run.rate_limited))
        b["total_cost_usd"] += model_run.cost_usd or 0.0
        if model_run.latency_ms is not None:
            b["latencies"].append(model_run.latency_ms)
        scores = _parse_scores(model_run)
        if scores:
            s = task_perf_score_run(scores, tt)
            if s is not None:
                b["task_perf_scores"].append(s)
            verification = scores.get("verification")
            if isinstance(verification, dict):
                b["verified_runs"] += 1
                b["verification_passed"] += int(bool(verification.get("passed")))
                b["patch_accepted"] += int(bool(verification.get("patch_accepted")))

    out = []
    for (profile_id, tt), b in buckets.items():
        out.append({
            "model_profile_id": profile_id,
            "model": b["model"],
            "task_type": tt,
            "attempts": b["attempts"],
            "completed": b["completed"],
            "completion_rate": round(b["completed"] / b["attempts"], 3) if b["attempts"] else 0,
            "errored": b["errored"],
            "rate_limited": b["rate_limited"],
            "total_cost_usd": round(b["total_cost_usd"], 4),
            "avg_latency_ms": round(sum(b["latencies"]) / len(b["latencies"])) if b["latencies"] else None,
            "scored_runs": len(b["task_perf_scores"]),
            "avg_task_perf_score": (round(sum(b["task_perf_scores"]) / len(b["task_perf_scores"]), 2)
                                    if b["task_perf_scores"] else None),
            "verified_runs": b["verified_runs"],
            "verification_passed": b["verification_passed"],
            "patch_accepted": b["patch_accepted"],
            "verification_pass_rate": (round(b["verification_passed"] / b["verified_runs"], 3)
                                       if b["verified_runs"] else None),
        })
    out.sort(key=lambda r: (r["model"] or "", r["task_type"]))
    return out


def model_lesson_gen_by_task(db, model_profile_id: Optional[str] = None,
                             task_type: Optional[str] = None) -> List[dict]:
    """Lesson-gen aggregate per (model_profile, task_type); never used by routing fitness."""
    from collections import defaultdict

    buckets: Dict[tuple, dict] = defaultdict(lambda: {
        "attempts": 0, "lesson_scores": [], "model": None,
        "fields": {f: [] for f in LESSON_GEN_SCORE_FIELDS},
    })
    for model_run, tt, model in _iter_model_runs(db, model_profile_id, task_type):
        b = buckets[(model_run.model_profile_id, tt)]
        b["model"] = model
        b["attempts"] += 1
        scores = _parse_scores(model_run)
        if not scores:
            continue
        s = lesson_gen_score_run(scores)
        if s is not None:
            b["lesson_scores"].append(s)
        for f in LESSON_GEN_SCORE_FIELDS:
            if scores.get(f) is not None:
                b["fields"][f].append(scores[f])

    out = []
    for (profile_id, tt), b in buckets.items():
        row = {
            "model_profile_id": profile_id,
            "model": b["model"],
            "task_type": tt,
            "attempts": b["attempts"],
            "lesson_scored_runs": len(b["lesson_scores"]),
            "avg_lesson_gen_score": (round(sum(b["lesson_scores"]) / len(b["lesson_scores"]), 2)
                                     if b["lesson_scores"] else None),
        }
        for f in LESSON_GEN_SCORE_FIELDS:
            vals = b["fields"][f]
            row[f"avg_{f}"] = round(sum(vals) / len(vals), 2) if vals else None
        out.append(row)
    out.sort(key=lambda r: (r["model"] or "", r["task_type"]))
    return out


def record_manual_score(db, model_run_id: str, new_scores: Dict[str, float]) -> dict:
    """Merge `new_scores` into the run's scores, persist, and return them."""
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
