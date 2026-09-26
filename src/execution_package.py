"""ExecutionPackage and DispatchDecisionReceipt: the immutable intent, hashed
before anything runs so the run can only be compared against it.

* The interface is the packet's own ``InterfaceField`` values, and
  ``interface_digest`` delegates to ``WorkPacket``, so package and worker can't
  disagree about it.
* Dispatch provenance doesn't pretend to be policy: without a wired policy a
  receipt says ``explicit_pin``; claiming ``ps605_policy`` without a policy
  revision is refused.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple

from src.evidence_contract import (
    INDEPENDENCE_EXISTING_AUTHORITATIVE,
    INDEPENDENCE_HARNESS_HIDDEN,
    KIND_DETERMINISTIC_VERIFICATION,
    KIND_NEGATIVE_CONTROL,
    KIND_SCOPE_CHECK,
    KIND_SOURCE_BINDING,
    EvidenceRequirement,
    requirement_index,
)
from src.source_snapshot import SourceSnapshotIdentity, source_snapshot_from_dict
from src.work_packet import WorkPacket, make_work_packet

EXECUTION_PACKAGE_SCHEMA_VERSION = 1
DISPATCH_RECEIPT_SCHEMA_VERSION = 1

#: How the target was actually chosen. Recorded, not inferred.
DECIDED_BY_EXPLICIT_PIN = "explicit_pin"
DECIDED_BY_POLICY = "ps605_policy"
KNOWN_DECIDED_BY = frozenset({DECIDED_BY_EXPLICIT_PIN, DECIDED_BY_POLICY})


class ExecutionPackageError(ValueError):
    """Raised when an execution package cannot be built or is malformed."""


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def _normalize_authority(value: Mapping[str, Any]) -> dict:
    """Canonical form of an optional ``authority`` block. An omitted sub-field and
    one set to ``None`` must hash identically, so both are dropped.
    Normalization only; nothing is required or checked.
    """
    return {k: v for k, v in dict(value).items() if v is not None}


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now() -> str:
    """One timestamp helper, so every receipt stamps time the same way."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Budgets:
    """The envelope the run may spend. Zero means "not declared", not "unlimited"."""

    context_tokens: int = 0
    output_tokens: int = 0
    time_seconds: int = 0
    max_attempts: int = 3
    max_repair_chars: int = 6000

    def __post_init__(self) -> None:
        for name in ("context_tokens", "output_tokens", "time_seconds",
                     "max_attempts", "max_repair_chars"):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise ExecutionPackageError(
                    f"budget {name} must be a non-negative int, got {value!r}")

    def to_dict(self) -> dict:
        return {"context_tokens": self.context_tokens,
                "output_tokens": self.output_tokens,
                "time_seconds": self.time_seconds,
                "max_attempts": self.max_attempts,
                "max_repair_chars": self.max_repair_chars}


@dataclass(frozen=True)
class VerificationPlan:
    """What will judge the work. ``verifier_digests`` for harness-owned or hidden
    verifiers are sealed before dispatch, so a swapped control is detectable
    without the content entering the worker's context."""

    verifier_id: str
    command: str
    verifier_paths: Tuple[str, ...] = ()
    verifier_digests: Tuple[Tuple[str, str], ...] = ()
    positive_control: str = ""
    negative_control: str = ""
    timeout_s: int = 600
    hidden_from_worker: bool = True

    def __post_init__(self) -> None:
        if not str(self.verifier_id or "").strip():
            raise ExecutionPackageError("verifier_id must be non-empty")
        if not str(self.command or "").strip():
            raise ExecutionPackageError("verification command must be non-empty")
        if self.timeout_s <= 0:
            raise ExecutionPackageError("verification timeout_s must be positive")

    def digest_of(self, path: str) -> str:
        for rel, digest in self.verifier_digests:
            if rel == path:
                return digest
        return ""

    def to_dict(self) -> dict:
        return {"verifier_id": self.verifier_id, "command": self.command,
                "verifier_paths": list(self.verifier_paths),
                "verifier_digests": [list(p) for p in self.verifier_digests],
                "positive_control": self.positive_control,
                "negative_control": self.negative_control,
                "timeout_s": self.timeout_s,
                "hidden_from_worker": self.hidden_from_worker}


def seal_verifier_digests(worktree: str, paths: Sequence[str]
                          ) -> Tuple[Tuple[str, str], ...]:
    """Hash verifier artifacts at authoring time, before any model call."""
    from src.source_snapshot import _hash_paths  # same bounded hasher

    pairs, _truncated = _hash_paths(worktree, paths, max_bytes=8 * 1024 * 1024)
    return pairs


@dataclass(frozen=True)
class DispatchDecisionReceipt:
    """Why this target ran this packet, as data.

    Candidate, capability-receipt and policy-revision fields may be empty now so
    a richer policy decision can fill them without a schema change.

    ``authority`` records who asked and on whose credential; it is not
    authorization. Omitted from ``core()`` when ``None`` so older receipts hash
    unchanged; hashed like any field when present.
    """

    receipt_id: str
    execution_package_hash: str
    run_id: str
    packet_id: str
    selected_target_id: str
    selected_host: str
    selected_model: str
    decided_by: str
    reason: str
    requested_role: str = ""
    requested_capabilities: Tuple[str, ...] = ()
    policy_ref: str = ""
    candidates_considered: Tuple[Mapping[str, Any], ...] = ()
    capability_receipt_refs: Tuple[str, ...] = ()
    capacity_receipt_refs: Tuple[str, ...] = ()
    offer_receipt_refs: Tuple[str, ...] = ()
    offer_quote_digests: Tuple[str, ...] = ()
    selected_runtime_kind: str = ""
    selected_runtime_version: str = ""
    selected_model_digest: str = ""
    selected_backend: str = ""
    granted_tools: Tuple[str, ...] = ()
    granted_write_scope: Tuple[str, ...] = ()
    granted_read_scope: Tuple[str, ...] = ()
    network_policy: str = ""
    decided_at: str = ""
    schema_version: int = DISPATCH_RECEIPT_SCHEMA_VERSION
    authority: Optional[Mapping[str, Any]] = None
    receipt_hash: str = field(default="")

    def __post_init__(self) -> None:
        for name in ("receipt_id", "execution_package_hash", "run_id", "packet_id",
                     "selected_target_id", "selected_host", "selected_model",
                     "reason"):
            if not str(getattr(self, name) or "").strip():
                raise ExecutionPackageError(
                    f"dispatch receipt {name} must be non-empty")
        if self.decided_by not in KNOWN_DECIDED_BY:
            raise ExecutionPackageError(
                f"unknown decided_by {self.decided_by!r}; "
                f"known: {sorted(KNOWN_DECIDED_BY)}")
        # A policy claim must name the policy; a pin can't borrow policy authority.
        if self.decided_by == DECIDED_BY_POLICY and not self.policy_ref.strip():
            raise ExecutionPackageError(
                "decided_by=ps605_policy requires a policy_ref: a routing claim "
                "with no policy revision is not a decision record")
        if self.decided_by == DECIDED_BY_EXPLICIT_PIN and self.policy_ref.strip():
            raise ExecutionPackageError(
                "decided_by=explicit_pin must not carry a policy_ref: the pin did "
                "not consult policy, and saying otherwise misattributes the choice")

    def core(self) -> dict:
        payload = {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "execution_package_hash": self.execution_package_hash,
            "run_id": self.run_id,
            "packet_id": self.packet_id,
            "requested_role": self.requested_role,
            "requested_capabilities": list(self.requested_capabilities),
            "policy_ref": self.policy_ref,
            "candidates_considered": [dict(c) for c in self.candidates_considered],
            "capability_receipt_refs": list(self.capability_receipt_refs),
            "selected_target_id": self.selected_target_id,
            "selected_host": self.selected_host,
            "selected_model": self.selected_model,
            "selected_runtime_kind": self.selected_runtime_kind,
            "selected_runtime_version": self.selected_runtime_version,
            "selected_model_digest": self.selected_model_digest,
            "selected_backend": self.selected_backend,
            "granted_tools": list(self.granted_tools),
            "granted_write_scope": list(self.granted_write_scope),
            "granted_read_scope": list(self.granted_read_scope),
            "network_policy": self.network_policy,
            "decided_by": self.decided_by,
            "reason": self.reason,
            "decided_at": self.decided_at,
        }
        # Omitted when absent so older receipts hash unchanged.
        if self.authority is not None:
            payload["authority"] = _normalize_authority(self.authority)
        if self.capacity_receipt_refs:
            payload["capacity_receipt_refs"] = list(self.capacity_receipt_refs)
        if self.offer_receipt_refs:
            payload["offer_receipt_refs"] = list(self.offer_receipt_refs)
        if self.offer_quote_digests:
            payload["offer_quote_digests"] = list(self.offer_quote_digests)
        return payload

    def to_dict(self) -> dict:
        payload = self.core()
        payload["receipt_hash"] = self.receipt_hash
        return payload

    @property
    def identity(self) -> str:
        """Alias for the receipt hash, so callers do not re-derive the name."""
        return self.receipt_hash


def _seal(cls, core: dict, hash_field: str):
    """Build a frozen record and seal a digest over its ``core()`` fields."""
    provisional = cls(**core)
    digest = _sha256_hex(_canonical(provisional.core()))
    return cls(**{**core, hash_field: digest})


def make_dispatch_receipt(**kwargs: Any) -> DispatchDecisionReceipt:
    """Build a dispatch receipt, stamping ``decided_at`` when it was not given."""
    known = {f.name for f in fields(DispatchDecisionReceipt)}
    unknown = set(kwargs) - known
    if unknown:
        raise ExecutionPackageError(
            f"unknown dispatch receipt field(s): {sorted(unknown)}")
    payload = dict(kwargs)
    payload.pop("receipt_hash", None)
    if not payload.get("decided_at"):
        payload["decided_at"] = utc_now()
    for name in ("requested_capabilities", "candidates_considered",
                 "capability_receipt_refs", "capacity_receipt_refs", "offer_receipt_refs", "offer_quote_digests", "granted_tools",
                 "granted_write_scope", "granted_read_scope"):
        if name in payload and payload[name] is not None:
            payload[name] = tuple(payload[name])
    return _seal(DispatchDecisionReceipt, payload, "receipt_hash")


REQ_DETERMINISTIC_VERIFICATION = "deterministic_verification"
REQ_NEGATIVE_CONTROL = "negative_control"
REQ_SCOPE_CHECK = "scope_check"
REQ_SOURCE_BINDING = "source_binding"


def default_requirements(verification: VerificationPlan, *,
                         negative_control: str = ""
                         ) -> Tuple[EvidenceRequirement, ...]:
    """The default requirement set for packets that declare none, so an empty
    set can't pass trivially. Each is closable by a receipt the harness produces."""
    requirements = [
        EvidenceRequirement(
            requirement_id=REQ_DETERMINISTIC_VERIFICATION,
            kind=KIND_DETERMINISTIC_VERIFICATION,
            vantage="harness worktree",
            expected_verifier=verification.verifier_id,
            independence=INDEPENDENCE_HARNESS_HIDDEN,
        ),
        EvidenceRequirement(
            requirement_id=REQ_SOURCE_BINDING,
            kind=KIND_SOURCE_BINDING,
            vantage="harness worktree",
            expected_verifier="src.source_snapshot.take_source_snapshot",
            independence=INDEPENDENCE_EXISTING_AUTHORITATIVE,
        ),
    ]
    if negative_control.strip():
        requirements.append(EvidenceRequirement(
            requirement_id=REQ_NEGATIVE_CONTROL,
            kind=KIND_NEGATIVE_CONTROL,
            vantage="harness worktree",
            expected_verifier=f"{verification.verifier_id} (negative control)",
            independence=INDEPENDENCE_HARNESS_HIDDEN,
        ))
    if verification.verifier_paths:
        requirements.append(EvidenceRequirement(
            requirement_id=REQ_SCOPE_CHECK,
            kind=KIND_SCOPE_CHECK,
            vantage="harness worktree",
            expected_verifier="actual write set vs authorized write scope",
            independence=INDEPENDENCE_EXISTING_AUTHORITATIVE,
        ))
    return tuple(requirements)


@dataclass(frozen=True)
class ExecutionPackage:
    """Immutable intent. ``package_hash`` covers every substantive field,
    including the interface and verifier digests, so later edits are detectable."""

    package_id: str
    run_id: str
    packet_id: str
    source: SourceSnapshotIdentity
    objective: str
    verification: VerificationPlan
    interface: Tuple[Any, ...] = ()
    contract: str = ""
    acceptance_criteria: Tuple[str, ...] = ()
    write_scope: Tuple[str, ...] = ()
    read_scope: Tuple[str, ...] = ()
    required_capabilities: Tuple[str, ...] = ()
    execution_role: str = ""
    allowed_tools: Tuple[str, ...] = ()
    network_policy: str = ""
    evidence_requirements: Tuple[EvidenceRequirement, ...] = ()
    negative_control: str = ""
    stop_conditions: Tuple[str, ...] = ()
    budgets: Budgets = field(default_factory=Budgets)
    jira_key: str = ""
    parent_ids: Tuple[str, ...] = ()
    schema_version: int = EXECUTION_PACKAGE_SCHEMA_VERSION
    package_hash: str = field(default="")

    def __post_init__(self) -> None:
        for name in ("package_id", "run_id", "packet_id", "objective"):
            if not str(getattr(self, name) or "").strip():
                raise ExecutionPackageError(f"{name} must be non-empty")
        if not isinstance(self.source, SourceSnapshotIdentity):
            raise ExecutionPackageError(
                "source must be a SourceSnapshotIdentity; a package without a "
                "measured source identity is not evidence")
        if not isinstance(self.verification, VerificationPlan):
            raise ExecutionPackageError(
                "verification must be a VerificationPlan: a package with no "
                "deterministic verifier cannot be judged")
        requirement_index(self.evidence_requirements)

    @property
    def is_writable(self) -> bool:
        """A package that may write must own a scope AND declare its interface."""
        return bool(self.write_scope)

    @property
    def interface_digest(self) -> str:
        """Delegates to the WorkPacket primitive's single definition."""
        from src.work_packet import interface_digest_of

        return interface_digest_of(self.interface)

    @property
    def normalized_interface(self) -> Tuple[str, ...]:
        from src.work_packet import coerce_interface

        return tuple(f.normalized() for f in coerce_interface(self.interface))

    def requirement(self, requirement_id: str) -> Optional[EvidenceRequirement]:
        for req in self.evidence_requirements:
            if req.requirement_id == requirement_id:
                return req
        return None

    def core(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "package_id": self.package_id,
            "run_id": self.run_id,
            "packet_id": self.packet_id,
            "jira_key": self.jira_key,
            "parent_ids": list(self.parent_ids),
            "source": self.source.to_dict(),
            "objective": self.objective,
            "contract": self.contract,
            "interface": list(self.normalized_interface),
            "interface_digest": self.interface_digest,
            "acceptance_criteria": list(self.acceptance_criteria),
            "write_scope": list(self.write_scope),
            "read_scope": list(self.read_scope),
            "required_capabilities": list(self.required_capabilities),
            "execution_role": self.execution_role,
            "allowed_tools": list(self.allowed_tools),
            "network_policy": self.network_policy,
            "negative_control": self.negative_control,
            "stop_conditions": list(self.stop_conditions),
            "budgets": self.budgets.to_dict(),
            "verification": self.verification.to_dict(),
            "evidence_requirements": [r.to_dict()
                                      for r in self.evidence_requirements],
        }

    def to_dict(self) -> dict:
        payload = self.core()
        payload["package_hash"] = self.package_hash
        return payload


def compute_package_hash(core: Mapping[str, Any]) -> str:
    """Digest of a package's core payload. The definition of "the package"."""
    return _sha256_hex(_canonical(dict(core)))


def package_hash_is_valid(payload: Mapping[str, Any]) -> bool:
    """True when a serialized package's hash matches its own fields. Re-hashes
    the serialized form, since the normalized interface strings can't round-trip
    through the constructor."""
    if not isinstance(payload, Mapping) or not payload.get("package_hash"):
        return False
    core = {k: v for k, v in payload.items() if k != "package_hash"}
    return compute_package_hash(core) == payload["package_hash"]


def build_execution_package(
    packet: Mapping[str, Any],
    *,
    source: SourceSnapshotIdentity,
    verification: VerificationPlan,
    run_id: str,
    package_id: str = "",
    jira_key: str = "",
    execution_role: str = "",
    required_capabilities: Sequence[str] = (),
    allowed_tools: Sequence[str] = (),
    network_policy: str = "",
    evidence_requirements: Sequence[EvidenceRequirement] = (),
    budgets: Optional[Budgets] = None,
    read_scope: Optional[Sequence[str]] = None,
    write_scope: Optional[Sequence[str]] = None,
    stop_conditions: Optional[Sequence[str]] = None,
    parent_ids: Sequence[str] = (),
) -> ExecutionPackage:
    """Validate a packet via ``make_work_packet`` and seal it.

    A writable packet without a declared interface is refused before dispatch.
    Manager-only keys (``role``, ``base_sha``) are filtered, not rejected.
    ``write_scope`` may be narrowed, never widened: the packet owns authorization.
    """
    if not str(run_id or "").strip():
        raise ExecutionPackageError("run_id is required to seal a package")
    if not isinstance(source, SourceSnapshotIdentity):
        raise ExecutionPackageError(
            "source must be a measured SourceSnapshotIdentity")
    if not isinstance(verification, VerificationPlan):
        raise ExecutionPackageError("verification must be a VerificationPlan")

    known = {f.name for f in fields(WorkPacket)}
    subset = {k: v for k, v in dict(packet).items() if k in known}
    try:
        validated = make_work_packet(**subset)
    except ValueError as exc:
        raise ExecutionPackageError(f"packet is not packageable: {exc}") from exc

    authorized = tuple(validated.write_scope)
    if write_scope is None:
        effective_write = authorized
    else:
        requested = tuple(str(p) for p in write_scope)
        extra = [p for p in requested if p not in authorized]
        if extra:
            raise ExecutionPackageError(
                f"package write scope widens the packet's authorization: {extra}")
        effective_write = requested

    requirements = tuple(evidence_requirements) if evidence_requirements else \
        default_requirements(verification, negative_control=validated.negative_control)

    core = {
        "schema_version": EXECUTION_PACKAGE_SCHEMA_VERSION,
        "package_id": package_id or f"{validated.packet_id}:{run_id}",
        "run_id": run_id,
        "packet_id": validated.packet_id,
        "jira_key": jira_key,
        "parent_ids": list(parent_ids),
        "source": source,
        "objective": validated.objective,
        "contract": validated.contract,
        "interface": tuple(validated.interface),
        "acceptance_criteria": tuple(validated.acceptance_criteria),
        "write_scope": effective_write,
        "read_scope": tuple(read_scope if read_scope is not None
                            else validated.read_scope),
        "required_capabilities": tuple(required_capabilities),
        "execution_role": execution_role or str(packet.get("role", "") or ""),
        "allowed_tools": tuple(allowed_tools),
        "network_policy": network_policy,
        "evidence_requirements": requirements,
        "negative_control": validated.negative_control,
        "stop_conditions": tuple(stop_conditions if stop_conditions is not None
                                 else validated.stop_conditions),
        "budgets": budgets if budgets is not None else Budgets(),
        "verification": verification,
    }
    return _seal(ExecutionPackage, core, "package_hash")
