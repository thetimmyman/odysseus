"""Deterministic execution-target selection.

One pure function of canonical inputs yields a selected target or a typed
refusal, recorded as a `DispatchDecision` whose receipt form is
`DispatchDecisionReceipt`.

    ExecutionPackage -> requested domain/role/capabilities
        -> deterministic policy (src.routing_domain_policy: privacy/allow/deny)
        -> fresh, qualified ExecutionTarget candidates (profile + capability receipt)
        -> privacy / permission / network / budget / exactness filters
        -> deterministically SELECTED target
        -> immutable DispatchDecision (+ receipt kwargs)
        -> pinned attempt identity

Structural properties:

1. Nothing here dispatches: :func:`select_target` does no I/O and returns a
   frozen record, so an adapter is handed its identity and can't reroute.
2. One filter path: fallback is a recorded preference rank, never an escape
   from policy. No survivor means a typed refusal (a privacy refusal for
   local-only work).
3. Capability comes from a fresh receipt for the exact profile, not a host
   name, so a runtime/model/backend change invalidates routing. Approximate
   profiles can't satisfy exact intent; deterministic-only nodes never serve
   inference.

Selection only: retries, accounting, ledger, evidence, verification and
landing live elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

#: Profile-level capabilities, measured per (runtime, backend, model, profile)
#: and carried in a capability receipt.
CAP_TEXT_GENERATION = "text_generation"
CAP_REASONING_FRAMING = "reasoning_framing"
CAP_SINGLE_TOOL_CALL = "single_tool_call"
CAP_PARALLEL_TOOL_CALLS = "parallel_tool_calls"
CAP_STREAMED_TOOL_CALLS = "streamed_tool_call_integrity"
CAP_NO_TOOL_DECLINE = "no_tool_decline"
CAP_MULTI_ROUND = "multi_round_tool_continuation"
CAP_CONTEXT_INTEGRITY = "context_integrity"
CAP_CANCELLATION = "cancellation"
#: Same string the capability registry uses, so packet, receipt and profile all
#: say "streaming".
CAP_STREAMING = "streaming"
CAP_EXACT_REFERENCE_SEMANTICS = "exact_reference_semantics"
CAP_DETERMINISTIC_VERIFICATION = "deterministic_verification"
#: Catalog role names, shared so both layers describe the estate in one vocabulary.
CAP_INTEGRATION_STRONG = "integration_strong"
CAP_IMPLEMENTATION_FAST = "implementation_fast"
CAP_BULK_LOCAL = "bulk_local"

KNOWN_CAPABILITIES: FrozenSet[str] = frozenset({
    CAP_TEXT_GENERATION, CAP_REASONING_FRAMING, CAP_SINGLE_TOOL_CALL,
    CAP_PARALLEL_TOOL_CALLS, CAP_STREAMED_TOOL_CALLS, CAP_NO_TOOL_DECLINE,
    CAP_MULTI_ROUND, CAP_CONTEXT_INTEGRITY, CAP_CANCELLATION, CAP_STREAMING,
    CAP_EXACT_REFERENCE_SEMANTICS, CAP_DETERMINISTIC_VERIFICATION,
    CAP_INTEGRATION_STRONG, CAP_IMPLEMENTATION_FAST, CAP_BULK_LOCAL,
})

#: Capabilities only an inference target can carry; requiring one makes a
#: deterministic-only node ineligible, not merely less preferred.
INFERENCE_CAPABILITIES: FrozenSet[str] = frozenset({
    CAP_TEXT_GENERATION, CAP_REASONING_FRAMING, CAP_SINGLE_TOOL_CALL,
    CAP_PARALLEL_TOOL_CALLS, CAP_STREAMED_TOOL_CALLS, CAP_NO_TOOL_DECLINE,
    CAP_MULTI_ROUND, CAP_EXACT_REFERENCE_SEMANTICS, CAP_INTEGRATION_STRONG,
    CAP_IMPLEMENTATION_FAST, CAP_BULK_LOCAL,
})

ROLE_IMPLEMENTER = "local_implementer"
ROLE_REPAIR = "local_repair"
ROLE_VERIFIER = "deterministic_verifier"
ROLE_GOVERNANCE_CI = "governance_ci"
ROLE_PLANNER = "planner"
ROLE_SCOUT = "read_only_analyst"
#: The routing harness's role names, shared so roles never drift from the
#: capability sets they imply.
ROLE_REVIEWER = "reviewer"
ROLE_DEBUGGER = "debugger"
ROLE_ESCALATION = "escalation"
#: Harness spellings of the two roles that differ from the canonical names.
ROLE_IMPLEMENTER_ROUTER = "implementer"
ROLE_SCOUT_ROUTER = "scout"

#: A role's capabilities when the request names none; shorthand, never a substitute.
ROLE_CAPABILITIES: Mapping[str, Tuple[str, ...]] = {
    ROLE_IMPLEMENTER: (CAP_TEXT_GENERATION, CAP_SINGLE_TOOL_CALL,
                       CAP_EXACT_REFERENCE_SEMANTICS),
    ROLE_REPAIR: (CAP_TEXT_GENERATION, CAP_SINGLE_TOOL_CALL,
                  CAP_EXACT_REFERENCE_SEMANTICS),
    ROLE_DEBUGGER: (CAP_TEXT_GENERATION, CAP_EXACT_REFERENCE_SEMANTICS),
    ROLE_VERIFIER: (CAP_DETERMINISTIC_VERIFICATION,),
    ROLE_GOVERNANCE_CI: (CAP_DETERMINISTIC_VERIFICATION,),
    ROLE_REVIEWER: (CAP_TEXT_GENERATION, CAP_EXACT_REFERENCE_SEMANTICS),
    ROLE_PLANNER: (CAP_TEXT_GENERATION, CAP_REASONING_FRAMING,
                   CAP_EXACT_REFERENCE_SEMANTICS),
    ROLE_SCOUT: (CAP_TEXT_GENERATION, CAP_EXACT_REFERENCE_SEMANTICS),
    ROLE_ESCALATION: (CAP_TEXT_GENERATION, CAP_REASONING_FRAMING,
                      CAP_EXACT_REFERENCE_SEMANTICS),
    ROLE_IMPLEMENTER_ROUTER: (CAP_TEXT_GENERATION, CAP_SINGLE_TOOL_CALL,
                              CAP_EXACT_REFERENCE_SEMANTICS),
    ROLE_SCOUT_ROUTER: (CAP_TEXT_GENERATION, CAP_EXACT_REFERENCE_SEMANTICS),
}

LOCALITY_LOCAL = "local"
LOCALITY_HOSTED = "hosted"

EXACTNESS_EXACT = "exact_reference_intent"
EXACTNESS_APPROXIMATE = "approximate"

KNOWN_EXACTNESS = frozenset({EXACTNESS_EXACT, EXACTNESS_APPROXIMATE})

#: Typed refusal reason codes, so callers and receipts never parse prose.
REFUSED_NO_CANDIDATES = "no_candidates_supplied"
REFUSED_NO_ELIGIBLE_TARGET = "no_eligible_target"
REFUSED_PRIVACY_LOCAL_ONLY = "privacy_local_only_no_eligible_target"
REFUSED_POLICY_DENIED = "policy_denied"
REFUSED_RECEIPT_MISSING = "capability_receipt_missing"
REFUSED_RECEIPT_MISMATCH = "capability_receipt_profile_mismatch"
REFUSED_RECEIPT_STALE = "capability_receipt_stale"
REFUSED_RECEIPT_UNHEALTHY = "capability_receipt_unhealthy"
REFUSED_CAPABILITY_MISSING = "capability_missing"
REFUSED_EXACTNESS = "exactness_unsatisfied"
REFUSED_NOT_INFERENCE_TARGET = "not_an_inference_target"
REFUSED_ROLE = "role_not_supported"
REFUSED_TOOL = "tool_not_granted"
REFUSED_NETWORK = "network_policy_unsatisfied"
REFUSED_BUDGET = "budget_class_exceeded"
REFUSED_RESOURCE = "resource_unavailable"
REFUSED_UNKNOWN_CAPABILITY = "unknown_capability"
REFUSED_CAPACITY_MISSING = "capacity_receipt_missing"
REFUSED_CAPACITY_STALE = "capacity_receipt_stale"
REFUSED_CAPACITY_UNUSABLE = "capacity_receipt_unusable"

KNOWN_REFUSALS: FrozenSet[str] = frozenset({
    REFUSED_NO_CANDIDATES, REFUSED_NO_ELIGIBLE_TARGET, REFUSED_PRIVACY_LOCAL_ONLY,
    REFUSED_POLICY_DENIED, REFUSED_RECEIPT_MISSING, REFUSED_RECEIPT_MISMATCH,
    REFUSED_RECEIPT_STALE, REFUSED_RECEIPT_UNHEALTHY, REFUSED_CAPABILITY_MISSING,
    REFUSED_EXACTNESS, REFUSED_NOT_INFERENCE_TARGET, REFUSED_ROLE, REFUSED_TOOL,
    REFUSED_NETWORK, REFUSED_BUDGET, REFUSED_RESOURCE, REFUSED_UNKNOWN_CAPABILITY,
    REFUSED_CAPACITY_MISSING, REFUSED_CAPACITY_STALE, REFUSED_CAPACITY_UNUSABLE,
})

#: How the selected candidate was reached, making fallback auditable.
REASON_SELECTED_PREFERRED = "selected_preferred_candidate"
REASON_SELECTED_FALLBACK = "selected_fallback_candidate"
REASON_SELECTED_LOCAL_ONLY = "selected_local_only_candidate"
REASON_SELECTED_DETERMINISTIC = "selected_deterministic_candidate"

#: The fallback rule, recorded on every decision.
#: Hash-stable: this exact string is embedded in hashed receipts, so never
#: reword it for new rules (e.g. the capacity gate) or every historical hash
#: changes. Document additions in _assess's numbered comments instead.
FALLBACK_RULE = (
    "every candidate is filtered by the same ordered rules (domain privacy, "
    "locality, inference capability, role, receipt presence/match/freshness/"
    "health, measured capabilities, exactness, tools, network, budget, "
    "resources); the first eligible candidate in deterministic preference order "
    "is selected; a fallback is therefore a LOWER PREFERENCE RANK, never a "
    "weaker standard"
)


def _canonical(payload: object) -> bytes:
    """Canonical JSON form, byte-for-byte, so receipt hashes agree.

    ``ensure_ascii=False`` matters: the receipt layer uses it too, and escaping
    would change the hash. A contract test asserts equality.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_ts(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class DispatchRoutingError(ValueError):
    """Raised when a request/profile/receipt is malformed (fail closed)."""


class RoutingRefused(Exception):
    """Typed, fail-closed refusal raised instead of a substitute target. Carries
    the per-candidate assessments so every rejection has a code and reason."""

    def __init__(self, code: str, reason: str, *,
                 assessments: Sequence["CandidateAssessment"] = (),
                 run_id: str = "", packet_id: str = ""):
        if code not in KNOWN_REFUSALS:
            raise DispatchRoutingError(f"unknown refusal code {code!r}")
        self.code = code
        self.reason = reason
        self.assessments = tuple(assessments)
        self.run_id = run_id
        self.packet_id = packet_id
        super().__init__(f"{code}: {reason}")

    def to_dict(self) -> dict:
        return {
            "refused": True,
            "code": self.code,
            "reason": self.reason,
            "run_id": self.run_id,
            "packet_id": self.packet_id,
            "candidates": [a.to_dict() for a in self.assessments],
        }


#: `DispatchDecisionReceipt` constructor fields and the fields its ``core()``
#: seals (DISPATCH_RECEIPT_SCHEMA_VERSION = 1). Frozen here as a contract,
#: asserted by a test, so emitted receipts construct unchanged.
PS638_RECEIPT_SCHEMA_VERSION_EXPECTED = 1
PS638_RECEIPT_FIELDS: Tuple[str, ...] = (
    "receipt_id", "execution_package_hash", "run_id", "packet_id",
    "selected_target_id", "selected_host", "selected_model", "decided_by",
    "reason", "requested_role", "requested_capabilities", "policy_ref",
    "candidates_considered", "capability_receipt_refs", "selected_runtime_kind",
    "selected_runtime_version", "selected_model_digest", "selected_backend",
    "granted_tools", "granted_write_scope", "granted_read_scope", "network_policy",
    "decided_at", "authority", "capacity_receipt_refs", "offer_receipt_refs", "offer_quote_digests", "schema_version",
)
#: Fields ``core()`` covers, in its order; the receipt hash is sha256 over their
#: canonical JSON with tuples as lists. ``authority`` is omitted entirely when
#: absent so older receipts hash unchanged; it is recorded, not enforced.
PS638_RECEIPT_CORE_FIELDS: Tuple[str, ...] = (
    "schema_version", "receipt_id", "execution_package_hash", "run_id",
    "packet_id", "requested_role", "requested_capabilities", "policy_ref",
    "candidates_considered", "capability_receipt_refs", "selected_target_id",
    "selected_host", "selected_model", "selected_runtime_kind",
    "selected_runtime_version", "selected_model_digest", "selected_backend",
    "granted_tools", "granted_write_scope", "granted_read_scope",
    "network_policy", "decided_by", "reason", "decided_at", "authority",
    "capacity_receipt_refs", "offer_receipt_refs", "offer_quote_digests",
)
_PS638_LIST_FIELDS: Tuple[str, ...] = (
    "requested_capabilities", "candidates_considered", "capability_receipt_refs",
    "capacity_receipt_refs", "offer_receipt_refs", "offer_quote_digests", "granted_tools", "granted_write_scope", "granted_read_scope",
)
#: Like ``authority``: omitted from the hashed core when absent (never ``null``),
#: so adding them never changes older receipt hashes.
_PS638_OPTIONAL_OMIT_WHEN_ABSENT_FIELDS: Tuple[str, ...] = (
    "authority", "capacity_receipt_refs", "offer_receipt_refs", "offer_quote_digests",
)
DECIDED_BY_POLICY = "ps605_policy"


def _normalize_authority(value: Mapping[str, Any]) -> dict:
    """Canonical form of an optional ``authority`` block. An omitted sub-field
    and one set to ``None`` must hash identically, so both are dropped."""
    return {k: v for k, v in dict(value).items() if v is not None}


def ps638_receipt_core(kwargs: Mapping[str, Any]) -> dict:
    """The exact dict hashed for a dispatch receipt; the single implementation,
    so the published hash can't drift from the verified one."""
    core: dict = {}
    for name in PS638_RECEIPT_CORE_FIELDS:
        value = kwargs.get(name)
        if name in _PS638_OPTIONAL_OMIT_WHEN_ABSENT_FIELDS:
            if value is not None:
                if name == "authority":
                    core[name] = _normalize_authority(value)
                elif name in _PS638_LIST_FIELDS:
                    core[name] = [dict(v) if isinstance(v, Mapping) else v
                                  for v in (value or ())]
                else:
                    core[name] = value
            continue
        if name in _PS638_LIST_FIELDS:
            core[name] = [dict(v) if isinstance(v, Mapping) else v
                          for v in (value or ())]
        else:
            core[name] = value
    return core


def ps638_receipt_hash(kwargs: Mapping[str, Any]) -> str:
    """sha256 over the receipt core, exactly as the receipt layer computes it."""
    return _sha256_hex(_canonical(ps638_receipt_core(kwargs)))


#: How a receipt's capabilities became known: "declared" (the row's own claim),
#: "detected" (probed on the endpoint) or "measured" (per-profile measurement).
PROVENANCE_MEASURED = "measured"
PROVENANCE_DETECTED = "detected"
PROVENANCE_DECLARED = "declared"
KNOWN_PROVENANCE = frozenset({PROVENANCE_MEASURED, PROVENANCE_DETECTED,
                              PROVENANCE_DECLARED})


@dataclass(frozen=True)
class LegacyCapabilityView:
    """Non-authoritative routing view of canonical capability evidence: the
    profile measured, what, when, how long valid and current health. Routing on
    a receipt, not a host name, keeps "node up" from meaning "capable"."""

    receipt_id: str
    profile_id: str
    target_id: str
    capabilities: FrozenSet[str] = frozenset()
    exactness: str = EXACTNESS_EXACT
    observed_at: str = ""
    ttl_s: int = 0
    healthy: bool = True
    runtime_version: str = ""
    model_digest: str = ""
    host: str = ""
    notes: str = ""
    provenance: str = PROVENANCE_DECLARED
    #: Hash of the persisted capability receipt this came from, when there is
    #: one, so readers can re-derive the evidence instead of trusting a summary.
    source_receipt_hash: str = ""
    schema_version: int = SCHEMA_VERSION
    receipt_hash: str = field(default="")

    def __post_init__(self) -> None:
        for name in ("receipt_id", "profile_id", "target_id", "observed_at"):
            if not str(getattr(self, name) or "").strip():
                raise DispatchRoutingError(
                    f"capability receipt {name} must be non-empty")
        if self.exactness not in KNOWN_EXACTNESS:
            raise DispatchRoutingError(
                f"unknown exactness {self.exactness!r}; known: "
                f"{sorted(KNOWN_EXACTNESS)}")
        if self.provenance not in KNOWN_PROVENANCE:
            raise DispatchRoutingError(
                f"unknown receipt provenance {self.provenance!r}; known: "
                f"{sorted(KNOWN_PROVENANCE)}")
        if not isinstance(self.ttl_s, int) or self.ttl_s < 0:
            raise DispatchRoutingError("ttl_s must be a non-negative int")
        if _parse_ts(self.observed_at) is None:
            raise DispatchRoutingError(
                f"observed_at must be an ISO timestamp, got {self.observed_at!r}")
        unknown = set(self.capabilities) - KNOWN_CAPABILITIES
        if unknown:
            raise DispatchRoutingError(
                f"unknown capability name(s) in receipt: {sorted(unknown)}")

    def freshness_s(self, now: Optional[datetime] = None) -> Optional[int]:
        """Seconds since measurement, or None when the timestamp is unreadable."""
        observed = _parse_ts(self.observed_at)
        if observed is None:
            return None
        current = now or datetime.now(timezone.utc)
        return int((current - observed).total_seconds())

    def is_fresh(self, now: Optional[datetime] = None) -> bool:
        """Fresh = readable timestamp and inside its TTL. Unknown never widens
        eligibility."""
        age = self.freshness_s(now)
        return age is not None and 0 <= age <= int(self.ttl_s)

    def core(self) -> dict:
        return {
            "schema_version": self.schema_version, "receipt_id": self.receipt_id,
            "profile_id": self.profile_id, "target_id": self.target_id,
            "capabilities": sorted(self.capabilities), "exactness": self.exactness,
            "observed_at": self.observed_at, "ttl_s": self.ttl_s,
            "healthy": self.healthy, "runtime_version": self.runtime_version,
            "model_digest": self.model_digest, "host": self.host, "notes": self.notes,
            "provenance": self.provenance,
            "source_receipt_hash": self.source_receipt_hash,
        }

    def to_dict(self) -> dict:
        payload = self.core()
        payload["receipt_hash"] = self.receipt_hash
        return payload


def make_legacy_capability_view(**kwargs: Any) -> LegacyCapabilityView:
    """Build a selector view; this is never a qualification or store authority."""
    from dataclasses import fields as _fields

    known = {f.name for f in _fields(LegacyCapabilityView)}
    unknown = set(kwargs) - known
    if unknown:
        raise DispatchRoutingError(
            f"unknown capability receipt field(s): {sorted(unknown)}")
    payload = dict(kwargs)
    payload.pop("receipt_hash", None)
    payload["capabilities"] = frozenset(payload.get("capabilities") or ())
    provisional = LegacyCapabilityView(**payload)
    return LegacyCapabilityView(receipt_hash=_sha256_hex(_canonical(provisional.core())),
                             **payload)


@dataclass(frozen=True)
class ExecutionTargetProfile:
    """One exact execution profile: runtime + backend + model + role identity.

    Per profile, not per host: ``host`` is evidence only, so a runtime/model/
    backend change on the same box is a different profile needing its own
    receipt. ``inference=False`` marks a deterministic-only node;
    ``exactness`` marks an approximation that can't satisfy exact intent.
    """

    target_id: str
    profile_id: str
    provider: str
    host: str = ""
    runtime_kind: str = ""
    runtime_version: str = ""
    runtime_commit: str = ""
    runtime_image_digest: str = ""
    model: str = ""
    model_digest: str = ""
    backend: str = ""
    backend_version: str = ""
    locality: str = LOCALITY_LOCAL
    exactness: str = EXACTNESS_EXACT
    roles: FrozenSet[str] = frozenset()
    tools: FrozenSet[str] = frozenset()
    network_policy: str = ""
    budget_class: str = "standard"
    cost_rank: int = 0
    inference: bool = True
    endpoint_url: str = ""
    endpoint_type: str = ""
    credential_sha256: str = ""
    runtime_options: Mapping[str, Any] = field(default_factory=dict)
    #: Request-scoped generation settings; not target authority.
    execution_options: Mapping[str, Any] = field(default_factory=dict)
    configured_context: int = 0
    configured_served_context: int = 0
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("target_id", "profile_id", "provider"):
            if not str(getattr(self, name) or "").strip():
                raise DispatchRoutingError(
                    f"target profile {name} must be non-empty")
        if self.locality not in (LOCALITY_LOCAL, LOCALITY_HOSTED):
            raise DispatchRoutingError(
                f"unknown locality {self.locality!r}; known: "
                f"{[LOCALITY_LOCAL, LOCALITY_HOSTED]}")
        if self.exactness not in KNOWN_EXACTNESS:
            raise DispatchRoutingError(
                f"unknown exactness {self.exactness!r}; known: "
                f"{sorted(KNOWN_EXACTNESS)}")
        if not isinstance(self.cost_rank, int) or self.cost_rank < 0:
            raise DispatchRoutingError("cost_rank must be a non-negative int")
        unknown_roles = set(self.roles) - set(ROLE_CAPABILITIES)
        if unknown_roles:
            raise DispatchRoutingError(
                f"unknown role(s) on profile: {sorted(unknown_roles)}")

    @property
    def is_local(self) -> bool:
        return self.locality == LOCALITY_LOCAL

    def supports_role(self, role: str) -> bool:
        """A profile supports a role only if it declares it; no roles are inferred."""
        return bool(role) and role in self.roles

    def core(self) -> dict:
        value = {
            "schema_version": self.schema_version, "target_id": self.target_id,
            "profile_id": self.profile_id, "provider": self.provider,
            "host": self.host, "runtime_kind": self.runtime_kind,
            "runtime_version": self.runtime_version, "runtime_commit": self.runtime_commit,
            "runtime_image_digest": self.runtime_image_digest, "model": self.model,
            "model_digest": self.model_digest, "backend": self.backend,
            "backend_version": self.backend_version,
            "locality": self.locality, "exactness": self.exactness,
            "roles": sorted(self.roles), "tools": sorted(self.tools),
            "network_policy": self.network_policy,
            "budget_class": self.budget_class, "cost_rank": self.cost_rank,
            "inference": self.inference, "endpoint_url": self.endpoint_url,
            "endpoint_type": self.endpoint_type,
            "runtime_options": dict(self.runtime_options),
            "configured_context": self.configured_context,
            "configured_served_context": self.configured_served_context,
        }

        # Keep historical local evidence hashes stable when this scope is absent.
        if self.credential_sha256:
            value["credential_sha256"] = self.credential_sha256
        return value

    def to_dict(self) -> dict:
        return self.core()


def make_target_profile(**kwargs: Any) -> ExecutionTargetProfile:
    from dataclasses import fields as _fields

    known = {f.name for f in _fields(ExecutionTargetProfile)}
    unknown = set(kwargs) - known
    if unknown:
        raise DispatchRoutingError(
            f"unknown target profile field(s): {sorted(unknown)}")
    payload = dict(kwargs)
    payload["roles"] = frozenset(payload.get("roles") or ())
    payload["tools"] = frozenset(payload.get("tools") or ())
    return ExecutionTargetProfile(**payload)


@dataclass(frozen=True)
class RoutingRequest:
    """What a packet needs, in terms routing may decide on, built from an
    `ExecutionPackage` so a decision traces back to the intent it served."""

    domain: str
    role: str
    run_id: str = ""
    packet_id: str = ""
    execution_package_hash: str = ""
    capabilities: Tuple[str, ...] = ()
    exactness: str = EXACTNESS_EXACT
    sensitivity: str = "internal"
    local_only: bool = False
    required_tools: Tuple[str, ...] = ()
    network_policy: str = ""
    budget_class: str = ""
    max_cost_rank: int = 2
    preferred_profile_ids: Tuple[str, ...] = ()
    write_scope: Tuple[str, ...] = ()
    read_scope: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.domain or "").strip():
            raise DispatchRoutingError("routing request domain must be non-empty")
        if not str(self.role or "").strip():
            raise DispatchRoutingError("routing request role must be non-empty")
        if self.role not in ROLE_CAPABILITIES:
            raise DispatchRoutingError(
                f"unknown role {self.role!r}; known: {sorted(ROLE_CAPABILITIES)}")
        if self.exactness not in KNOWN_EXACTNESS:
            raise DispatchRoutingError(
                f"unknown exactness {self.exactness!r}; known: "
                f"{sorted(KNOWN_EXACTNESS)}")
        unknown = set(self.capabilities) - KNOWN_CAPABILITIES
        if unknown:
            raise DispatchRoutingError(
                f"unknown capability name(s) in request: {sorted(unknown)}")

    def required_capabilities(self) -> Tuple[str, ...]:
        """Explicit capabilities unioned with the role's shorthand; a role can
        only add requirements."""
        merged = list(ROLE_CAPABILITIES.get(self.role, ()))
        for cap in self.capabilities:
            if cap not in merged:
                merged.append(cap)
        return tuple(merged)

    def needs_inference(self) -> bool:
        return any(cap in INFERENCE_CAPABILITIES
                   for cap in self.required_capabilities())

    def core(self) -> dict:
        return {
            "run_id": self.run_id, "packet_id": self.packet_id,
            "execution_package_hash": self.execution_package_hash,
            "domain": self.domain, "role": self.role,
            "capabilities": list(self.required_capabilities()),
            "exactness": self.exactness, "sensitivity": self.sensitivity,
            "local_only": self.local_only,
            "required_tools": list(self.required_tools),
            "network_policy": self.network_policy,
            "budget_class": self.budget_class, "max_cost_rank": self.max_cost_rank,
            "preferred_profile_ids": list(self.preferred_profile_ids),
            "write_scope": list(self.write_scope),
            "read_scope": list(self.read_scope),
        }

    def to_dict(self) -> dict:
        return self.core()


@dataclass(frozen=True)
class PolicySnapshot:
    """The policy a decision was made under, by content: version, source and
    content hash (versions alone can collide), joined in ``policy_ref``."""

    version: str
    policy_hash: str
    source: str = ""
    schema_version: int = SCHEMA_VERSION

    @property
    def policy_ref(self) -> str:
        return f"routing_policy@{self.version}+sha256:{self.policy_hash[:16]}"

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version, "version": self.version,
                "policy_hash": self.policy_hash, "source": self.source,
                "policy_ref": self.policy_ref}


def policy_snapshot(*, policy: Optional[Mapping[str, Any]] = None) -> PolicySnapshot:
    """Snapshot the routing policy (supplied, or loaded via load_policy). The hash
    covers the loaded content, so an edited policy gets a new policy_ref."""
    source = "src.routing_policy.load_policy"
    loaded: Mapping[str, Any] = {}
    if policy is not None:
        loaded = policy
        source = "caller-supplied"
    else:
        try:
            from src.routing_policy import load_policy

            loaded = load_policy() or {}
        except Exception:
            # Unreadable is not "no policy": hash the empty mapping; the domain
            # layer still fails closed on unknown domains.
            loaded = {}
            source = "unavailable"
    version = str(loaded.get("routingPolicyVersion") or "unversioned")
    digest = _sha256_hex(_canonical(dict(loaded)))
    return PolicySnapshot(version=version, policy_hash=digest, source=source)


@dataclass(frozen=True)
class CandidateAssessment:
    """One candidate's fate and the rule that decided it, so refusals are
    diagnosable."""

    target_id: str
    profile_id: str
    provider: str
    locality: str
    eligible: bool
    rule: str
    reason: str = ""
    receipt_id: str = ""
    receipt_hash: str = ""
    receipt_observed_at: str = ""
    receipt_freshness_s: Optional[int] = None
    receipt_ttl_s: Optional[int] = None
    budget_class: str = ""
    exactness: str = ""
    cost_rank: int = 0
    preference_rank: int = 0

    def to_dict(self) -> dict:
        return {
            "target_id": self.target_id, "profile_id": self.profile_id,
            "provider": self.provider, "locality": self.locality,
            "eligible": self.eligible, "rule": self.rule, "reason": self.reason,
            "capability_receipt_id": self.receipt_id,
            "capability_receipt_hash": self.receipt_hash,
            "capability_receipt_observed_at": self.receipt_observed_at,
            "capability_receipt_freshness_s": self.receipt_freshness_s,
            "capability_receipt_ttl_s": self.receipt_ttl_s,
            "budget_class": self.budget_class, "exactness": self.exactness,
            "cost_rank": self.cost_rank, "preference_rank": self.preference_rank,
        }


def classify_capacity_for(profile: ExecutionTargetProfile,
                           capacity_receipts: Sequence[Any], *,
                           now: Optional[datetime] = None) -> Tuple[bool, Tuple[str, ...], str, str]:
    """Classify provider capacity facts for one target profile."""
    if profile.locality == LOCALITY_LOCAL:
        return (True, (), "eligible", "local target: no subscription capacity required")

    matching = tuple(r for r in (capacity_receipts or ())
                     if profile.model in tuple(getattr(r, "exposed_models", ()))
                     and r.provider == profile.provider
                     and bool(profile.endpoint_url) and r.endpoint_url == profile.endpoint_url
                     and bool(profile.credential_sha256)
                     and r.credential_sha256 == profile.credential_sha256)
    if not matching:
        return (False, (), REFUSED_CAPACITY_MISSING,
                f"no capacity receipt exposes model {profile.model!r}")
    fresh = tuple(r for r in matching if r.facts_are_fresh(now))
    if not fresh:
        return (False, (), REFUSED_CAPACITY_STALE,
                f"capacity receipts exposing model {profile.model!r} are stale")
    from src.provider_capacity import Entitlement
    # A healthy subscription doesn't grant its native entitlement to this hosted
    # adapter; third-party harness use needs a separate policy grant.
    usable = tuple(r for r in fresh if r.entitlement in (Entitlement.API, Entitlement.AGENT_SDK)
                   and r.has_usable_capacity_facts(now=now))
    if not usable:
        return (False, (), REFUSED_CAPACITY_UNUSABLE,
                f"fresh capacity receipts exposing model {profile.model!r} are unusable")
    refs = tuple(sorted({str(r.ref) for r in usable}))
    return (True, refs, "eligible", "fresh usable capacity facts available")


def _assess(profile: ExecutionTargetProfile,
            receipt: Optional[LegacyCapabilityView],
            request: RoutingRequest, *, domain_policy: Any = None,
            policy_local_only: bool = False,
            resources: Optional[Mapping[str, Mapping[str, Any]]] = None,
            capacity_receipts: Sequence[Any] = (),
            now: Optional[datetime] = None,
            preference_rank: int = 0) -> CandidateAssessment:
    """Filter ONE candidate. First failing rule wins; the order IS the contract."""
    required = request.required_capabilities()
    state = (resources or {}).get(profile.target_id) or {}
    age = receipt.freshness_s(now) if receipt is not None else None

    def fate(eligible: bool, rule: str, reason: str) -> CandidateAssessment:
        return CandidateAssessment(
            target_id=profile.target_id, profile_id=profile.profile_id,
            provider=profile.provider, locality=profile.locality,
            eligible=eligible, rule=rule, reason=reason,
            receipt_id=(receipt.receipt_id if receipt else ""),
            receipt_hash=(receipt.receipt_hash if receipt else ""),
            receipt_observed_at=(receipt.observed_at if receipt else ""),
            receipt_freshness_s=age,
            receipt_ttl_s=(receipt.ttl_s if receipt else None),
            budget_class=profile.budget_class, exactness=profile.exactness,
            cost_rank=profile.cost_rank, preference_rank=preference_rank)

    # 1. domain privacy / allow-deny / sensitivity ceiling.
    try:
        from src.routing_domain_policy import evaluate_route

        decision = evaluate_route(
            domain=request.domain, provider=profile.provider,
            sensitivity=request.sensitivity, policy=domain_policy,
            endpoint_url=profile.endpoint_url)
    except Exception as exc:  # a policy that cannot be evaluated DENIES
        return fate(False, REFUSED_POLICY_DENIED,
                    f"domain policy could not be evaluated: {exc}")
    if not decision.allowed:
        return fate(False, REFUSED_POLICY_DENIED,
                    f"domain policy rule {decision.rule}: {decision.reason}")

    # 2. locality: local-only work can never use a hosted profile.
    if (request.local_only or policy_local_only) and not profile.is_local:
        return fate(False, REFUSED_PRIVACY_LOCAL_ONLY,
                    "local-only work may not execute on a hosted target")

    # 3. inference: deterministic-only nodes are ineligible for inference.
    if request.needs_inference() and not profile.inference:
        return fate(False, REFUSED_NOT_INFERENCE_TARGET,
                    f"profile {profile.target_id} is not an inference target")

    # 4. exactness: declared or measured approximation can't satisfy exact intent.
    if request.exactness == EXACTNESS_EXACT and profile.exactness != EXACTNESS_EXACT:
        return fate(False, REFUSED_EXACTNESS,
                    "an approximate profile cannot satisfy an exact/"
                    "reference-intent request")
    if (request.exactness == EXACTNESS_EXACT and receipt is not None
            and receipt.exactness != EXACTNESS_EXACT):
        return fate(False, REFUSED_EXACTNESS,
                    "the receipt evidences an approximate profile, which cannot "
                    "satisfy an exact/reference-intent request")

    # 5. role.
    if not profile.supports_role(request.role):
        return fate(False, REFUSED_ROLE,
                    f"profile does not declare role {request.role!r}")

    # 6. a receipt is required, for this exact profile.
    if receipt is None:
        return fate(False, REFUSED_RECEIPT_MISSING,
                    "no capability receipt for this profile")
    if (receipt.profile_id != profile.profile_id
            or receipt.target_id != profile.target_id):
        return fate(False, REFUSED_RECEIPT_MISMATCH,
                    f"receipt {receipt.receipt_id} was measured for "
                    f"{receipt.profile_id}/{receipt.target_id}")

    # 7. freshness.
    if age is None:
        return fate(False, REFUSED_RECEIPT_STALE,
                    "receipt timestamp is unreadable, so freshness is unknown")
    if age < 0 or age > int(receipt.ttl_s):
        return fate(False, REFUSED_RECEIPT_STALE,
                    f"receipt age {age}s exceeds ttl {receipt.ttl_s}s")

    # 8. health.
    if not receipt.healthy:
        return fate(False, REFUSED_RECEIPT_UNHEALTHY,
                    f"receipt reports the profile unhealthy: {receipt.notes}")

    # 9. measured capabilities (the receipt's evidence, not the profile's claim).
    missing = sorted(set(required) - set(receipt.capabilities))
    if missing:
        return fate(False, REFUSED_CAPABILITY_MISSING,
                    f"receipt does not evidence capability(ies) {missing}")

    # 10. provider capacity / entitlement facts for hosted profiles.
    capacity_ok, _, capacity_rule, capacity_reason = classify_capacity_for(
        profile, capacity_receipts, now=now)
    if not capacity_ok:
        return fate(False, capacity_rule, capacity_reason)

    # 11. tools / permission envelope.
    missing_tools = sorted(set(request.required_tools) - set(profile.tools))
    if missing_tools:
        return fate(False, REFUSED_TOOL,
                    f"profile does not grant tool(s) {missing_tools}")

    # 12. network.
    if request.network_policy and profile.network_policy != request.network_policy:
        return fate(False, REFUSED_NETWORK,
                    f"profile network policy {profile.network_policy!r} does not "
                    f"satisfy {request.network_policy!r}")

    # 13. budget.
    if request.budget_class and profile.budget_class != request.budget_class:
        return fate(False, REFUSED_BUDGET,
                    f"profile budget class {profile.budget_class!r} does not match "
                    f"{request.budget_class!r}")
    if profile.cost_rank > request.max_cost_rank:
        return fate(False, REFUSED_BUDGET,
                    f"cost rank {profile.cost_rank} exceeds the request ceiling "
                    f"{request.max_cost_rank}")

    # 14. resources (a fact about NOW, not about capability).
    if state and state.get("available") is False:
        return fate(False, REFUSED_RESOURCE,
                    str(state.get("reason") or "resource unavailable"))

    return fate(True, "eligible", "all filters passed")


@dataclass(frozen=True)
class DispatchDecision:
    """The immutable routing decision and the receipt's source of truth.

    `to_ps638_receipt_kwargs()` returns `DispatchDecisionReceipt`'s fields and
    ``receipt_hash`` uses its ``core()`` rule, so an `AttemptReceipt` records
    this decision's identity. Facts the receipt has no field for (fallback rule,
    budget/resource facts) ride on the selected entry in ``candidates_considered``.
    ``authority`` is recorded, never enforced, and omitted from the hash input
    when absent.
    """

    decision_id: str
    request: RoutingRequest
    selected_profile: ExecutionTargetProfile
    selected_receipt: Optional[LegacyCapabilityView]
    policy: PolicySnapshot
    candidates: Tuple[CandidateAssessment, ...] = ()
    reason_code: str = ""
    reason: str = ""
    fallback_used: bool = False
    fallback_rule: str = FALLBACK_RULE
    preference_rank: int = 0
    budget_facts: Mapping[str, Any] = field(default_factory=dict)
    resource_facts: Mapping[str, Any] = field(default_factory=dict)
    granted_tools: Tuple[str, ...] = ()
    granted_write_scope: Tuple[str, ...] = ()
    granted_read_scope: Tuple[str, ...] = ()
    network_policy: str = ""
    observed_at: str = ""
    schema_version: int = SCHEMA_VERSION
    authority: Optional[Mapping[str, Any]] = None
    capacity_receipt_refs: Tuple[str, ...] = ()
    offer_receipt_refs: Tuple[str, ...] = ()
    offer_quote_digests: Tuple[str, ...] = ()
    receipt_hash: str = field(default="")

    def pin(self) -> dict:
        """The frozen execution identity an attempt must use; the adapter has
        nothing left to choose."""
        profile = self.selected_profile
        return {
            "target_id": profile.target_id, "profile_id": profile.profile_id,
            "host": profile.host, "provider": profile.provider,
            "runtime_kind": profile.runtime_kind,
            "runtime_version": profile.runtime_version, "model": profile.model,
            "model_digest": profile.model_digest, "backend": profile.backend,
            "locality": profile.locality, "endpoint_url": profile.endpoint_url,
            "endpoint_type": profile.endpoint_type, "exactness": profile.exactness,
            "granted_tools": list(self.granted_tools),
            "granted_write_scope": list(self.granted_write_scope),
            "granted_read_scope": list(self.granted_read_scope),
            "network_policy": self.network_policy,
            "decision_id": self.decision_id,
            "receipt_hash": self.receipt_hash,
        }

    @property
    def capability_receipt_refs(self) -> Tuple[str, ...]:
        """Content hashes of the receipts that made the selection defensible."""
        refs: list = []
        for receipt in (self.selected_receipt,):
            if receipt is None:
                continue
            # A persisted capability receipt is the stronger reference: the
            # evidence identity, not a decision-time summary.
            ref = str(getattr(receipt, "source_receipt_hash", "")
                      or receipt.receipt_hash)
            if ref not in refs:
                refs.append(ref)
        return tuple(refs)

    def attempt_binding(self) -> dict:
        """What an AttemptReceipt records to bind itself to this decision."""
        return {
            "dispatch_receipt_hash": self.receipt_hash,
            "dispatch_receipt_id": f"dispatch-{self.decision_id}",
            "selected_target_id": self.selected_profile.target_id,
        }

    def to_ps638_receipt_kwargs(self) -> dict:
        """Exactly ``DispatchDecisionReceipt``'s constructor fields. The selected
        entry carries decision-level facts so the receipt is self-contained."""
        considered = []
        for assessment in self.candidates:
            entry = assessment.to_dict()
            if (assessment.target_id == self.selected_profile.target_id
                    and assessment.profile_id == self.selected_profile.profile_id):
                entry = {**entry, "selected": True, "reason_code": self.reason_code,
                         "fallback_used": self.fallback_used,
                         "fallback_rule": self.fallback_rule,
                         "budget_facts": dict(self.budget_facts),
                         "resource_facts": dict(self.resource_facts),
                         "exactness": self.selected_profile.exactness}
            considered.append(entry)
        profile = self.selected_profile
        kwargs = {
            "receipt_id": f"dispatch-{self.decision_id}",
            "execution_package_hash": self.request.execution_package_hash,
            "run_id": self.request.run_id,
            "packet_id": self.request.packet_id,
            "selected_target_id": profile.target_id,
            "selected_host": profile.host,
            "selected_model": profile.model,
            "decided_by": DECIDED_BY_POLICY,
            "reason": f"{self.reason_code}: {self.reason}",
            "requested_role": self.request.role,
            "requested_capabilities": tuple(self.request.required_capabilities()),
            "policy_ref": self.policy.policy_ref,
            "candidates_considered": tuple(considered),
            "capability_receipt_refs": self.capability_receipt_refs,
            "selected_runtime_kind": profile.runtime_kind,
            "selected_runtime_version": profile.runtime_version,
            "selected_model_digest": profile.model_digest,
            "selected_backend": profile.backend,
            "granted_tools": tuple(self.granted_tools),
            "granted_write_scope": tuple(self.granted_write_scope),
            "granted_read_scope": tuple(self.granted_read_scope),
            "network_policy": self.network_policy,
            "decided_at": self.observed_at,
            "schema_version": PS638_RECEIPT_SCHEMA_VERSION_EXPECTED,
        }
        # Omit the key entirely when authority is absent: this dict is hashed
        # verbatim as seal.evidence_hash, so a `None` entry would change that
        # hash for every authority-free dispatch.
        if self.authority is not None:
            kwargs["authority"] = self.authority
        if self.capacity_receipt_refs:
            kwargs["capacity_receipt_refs"] = self.capacity_receipt_refs
        if self.offer_receipt_refs:
            kwargs["offer_receipt_refs"] = self.offer_receipt_refs
        if self.offer_quote_digests:
            kwargs["offer_quote_digests"] = self.offer_quote_digests
        return kwargs

    def core(self) -> dict:
        payload = {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "request": self.request.core(),
            "policy": self.policy.to_dict(),
            "candidates": [a.to_dict() for a in self.candidates],
            "selected_profile": self.selected_profile.to_dict(),
            "selected_receipt_hash": (self.selected_receipt.receipt_hash
                                      if self.selected_receipt else ""),
            "reason_code": self.reason_code, "reason": self.reason,
            "fallback_used": self.fallback_used, "fallback_rule": self.fallback_rule,
            "preference_rank": self.preference_rank,
            "budget_facts": dict(self.budget_facts),
            "resource_facts": dict(self.resource_facts),
            "granted_tools": list(self.granted_tools),
            "granted_write_scope": list(self.granted_write_scope),
            "granted_read_scope": list(self.granted_read_scope),
            "network_policy": self.network_policy,
            "observed_at": self.observed_at,
        }
        if self.capacity_receipt_refs:
            payload["capacity_receipt_refs"] = list(self.capacity_receipt_refs)
        if self.offer_receipt_refs:
            payload["offer_receipt_refs"] = list(self.offer_receipt_refs)
        if self.offer_quote_digests:
            payload["offer_quote_digests"] = list(self.offer_quote_digests)
        return payload

    @property
    def decision_hash(self) -> str:
        """Content hash of this record's own fields (distinct from receipt_hash)."""
        return _sha256_hex(_canonical(self.core()))

    def to_dict(self) -> dict:
        payload = self.core()
        payload["receipt_hash"] = self.receipt_hash
        payload["decision_hash"] = self.decision_hash
        return payload


def _receipt_index(receipts: Any) -> dict:
    """Index capability receipts by profile_id (and target_id as a fallback)."""
    index: dict = {}
    if not receipts:
        return index
    items = (list(receipts.values()) if isinstance(receipts, Mapping)
             else list(receipts))
    for receipt in items:
        if receipt is None:
            continue
        index[receipt.profile_id] = receipt
        index.setdefault(f"target:{receipt.target_id}", receipt)
    return index


def _resolve_domain_policy(request: RoutingRequest, provided: Any) -> tuple:
    """(domain_policy, local_only); fails closed when the policy can't be read."""
    if provided is not None:
        return provided, bool(getattr(provided, "local_only", False))
    try:
        from src.routing_domain_policy import domain_policy

        pol = domain_policy(request.domain)
        return pol, bool(getattr(pol, "local_only", False))
    except Exception:
        # Unresolvable policy means local-only: refuse to leave the machine.
        return None, True


def select_target(request: RoutingRequest, *,
                  profiles: Sequence[ExecutionTargetProfile],
                  receipts: Any = None,
                  policy: Optional[PolicySnapshot] = None,
                  domain_policy: Any = None,
                  resources: Optional[Mapping[str, Mapping[str, Any]]] = None,
                  capacity_receipts: Sequence[Any] = (),
                  now: Optional[datetime] = None,
                  decision_id: str = "") -> DispatchDecision:
    """Select an execution target, or raise :class:`RoutingRefused`.

    Pure: every candidate gets the same ordered rules and the first eligible one
    in deterministic order wins (tests assert identical ``decision_hash``).
    """
    # Evaluate capacity freshness at one instant for the whole decision, so a
    # cited capacity ref can't go stale between assessment and selection.
    selection_time = now if now is not None else datetime.now(timezone.utc)
    profiles = list(profiles or ())
    if not profiles:
        raise RoutingRefused(REFUSED_NO_CANDIDATES,
                             "no candidate profiles were supplied",
                             run_id=request.run_id, packet_id=request.packet_id)

    snapshot = policy or policy_snapshot()
    resolved_policy, policy_local_only = _resolve_domain_policy(request, domain_policy)
    policy_local_only = policy_local_only or request.local_only
    index = _receipt_index(receipts)
    preferred = list(request.preferred_profile_ids)

    assessments: list = []
    for profile in profiles:
        rank = (preferred.index(profile.profile_id)
                if profile.profile_id in preferred else len(preferred))
        receipt = index.get(profile.profile_id) or index.get(
            f"target:{profile.target_id}")
        assessments.append(_assess(
            profile, receipt, request, domain_policy=resolved_policy,
            policy_local_only=policy_local_only, resources=resources,
            capacity_receipts=capacity_receipts, now=selection_time, preference_rank=rank))

    eligible = [a for a in assessments if a.eligible]
    local_only = bool(policy_local_only)
    if not eligible:
        if local_only and not [a for a in eligible
                               if a.locality == LOCALITY_LOCAL]:
            raise RoutingRefused(
                REFUSED_PRIVACY_LOCAL_ONLY,
                "no eligible LOCAL profile: local-only work must not escape to a "
                "hosted target, so this is a refusal, not a downgrade",
                assessments=assessments, run_id=request.run_id,
                packet_id=request.packet_id)
        # If every candidate failed for the same reason, that is the refusal.
        rules = sorted({a.rule for a in assessments})
        code = rules[0] if len(rules) == 1 else REFUSED_NO_ELIGIBLE_TARGET
        raise RoutingRefused(
            code,
            ("every candidate was refused for the same reason"
             if len(rules) == 1 else
             "no candidate profile survived the ordered filters; refusals: "
             + ", ".join(f"{a.target_id}={a.rule}" for a in assessments)),
            assessments=assessments, run_id=request.run_id,
            packet_id=request.packet_id)

    # Preference order: explicit preference, local-first when required, cost
    # rank, stable tie-break. This order is the fallback rule.
    eligible.sort(key=lambda a: (
        a.preference_rank,
        0 if (local_only and a.locality == LOCALITY_LOCAL) else 1,
        a.cost_rank, a.target_id, a.profile_id))
    chosen = eligible[0]
    profile = next(p for p in profiles
                   if p.target_id == chosen.target_id
                   and p.profile_id == chosen.profile_id)
    receipt = (index.get(profile.profile_id)
               or index.get(f"target:{profile.target_id}"))
    _, selected_capacity_refs, _, _ = classify_capacity_for(
        profile, capacity_receipts, now=selection_time)
    # Sort assessments so input order never changes the decision hash.
    ordered = sorted(assessments, key=lambda a: (
        a.preference_rank,
        0 if a.eligible else 1,
        0 if (local_only and a.locality == LOCALITY_LOCAL) else 1,
        a.cost_rank, a.target_id, a.profile_id))


    if local_only:
        reason_code = REASON_SELECTED_LOCAL_ONLY
        reason = ("local-only work selected a local profile; every hosted "
                  "candidate was refused by policy")
    elif chosen.preference_rank < len(preferred):
        reason_code = REASON_SELECTED_PREFERRED
        reason = ("selected an explicitly preferred candidate at preference rank "
                  f"{chosen.preference_rank}")
    elif preferred:
        reason_code = REASON_SELECTED_FALLBACK
        reason = (f"no preferred candidate was eligible; selected the first "
                  f"eligible candidate at preference rank {chosen.preference_rank}")
    else:
        # No preference expressed, so nothing was substituted.
        reason_code = REASON_SELECTED_DETERMINISTIC
        reason = ("no profile preference was expressed; the deterministic order "
                  "decided")

    observed = selection_time.isoformat()
    facts = {
        "request_budget_class": request.budget_class,
        "selected_budget_class": profile.budget_class,
        "max_cost_rank": request.max_cost_rank,
        "selected_cost_rank": profile.cost_rank,
        "eligible_candidates": len(eligible),
        "candidates_considered": len(assessments),
    }
    provisional = DispatchDecision(
        decision_id=decision_id or uuid.uuid4().hex,
        request=request, selected_profile=profile, selected_receipt=receipt,
        policy=snapshot, candidates=tuple(ordered), reason_code=reason_code,
        reason=reason, fallback_used=(reason_code == REASON_SELECTED_FALLBACK),
        preference_rank=chosen.preference_rank, budget_facts=facts,
        resource_facts=dict((resources or {}).get(profile.target_id) or {}),
        # Grant only what the request needed (within the profile's envelope);
        # naming no tools grants none.
        granted_tools=tuple(sorted(request.required_tools)),
        granted_write_scope=tuple(request.write_scope),
        granted_read_scope=tuple(request.read_scope),
        network_policy=request.network_policy or profile.network_policy,
        observed_at=observed, capacity_receipt_refs=selected_capacity_refs)
    payload = {name: getattr(provisional, name)
               for name in provisional.__dataclass_fields__
               if name != "receipt_hash"}
    payload["receipt_hash"] = ps638_receipt_hash(
        provisional.to_ps638_receipt_kwargs())
    return DispatchDecision(**payload)
