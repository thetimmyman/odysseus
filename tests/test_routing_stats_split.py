"""Phase 5 (WP6) split stats: task-performance vs lesson-generation aggregates
are SEPARATE, and routing fitness (historical_score -> route_task) consumes
ONLY the task-performance family — lesson-gen-only score changes must never
move route_task ordering, for ANY task type (including plan/review types whose
Section 13 display weights mention the lesson-gen fields)."""
import json
import sys
import types
import uuid
from pathlib import Path

import sqlalchemy
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.database as cdb
from src.routing_engine import route_task
from src.routing_scoring import (
    ALL_SCORE_FIELDS,
    LESSON_GEN_SCORE_FIELDS,
    TASK_PERF_SCORE_FIELDS,
    historical_score,
    lesson_gen_score_run,
    model_lesson_gen_by_task,
    model_task_perf_by_task,
    task_perf_score_run,
)

_BUNDLE = {"metadata": {"token_estimate": 10}, "files": []}


def _db():
    engine = sqlalchemy.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    cdb.Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False)()


def _seed_profile(db, pid, roles=("scout",)):
    db.add(cdb.RoutingModelProfile(
        id=pid, model=f"test/{pid}", roles=json.dumps(list(roles)),
        context_window=32768, is_free=True, enabled=True,
    ))
    db.commit()


def _seed_run(db, profile_id, task_type, scores=None, *, completed=True,
              errored=False, rate_limited=False, cost_usd=0.0, latency_ms=None):
    """One RoutingTask + RoutingRun + RoutingModelRun chain (sequential commits:
    no relationship()s, so FK parents must land first)."""
    tid, rid, mid = (str(uuid.uuid4()) for _ in range(3))
    db.add(cdb.RoutingTask(id=tid, title="t", objective="o",
                           task_type=task_type, repo_path="."))
    db.commit()
    db.add(cdb.RoutingRun(id=rid, task_id=tid, status="succeeded"))
    db.commit()
    db.add(cdb.RoutingModelRun(
        id=mid, run_id=rid, model_profile_id=profile_id,
        completed=completed, errored=errored, rate_limited=rate_limited,
        cost_usd=cost_usd, latency_ms=latency_ms,
        scores=json.dumps(scores) if scores is not None else None,
    ))
    db.commit()
    return mid


def _route_stub(task_type):
    return types.SimpleNamespace(
        id="t-stub", task_type=task_type, risk="low",
        allow_free_models=True, allow_paid_models=False,
        allow_premium_models=False, data_sensitivity="internal",
    )


# --- the field split itself ---
def test_split_partitions_all_score_fields():
    """Task-perf and lesson-gen are DISJOINT and together cover every human
    score field — no field can leak into both aggregates."""
    tp, lg = set(TASK_PERF_SCORE_FIELDS), set(LESSON_GEN_SCORE_FIELDS)
    assert tp & lg == set()
    assert tp | lg == set(ALL_SCORE_FIELDS)


def test_task_perf_score_ignores_lesson_fields():
    # feature_plan's Section 13 display weights lean on plan_quality (0.40);
    # the task-perf score must exclude it and renormalize over the rest.
    scores = {"plan_quality": 5.0, "repo_convention_fit": 2.0,
              "test_awareness": 2.0, "hallucination_control": 2.0, "minimality": 2.0}
    assert task_perf_score_run(scores, "feature_plan") == 2.0
    # Lesson-gen-only scores yield NO task-perf score at all (not 0).
    assert task_perf_score_run({"plan_quality": 5.0}, "feature_plan") is None


def test_lesson_gen_score_ignores_task_perf_fields():
    scores = {"plan_quality": 4.0, "adversarial_review_quality": 2.0,
              "patch_correctness": 0.0, "hallucination_control": 0.0}
    assert lesson_gen_score_run(scores) == 3.0
    assert lesson_gen_score_run({"patch_correctness": 5.0}) is None


def test_historical_score_is_task_perf_only():
    """The routing-fitness aggregate must exclude lesson-gen fields even for
    plan-type tasks whose display weights include them."""
    db = _db()
    _seed_profile(db, "p1")
    _seed_run(db, "p1", "feature_plan", {
        "plan_quality": 5.0, "repo_convention_fit": 2.0, "test_awareness": 2.0,
        "hallucination_control": 2.0, "minimality": 2.0,
    })
    assert historical_score(db, "p1", "feature_plan") == 2.0
    # Lesson-gen-only history is NOT routing history.
    _seed_profile(db, "p2")
    _seed_run(db, "p2", "feature_plan", {"plan_quality": 5.0})
    assert historical_score(db, "p2", "feature_plan") is None


# --- aggregate builders ---
def test_task_perf_aggregate_outcomes_and_verification():
    db = _db()
    _seed_profile(db, "p1")
    _seed_run(db, "p1", "bug_debug", {
        "root_cause_accuracy": 4.0, "patch_correctness": 4.0, "minimality": 4.0,
        "test_awareness": 4.0, "hallucination_control": 4.0,
        "plan_quality": 0.0,  # lesson-gen field must not drag the average
        "verification": {"mode": "bug_fix", "passed": True, "patch_accepted": True},
    }, cost_usd=0.5, latency_ms=100)
    _seed_run(db, "p1", "bug_debug", {
        "verification": {"mode": "bug_fix", "passed": False, "patch_accepted": False},
    }, cost_usd=0.25, latency_ms=300)
    _seed_run(db, "p1", "bug_debug", None, completed=False, errored=True)

    (row,) = model_task_perf_by_task(db, task_type="bug_debug")
    assert row["model_profile_id"] == "p1"
    assert row["task_type"] == "bug_debug"
    assert row["attempts"] == 3
    assert row["completed"] == 2
    assert row["errored"] == 1
    assert row["total_cost_usd"] == 0.75
    assert row["avg_latency_ms"] == 200
    assert row["scored_runs"] == 1
    assert row["avg_task_perf_score"] == 4.0  # plan_quality=0 excluded
    assert row["verified_runs"] == 2
    assert row["verification_passed"] == 1
    assert row["patch_accepted"] == 1
    assert row["verification_pass_rate"] == 0.5


def test_lesson_gen_aggregate_only_lesson_fields():
    db = _db()
    _seed_profile(db, "p1")
    _seed_run(db, "p1", "feature_review", {
        "adversarial_review_quality": 4.0, "plan_quality": 2.0,
        "hallucination_control": 0.0,  # task-perf field: must not appear here
    })
    _seed_run(db, "p1", "feature_review", {"patch_correctness": 5.0})  # no lesson fields

    (row,) = model_lesson_gen_by_task(db, task_type="feature_review")
    assert row["attempts"] == 2
    assert row["lesson_scored_runs"] == 1
    assert row["avg_lesson_gen_score"] == 3.0
    assert row["avg_plan_quality"] == 2.0
    assert row["avg_adversarial_review_quality"] == 4.0


def test_aggregate_filters():
    db = _db()
    _seed_profile(db, "p1")
    _seed_profile(db, "p2")
    _seed_run(db, "p1", "bug_debug", None)
    _seed_run(db, "p2", "implementation", None)
    assert {r["model_profile_id"] for r in model_task_perf_by_task(db)} == {"p1", "p2"}
    assert [r["model_profile_id"] for r in model_task_perf_by_task(db, model_profile_id="p1")] == ["p1"]
    assert [r["task_type"] for r in model_lesson_gen_by_task(db, task_type="implementation")] == ["implementation"]


# --- routing fitness never consumes lesson-gen ---
def _ordering(db, task_type):
    result = route_task(db, _route_stub(task_type), _BUNDLE)
    return [(c["profile_id"], c["score"]) for c in result["candidates"]]


def test_route_ordering_invariant_under_lesson_gen_changes():
    """Changing ONLY lesson-gen scores leaves route_task candidate scores and
    ordering byte-identical — proven on feature_plan, the task type whose
    display weights lean hardest on a lesson-gen field."""
    db = _db()
    _seed_profile(db, "p-prose")
    _seed_profile(db, "p-coder")
    # p-prose: gorgeous plans/reviews, zero task-perf history.
    prose_run = _seed_run(db, "p-prose", "feature_plan",
                          {"plan_quality": 5.0, "adversarial_review_quality": 5.0})
    # p-coder: modest but real task-perf history.
    _seed_run(db, "p-coder", "feature_plan", {
        "repo_convention_fit": 3.0, "test_awareness": 3.0,
        "hallucination_control": 3.0, "minimality": 3.0,
    })

    before = _ordering(db, "feature_plan")
    assert before[0][0] == "p-coder"  # task-perf history wins; prose alone earns nothing

    # Flip p-prose's lesson-gen scores hard, both directions.
    row = db.get(cdb.RoutingModelRun, prose_run)
    row.scores = json.dumps({"plan_quality": 0.0, "adversarial_review_quality": 0.0})
    db.commit()
    assert _ordering(db, "feature_plan") == before

    row.scores = json.dumps({"plan_quality": 5.0, "adversarial_review_quality": 5.0})
    db.commit()
    assert _ordering(db, "feature_plan") == before

    # And on a patch-shaped type too (bug_debug weights never had lesson fields).
    _seed_run(db, "p-prose", "bug_debug", {"adversarial_review_quality": 5.0})
    bd_before = _ordering(db, "bug_debug")
    _seed_run(db, "p-prose", "bug_debug", {"plan_quality": 5.0})
    assert _ordering(db, "bug_debug") == bd_before


def test_route_ordering_does_respond_to_task_perf_changes():
    """Sanity check for the invariant above: task-perf scores DO move the
    ranking, so the invariance test can't pass vacuously."""
    db = _db()
    _seed_profile(db, "p-a")
    _seed_profile(db, "p-b")
    _seed_run(db, "p-a", "feature_plan", {"repo_convention_fit": 1.0})
    _seed_run(db, "p-b", "feature_plan", {"repo_convention_fit": 2.0})
    before = _ordering(db, "feature_plan")
    assert before[0][0] == "p-b"
    _seed_run(db, "p-a", "feature_plan", {"repo_convention_fit": 5.0, "test_awareness": 5.0})
    after = _ordering(db, "feature_plan")
    assert after != before
    assert after[0][0] == "p-a"
