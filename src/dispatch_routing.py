"""src/dispatch_routing.py — deterministic execution-target selection (PS-605).

This is the production routing seam. One function of canonical inputs produces
either a selected target or a typed refusal, and the decision is written down as a
`DispatchDecision` whose receipt form is PS-638's `DispatchDecisionReceipt`.

    ExecutionPackage -> requested domain/role/capabilities
        -> deterministic policy (src.routing_domain_policy: privacy/allow/deny)
        -> fresh, qualified ExecutionTarget candidates (profile + capability receipt)
        -> privacy / permission / network / budget / exactness filters
        -> deterministically SELECTED target
        -> immutable DispatchDecision (+ PS-638 receipt kwargs)
        -> pinned attempt identity

Three properties are structural, not promised:

**1. Nothing here dispatches.** :func:`select_target` is a pure function of its
arguments: no I/O, no model call, no lifecycle write. It returns a frozen record.
A runtime adapter therefore cannot reroute itself — there is no API to call, and
the selected identity is data handed TO the adapter, not a decision the adapter
makes.

**2. There is ONE filter path.** Fallback is not a second, weaker route: every
candidate goes through the same ordered filters, and the selection is the first
eligible candidate in a deterministic order. "Fallback" is then a recorded fact
about which preference rank was used, never an escape from policy. If no candidate
survives, the result is a typed refusal — and for local-only work it is
specifically a privacy refusal, because failing closed there is the point.

**3. Capability comes from a RECEIPT, not from a host name.** A profile is
eligible only with a fresh, healthy capability receipt for that exact profile, so a
runtime/model/backend/approximation change that invalidates capability also
invalidates routing, even though the physical host is unchanged. Approximate
profiles cannot satisfy an exact/reference-intent request; a deterministic-only
node cannot be routed as an inference target.

Deliberately NOT here: retry policy, budgets as accounting, the ledger, the
evidence package, verification, landing, or any notion of acceptance. PS-605 owns
selection; PS-635 owns the loop and PS-638 owns the envelope.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

# ------------------------------------------------------------- capabilities ---
#: Profile-level capabilities, measured per (runtime, backend, model, profile) by
#: PS-632 and carried in a capability receipt. Names mirror the PS-605 ticket's
#: list verbatim so a packet can require exactly what the ticket names.
CAP_TEXT_GENERATION = "text_generation"
CAP_REASONING_FRAMING = "reasoning_framing"
CAP_SINGLE_TOOL_CALL = "single_tool_call"
CAP_PARALLEL_TOOL_CALLS = "parallel_tool_calls"
CAP_STREAMED_TOOL_CALLS = "streamed_tool_call_integrity"
CAP_NO_TOOL_DECLINE = "no_tool_decline"
CAP_MULTI_ROUND = "multi_round_tool_continuation"
CAP_CONTEXT_INTEGRITY = "context_integrity"
CAP_CANCELLATION = "cancellation"
#: The name PS-632's registry uses for a streamed runtime. Deliberately the SAME
#: string, so a packet requirement, a capability receipt and a profile all say
#: "streaming" instead of three near-synonyms that stop matching each other.
CAP_STREAMING = "streaming"
CAP_EXACT_REFERENCE_SEMANTICS = "exact_reference_semantics"
CAP_DETERMINISTIC_VERIFICATION = "deterministic_verification"
#: PS-623's catalog vocabulary, kept as the hosted/subscription role names so the
#: two layers describe the same estate instead of inventing parallel words.
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

#: Capabilities that only an INFERENCE target can carry. Their presence in a
#: request is what makes a deterministic-only node ineligible rather than
#: "merely not preferred".
INFERENCE_CAPABILITIES: FrozenSet[str] = frozenset({
    CAP_TEXT_GENERATION, CAP_REASONING_FRAMING, CAP_SINGLE_TOOL_CALL,
    CAP_PARALLEL_TOOL_CALLS, CAP_STREAMED_TOOL_CALLS, CAP_NO_TOOL_DECLINE,
    CAP_MULTI_ROUND, CAP_EXACT_REFERENCE_SEMANTICS, CAP_INTEGRATION_STRONG,
    CAP_IMPLEMENTATION_FAST, CAP_BULK_LOCAL,
})

# -------------------------------------------------------------------- roles ---
ROLE_IMPLEMENTER = "local_implementer"
ROLE_REPAIR = "local_repair"
ROLE_VERIFIER = "deterministic_verifier"
ROLE_GOVERNANCE_CI = "governance_ci"
ROLE_PLANNER = "planner"
ROLE_SCOUT = "read_only_analyst"
#: The routing harness's own role names (RoutingModelProfile.roles + ROLE_BY_TASK).
#: Included so the two layers share one vocabulary instead of translating between
#: two, which is how a role quietly stops matching the capability set it implies.
ROLE_REVIEWER = "reviewer"
ROLE_DEBUGGER = "debugger"
ROLE_ESCALATION = "escalation"
#: The bare names the routing harness uses for the two roles that differ from the
#: canonical spelling (ROLE_BY_TASK / RoutingModelProfile.roles).
ROLE_IMPLEMENTER_ROUTER = "implementer"
ROLE_SCOUT_ROUTER = "scout"

#: What a role requires when the request does not name its own capabilities. A
#: role is a shorthand for a capability set, never a substitute for one.
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

# ------------------------------------------------------- locality/exactness ---
LOCALITY_LOCAL = "local"
LOCALITY_HOSTED = "hosted"

EXACTNESS_EXACT = "exact_reference_intent"
EXACTNESS_APPROXIMATE = "approximate"

KNOWN_EXACTNESS = frozenset({EXACTNESS_EXACT, EXACTNESS_APPROXIMATE})

# ---------------------------------------------------------- refusal codes ---
#: Every refusal is typed. These are reason CODES, not prose: a caller can branch
#: on them, and a receipt can name them, without parsing a sentence.
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

KNOWN_REFUSALS: FrozenSet[str] = frozenset({
    REFUSED_NO_CANDIDATES, REFUSED_NO_ELIGIBLE_TARGET, REFUSED_PRIVACY_LOCAL_ONLY,
    REFUSED_POLICY_DENIED, REFUSED_RECEIPT_MISSING, REFUSED_RECEIPT_MISMATCH,
    REFUSED_RECEIPT_STALE, REFUSED_RECEIPT_UNHEALTHY, REFUSED_CAPABILITY_MISSING,
    REFUSED_EXACTNESS, REFUSED_NOT_INFERENCE_TARGET, REFUSED_ROLE, REFUSED_TOOL,
    REFUSED_NETWORK, REFUSED_BUDGET, REFUSED_RESOURCE, REFUSED_UNKNOWN_CAPABILITY,
})

#: Success reason codes: HOW the selected candidate was reached, which is what
#: makes "fallback" an auditable fact rather than an omission.
REASON_SELECTED_PREFERRED = "selected_preferred_candidate"
REASON_SELECTED_FALLBACK = "selected_fallback_candidate"
REASON_SELECTED_LOCAL_ONLY = "selected_local_only_candidate"
REASON_SELECTED_DETERMINISTIC = "selected_deterministic_candidate"

#: The fallback rule, recorded verbatim on every decision. It is a property of the
#: SELECTOR (one filter path, deterministic order), not a per-call choice.
FALLBACK_RULE = (
    "every candidate is filtered by the same ordered rules (domain privacy, "
    "locality, inference capability, role, receipt presence/match/freshness/"
    "health, measured capabilities, exactness, tools, network, budget, "
    "resources); the first eligible candidate in deterministic preference order "
    "is selected; a fallback is therefore a LOWER PREFERENCE RANK, never a "
    "weaker standard"
)


def _canonical(payload: object) -> bytes:
    """PS-638's canonical form, byte-for-byte, because receipt hashes must agree.

    ``ensure_ascii=False`` is not cosmetic: PS-638's ``_canonical`` uses it, and a
    receipt hash computed over an escaped payload would differ from the hash PS-638
    stamps on the same fields. The cross-lineage contract test asserts the equality
    rather than trusting this comment.
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
    """Typed, fail-closed refusal. Raised INSTEAD of a substitute target.

    A refusal carries the per-candidate assessment list, so "why not" is data:
    every candidate that was considered has a code and a reason, which is what
    makes an ineligible estate debuggable instead of mysterious.
    """

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


# ------------------------------------------------------- PS-638 receipt shape ---
#: PS-638 `DispatchDecisionReceipt`'s constructor fields and the fields its
#: ``core()`` seals over, copied from src/execution_package.py on branch
#: work/ps-635-loop-demo (DISPATCH_RECEIPT_SCHEMA_VERSION = 1). Frozen here as a
#: CONTRACT: this module emits a receipt PS-638 can construct UNCHANGED, and the
#: mapping is asserted by a test rather than discovered when the branches meet.
PS638_RECEIPT_SCHEMA_VERSION_EXPECTED = 1
PS638_RECEIPT_FIELDS: Tuple[str, ...] = (
    "receipt_id", "execution_package_hash", "run_id", "packet_id",
    "selected_target_id", "selected_host", "selected_model", "decided_by",
    "reason", "requested_role", "requested_capabilities", "policy_ref",
    "candidates_considered", "capability_receipt_refs", "selected_runtime_kind",
    "selected_runtime_version", "selected_model_digest", "selected_backend",
    "granted_tools", "granted_write_scope", "granted_read_scope",
    "network_policy", "decided_at", "schema_version",
)
#: The fields PS-638's ``core()`` covers, in ITS order. The receipt hash is
#: sha256 over the canonical JSON of exactly these, with tuple-valued fields
#: serialized as lists (PS-638's own loose ends: it does that in ``core()``).
PS638_RECEIPT_CORE_FIELDS: Tuple[str, ...] = (
    "schema_version", "receipt_id", "execution_package_hash", "run_id",
    "packet_id", "requested_role", "requested_capabilities", "policy_ref",
    "candidates_considered", "capability_receipt_refs", "selected_target_id",
    "selected_host", "selected_model", "selected_runtime_kind",
    "selected_runtime_version", "selected_model_digest", "selected_backend",
    "granted_tools", "granted_write_scope", "granted_read_scope",
    "network_policy", "decided_by", "reason", "decided_at",
)
_PS638_LIST_FIELDS: Tuple[str, ...] = (
    "requested_capabilities", "candidates_considered", "capability_receipt_refs",
    "granted_tools", "granted_write_scope", "granted_read_scope",
)
DECIDED_BY_POLICY = "ps605_policy"


def ps638_receipt_core(kwargs: Mapping[str, Any]) -> dict:
    """The exact dict PS-638 hashes for a dispatch receipt.

    One implementation, used for the receipt this module emits, so the hash a
    decision publishes is the hash PS-638 will verify — not a lookalike computed
    by a second code path that could drift from it.
    """
    core: dict = {}
    for name in PS638_RECEIPT_CORE_FIELDS:
        value = kwargs.get(name)
        if name in _PS638_LIST_FIELDS:
            core[name] = [dict(v) if isinstance(v, Mapping) else v
                          for v in (value or ())]
        else:
            core[name] = value
    return core


def ps638_receipt_hash(kwargs: Mapping[str, Any]) -> str:
    """sha256 over PS-638's receipt core, exactly as PS-638 computes it."""
    return _sha256_hex(_canonical(ps638_receipt_core(kwargs)))


# ------------------------------------------------------- capability receipt ---
#: HOW a receipt's capabilities became known. The strength of the claim is part of
#: the evidence, so it is recorded rather than assumed: a "declared" capability is
#: what a profile row says about itself, "detected" is a fact the system probed
#: about the endpoint, and "measured" is PS-632's per-profile measurement.
PROVENANCE_MEASURED = "measured"
PROVENANCE_DETECTED = "detected"
PROVENANCE_DECLARED = "declared"
KNOWN_PROVENANCE = frozenset({PROVENANCE_MEASURED, PROVENANCE_DETECTED,
                              PROVENANCE_DECLARED})


@dataclass(frozen=True)
class LegacyCapabilityView:
    """Non-authoritative PS-605 view of canonical PS-632 evidence.

    A receipt is what makes eligibility measurable rather than assumed: it names
    the profile it was measured against, what was actually measured, when, how
    long it stays valid, and whether the runtime is currently healthy. Routing on
    a host name instead of a receipt is how "the node is up" quietly becomes "the
    capability is present".
    """

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
    #: The PS-632 persisted-receipt hash this capability evidence came from, when
    #: there is one. PS-605's own receipt_hash covers its ROUTING view; this is the
    #: identity that resolves back to the exact measured capability evidence, so a
    #: later reader can re-derive what the profile could do instead of trusting a
    #: decision-time summary of it.
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
        """Fresh = readable timestamp AND inside its TTL.

        An unreadable timestamp is NOT fresh: "unknown" must never widen
        eligibility, which is the same rule the context budget uses for an
        unmeasured window.
        """
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


# ------------------------------------------------------------ the profile ---
@dataclass(frozen=True)
class ExecutionTargetProfile:
    """One exact execution profile: runtime + backend + model + role identity.

    Deliberately per-PROFILE, not per-host. ``host`` is recorded because evidence
    needs it, never used as the routing key: a runtime/model/backend change on the
    same box is a different profile with a different receipt, and possibly no
    receipt at all — which is exactly when it must stop being eligible.

    ``inference=False`` marks a node that must never be selected for inference
    work (MS-R1 is deterministic verification/governance only). ``exactness``
    marks an approximation profile, which may not satisfy a request that demands
    exact/reference-intent semantics.
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
    runtime_options: Mapping[str, Any] = field(default_factory=dict)
    #: Request-scoped generation settings are not target authority. They remain
    #: available only for a future PS-641 request/decision composition seam.
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
        """A profile supports a role only if it declares it.

        An empty role set means "no declared roles", so nothing is inferred: an
        unlabelled profile is not silently treated as a general-purpose worker.
        """
        return bool(role) and role in self.roles

    def core(self) -> dict:
        return {
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


# ------------------------------------------------------------ the request ---
@dataclass(frozen=True)
class RoutingRequest:
    """What a packet needs, in the terms routing is allowed to decide on.

    Built from an `ExecutionPackage` (PS-638) rather than from a caller's
    preference: domain, role, capabilities, privacy class, permission envelope and
    budgets are the package's fields, so a decision can be traced back to the
    intent it served.
    """

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
        """Explicit capabilities, or the role's shorthand — unioned, never one of.

        A caller that names both gets both: a role is a shorthand for a capability
        set, so it can only ever ADD requirements.
        """
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


# ------------------------------------------------------------ the policy ---
@dataclass(frozen=True)
class PolicySnapshot:
    """The policy revision a decision was made under, BY CONTENT.

    A version string alone is not enough: two different policies can share a
    version, and a routing receipt that cannot name the exact policy it applied
    cannot be re-evaluated later. So the version, the source and a content hash
    are all recorded, and ``policy_ref`` carries them as one string for the
    receipt.
    """

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
    """Snapshot the live routing policy (version + content hash).

    ``policy`` may be supplied directly (tests, or a caller that already loaded
    it); otherwise the live versioned policy is read through
    ``src.routing_policy.load_policy``. The hash is computed over the CONTENT of
    whatever was loaded, so a monkeypatched or edited policy produces a different
    policy_ref instead of inheriting the old one.
    """
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
            # A policy that cannot be read is NOT "no policy": the caller gets an
            # unversioned snapshot whose hash covers the empty mapping, and the
            # domain layer still fails closed on unknown domains.
            loaded = {}
            source = "unavailable"
    version = str(loaded.get("routingPolicyVersion") or "unversioned")
    digest = _sha256_hex(_canonical(dict(loaded)))
    return PolicySnapshot(version=version, policy_hash=digest, source=source)


# ------------------------------------------------------- candidate filter ---
@dataclass(frozen=True)
class CandidateAssessment:
    """One candidate's fate, with the rule that decided it.

    Every candidate gets one of these, eligible or not. That is what makes a
    refusal diagnosable: "nothing worked" is not an answer a person can act on,
    and a refusal that cannot be explained gets explained away instead.
    """

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


def _assess(profile: ExecutionTargetProfile,
            receipt: Optional[LegacyCapabilityView],
            request: RoutingRequest, *, domain_policy: Any = None,
            policy_local_only: bool = False,
            resources: Optional[Mapping[str, Mapping[str, Any]]] = None,
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

    # 2. locality. A local-only request (explicitly, or because its domain is
    #    local-only) cannot be satisfied by a hosted profile, ever.
    if (request.local_only or policy_local_only) and not profile.is_local:
        return fate(False, REFUSED_PRIVACY_LOCAL_ONLY,
                    "local-only work may not execute on a hosted target")

    # 3. inference: a deterministic-only node is not "less preferred", it is
    #    ineligible for inference work.
    if request.needs_inference() and not profile.inference:
        return fate(False, REFUSED_NOT_INFERENCE_TARGET,
                    f"profile {profile.target_id} is not an inference target")

    # 4. exactness: an approximation profile — declared, or MEASURED as
    #    approximate in the receipt — cannot satisfy exact/reference intent.
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

    # 6. a receipt is required, and it must be FOR THIS PROFILE.
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

    # 9. MEASURED capabilities (the receipt's evidence, not the profile's claim).
    missing = sorted(set(required) - set(receipt.capabilities))
    if missing:
        return fate(False, REFUSED_CAPABILITY_MISSING,
                    f"receipt does not evidence capability(ies) {missing}")

    # 10. tools / permission envelope.
    missing_tools = sorted(set(request.required_tools) - set(profile.tools))
    if missing_tools:
        return fate(False, REFUSED_TOOL,
                    f"profile does not grant tool(s) {missing_tools}")

    # 11. network.
    if request.network_policy and profile.network_policy != request.network_policy:
        return fate(False, REFUSED_NETWORK,
                    f"profile network policy {profile.network_policy!r} does not "
                    f"satisfy {request.network_policy!r}")

    # 12. budget.
    if request.budget_class and profile.budget_class != request.budget_class:
        return fate(False, REFUSED_BUDGET,
                    f"profile budget class {profile.budget_class!r} does not match "
                    f"{request.budget_class!r}")
    if profile.cost_rank > request.max_cost_rank:
        return fate(False, REFUSED_BUDGET,
                    f"cost rank {profile.cost_rank} exceeds the request ceiling "
                    f"{request.max_cost_rank}")

    # 13. resources (a fact about NOW, not about capability).
    if state and state.get("available") is False:
        return fate(False, REFUSED_RESOURCE,
                    str(state.get("reason") or "resource unavailable"))

    return fate(True, "eligible", "all filters passed")


# ------------------------------------------------------------- the decision ---
@dataclass(frozen=True)
class DispatchDecision:
    """The immutable routing decision, in the exact shape a receipt needs.

    This IS the receipt's source of truth: `to_ps638_receipt_kwargs()` returns the
    field set PS-638's `DispatchDecisionReceipt` accepts, and ``receipt_hash`` is
    computed with PS-638's own rule (sha256 over its ``core()`` fields), so the
    identity an `AttemptReceipt` records is the identity of THIS decision.

    Decision-level facts PS-638 has no field for — the fallback rule, the
    budget/resource facts that materially affected selection — are carried on the
    SELECTED candidate's entry in ``candidates_considered``, so a sealed package
    built from this receipt still contains them.
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
    receipt_hash: str = field(default="")

    def pin(self) -> dict:
        """The frozen execution identity an attempt must use.

        A caller pins THIS, not a host name: target/profile/provider/runtime/
        model/digest/backend and the granted envelope all come from the decision,
        so an adapter has nothing left to choose.
        """
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
            # A persisted PS-632 receipt is the stronger reference: it is the
            # capability EVIDENCE identity, not a decision-time summary of it.
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
        """Exactly PS-638's ``DispatchDecisionReceipt`` constructor fields.

        The SELECTED candidate's entry also carries the decision-level facts
        (reason_code, fallback rule/usage, budget and resource facts), so a sealed
        receipt is self-contained without a PS-638 schema change.
        """
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
        return {
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

    def core(self) -> dict:
        return {
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

    @property
    def decision_hash(self) -> str:
        """Content identity of the PS-605 record (distinct from the receipt hash).

        Both are named and both are published: the receipt hash is what PS-638
        verifies, and the decision hash is what this record's own fields add up to.
        """
        return _sha256_hex(_canonical(self.core()))

    def to_dict(self) -> dict:
        payload = self.core()
        payload["receipt_hash"] = self.receipt_hash
        payload["decision_hash"] = self.decision_hash
        return payload


# ------------------------------------------------------------- the selector ---
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
    """(domain_policy, local_only). Fail CLOSED when the policy cannot be read."""
    if provided is not None:
        return provided, bool(getattr(provided, "local_only", False))
    try:
        from src.routing_domain_policy import domain_policy

        pol = domain_policy(request.domain)
        return pol, bool(getattr(pol, "local_only", False))
    except Exception:
        # An unresolvable domain policy is treated as local-only: the failure mode
        # must be "refuse to leave the machine", never "assume unrestricted".
        return None, True


def select_target(request: RoutingRequest, *,
                  profiles: Sequence[ExecutionTargetProfile],
                  receipts: Any = None,
                  policy: Optional[PolicySnapshot] = None,
                  domain_policy: Any = None,
                  resources: Optional[Mapping[str, Mapping[str, Any]]] = None,
                  now: Optional[datetime] = None,
                  decision_id: str = "") -> DispatchDecision:
    """Select an execution target, or raise :class:`RoutingRefused`.

    Pure: no I/O, no model call, no state change. Every candidate is filtered by
    the same ordered rules and the first ELIGIBLE one in deterministic preference
    order is selected. Determinism is asserted by the test suite (same inputs ->
    identical ``decision_hash``), which is what makes a receipt re-checkable.
    """
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
            policy_local_only=policy_local_only, resources=resources, now=now,
            preference_rank=rank))

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
        # When every candidate was refused for the SAME reason, that reason IS the
        # refusal: "no_eligible_target" is the honest summary only when the estate
        # failed in different ways. The per-candidate rules are always attached.
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

    # Deterministic preference order: explicit preference, then local-first when
    # locality is a requirement, then cost rank, then a stable tie-break. This
    # ordering IS the fallback rule; a lower rank is a lower preference, never a
    # weaker standard.
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
    # The RECORD is canonical, not input-ordered: the assessment list is sorted so
    # two runs over the same candidates produce the same decision hash even if the
    # caller passed the profiles in a different order.
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
        # No preference was expressed, so nothing was substituted: the
        # deterministic order decided, and saying "preferred" here would imply the
        # caller asked for something it did not.
        reason_code = REASON_SELECTED_DETERMINISTIC
        reason = ("no profile preference was expressed; the deterministic order "
                  "decided")

    observed = (now or datetime.now(timezone.utc)).isoformat()
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
        # The granted tool envelope is what the REQUEST needed, verified to be
        # inside the profile's declared envelope by the filters above. A packet
        # that names no tools is granted none — silence is not a grant.
        granted_tools=tuple(sorted(request.required_tools)),
        granted_write_scope=tuple(request.write_scope),
        granted_read_scope=tuple(request.read_scope),
        network_policy=request.network_policy or profile.network_policy,
        observed_at=observed)
    payload = {name: getattr(provisional, name)
               for name in provisional.__dataclass_fields__
               if name != "receipt_hash"}
    payload["receipt_hash"] = ps638_receipt_hash(
        provisional.to_ps638_receipt_kwargs())
    return DispatchDecision(**payload)
