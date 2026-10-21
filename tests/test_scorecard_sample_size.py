"""Insufficient-sample rule: show explicit N + confidence, never a precise ranking."""
from __future__ import annotations

import json

from src.execution_outcomes import build_outcome_record
from src.outcome_scorecard import aggregate_outcomes
from test_execution_outcomes import make_evidence, make_facts

# Any plausible minimum-sample threshold for a ranking cohort is larger than
# this; the scorecard must keep such aggregates descriptive.
BELOW_THRESHOLD_N = 3


def _build_cohort(tmp_path, count):
    records = []
    for index in range(count):
        evidence, extensions, _, _, _, _ = make_evidence(
            tmp_path / f"repo-{index}", run_id=f"small-sample-{index}")
        records.append(build_outcome_record(evidence_package=evidence,
                                            companion_facts=make_facts(evidence),
                                            artifact_extensions=extensions))
    return records


def test_small_cohort_reports_explicit_n_and_confidence_instead_of_ranking(tmp_path):
    records = _build_cohort(tmp_path, BELOW_THRESHOLD_N)
    view = aggregate_outcomes(records, scoring_version="test-v1")

    # Confidence/uncertainty marker, not a precise ranking value.
    assert view["confidence_status"] == "INSUFFICIENT"
    assert view["ranking_allowed"] is False

    assert len(view["arms"]) == 1
    arm = view["arms"][0]

    # Explicit N fields for the cohort and its aggregates.
    assert arm["distinct_execution_n"] == BELOW_THRESHOLD_N
    assert arm["attempt_n"] == BELOW_THRESHOLD_N
    for name in ("verified_n", "accepted_n", "repository_landed_n",
                 "operator_intervention_n"):
        assert isinstance(arm[name], int)
    for bucket in ("first_pass_verified", "first_pass_accepted"):
        assert arm[bucket]["denominator"] == BELOW_THRESHOLD_N
    for name in ("verified", "semantic_acceptance", "repository_landed", "delivery"):
        assert arm["gate_coverage"][name]["denominator"] == BELOW_THRESHOLD_N

    # Every metric aggregate carries an explicit n and never a precise
    # small-sample quantile: p95 stays an UNSUPPORTED marker.
    for metric in arm["metrics"].values():
        assert "n" in metric and "observed_n" in metric
        assert metric["p95"]["status"] == "UNSUPPORTED"

    # Rate-style ranking aggregates stay uncertainty markers, not numbers.
    assert arm["accepted_per_hour"]["status"] == "UNKNOWN"
    assert arm["accepted_per_dollar"]["status"] == "UNKNOWN"

    # No precise ranking value hides anywhere in the serialized arms.
    assert "rank" not in json.dumps(view["arms"]).lower()
