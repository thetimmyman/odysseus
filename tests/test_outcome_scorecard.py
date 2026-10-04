"""Synthetic scorecard controls: representability only, never empirical claims."""
from __future__ import annotations

import pytest

from src.execution_outcomes import OutcomeError, build_outcome_record
from src.outcome_scorecard import aggregate_outcomes
from test_execution_outcomes import make_evidence, make_facts


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
