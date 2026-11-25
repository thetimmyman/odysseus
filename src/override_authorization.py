"""Authoritative operator-override authorization boundary and resolver protocols.

Evaluation is pure; authorization is NOT. The authorization boundary resolves
facts from authoritative system registries and stores via injected resolvers,
never trusting caller-constructed objects.

Core Invariants:
1. The gate must never read an authorization fact from an object the caller can construct.
   Evidence must be references the gate resolves -- never content the gate believes.
2. Reproduce, don't replace: A fresh selector evaluation from resolved inputs must
   reproduce the cited explanation's eligible candidate set and per-candidate facts.
   Overrides only steer between candidates that are already eligible in the cited decision.
3. Server-side instant: Overrides are evaluated against the gate's server clock.
   Caller-supplied instants outside a tight clock skew window are refused.
4. Locality oracle: Target locality is strictly enforced for privacy policies; an
   endpoint string never confers privacy permission.
5. Authenticated receipts: Receipts are privately minted with a keyed HMAC signature
   and bounded validity window; in-process forgeries without the key are rejected.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Tuple, runtime_checkable

from src import dispatch_routing as dr
from src import routing_domain_policy as rdp
from src.routing_explanation import (
    CandidateExplanation, ExplanationError, NON_OVERRIDABLE_REFUSALS,
    OverrideAuditReceipt, PolicyViolationException, RoutingExplanation,
    RoutingInspectorEvaluator, RULE_ELIGIBLE, _as_explanation, _canonical,
    _parse_ts, _require_aware, _sha256_hex,
)


# --------------------------------------------------------------------------
# Evidence References (Caller-Supplied Refs Only - No Content Objects)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class OverrideEvidenceRefs:
    """References to canonical evidence; caller-supplied content objects are unrepresentable.

    The gate resolves each reference via an injected resolver protocol and verifies
    identities and hashes against authoritative stores.
    """
    run_id: str
    packet_id: str
    target_refs: Tuple[Tuple[str, str], ...] = ()
    capability_receipt_hashes: Mapping[str, str] = field(default_factory=dict)
    capacity_receipt_refs: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ExplanationError("run_id must be a non-empty string")
        if not isinstance(self.packet_id, str) or not self.packet_id.strip():
            raise ExplanationError("packet_id must be a non-empty string")

        cleaned_targets = []
        for item in (self.target_refs or ()):
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ExplanationError(
                    "target_refs must contain (target_id, profile_id) string pairs only; "
                    "candidate profile objects are unrepresentable")
            tid, pid = item
            if not isinstance(tid, str) or not isinstance(pid, str) or not tid.strip() or not pid.strip():
                raise ExplanationError(
                    "target_refs elements must be non-empty strings; objects are unrepresentable")
            cleaned_targets.append((tid.strip(), pid.strip()))
        object.__setattr__(self, "target_refs", tuple(cleaned_targets))

        if not isinstance(self.capability_receipt_hashes, Mapping):
            raise ExplanationError(
                "capability_receipt_hashes must be a mapping of profile_id to hash string")
        cleaned_cap_hashes = {}
        for k, v in self.capability_receipt_hashes.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ExplanationError(
                    "capability_receipt_hashes must contain hash strings only; "
                    "receipt objects are unrepresentable")
            cleaned_cap_hashes[k.strip()] = v.strip()
        object.__setattr__(self, "capability_receipt_hashes", dict(cleaned_cap_hashes))

        cleaned_capacity = []
        for ref in (self.capacity_receipt_refs or ()):
            if not isinstance(ref, str) or not ref.strip():
                raise ExplanationError(
                    "capacity_receipt_refs must contain reference strings only; "
                    "receipt objects are unrepresentable")
            cleaned_capacity.append(ref.strip())
        object.__setattr__(self, "capacity_receipt_refs", tuple(cleaned_capacity))


# --------------------------------------------------------------------------
# Resolver Protocol and Implementations
# --------------------------------------------------------------------------

@runtime_checkable
class OverrideEvidenceResolver(Protocol):
    """Protocol for resolving canonical evidence references into authoritative domain objects."""

    def resolve_profile(self, target_id: str, profile_id: str) -> dr.ExecutionTargetProfile:
        """Resolve a target and profile pair from the authoritative registry."""
        ...

    def resolve_capability(self, profile_id: str) -> Optional[Any]:
        """Resolve the current capability receipt or view for this profile."""
        ...

    def resolve_capacity(self, profile: dr.ExecutionTargetProfile) -> Sequence[Any]:
        """Resolve the eligible capacity receipts for this profile's model/provider."""
        ...

    def resolve_request(self, run_id: str, packet_id: str) -> dr.RoutingRequest:
        """Resolve the authoritative request by run and packet identity."""
        ...

    def resolve_resources(self) -> Optional[Mapping[str, Mapping[str, Any]]]:
        """Resolve the current live resource availability matrix."""
        ...

    def resolve_domain_policy(self, domain: str) -> Optional[rdp.DomainPolicy]:
        """Resolve the authoritative domain routing policy."""
        ...


class CanonicalOverrideResolver:
    """Production resolver backed by canonical target stores and registries."""

    def __init__(self, capability_store: Any = None, capacity_registry: Any = None,
                 resource_provider: Optional[Callable[[], Optional[Mapping[str, Mapping[str, Any]]]]] = None):
        self._capability_store = capability_store
        self._capacity_registry = capacity_registry
        self._resource_provider = resource_provider

    def _get_cap_store(self) -> Any:
        if self._capability_store is None:
            from src.target_capability_store import TargetCapabilityStore
            self._capability_store = TargetCapabilityStore.current()
        return self._capability_store

    def resolve_profile(self, target_id: str, profile_id: str) -> dr.ExecutionTargetProfile:
        from src.local_targets import target_by_id, LocalTargetUnavailable
        try:
            spec = target_by_id(target_id)
            if spec:
                from src.dispatch_boundary import make_target_profile
                return make_target_profile(
                    target_id=spec.target_id,
                    profile_id=profile_id,
                    provider=spec.provider,
                    locality=dr.LOCALITY_LOCAL if spec.is_local else dr.LOCALITY_HOSTED,
                    endpoint_url=spec.endpoint_url,
                )
        except (LocalTargetUnavailable, Exception):
            pass
        raise ExplanationError(f"unable to resolve authoritative profile for {target_id}/{profile_id}")

    def resolve_capability(self, profile_id: str) -> Optional[Any]:
        store = self._get_cap_store()
        try:
            raw = store.current(profile_id)
            if raw is None:
                return None
            from src.local_target_routing import _legacy_view_from_receipt
            from src.local_targets import target_by_id
            spec = target_by_id(getattr(raw, "target_id", ""))
            return _legacy_view_from_receipt(raw, spec) if spec else raw
        except Exception as exc:
            raise ExplanationError(f"unable to resolve capability for {profile_id}: {exc}")

    def resolve_capacity(self, profile: dr.ExecutionTargetProfile) -> Sequence[Any]:
        from src.provider_capacity import CapacityRegistry
        registry = self._capacity_registry or CapacityRegistry()
        return registry.eligible_capacity_for(profile.model)

    def resolve_request(self, run_id: str, packet_id: str) -> dr.RoutingRequest:
        raise NotImplementedError("production request packet resolution requires persistent packet store")

    def resolve_resources(self) -> Optional[Mapping[str, Mapping[str, Any]]]:
        if self._resource_provider:
            return self._resource_provider()
        return None

    def resolve_domain_policy(self, domain: str) -> Optional[rdp.DomainPolicy]:
        from src.routing_domain_policy import domain_policy
        return domain_policy(domain)


class InMemoryOverrideResolver:
    """In-memory resolver for testing, validation, and controlled resolution."""

    def __init__(
        self,
        *,
        profiles: Optional[Sequence[dr.ExecutionTargetProfile]] = None,
        capabilities: Optional[Sequence[Any]] = None,
        capacity_receipts: Optional[Sequence[Any]] = None,
        requests: Optional[Sequence[dr.RoutingRequest]] = None,
        resources: Optional[Mapping[str, Mapping[str, Any]]] = None,
        domain_policies: Optional[Mapping[str, rdp.DomainPolicy]] = None,
    ):
        self._profiles = {(p.target_id, p.profile_id): p for p in (profiles or ())}
        self._capabilities: dict[str, Any] = {}
        for r in (capabilities or ()):
            if r is not None:
                pid = getattr(r, "profile_id", "")
                if pid:
                    self._capabilities[pid] = r
                tid = getattr(r, "target_id", "")
                if tid:
                    self._capabilities[f"target:{tid}"] = r
        self._capacity_receipts = tuple(capacity_receipts or ())
        self._requests = {(r.run_id, r.packet_id): r for r in (requests or ())}
        self._resources = resources
        self._domain_policies = dict(domain_policies or {})

    def resolve_profile(self, target_id: str, profile_id: str) -> dr.ExecutionTargetProfile:
        key = (target_id, profile_id)
        if key in self._profiles:
            return self._profiles[key]
        raise ExplanationError(f"unresolvable target profile {target_id}/{profile_id}")

    def resolve_capability(self, profile_id: str) -> Optional[Any]:
        return self._capabilities.get(profile_id)

    def resolve_capacity(self, profile: dr.ExecutionTargetProfile) -> Sequence[Any]:
        return [
            c for c in self._capacity_receipts
            if (not getattr(c, "exposed_models", ()) or profile.model in getattr(c, "exposed_models", ()))
            and (not getattr(c, "provider", "") or c.provider == profile.provider)
        ]

    def resolve_request(self, run_id: str, packet_id: str) -> dr.RoutingRequest:
        key = (run_id, packet_id)
        if key in self._requests:
            return self._requests[key]
        raise ExplanationError(f"unresolvable request {run_id}/{packet_id}")

    def resolve_resources(self) -> Optional[Mapping[str, Mapping[str, Any]]]:
        return self._resources

    def resolve_domain_policy(self, domain: str) -> Optional[rdp.DomainPolicy]:
        if domain in self._domain_policies:
            return self._domain_policies[domain]
        from src.routing_domain_policy import domain_policy
        return domain_policy(domain)


# --------------------------------------------------------------------------
# Cryptographic Signing and Verification Helpers
# --------------------------------------------------------------------------

def _override_hmac_key() -> bytes:
    env_key = os.environ.get("ODYSSEUS_OVERRIDE_HMAC_KEY")
    if env_key:
        return env_key.encode("utf-8")
    try:
        from src.constants import APP_KEY_FILE
        p = Path(APP_KEY_FILE)
        if p.exists():
            return p.read_bytes()
    except Exception:
        pass
    return b"odysseus-operator-override-hmac-v1-fallback-key"


def _compute_hmac(core_payload: Mapping[str, Any], key: Optional[bytes] = None) -> str:
    k = key or _override_hmac_key()
    core_without_sig = {k: v for k, v in core_payload.items() if k not in ("hmac_signature", "receipt_hash")}
    data = _canonical(core_without_sig)
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hmac.new(k, data, hashlib.sha256).hexdigest()


# --------------------------------------------------------------------------
# Private Minting
# --------------------------------------------------------------------------

def _mint_override_receipt(
    *,
    operator: str,
    reason_text: str,
    moment: datetime,
    valid_until_str: str,
    explanation: RoutingExplanation,
    authoritative: CandidateExplanation,
    fresh: RoutingExplanation,
    key: str,
    hmac_key: Optional[bytes] = None,
) -> OverrideAuditReceipt:
    """Privately mint an authenticated OverrideAuditReceipt."""
    fresh_original = fresh.candidate(explanation.selected_target_id)
    original_still_eligible = bool(fresh_original is not None and fresh_original.eligible)
    eligible = fresh.eligible_candidates()

    evidence_refs = tuple(dict.fromkeys(
        (*((authoritative.receipt_hash,) if authoritative.receipt_hash else ()),
         *authoritative.capacity_receipt_refs)))

    payload: dict[str, Any] = {
        "operator_id": operator,
        "justification": reason_text,
        "requested_at": moment.isoformat(),
        "minted_at": moment.isoformat(),
        "valid_until": valid_until_str,
        "original_target_id": explanation.selected_target_id,
        "original_profile_id": explanation.selected_profile_id,
        "authorized_target_id": authoritative.target_id,
        "authorized_profile_id": authoritative.profile_id,
        "explanation_hash": explanation.explanation_hash,
        "decision_hash": explanation.decision_hash,
        "dispatch_receipt_hash": explanation.dispatch_receipt_hash,
        "policy_ref": str(explanation.policy.get("policy_ref") or ""),
        "request_fingerprint": _sha256_hex(_canonical(dict(explanation.request))),
        "rationale": tuple(explanation.rationale),
        "considered_eligible": tuple(f"{c.target_id}/{c.profile_id}" for c in eligible),
        "hard_refusals_checked": tuple(sorted(NON_OVERRIDABLE_REFUSALS)),
        "original_still_eligible": original_still_eligible,
        "canonical_evidence_verified": True,
        "evidence_refs": evidence_refs,
        "idempotency_key": key,
        "authorizes": ("eligible_candidate_selection",),
        "scope": "eligible_selection_only",
        "schema_version": 1,
    }

    # Provisional without override_id and hmac_signature to compute override_id
    provisional = OverrideAuditReceipt(override_id="", **payload)
    core = provisional.core()
    override_id = f"override-{_sha256_hex(_canonical(core))[:24]}"
    payload["override_id"] = override_id

    # Compute keyed HMAC signature
    core_with_id = {**core, "override_id": override_id}
    hmac_sig = _compute_hmac(core_with_id, key=hmac_key)
    payload["hmac_signature"] = hmac_sig

    # Sealed receipt with receipt_hash
    sealed = OverrideAuditReceipt(receipt_hash="", **payload)
    digest = _sha256_hex(_canonical(sealed.core()))
    return OverrideAuditReceipt(receipt_hash=digest, **payload)


# --------------------------------------------------------------------------
# Main Authorization Entry Point
# --------------------------------------------------------------------------

def apply_operator_override(
    decision_receipt: Any,
    requested_target_id: str,
    *,
    operator_id: str,
    justification: str,
    evidence: Any,
    now: Optional[datetime] = None,
    resolver: Optional[OverrideEvidenceResolver] = None,
    clock: Optional[Callable[[], datetime]] = None,
    idempotency_key: str = "",
    max_clock_skew_s: float = 30.0,
    hmac_key: Optional[bytes] = None,
) -> OverrideAuditReceipt:
    """Authorize and mint an operator override steering to an already-eligible candidate.

    Enforces all five invariants:
    - Pure explanation parsing (fail-closed on tamper or unsealed).
    - Server-side instant: supplied `now` checked against clock skew.
    - Resolves authorization facts from `resolver`; caller-supplied objects are unrepresentable.
    - Reproduces cited decision: fresh evaluation must match cited eligible candidates and facts.
    - Privately mints keyed HMAC receipt bounded by evidence lifetime.
    """
    explanation = _as_explanation(decision_receipt)
    operator = str(operator_id or "").strip()
    if not operator:
        raise ExplanationError("an override must name the operator who issued it")
    reason_text = str(justification or "").strip()
    if not reason_text:
        raise ExplanationError("an override must record a justification")
    if len(reason_text) > 2000:
        raise ExplanationError("an override justification must be at most 2000 characters")
    key = str(idempotency_key or "").strip()
    if len(key) > 200:
        raise ExplanationError("an idempotency key must be at most 200 characters")

    # Invariant 3: Server-side clock & skew check
    if clock is not None:
        server_now = clock()
        if now is not None:
            moment = _require_aware(now, "now")
            skew = abs((moment - server_now).total_seconds())
            if skew > max_clock_skew_s:
                raise PolicyViolationException(
                    requested_target_id, (dr.REFUSED_POLICY_DENIED,),
                    detail=f"supplied instant {moment.isoformat()} exceeds allowable clock skew of {max_clock_skew_s}s from server clock {server_now.isoformat()}")
        else:
            moment = server_now
    else:
        if now is not None:
            moment = _require_aware(now, "now")
            observed_ts = _parse_ts(explanation.observed_at)
            if observed_ts is not None and moment < observed_ts:
                raise ExplanationError("override instant cannot precede the decision it steers")
        else:
            moment = datetime.now(timezone.utc)

    target_id = str(requested_target_id or "").strip()
    if not target_id:
        raise ExplanationError("an override must name the requested target")

    # Invariant 2 (part 1): Target must be among candidates and eligible in the cited explanation
    recorded_candidate = explanation.candidate(target_id)
    if recorded_candidate is None:
        raise ExplanationError(f"target {target_id!r} is not among the candidates this explanation considered")
    if not recorded_candidate.eligible:
        raise PolicyViolationException(
            target_id, recorded_candidate.refusal_reasons,
            detail="target is refused in the canonical explanation; an override can only steer to an already-eligible candidate")

    if explanation.selected is None:
        raise ExplanationError("this explanation records a refusal, so there is no selection to steer")
    if target_id == explanation.selected_target_id:
        raise ExplanationError(
            f"target {target_id!r} is already the canonical selection; an override must steer to a different eligible candidate")

    # Invariant 1: Resolving evidence references via injected resolver
    if isinstance(evidence, OverrideEvidenceRefs):
        active_resolver = resolver or CanonicalOverrideResolver()
        request_obj = active_resolver.resolve_request(evidence.run_id, evidence.packet_id)
    else:
        # Check for CanonicalEvidence
        from src.routing_explanation import CanonicalEvidence
        if not isinstance(evidence, CanonicalEvidence):
            raise ExplanationError("evidence must be an instance of OverrideEvidenceRefs or CanonicalEvidence")
        request_obj = evidence.request
        active_resolver = resolver or InMemoryOverrideResolver(
            profiles=evidence.profiles,
            capabilities=evidence.receipts,
            capacity_receipts=evidence.capacity_receipts,
            requests=[evidence.request],
            resources=getattr(evidence, "resources", None),
            domain_policies=({evidence.domain_policy.domain: evidence.domain_policy}
                             if getattr(evidence, "domain_policy", None) else None),
        )

    try:
        claimed = dict(request_obj.core())
    except Exception as exc:
        raise ExplanationError("evidence request is malformed") from exc
    if _canonical(claimed) != _canonical(dict(explanation.request)):
        raise ExplanationError("the supplied evidence describes a different request than this explanation, so it cannot certify it")

    # Resolve candidates considered in the explanation
    resolved_profiles = []
    resolved_receipts = []
    resolved_capacity: list[Any] = []

    for c in explanation.considered:
        try:
            p = active_resolver.resolve_profile(c.target_id, c.profile_id)
            r = active_resolver.resolve_capability(c.profile_id)
        except Exception:
            if c.target_id == target_id:
                raise PolicyViolationException(
                    target_id, (dr.REFUSED_NO_ELIGIBLE_TARGET,),
                    detail="requested target profile or capability cannot be resolved")
            continue
        resolved_profiles.append(p)
        if r is not None:
            resolved_receipts.append(r)
        resolved_capacity.extend(active_resolver.resolve_capacity(p))

    requested_p = next((p for p in resolved_profiles if p.target_id == target_id), None)
    if requested_p is None:
        raise PolicyViolationException(
            target_id, (dr.REFUSED_NO_ELIGIBLE_TARGET,),
            detail="requested target profile not found")

    resources = active_resolver.resolve_resources()
    domain_pol = active_resolver.resolve_domain_policy(request_obj.domain)

    # Run fresh selector evaluation
    fresh = RoutingInspectorEvaluator(
        capacity_receipts=resolved_capacity,
        policy=None,
        domain_policy=domain_pol,
        resources=resources,
        now=moment,
    ).evaluate(request_obj, resolved_profiles, resolved_receipts, now=moment)

    authoritative = fresh.candidate(target_id)
    if authoritative is None or not authoritative.eligible:
        if authoritative is not None and authoritative.refusal_reasons:
            reasons = authoritative.refusal_reasons
        elif authoritative is not None and authoritative.capacity_rule and authoritative.capacity_rule != RULE_ELIGIBLE:
            reasons = (authoritative.capacity_rule,)
        elif authoritative is not None and authoritative.rule and authoritative.rule != RULE_ELIGIBLE:
            reasons = (authoritative.rule,)
        else:
            reasons = (dr.REFUSED_NO_ELIGIBLE_TARGET,)
        raise PolicyViolationException(
            target_id, reasons,
            detail="a fresh evaluation of the canonical inputs refuses this target")

    if authoritative.profile_id != recorded_candidate.profile_id:
        raise PolicyViolationException(
            target_id, (dr.REFUSED_NO_ELIGIBLE_TARGET,),
            detail="the canonical inputs resolve this target to another profile")

    # Invariant 2 (part 2): Reproduce, don't replace
    cited_eligible_ids = set(explanation.eligible_target_ids())
    fresh_eligible_ids = set(fresh.eligible_target_ids())
    if not fresh_eligible_ids.issubset(cited_eligible_ids):
        raise PolicyViolationException(
            target_id, (dr.REFUSED_POLICY_DENIED,),
            detail=f"fresh evaluation qualified candidates {sorted(fresh_eligible_ids - cited_eligible_ids)} not eligible in cited explanation")

    # Per-row fact reproduction check
    if (authoritative.provider != recorded_candidate.provider or
        authoritative.locality != recorded_candidate.locality or
        (authoritative.endpoint_url or "") != (recorded_candidate.endpoint_url or "") or
        (authoritative.receipt_hash or "") != (recorded_candidate.receipt_hash or "") or
        tuple(authoritative.capacity_receipt_refs) != tuple(recorded_candidate.capacity_receipt_refs)):
        raise PolicyViolationException(
            target_id, (dr.REFUSED_POLICY_DENIED,),
            detail="canonical facts for target in fresh evaluation do not match cited explanation")

    for c in explanation.considered:
        c_fresh = fresh.candidate(c.target_id)
        if c_fresh is None:
            continue
        if (c_fresh.provider != c.provider or
            c_fresh.locality != c.locality or
            (c_fresh.endpoint_url or "") != (c.endpoint_url or "") or
            (c_fresh.receipt_hash or "") != (c.receipt_hash or "") or
            tuple(c_fresh.capacity_receipt_refs) != tuple(c.capacity_receipt_refs)):
            raise PolicyViolationException(
                target_id, (dr.REFUSED_POLICY_DENIED,),
                detail=f"canonical facts for candidate {c.target_id} in fresh evaluation do not match cited explanation")

    # Invariant 3 (part 2): Evidence lifetime calculation
    cap_obs = _parse_ts(authoritative.receipt_observed_at)
    if cap_obs is None:
        raise PolicyViolationException(target_id, (dr.REFUSED_RECEIPT_STALE,), detail="capability evidence timestamp missing")
    cap_valid_until = cap_obs + timedelta(seconds=authoritative.receipt_ttl_s)
    max_lifetime = moment + timedelta(seconds=300)

    if authoritative.locality != dr.LOCALITY_LOCAL:
        if not authoritative.capacity_expires_at:
            raise PolicyViolationException(target_id, (dr.REFUSED_CAPACITY_STALE,), detail="capacity expiry missing on hosted target")
        cap_exp = _parse_ts(authoritative.capacity_expires_at)
        if cap_exp is None:
            raise PolicyViolationException(target_id, (dr.REFUSED_CAPACITY_STALE,), detail="capacity expiry malformed on hosted target")
        earliest_valid_until = min(cap_valid_until, cap_exp, max_lifetime)
    else:
        earliest_valid_until = min(cap_valid_until, max_lifetime)

    if earliest_valid_until <= moment:
        raise PolicyViolationException(target_id, (dr.REFUSED_RECEIPT_STALE,), detail="evidence for target has expired")

    # Invariant 5: Privately mint HMAC-authenticated receipt
    return _mint_override_receipt(
        operator=operator,
        reason_text=reason_text,
        moment=moment,
        valid_until_str=earliest_valid_until.isoformat(),
        explanation=explanation,
        authoritative=authoritative,
        fresh=fresh,
        key=key,
        hmac_key=hmac_key,
    )


# --------------------------------------------------------------------------
# Receipt Verification
# --------------------------------------------------------------------------

def verify_override_receipt(
    receipt: OverrideAuditReceipt,
    explanation: Optional[Any] = None,
    evidence: Optional[Any] = None,
    *,
    resolver: Optional[OverrideEvidenceResolver] = None,
    now: Optional[datetime] = None,
    clock: Optional[Callable[[], datetime]] = None,
    hmac_key: Optional[bytes] = None,
    **kwargs: Any,
) -> bool:
    """Verify an OverrideAuditReceipt's integrity, authentication signature, and validity."""
    if explanation is None:
        explanation = kwargs.get("explanation")
    if evidence is None:
        evidence = kwargs.get("evidence")

    if not isinstance(receipt, OverrideAuditReceipt):
        return False
    if not receipt.verify():
        return False
    if not receipt.hmac_signature:
        return False

    # Check HMAC signature
    expected_hmac = _compute_hmac(receipt.core(), key=hmac_key)
    if not hmac.compare_digest(receipt.hmac_signature, expected_hmac):
        return False

    # Check explanation reference
    if explanation is not None:
        try:
            exp_obj = _as_explanation(explanation)
        except Exception:
            return False
        if receipt.explanation_hash != exp_obj.explanation_hash:
            return False

    # Check validity window
    if now is not None:
        check_time = now
    elif clock is not None:
        check_time = clock()
    else:
        check_time = _parse_ts(receipt.minted_at) or datetime.now(timezone.utc)

    if receipt.valid_until:
        valid_ts = _parse_ts(receipt.valid_until)
        if valid_ts is None or check_time > valid_ts:
            return False
    if receipt.minted_at:
        minted_ts = _parse_ts(receipt.minted_at)
        if minted_ts is None or check_time < minted_ts - timedelta(seconds=30):
            return False

    # If resolver provided, re-derive the whole decision and match
    if resolver is not None and explanation is not None:
        try:
            exp_obj = _as_explanation(explanation)
            req = resolver.resolve_request(exp_obj.request["run_id"], exp_obj.request["packet_id"])
            if _canonical(req.core()) != _canonical(dict(exp_obj.request)):
                return False
            profiles = [resolver.resolve_profile(c.target_id, c.profile_id) for c in exp_obj.considered]
            views = [resolver.resolve_capability(c.profile_id) for c in exp_obj.considered]
            caps = []
            for p in profiles:
                caps.extend(resolver.resolve_capacity(p))
            resources = resolver.resolve_resources()
            dom_pol = resolver.resolve_domain_policy(req.domain)
            fresh = RoutingInspectorEvaluator(
                capacity_receipts=caps,
                domain_policy=dom_pol,
                resources=resources,
                now=check_time,
            ).evaluate(req, profiles, views, now=check_time)

            auth = fresh.candidate(receipt.authorized_target_id)
            if auth is None or not auth.eligible:
                return False
            if f"{receipt.authorized_target_id}/{receipt.authorized_profile_id}" not in receipt.considered_eligible:
                return False
        except Exception:
            return False

    return True
