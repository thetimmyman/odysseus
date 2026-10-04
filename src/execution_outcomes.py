"""Pure, immutable projections of canonical execution evidence.

This module reads only caller-supplied mappings and bytes. It has no persistence,
filesystem discovery, provider, routing, or delivery behavior.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
from dataclasses import MISSING, dataclass
from typing import Any, Mapping

from src.attempt_receipt import (
    AttemptReceipt,
    VerificationReceipt,
    attempt_receipt_hash_is_valid,
    recompute_outcome,
    verification_receipt_hash_is_valid,
)
from src.evidence_contract import STATE_BLOCKED, STATE_FAILED
from src.evidence_package import (
    EvidencePackage,
    evidence_package_hash_is_valid,
    validate_evidence_package,
)
from src.execution_package import (
    DispatchDecisionReceipt,
    ExecutionPackage,
    package_hash_is_valid,
)
from src.mechanical_landing import (
    LandingReceipt,
    LandingStrategy,
    SemanticAcceptance,
    landing_receipt_hash_is_valid,
    prove_landed_equivalence,
)
from src.provider_capacity import capacity_receipt_from_dict
from src.provider_model_offer import provider_model_offer_from_dict
from src.source_snapshot import (
    snapshot_digest_is_valid,
    source_snapshot_from_dict,
)

SCHEMA = "execution-outcome-record-v1"
SCORING_VERSION = "descriptive-first-slice-v1"
_HASH = set("0123456789abcdef")


class OutcomeError(ValueError):
    """Input cannot be represented as a source-bound outcome record."""


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OutcomeError("outcome input is not strict finite JSON") from exc


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in _HASH for c in value)


def _copy_json(value: Any) -> Any:
    return json.loads(_canonical(value))


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OutcomeError("duplicate JSON key in outcome record")
        result[key] = value
    return result


def _verify_dispatch(row: Mapping[str, Any]) -> str:
    if not isinstance(row, Mapping):
        raise OutcomeError("dispatch receipt must be a mapping")
    declared = DispatchDecisionReceipt.__dataclass_fields__
    allowed = set(declared)
    required = {name for name, item in declared.items()
                if item.default is MISSING and item.default_factory is MISSING}
    if set(row) - allowed or not required.issubset(row) or not _is_hash(row.get("receipt_hash")):
        raise OutcomeError("dispatch receipt has missing or unknown fields")
    values = dict(row)
    supplied = values.pop("receipt_hash")
    for key in ("requested_capabilities", "candidates_considered", "capability_receipt_refs",
                "capacity_receipt_refs", "offer_receipt_refs", "offer_quote_digests",
                "granted_tools", "granted_write_scope", "granted_read_scope"):
        if key in values:
            values[key] = tuple(values[key])
    if "authority" in values and values["authority"] is not None:
        values["authority"] = dict(values["authority"])
    try:
        receipt = DispatchDecisionReceipt(**values)
        digest = _hash(_canonical(receipt.core()))
    except (TypeError, ValueError, KeyError) as exc:
        raise OutcomeError("dispatch receipt cannot be reconstructed") from exc
    if supplied != digest:
        raise OutcomeError("dispatch receipt hash mismatch")
    return digest


def _validate_artifact_inputs(evidence: Mapping[str, Any], extensions: Mapping[str, bytes]) -> dict[str, bytes]:
    if not isinstance(extensions, Mapping):
        raise OutcomeError("artifact_extensions must be a mapping of locator to bytes")
    detached: dict[str, bytes] = {}
    for key, data in extensions.items():
        if not isinstance(key, str) or not key or not isinstance(data, bytes):
            raise OutcomeError("artifact extensions require string locators and byte values")
        detached[key] = bytes(data)
    refs = []
    for receipt in list(evidence.get("attempt_receipts") or ()) + list(evidence.get("verification_receipts") or ()):
        if not isinstance(receipt, Mapping):
            continue
        for key in ("rendered_context_ref", "output_ref", "stdout_ref", "stderr_ref"):
            ref = receipt.get(key)
            if isinstance(ref, Mapping):
                refs.append(ref)
        refs.extend(ref for ref in (receipt.get("artifact_refs") or ()) if isinstance(ref, Mapping))
    for ref in refs:
        uri = ref.get("storage_uri")
        if not isinstance(uri, str) or not uri:
            raise OutcomeError("every canonical artifact requires an explicit caller-byte locator")
        if uri not in detached:
            raise OutcomeError("caller must provide bytes for every canonical artifact; no file fallback")
        if not isinstance(ref.get("sha256"), str) or not _is_hash(ref["sha256"]):
            raise OutcomeError("canonical artifact hash is malformed")
    return detached


def _source_ref_index(package: Mapping[str, Any], evidence: Mapping[str, Any],
                      capacity: Mapping[str, Any] | None, offer: Mapping[str, Any] | None,
                      acceptance: Mapping[str, Any] | None, landing: Mapping[str, Any] | None) -> set[str]:
    result = {f"package:{package.get('package_hash', '')}",
              f"evidence:{evidence.get('evidence_package_hash', '')}"}
    for kind, rows in (("dispatch", evidence.get("dispatch_receipts") or ()),
                       ("attempt", evidence.get("attempt_receipts") or ()),
                       ("verification", evidence.get("verification_receipts") or ())):
        result.update(f"{kind}:{row.get('receipt_hash', '')}" for row in rows if isinstance(row, Mapping))
    if capacity:
        result.add(f"capacity:{capacity.get('receipt_hash', '')}")
    if offer:
        result.add(f"offer:{offer.get('receipt_hash', '')}")
    if acceptance:
        result.add(f"acceptance:{acceptance.get('acceptance_hash', '')}")
    if landing:
        result.add(f"landing:{landing.get('receipt_hash', '')}")
    return result


def _check_binding_fact(value: Any, name: str, execution_key: str, known_sources: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"status": "UNKNOWN", "reason": f"{name} was not supplied",
                "execution_key": execution_key}
    fact = _copy_json(dict(value))
    allowed_keys = {"status", "value", "reason", "source_ref", "source_sha256",
                    "execution_key", "attempt", "unit", "currency", "duration_s"}
    if set(fact) - allowed_keys:
        raise OutcomeError(f"{name} contains unknown fact fields")
    status = fact.get("status")
    if not isinstance(status, str) or status not in {"OBSERVED", "UNKNOWN", "ABSENT"}:
        raise OutcomeError(f"{name} status must be OBSERVED, UNKNOWN, or ABSENT")
    if fact.get("execution_key") != execution_key:
        raise OutcomeError(f"{name} is not bound to this execution")
    if status == "OBSERVED":
        ref, digest = fact.get("source_ref"), fact.get("source_sha256")
        if not isinstance(ref, str) or ref not in known_sources or not _is_hash(digest):
            raise OutcomeError(f"{name} observed fact lacks a canonical source binding")
        if ref.rsplit(":", 1)[-1] != digest:
            raise OutcomeError(f"{name} source reference and digest disagree")
        if "value" not in fact or fact["value"] is None:
            raise OutcomeError(f"{name} observed fact requires a value")
    else:
        if not isinstance(fact.get("reason"), str) or not fact["reason"].strip():
            raise OutcomeError(f"{name} {status} fact requires a reason")
        if "source_ref" in fact or "source_sha256" in fact:
            ref, digest = fact.get("source_ref"), fact.get("source_sha256")
            if not isinstance(ref, str) or ref not in known_sources or not _is_hash(digest):
                raise OutcomeError(f"{name} optional source binding is malformed")
            if ref.rsplit(":", 1)[-1] != digest:
                raise OutcomeError(f"{name} source reference and digest disagree")
    if "duration_s" in fact and (isinstance(fact["duration_s"], bool)
                                 or not isinstance(fact["duration_s"], (int, float))
                                 or not math.isfinite(fact["duration_s"]) or fact["duration_s"] < 0):
        raise OutcomeError(f"{name} duration_s must be finite and non-negative")
    return fact


def _validate_facts(raw: Any, execution_key: str, known_sources: set[str], *,
                    attempts_by_hash: Mapping[str, Mapping[str, Any]],
                    observed_fields: set[str]) -> dict[str, Any]:
    facts = {} if raw is None else _copy_json(dict(raw)) if isinstance(raw, Mapping) else None
    if facts is None:
        raise OutcomeError("companion_facts must be a mapping")
    allowed = {"task_class", "risk_class", "harness", "provider", "pool", "account",
               "metrics", "fault_events", "interventions"}
    if set(facts) - allowed:
        raise OutcomeError("companion_facts contains unknown fields")
    result = {}
    for name in ("task_class", "risk_class", "harness", "provider", "pool", "account"):
        result[name] = _check_binding_fact(facts.get(name), name, execution_key, known_sources)
        if result[name]["status"] == "OBSERVED" and name not in observed_fields:
            raise OutcomeError(f"{name} has no canonical source field for an observed value")
    for name in ("task_class", "risk_class", "provider", "pool", "account"):
        fact = result[name]
        if fact["status"] == "OBSERVED" and (not isinstance(fact.get("value"), str)
                                               or not fact["value"].strip()):
            raise OutcomeError(f"{name} must be an explicit nonempty string")
    harness = result["harness"]
    if harness["status"] == "OBSERVED":
        value = harness.get("value")
        if (not isinstance(value, Mapping) or not isinstance(value.get("identity"), str)
                or not value["identity"].strip() or not isinstance(value.get("version"), str)
                or not value["version"].strip() or not _is_hash(value.get("config_sha256"))
                or not isinstance(value.get("usage_path"), str) or not value["usage_path"].strip()):
            raise OutcomeError("observed harness requires identity, version, config digest, and usage path")
    metrics = facts.get("metrics", {})
    if not isinstance(metrics, Mapping):
        raise OutcomeError("metrics must be a mapping")
    checked_metrics = {}
    for name, entries in metrics.items():
        if name not in {"elapsed_s", "ttft_s", "prompt_tokens", "completion_tokens", "realized_cost"}:
            raise OutcomeError(f"unsupported metric {name!r}")
        if not isinstance(entries, list):
            raise OutcomeError(f"metric {name} must contain a list of per-attempt facts")
        checked = []
        for entry in entries:
            fact = _check_binding_fact(entry, f"metric.{name}", execution_key, known_sources)
            attempt = fact.get("attempt")
            source_field = {"elapsed_s": "elapsed_s", "prompt_tokens": "prompt_tokens",
                            "completion_tokens": "completion_tokens"}.get(name)
            source_attempt = next((row for row in attempts_by_hash.values()
                                   if row.get("attempt") == attempt), None)
            canonical_value = (source_attempt.get(source_field)
                               if source_attempt is not None and source_field else None)
            if (fact["status"] != "OBSERVED" and isinstance(canonical_value, (int, float))
                    and not isinstance(canonical_value, bool) and canonical_value > 0):
                raise OutcomeError(f"metric {name} cannot hide a populated canonical receipt value")
            if fact["status"] == "OBSERVED":
                if name == "realized_cost":
                    raise OutcomeError("no canonical actual-charge source supports realized_cost")
                if not fact.get("source_ref", "").startswith("attempt:"):
                    raise OutcomeError(f"metric {name} must cite its canonical attempt receipt")
                attempt_hash = fact["source_ref"].split(":", 1)[1]
                source = attempts_by_hash.get(attempt_hash)
                if source is None or source.get("attempt") != fact.get("attempt"):
                    raise OutcomeError(f"metric {name} does not cite its exact attempt")
                if source_field is None or source_field not in source:
                    raise OutcomeError(f"metric {name} is absent from the cited canonical receipt")
                expected_unit = "seconds" if name == "elapsed_s" else "tokens"
                if fact.get("unit") != expected_unit:
                    raise OutcomeError(f"metric {name} unit must be {expected_unit}")
                if fact["value"] != source[source_field]:
                    raise OutcomeError(f"metric {name} contradicts its canonical receipt")
            if fact["status"] == "OBSERVED":
                value = fact["value"]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise OutcomeError(f"metric {name} must be finite and non-negative")
                if name in {"prompt_tokens", "completion_tokens"} and not isinstance(value, int):
                    raise OutcomeError(f"metric {name} must be an integer")
                if name == "realized_cost" and (not isinstance(fact.get("currency"), str)
                                                 or not fact["currency"].strip()):
                    raise OutcomeError("realized_cost requires an explicit currency")
                if name != "realized_cost" and (not isinstance(fact.get("unit"), str)
                                                 or not fact["unit"].strip()):
                    raise OutcomeError(f"metric {name} requires an explicit unit")
            if type(attempt) is not int or attempt < 1:
                raise OutcomeError(f"metric {name} requires a positive attempt number")
            if any(old.get("attempt") == attempt for old in checked):
                raise OutcomeError(f"metric {name} has duplicate attempt observations")
            checked.append(fact)
        checked_metrics[name] = checked
    result["metrics"] = checked_metrics
    for list_name, allowed in (("fault_events", {"runtime_provider", "harness_tool", "task_quality", "unknown"}),
                               ("interventions", None)):
        entries = facts.get(list_name, [])
        if not isinstance(entries, list):
            raise OutcomeError(f"{list_name} must be a list")
        normalized = []
        for entry in entries:
            fact = _check_binding_fact(entry, list_name, execution_key, known_sources)
            if list_name == "fault_events" and (type(fact.get("attempt")) is not int or fact["attempt"] < 1):
                raise OutcomeError("fault event requires a positive attempt number")
            if list_name == "fault_events" and fact["status"] == "OBSERVED":
                ref = fact.get("source_ref", "")
                attempt_hash = ref.split(":", 1)[1] if ref.startswith("attempt:") else ""
                source = attempts_by_hash.get(attempt_hash)
                if source is None or source.get("attempt") != fact["attempt"]:
                    raise OutcomeError("fault classification must cite its exact attempt receipt")
                canonical_class = source.get("failure_class")
                if not canonical_class:
                    raise OutcomeError("canonical attempt does not establish a fault classification")
                expected_class = "runtime_provider" if canonical_class == "runtime_provider" else "unknown"
                if fact.get("value") != expected_class:
                    raise OutcomeError("fault classification contradicts its canonical attempt")
            if list_name == "interventions" and fact["status"] == "OBSERVED":
                raise OutcomeError("no canonical intervention receipt supports an observed intervention")
            if fact["status"] == "OBSERVED" and allowed is not None and fact["value"] not in allowed:
                raise OutcomeError("fault classification is outside the known taxonomy")
            normalized.append(fact)
        result[list_name] = normalized
    return result


def _arm_fact(fact: Mapping[str, Any]) -> dict[str, Any]:
    """Drop per-execution provenance while retaining observed/unknown arm identity."""
    keys = ("status", "value", "reason")
    return {key: fact[key] for key in keys if key in fact}


def _gate_record(status: str, value: Any = None, reason: str = "") -> dict[str, Any]:
    item = {"status": status}
    if value is not None:
        item["value"] = value
    if reason:
        item["reason"] = reason
    return item


def _first_pass_status(validation_ok: bool, attempt_outcomes: list[Mapping[str, Any]],
                       semantic_disposition: str | None) -> dict[str, str]:
    first = next((item for item in attempt_outcomes if item.get("attempt") == 1), None)
    if first is None:
        verified = "UNKNOWN"
    elif first.get("verification") == "FAIL":
        verified = "FALSE"
    elif validation_ok and first.get("verification") == "PASS":
        verified = "TRUE"
    else:
        verified = "UNKNOWN"
    if verified == "FALSE":
        accepted = "FALSE"
    elif verified == "TRUE" and len(attempt_outcomes) == 1:
        if semantic_disposition == "ACCEPTED":
            accepted = "TRUE"
        elif semantic_disposition in {"REWORK", "BLOCKED", "REJECTED"}:
            accepted = "FALSE"
        else:
            accepted = "UNKNOWN"
    else:
        accepted = "UNKNOWN"
    return {"verified": verified, "accepted": accepted}


def _evaluate(evidence: Mapping[str, Any], facts: Mapping[str, Any],
              semantic: Mapping[str, Any] | None, landing: Mapping[str, Any] | None,
              capacity_payload: Mapping[str, Any] | None, offer_payload: Mapping[str, Any] | None,
              artifact_extensions: Mapping[str, bytes],
              candidate_source_snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    evidence = _copy_json(dict(evidence)) if isinstance(evidence, Mapping) else evidence
    if semantic is not None and isinstance(semantic, Mapping):
        semantic = _copy_json(dict(semantic))
    if landing is not None and isinstance(landing, Mapping):
        landing = _copy_json(dict(landing))
    if candidate_source_snapshot is not None:
        if not isinstance(candidate_source_snapshot, Mapping):
            raise OutcomeError("candidate_source_snapshot must be a canonical mapping")
        candidate_source_snapshot = _copy_json(dict(candidate_source_snapshot))
        if not snapshot_digest_is_valid(candidate_source_snapshot):
            raise OutcomeError("candidate source snapshot digest is invalid")
        try:
            candidate_source_snapshot = source_snapshot_from_dict(candidate_source_snapshot).to_dict()
        except (TypeError, ValueError) as exc:
            raise OutcomeError("candidate source snapshot cannot be reconstructed") from exc
    evidence_fields = set(EvidencePackage.__dataclass_fields__) | {"evidence_package_hash"}
    if (not isinstance(evidence, Mapping) or set(evidence) != evidence_fields
            or not evidence_package_hash_is_valid(evidence)):
        raise OutcomeError("canonical evidence package hash or schema mismatch")
    package = evidence.get("execution_package")
    package_fields = set(ExecutionPackage.__dataclass_fields__) | {"interface_digest"}
    if (not isinstance(package, Mapping) or set(package) != package_fields
            or not package_hash_is_valid(package)):
        raise OutcomeError("canonical execution package hash or schema mismatch")
    plan = package.get("verification")
    if (not isinstance(plan, Mapping) or not str(plan.get("verifier_id") or "").strip()
            or not str(plan.get("command") or "").strip()
            or type(plan.get("timeout_s")) is not int or plan["timeout_s"] <= 0
            or not plan.get("verifier_paths") or not plan.get("verifier_digests")):
        raise OutcomeError("a deterministic verification plan with sealed verifier paths is required")
    plan_digests = {pair[0]: pair[1] for pair in plan.get("verifier_digests", [])
                    if isinstance(pair, list) and len(pair) == 2}
    if (set(plan_digests) != set(plan["verifier_paths"])
            or any(not _is_hash(value) for value in plan_digests.values())):
        raise OutcomeError("verification plan digests do not cover every verifier path")
    requirements = package.get("evidence_requirements")
    if (not isinstance(requirements, list) or not requirements
            or not any(isinstance(req, Mapping) and req.get("mandatory", True)
                       and req.get("kind") == "deterministic_verification" for req in requirements)):
        raise OutcomeError("non-vacuous mandatory verification requirements are required")
    package_hash = package["package_hash"]
    run_id, packet_id = package.get("run_id"), package.get("packet_id")
    if not isinstance(run_id, str) or not run_id or not isinstance(packet_id, str) or not packet_id:
        raise OutcomeError("canonical package requires exact run_id and packet_id")
    dispatches = evidence.get("dispatch_receipts")
    attempts = evidence.get("attempt_receipts")
    verifications = evidence.get("verification_receipts")
    if not isinstance(dispatches, list) or not dispatches or not isinstance(attempts, list) or not attempts:
        raise OutcomeError("nonempty canonical dispatch and attempt receipts are required")
    dispatch_hashes = [_verify_dispatch(row) for row in dispatches]
    if len(set(dispatch_hashes)) != len(dispatch_hashes):
        raise OutcomeError("duplicate dispatch receipt identity is refused")
    if not isinstance(verifications, list) or not verifications:
        raise OutcomeError("nonempty canonical verification receipts are required")
    if any(row["run_id"] != run_id or row["packet_id"] != packet_id
           or row["execution_package_hash"] != package_hash for row in dispatches):
        raise OutcomeError("dispatch receipts do not bind exact package/run/packet")
    if len({(row.get("selected_target_id"), row.get("selected_host"), row.get("selected_model"),
             row.get("selected_runtime_kind"), row.get("selected_runtime_version"),
             row.get("selected_model_digest"), row.get("selected_backend"))
            for row in dispatches}) != 1:
        raise OutcomeError("mixed materially distinct dispatch arms are refused")
    for row in attempts:
        if (not isinstance(row, Mapping) or set(row) != set(AttemptReceipt.__dataclass_fields__)
                or not attempt_receipt_hash_is_valid(row)):
            raise OutcomeError("attempt receipt hash or schema mismatch")
        for name in ("attempt", "repair_of", "prompt_tokens", "completion_tokens",
                     "requested_context", "served_context"):
            if name in row and (type(row[name]) is not int or row[name] < 0):
                raise OutcomeError(f"attempt field {name} must be a non-negative integer")
        for name in ("elapsed_s",):
            if name in row and (isinstance(row[name], bool) or not isinstance(row[name], (int, float))
                                or not math.isfinite(row[name]) or row[name] < 0):
                raise OutcomeError(f"attempt field {name} must be finite and non-negative")
    numbers = [row.get("attempt") for row in attempts]
    if any(type(n) is not int for n in numbers) or sorted(numbers) != list(range(1, len(numbers) + 1)):
        raise OutcomeError("attempt chain must be unique and contiguous from one")
    dispatch_by_hash = {row["receipt_hash"]: row for row in dispatches}
    for row in attempts:
        cited = dispatch_by_hash.get(row.get("dispatch_receipt_hash"))
        if (row.get("run_id") != run_id or row.get("packet_id") != packet_id
                or row.get("execution_package_hash") != package_hash or cited is None
                or (row.get("target_id"), row.get("host"), row.get("model")) !=
                (cited.get("selected_target_id"), cited.get("selected_host"), cited.get("selected_model"))):
            raise OutcomeError("attempt receipts do not bind exact dispatch/package/run/packet")
        for field, selected in (("runtime_kind", cited.get("selected_runtime_kind")),
                                ("runtime_version", cited.get("selected_runtime_version"))):
            if row.get(field) and selected and row[field] != selected:
                raise OutcomeError(f"attempt {field} conflicts with selected dispatch runtime")
    for row in verifications:
        if (not isinstance(row, Mapping)
                or set(row) != set(VerificationReceipt.__dataclass_fields__) | {"outcome"}
                or not verification_receipt_hash_is_valid(row)):
            raise OutcomeError("verification receipt hash or schema mismatch")
        if type(row.get("exit_code")) is not int:
            raise OutcomeError("verification exit_code must be a plain integer")
        if row.get("outcome") != recompute_outcome(row):
            raise OutcomeError("verification receipt outcome was not recomputed")
        if row.get("verifier_id") != plan.get("verifier_id") or row.get("normalized_command") != plan.get("command"):
            raise OutcomeError("verification receipt does not match the sealed verification plan")
        if (tuple(row.get("verifier_paths") or ()) != tuple(plan.get("verifier_paths") or ())
                or any(plan_digests.get(path) != row.get("verifier_digest") for path in row["verifier_paths"])):
            raise OutcomeError("verification digest does not match planned verifier bytes")
        for name in ("elapsed_s",):
            if isinstance(row.get(name), bool) or not isinstance(row.get(name), (int, float)) \
                    or not math.isfinite(row[name]) or row[name] < 0:
                raise OutcomeError(f"verification field {name} must be finite and non-negative")
        for name in ("tests_collected", "tests_executed", "tests_passed", "tests_failed", "tests_skipped"):
            if row.get(name) is not None and (type(row[name]) is not int or row[name] < 0):
                raise OutcomeError(f"verification field {name} must be a non-negative integer")
    if any(row.get("run_id") != run_id or row.get("packet_id") != packet_id
           or row.get("execution_package_hash") != package_hash
           or type(row.get("attempt")) is not int or row["attempt"] not in numbers
           for row in verifications):
        raise OutcomeError("verification receipts do not bind exact attempt/package/run/packet")
    ext = _validate_artifact_inputs(evidence, artifact_extensions)
    # Every file-backed artifact must have supplied bytes. The extension map is
    # passed for all URIs, so the canonical validator never needs its file loader.
    validation = validate_evidence_package(evidence, artifact_extensions=ext)
    verification_by_attempt: dict[int, list[str]] = {}
    for row in verifications:
        verification_by_attempt.setdefault(row["attempt"], []).append(recompute_outcome(row))
    attempt_outcomes = []
    for row in sorted(attempts, key=lambda item: item["attempt"]):
        checks = verification_by_attempt.get(row["attempt"], [])
        attempt_outcomes.append({"attempt": row["attempt"],
                                 "verification": "FAIL" if any(v == "FAIL" for v in checks)
                                 else "BLOCKED" if any(v == "BLOCKED" for v in checks)
                                 else "PASS" if checks and all(v == "PASS" for v in checks)
                                 else "UNKNOWN",
                                 "repair_of": row.get("repair_of", 0),
                                 "attempt_receipt_hash": row["receipt_hash"],
                                 "verification_outcomes": checks})
    missing_verification_attempts = sorted(set(numbers) - set(verification_by_attempt))
    issue_codes = {i.code for i in validation.issues}
    if missing_verification_attempts:
        issue_codes.add("verification_missing_for_attempt")
    raw_outcomes = {recompute_outcome(row) for row in verifications}
    requirement_states = {state.state for state in validation.requirement_states}
    if "FAIL" in raw_outcomes or STATE_FAILED in requirement_states:
        verified = _gate_record("OBSERVED", False,
                                ",".join(sorted(issue_codes)) or "verification failed")
    elif "BLOCKED" in raw_outcomes or STATE_BLOCKED in requirement_states:
        verified = _gate_record("BLOCKED",
                                reason=",".join(sorted(issue_codes)) or "verification blocked")
    elif "INCONCLUSIVE" in raw_outcomes:
        verified = _gate_record("UNKNOWN",
                                reason=",".join(sorted(issue_codes)) or "verification capture is incomplete")
    elif not validation.ok or missing_verification_attempts:
        reason = ",".join(sorted(issue_codes))
        verified = _gate_record("UNKNOWN", reason=reason or "verification evidence is incomplete")
    else:
        verified = _gate_record("OBSERVED", True)
    source_digests = {row.get("source_snapshot_digest") for row in verifications
                      if recompute_outcome(row) == "PASS"}
    source_digests.discard("")
    all_verification_sources = {row.get("source_snapshot_digest") for row in verifications}
    all_verification_sources.discard("")
    if len(source_digests) > 1:
        raise OutcomeError("passing verification receipts disagree on candidate source")
    verified_source = next(iter(source_digests), None)
    if not verified_source and len(all_verification_sources) > 1:
        raise OutcomeError("nonpassing verification receipts disagree on source")
    source_for_acceptance = verified_source or next(iter(all_verification_sources), None)
    semantic_result = _gate_record("ABSENT", reason="no semantic acceptance receipt was supplied")
    semantic_hash = None
    semantic_accepted = False
    if semantic is not None:
        if not isinstance(semantic, Mapping):
            raise OutcomeError("semantic acceptance must be a serialized mapping")
        values = dict(semantic)
        supplied = values.pop("acceptance_hash", None)
        known = set(SemanticAcceptance.__dataclass_fields__) - {"acceptance_hash"}
        if set(values) != known:
            raise OutcomeError("semantic acceptance has missing or unknown fields")
        try:
            acceptance = SemanticAcceptance(**values, acceptance_hash=supplied or "")
        except (TypeError, ValueError) as exc:
            raise OutcomeError("semantic acceptance cannot be reconstructed") from exc
        if not _is_hash(supplied) or supplied != _hash(_canonical(acceptance.core())):
            raise OutcomeError("semantic acceptance hash mismatch")
        if acceptance.evidence_package_hash != evidence.get("evidence_package_hash"):
            raise OutcomeError("semantic acceptance names different evidence")
        if not source_for_acceptance or acceptance.candidate_source_digest != source_for_acceptance:
            raise OutcomeError("semantic acceptance is not bound to the canonical verification source")
        semantic_hash = supplied
        if candidate_source_snapshot is None:
            semantic_result = _gate_record(
                "UNKNOWN", reason="verified candidate head and diff snapshot were not supplied")
        else:
            snapshot = candidate_source_snapshot
            if (snapshot.get("snapshot_digest") != acceptance.candidate_source_digest
                    or snapshot.get("snapshot_digest") != source_for_acceptance
                    or snapshot.get("head_sha") != acceptance.candidate_head_sha
                    or snapshot.get("tracked_diff_digest") != acceptance.candidate_diff_digest
                    or snapshot.get("diff_truncated") or snapshot.get("truncated_paths")):
                raise OutcomeError("semantic acceptance does not match the verified candidate source snapshot")
            semantic_accepted = verified.get("value") is True and acceptance.disposition == "ACCEPTED"
            semantic_result = _gate_record(
                "OBSERVED", acceptance.disposition,
                "accepted" if semantic_accepted else "not accepted or canonical evidence is not verified")
    landing_result = _gate_record("ABSENT", reason="repository LandingReceipt was not supplied")
    landed = False
    if landing is not None:
        if semantic is None or not semantic_accepted:
            raise OutcomeError("landing receipt cannot bind without verified accepted semantic evidence")
        landing_fields = set(LandingReceipt.__dataclass_fields__)
        if (not isinstance(landing, Mapping) or set(landing) != landing_fields
                or not landing_receipt_hash_is_valid(landing)):
            raise OutcomeError("landing receipt hash mismatch or schema mismatch")
        if (landing.get("evidence_package_hash") != evidence.get("evidence_package_hash")
                or landing.get("evidence_package_id") != evidence.get("evidence_package_id")):
            raise OutcomeError("landing receipt names different evidence")
        if landing.get("semantic_acceptance_hash") != semantic_hash or landing.get("semantic_acceptance_id") != semantic.get("acceptance_id"):
            raise OutcomeError("landing receipt is detached from semantic acceptance")
        try:
            strategy = LandingStrategy(landing.get("landing_strategy"))
        except ValueError as exc:
            raise OutcomeError("landing strategy is invalid") from exc
        result = landing.get("landed_result")
        if not isinstance(result, Mapping):
            raise OutcomeError("landing receipt has no landed result")
        acceptance = SemanticAcceptance(**{**dict(semantic), "acceptance_hash": semantic_hash})
        proof = prove_landed_equivalence(acceptance, result, strategy)
        recorded = landing.get("equivalence")
        if (recorded != proof.to_dict() or landing.get("landed_head") != result.get("head_sha")
                or landing.get("landed_tree_sha") != result.get("tree_sha")
                or landing.get("repository") != result.get("repository")
                or landing.get("destination_branch") != result.get("destination_branch")):
            raise OutcomeError("landing equivalence or landed source identity is not recomputed")
        accepted_candidate = landing.get("accepted_candidate")
        expected_candidate = {"source_digest": acceptance.candidate_source_digest,
                              "head_sha": acceptance.candidate_head_sha,
                              "tree_sha": acceptance.candidate_tree_sha,
                              "diff_digest": acceptance.candidate_diff_digest}
        if accepted_candidate != expected_candidate:
            raise OutcomeError("landing receipt accepted candidate differs from semantic acceptance")
        landed = proof.equivalent
        landing_result = _gate_record("OBSERVED", bool(landed),
                                      "repository equivalence proved" if landed else proof.reason)
    capacity = None
    offer = None
    if capacity_payload is not None:
        try:
            capacity = capacity_receipt_from_dict(capacity_payload).to_dict()
        except Exception as exc:
            raise OutcomeError("capacity receipt is invalid") from exc
    if offer_payload is not None:
        try:
            offer = provider_model_offer_from_dict(offer_payload).to_dict()
        except Exception as exc:
            raise OutcomeError("offer receipt is invalid") from exc
    all_dispatch = dispatches
    cap_refs = {ref for row in all_dispatch for ref in row.get("capacity_receipt_refs", ())}
    offer_refs = {ref for row in all_dispatch for ref in row.get("offer_receipt_refs", ())}
    if capacity and cap_refs != {f"capacity:{capacity['receipt_hash']}"}:
        raise OutcomeError("capacity receipt is not the exact source selected by dispatch")
    if offer and offer_refs != {f"offer:{offer['receipt_hash']}"}:
        raise OutcomeError("offer receipt is not the exact source selected by dispatch")
    if offer and not cap_refs.intersection({offer.get("capacity_receipt_ref")}):
        raise OutcomeError("offer capacity reference was not recorded by dispatch")
    if capacity:
        for field, actual in (("provider", capacity["provider"]), ("pool", capacity["pool_id"]),
                              ("account", capacity["account_identity"])):
            fact = facts.get(field)
            if not isinstance(fact, Mapping) or fact.get("status") != "OBSERVED" or fact.get("value") != actual:
                raise OutcomeError(f"{field} facts do not match the selected capacity receipt")
            if fact.get("source_ref") != f"capacity:{capacity['receipt_hash']}":
                raise OutcomeError(f"{field} facts are not sourced from the selected capacity receipt")
    if offer and capacity:
        if offer["capacity_receipt_ref"] != f"capacity:{capacity['receipt_hash']}":
            raise OutcomeError("offer receipt does not bind the supplied capacity receipt")
        if offer["provider"] != capacity["provider"] or offer["pool_id"] != capacity["pool_id"]:
            raise OutcomeError("offer and capacity pool identities disagree")
        if offer["native_model"] not in capacity["exposed_models"]:
            raise OutcomeError("offer model is not exposed by its capacity receipt")
    if offer:
        if offer["native_model"] != dispatches[0].get("selected_model"):
            raise OutcomeError("offer model does not match the selected dispatch model")
        if not capacity:
            for field in ("provider", "pool"):
                fact = facts.get(field)
                if not isinstance(fact, Mapping) or fact.get("status") != "OBSERVED" or fact.get("value") != offer["provider" if field == "provider" else "pool_id"]:
                    raise OutcomeError(f"{field} facts do not match selected offer")
                if fact.get("source_ref") != f"offer:{offer['receipt_hash']}":
                    raise OutcomeError(f"{field} facts are not sourced from the selected offer")
    observed_fields = set()
    if capacity:
        observed_fields.update({"provider", "pool", "account"})
    if offer:
        observed_fields.update({"provider", "pool"})
    execution_key = _hash(_canonical({"package_hash": package_hash, "run_id": run_id,
                                      "packet_id": packet_id,
                                      "dispatch_receipt_hashes": sorted(dispatch_hashes)}))
    known_sources = _source_ref_index(package, evidence, capacity, offer, semantic, landing)
    attempts_by_hash = {row["receipt_hash"]: row for row in attempts}
    checked_facts = _validate_facts(facts, execution_key, known_sources,
                                    attempts_by_hash=attempts_by_hash,
                                    observed_fields=observed_fields)
    attempt_numbers = {row["attempt"] for row in attempts}
    for metric, entries in checked_facts["metrics"].items():
        if any(entry.get("attempt") not in attempt_numbers for entry in entries):
            raise OutcomeError(f"metric {metric} references an unknown attempt")
    for collection in (checked_facts["fault_events"], checked_facts["interventions"]):
        if any(item.get("attempt") is not None and item["attempt"] not in attempt_numbers for item in collection):
            raise OutcomeError("fact references an unknown attempt")
    existing_faults = {(item.get("attempt"), item.get("value"))
                       for item in checked_facts["fault_events"]
                       if item.get("status") == "OBSERVED"}
    for receipt in attempts:
        failure = receipt.get("failure_class")
        if not failure:
            if not any(item.get("attempt") == receipt["attempt"]
                       and item.get("status") == "UNKNOWN"
                       for item in checked_facts["fault_events"]):
                checked_facts["fault_events"].append({
                    "status": "UNKNOWN", "reason": "canonical attempt has no failure attribution",
                    "attempt": receipt["attempt"], "execution_key": execution_key})
            continue
        classification = "runtime_provider" if failure == "runtime_provider" else "unknown"
        if (receipt["attempt"], classification) in existing_faults:
            continue
        if any(item.get("attempt") == receipt["attempt"] and item.get("status") == "OBSERVED"
               for item in checked_facts["fault_events"]):
            raise OutcomeError("fault event contradicts canonical provider failure attribution")
        checked_facts["fault_events"].append({
            "status": "OBSERVED", "value": classification,
            "source_ref": f"attempt:{receipt['receipt_hash']}",
            "source_sha256": receipt["receipt_hash"],
            "attempt": receipt["attempt"], "execution_key": execution_key})
    metric_units = {"elapsed_s": "seconds", "prompt_tokens": "tokens",
                    "completion_tokens": "tokens", "ttft_s": "seconds",
                    "realized_cost": "currency"}
    attempt_by_number = {row["attempt"]: row for row in attempts}
    for metric, unit in metric_units.items():
        entries = checked_facts["metrics"].setdefault(metric, [])
        observed_attempts = {entry["attempt"] for entry in entries}
        for number, receipt in sorted(attempt_by_number.items()):
            if number in observed_attempts:
                continue
            raw_value = receipt.get(metric) if metric in receipt else None
            if metric in {"elapsed_s", "prompt_tokens", "completion_tokens"} \
                    and isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool) \
                    and math.isfinite(raw_value) and raw_value > 0:
                entries.append({"status": "OBSERVED", "value": raw_value,
                                "attempt": number, "unit": unit,
                                "source_ref": f"attempt:{receipt['receipt_hash']}",
                                "source_sha256": receipt["receipt_hash"],
                                "execution_key": execution_key})
            else:
                entries.append({"status": "ABSENT", "reason": "no explicit source-backed measurement",
                                "attempt": number, "execution_key": execution_key})
        entries.sort(key=lambda item: item["attempt"])
    arm = {
        "task_class": _arm_fact(checked_facts["task_class"]),
        "risk_class": _arm_fact(checked_facts["risk_class"]),
        "role": dispatches[0].get("requested_role") or {"status": "UNKNOWN", "reason": "dispatch role missing"},
        "harness": _arm_fact(checked_facts["harness"]),
        "offer_harness": {
            "status": "OBSERVED", "identity": offer["harness"],
            "usage_path": offer["usage_path"],
            "installed_version": {"status": "UNKNOWN", "reason": "offer does not establish installed harness version"},
            "config_sha256": {"status": "UNKNOWN", "reason": "offer does not establish harness configuration"},
        } if offer else {"status": "UNKNOWN", "reason": "no selected offer receipt"},
        "target_id": dispatches[0].get("selected_target_id"),
        "host": dispatches[0].get("selected_host"),
        "model": dispatches[0].get("selected_model"),
        "runtime_kind": dispatches[0].get("selected_runtime_kind") or {"status": "UNKNOWN"},
        "runtime_version": dispatches[0].get("selected_runtime_version") or {"status": "UNKNOWN"},
        "model_digest": dispatches[0].get("selected_model_digest") or {"status": "UNKNOWN"},
        "backend": dispatches[0].get("selected_backend") or {"status": "UNKNOWN"},
        "provider": _arm_fact(checked_facts["provider"]),
        "pool": _arm_fact(checked_facts["pool"]),
        "account": _arm_fact(checked_facts["account"]),
    }
    first_pass = _first_pass_status(verified.get("value") is True, attempt_outcomes,
                                    semantic_result.get("value") if semantic_result.get("status") == "OBSERVED" else None)
    return {
        "schema": SCHEMA, "execution_key": execution_key,
        "raw_evidence_package": _copy_json(dict(evidence)),
        "companion_facts": checked_facts,
        "artifact_extensions_b64": {k: base64.b64encode(v).decode("ascii") for k, v in sorted(ext.items())},
        "semantic_acceptance": _copy_json(dict(semantic)) if semantic is not None else None,
        "candidate_source_snapshot": _copy_json(dict(candidate_source_snapshot))
        if candidate_source_snapshot is not None else None,
        "landing_receipt": _copy_json(dict(landing)) if landing is not None else None,
        "capacity_receipt": capacity, "offer_receipt": offer,
        "arm": arm, "attempt_outcomes": attempt_outcomes,
        "first_pass": first_pass,
        "gates": {"verified": verified, "semantic_acceptance": semantic_result,
                  "repository_landed": landing_result,
                  "delivery": _gate_record("ABSENT", reason="no source-bound delivery contract exists")},
    }


@dataclass(frozen=True)
class ExecutionOutcomeRecord:
    """Canonical immutable bytes; mapping views are detached snapshots."""
    _canonical_bytes: bytes

    def __post_init__(self) -> None:
        if not isinstance(self._canonical_bytes, bytes):
            raise OutcomeError("canonical outcome storage must be immutable bytes")
        object.__setattr__(self, "_canonical_bytes", bytes(self._canonical_bytes))

    @property
    def raw_record_hash(self) -> str:
        return _hash(self._canonical_bytes)

    @property
    def execution_key(self) -> str:
        return self.to_dict()["execution_key"]

    @property
    def arm_key(self) -> str:
        return _hash(_canonical(self.to_dict()["arm"]))

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self._canonical_bytes)

    def to_bytes(self) -> bytes:
        return bytes(self._canonical_bytes)


def build_outcome_record(*, evidence_package: Mapping[str, Any], companion_facts: Mapping[str, Any] | None = None,
                         semantic_acceptance: Mapping[str, Any] | None = None,
                         landing_receipt: Mapping[str, Any] | None = None,
                         capacity_receipt: Mapping[str, Any] | None = None,
                         offer_receipt: Mapping[str, Any] | None = None,
                         candidate_source_snapshot: Mapping[str, Any] | None = None,
                         artifact_extensions: Mapping[str, bytes] | None = None) -> ExecutionOutcomeRecord:
    """Build a pure snapshot from exact caller-owned receipts and artifact bytes."""
    data = _evaluate(evidence_package, companion_facts or {}, semantic_acceptance,
                     landing_receipt, capacity_receipt, offer_receipt,
                     artifact_extensions or {}, candidate_source_snapshot)
    return ExecutionOutcomeRecord(_canonical(data))


def validate_outcome_record(record: ExecutionOutcomeRecord | bytes) -> ExecutionOutcomeRecord:
    """Strictly revalidate canonical bytes and all derived gates without I/O."""
    raw = record.to_bytes() if isinstance(record, ExecutionOutcomeRecord) else record
    if not isinstance(raw, bytes):
        raise OutcomeError("validation requires canonical outcome bytes or record")
    try:
        data = json.loads(raw, object_pairs_hook=_unique_json,
                          parse_constant=lambda _: (_ for _ in ()).throw(OutcomeError("non-finite JSON number")))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OutcomeError("outcome record is not valid JSON") from exc
    if _canonical(data) != raw or not isinstance(data, Mapping) or data.get("schema") != SCHEMA:
        raise OutcomeError("outcome record is not canonical or has an unknown schema")
    try:
        ext = {k: base64.b64decode(v, validate=True)
               for k, v in data.get("artifact_extensions_b64", {}).items()}
        rebuilt = _evaluate(data.get("raw_evidence_package"), data.get("companion_facts"),
                            data.get("semantic_acceptance"), data.get("landing_receipt"),
                            data.get("capacity_receipt"), data.get("offer_receipt"), ext,
                            data.get("candidate_source_snapshot"))
    except OutcomeError:
        raise
    except Exception as exc:
        raise OutcomeError("outcome record contains malformed canonical data") from exc
    if _canonical(rebuilt) != raw:
        raise OutcomeError("derived outcome fields do not match their canonical source records")
    return ExecutionOutcomeRecord(bytes(raw))
