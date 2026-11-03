"""Synthetic scorecard controls: representability only, never empirical claims."""
from __future__ import annotations

import pytest

from src.execution_outcomes import OutcomeError, build_outcome_record
from src.outcome_scorecard import aggregate_outcomes
from test_execution_outcomes import (
    _build_offer_record,
    execution_key,
    make_evidence,
    make_facts,
)


def test_duplicate_records_are_idempotent_and_scorecard_never_ranks(tmp_path):
    evidence, extensions, _, _, _, _ = make_evidence(tmp_path / "repo", prompt_tokens=0)
    facts = make_facts(evidence, prompt_zero=True)
    record = build_outcome_record(evidence_package=evidence, companion_facts=facts,
                                  artifact_extensions=extensions)
    view = aggregate_outcomes([record, record], scoring_version="test-v1")
    assert view["confidence_status"] == "INSUFFICIENT"
    assert view["ranking_allowed"] is False
    assert len(view["arms"]) == 1
    arm = view["arms"][0]
    assert arm["distinct_execution_n"] == 1
    assert arm["metrics"]["prompt_tokens"]["median"] == 0
    assert arm["metrics"]["realized_cost"]["status"] == "ABSENT"
    assert arm["metrics"]["ttft_s"]["status"] == "UNKNOWN"
    assert arm["metrics"]["ttft_s"]["unknown_n"] == 1
    assert arm["gate_coverage"]["semantic_acceptance"]["absent_n"] == 1
    assert all(row["p95"]["status"] == "UNSUPPORTED" for row in arm["metrics"].values())
    assert arm["accepted_per_dollar"]["status"] == "UNKNOWN"
    assert arm["delivery"]["status"] == "ABSENT"


def test_conflicting_snapshots_of_one_execution_refuse(tmp_path):
    evidence, extensions, _, _, _, _ = make_evidence(tmp_path / "repo")
    original = build_outcome_record(evidence_package=evidence,
                                    companion_facts=make_facts(evidence),
                                    artifact_extensions=extensions)
    changed = make_facts(evidence)
    changed["task_class"]["reason"] = "another explicit unknown explanation"
    conflicting = build_outcome_record(evidence_package=evidence,
                                       companion_facts=changed,
                                       artifact_extensions=extensions)
    assert original.execution_key == conflicting.execution_key
    assert original.raw_record_hash != conflicting.raw_record_hash
    with pytest.raises(OutcomeError, match="conflicting snapshots"):
        aggregate_outcomes([original, conflicting], scoring_version="test-v1")


def test_distinct_dispatch_models_and_targets_remain_separate_synthetic_arms(tmp_path):
    records = []
    index = 0
    for target in ("target-a", "target-b"):
        for model in ("model-a", "model-b", "model-c"):
            index += 1
            evidence, extensions, _, _, _, _ = make_evidence(
                tmp_path / f"repo-{index}", run_id=f"synthetic-run-{index}",
                selected_target=target, selected_model=model)
            facts = make_facts(evidence)
            records.append(build_outcome_record(evidence_package=evidence,
                                                companion_facts=facts,
                                                artifact_extensions=extensions))
    view = aggregate_outcomes(records, scoring_version="representability-only")
    assert len(view["arms"]) == 6
    assert all(row["distinct_execution_n"] == 1 for row in view["arms"])
    assert view["confidence_status"] == "INSUFFICIENT"
    assert view["ranking_allowed"] is False


def test_controlled_same_provider_model_different_harness_comparison_does_not_conflate_arms(tmp_path):
    # Same provider, pool, model, and tariff; only coding-agent harness differs.
    # OD-19: Must represent harness differences without conflating harness and model effects,
    # and recognize that multiple harnesses on one account remain one capacity pool.
    records = []
    for harness in ("pi", "opencode", "cline"):
        record, _, _, _, _, _, _ = _build_offer_record(
            tmp_path / f"repo-{harness}", run_id=f"run-harness-{harness}",
            tariff_version="tariff-v1", harness=harness,
        )
        records.append(record)

    view = aggregate_outcomes(records, scoring_version="controlled-harness-v1")
    assert len(view["arms"]) == 3
    assert all(arm["distinct_execution_n"] == 1 for arm in view["arms"])

    # Harness identities are distinct across the three arms:
    harnesses = {arm["arm"]["offer_harness"]["identity"] for arm in view["arms"]}
    assert harnesses == {"pi", "opencode", "cline"}

    # Model and provider remain identical:
    assert {arm["arm"]["model"] for arm in view["arms"]} == {"synthetic-model"}
    assert {arm["arm"]["provider"]["value"] for arm in view["arms"]} == {"synthetic-provider"}

    # One shared account across three harnesses is exactly one capacity pool:
    assert view["capacity_pool_coverage"]["distinct_pool_identity_n"] == 1
    assert view["capacity_pool_coverage"]["status"] == "OBSERVED"


def test_multi_task_class_and_multi_stack_comparison_preserves_distinct_arms_and_taxonomy(tmp_path):
    # OD-19: At least two task classes and three materially different execution stacks
    # can be compared from replayed evidence without cross-pollution.
    records = []
    index = 0
    task_classes = ("bounded_bugfix", "documentation_refactor")
    stacks = (
        ("local-qwen", "qwen3.8-27b"),
        ("hosted-flash", "deepseek-v4.1-flash"),
        ("hosted-pro", "deepseek-v4-pro"),
    )
    for tc in task_classes:
        for target, model in stacks:
            index += 1
            evidence, extensions, _, _, _, _ = make_evidence(
                tmp_path / f"repo-{index}", run_id=f"matrix-run-{index}",
                selected_target=target, selected_model=model,
            )
            facts = make_facts(evidence)
            key = execution_key(evidence)
            facts["task_class"] = {
                "status": "UNKNOWN",
                "reason": f"unadmitted task class: {tc}",
                "execution_key": key,
            }
            records.append(build_outcome_record(
                evidence_package=evidence,
                companion_facts=facts,
                artifact_extensions=extensions,
            ))

    view = aggregate_outcomes(records, scoring_version="multi-stack-matrix-v1")
    # 2 task classes x 3 stacks = 6 distinct arms:
    assert len(view["arms"]) == 6
    assert all(arm["distinct_execution_n"] == 1 for arm in view["arms"])
    assert view["confidence_status"] == "INSUFFICIENT"
    assert view["ranking_allowed"] is False

    # Verify failure taxonomy, gate coverage, and provenance are preserved per arm:
    for arm in view["arms"]:
        assert "gate_coverage" in arm
        assert "fault_counts" in arm
        assert "source_record_hashes" in arm
        assert len(arm["source_record_hashes"]) == 1


def test_historical_records_remain_replayable_under_different_scoring_versions(tmp_path):
    # OD-19: Historical records remain replayable after scoring logic changes.
    evidence, extensions, _, _, _, _ = make_evidence(tmp_path / "repo-replay")
    record = build_outcome_record(
        evidence_package=evidence,
        companion_facts=make_facts(evidence),
        artifact_extensions=extensions,
    )
    view_v1 = aggregate_outcomes([record], scoring_version="scoring-v1")
    view_v2 = aggregate_outcomes([record], scoring_version="scoring-v2")

    # Underlying immutable record hash and arm key remain identical:
    assert view_v1["arms"][0]["arm_key"] == view_v2["arms"][0]["arm_key"]
    assert view_v1["arms"][0]["source_record_hashes"] == view_v2["arms"][0]["source_record_hashes"]
    # Scoring version metadata updates truthfully:
    assert view_v1["scoring_version"] == "scoring-v1"
    assert view_v2["scoring_version"] == "scoring-v2"
