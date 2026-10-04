"""Descriptive, non-ranking scorecards over immutable outcome snapshots."""
from __future__ import annotations

from typing import Any

from src.execution_outcomes import ExecutionOutcomeRecord, OutcomeError, validate_outcome_record

def aggregate_outcomes(records: list[ExecutionOutcomeRecord], *, scoring_version: str) -> dict[str, Any]:
    """Deterministic descriptive grouped view. It never ranks or authorizes routing."""
    if not isinstance(scoring_version, str) or not scoring_version.strip():
        raise OutcomeError("scoring_version must be explicit and nonempty")
    unique: dict[str, ExecutionOutcomeRecord] = {}
    executions: dict[str, str] = {}
    for item in records:
        valid = validate_outcome_record(item)
        raw_hash = valid.raw_record_hash
        existing = executions.get(valid.execution_key)
        if existing is not None and existing != raw_hash:
            raise OutcomeError("conflicting snapshots of the same execution are refused")
        executions[valid.execution_key] = raw_hash
        unique[raw_hash] = valid
    groups: dict[str, list[ExecutionOutcomeRecord]] = {}
    for item in unique.values():
        groups.setdefault(item.arm_key, []).append(item)
    arms = []
    for arm_key, cohort in sorted(groups.items()):
        first_statuses = [r.to_dict()["first_pass"] for r in cohort]
        first_true = sum(row["verified"] == "TRUE" for row in first_statuses)
        first_false = sum(row["verified"] == "FALSE" for row in first_statuses)
        first_unknown = sum(row["verified"] == "UNKNOWN" for row in first_statuses)
        first_accepted_true = sum(row["accepted"] == "TRUE" for row in first_statuses)
        first_accepted_false = sum(row["accepted"] == "FALSE" for row in first_statuses)
        first_accepted_unknown = sum(row["accepted"] == "UNKNOWN" for row in first_statuses)
        verified = sum(r.to_dict()["gates"]["verified"].get("value") is True for r in cohort)
        accepted = sum(r.to_dict()["gates"]["verified"].get("value") is True
                       and r.to_dict()["gates"]["semantic_acceptance"].get("value") == "ACCEPTED"
                       for r in cohort)
        landed = sum(r.to_dict()["gates"]["repository_landed"].get("value") is True for r in cohort)
        metric_rows = {}
        denominator = sum(len(rec.to_dict()["attempt_outcomes"]) for rec in cohort)
        for metric in ("elapsed_s", "ttft_s", "prompt_tokens", "completion_tokens", "realized_cost"):
            values = []
            currencies = set()
            unknown_n = 0
            absent_n = 0
            for rec in cohort:
                for fact in rec.to_dict()["companion_facts"]["metrics"].get(metric, []):
                    if fact.get("status") == "OBSERVED":
                        values.append(float(fact["value"]))
                        if metric == "realized_cost":
                            currencies.add(fact["currency"])
                    elif fact.get("status") == "UNKNOWN":
                        unknown_n += 1
                    else:
                        absent_n += 1
            values.sort()
            if metric == "realized_cost" and len(currencies) > 1:
                metric_rows[metric] = {"status": "UNKNOWN", "reason": "multiple currencies are not summed",
                                       "n": len(values), "observed_n": len(values),
                                       "unknown_n": unknown_n, "absent_n": absent_n,
                                       "missing_n": unknown_n + absent_n, "denominator": denominator,
                                       "currencies": sorted(currencies)}
            else:
                status = "OBSERVED" if values else "UNKNOWN" if unknown_n else "ABSENT"
                metric_rows[metric] = {"status": status,
                                       "n": len(values), "observed_n": len(values),
                                       "unknown_n": unknown_n, "absent_n": absent_n,
                                       "missing_n": unknown_n + absent_n, "denominator": denominator,
                                       "unit": sorted(currencies)[0] if currencies else _metric_unit(metric),
                                       "median": _median(values) if values else None,
                                       "p95": {"status": "UNSUPPORTED"}}
        arms.append({"arm_key": arm_key, "arm": cohort[0].to_dict()["arm"],
                     "distinct_execution_n": len(cohort), "attempt_n": denominator,
                     "verified_n": verified, "accepted_n": accepted, "repository_landed_n": landed,
                     "delivery": {"status": "ABSENT", "reason": "no source-bound delivery contract"},
                     "first_pass_verified": {"true_n": first_true, "false_n": first_false,
                                             "unknown_n": first_unknown, "denominator": len(cohort)},
                     "first_pass_accepted": {"true_n": first_accepted_true,
                                              "false_n": first_accepted_false,
                                              "unknown_n": first_accepted_unknown,
                                              "denominator": len(cohort)},
                     "verification_outcome_counts": _verification_counts(cohort),
                     "semantic_disposition_counts": _semantic_counts(cohort),
                     "gate_coverage": _gate_coverage(cohort),
                     "accepted_per_hour": {"status": "UNKNOWN", "reason": "no complete matched comparable cohort"},
                     "accepted_per_dollar": {"status": "UNKNOWN", "reason": "no complete matched comparable cohort"},
                     "metrics": metric_rows,
                     "fault_counts": _fault_counts(cohort),
                     "operator_intervention_n": sum(
                         sum(item.get("status") == "OBSERVED" for item in r.to_dict()["companion_facts"]["interventions"])
                         for r in cohort),
                     "operator_intervention_duration_s": _intervention_duration(cohort),
                     "source_record_hashes": sorted(r.raw_record_hash for r in cohort)})
    pools = set()
    for item in unique.values():
        capacity = item.to_dict().get("capacity_receipt")
        if capacity:
            pools.add((capacity.get("provider"), capacity.get("pool_id"), capacity.get("account_identity")))
    return {"schema": "execution-outcome-scorecard-v1", "scoring_version": scoring_version,
            "confidence_status": "INSUFFICIENT", "ranking_allowed": False,
            "capacity_pool_coverage": {"status": "OBSERVED" if pools else "ABSENT",
                                       "distinct_pool_identity_n": len(pools)},
            "capacity_pool_identities": [list(row) for row in sorted(pools)],
            "source_record_hashes": sorted(unique), "arms": arms}


def _metric_unit(metric: str) -> str:
    return {"elapsed_s": "seconds", "ttft_s": "seconds", "prompt_tokens": "tokens",
            "completion_tokens": "tokens", "realized_cost": "currency"}[metric]


def _median(values: list[float]) -> float:
    n = len(values)
    return values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2


def _fault_counts(cohort: list[ExecutionOutcomeRecord]) -> dict[str, int]:
    counts = {name: 0 for name in ("runtime_provider", "harness_tool", "task_quality", "unknown")}
    counts["unattributed_n"] = 0
    for record in cohort:
        for fact in record.to_dict()["companion_facts"]["fault_events"]:
            if fact.get("status") == "OBSERVED":
                counts[fact["value"]] += 1
            elif fact.get("status") == "UNKNOWN":
                counts["unattributed_n"] += 1
    return counts


def _verification_counts(cohort: list[ExecutionOutcomeRecord]) -> dict[str, int]:
    counts = {"PASS": 0, "FAIL": 0, "BLOCKED": 0, "UNKNOWN": 0}
    for record in cohort:
        for outcome in record.to_dict()["attempt_outcomes"]:
            value = outcome["verification"]
            counts[value if value in counts else "UNKNOWN"] += 1
    return counts


def _semantic_counts(cohort: list[ExecutionOutcomeRecord]) -> dict[str, int]:
    counts = {"ACCEPTED": 0, "REWORK": 0, "BLOCKED": 0, "REJECTED": 0,
              "UNKNOWN": 0, "ABSENT": 0}
    for record in cohort:
        gates = record.to_dict()["gates"]["semantic_acceptance"]
        value = gates.get("value") if gates["status"] == "OBSERVED" else gates["status"]
        counts[value if value in counts else "ABSENT"] += 1
    return counts


def _gate_coverage(cohort: list[ExecutionOutcomeRecord]) -> dict[str, Any]:
    result = {}
    for name in ("verified", "semantic_acceptance", "repository_landed", "delivery"):
        counts = {"true_n": 0, "false_n": 0, "observed_n": 0,
                  "unknown_n": 0, "absent_n": 0, "blocked_n": 0}
        for record in cohort:
            gate = record.to_dict()["gates"][name]
            if gate["status"] == "UNKNOWN":
                counts["unknown_n"] += 1
            elif gate["status"] == "ABSENT":
                counts["absent_n"] += 1
            elif gate["status"] == "BLOCKED":
                counts["blocked_n"] += 1
            elif gate.get("value") is True:
                counts["true_n"] += 1
            elif gate.get("value") is False:
                counts["false_n"] += 1
            elif gate["status"] == "OBSERVED":
                counts["observed_n"] += 1
            else:
                counts["unknown_n"] += 1
        result[name] = {**counts, "denominator": len(cohort)}
    return result



def _intervention_duration(cohort: list[ExecutionOutcomeRecord]) -> dict[str, Any]:
    values = sorted(float(item["duration_s"])
                    for record in cohort
                    for item in record.to_dict()["companion_facts"]["interventions"]
                    if item.get("status") == "OBSERVED" and "duration_s" in item)
    return {"status": "OBSERVED" if values else "UNKNOWN", "n": len(values),
            "median": _median(values) if values else None, "unit": "seconds"}
