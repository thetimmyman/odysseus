"""Bounded local worker coordinator with repair and dispatch revalidation.

This is deliberately a first-slice coordinator, not a scheduler or durable
ledger. Callers supply the already-bound dispatch decision and narrow execution
adapters. Every retry reuses a still-valid standing decision; the selector is
called only after an observed routable invalidation and at most once for that
retry. Receipt/evidence authority remains in the canonical evidence modules.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import re
from typing import Any, Callable, Mapping, Optional, Sequence

from src.attempt_receipt import (
    CLAIM_BLOCKED, CLAIM_FAIL, CLAIM_INCONCLUSIVE, CLAIM_PASS,
    attempt_receipt_hash_is_valid, context_projection_digest,
    verification_receipt_hash_is_valid,
)
from src.dispatch_boundary import verify_invocation
from src.evidence_contract import requirement_index
from src.evidence_package import (
    requirement_from_dict, seal_evidence_package, validate_evidence_package,
)
from src.execution_package import Budgets, VerificationPlan, package_hash_is_valid
from src.repair_packet import (
    RepairContextTooLarge, build_repair_context, parse_verification_failure,
    render_initial_context, render_repair_context, failure_fingerprint,
)
from src.source_snapshot import snapshot_digest_is_valid
from src.work_packet import interface_digest_from_normalized

ACCEPTED_CANDIDATE = "ACCEPTED_CANDIDATE"
REFUSED = "REFUSED"
BLOCKED = "BLOCKED"
ESCALATE = "ESCALATE"

_ROUTABLE_FACTS = (
    "capability_receipts", "capacity_fresh", "capacity_receipt_refs",
    "policy_ref", "target_id", "profile_id", "endpoint_identity",
    "runtime_identity", "granted_tools", "granted_write_scope",
    "granted_read_scope", "network_policy", "execution_package_hash",
    "source_digest", "interface_digest",
)
_CAPACITY_REF = re.compile(r"^capacity:[0-9a-f]{64}$")


@dataclass(frozen=True)
class CurrentDispatchFacts:
    """Measured DR-10 facts captured immediately before a possible dispatch."""
    capability_receipts: Mapping[str, bool] = field(default_factory=dict)
    capacity_fresh: Optional[bool] = None
    capacity_receipt_refs: tuple[str, ...] = ()
    policy_ref: str = ""
    target_id: str = ""
    profile_id: str = ""
    endpoint_identity: str = ""
    runtime_identity: Mapping[str, Any] = field(default_factory=dict)
    granted_tools: tuple[str, ...] = ()
    granted_write_scope: tuple[str, ...] = ()
    granted_read_scope: tuple[str, ...] = ()
    network_policy: str = ""
    execution_package_hash: str = ""
    source_digest: str = ""
    interface_digest: str = ""


@dataclass(frozen=True)
class WorkerExecution:
    """Adapter return: canonical receipt plus bytes for every referenced artifact."""
    receipt: Any
    artifacts: Mapping[str, bytes] = field(default_factory=dict)
    failure_kind: str = ""
    changed_files: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationExecution:
    receipt: Any
    artifacts: Mapping[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class LoopResult:
    status: str
    attempts: tuple = ()
    verifications: tuple = ()
    dispatches: tuple = ()
    evidence_package: Optional[Mapping[str, Any]] = None
    validation: Any = None
    refusal_reason: str = ""


class _Stop(Exception):
    pass


@dataclass(frozen=True)
class _FrozenExecutionPackage:
    """ExecutionPackage-compatible stable view used for canonical sealing."""
    encoded: str

    def to_dict(self) -> dict:
        return json.loads(self.encoded)


def _package_payload(package: Any) -> Mapping[str, Any]:
    if hasattr(package, "to_dict"):
        return package.to_dict()
    return package if isinstance(package, Mapping) else {}


def _freeze_package_payload(package: Any) -> tuple[dict, str]:
    """Take a canonical JSON snapshot so mutable adapters cannot retask retries."""
    encoded = json.dumps(_package_payload(package), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False,
                         allow_nan=False)
    frozen = json.loads(encoded)
    if not isinstance(frozen, dict):
        raise ValueError("execution package payload must be a JSON object")
    return frozen, encoded


def _runtime_pin_identity(bound: Any) -> dict:
    pin = bound.pin_for(bound.decision.selected_profile.profile_id) or {}
    return {key: pin.get(key) for key in (
        "provider", "runtime_kind", "runtime_version", "runtime_commit",
        "runtime_image_digest", "backend", "backend_version", "model",
        "model_digest", "runtime_options", "configured_context",
        "configured_served_context")}


def _valid_package(package: Any, bound: Any) -> tuple[bool, str]:
    payload = _package_payload(package)
    if not package_hash_is_valid(payload):
        return False, "execution package hash is invalid"
    source = payload.get("source") or {}
    if not snapshot_digest_is_valid(source):
        return False, "source snapshot identity is invalid"
    if source.get("truncated_paths") or source.get("diff_truncated"):
        return False, "source snapshot is incomplete"
    if not payload.get("write_scope"):
        return False, "writable worker package has no declared write scope"
    interface = payload.get("interface")
    if (not isinstance(interface, list) or not interface
            or any(not isinstance(item, str) or not item for item in interface)
            or interface_digest_from_normalized(interface) != payload.get("interface_digest")):
        return False, "writable worker package has no declared interface"
    try:
        plan = VerificationPlan(**dict(payload.get("verification") or {}))
        budgets = Budgets(**dict(payload.get("budgets") or {}))
        requirement_index(tuple(requirement_from_dict(r)
                               for r in payload.get("evidence_requirements") or ()))
    except (TypeError, ValueError) as exc:
        return False, f"verifier contract or budget is invalid: {exc}"
    if budgets.max_attempts < 1:
        return False, "package has no attempt budget"
    if (plan.verifier_paths and any(not plan.digest_of(path)
                                    for path in plan.verifier_paths)):
        return False, "verifier source path lacks a sealed digest"
    if not str(payload.get("objective") or "").strip():
        return False, "objective must be non-empty"
    req = getattr(bound, "request", None)
    if (req is None or req.execution_package_hash != payload.get("package_hash")
            or req.run_id != payload.get("run_id")
            or req.packet_id != payload.get("packet_id")):
        return False, "standing routing request is not bound to this package"
    decision = getattr(bound, "decision", None)
    if decision is None or decision.request != req:
        return False, "standing decision does not bind the original request"
    return True, ""


def _fact_map(facts: Any) -> Mapping[str, Any]:
    if isinstance(facts, Mapping):
        return facts
    return {name: getattr(facts, name) for name in _ROUTABLE_FACTS
            if hasattr(facts, name)}


def _decision_facts_match(bound: Any, package: Any, facts: Any, *,
                          immutable_runtime_identity: Optional[Mapping[str, Any]] = None
                          ) -> tuple[bool, str, bool]:
    f = _fact_map(facts)
    d = bound.decision
    payload = _package_payload(package)
    pin = bound.pin_for(d.selected_profile.profile_id) or {}
    # Immutable bindings cannot be repaired by selecting a different target.
    immutable = (
        ("execution_package_hash", payload.get("package_hash", "")),
        ("source_digest", (payload.get("source") or {}).get("snapshot_digest", "")),
        ("interface_digest", payload.get("interface_digest", "")),
    )
    for key, expected in immutable:
        if f.get(key) != expected:
            return False, f"immutable {key} differs from the sealed package", True
    expected_runtime = (dict(immutable_runtime_identity)
                        if immutable_runtime_identity is not None
                        else _runtime_pin_identity(bound))
    if dict(f.get("runtime_identity") or {}) != expected_runtime:
        return False, "immutable runtime/model identity differs from the standing decision", True
    required_caps = tuple(d.capability_receipt_refs
                          if hasattr(d, "capability_receipt_refs") else ())
    caps = f.get("capability_receipts") or {}
    if any(caps.get(ref) is not True for ref in required_caps):
        return False, "capability receipt missing, stale, or unqualified", False
    resource = dict(getattr(d, "resource_facts", {}) or {})
    if hasattr(d, "capacity_receipt_refs"):
        raw_capacity_refs = d.capacity_receipt_refs
        if not isinstance(raw_capacity_refs, (tuple, list)):
            return False, "decision capacity receipt references are malformed", False
        relied_refs = tuple(raw_capacity_refs)
        hosted = pin.get("locality") == "hosted"
        legacy_capacity_relied_on = False
    else:
        # Compatibility for pre-top-level decision objects: only their sealed
        # resource facts may declare that capacity was relied on.
        raw_capacity_refs = resource.get("capacity_receipt_refs") or ()
        if not isinstance(raw_capacity_refs, (tuple, list)):
            return False, "legacy capacity receipt references are malformed", False
        relied_refs = tuple(raw_capacity_refs)
        hosted = pin.get("locality") == "hosted"
        legacy_capacity_relied_on = bool(relied_refs) or any(
            "capacity" in str(key).lower() or "entitlement" in str(key).lower()
            for key in resource)
    if any(not isinstance(ref, str) or not _CAPACITY_REF.fullmatch(ref)
           for ref in relied_refs) or len(set(relied_refs)) != len(relied_refs):
        return False, "decision capacity receipt references are malformed", False
    capacity_relied_on = bool(relied_refs) or hosted or legacy_capacity_relied_on
    if capacity_relied_on:
        observed_raw = f.get("capacity_receipt_refs") or ()
        if not isinstance(observed_raw, (tuple, list)):
            return False, "current capacity receipt references are malformed", False
        observed_refs = tuple(observed_raw)
        if (f.get("capacity_fresh") is not True
                or (hosted and not relied_refs)
                or any(not isinstance(ref, str) or not _CAPACITY_REF.fullmatch(ref)
                       for ref in observed_refs)
                or len(set(observed_refs)) != len(observed_refs)
                or set(observed_refs) != set(relied_refs)):
            return False, "relied-on capacity facts are stale or unproven", False
    comparisons = (
        ("policy_ref", getattr(d.policy, "policy_ref", "")),
        ("target_id", pin.get("target_id", "")),
        ("profile_id", pin.get("profile_id", "")),
        ("endpoint_identity", pin.get("endpoint_identity", "")),
        ("granted_tools", tuple(d.granted_tools)),
        ("granted_write_scope", tuple(d.granted_write_scope)),
        ("granted_read_scope", tuple(d.granted_read_scope)),
        ("network_policy", d.network_policy),
    )
    for key, expected in comparisons:
        actual = f.get(key)
        if isinstance(expected, tuple):
            actual = tuple(actual or ())
        if actual != expected:
            return False, f"current {key} differs from the standing decision/package", False
    return True, "", False


def _receipt_matches(receipt: Any, package: Any, attempt: int, dispatch_hash: str,
                     *, target_id: str, host: str, model: str,
                     repair_of: int = 0) -> bool:
    p = _package_payload(package)
    return (bool(receipt) and attempt_receipt_hash_is_valid(receipt.to_dict())
            and receipt.run_id == p.get("run_id")
            and receipt.packet_id == p.get("packet_id")
            and receipt.execution_package_hash == p.get("package_hash")
            and receipt.attempt == attempt and receipt.repair_of == repair_of
            and receipt.dispatch_receipt_hash == dispatch_hash
            and receipt.target_id == target_id and receipt.host == host
            and receipt.model == model
            and receipt.context_projection_hash
            and receipt.rendered_context_ref is not None
            and receipt.output_ref is not None
            and not receipt.writes_outside_scope
            and set(receipt.declared_write_set) == set(p.get("write_scope") or ()))


def _verification_matches(receipt: Any, package: Any, attempt: int) -> bool:
    p = _package_payload(package)
    plan = p.get("verification") or {}
    digest_set = {d for _path, d in plan.get("verifier_digests") or ()}
    return (bool(receipt) and verification_receipt_hash_is_valid(receipt.to_dict())
            and receipt.run_id == p.get("run_id")
            and receipt.packet_id == p.get("packet_id")
            and receipt.execution_package_hash == p.get("package_hash")
            and receipt.attempt == attempt
            and receipt.verifier_id == plan.get("verifier_id")
            and receipt.normalized_command == plan.get("command")
            and receipt.source_snapshot_digest == (p.get("source") or {}).get("snapshot_digest")
            and receipt.verifier_digest in digest_set)


def _collect_artifacts(dst: dict, artifacts: Mapping[str, bytes]) -> None:
    for uri, content in (artifacts or {}).items():
        if uri in dst and dst[uri] != content:
            raise ValueError("artifact URI reused with different bytes")
        dst[uri] = bytes(content)


def _artifact_matches(ref: Any, data: Optional[bytes]) -> bool:
    if ref is None or data is None:
        return False
    return (len(data) == ref.size and
            hashlib.sha256(data).hexdigest() == ref.sha256)


def _seal(package: Any, attempts: Sequence[Any], verifications: Sequence[Any],
          dispatches: Sequence[Any], artifacts: Mapping[str, bytes], *,
          package_id: str) -> tuple[Mapping[str, Any], Any]:
    ep = seal_evidence_package(
        evidence_package_id=package_id, execution_package=package,
        attempt_receipts=attempts, verification_receipts=verifications,
        dispatch_receipts=dispatches)
    payload = ep.to_dict()
    validation = validate_evidence_package(payload, artifact_extensions=artifacts)
    return payload, validation


def run_local_worker_loop(*, execution_package: Any, standing_dispatch: Any,
                          current_facts: Callable[[Any, int], Any],
                          invocation: Callable[[Any], Any],
                          execute_worker: Callable[[Any, str, int, int], WorkerExecution],
                          execute_verifier: Callable[[Any, Any, int], VerificationExecution],
                          changed_files: Optional[Callable[[Any, WorkerExecution], Mapping[str, str]]] = None,
                          select_fresh: Optional[Callable[[Any, int], Any]] = None,
                          max_excerpt: int = 2000) -> LoopResult:
    """Run bounded G1/repair attempts against an immutable package.

    Adapter callbacks do the actual local work; this coordinator validates
    identities and evidence. `select_fresh` receives only the original bound
    request and retry number and is called once only after DR-10 invalidation.
    """
    try:
        p, frozen_package_json = _freeze_package_payload(execution_package)
    except (TypeError, ValueError) as exc:
        return LoopResult(REFUSED, refusal_reason=f"execution package is not canonical JSON: {exc}")
    ok, reason = _valid_package(p, standing_dispatch)
    if not ok:
        return LoopResult(REFUSED, refusal_reason=reason)
    immutable_runtime_identity = _runtime_pin_identity(standing_dispatch)
    frozen_package = _FrozenExecutionPackage(frozen_package_json)
    limit = int((p.get("budgets") or {}).get("max_attempts", 0))
    if limit < 1:
        return LoopResult(REFUSED, refusal_reason="package has no attempt budget")
    attempts, verifications, dispatches = [], [], []
    artifacts: dict[str, bytes] = {}
    seen_fingerprints: set[str] = set()
    bound = standing_dispatch
    last_failure = None

    def package_guard(current_bound: Any) -> Optional[str]:
        try:
            _current, encoded = _freeze_package_payload(execution_package)
        except (TypeError, ValueError) as exc:
            return f"execution package changed or became invalid: {exc}"
        if encoded != frozen_package_json:
            return "immutable execution package changed during the attempt loop"
        valid_package, package_reason = _valid_package(p, current_bound)
        if not valid_package:
            return f"execution package/dispatch binding refused: {package_reason}"
        return None

    def history_result(status: str, reason: str) -> LoopResult:
        if not attempts:
            return LoopResult(status, refusal_reason=reason)
        try:
            payload, validation = _seal(
                frozen_package, attempts, verifications, dispatches, artifacts,
                package_id=f"{p['run_id']}-worker-loop")
            return LoopResult(status, tuple(attempts), tuple(verifications),
                              tuple(dispatches), payload, validation, reason)
        except Exception as exc:
            return LoopResult(status, tuple(attempts), tuple(verifications),
                              tuple(dispatches), refusal_reason=
                              f"{reason}; canonical history sealing failed: {exc}")

    for number in range(1, limit + 1):
        guard_failure = package_guard(bound)
        if guard_failure:
            return history_result(BLOCKED, guard_failure)
        try:
            facts = current_facts(bound, number)
        except Exception as exc:
            return history_result(BLOCKED, f"DR-10 current-facts capture failed: {exc}")
        valid, why, immutable_drift = _decision_facts_match(
            bound, p, facts, immutable_runtime_identity=immutable_runtime_identity)
        if not valid:
            if number == 1 or immutable_drift or select_fresh is None:
                return history_result(BLOCKED, why)
            # Selector authority is used only on an observed invalidation and
            # once. The original request is immutable across that invocation.
            original_request = standing_dispatch.request
            guard_failure = package_guard(bound)
            if guard_failure:
                return history_result(BLOCKED, guard_failure)
            try:
                replacement = select_fresh(original_request, number)
            except Exception as exc:
                return history_result(BLOCKED, f"fresh dispatch selection failed closed: {exc}")
            if (replacement is None or replacement.request != original_request or
                    replacement.decision.request != original_request or
                    tuple(replacement.decision.granted_tools) != tuple(standing_dispatch.decision.granted_tools) or
                    tuple(replacement.decision.granted_write_scope) != tuple(standing_dispatch.decision.granted_write_scope) or
                    tuple(replacement.decision.granted_read_scope) != tuple(standing_dispatch.decision.granted_read_scope) or
                    replacement.decision.network_policy != standing_dispatch.decision.network_policy):
                return history_result(BLOCKED, "fresh dispatch selection failed closed")
            bound = replacement
            guard_failure = package_guard(bound)
            if guard_failure:
                return history_result(BLOCKED, guard_failure)
            if _runtime_pin_identity(bound) != immutable_runtime_identity:
                return history_result(BLOCKED,
                                      "fresh dispatch selection changed immutable runtime/model identity")
            try:
                facts = current_facts(bound, number)
            except Exception as exc:
                return history_result(BLOCKED, f"fresh DR-10 facts unavailable: {exc}")
            valid, why, immutable_drift = _decision_facts_match(
                bound, p, facts, immutable_runtime_identity=immutable_runtime_identity)
            if not valid:
                return history_result(BLOCKED, why)
        try:
            guard_failure = package_guard(bound)
            if guard_failure:
                return history_result(BLOCKED, guard_failure)
            verify_invocation(bound, invocation=invocation(bound))
            dispatch = bound.decision.to_ps638_receipt_kwargs()
            from src.execution_package import make_dispatch_receipt
            dispatch_receipt = make_dispatch_receipt(**dispatch)
            if dispatch_receipt.receipt_hash != bound.decision.receipt_hash:
                raise ValueError("canonical dispatch receipt does not match decision")
        except Exception as exc:
            return history_result(BLOCKED, f"dispatch pin refused: {exc}")
        if not dispatches or dispatches[-1].receipt_hash != dispatch_receipt.receipt_hash:
            dispatches.append(dispatch_receipt)
        if number == 1:
            context = render_initial_context(p)
            repair_of = 0
        else:
            if last_failure is None:
                return history_result(ESCALATE, "no deterministic repair evidence")
            guard_failure = package_guard(bound)
            if guard_failure:
                return history_result(BLOCKED, guard_failure)
            try:
                current_changed_files = (changed_files(bound, last_execution)
                                         if changed_files
                                         else last_execution.changed_files)
                context_obj = build_repair_context(
                    p, failure=last_failure,
                    changed_files=current_changed_files,
                    attempt=number, budget_remaining=limit - number)
            except Exception as exc:
                return history_result(
                    BLOCKED, f"repair evidence/context capture failed: {exc}")
            try:
                context = render_repair_context(
                    context_obj, max_chars=int((p.get("budgets") or {}).get("max_repair_chars", 0)))
            except RepairContextTooLarge as exc:
                return history_result(ESCALATE, str(exc))
            repair_of = number - 1
        try:
            guard_failure = package_guard(bound)
            if guard_failure:
                return history_result(BLOCKED, guard_failure)
            execution = execute_worker(bound, context, number,
                                       int((p.get("budgets") or {}).get("output_tokens", 0)))
        except Exception as exc:
            return history_result(BLOCKED, f"worker adapter failed: {exc}")
        last_execution = execution
        ar = execution.receipt
        expected_target = bound.decision.selected_profile
        if not _receipt_matches(ar, p, number,
                                dispatch_receipt.receipt_hash,
                                target_id=expected_target.target_id,
                                host=expected_target.host, model=expected_target.model,
                                repair_of=repair_of):
            return history_result(BLOCKED, "attempt receipt failed identity/scope validation")
        rendered = execution.artifacts.get(ar.rendered_context_ref.storage_uri)
        output = execution.artifacts.get(ar.output_ref.storage_uri)
        if (not _artifact_matches(ar.rendered_context_ref, rendered)
                or not _artifact_matches(ar.output_ref, output)
                or rendered.decode("utf-8", "replace") != context
                or context_projection_digest(context) != ar.context_projection_hash):
            return history_result(BLOCKED, "rendered context/output artifacts are missing or mismatched")
        attempts.append(ar)
        try:
            _collect_artifacts(artifacts, execution.artifacts)
        except Exception as exc:
            return history_result(BLOCKED, str(exc))
        canonical_failure = ar.failure_class or ""
        if execution.failure_kind and canonical_failure != execution.failure_kind:
            return history_result(BLOCKED,
                                  "worker failure class differs from its canonical attempt receipt")
        if canonical_failure in {"infra", "runtime_provider", "policy", "context"}:
            return history_result(BLOCKED,
                                  f"non-repairable worker failure: {canonical_failure}")
        if canonical_failure not in {"", "technical"}:
            return history_result(BLOCKED,
                                  f"unsupported canonical worker failure class: {canonical_failure}")
        try:
            guard_failure = package_guard(bound)
            if guard_failure:
                return history_result(BLOCKED, guard_failure)
            checked = execute_verifier(bound, ar, number)
        except Exception as exc:
            return history_result(BLOCKED, f"verifier adapter failed: {exc}")
        vr = checked.receipt
        if not _verification_matches(vr, p, number):
            return history_result(BLOCKED, "verification receipt failed identity validation")
        verifications.append(vr)
        try:
            _collect_artifacts(artifacts, checked.artifacts)
        except Exception as exc:
            return history_result(BLOCKED, str(exc))
        if vr.outcome == CLAIM_PASS:
            payload, validation = _seal(
                frozen_package, attempts, verifications, dispatches, artifacts,
                package_id=f"{p['run_id']}-worker-loop")
            status = ACCEPTED_CANDIDATE if validation.ok else BLOCKED
            return LoopResult(status, tuple(attempts), tuple(verifications),
                              tuple(dispatches), payload, validation,
                              "" if validation.ok else "canonical evidence validation failed")
        if vr.outcome in (CLAIM_BLOCKED, CLAIM_INCONCLUSIVE):
            return history_result(BLOCKED, f"verification outcome is {vr.outcome}")
        if vr.outcome != CLAIM_FAIL:
            return history_result(BLOCKED, f"unknown verification outcome {vr.outcome!r}")
        stdout = checked.artifacts.get(vr.stdout_ref.storage_uri) if vr.stdout_ref else None
        stderr = checked.artifacts.get(vr.stderr_ref.storage_uri) if vr.stderr_ref else None
        if (not vr.stdout_complete or not vr.stderr_complete or
                not _artifact_matches(vr.stdout_ref, stdout) or
                (vr.stderr_ref is not None and not _artifact_matches(vr.stderr_ref, stderr))):
            return history_result(BLOCKED,
                                  "technical failure evidence is incomplete or artifact hash mismatched")
        text = stdout.decode("utf-8", "replace") if stdout is not None else ""
        last_failure = parse_verification_failure(
            (p.get("verification") or {}).get("command", ""), vr.exit_code,
            text, max_excerpt=max_excerpt)
        if not (last_failure.failing_tests or last_failure.collection_error):
            return history_result(BLOCKED,
                                  "deterministic failure output has no parsed failing test or collection error")
        fp = failure_fingerprint(last_failure)
        if fp in seen_fingerprints:
            return history_result(ESCALATE, "repeated deterministic failure fingerprint")
        seen_fingerprints.add(fp)
    return history_result(ESCALATE,
                          "shared attempt budget exhausted with deterministic failures")
