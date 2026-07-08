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

# --- Phase 5 (WP6) split: task-performance vs lesson-generation fields ---
# The spec requires task-performance stats and lesson-generation stats to be
# SEPARATE aggregates, and routing fitness may ONLY consume task-performance.
#
# TASK_PERF_SCORE_FIELDS rate how well the model executed the code task
# itself (diagnosis, patch, scope discipline, conventions, groundedness).
# LESSON_GEN_SCORE_FIELDS rate the quality of the explanatory/analytical
# prose artifacts the model produced (plans, adversarial reviews) — the
# closest existing proxy for "would this model write a good lesson/summary
# for the knowledge base" until WP7 adds dedicated lesson-quality scoring.
# The two sets are DISJOINT and together partition ALL_SCORE_FIELDS
# (unit-tested), so no human score can leak into both aggregates.
TASK_PERF_SCORE_FIELDS = [
    "root_cause_accuracy", "patch_correctness", "minimality", "test_awareness",
    "repo_convention_fit", "hallucination_control",
]
LESSON_GEN_SCORE_FIELDS = ["plan_quality", "adversarial_review_quality"]


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
    type it was run against, over ALL score fields (task-perf AND lesson-gen)
    — the human-facing display score used by `odysseus-score show` and the
    legacy `odysseus-stats by-model/by-task-type` views. Routing fitness must
    NOT use this (it mixes lesson-gen fields in); it uses
    task_perf_score_run() via historical_score(). Returns None if `scores` is
    empty/unscored."""
    if not scores:
        return None
    weights = SCORE_WEIGHTS.get(task_type, _DEFAULT_WEIGHTS)
    return _weighted_average(scores, weights)


def task_perf_score_run(scores: Dict[str, Optional[float]], task_type: str) -> Optional[float]:
    """Task-PERFORMANCE-only weighted 0-5 score for one run: the task-type
    weights restricted to TASK_PERF_SCORE_FIELDS, renormalized over whatever
    subset is present (same partial-scoring semantics as score_run). This is
    the ONLY human-score signal routing fitness may consume (spec Phase 5:
    "routing fitness may only consume task-perf") — lesson-gen fields
    (plan_quality, adversarial_review_quality) are excluded here even for
    task types whose Section 13 weights mention them, so a model that writes
    beautiful plans/reviews but lands bad patches can't ride that prose
    skill into code-task routing. Returns None when no task-perf field is
    scored yet."""
    if not scores:
        return None
    weights = [(f, w) for f, w in SCORE_WEIGHTS.get(task_type, _DEFAULT_WEIGHTS)
               if f in TASK_PERF_SCORE_FIELDS]
    return _weighted_average(scores, weights)


def lesson_gen_score_run(scores: Dict[str, Optional[float]]) -> Optional[float]:
    """LESSON-GENERATION-only 0-5 score for one run: the plain mean of the
    LESSON_GEN_SCORE_FIELDS that are present. Deliberately task-type-agnostic
    (explanatory-artifact quality is the same skill regardless of task type)
    and deliberately NOT consumed by routing fitness — it exists for WP7's
    lesson-generator selection / KB ranking. Returns None when no lesson-gen
    field is scored yet."""
    if not scores:
        return None
    values = [scores[f] for f in LESSON_GEN_SCORE_FIELDS if scores.get(f) is not None]
    if not values:
        return None
    return sum(values) / len(values)


def historical_score(db, model_profile_id: str, task_type: str) -> Optional[float]:
    """Average task_perf_score_run() across this model's past scored runs for
    this task type — the routing-fitness aggregate routing_engine.route_task()
    consumes. Per spec Phase 5 this consumes ONLY task-performance fields
    (TASK_PERF_SCORE_FIELDS): lesson-gen scores (plan_quality,
    adversarial_review_quality) never move routing fitness, for any task type
    (unit-tested in tests/test_routing_stats_split.py). Returns None (not a
    neutral default) when there's no scored history yet —
    routing_engine.route_task() should skip the historical bonus term
    entirely in that case, matching the source spec's own
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
        s = task_perf_score_run(scores, task_type)
        if s is not None:
            values.append(s)
    if not values:
        return None
    return sum(values) / len(values)


# --- Phase 5 split aggregates (computed queries, never materialized) ---
def _iter_model_runs(db, model_profile_id: Optional[str] = None,
                     task_type: Optional[str] = None):
    """(RoutingModelRun, task_type, model_label) tuples for every attempt,
    joined through runs/tasks and outer-joined to the profile for a display
    label. Shared by both split-aggregate builders so they can never drift
    onto different row populations."""
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
    """TASK-PERFORMANCE aggregate per (model_profile, task_type): completion/
    error/rate-limit outcomes, verification outcomes persisted by WP5
    (scores["verification"].passed / .patch_accepted), and the human
    task-quality scores restricted to TASK_PERF_SCORE_FIELDS (via
    task_perf_score_run). This is the ONLY aggregate family routing fitness
    (historical_score -> route_task) is allowed to consume; lesson-gen fields
    never enter any number returned here. Computed on the fly from
    RoutingModelRun rows — never materialized (spec: aggregates must stay
    derivable)."""
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
    """LESSON-GENERATION aggregate per (model_profile, task_type): ONLY the
    LESSON_GEN_SCORE_FIELDS (plan_quality, adversarial_review_quality) — no
    completion/verification outcomes, no task-perf fields. Exposed for WP7's
    lesson-generator selection and KB ranking; routing fitness NEVER consumes
    this aggregate (spec Phase 5), which is unit-tested by asserting
    route_task ordering is invariant under lesson-gen-only score changes.
    Computed on the fly — never materialized."""
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
