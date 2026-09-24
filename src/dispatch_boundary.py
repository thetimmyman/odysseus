"""src/dispatch_boundary.py — the production dispatch boundary (PS-605).

`src/dispatch_routing.py` decides; this module makes that decision the thing the
real dispatcher consumes. It is the seam between "the router chose" and "a model
was called":

    RoutingTask + candidate rows  ->  RoutingRequest        (intent, from data)
    candidates + endpoint rows    ->  profiles + receipts   (the estate)
    profiles/receipts + policy    ->  DispatchDecision      (selection authority)
    decision + candidates         ->  execution ORDER       (what may be attempted)
    resolved endpoint + decision  ->  pin check             (invocation guard)
    invocation + decision         ->  AttemptBinding        (PS-638 identity)
    all of the above              ->  sealed evidence       (re-checkable proof)

Four properties, all structural:

**1. There is no post-decision chooser.** The dispatcher iterates the order this
module returns; a candidate the decision did not find eligible is never attempted,
and an invocation whose resolved endpoint/model does not match the pinned identity
is REFUSED before the network call (:func:`verify_invocation`). A runtime adapter
can report facts and failures; it cannot substitute a target, because the only
identity it is handed is the one the decision pinned.

**2. Declaration is labelled, measurement is not.** A capability receipt carries
its provenance: ``measured`` (PS-632's job), ``detected`` (an endpoint flag the
system actually probed, e.g. ``ModelEndpoint.supports_tools``), or ``declared``
(what the row says about itself). "Unknown" is never a capability — a NULL
``supports_tools`` grants nothing — and a request may require measured-only
capabilities, which no declared receipt can satisfy. The decision records the
provenance it relied on, so the strength of the claim is visible in the evidence.

**3. A refusal stops the run.** When PS-605 refuses, the boundary raises and the
dispatcher records the refusal and attempts nothing: a local-only packet with no
eligible local profile produces ZERO invocations, not a quiet downgrade.

**4. Evidence is checkable, and tampering is detectable.** :func:`seal_dispatch_
evidence` binds the execution-package identity, policy revision AND content hash,
the full candidate set with per-candidate reasons, capability receipt ids and
freshness, the budget/resource snapshot actually used, the pinned identity, the
invocation record and the attempt binding into one content-addressed payload, and
:func:`validate_dispatch_evidence` re-derives every one of them — so changing the
selected target, a receipt, or the policy revision after sealing invalidates it.
"""
from __future__ import annotations

import dataclasses
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import (Any, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple)

# The dispatch_routing names below are RE-EXPORTED on purpose: this module is the
# boundary's public face, so a caller (or a test) asking for the routing vocabulary
# gets it from one import instead of two.
from src.dispatch_routing import (
    CAP_DETERMINISTIC_VERIFICATION,
    CAP_EXACT_REFERENCE_SEMANTICS,
    CAP_REASONING_FRAMING,
    CAP_SINGLE_TOOL_CALL,
    CAP_TEXT_GENERATION,
    EXACTNESS_APPROXIMATE,
    EXACTNESS_EXACT,
    FALLBACK_RULE,
    KNOWN_PROVENANCE,
    LOCALITY_HOSTED,
    LOCALITY_LOCAL,
    PROVENANCE_DECLARED,
    PROVENANCE_DETECTED,
    PROVENANCE_MEASURED,
    REFUSED_PRIVACY_LOCAL_ONLY,
    ROLE_DEBUGGER,
    ROLE_ESCALATION,
    ROLE_GOVERNANCE_CI,
    ROLE_IMPLEMENTER,
    ROLE_IMPLEMENTER_ROUTER,
    ROLE_PLANNER,
    ROLE_REPAIR,
    ROLE_REVIEWER,
    ROLE_SCOUT,
    ROLE_SCOUT_ROUTER,
    ROLE_VERIFIER,
    DispatchRoutingError,
    RoutingRefused,
    RoutingRequest,
    LegacyCapabilityView,
    make_legacy_capability_view,
    make_target_profile,
    policy_snapshot,
    ps638_receipt_hash,
    select_target,
)

SCHEMA_VERSION = 1

#: Capabilities a DECLARED receipt may evidence: things a profile row can honestly
#: claim about a text model. Tool-call and context-integrity capabilities are NOT
#: here on purpose — those need a detection or a measurement, and "the row says it
#: is a good model" is exactly the reduction the ticket forbids.
DECLARABLE_CAPABILITIES: Tuple[str, ...] = (
    CAP_TEXT_GENERATION, CAP_EXACT_REFERENCE_SEMANTICS, CAP_REASONING_FRAMING,
)

#: Capabilities a DETECTED receipt may evidence (an endpoint-level probe).
DETECTABLE_CAPABILITIES: Tuple[str, ...] = (
    CAP_TEXT_GENERATION, CAP_EXACT_REFERENCE_SEMANTICS, CAP_REASONING_FRAMING,
    CAP_SINGLE_TOOL_CALL,
)

#: Roles that make a profile an INFERENCE target. A profile declaring none of these
#: (MS-R1's governance_ci / verifier only) is deterministic and must never be
#: selected for generation work.
INFERENCE_ROLE_NAMES: FrozenSet[str] = frozenset({
    ROLE_IMPLEMENTER, ROLE_IMPLEMENTER_ROUTER, ROLE_REPAIR, ROLE_PLANNER,
    ROLE_SCOUT, ROLE_SCOUT_ROUTER, ROLE_DEBUGGER, ROLE_REVIEWER, ROLE_ESCALATION,
})


# ------------------------------------------------------------- refusal codes ---
BOUNDARY_REFUSED_NO_PROFILES = "no_dispatchable_profiles"
BOUNDARY_REFUSED_NO_ENDPOINT = "candidate_has_no_enabled_endpoint"
PIN_TARGET_MISMATCH = "pin_target_mismatch"
PIN_MODEL_MISMATCH = "pin_model_mismatch"
PIN_LOCALITY_MISMATCH = "pin_locality_mismatch"
PIN_PROFILE_MISMATCH = "pin_profile_mismatch"
PIN_ENDPOINT_MISMATCH = "pin_endpoint_mismatch"
PIN_RUNTIME_MISMATCH = "pin_runtime_identity_mismatch"
INVOCATION_REFUSED_BEFORE_DISPATCH = "invocation_refused_before_dispatch"
EVIDENCE_HASH_MISMATCH = "evidence_hash_mismatch"
EVIDENCE_DECISION_MISMATCH = "decision_receipt_hash_mismatch"
EVIDENCE_TARGET_MISMATCH = "attempt_target_mismatch"
EVIDENCE_ATTEMPT_UNBOUND = "attempt_receipt_unbound"
EVIDENCE_INVOCATION_OUTSIDE_DECISION = "invocation_outside_decision"
EVIDENCE_HOSTED_FOR_LOCAL_ONLY = "hosted_invocation_for_local_only"
EVIDENCE_RECEIPT_CHANGED = "capability_receipt_changed"
EVIDENCE_CAPACITY_CHANGED = "capacity_receipt_changed"
EVIDENCE_POLICY_CHANGED = "policy_revision_changed"
EVIDENCE_PIN_CHANGED = "pin_changed_after_sealing"
KNOWN_EVIDENCE_CODES = frozenset({
    EVIDENCE_HASH_MISMATCH, EVIDENCE_DECISION_MISMATCH, EVIDENCE_TARGET_MISMATCH,
    EVIDENCE_ATTEMPT_UNBOUND, EVIDENCE_INVOCATION_OUTSIDE_DECISION,
    EVIDENCE_HOSTED_FOR_LOCAL_ONLY, EVIDENCE_RECEIPT_CHANGED,
    EVIDENCE_CAPACITY_CHANGED,
    EVIDENCE_POLICY_CHANGED, EVIDENCE_PIN_CHANGED,
})


class DispatchBoundaryError(RuntimeError):
    """Malformed boundary input (fail closed)."""


class DispatchPinViolation(DispatchBoundaryError):
    """The invocation does not match the decision's pin: refused BEFORE dispatch.

    Raised instead of calling the model. This is the "runtime executes on a target
    different from the receipt" control, and it fires before the network call so a
    mismatch costs nothing and cannot half-happen.
    """

    def __init__(self, code: str, reason: str, *, decision_id: str = ""):
        self.code = code
        self.reason = reason
        self.decision_id = decision_id
        super().__init__(f"{code}: {reason}")

    def to_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason,
                "decision_id": self.decision_id}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(payload: object) -> bytes:
    """PS-638's canonical form, byte-for-byte (see dispatch_routing._canonical)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _json_list(value: Any) -> List[str]:
    """Parse a JSON list column defensively; a malformed value yields []."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    if isinstance(parsed, list):
        return [str(v) for v in parsed]
    return []


def _host_of(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(str(url or "")).hostname or ""


def _endpoint_identity(profile: Any) -> str:
    from src.endpoint_identity import canonical_endpoint_identity

    return canonical_endpoint_identity(
        getattr(profile, "endpoint_url", ""),
        getattr(profile, "endpoint_type", "")) if getattr(
        profile, "endpoint_url", "") else ""


_PROFILE_EXECUTION_FIELDS = (
    "provider", "runtime_kind", "runtime_version", "runtime_commit",
    "runtime_image_digest", "model", "model_digest", "backend",
    "backend_version", "endpoint_url", "endpoint_type", "runtime_options", "credential_sha256",
    "configured_context", "configured_served_context",
    "locality",
)


def _profile_execution_material(profile: Any) -> dict:
    return {
        name: (dict(getattr(profile, name))
               if name == "runtime_options"
               else getattr(profile, name))
        for name in _PROFILE_EXECUTION_FIELDS
    }


def _canonical_profile_from_receipt(profile: Any, receipt: Any) -> Any:
    """Rebuild the execution portion of a profile from PS-632 evidence."""
    from src.local_targets import PRIVACY_LOCAL_ONLY

    from src.subscription_capacity import subscription_endpoint_identity
    subscription = subscription_endpoint_identity(receipt.runtime.endpoint_url)
    locality = (LOCALITY_LOCAL if receipt.locality == PRIVACY_LOCAL_ONLY
                else receipt.locality)
    return dataclasses.replace(
        profile,
        provider=subscription[0] if subscription else receipt.runtime.provider,
        runtime_kind=receipt.runtime.runtime_kind,
        runtime_version=receipt.runtime.version,
        runtime_commit=receipt.runtime.commit,
        runtime_image_digest=receipt.runtime.image_digest,
        model=receipt.model.model_id,
        model_digest=receipt.model.digest,
        backend=receipt.runtime.backend,
        backend_version=receipt.runtime.backend_version,
        endpoint_url=subscription[1] if subscription else receipt.runtime.endpoint_url,
        endpoint_type=receipt.runtime.endpoint_type,
        runtime_options=receipt.context.options,
        configured_context=receipt.context.configured_context,
        configured_served_context=receipt.context.configured_served_context,
        locality=locality)


# ------------------------------------------------------------ the estate ---
@dataclass(frozen=True)
class TargetEstate:
    """The candidate estate as PS-605 sees it: profiles + their receipts + notes.

    ``provenance`` lives on each receipt, so a mixed estate (one measured profile,
    the rest declared) is representable and the decision records which one it
    relied on. ``skipped`` names candidates that could not become profiles at all,
    so a dropped candidate is visible instead of merely absent.
    """

    profiles: Tuple[Any, ...] = ()
    receipts: Tuple[Any, ...] = ()
    skipped: Tuple[Mapping[str, str], ...] = ()
    candidates: Tuple[Mapping[str, Any], ...] = ()

    def receipt_for(self, profile_id: str) -> Optional[Any]:
        for receipt in self.receipts:
            if receipt.profile_id == profile_id:
                return receipt
        return None

    def profile_for(self, profile_id: str) -> Optional[Any]:
        for profile in self.profiles:
            if profile.profile_id == profile_id:
                return profile
        return None

    def provenance_summary(self) -> Dict[str, str]:
        return {r.profile_id: r.provenance for r in self.receipts}


def _budget_class(row: Any) -> str:
    if getattr(row, "is_premium", False):
        return "premium"
    if getattr(row, "is_free", False):
        return "free"
    return "paid"


def _cost_rank(row: Any) -> int:
    if getattr(row, "is_free", False):
        return 0
    if getattr(row, "is_premium", False):
        return 2
    return 1


def profiles_from_candidates(db: Any, candidates: Sequence[Mapping[str, Any]], *,
                             ttl_s: int = 86400,
                             now: Optional[datetime] = None,
                             receipt_overrides: Optional[Mapping[str, Any]] = None,
                             network_classes: Optional[Mapping[str, str]] = None,
                             capability_store: Any = None
                             ) -> TargetEstate:
    """Project DB rows + candidate dicts into PS-605 profiles and receipts.

    A candidate whose profile row is missing/disabled, or whose endpoint is absent,
    is SKIPPED and recorded — it cannot be a candidate, and silently dropping it
    would make the decision's candidate list disagree with the estate it decided on.

    Production callers must provide ``capability_store``. Database rows are only
    discovery. There is deliberately no fixture escape hatch here: tests must
    create canonical PS-632 receipts in a test store.
    """
    from core.database import ModelEndpoint, RoutingModelProfile
    from src.routing_engine import endpoint_is_local

    overrides = dict(receipt_overrides or {})
    profiles: List[Any] = []
    receipts: List[Any] = []
    skipped: List[Dict[str, str]] = []
    kept: List[Mapping[str, Any]] = []

    for candidate in candidates or ():
        profile_id = str(candidate.get("profile_id") or "")
        row = db.get(RoutingModelProfile, profile_id) if profile_id else None
        if row is None or not getattr(row, "enabled", False):
            skipped.append({"profile_id": profile_id,
                            "reason": "missing_or_disabled_profile"})
            continue
        endpoint = (db.get(ModelEndpoint, row.model_endpoint_id)
                    if row.model_endpoint_id else None)
        if endpoint is None or not getattr(endpoint, "is_enabled", False):
            skipped.append({"profile_id": profile_id,
                            "reason": BOUNDARY_REFUSED_NO_ENDPOINT})
            continue

        base_url = str(getattr(endpoint, "base_url", "") or "")
        locality = (LOCALITY_LOCAL if endpoint_is_local(base_url)
                    else LOCALITY_HOSTED)
        roles = frozenset(_json_list(row.roles))
        supports_tools = getattr(endpoint, "supports_tools", None) is True
        target_id = f"profile:{row.id}"
        profile = make_target_profile(
            target_id=target_id, profile_id=row.id,
            provider=str(getattr(endpoint, "name", "") or "endpoint").strip().lower()
            .replace(" ", "-"),
            host=_host_of(base_url),
            runtime_kind=str(getattr(endpoint, "endpoint_kind", "") or "openai_compatible"),
            model=str(row.model or ""), locality=locality, roles=roles,
            # The URL matters: the domain layer decides locality from the endpoint
            # AND the provider, so omitting it silently marks every target remote.
            endpoint_url=base_url,
            endpoint_type=str(getattr(endpoint, "endpoint_kind", "") or ""),
            # Silence is not a grant: supports_tools NULL/False grants no tools.
            tools=frozenset({"write_file"}) if supports_tools else frozenset(),
            # A registry that already names the network class (PS-632's
            # ``NETWORK_TAILNET``) wins: the packet, the profile and the receipt must
            # agree on one string, and inventing a second spelling here is how a
            # constraint silently stops matching.
            network_policy=(dict(network_classes or {}).get(row.id)
                            or ("local-network" if locality == LOCALITY_LOCAL
                                else "hosted-egress")),
            budget_class=_budget_class(row), cost_rank=_cost_rank(row),
            inference=bool(roles & INFERENCE_ROLE_NAMES))

        if capability_store is None:
            skipped.append({"profile_id": profile_id,
                            "reason": "unqualified_candidate"})
            continue
        if row.id in overrides:
            skipped.append({"profile_id": profile_id,
                            "reason": "legacy capability override is not a PS-632 receipt"})
            continue
        try:
            canonical = capability_store.current(row.id)
        except Exception as exc:
            skipped.append({"profile_id": profile_id,
                            "reason": f"capability_store_untrusted:{exc}"})
            continue
        if canonical is None:
            skipped.append({"profile_id": profile_id,
                            "reason": "unqualified_candidate"})
            continue
        if canonical.profile_id != row.id:
            skipped.append({"profile_id": profile_id,
                            "reason": "canonical_profile_mismatch"})
            continue
        from src.subscription_capacity import subscription_endpoint_identity
        endpoint_identity = subscription_endpoint_identity(base_url)
        canonical_identity = subscription_endpoint_identity(canonical.runtime.endpoint_url)
        if (canonical.runtime.endpoint_url != base_url and
                not (endpoint_identity and endpoint_identity == canonical_identity)):
            skipped.append({"profile_id": profile_id,
                            "reason": "canonical_endpoint_mismatch"})
            continue
        if canonical.qualification_state(now=now) != "valid":
            skipped.append({"profile_id": profile_id,
                            "reason": "unqualified_candidate"})
            continue
        from src.local_target_routing import _legacy_view_from_receipt
        # The qualified receipt, not the mutable discovery row, supplies the
        # exact execution identity pinned into the decision.
        profile = _canonical_profile_from_receipt(profile, canonical)
        if profile.locality == LOCALITY_HOSTED:
            from src.endpoint_resolver import resolve_endpoint_runtime, build_chat_url, build_headers
            from src.offer_economics import credential_fingerprint
            try:
                resolved_base, resolved_key = resolve_endpoint_runtime(endpoint)
                if build_chat_url(resolved_base) != profile.endpoint_url:
                    raise ValueError("resolved hosted endpoint differs from qualified endpoint")
                profile = dataclasses.replace(profile, credential_sha256=credential_fingerprint(build_headers(resolved_key, resolved_base)))
            except Exception:
                skipped.append({"profile_id": profile_id, "reason": "hosted_credential_scope_unresolved"})
                continue
        profiles.append(profile)
        receipts.append(_legacy_view_from_receipt(canonical, profile, now=now))
        kept.append(candidate)

    return TargetEstate(profiles=tuple(profiles), receipts=tuple(receipts),
                        skipped=tuple(skipped), candidates=tuple(kept))


def _sensitivity_local_only(task: Any) -> bool:
    """True when the task's sensitivity ranks above the policy's remote ceiling."""
    from src.routing_engine import sensitivity_requires_local_only

    return sensitivity_requires_local_only(
        getattr(task, "data_sensitivity", None))


def execution_package_hash(task: Any) -> str:
    """Content identity of the routing intent, from the task's OWN fields.

    PS-638's ExecutionPackage owns the full envelope; that package does not exist on
    this branch, so the binding is over exactly the task fields the request was
    derived from — a stable, re-derivable digest rather than a placeholder string.
    """
    fields = {name: getattr(task, name, None) for name in (
        "id", "work_item_id", "title", "objective", "task_type", "repo_path",
        "branch_name", "risk", "constraints", "inputs", "data_sensitivity",
        "verification_mode", "max_cost_usd", "allow_free_models",
        "allow_paid_models", "allow_premium_models", "max_attempts")}
    return _sha256_hex(_canonical(fields))


def route_request_from_task(task: Any, *, role: str,
                            domain: Optional[str] = None,
                            capabilities: Sequence[str] = (),
                            exactness: str = EXACTNESS_EXACT,
                            required_tools: Sequence[str] = (),
                            network_policy: str = "",
                            preferred_profile_ids: Sequence[str] = (),
                            max_cost_rank: Optional[int] = None,
                            packet_id: str = "",
                            execution_package_hash_: str = "",
                            run_id: str = "") -> RoutingRequest:
    """Build the PS-605 request from a RoutingTask row (its OWN fields, not args).

    ``local_only`` comes from the task's ``data_sensitivity`` and the policy's
    remote ceiling — the same rule the existing hard filter applies, now expressed
    as an INPUT to the routing decision instead of as a parallel filter beside it.
    ``max_cost_rank`` defaults to what the task's own allow_free/paid/premium flags
    permit, so a free-only task cannot be routed to a paid profile no matter who
    calls this function.
    """
    if max_cost_rank is None:
        if getattr(task, "allow_premium_models", False):
            max_cost_rank = 2
        elif getattr(task, "allow_paid_models", False):
            max_cost_rank = 1
        else:
            max_cost_rank = 0
    return RoutingRequest(
        domain=str(domain or "general_swe"), role=role,
        packet_id=packet_id or str(getattr(task, "id", "") or ""),
        run_id=run_id,
        # A caller that already sealed a PS-638 ExecutionPackage passes ITS hash:
        # the receipt must bind the package that will actually carry it, not a
        # digest re-derived from task columns.
        execution_package_hash=(execution_package_hash_
                                or execution_package_hash(task)),
        capabilities=tuple(capabilities), exactness=exactness,
        sensitivity=str(getattr(task, "data_sensitivity", None) or "internal"),
        local_only=_sensitivity_local_only(task),
        required_tools=tuple(required_tools), network_policy=network_policy,
        max_cost_rank=int(max_cost_rank),
        preferred_profile_ids=tuple(preferred_profile_ids))


# --------------------------------------------------- the bound dispatch ---
@dataclass(frozen=True)
class BoundDispatch:
    """A decision together with the estate and request it was made from.

    The dispatcher needs all three: the REQUEST (what the work needs), the ESTATE
    (what exists, with provenance and skips) and the DECISION (what may run). They
    travel as one value so a caller cannot pair a decision with a different
    candidate list by accident.
    """

    request: RoutingRequest
    estate: TargetEstate
    decision: Any
    policy: Any
    capacity_receipts: Tuple[Any, ...] = ()
    offer_quotes: Tuple[Mapping[str, Any], ...] = ()

    def execution_order(self,
                        candidates: Sequence[Mapping[str, Any]]
                        ) -> List[Mapping[str, Any]]:
        """The candidates a dispatcher may attempt, in the decision's order.

        Members only: a candidate the decision did not find eligible is not
        attempted at all, so there is no point after the decision at which the
        dispatcher could choose a target the decision refused.
        """
        by_profile = {str(c.get("profile_id")): c for c in candidates or ()}
        ordered: List[Mapping[str, Any]] = []
        for assessment in self.decision.candidates:
            if not assessment.eligible:
                continue
            candidate = by_profile.get(assessment.profile_id)
            if candidate is not None:
                ordered.append(candidate)
        return ordered

    def pin_for(self, profile_id: str) -> Optional[Mapping[str, Any]]:
        """The pinned identity for an eligible profile, or None if not eligible."""
        for assessment in self.decision.candidates:
            if assessment.profile_id == profile_id and assessment.eligible:
                profile = self.estate.profile_for(profile_id)
                if profile is None:
                    return None
                return {
                    "receipt_hash": self.decision.receipt_hash,
                    "run_id": self.request.run_id,
                    "packet_id": self.request.packet_id,
                    "execution_package_hash": self.request.execution_package_hash,
                    "target_id": profile.target_id, "profile_id": profile.profile_id,
                    "provider": profile.provider, "host": profile.host,
                    "model": profile.model, "runtime_kind": profile.runtime_kind,
                    "runtime_version": profile.runtime_version,
                    "runtime_commit": profile.runtime_commit,
                    "runtime_image_digest": profile.runtime_image_digest,
                    "model_digest": profile.model_digest,
                    "backend": profile.backend,
                    "backend_version": profile.backend_version,
                    "locality": profile.locality, "endpoint_url": profile.endpoint_url,
                    "endpoint_type": profile.endpoint_type,
                    "credential_sha256": profile.credential_sha256,
                    "endpoint_identity": _endpoint_identity(profile),
                    "runtime_options": dict(profile.runtime_options),
                    "configured_context": profile.configured_context,
                    "configured_served_context": profile.configured_served_context,
                    "selected": (assessment.profile_id
                                 == self.decision.selected_profile.profile_id),
                }
        return None

    def receipt_hash(self) -> str:
        return self.decision.receipt_hash

    def to_dict(self) -> dict:
        return {
            "request": self.request.to_dict(),
            "decision": self.decision.to_dict(),
            "policy": self.policy.to_dict(),
            "provenance": self.estate.provenance_summary(),
            "skipped_candidates": [dict(s) for s in self.estate.skipped],
        }


def role_for_task(task: Any, estate: "TargetEstate",
                  available_roles: Sequence[str] = ()) -> str:
    """The role the run is routed as: the task's FIRST supported preference.

    Uses the routing layer's public role-preference contract
    (``routing_engine.roles_for_task_type``) and the estate's declared roles, so the
    request asks for what the task MEANS rather than for what happens
    to be available — the difference matters when nothing supports it, which is a
    refusal rather than a downgrade.
    """
    from src.routing_engine import roles_for_task_type

    declared: set = set(available_roles)
    for profile in estate.profiles:
        declared |= set(profile.roles)
    preferences = roles_for_task_type(getattr(task, "task_type", ""))
    for role in preferences:
        if role in declared:
            return role
    return (preferences or ["scout"])[0]


def resolve_dispatch(db: Any, task: Any, candidates: Sequence[Mapping[str, Any]], *,
                     role: Optional[str] = None,
                     domain: Optional[str] = None,
                     capabilities: Sequence[str] = (),
                     exactness: str = EXACTNESS_EXACT,
                     required_tools: Sequence[str] = (),
                     network_policy: str = "",
                     preferred_profile_ids: Sequence[str] = (),
                     max_cost_rank: Optional[int] = None,
                     execution_package_hash_: str = "",
                     run_id: str = "",
                     packet_id: str = "",
                     receipt_overrides: Optional[Mapping[str, Any]] = None,
                     network_classes: Optional[Mapping[str, str]] = None,
                     ttl_s: int = 86400,
                     policy: Any = None,
                     resources: Optional[Mapping[str, Mapping[str, Any]]] = None,
                     capability_store: Any = None,
                     capacity_receipts: Optional[Sequence[Any]] = None,
                     workload: Optional[Mapping[str, Any]] = None,
                     offer_identity: Optional[Mapping[str, Any]] = None,
                     now: Optional[datetime] = None,
                     decision_id: str = "") -> BoundDispatch:
    """Resolve the routing decision the dispatcher must obey. Raises on refusal.

    Raises :class:`RoutingRefused` (carrying the per-candidate reasons) when no
    candidate survives, and :class:`DispatchBoundaryError` when there is nothing to
    decide over at all. Both are fail-closed: the caller must not dispatch.
    """
    snapshot = policy or policy_snapshot()
    estate = profiles_from_candidates(
        db, candidates, ttl_s=ttl_s, now=now, receipt_overrides=receipt_overrides,
        network_classes=network_classes, capability_store=capability_store)
    if not estate.profiles:
        raise DispatchBoundaryError(
            f"{BOUNDARY_REFUSED_NO_PROFILES}: no candidate resolved to an enabled "
            "profile with an enabled endpoint (skipped: "
            f"{[dict(s) for s in estate.skipped]})")

    resolved_role = role or role_for_task(task, estate)
    request = route_request_from_task(
        task, role=resolved_role, domain=domain, capabilities=capabilities,
        exactness=exactness, required_tools=required_tools,
        network_policy=network_policy, preferred_profile_ids=preferred_profile_ids,
        max_cost_rank=max_cost_rank, execution_package_hash_=execution_package_hash_,
        run_id=run_id, packet_id=packet_id)
    # One selection path: the DB-backed resolver binds through the same function a
    # registry-backed caller uses, so there is exactly one place selection happens.
    return resolve_from_estate(estate, request, capability_store=capability_store,
                               network_classes=network_classes,
                               policy=snapshot, resources=resources,
                               capacity_receipts=capacity_receipts or (), now=now,
                               workload=workload, offer_identity=offer_identity,
                               decision_id=decision_id)


def resolve_from_estate(estate: TargetEstate, request: RoutingRequest, *,
                        capability_store: Any = None,
                        network_classes: Optional[Mapping[str, str]] = None,
                        policy: Any = None,
                        resources: Optional[Mapping[str, Mapping[str, Any]]] = None,
                        capacity_receipts: Optional[Sequence[Any]] = None,
                        workload: Optional[Mapping[str, Any]] = None,
                        offer_identity: Optional[Mapping[str, Any]] = None,
                        now: Optional[datetime] = None,
                        decision_id: str = "") -> BoundDispatch:
    """Bind an already-built estate + request to a decision: no DB, no task row.

    The DB-backed :func:`resolve_dispatch` is one caller of this; a caller whose
    estate comes from somewhere else (PS-632's measured fleet, a fixture, a replay)
    uses this directly rather than fabricating a task row to satisfy the other.
    """
    if capability_store is None:
        raise DispatchBoundaryError(
            "canonical capability store is required; an estate projection or "
            "legacy capability view cannot authorize dispatch")
    from src.local_target_routing import _legacy_view_from_receipt
    canonical_receipts = []
    canonical_profiles = []
    for profile in estate.profiles:
        if dict(getattr(profile, "execution_options", {}) or {}):
            raise DispatchBoundaryError(
                "invocation_options_unbound: execution_options must come from "
                "an authorized request/decision, not an estate profile")
        try:
            canonical = capability_store.current(profile.profile_id)
        except Exception as exc:
            raise DispatchBoundaryError(f"canonical capability store unusable: {exc}")
        if canonical is None or canonical.profile_id != profile.profile_id:
            raise DispatchBoundaryError(
                f"unqualified candidate {profile.profile_id!r}: no current PS-632 receipt")
        if canonical.qualification_state(now=now) != "valid":
            raise DispatchBoundaryError(
                f"unqualified candidate {profile.profile_id!r}: receipt is not valid")
        canonical_profile = _canonical_profile_from_receipt(profile, canonical)
        if (_profile_execution_material(profile)
                != _profile_execution_material(canonical_profile)):
            raise DispatchBoundaryError(
                f"profile_receipt_identity_mismatch for {profile.profile_id!r}")
        canonical_profiles.append(canonical_profile)
        canonical_receipts.append(
            _legacy_view_from_receipt(canonical, canonical_profile, now=now))
    estate = dataclasses.replace(estate, profiles=tuple(canonical_profiles),
                                receipts=tuple(canonical_receipts))
    snapshot = policy or policy_snapshot()
    if not estate.profiles:
        raise DispatchBoundaryError(
            f"{BOUNDARY_REFUSED_NO_PROFILES}: the estate has no profiles to decide "
            f"over (skipped: {[dict(s) for s in estate.skipped]})")
    if network_classes:
        classes = dict(network_classes)
        estate = TargetEstate(
            profiles=tuple(
                dataclasses.replace(
                    profile, network_policy=classes.get(profile.profile_id)
                    or profile.network_policy)
                for profile in estate.profiles),
            receipts=estate.receipts, skipped=estate.skipped,
            candidates=estate.candidates)
    capacity = tuple(capacity_receipts or ())
    decision = select_target(
        request, profiles=estate.profiles, receipts=estate.receipts,
        policy=snapshot, resources=resources,
        capacity_receipts=capacity, now=now, decision_id=decision_id)
    from src.offer_economics import configured_quote, quote_digest
    selected = decision.selected_profile
    quote = configured_quote(profile_id=selected.profile_id, model=selected.model,
        chat_url=selected.endpoint_url, harness=selected.runtime_kind, provider=selected.provider,
        workload=workload, now=datetime.fromisoformat(decision.observed_at.replace("Z", "+00:00")), **dict(offer_identity or {}))
    if quote is not None:
        from decimal import Decimal
        if Decimal(quote["predicted_cash_usd"]) > 0 and request.max_cost_rank < 1:
            raise DispatchBoundaryError("dispatch request does not authorize paid offers")
        if quote["capacity_receipt_ref"] not in {r.ref for r in capacity}:
            raise DispatchBoundaryError("offer capacity is not bound to this dispatch")
        decision = dataclasses.replace(decision, offer_receipt_refs=tuple(quote["offer_refs"]),
                                       offer_quote_digests=(quote_digest(quote),))
        decision = dataclasses.replace(decision, receipt_hash=ps638_receipt_hash(decision.to_ps638_receipt_kwargs()))
    return BoundDispatch(request=request, estate=estate, decision=decision,
                         policy=snapshot, capacity_receipts=capacity,
                         offer_quotes=(quote,) if quote else ())


@dataclass(frozen=True)
class InvocationIdentity:
    """Facts observed by the adapter immediately before making a call."""

    profile_id: str
    provider: str
    runtime_kind: str
    runtime_version: str
    runtime_commit: str
    runtime_image_digest: str
    backend: str
    backend_version: str
    model: str
    model_digest: str
    chat_url: str
    endpoint_type: str
    locality: str
    runtime_options: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    execution_options: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    configured_context: int = 0
    configured_served_context: int = 0
    credential_sha256: str = ""
    workload: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    offer_identity: Mapping[str, Any] = dataclasses.field(default_factory=dict)


def verify_invocation(bound: BoundDispatch, *,
                      invocation: InvocationIdentity) -> Mapping[str, Any]:
    """Refuse an invocation that does not match the decision. Call BEFORE dispatch.

    Three ways to fail, each with its own code: the profile is not in the decision's
    eligible set, the resolved MODEL is not the pinned model, or the resolved
    endpoint's LOCALITY contradicts the pinned locality (a local pin resolving to a
    remote URL is precisely the failure this exists to stop).
    """
    from src.endpoint_identity import canonical_endpoint_identity, EndpointIdentityError
    from src.routing_engine import endpoint_is_local

    profile_id = invocation.profile_id
    pin = bound.pin_for(profile_id)
    if pin is None:
        raise DispatchPinViolation(
            PIN_PROFILE_MISMATCH,
            f"profile {profile_id!r} is not in the decision's eligible set",
            decision_id=bound.decision.decision_id)
    pinned_model = str(pin.get("model") or "")
    if pinned_model and invocation.model != pinned_model:
        raise DispatchPinViolation(
            PIN_MODEL_MISMATCH,
            f"resolved model {invocation.model!r} is not the pinned model {pinned_model!r}",
            decision_id=bound.decision.decision_id)
    if pin.get("locality") == LOCALITY_HOSTED and (
            not pin.get("credential_sha256") or invocation.credential_sha256 != pin["credential_sha256"]):
        raise DispatchPinViolation(PIN_PROFILE_MISMATCH, "resolved hosted credentials differ from capacity scope",
                                   decision_id=bound.decision.decision_id)
    resolved_local = endpoint_is_local(invocation.chat_url)
    if resolved_local != (pin.get("locality") == LOCALITY_LOCAL):
        raise DispatchPinViolation(
            PIN_LOCALITY_MISMATCH,
            "resolved endpoint locality "
            f"({'local' if resolved_local else 'hosted'}) contradicts the pinned "
            f"locality {pin.get('locality')!r}",
            decision_id=bound.decision.decision_id)
    expected_endpoint = str(pin.get("endpoint_identity") or "")
    try:
        actual_endpoint = canonical_endpoint_identity(
            invocation.chat_url, invocation.endpoint_type)
    except EndpointIdentityError as exc:
        raise DispatchPinViolation(
            PIN_ENDPOINT_MISMATCH, str(exc),
            decision_id=bound.decision.decision_id) from exc
    if expected_endpoint and actual_endpoint != expected_endpoint:
        raise DispatchPinViolation(
            PIN_ENDPOINT_MISMATCH,
            f"resolved endpoint {actual_endpoint!r} is not the pinned endpoint",
            decision_id=bound.decision.decision_id)
    checks = {
        "provider": invocation.provider,
        "runtime_kind": invocation.runtime_kind,
        "runtime_version": invocation.runtime_version,
        "runtime_commit": invocation.runtime_commit,
        "runtime_image_digest": invocation.runtime_image_digest,
        "backend": invocation.backend,
        "backend_version": invocation.backend_version,
        "model_digest": invocation.model_digest,
        "endpoint_type": invocation.endpoint_type,
        "locality": invocation.locality,
        "runtime_options": dict(invocation.runtime_options),
        "configured_context": invocation.configured_context,
        "configured_served_context": invocation.configured_served_context,
    }
    if dict(invocation.execution_options or {}):
        raise DispatchPinViolation(
            PIN_RUNTIME_MISMATCH,
            "invocation execution_options have no canonical request authority",
            decision_id=bound.decision.decision_id)
    for name, actual in checks.items():
        expected = pin.get(name)
        if expected in (None, "", {}, 0) and actual in (None, "", {}, 0):
            continue
        if expected != actual:
            raise DispatchPinViolation(
                PIN_RUNTIME_MISMATCH,
                f"invocation {name} {actual!r} is not the pinned value {expected!r}",
                decision_id=bound.decision.decision_id)
    from src.promotional_dispatch import enforce_free_offer
    enforce_free_offer(profile_id=profile_id, model=invocation.model,
                       chat_url=invocation.chat_url, harness=invocation.runtime_kind,
                       provider=invocation.provider, **dict(invocation.offer_identity))
    from src.offer_economics import configured_quote
    prior_quote = next((q for q in bound.offer_quotes if q["profile_id"] == profile_id), None)
    if prior_quote is not None and dict(invocation.workload) != prior_quote["workload"]:
        raise DispatchPinViolation(PIN_RUNTIME_MISMATCH, "actual invocation workload differs from selected quote",
                                   decision_id=bound.decision.decision_id)
    current_quote = configured_quote(profile_id=profile_id, model=invocation.model,
        chat_url=invocation.chat_url, harness=invocation.runtime_kind, provider=invocation.provider,
        workload=dict(invocation.workload) or None, **dict(invocation.offer_identity))
    if (prior_quote is None) != (current_quote is None):
        raise DispatchPinViolation(PIN_RUNTIME_MISMATCH, "offer policy changed after selection",
                                   decision_id=bound.decision.decision_id)
    if prior_quote is not None:
        for field in ("offer_refs", "capacity_receipt_ref", "predicted_cash_usd", "maximum_predicted_request_usd", "workload", "credential_sha256", "endpoint_id", "transport_provider", "account_identity"):
            if prior_quote[field] != current_quote[field]:
                raise DispatchPinViolation(PIN_RUNTIME_MISMATCH, "offer binding changed after selection",
                                           decision_id=bound.decision.decision_id)
    return pin


# --------------------------------------------------------- attempt binding ---
#: The AttemptReceipt fields PS-638 requires that carry the routing identity. A
#: receipt missing any of these cannot be tied to a decision, so the tuple is
#: asserted by the tests as a contract rather than described in prose.
PS638_ATTEMPT_BINDING_FIELDS: Tuple[str, ...] = (
    "run_id", "packet_id", "attempt", "execution_package_hash",
    "dispatch_receipt_hash", "target_id", "host", "model", "runtime_kind",
)


@dataclass(frozen=True)
class DispatchAttempt:
    """One attempt, bound to the exact dispatch receipt that authorised it."""

    run_id: str
    packet_id: str
    execution_package_hash: str
    attempt: int
    dispatch_receipt_hash: str
    decision_id: str
    target_id: str
    profile_id: str
    host: str = ""
    model: str = ""
    runtime_kind: str = ""
    model_digest: str = ""
    locality: str = ""
    selected: bool = False
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.dispatch_receipt_hash:
            raise DispatchBoundaryError(
                "an attempt must carry the dispatch_receipt_hash that authorised it")
        if int(self.attempt) < 1:
            raise DispatchBoundaryError("attempt must be >= 1")

    def to_dict(self) -> dict:
        return {"schema_version": self.schema_version, "run_id": self.run_id,
                "packet_id": self.packet_id,
                "execution_package_hash": self.execution_package_hash,
                "attempt": int(self.attempt),
                "dispatch_receipt_hash": self.dispatch_receipt_hash,
                "decision_id": self.decision_id, "target_id": self.target_id,
                "profile_id": self.profile_id, "host": self.host,
                "model": self.model, "runtime_kind": self.runtime_kind,
                "model_digest": self.model_digest, "locality": self.locality,
                "selected": self.selected}


def attempt_for(bound: BoundDispatch, *, attempt: int, profile_id: str,
                run_id: str = "", host: str = "", model: str = "",
                runtime_kind: str = "", model_digest: str = "") -> DispatchAttempt:
    """Build the attempt record for one candidate, bound to the decision's receipt."""
    profile = bound.estate.profile_for(profile_id)
    if profile is None:
        raise DispatchPinViolation(
            PIN_PROFILE_MISMATCH,
            f"cannot bind an attempt for unknown profile {profile_id!r}",
            decision_id=bound.decision.decision_id)
    return DispatchAttempt(
        run_id=run_id, packet_id=bound.request.packet_id,
        execution_package_hash=bound.request.execution_package_hash,
        attempt=int(attempt), dispatch_receipt_hash=bound.decision.receipt_hash,
        decision_id=bound.decision.decision_id, target_id=profile.target_id,
        profile_id=profile.profile_id, host=host or profile.host,
        model=model or profile.model, runtime_kind=runtime_kind or profile.runtime_kind,
        model_digest=model_digest, locality=profile.locality,
        selected=(profile.profile_id
                  == bound.decision.selected_profile.profile_id))


class InvocationRecorder:
    """Records every invocation the dispatcher actually performed.

    The point is the negative: a local-only dispatch must show ZERO hosted
    invocations, and that is only checkable if something wrote down every attempt.
    Adapters report here; nothing else does.
    """

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []

    def record(self, *, target_id: str, locality: str, model: str = "",
               endpoint_host: str = "", ok: bool = True, detail: str = "",
               attempt: int = 0) -> Dict[str, Any]:
        entry = {"target_id": target_id, "locality": locality, "model": model,
                 "endpoint_host": endpoint_host, "ok": bool(ok),
                 "detail": str(detail)[:300], "attempt": int(attempt),
                 "observed_at": _utc_now()}
        self.records.append(entry)
        return entry

    def attempted(self) -> int:
        return len(self.records)

    def hosted(self) -> List[Dict[str, Any]]:
        return [r for r in self.records if r["locality"] == LOCALITY_HOSTED]

    def local(self) -> List[Dict[str, Any]]:
        return [r for r in self.records if r["locality"] == LOCALITY_LOCAL]

    def assert_no_hosted(self) -> None:
        hosted = self.hosted()
        if hosted:
            raise DispatchBoundaryError(
                f"a local-only dispatch performed {len(hosted)} hosted "
                f"invocation(s): {hosted}")


# ------------------------------------------------------------- the evidence ---
def seal_dispatch_evidence(bound: BoundDispatch, *,
                           attempts: Sequence[DispatchAttempt],
                           invocations: Sequence[Mapping[str, Any]],
                           budget_snapshot: Optional[Mapping[str, Any]] = None,
                           resource_snapshot: Optional[Mapping[str, Any]] = None,
                           fixture: Optional[Mapping[str, Any]] = None,
                           sealed_at: str = "") -> Dict[str, Any]:
    """Seal one dispatch into a content-addressed, re-checkable payload.

    Everything the decision was made from is in here, so the payload is not a
    summary of the decision — it is the inputs and the outcome together, which is
    what makes "change the selected target and the evidence is invalid" detectable
    rather than a matter of trust.
    """
    decision = bound.decision
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "sealed_at": sealed_at or _utc_now(),
        "execution_package": {
            "packet_id": bound.request.packet_id,
            "execution_package_hash": bound.request.execution_package_hash,
            "domain": bound.request.domain,
            "role": bound.request.role,
            "data_sensitivity": bound.request.sensitivity,
        },
        "policy": bound.policy.to_dict(),
        "route_request": bound.request.to_dict(),
        "decision": decision.to_dict(),
        "decision_receipt": decision.to_ps638_receipt_kwargs(),
        "capability_receipts": [r.to_dict() for r in bound.estate.receipts],
        "capability_provenance": bound.estate.provenance_summary(),
        "skipped_candidates": [dict(s) for s in bound.estate.skipped],
        "budget_snapshot": dict(budget_snapshot or {}),
        "resource_snapshot": dict(resource_snapshot or {}),
        "attempts": [a.to_dict() for a in attempts],
        "invocations": [dict(i) for i in invocations],
        "fixture": dict(fixture or {}),
    }
    if bound.capacity_receipts:
        payload["capacity_receipts"] = [
            getattr(r, "to_dict", lambda: dict(r))() for r in bound.capacity_receipts]
    if bound.offer_quotes:
        payload["offer_quotes"] = [dict(q) for q in bound.offer_quotes]
    payload["seal"] = {
        "evidence_hash": _sha256_hex(_canonical(payload)),
        "policy_ref": bound.policy.policy_ref,
        "dispatch_receipt_hash": decision.receipt_hash,
        "decision_hash": decision.decision_hash,
        "selected_target_id": decision.selected_profile.target_id,
        "selected_profile_id": decision.selected_profile.profile_id,
    }
    return payload


def evidence_core(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """The payload without its seal — what the seal hashes over."""
    return {k: v for k, v in dict(payload).items() if k != "seal"}


def _receipt_hash_of(recorded: Mapping[str, Any]) -> str:
    """Re-derive a recorded receipt's content hash from its OWN fields.

    This is what makes "the capability receipt was changed" detectable from the
    payload alone: the hash is recomputed from what is written down, so editing
    capabilities (or freshness, or health) without re-sealing breaks the link
    between the receipt and the hash the decision cited.
    """
    from src.dispatch_routing import make_legacy_capability_view

    try:
        return make_legacy_capability_view(
            receipt_id=str(recorded.get("receipt_id") or ""),
            profile_id=str(recorded.get("profile_id") or ""),
            target_id=str(recorded.get("target_id") or ""),
            capabilities=frozenset(recorded.get("capabilities") or ()),
            exactness=str(recorded.get("exactness") or ""),
            observed_at=str(recorded.get("observed_at") or ""),
            ttl_s=int(recorded.get("ttl_s") or 0),
            healthy=bool(recorded.get("healthy", True)),
            runtime_version=str(recorded.get("runtime_version") or ""),
            model_digest=str(recorded.get("model_digest") or ""),
            host=str(recorded.get("host") or ""),
            notes=str(recorded.get("notes") or ""),
            provenance=str(recorded.get("provenance") or PROVENANCE_DECLARED),
        ).receipt_hash
    except Exception:
        # An unreadable record cannot be re-derived, so it cannot be trusted.
        return ""



def validate_dispatch_evidence(payload: Mapping[str, Any]
                               ) -> Tuple[bool, Tuple[str, ...]]:
    """Re-derive every claim in a sealed payload. Returns (ok, (codes,)).

    Each check corresponds to a mutation that must NOT survive: a changed selected
    target, a changed capability receipt, a changed policy revision, an attempt
    bound to a different receipt, an invocation outside the decision, or a hosted
    invocation for local-only work.
    """
    codes: List[str] = []
    from src import provider_capacity
    body = evidence_core(payload)
    seal = dict(payload.get("seal") or {})
    decision = dict(body.get("decision") or {})
    receipt = dict(body.get("decision_receipt") or {})
    attempts = list(body.get("attempts") or ())
    invocations = list(body.get("invocations") or ())
    request = dict(body.get("route_request") or {})
    policy = dict(body.get("policy") or {})
    receipts = list(body.get("capability_receipts") or ())

    # 1. the seal itself
    if seal.get("evidence_hash") != _sha256_hex(_canonical(body)):
        codes.append(EVIDENCE_HASH_MISMATCH)

    # 2. the decision's receipt hash must be what PS-638 computes from the receipt
    #    fields recorded beside it: RE-DERIVED, not trusted.
    if seal.get("dispatch_receipt_hash") != ps638_receipt_hash(receipt):
        codes.append(EVIDENCE_DECISION_MISMATCH)

    # 3. the pin must still agree in ALL THREE places that record it: the seal, the
    #    PS-638 receipt, and the full decision. `selected_target_id` is not part of
    #    the receipt's core hash, so this agreement (not the hash) is what makes a
    #    changed pin detectable in a payload with no attempts.
    selected = str(receipt.get("selected_target_id") or "")
    decision_profile = str(dict(decision.get("selected_profile") or {})
                           .get("profile_id") or "")
    if (str(seal.get("selected_target_id") or "") != selected
            or str(seal.get("selected_profile_id") or "") != decision_profile
            or str(seal.get("selected_target_id") or "")
            != str(decision_profile and f"profile:{decision_profile}")
            or str(decision.get("receipt_hash") or "")
            != str(seal.get("dispatch_receipt_hash") or "")):
        codes.append(EVIDENCE_PIN_CHANGED)

    # 4. every attempt binds THIS receipt, and the attempts agree with the pin.
    for attempt in attempts:
        if attempt.get("dispatch_receipt_hash") != seal.get("dispatch_receipt_hash"):
            codes.append(EVIDENCE_ATTEMPT_UNBOUND)
            break
    if attempts and str(attempts[0].get("target_id") or "") != selected:
        codes.append(EVIDENCE_TARGET_MISMATCH)

    # 5. invocations must be inside the decision's eligible set and respect the
    #    locality the decision required.
    candidates = list(decision.get("candidates") or ())
    eligible = {str(c.get("target_id")) for c in candidates if c.get("eligible")}
    local_only = bool(request.get("local_only"))
    for invocation in invocations:
        if str(invocation.get("target_id")) not in eligible:
            codes.append(EVIDENCE_INVOCATION_OUTSIDE_DECISION)
        if local_only and invocation.get("locality") == LOCALITY_HOSTED:
            codes.append(EVIDENCE_HOSTED_FOR_LOCAL_ONLY)

    # 6. capability receipts must still be the ones the decision relied on, and
    #    their CONTENT must still hash to what the payload recorded: mutating a
    #    receipt's fields is caught by re-deriving its hash, not only by the seal.
    by_profile = {str(r.get("profile_id")): r for r in receipts}
    known_hashes = {str(r.get("receipt_hash")) for r in receipts}
    for ref in (receipt.get("capability_receipt_refs") or ()):
        if str(ref) not in known_hashes:
            codes.append(EVIDENCE_RECEIPT_CHANGED)
            break
    for candidate in candidates:
        if not candidate.get("eligible"):
            continue
        recorded = by_profile.get(str(candidate.get("profile_id")))
        if (recorded is None
                or str(recorded.get("receipt_hash") or "")
                != str(candidate.get("capability_receipt_hash") or "")):
            codes.append(EVIDENCE_RECEIPT_CHANGED)
            break
        if _receipt_hash_of(recorded) != str(recorded.get("receipt_hash") or ""):
            codes.append(EVIDENCE_RECEIPT_CHANGED)
            break


    # 7. capacity receipts (PS-640) must still hash to their recorded content, and
    #    every ref the decision cited must be present among the valid receipts.
    valid_capacity_refs: set = set()
    capacity_by_ref: Dict[str, Mapping[str, Any]] = {}
    for recorded in (body.get("capacity_receipts") or ()):
        if (not isinstance(recorded, dict)
                or not provider_capacity.capacity_receipt_hash_is_valid(recorded)):
            codes.append(EVIDENCE_CAPACITY_CHANGED)
            continue
        receipt_hash = str(recorded.get("receipt_hash") or "")
        if receipt_hash:
            ref = f"capacity:{receipt_hash}"
            valid_capacity_refs.add(ref)
            capacity_by_ref[ref] = recorded
    decision_capacity_refs = list(receipt.get("capacity_receipt_refs") or ())
    recorded_decision_refs = list(decision.get("capacity_receipt_refs") or ())
    selected_profile = dict(decision.get("selected_profile") or {})
    selected_is_hosted = selected_profile.get("locality") == LOCALITY_HOSTED
    if decision_capacity_refs != recorded_decision_refs:
        codes.append(EVIDENCE_CAPACITY_CHANGED)
    if selected_is_hosted and not decision_capacity_refs:
        codes.append(EVIDENCE_CAPACITY_CHANGED)
    for ref in decision_capacity_refs:
        if str(ref) not in valid_capacity_refs:
            codes.append(EVIDENCE_CAPACITY_CHANGED)
            break
    if selected_is_hosted and decision_capacity_refs:
        # Use the recorded selection instant, not current wall time. A historical
        # evidence review must not reject a receipt merely because it expired later.
        try:
            selected_at_text = str(decision.get("observed_at") or receipt.get("decided_at") or "")
            selected_at = datetime.fromisoformat(selected_at_text.replace("Z", "+00:00"))
            if selected_at.tzinfo is None or selected_at.utcoffset() is None:
                raise ValueError("selection time must include a timezone")
            selected_at = selected_at.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            selected_at = None
            codes.append(EVIDENCE_CAPACITY_CHANGED)
        for ref in decision_capacity_refs:
            recorded = capacity_by_ref.get(str(ref))
            if recorded is None:
                continue
            try:
                capacity = provider_capacity.capacity_receipt_from_dict(recorded)
                matches_selected = (
                    bool(selected_profile.get("provider"))
                    and capacity.provider == selected_profile.get("provider")
                    and bool(selected_profile.get("endpoint_url"))
                    and capacity.endpoint_url == selected_profile.get("endpoint_url")
                    and bool(selected_profile.get("credential_sha256"))
                    and capacity.credential_sha256 == selected_profile.get("credential_sha256")
                    and bool(selected_profile.get("model"))
                    and selected_profile.get("model") in capacity.exposed_models)
                accepted_entitlements = (provider_capacity.Entitlement.API,
                                         provider_capacity.Entitlement.AGENT_SDK)
                if (not matches_selected or selected_at is None
                        or capacity.entitlement not in accepted_entitlements
                        or not capacity.has_usable_capacity_facts(now=selected_at)):
                    codes.append(EVIDENCE_CAPACITY_CHANGED)
                    break
            except (TypeError, ValueError, KeyError, provider_capacity.CapacityError):
                codes.append(EVIDENCE_CAPACITY_CHANGED)
                break

    # Offer receipts are optional for legacy dispatches, but binding is strict
    # whenever present. Recompute quotes at the recorded observation time.
    from src.offer_economics import comparable_cash, quote_digest
    from src.provider_model_offer import provider_model_offer_from_dict
    valid_offer_refs = set()
    quote_digests = []
    for quote in body.get("offer_quotes") or ():
        try:
            offers = [provider_model_offer_from_dict(o) for o in quote["offers"]]
            if quote["capacity_receipt_ref"] not in valid_capacity_refs:
                raise ValueError("unbound capacity")
            capacity_fact = next(r for r in body["capacity_receipts"]
                                 if "capacity:" + r["receipt_hash"] == quote["capacity_receipt_ref"])
            if ((capacity_fact.get("endpoint_url") and capacity_fact["endpoint_url"] != quote.get("chat_url"))
                    or quote.get("credential_sha256") != capacity_fact.get("credential_sha256")
                    or quote.get("account_identity") != capacity_fact.get("account_identity")):
                raise ValueError("quote account/credential differs from capacity")
            at = datetime.fromisoformat(quote["observed_at"].replace("Z", "+00:00"))
            decided = datetime.fromisoformat(receipt["decided_at"].replace("Z", "+00:00"))
            if not 0 <= (decided - at).total_seconds() <= 5:
                raise ValueError("quote observation is not anchored to dispatch time")
            quote_digests.append(quote_digest(quote))
            if quote["offer_refs"] != [o.ref for o in offers] or any(not o.is_eligible(
                    now=at, provider=quote["provider"], pool_id=quote["pool_id"],
                    capacity_receipt_ref=quote["capacity_receipt_ref"], harness=quote["harness"],
                    usage_path=quote["usage_path"], native_model=quote["model"]) for o in offers):
                raise ValueError("invalid offer scope")
            from decimal import Decimal
            cash = comparable_cash(offers, quote["workload"])
            if cash != Decimal(quote["predicted_cash_usd"]) or cash > Decimal(quote["maximum_predicted_request_usd"]):
                raise ValueError("quote changed")
            selected = decision.get("selected_profile") or {}
            if (quote["profile_id"] != selected.get("profile_id") or quote["model"] != selected.get("model")
                    or quote["provider"] != selected.get("provider") or quote["harness"] != selected.get("runtime_kind")
                    or quote["chat_url"] != selected.get("endpoint_url")):
                raise ValueError("offer is not bound to selected execution identity")
            valid_offer_refs.update(o.ref for o in offers)
        except (KeyError, ValueError, TypeError, ArithmeticError):
            codes.append("offer_receipt_changed")
    if set(receipt.get("offer_receipt_refs") or ()) != valid_offer_refs:
        codes.append("offer_receipt_changed")
    if list(receipt.get("offer_quote_digests") or ()) != quote_digests:
        codes.append("offer_quote_changed")

    # 8. the policy revision AND its content hash must still match the ref.
    policy_ref = str(receipt.get("policy_ref") or "")
    if policy_ref != str(policy.get("policy_ref") or ""):
        codes.append(EVIDENCE_POLICY_CHANGED)
    elif policy_ref and f"@{policy.get('version')}+sha256:" not in policy_ref:
        codes.append(EVIDENCE_POLICY_CHANGED)
    elif str(policy.get("policy_hash") or "")[:16] not in policy_ref:
        codes.append(EVIDENCE_POLICY_CHANGED)

    return (not codes, tuple(dict.fromkeys(codes)))



def seal_recorded_dispatch(*, record: Mapping[str, Any],
                           budget_snapshot: Optional[Mapping[str, Any]] = None,
                           resource_snapshot: Optional[Mapping[str, Any]] = None,
                           fixture: Optional[Mapping[str, Any]] = None,
                           sealed_at: str = "") -> Dict[str, Any]:
    """Seal the dispatch RECORD a production run wrote to disk.

    `routing_executor` writes ``dispatch_receipt.json`` (the full PS-605 dispatch
    under ``dispatch``, the PS-638 receipt fields under ``dispatch_receipt``, the
    attempt bindings and the invocation log) next to each run. This turns that
    artifact into the same sealed payload `seal_dispatch_evidence` produces, so the
    production record — not a re-derivation of it — is what gets validated.
    """
    dispatch = dict(record.get("dispatch") or {})
    decision = dict(dispatch.get("decision") or {})
    request = dict(dispatch.get("request") or {})
    payload: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "sealed_at": sealed_at or _utc_now(),
        "execution_package": dict(record.get("execution_package") or {
            "packet_id": decision.get("packet_id", ""),
            "execution_package_hash": request.get("execution_package_hash", ""),
        }),
        "policy": dict(record.get("policy") or dispatch.get("policy") or {}),
        "route_request": request,
        "decision": decision,
        "decision_receipt": dict(record.get("dispatch_receipt") or {}),
        "capability_receipts": list(record.get("capability_receipts") or ()),
        "capability_provenance": dict(
            record.get("capability_provenance") or dispatch.get("provenance") or {}),
        "skipped_candidates": list(
            record.get("skipped_candidates")
            or dispatch.get("skipped_candidates") or ()),
        "budget_snapshot": dict(budget_snapshot or {}),
        "resource_snapshot": dict(resource_snapshot or {}),
        "attempts": list(record.get("attempts") or ()),
        "invocations": list(record.get("invocations") or ()),
        "fixture": dict(fixture or {}),
    }
    payload["seal"] = {
        "evidence_hash": _sha256_hex(_canonical(payload)),
        "policy_ref": payload["policy"].get("policy_ref", ""),
        "dispatch_receipt_hash": record.get("dispatch_receipt_hash", ""),
        "decision_hash": record.get("decision_hash", ""),
        "selected_target_id": (payload["decision_receipt"].get("selected_target_id")
                               or ""),
        "selected_profile_id": str(
            dict(payload["decision"].get("selected_profile") or {}).get(
                "profile_id", "")),
    }
    return payload


def write_dispatch_evidence(path: str, payload: Mapping[str, Any]) -> str:
    """Write a sealed payload to disk, creating parent directories."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True, default=str)
        handle.write("\n")
    return path
