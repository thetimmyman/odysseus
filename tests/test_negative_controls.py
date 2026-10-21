"""Negative-control assertion tests for outcome classification (OD-19 epic).

One test per enumerated negative control. Each asserts the classification
outcome through the existing scorecard-groundwork fixtures; these are
synthetic controls and make no empirical ranking claims. Installed harness
drift remains blocked: no canonical installed-harness receipt is accepted.
"""
from __future__ import annotations

import pytest

from src.execution_outcomes import OutcomeError, build_outcome_record
from src.outcome_scorecard import aggregate_outcomes
from test_execution_outcomes import (
    _build_offer_record, execution_key, fact, make_acceptance, make_evidence, make_facts,
)


def _observed_fault(facts, evidence, *, value):
    attempt_hash = evidence["attempt_receipts"][0]["receipt_hash"]
    facts["fault_events"] = [{
        "status": "OBSERVED", "value": value, "attempt": 1,
        "source_ref": f"attempt:{attempt_hash}", "source_sha256": attempt_hash,
        "execution_key": execution_key(evidence)}]
    return facts


def _observed_faults(record):
    return [fact for fact in record.to_dict()["companion_facts"]["fault_events"]
            if fact["status"] == "OBSERVED"]


def test_provider_timeout_is_not_a_model_quality_failure(tmp_path):
    # A provider timeout is canonically attributed runtime_provider; the
    # scorecard must refuse to re-bucket it as task quality.
    evidence, extensions, _, _, _, _ = make_evidence(
        tmp_path / "repo", failure_class="runtime_provider")
    fabricated = _observed_fault(make_facts(evidence), evidence, value="task_quality")
    with pytest.raises(OutcomeError, match="contradicts its canonical attempt"):
        build_outcome_record(evidence_package=evidence, companion_facts=fabricated,
                             artifact_extensions=extensions)
    record = build_outcome_record(evidence_package=evidence,
                                  companion_facts=make_facts(evidence),
                                  artifact_extensions=extensions)
    assert [fact["value"] for fact in _observed_faults(record)] == ["runtime_provider"]


def test_harness_tool_parser_failure_is_not_a_provider_failure(tmp_path):
    # A harness-side tool-parser failure (canonical class "technical") must not
    # be attributed to the provider/model; without a canonical provider fault it
    # is retained as unknown attribution.
    evidence, extensions, _, _, _, _ = make_evidence(tmp_path / "repo", failure_class="technical")
    fabricated = _observed_fault(make_facts(evidence), evidence, value="runtime_provider")
    with pytest.raises(OutcomeError, match="contradicts its canonical attempt"):
        build_outcome_record(evidence_package=evidence, companion_facts=fabricated,
                             artifact_extensions=extensions)
    record = build_outcome_record(evidence_package=evidence,
                                  companion_facts=make_facts(evidence),
                                  artifact_extensions=extensions)
    assert [fact["value"] for fact in _observed_faults(record)] == ["unknown"]
    view = aggregate_outcomes([record], scoring_version="negative-control-harness-fault")
    counts = view["arms"][0]["fault_counts"]
    assert counts["unknown"] == 1
    assert counts["runtime_provider"] == 0


def test_deterministic_pass_with_semantic_reject_is_not_success(tmp_path):
    evidence, extensions, source, _, _, _ = make_evidence(tmp_path / "repo")
    acceptance = make_acceptance(evidence, source, disposition="REJECTED")
    record = build_outcome_record(evidence_package=evidence,
                                  companion_facts=make_facts(evidence),
                                  semantic_acceptance=acceptance.to_dict(),
                                  candidate_source_snapshot=source.to_dict(),
                                  artifact_extensions=extensions)
    result = record.to_dict()
    assert result["gates"]["verified"]["value"] is True
    assert result["gates"]["semantic_acceptance"]["value"] == "REJECTED"
    assert result["first_pass"] == {"verified": "TRUE", "accepted": "FALSE"}
    arm = aggregate_outcomes([record], scoring_version="negative-control-reject")["arms"][0]
    assert arm["verified_n"] == 1
    assert arm["accepted_n"] == 0
    assert arm["first_pass_accepted"]["false_n"] == 1
    assert arm["semantic_disposition_counts"]["REJECTED"] == 1


def test_tracker_done_without_landing_receipt_is_not_delivered(tmp_path):
    # Tracker status is not an authorized evidence field. Reject the actual
    # untrusted Done claim, then retain ABSENT delivery without a receipt.
    evidence, extensions, source, _, _, _ = make_evidence(tmp_path / "repo")
    acceptance = make_acceptance(evidence, source)
    untrusted = make_facts(evidence)
    untrusted["tracker_status"] = "Done"
    with pytest.raises(OutcomeError, match="companion_facts contains unknown fields"):
        build_outcome_record(evidence_package=evidence, companion_facts=untrusted,
                             semantic_acceptance=acceptance.to_dict(),
                             candidate_source_snapshot=source.to_dict(),
                             artifact_extensions=extensions)
    record = build_outcome_record(evidence_package=evidence,
                                  companion_facts=make_facts(evidence),
                                  semantic_acceptance=acceptance.to_dict(),
                                  candidate_source_snapshot=source.to_dict(),
                                  artifact_extensions=extensions)
    result = record.to_dict()
    assert result["landing_receipt"] is None
    assert result["gates"]["repository_landed"]["status"] == "ABSENT"
    assert result["gates"]["delivery"]["status"] == "ABSENT"
    arm = aggregate_outcomes([record], scoring_version="negative-control-delivery")["arms"][0]
    assert arm["delivery"]["status"] == "ABSENT"
    assert arm["gate_coverage"]["delivery"]["absent_n"] == 1
    assert arm["gate_coverage"]["repository_landed"]["absent_n"] == 1


def test_missing_cost_source_is_absent_not_zero(tmp_path):
    evidence, extensions, _, _, _, _ = make_evidence(tmp_path / "repo")
    facts = make_facts(evidence)
    del facts["metrics"]["realized_cost"]
    record = build_outcome_record(evidence_package=evidence,
                                  companion_facts=facts,
                                  artifact_extensions=extensions)
    cost = aggregate_outcomes([record], scoring_version="negative-control-cost")[
        "arms"][0]["metrics"]["realized_cost"]
    assert cost["status"] == "ABSENT"
    assert cost["median"] is None
    assert cost["observed_n"] == 0
    assert cost["absent_n"] == 1


@pytest.mark.parametrize("version,config_digest", [("1", "a" * 64), ("2", "b" * 64)])
def test_installed_harness_observation_requires_a_canonical_source(tmp_path, version, config_digest):
    # There is no authorized installed-harness source yet. Do not label offer
    # identity separation as installed-harness drift coverage.
    record, capacity, offer, evidence, facts, key, extensions = _build_offer_record(
        tmp_path / "repo", run_id="drift-run", tariff_version="tariff-v1")
    assert record.to_dict()["arm"]["harness"]["status"] == "UNKNOWN"
    facts["harness"] = fact(
        {"identity": "command-code", "version": version, "config_sha256": config_digest,
         "usage_path": "subscription-cli"}, f"offer:{offer.receipt_hash}", key)
    with pytest.raises(OutcomeError, match="harness has no canonical source field"):
        build_outcome_record(evidence_package=evidence, companion_facts=facts,
                             capacity_receipt=capacity.to_dict(), offer_receipt=offer.to_dict(),
                             artifact_extensions=extensions)


def test_operator_override_is_retained_without_observed_status(tmp_path):
    evidence, extensions, _, _, _, _ = make_evidence(tmp_path / "repo")
    facts = make_facts(evidence)
    facts["interventions"] = [{
        "status": "UNKNOWN", "reason": "operator override without a canonical receipt",
        "attempt": 1, "execution_key": execution_key(evidence)}]
    record = build_outcome_record(evidence_package=evidence, companion_facts=facts,
                                  artifact_extensions=extensions)
    retained = record.to_dict()["companion_facts"]["interventions"]
    assert len(retained) == 1
    assert retained[0]["status"] == "UNKNOWN"
    fabricated = make_facts(evidence)
    attempt_hash = evidence["attempt_receipts"][0]["receipt_hash"]
    fabricated["interventions"] = [{
        "status": "OBSERVED", "value": "operator override", "attempt": 1,
        "source_ref": f"attempt:{attempt_hash}", "source_sha256": attempt_hash,
        "execution_key": execution_key(evidence)}]
    with pytest.raises(OutcomeError, match="no canonical intervention receipt"):
        build_outcome_record(evidence_package=evidence, companion_facts=fabricated,
                             artifact_extensions=extensions)
    arm = aggregate_outcomes([record], scoring_version="negative-control-intervention")[
        "arms"][0]
    assert arm["operator_intervention_n"] == 0
    assert arm["operator_intervention_duration_s"]["status"] == "UNKNOWN"


def test_shared_account_remains_one_capacity_pool(tmp_path):
    first, capacity, *_ = _build_offer_record(tmp_path / "repo-a", run_id="pool-run-a",
                                              tariff_version="tariff-v1")
    second, second_capacity, *_ = _build_offer_record(
        tmp_path / "repo-b", run_id="pool-run-b", tariff_version="tariff-v1",
        observed_at="2026-10-04T00:01:00+00:00")
    assert first.to_dict()["capacity_receipt"]["receipt_hash"] == capacity.receipt_hash
    assert second.to_dict()["capacity_receipt"]["receipt_hash"] == second_capacity.receipt_hash
    assert capacity.receipt_hash != second_capacity.receipt_hash
    view = aggregate_outcomes([first, second], scoring_version="negative-control-pool")
    coverage = view["capacity_pool_coverage"]
    assert coverage["status"] == "OBSERVED"
    assert coverage["distinct_pool_identity_n"] == 1
    assert coverage["distinct_pool_identity_n"] == len(view["capacity_pool_identities"])
