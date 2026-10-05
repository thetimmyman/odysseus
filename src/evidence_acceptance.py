"""Acceptance bindings for EvidencePackage v2, independent of model narration.

The envelope retains full routing evidence, every verified source and exact-base
baseline receipts. Mutable observations are supplied by consumers, never copied
over the historical sealed records. v1 remains readable with its original hash.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from src.attempt_receipt import recompute_outcome, verification_receipt_hash_is_valid
from src.dispatch_boundary import validate_dispatch_evidence
from src.dispatch_routing import EXACTNESS_EXACT
from src.evidence_package import ValidationIssue
from src.source_snapshot import snapshot_digest_is_valid


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("evidence timestamps must include a timezone")
    return result


def acceptance_issues(payload: Mapping[str, Any], *, current_profiles=None,
                      current_verifier_digests=None, current_policy_ref=None,
                      now=None) -> tuple[ValidationIssue, ...]:
    """Fail closed for missing or malformed v2 bindings, with stable reason codes."""
    issues: list[ValidationIssue] = []

    def reject(code, detail, subject="acceptance_context"):
        issues.append(ValidationIssue(code, detail, subject=subject))

    try:
        context = payload["acceptance_context"]
        if not isinstance(context, Mapping):
            raise ValueError("acceptance_context must be a mapping")
        sources = context["source_snapshots"]
        routing = context["dispatch_evidence"]
        baselines = context.get("baseline_receipts", [])
        if not isinstance(sources, Mapping) or not isinstance(routing, Mapping):
            raise ValueError("source and dispatch indexes must be mappings")
        package = payload["execution_package"]
        planned = dict(package["verification"]["verifier_digests"])
        if not planned:
            reject("verifier_identity_mismatch", "v2 requires preregistered verifier digests")
        if current_verifier_digests is not None and dict(current_verifier_digests) != planned:
            reject("verifier_identity_mismatch", "current verifier differs from its preregistered identity")
        for digest, source in sources.items():
            if (not snapshot_digest_is_valid(dict(source)) or source["snapshot_digest"] != digest
                    or source.get("truncated_paths") or source.get("diff_truncated")):
                reject("source_identity_missing_or_ambiguous", "source snapshot is incomplete or invalid")
            if (source.get("repo_identity") != package["source"].get("repo_identity")
                    or source.get("base_sha") != package["source"].get("base_sha")):
                reject("source_changed_after_verification", "snapshot belongs to another repository or base")

        dispatches = {d["receipt_hash"]: d for d in payload["dispatch_receipts"]}
        if not dispatches or set(routing) != set(dispatches):
            reject("dispatch_evidence_missing", "every dispatch requires its complete routing evidence")
        profiles = {}
        for digest, dispatch in dispatches.items():
            evidence = routing.get(digest)
            if not evidence:
                continue
            valid, reasons = validate_dispatch_evidence(evidence)
            if not valid or evidence["seal"]["dispatch_receipt_hash"] != digest:
                reject("dispatch_target_mismatch", "routing evidence invalid: " + ",".join(reasons))
            profile = evidence["decision"]["selected_profile"]
            profiles[digest] = profile
            for name in ("profile_id", "provider", "runtime_kind", "runtime_version",
                         "backend", "model", "model_digest", "exactness"):
                if not profile.get(name):
                    reject("runtime_identity_missing", "runtime profile lacks " + name)
            if "runtime_options" not in profile:
                reject("runtime_identity_missing", "runtime semantics options must be explicit")
            if (profile["exactness"] != EXACTNESS_EXACT and
                    "exact_reference_semantics" in dispatch.get("requested_capabilities", [])):
                reject("approximate_profile_for_exact_requirement", "approximate execution cannot inherit reference evidence")
            if current_profiles is not None and current_profiles.get(profile["profile_id"]) != profile:
                reject("runtime_profile_changed", "current runtime/model/profile differs from the sealed execution")
            if current_policy_ref is not None and current_policy_ref != dispatch.get("policy_ref"):
                reject("policy_changed", "current policy differs from the dispatch policy")

        attempts = {a["attempt"]: a for a in payload["attempt_receipts"]}
        if not attempts:
            reject("no_attempts", "v2 requires actual attempts")
        if list(attempts) != list(range(1, len(payload["attempt_receipts"]) + 1)):
            reject("retry_history_omitted", "attempt order and numbers must be contiguous")
        verifications = payload["verification_receipts"]
        initial = package["source"]
        initial_digests = dict(initial.get("relevant_digests", []))
        for number in attempts:
            writes = {path for n, attempt in attempts.items() if n <= number
                      for path in attempt.get("actual_write_set", [])}
            for receipt in (r for r in verifications if r["attempt"] == number):
                source = sources.get(receipt["source_snapshot_digest"], {})
                measured = set(source.get("changed_paths", []))
                measured.update(path for key in ("staged_paths", "unstaged_paths", "untracked_paths")
                                for path in source.get(key, []))
                current_digests = dict(source.get("relevant_digests", []))
                if source.get("head_sha") != initial.get("head_sha") and not source.get("changed_paths"):
                    reject("source_identity_missing_or_ambiguous", "changed HEAD requires measured paths against the sealed base")
                measured.update(path for path, digest in current_digests.items()
                                if initial_digests.get(path) != digest)
                measured.update(set(initial_digests) - set(current_digests))
                unchanged = {path for path, digest in initial_digests.items()
                             if current_digests.get(path) == digest}
                unexplained = measured - unchanged - writes
                if unexplained or (measured - unchanged - set(package["write_scope"])):
                    reject("write_outside_authorized_scope", "measured source changes are not reconciled to recorded authorized writes")
        checked_attempts = set()
        for receipt in verifications:
            number = receipt["attempt"]
            checked_attempts.add(number)
            if (number not in attempts or receipt.get("run_id") != package["run_id"]
                    or receipt.get("packet_id") != package["packet_id"]
                    or receipt.get("execution_package_hash") != package["package_hash"]):
                reject("verification_receipt_unbound", "verification is detached from this run/attempt")
            if receipt["source_snapshot_digest"] not in sources:
                reject("source_identity_missing_or_ambiguous", "verified candidate snapshot is absent")
            if receipt.get("normalized_command") != package["verification"]["command"]:
                reject("verifier_identity_mismatch", "verification command differs from the plan")
            if (not receipt.get("stdout_ref") or not receipt.get("stderr_ref")
                    or not receipt.get("started_at") or not receipt.get("ended_at")):
                reject("command_capture_missing", "authoritative command must retain both streams and timestamps")
            if type(receipt.get("exit_code")) is not int:
                reject("malformed_record", "command exit code must be an integer")
            if receipt.get("claimed_preexisting"):
                baseline = next((b for b in baselines if b.get("receipt_hash") ==
                                 receipt.get("baseline_receipt_hash")), None)
                source = sources.get(receipt.get("baseline_source_digest"), {})
                if (not baseline or not verification_receipt_hash_is_valid(baseline)
                        or baseline.get("source_snapshot_digest") != source.get("snapshot_digest")
                        or source.get("head_sha") != package["source"]["base_sha"]
                        or any(source.get(k) for k in ("staged_paths", "unstaged_paths", "untracked_paths"))
                        or baseline.get("verifier_digest") != receipt.get("verifier_digest")
                        or baseline.get("normalized_command") != receipt.get("normalized_command")
                        or recompute_outcome(baseline) != "FAIL"
                        or baseline.get("failure_fingerprint") != receipt.get("failure_fingerprint")
                        or receipt.get("baseline_failure_fingerprint") != baseline.get("failure_fingerprint")):
                    reject("preexisting_failure_without_baseline", "exact-base matching command receipt is required")
        if set(attempts) != checked_attempts:
            reject("verification_receipt_unbound", "each worker attempt requires retained verification history")
        for number, attempt in attempts.items():
            if attempt.get("run_id") != package["run_id"] or attempt.get("packet_id") != package["packet_id"]:
                reject("dispatch_target_mismatch", "attempt belongs to another run or packet")
            if not all(attempt.get(key) for key in ("output_ref", "rendered_context_ref",
                                                   "started_at", "ended_at", "generation_token")):
                reject("attempt_capture_missing", "complete worker output, context, timestamps and fencing token are required")
            profile = profiles.get(attempt["dispatch_receipt_hash"], {})
            dispatch = dispatches.get(attempt["dispatch_receipt_hash"], {})
            writes = set(attempt.get("actual_write_set", []))
            if (not writes.issubset(set(package["write_scope"])) or
                    not writes.issubset(set(dispatch.get("granted_write_scope", [])))):
                reject("write_outside_authorized_scope", "write not authorized by both intent and this attempt's dispatch")
            if (not set(dispatch.get("granted_write_scope", [])).issubset(set(package["write_scope"]))
                    or not set(dispatch.get("granted_read_scope", [])).issubset(set(package["read_scope"]))
                    or not set(dispatch.get("granted_tools", [])).issubset(set(package.get("allowed_tools", [])))
                    or dispatch.get("network_policy") != package.get("network_policy")):
                reject("dispatch_target_mismatch", "dispatch grants exceed the immutable execution envelope")
            if any(attempt.get(name) != profile.get(name) for name in ("runtime_kind", "runtime_version")):
                reject("runtime_profile_changed", "attempt runtime differs from the selected profile")
            if attempt.get("model_version") != profile.get("model_digest"):
                reject("runtime_profile_changed", "attempt must name the exact model artifact")
            if number > 1 and attempt.get("repair_of") != number - 1:
                reject("retry_history_omitted", "repair chain does not name the preceding attempt")
        if attempts:
            final = max(attempts)
            for control in ("positive_control", "negative_control"):
                if package["verification"].get(control) and not any(
                        r.get("attempt") == final and r.get("control_id") == control
                        and r.get("control_passed") is True
                        and r.get("control_expected") and r.get("control_expected") == r.get("control_observed")
                        and recompute_outcome(r) == "PASS" for r in verifications):
                    reject("missing_required_" + control, "final candidate requires the declared " + control)
        now = now or datetime.now(timezone.utc)
        for requirement in package["evidence_requirements"]:
            rule = requirement.get("freshness_rule")
            for receipt in verifications:
                if receipt["attempt"] != max(attempts, default=0):
                    continue
                if requirement["requirement_id"] not in receipt.get("requirement_ids", []):
                    continue
                expected = requirement["expected_verifier"]
                kind = requirement["kind"]
                derived_verifiers = {
                    "source_binding": "src.source_snapshot.take_source_snapshot",
                    "scope_check": "actual write set vs authorized write scope",
                    "negative_control": receipt.get("verifier_id", "") + " (negative control)",
                }
                binding_ok = expected == receipt.get("verifier_id") or expected == derived_verifiers.get(kind)
                if kind == "negative_control":
                    binding_ok = binding_ok and receipt.get("control_id") == "negative_control"
                if not binding_ok:
                    reject("verifier_identity_mismatch", "requirement names a different authoritative verifier")
                if requirement["vantage"] != receipt.get("proof_vantage"):
                    reject("proof_vantage_mismatch", "receipt cannot prove the required observation vantage")
                if rule:
                    # One documented, deterministic rule; unknown syntax fails closed.
                    if not rule.startswith("ttl_seconds:") or not rule[12:].isdigit() or int(rule[12:]) <= 0:
                        reject("freshness_rule_invalid", "expected ttl_seconds:<positive integer>")
                    elif (now - _time(receipt["ended_at"])).total_seconds() >= int(rule[12:]):
                        reject("live_evidence_stale", "live observation exceeded its TTL")
                    elif _time(receipt["ended_at"]) > now:
                        reject("freshness_rule_invalid", "live observation is in the future")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        reject("acceptance_context_invalid", "malformed or missing v2 acceptance binding: " + type(exc).__name__)
    return tuple(issues)
