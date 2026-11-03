"""Deterministic routing explanations and audited operator steering.

Why a target ran, and what an operator may legitimately change about that, as
data:

    RequestedCapability + AvailableTargets + PolicyContext
        -> eligibility matrix over the *canonical* selector's own rules
        -> RoutingExplanation (sealed: selections AND every rejection, with codes)
        -> apply_operator_override(explanation, target)
        -> OverrideAuditReceipt, or PolicyViolationException and no receipt

Structural properties, each one a negative control:

1. **No generated rationale.** An explanation is a projection of canonical
   receipts. The prose in ``reason`` is copied from the selector's own
   ``CandidateAssessment``; the codes are the selector's own constants. This
   module imports no model client and opens no socket (asserted by a test), so a
   post-hoc rationale has nowhere to come from.

2. **No second rule engine.** :class:`RoutingInspectorEvaluator` calls
   ``dispatch_routing.select_target`` — the single ordered filter path — instead
   of re-implementing eligibility. An explanation can therefore never disagree
   with the decision it explains.

3. **An override only steers.** It can move execution between candidates that
   are *already eligible*. Every ineligibility, hard or soft, raises
   :class:`PolicyViolationException` and mints nothing, so a convenience
   override cannot turn a DENY into an ALLOW.

4. **Eligibility is re-derived at steering time, never trusted.** The gate
   re-checks privacy, health, credential binding, entitlement and evidence
   freshness against ``now`` from the request and the recorded evidence. A
   receipt that *claims* ``eligible`` for a target that fails a hard rule is
   refused even when the claim is hash-sealed and self-consistent: the flag is
   an assertion, the gate is the control. In particular, capability evidence
   that has since expired cannot be overridden into freshness.

5. **The override receipt authorizes nothing else.** It records which eligible
   candidate was steered to, and ``authorizes`` is a closed vocabulary that
   admits no dispatch, no capability grant, no policy change and no evidence
   refresh. A historical observation is never backdated or renewed here.

Freshness is evaluated at one instant per evaluation and one instant per
override, so a cited receipt cannot go stale mid-decision.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Sequence, Tuple

from src import dispatch_routing as dr
from src.execution_package import _canonical

SCHEMA_VERSION = 1
OVERRIDE_SCHEMA_VERSION = 1

#: The only thing an override receipt authorizes. Closed set: anything added
#: here is a scope change to audited operator authority, not a detail.
OVERRIDE_AUTHORIZES: Tuple[str, ...] = ("eligible_candidate_selection",)
OVERRIDE_SCOPE = "eligible_selection_only"

#: Rules no operator authority can cure: privacy, credential isolation, missing
#: entitlement, and the integrity/freshness of the evidence itself. A policy
#: grant or a re-measurement can clear the rest (see ANNOTATABLE_REFUSALS);
#: none of them, hard or soft, is overridable.
NON_OVERRIDABLE_REFUSALS: frozenset[str] = frozenset({
    dr.REFUSED_PRIVACY_LOCAL_ONLY,
    dr.REFUSED_POLICY_DENIED,
    dr.REFUSED_CAPACITY_MISSING,
    dr.REFUSED_CAPACITY_UNUSABLE,
    dr.REFUSED_RECEIPT_MISSING,
    dr.REFUSED_RECEIPT_MISMATCH,
    dr.REFUSED_RECEIPT_STALE,
    dr.REFUSED_RECEIPT_UNHEALTHY,
    dr.REFUSED_NETWORK,
    dr.REFUSED_TOOL,
    dr.REFUSED_NO_CANDIDATES,
    dr.REFUSED_NO_ELIGIBLE_TARGET,
})

#: Ineligible today, but a changed canonical input (not an override) could
#: qualify them: a re-measurement, a fresh capacity collection, a different
#: envelope or budget.
ANNOTATABLE_REFUSALS: frozenset[str] = frozenset({
    dr.REFUSED_CAPACITY_STALE,
    dr.REFUSED_EXACTNESS,
    dr.REFUSED_ROLE,
    dr.REFUSED_NOT_INFERENCE_TARGET,
    dr.REFUSED_BUDGET,
    dr.REFUSED_RESOURCE,
    dr.REFUSED_CAPABILITY_MISSING,
    dr.REFUSED_UNKNOWN_CAPABILITY,
})

#: What would have to change for this candidate to qualify. Deterministic text
#: per rule code, so two evaluations of the same evidence agree byte for byte.
REQUIRED_CHANGE: Mapping[str, str] = {
    dr.REFUSED_PRIVACY_LOCAL_ONLY:
        "none: local-only work cannot execute on a hosted target, and no "
        "operator override relaxes that",
    dr.REFUSED_POLICY_DENIED:
        "a domain policy revision permitting this provider; an override cannot "
        "substitute for one",
    dr.REFUSED_RECEIPT_STALE:
        "a new capability measurement inside its ttl_s; a re-stamped old "
        "observation does not qualify",
    dr.REFUSED_RECEIPT_UNHEALTHY:
        "a fresh measurement reporting the profile healthy",
    dr.REFUSED_RECEIPT_MISSING:
        "a capability receipt measured for this exact profile",
    dr.REFUSED_RECEIPT_MISMATCH:
        "a capability receipt measured for this exact profile and target pair",
    dr.REFUSED_CAPACITY_MISSING:
        "a capacity receipt exposing this model for this provider endpoint and "
        "credential",
    dr.REFUSED_CAPACITY_UNUSABLE:
        "a fresh, entitled (api/agent_sdk) capacity receipt with usable facts",
    dr.REFUSED_CAPACITY_STALE:
        "capacity facts collected inside their ttl",
    dr.REFUSED_NETWORK:
        "a profile whose network policy satisfies the request, or a request "
        "that requires that policy",
    dr.REFUSED_TOOL:
        "a profile envelope that grants the required tools, or a request that "
        "needs fewer",
    dr.REFUSED_EXACTNESS:
        "an exactly-measured profile, which approximate intent cannot substitute for",
    dr.REFUSED_ROLE:
        "a profile that declares the requested role",
    dr.REFUSED_NOT_INFERENCE_TARGET:
        "an inference-capable profile, which a deterministic-only node is not",
    dr.REFUSED_BUDGET:
        "a profile inside the requested budget class and cost ceiling",
    dr.REFUSED_RESOURCE:
        "the resource becoming available: a fact about now, not a capability",
    dr.REFUSED_CAPABILITY_MISSING:
        "a receipt evidencing the required capabilities, or a request that drops them",
    dr.REFUSED_UNKNOWN_CAPABILITY:
        "a request restricted to known capability names",
    dr.REFUSED_NO_CANDIDATES:
        "at least one candidate profile",
    dr.REFUSED_NO_ELIGIBLE_TARGET:
        "at least one candidate surviving every ordered filter",
}

#: Every code an explanation's ``rationale`` may carry.
KNOWN_REASON_CODES: frozenset[str] = frozenset(dr.KNOWN_REFUSALS) | frozenset({
    dr.REASON_SELECTED_PREFERRED, dr.REASON_SELECTED_FALLBACK,
    dr.REASON_SELECTED_LOCAL_ONLY, dr.REASON_SELECTED_DETERMINISTIC,
})

#: The marker that a candidate passed the selector's own final rule.
RULE_ELIGIBLE = "eligible"


class ExplanationError(ValueError):
    """An explanation or override request that is malformed, unknown or a no-op.

    Deliberately distinct from :class:`PolicyViolationException`: this is a
    caller/storage fault, not a policy refusal, and it also mints nothing.
    """


class PolicyViolationException(Exception):
    """An override aimed at a target policy does not permit. Fail-closed.

    ``refusal_reasons`` are the canonical rule codes for every hard reason the
    target does not qualify, so callers never parse prose to learn why.
    """

    def __init__(self, target_id: str, refusal_reasons: Sequence[str], *,
                 detail: str = ""):
        codes = tuple(dict.fromkeys(str(code) for code in refusal_reasons if str(code)))
        if not codes:
            # A refusal with no reason is a bug in the caller of the refusal.
            raise ExplanationError(
                f"policy violation for {target_id!r} must name at least one reason")
        self.target_id = str(target_id)
        self.refusal_reasons = codes
        self.detail = str(detail)
        known = set(codes) <= set(dr.KNOWN_REFUSALS)
        super().__init__(
            f"override refused for {self.target_id}: {', '.join(codes)}"
            + ("" if known else " (unrecognized code)")
            + (f": {self.detail}" if self.detail else ""))

    def to_dict(self) -> dict:
        return {"refused": True, "kind": "policy_violation",
                "target_id": self.target_id,
                "refusal_reasons": list(self.refusal_reasons),
                "detail": self.detail}


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_ts(value: Any) -> Optional[datetime]:
    """Aware timestamp or None. Naive input is unknown, and unknown fails closed."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)



def _require_aware(value: Any, name: str) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ExplanationError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _required_change(rule: str) -> str:
    return REQUIRED_CHANGE.get(rule, "unclassified refusal: no operator path (fail closed)")


@dataclass(frozen=True)
class CandidateExplanation:
    """One alternative's fate, its canonical rule code, and the evidence behind it.

    ``rule`` is the deciding code from ``dispatch_routing._assess`` (or
    ``"eligible"``), never a repackaged judgement. ``reason`` is that
    assessment's own prose, carried for operators rather than generated here.
    """

    target_id: str
    profile_id: str
    provider: str
    locality: str
    eligible: bool
    rule: str
    reason: str = ""
    refusal_reasons: Tuple[str, ...] = ()
    hard_refusals: Tuple[str, ...] = ()
    required_change_to_qualify: str = ""
    receipt_id: str = ""
    receipt_hash: str = ""
    receipt_observed_at: str = ""
    receipt_ttl_s: int = 0
    #: Earliest expiry among the capacity receipts that evidenced this target's
    #: entitlement, so the gate can re-check freshness instead of trusting a flag.
    capacity_expires_at: str = ""
    endpoint_url: str = ""
    receipt_healthy: bool = False
    credential_bound: bool = False
    capacity_eligible: bool = False
    capacity_rule: str = ""
    capacity_receipt_refs: Tuple[str, ...] = ()
    budget_class: str = ""
    exactness: str = ""
    cost_rank: int = 0
    preference_rank: int = 0

    def __post_init__(self) -> None:
        for name in ("target_id", "profile_id", "provider", "rule"):
            if not str(getattr(self, name) or "").strip():
                raise ExplanationError(f"candidate explanation {name} must be non-empty")
        if type(self.eligible) is not bool:
            raise ExplanationError("candidate explanation eligible must be a bool")
        if self.locality not in (dr.LOCALITY_LOCAL, dr.LOCALITY_HOSTED):
            raise ExplanationError(
                f"unknown candidate locality {self.locality!r}")
        for code in self.refusal_reasons:
            if code not in dr.KNOWN_REFUSALS:
                raise ExplanationError(f"unknown refusal code {code!r}")
        for code in self.hard_refusals:
            if code not in NON_OVERRIDABLE_REFUSALS:
                raise ExplanationError(
                    f"hard refusals must be non-overridable codes, got {code!r}")
        for name in ("receipt_ttl_s", "cost_rank", "preference_rank"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ExplanationError(
                    f"candidate explanation {name} must be a non-negative int")
        for name in ("receipt_healthy", "credential_bound", "capacity_eligible"):
            if type(getattr(self, name)) is not bool:
                raise ExplanationError(
                    f"candidate explanation {name} must be a bool")
        # The matrix is self-describing: a passing candidate carries no refusal,
        # a refused one carries a code. A row that says both is not an explanation.
        if self.eligible:
            if self.rule != RULE_ELIGIBLE:
                raise ExplanationError(
                    f"eligible candidate {self.target_id!r} must have rule "
                    f"{RULE_ELIGIBLE!r}, got {self.rule!r}")
            if self.refusal_reasons or self.hard_refusals:
                raise ExplanationError(
                    f"eligible candidate {self.target_id!r} must carry no refusal")
        else:
            if self.rule == RULE_ELIGIBLE:
                raise ExplanationError(
                    f"ineligible candidate {self.target_id!r} must record its rule")
            if not self.refusal_reasons:
                raise ExplanationError(
                    f"ineligible candidate {self.target_id!r} must name a refusal code")
            if self.rule not in self.refusal_reasons:
                raise ExplanationError(
                    f"candidate {self.target_id!r} rule {self.rule!r} is not among "
                    "its refusal reasons")
        if not set(self.hard_refusals) <= set(self.refusal_reasons):
            raise ExplanationError(
                f"candidate {self.target_id!r} hard refusals must be refusal reasons too")

    @property
    def target_key(self) -> Tuple[str, str]:
        return (self.target_id, self.profile_id)

    @property
    def blocking_rule(self) -> str:
        """The first hard rule that makes this candidate unusable, else its rule."""
        return self.hard_refusals[0] if self.hard_refusals else self.rule

    def to_dict(self) -> dict:
        return {
            "target_id": self.target_id, "profile_id": self.profile_id,
            "provider": self.provider, "locality": self.locality,
            "eligible": self.eligible, "rule": self.rule, "reason": self.reason,
            "refusal_reasons": list(self.refusal_reasons),
            "hard_refusals": list(self.hard_refusals),
            "required_change_to_qualify": self.required_change_to_qualify,
            "capability_receipt_id": self.receipt_id,
            "capability_receipt_hash": self.receipt_hash,
            "capability_receipt_observed_at": self.receipt_observed_at,
            "capability_receipt_ttl_s": self.receipt_ttl_s,
            "capacity_expires_at": self.capacity_expires_at,
            "endpoint_url": self.endpoint_url,
            "capability_receipt_healthy": self.receipt_healthy,
            "credential_bound": self.credential_bound,
            "capacity_eligible": self.capacity_eligible,
            "capacity_rule": self.capacity_rule,
            "capacity_receipt_refs": list(self.capacity_receipt_refs),
            "budget_class": self.budget_class, "exactness": self.exactness,
            "cost_rank": self.cost_rank, "preference_rank": self.preference_rank,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CandidateExplanation":
        if not isinstance(payload, Mapping):
            raise ExplanationError("candidate explanation must be a mapping")
        try:
            return cls(
                target_id=payload["target_id"], profile_id=payload["profile_id"],
                provider=payload["provider"], locality=payload["locality"],
                eligible=payload["eligible"], rule=payload["rule"],
                reason=payload.get("reason", ""),
                refusal_reasons=tuple(payload.get("refusal_reasons") or ()),
                hard_refusals=tuple(payload.get("hard_refusals") or ()),
                required_change_to_qualify=payload.get("required_change_to_qualify", ""),
                receipt_id=payload.get("capability_receipt_id", ""),
                receipt_hash=payload.get("capability_receipt_hash", ""),
                receipt_observed_at=payload.get("capability_receipt_observed_at", ""),
                receipt_ttl_s=payload.get("capability_receipt_ttl_s", 0),
                capacity_expires_at=payload.get("capacity_expires_at", ""),
                endpoint_url=payload.get("endpoint_url", ""),
                receipt_healthy=payload.get("capability_receipt_healthy", False),
                credential_bound=payload.get("credential_bound", False),
                capacity_eligible=payload.get("capacity_eligible", False),
                capacity_rule=payload.get("capacity_rule", ""),
                capacity_receipt_refs=tuple(payload.get("capacity_receipt_refs") or ()),
                budget_class=payload.get("budget_class", ""),
                exactness=payload.get("exactness", ""),
                cost_rank=payload.get("cost_rank", 0),
                preference_rank=payload.get("preference_rank", 0),
            )
        except KeyError as exc:
            raise ExplanationError(f"candidate explanation missing field {exc}") from exc


@dataclass(frozen=True)
class RoutingExplanation:
    """A sealed, deterministic account of one routing evaluation.

    ``selected`` is the canonical decision's own pinned identity (or ``None``
    when the selector refused). ``considered`` holds every alternative, and
    ``rationale`` carries the selector's reason codes, not prose. ``scope`` is
    descriptive: this record never selects anything by itself.
    """

    #: Recorded so a reader can tell an explanation apart from an authorization.
    scope = OVERRIDE_SCOPE

    decision_id: str
    request: Mapping[str, Any]
    policy: Mapping[str, Any]
    considered: Tuple[CandidateExplanation, ...]
    selected: Optional[Mapping[str, Any]] = None
    rationale: Tuple[str, ...] = ()
    refusal_code: str = ""
    refusal_reason: str = ""
    observed_at: str = ""
    dispatch_receipt_hash: str = ""
    decision_hash: str = ""
    #: The resolved DomainPolicy this evaluation used, as data, so the override
    #: gate can re-evaluate provider allow/deny instead of trusting a row flag.
    domain_policy: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    explanation_hash: str = field(default="")

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ExplanationError(
                f"unsupported explanation schema_version {self.schema_version!r}")
        if not str(self.decision_id or "").strip():
            raise ExplanationError("explanation decision_id must be non-empty")
        if _parse_ts(self.observed_at) is None:
            raise ExplanationError(
                f"explanation observed_at must be an aware ISO timestamp, got "
                f"{self.observed_at!r}")
        if not isinstance(self.request, Mapping) or not isinstance(self.policy, Mapping):
            raise ExplanationError("explanation request and policy must be mappings")
        if not isinstance(self.domain_policy, Mapping):
            raise ExplanationError("explanation domain_policy must be a mapping")
        if not str(self.request.get("domain") or "").strip():
            raise ExplanationError("explanation request must record its domain")
        for code in self.rationale:
            if code not in KNOWN_REASON_CODES:
                raise ExplanationError(f"unknown rationale code {code!r}")
        seen: set[Tuple[str, str]] = set()
        for candidate in self.considered:
            if not isinstance(candidate, CandidateExplanation):
                raise ExplanationError("considered entries must be candidate explanations")
            if candidate.target_key in seen:
                raise ExplanationError(
                    f"duplicate considered candidate {candidate.target_key!r}")
            seen.add(candidate.target_key)
        if self.selected is None:
            if not self.refusal_code:
                raise ExplanationError(
                    "a refused explanation must name its refusal code")
            if self.refusal_code not in dr.KNOWN_REFUSALS:
                raise ExplanationError(f"unknown refusal code {self.refusal_code!r}")
            if any(candidate.eligible for candidate in self.considered):
                raise ExplanationError(
                    "a refused explanation must not carry an eligible candidate")
        else:
            if self.refusal_code:
                raise ExplanationError(
                    "a selection cannot also carry a refusal code")
            if not str(self.selected.get("target_id") or "").strip():
                raise ExplanationError("selected identity must name its target_id")
            match = [candidate for candidate in self.considered
                     if candidate.eligible
                     and candidate.target_id == self.selected.get("target_id")
                     and candidate.profile_id == self.selected.get("profile_id")]
            if not match:
                raise ExplanationError(
                    "the selected identity must appear as an eligible candidate")

    def core(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "request": dict(self.request),
            "policy": dict(self.policy),
            "selected": None if self.selected is None else dict(self.selected),
            "rationale": list(self.rationale),
            "refusal_code": self.refusal_code,
            "refusal_reason": self.refusal_reason,
            "considered": [candidate.to_dict() for candidate in self.considered],
            "observed_at": self.observed_at,
            "dispatch_receipt_hash": self.dispatch_receipt_hash,
            "decision_hash": self.decision_hash,
            "domain_policy": dict(self.domain_policy),
            "scope": self.scope,
        }

    def to_dict(self) -> dict:
        payload = self.core()
        payload["explanation_hash"] = self.explanation_hash
        return payload

    @property
    def selected_target_id(self) -> str:
        return str((self.selected or {}).get("target_id") or "")

    @property
    def selected_profile_id(self) -> str:
        return str((self.selected or {}).get("profile_id") or "")

    @property
    def local_only(self) -> bool:
        """Whether the recorded request required local execution."""
        return bool(self.request.get("local_only"))

    def eligible_candidates(self) -> Tuple[CandidateExplanation, ...]:
        return tuple(c for c in self.considered if c.eligible)

    def eligible_target_ids(self) -> Tuple[str, ...]:
        """The closed set an override may steer to, in deterministic order."""
        return tuple(c.target_id for c in self.eligible_candidates())

    def candidate(self, target_id: str) -> Optional[CandidateExplanation]:
        """The one candidate for ``target_id``; ambiguous ids are refused."""
        key = str(target_id or "").strip()
        matches = [c for c in self.considered if c.target_id == key]
        if not matches:
            return None
        if len(matches) > 1:
            raise ExplanationError(
                f"target_id {key!r} names {len(matches)} profiles; an override must "
                "name an unambiguous target")
        return matches[0]


def explanation_hash_is_valid(payload: Mapping[str, Any]) -> bool:
    """Whether ``explanation_hash`` seals exactly these fields."""
    try:
        recorded = str(payload.get("explanation_hash") or "")
        core = {key: value for key, value in dict(payload).items()
                if key != "explanation_hash"}
    except Exception:
        return False
    return bool(recorded) and _sha256_hex(_canonical(core)) == recorded


def routing_explanation_from_dict(payload: Mapping[str, Any]) -> RoutingExplanation:
    """Rebuild a stored explanation, refusing anything that is not self-sealed."""
    if not isinstance(payload, Mapping):
        raise ExplanationError("explanation payload must be a mapping")
    recorded = str(payload.get("explanation_hash") or "")
    if not recorded:
        raise ExplanationError("stored explanation carries no explanation_hash")
    if payload.get("scope") != OVERRIDE_SCOPE:
        raise ExplanationError(
            f"stored explanation scope {payload.get('scope')!r} is not "
            f"{OVERRIDE_SCOPE!r}")
    if not explanation_hash_is_valid(payload):
        raise ExplanationError("stored explanation hash does not match its fields")
    selected = payload.get("selected")
    try:
        explanation = RoutingExplanation(
            decision_id=payload["decision_id"],
            request=dict(payload["request"]),
            policy=dict(payload["policy"]),
            considered=tuple(CandidateExplanation.from_dict(row)
                             for row in payload["considered"]),
            selected=None if selected is None else dict(selected),
            rationale=tuple(payload.get("rationale") or ()),
            refusal_code=str(payload.get("refusal_code") or ""),
            refusal_reason=str(payload.get("refusal_reason") or ""),
            observed_at=str(payload.get("observed_at") or ""),
            dispatch_receipt_hash=str(payload.get("dispatch_receipt_hash") or ""),
            decision_hash=str(payload.get("decision_hash") or ""),
            domain_policy=dict(payload.get("domain_policy") or {}),
            schema_version=payload.get("schema_version", SCHEMA_VERSION),
            explanation_hash=recorded,
        )
    except KeyError as exc:
        raise ExplanationError(f"stored explanation missing field {exc}") from exc
    # The rebuilt record must re-seal to the same digest: a field the loader
    # dropped or normalized would otherwise pass as the original evidence.
    if _sha256_hex(_canonical(explanation.core())) != recorded:
        raise ExplanationError("stored explanation hash does not round-trip")
    return explanation


def _order_key(candidate: CandidateExplanation) -> Tuple[Any, ...]:
    """The selector's own deterministic order, so input order cannot change a hash."""
    return (candidate.preference_rank, 0 if candidate.eligible else 1,
            0 if candidate.locality == dr.LOCALITY_LOCAL else 1,
            candidate.cost_rank, candidate.target_id, candidate.profile_id)


def _capacity_expiry_s(capacity_receipts: Sequence[Any],
                       refs: Sequence[str]) -> str:
    """Earliest fact-expiry among the capacity receipts named by ``refs``.

    Empty when no receipt is named: a local target needs no capacity, and a
    hosted one with no expiry evidence fails closed in the gate.
    """
    wanted = {str(ref) for ref in refs}
    if not wanted:
        return ""
    expiries = []
    for receipt in capacity_receipts or ():
        if str(getattr(receipt, "ref", "")) not in wanted:
            continue
        observed = _parse_ts(getattr(receipt, "observed_at", ""))
        ttl = getattr(receipt, "ttl_seconds", None)
        if observed is None or type(ttl) is not int or ttl < 0:
            return ""  # unreadable or malformed facts never look fresh
        expiries.append(observed + timedelta(seconds=ttl))
    return min(expiries).isoformat() if expiries else ""


def _candidate_explanation(assessment: Any, profile: Any, receipt: Any, *,
                           capacity_receipts: Sequence[Any],
                           now: datetime) -> CandidateExplanation:
    """Project ONE canonical assessment, adding only facts the receipt lacks."""
    capacity_ok, capacity_refs, capacity_rule, _ = dr.classify_capacity_for(
        profile, capacity_receipts, now=now)
    hard = ((assessment.rule,) if not assessment.eligible
            and assessment.rule in NON_OVERRIDABLE_REFUSALS else ())
    ttl_s = 0 if assessment.receipt_ttl_s is None else assessment.receipt_ttl_s
    try:
        ttl_s = int(ttl_s)
    except (TypeError, ValueError) as exc:
        raise ExplanationError(
            f"capability receipt ttl is malformed: {ttl_s!r}") from exc
    if ttl_s < 0:
        raise ExplanationError("capability receipt ttl must be non-negative")
    return CandidateExplanation(
        target_id=assessment.target_id, profile_id=assessment.profile_id,
        provider=assessment.provider, locality=assessment.locality,
        eligible=bool(assessment.eligible), rule=assessment.rule,
        reason=assessment.reason,
        refusal_reasons=((assessment.rule,) if not assessment.eligible else ()),
        hard_refusals=hard,
        required_change_to_qualify=("" if assessment.eligible
                                    else _required_change(assessment.rule)),
        receipt_id=assessment.receipt_id, receipt_hash=assessment.receipt_hash,
        receipt_observed_at=assessment.receipt_observed_at,
        receipt_ttl_s=ttl_s,
        endpoint_url=str(getattr(profile, "endpoint_url", "") or ""),
        receipt_healthy=bool(getattr(receipt, "healthy", False)),
        credential_bound=bool(profile.credential_sha256),
        capacity_eligible=bool(capacity_ok), capacity_rule=capacity_rule,
        capacity_receipt_refs=tuple(capacity_refs),
        capacity_expires_at=_capacity_expiry_s(capacity_receipts, capacity_refs),
        budget_class=assessment.budget_class, exactness=assessment.exactness,
        cost_rank=assessment.cost_rank, preference_rank=assessment.preference_rank,
    )


def _identity_index(receipts: Sequence[Any]) -> dict:
    """Capability views by receipt_id and by content hash, for faithful projection."""
    index: dict = {}
    for receipt in receipts or ():
        if receipt is None:
            continue
        for key in (getattr(receipt, "receipt_id", ""),
                    getattr(receipt, "receipt_hash", "")):
            if key:
                index.setdefault(str(key), receipt)
    return index


def _profile_index(profiles: Sequence[Any]) -> dict:
    return {(p.target_id, p.profile_id): p for p in profiles or ()}


def _decision_id_for(request: Any) -> str:
    """A deterministic decision id: the same request always explains the same way."""
    digest = _sha256_hex(_canonical({
        "schema_version": SCHEMA_VERSION, "request": request.core()}))
    return f"explain-{digest[:24]}"


class RoutingInspectorEvaluator:
    """Compute the eligibility matrix and a sealed explanation for one request.

    The evaluator adds no rules of its own. It fixes the evidence and the instant
    once, calls the canonical selector, and projects the result, so an
    explanation and the decision it describes cannot disagree.
    """

    def __init__(self, *, capacity_receipts: Sequence[Any] = (),
                 resources: Optional[Mapping[str, Mapping[str, Any]]] = None,
                 policy: Any = None, domain_policy: Any = None,
                 now: Optional[datetime] = None):
        self._capacity_receipts = tuple(capacity_receipts or ())
        self._resources = resources
        self._policy = policy
        self._domain_policy = domain_policy
        self._now = now

    def evaluate(self, request: Any, profiles: Sequence[Any],
                 receipts: Sequence[Any] = (), *,
                 now: Optional[datetime] = None,
                 decision_id: str = "") -> RoutingExplanation:
        """Explain one request over these alternatives. Pure; never dispatches.

        ``decision_id`` pins the explanation to a decision that was actually
        taken, so ``dispatch_receipt_hash`` is comparable with the retained
        `DispatchDecisionReceipt`. Omitted, it is derived from the request, so the
        same request always explains identically.
        """
        profiles = list(profiles or ())
        receipts = list(receipts or ())
        moment = _require_aware(now if now is not None else self._now, "now")
        decision_id = str(decision_id or "").strip() or _decision_id_for(request)
        # Resolve the domain policy once, record it, and hand the SAME object to
        # the selector, so the explanation and the decision cannot disagree and
        # the override gate can re-evaluate provider allow/deny offline.
        if self._domain_policy is not None:
            resolved_domain_policy = self._domain_policy
        else:
            try:
                from src.routing_domain_policy import domain_policy

                resolved_domain_policy = domain_policy(request.domain)
            except Exception:
                resolved_domain_policy = None
        recorded_domain_policy = _recorded_domain_policy(resolved_domain_policy)
        options = {
            "policy": self._policy, "domain_policy": resolved_domain_policy,
            "resources": self._resources,
            "capacity_receipts": self._capacity_receipts,
            "now": moment, "decision_id": decision_id,
        }
        try:
            decision = dr.select_target(
                request, profiles=profiles, receipts=receipts, **options)
        except dr.RoutingRefused as refused:
            return self._refused_explanation(
                request, refused, profiles=profiles, receipts=receipts,
                now=moment, decision_id=decision_id,
                domain_policy=recorded_domain_policy)
        return self._selected_explanation(
            request, decision, profiles=profiles, receipts=receipts, now=moment,
            domain_policy=recorded_domain_policy)

    def _project(self, assessments: Sequence[Any], profiles: Sequence[Any],
                 receipts: Sequence[Any], now: datetime) -> Tuple[CandidateExplanation, ...]:
        receipts_by_identity = _identity_index(receipts)
        profiles_by_identity = _profile_index(profiles)
        rows = []
        for assessment in assessments:
            profile = profiles_by_identity.get(
                (assessment.target_id, assessment.profile_id))
            if profile is None:
                raise ExplanationError(
                    f"assessment names unknown profile "
                    f"{assessment.target_id}/{assessment.profile_id}")
            receipt = (receipts_by_identity.get(assessment.receipt_hash)
                       or receipts_by_identity.get(assessment.receipt_id))
            rows.append(_candidate_explanation(
                assessment, profile, receipt,
                capacity_receipts=self._capacity_receipts, now=now))
        return tuple(sorted(rows, key=_order_key))

    def _explanation(self, request: Any, *, decision_id: str, considered: tuple,
                     selected: Optional[Mapping[str, Any]] = None,
                     rationale: Tuple[str, ...] = (), refusal_code: str = "",
                     refusal_reason: str = "", observed_at: str = "",
                     dispatch_receipt_hash: str = "", decision_hash: str = "",
                     policy: Any = None,
                     domain_policy: Optional[Mapping[str, Any]] = None) -> RoutingExplanation:
        provisional = RoutingExplanation(
            decision_id=decision_id, request=dict(request.core()),
            policy=dict((policy or dr.policy_snapshot()).to_dict()),
            considered=considered, selected=selected, rationale=rationale,
            refusal_code=refusal_code, refusal_reason=refusal_reason,
            observed_at=observed_at, dispatch_receipt_hash=dispatch_receipt_hash,
            decision_hash=decision_hash, domain_policy=dict(domain_policy or {}))
        digest = _sha256_hex(_canonical(provisional.core()))
        return RoutingExplanation(
            decision_id=provisional.decision_id, request=provisional.request,
            policy=provisional.policy, considered=provisional.considered,
            selected=provisional.selected, rationale=provisional.rationale,
            refusal_code=provisional.refusal_code,
            refusal_reason=provisional.refusal_reason,
            observed_at=provisional.observed_at,
            dispatch_receipt_hash=provisional.dispatch_receipt_hash,
            decision_hash=provisional.decision_hash,
            domain_policy=provisional.domain_policy, explanation_hash=digest)

    def _selected_explanation(self, request: Any, decision: Any, *,
                              profiles: Sequence[Any], receipts: Sequence[Any],
                              now: datetime,
                              domain_policy: Optional[Mapping[str, Any]] = None
                              ) -> RoutingExplanation:
        considered = self._project(decision.candidates, profiles, receipts, now)
        return self._explanation(
            request, decision_id=decision.decision_id, considered=considered,
            selected=dict(decision.pin()), rationale=(decision.reason_code,),
            observed_at=decision.observed_at,
            dispatch_receipt_hash=decision.receipt_hash,
            decision_hash=decision.decision_hash, policy=decision.policy,
            domain_policy=domain_policy)

    def _refused_explanation(self, request: Any, refused: Any, *,
                             profiles: Sequence[Any], receipts: Sequence[Any],
                             now: datetime, decision_id: str,
                             domain_policy: Optional[Mapping[str, Any]] = None
                             ) -> RoutingExplanation:
        considered = self._project(refused.assessments, profiles, receipts, now)
        # A refusal that still reports an eligible candidate is not a refusal.
        if any(candidate.eligible for candidate in considered):
            raise ExplanationError(
                f"selector refusal {refused.code} contradicts its own assessments")
        return self._explanation(
            request, decision_id=decision_id, considered=considered,
            rationale=(refused.code,), refusal_code=refused.code,
            refusal_reason=refused.reason, observed_at=now.isoformat(),
            policy=self._policy or dr.policy_snapshot(),
            domain_policy=domain_policy)


def _as_explanation(value: Any) -> RoutingExplanation:
    """Accept a sealed explanation, or stored JSON that still seals itself."""
    if isinstance(value, RoutingExplanation):
        if not value.explanation_hash:
            raise ExplanationError("explanation is not sealed; it is not evidence")
        if not explanation_hash_is_valid(value.to_dict()):
            raise ExplanationError("explanation hash does not match its fields")
        return value
    return routing_explanation_from_dict(value)


def _evidence_age_s(candidate: CandidateExplanation, now: datetime) -> Optional[int]:
    observed = _parse_ts(candidate.receipt_observed_at)
    if observed is None:
        return None
    try:
        return int((now - observed).total_seconds())
    except (OverflowError, TypeError, ValueError):
        # An uncomputable age is unknown, and unknown freshness fails closed.
        return None


def _recorded_domain_policy(policy: Any) -> dict:
    """The resolved ``DomainPolicy`` as data, so the gate can re-evaluate it.

    Unresolvable policy is recorded as local-only, matching the fail-closed
    default ``dispatch_routing`` already applies.
    """
    if policy is None:
        return {"resolution": "unavailable", "domain": "", "local_only": True,
                "allowed_providers": None, "denied_providers": [],
                "fallback_permitted": False, "model_substitution_permitted": False,
                "approval_required": True, "budget_class": "sensitive"}
    allowed = getattr(policy, "allowed_providers", None)
    return {
        "resolution": "resolved",
        "domain": str(getattr(policy, "domain", "") or ""),
        "local_only": bool(getattr(policy, "local_only", False)),
        "allowed_providers": (None if allowed is None
                              else sorted(str(p) for p in allowed)),
        "denied_providers": sorted(str(p) for p in
                                   getattr(policy, "denied_providers", ())),
        "fallback_permitted": bool(getattr(policy, "fallback_permitted", False)),
        "model_substitution_permitted": bool(
            getattr(policy, "model_substitution_permitted", False)),
        "approval_required": bool(getattr(policy, "approval_required", False)),
        "budget_class": str(getattr(policy, "budget_class", "standard") or "standard"),
    }


def _domain_policy_violations(explanation: RoutingExplanation,
                              candidate: CandidateExplanation) -> Tuple[str, ...]:
    """Re-run the canonical domain evaluator over the recorded policy revision."""
    recorded = dict(explanation.domain_policy or {})
    if recorded.get("resolution") != "resolved":
        return (dr.REFUSED_POLICY_DENIED,)
    try:
        from src.routing_domain_policy import (
            DomainPolicy, domain_policy as resolve, evaluate_route,
        )

        domain = str(explanation.request.get("domain")
                     or recorded.get("domain") or "")
        # The AUTHORITATIVE revision governs a live override. The recorded one is
        # evidence, not control: a forger who rewrites it and reseals must not be
        # believed, and a policy that moved since the decision re-decides.
        authoritative = resolve(domain)
        if recorded != _recorded_domain_policy(authoritative):
            return (dr.REFUSED_POLICY_DENIED,)
        recorded = _recorded_domain_policy(authoritative)
        allowed = recorded.get("allowed_providers")
        policy = DomainPolicy(
            domain=str(recorded.get("domain") or ""),
            local_only=bool(recorded.get("local_only")),
            allowed_providers=(frozenset(str(p).lower() for p in allowed)
                               if allowed is not None else None),
            denied_providers=frozenset(
                str(p).lower() for p in recorded.get("denied_providers") or ()),
            fallback_permitted=bool(recorded.get("fallback_permitted")),
            model_substitution_permitted=bool(
                recorded.get("model_substitution_permitted")),
            approval_required=bool(recorded.get("approval_required")),
            budget_class=str(recorded.get("budget_class") or "standard"))
        decision = evaluate_route(
            domain=policy.domain or str(explanation.request.get("domain") or ""),
            provider=candidate.provider,
            sensitivity=str(explanation.request.get("sensitivity") or "internal"),
            policy=policy,
            endpoint_url=candidate.endpoint_url or None)
    except Exception:
        # A policy that cannot be re-evaluated DENIES, as it does in the selector.
        return (dr.REFUSED_POLICY_DENIED,)
    if decision.allowed:
        return ()
    if decision.rule == "sensitive-domain-local-only":
        return (dr.REFUSED_PRIVACY_LOCAL_ONLY,)
    return (dr.REFUSED_POLICY_DENIED,)


def _gate_violations(explanation: RoutingExplanation,
                     candidate: CandidateExplanation, now: datetime) -> Tuple[str, ...]:
    """Re-derive every hard rule for this candidate at ``now``.

    Deliberately does not trust ``candidate.eligible``: a hash-sealed receipt can
    still be wrong, and this is the control that makes a DENY un-overridable.
    """
    codes: list[str] = list(candidate.hard_refusals)
    if candidate.rule != RULE_ELIGIBLE:
        codes.append(candidate.rule)

    # 1. domain policy: re-evaluate the RECORDED revision with the canonical
    #    evaluator. Without this, a forged-but-sealed row could mark a
    #    policy-denied provider eligible and the flag would be believed.
    codes.extend(_domain_policy_violations(explanation, candidate))

    # 1b. privacy: local-only intent never executes on a hosted target.
    if explanation.local_only and candidate.locality != dr.LOCALITY_LOCAL:
        codes.append(dr.REFUSED_PRIVACY_LOCAL_ONLY)

    # 2. evidence presence, health and freshness at the override instant.
    has_evidence = bool(candidate.receipt_hash or candidate.receipt_id)
    if not has_evidence:
        codes.append(dr.REFUSED_RECEIPT_MISSING)
    else:
        if not candidate.receipt_healthy:
            codes.append(dr.REFUSED_RECEIPT_UNHEALTHY)
        age = _evidence_age_s(candidate, now)
        # Stale evidence is never freshened into eligibility by an override.
        if age is None or age < 0 or age > candidate.receipt_ttl_s:
            codes.append(dr.REFUSED_RECEIPT_STALE)

    # 4. entitlement and credential isolation for hosted targets.
    if candidate.locality != dr.LOCALITY_LOCAL:
        if not candidate.credential_bound:
            codes.append(dr.REFUSED_CAPACITY_MISSING)
        if not candidate.capacity_eligible:
            codes.append(candidate.capacity_rule or dr.REFUSED_CAPACITY_MISSING)
        else:
            # Freshness of the entitlement facts is a fact about now: a grant
            # whose evidence has expired (or was never recorded) is not
            # established, and no override supplies it.
            expires = _parse_ts(candidate.capacity_expires_at)
            if expires is None or now > expires:
                codes.append(dr.REFUSED_CAPACITY_STALE)

    if any(code not in dr.KNOWN_REFUSALS for code in codes):
        raise ExplanationError("gate produced an unknown refusal code")
    return tuple(sorted(set(codes)))


@dataclass(frozen=True)
class CanonicalEvidence:
    """The canonical inputs an explanation claims to describe.

    Supplying these turns the gate's record-internal consistency checks into a
    real check: the target must be eligible in a FRESH evaluation of the actual
    inputs, so a rewritten request or evidence identity cannot be believed.
    """

    request: Any
    profiles: Sequence[Any] = ()
    receipts: Sequence[Any] = ()
    capacity_receipts: Sequence[Any] = ()
    domain_policy: Any = None


def apply_operator_override(decision_receipt: Any, requested_target_id: str, *,
                            operator_id: str, justification: str,
                            evidence: "CanonicalEvidence",
                            now: Optional[datetime] = None,
                            idempotency_key: str = "",
                            ) -> "OverrideAuditReceipt":
    """Steer execution to an already-eligible candidate, or refuse and mint nothing.

    ``decision_receipt`` is a sealed :class:`RoutingExplanation` (or its stored
    JSON). Eligibility is re-derived from ``evidence`` at ``now``, so this is a fresh selector
    evaluation, not a replay of the decision-time one. Record-only steering without
    evidence is not supported.

    Raises :class:`PolicyViolationException` when the target is ineligible for
    any reason — hard refusals included, and never converted. Raises
    :class:`ExplanationError` for an unsealed receipt, an unknown or ambiguous
    target, a no-op, missing evidence, or a missing operator/justification.
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
    moment = _require_aware(now, "now")

    if not isinstance(evidence, CanonicalEvidence):
        raise ExplanationError("evidence must be a CanonicalEvidence")
    try:
        claimed = dict(evidence.request.core())
    except Exception as exc:
        raise ExplanationError("evidence request is malformed") from exc
    if _canonical(claimed) != _canonical(dict(explanation.request)):
        raise ExplanationError(
            "the supplied evidence describes a different request than this "
            "explanation, so it cannot certify it")

    target_id = str(requested_target_id or "").strip()
    if not target_id:
        raise ExplanationError("an override must name the requested target")
    recorded_candidate = explanation.candidate(target_id)
    if recorded_candidate is None:
        raise ExplanationError(
            f"target {target_id!r} is not among the candidates this explanation "
            "considered")

    fresh = RoutingInspectorEvaluator(
        capacity_receipts=evidence.capacity_receipts,
        policy=None, domain_policy=evidence.domain_policy).evaluate(
            evidence.request, list(evidence.profiles), list(evidence.receipts),
            now=moment)

    authoritative = fresh.candidate(target_id)
    violations = _gate_violations(fresh, authoritative, moment) if authoritative is not None else (dr.REFUSED_NO_ELIGIBLE_TARGET,)
    if authoritative is None or violations or not authoritative.eligible:
        reasons = violations or (authoritative.refusal_reasons if authoritative is not None else (dr.REFUSED_NO_ELIGIBLE_TARGET,))
        raise PolicyViolationException(
            target_id, reasons,
            detail="a fresh evaluation of the canonical inputs refuses this target")

    if authoritative.profile_id != recorded_candidate.profile_id:
        raise PolicyViolationException(
            target_id, (dr.REFUSED_NO_ELIGIBLE_TARGET,),
            detail="the canonical inputs resolve this target to another profile")

    if explanation.selected is None:
        raise ExplanationError(
            "this explanation records a refusal, so there is no selection to steer")
    if target_id == explanation.selected_target_id:
        raise ExplanationError(
            f"target {target_id!r} is already the canonical selection; an override "
            "must steer to a different eligible candidate")

    fresh_original = fresh.candidate(explanation.selected_target_id)
    original_violations = _gate_violations(fresh, fresh_original, moment) if fresh_original is not None else ()
    original_still_eligible = bool(
        fresh_original is not None and fresh_original.eligible and not original_violations)

    eligible = fresh.eligible_candidates()
    return OverrideAuditReceipt.seal(
        operator_id=operator, justification=reason_text, requested_at=moment.isoformat(),
        original_target_id=explanation.selected_target_id,
        original_profile_id=explanation.selected_profile_id,
        authorized_target_id=authoritative.target_id,
        authorized_profile_id=authoritative.profile_id,
        explanation_hash=explanation.explanation_hash,
        decision_hash=explanation.decision_hash,
        dispatch_receipt_hash=explanation.dispatch_receipt_hash,
        policy_ref=str(explanation.policy.get("policy_ref") or ""),
        request_fingerprint=_sha256_hex(_canonical(dict(explanation.request))),
        # The canonical reason codes of the explanation this override steers
        # within; the new target's eligibility is proven by the gate and by its
        # membership in considered_eligible.
        rationale=tuple(explanation.rationale),
        considered_eligible=tuple(f"{c.target_id}/{c.profile_id}" for c in eligible),
        hard_refusals_checked=tuple(sorted(NON_OVERRIDABLE_REFUSALS)),
        original_still_eligible=original_still_eligible,
        canonical_evidence_verified=True,
        evidence_refs=tuple(dict.fromkeys(
            (*((authoritative.receipt_hash,) if authoritative.receipt_hash else ()),
             *authoritative.capacity_receipt_refs))),
        idempotency_key=key)


@dataclass(frozen=True)
class OverrideAuditReceipt:
    """Immutable record that an operator steered between eligible candidates.

    It is an authorization to select an already-eligible candidate and nothing
    else: ``authorizes`` is a closed vocabulary and ``scope`` is fixed, so the
    record cannot be read as a dispatch, a capability grant, a policy change or
    a refreshed observation.
    """

    operator_id: str
    justification: str
    requested_at: str
    original_target_id: str
    original_profile_id: str
    authorized_target_id: str
    authorized_profile_id: str
    explanation_hash: str
    decision_hash: str
    dispatch_receipt_hash: str
    policy_ref: str
    request_fingerprint: str
    rationale: Tuple[str, ...]
    considered_eligible: Tuple[str, ...]
    hard_refusals_checked: Tuple[str, ...]
    original_still_eligible: bool
    #: True only when eligibility was re-derived from canonical inputs rather
    #: than from the sealed record alone. Every valid override requires True:
    #: record-only overrides without canonical evidence are not supported.
    canonical_evidence_verified: bool = True
    evidence_refs: Tuple[str, ...] = ()
    idempotency_key: str = ""
    override_id: str = ""
    authorizes: Tuple[str, ...] = OVERRIDE_AUTHORIZES
    scope: str = OVERRIDE_SCOPE
    schema_version: int = OVERRIDE_SCHEMA_VERSION
    receipt_hash: str = field(default="")

    def __post_init__(self) -> None:
        for name in ("operator_id", "justification", "original_target_id",
                     "original_profile_id", "authorized_target_id",
                     "authorized_profile_id", "explanation_hash", "decision_hash",
                     "policy_ref", "request_fingerprint"):
            if not str(getattr(self, name) or "").strip():
                raise ExplanationError(f"override receipt {name} must be non-empty")
        if _parse_ts(self.requested_at) is None:
            raise ExplanationError(
                f"override receipt requested_at must be an aware ISO timestamp, "
                f"got {self.requested_at!r}")
        if type(self.original_still_eligible) is not bool:
            raise ExplanationError("override receipt original_still_eligible must be a bool")
        if self.canonical_evidence_verified is not True:
            raise ExplanationError(
                "an override receipt must have canonical_evidence_verified=True; "
                "record-only overrides are not supported")
        if self.schema_version != OVERRIDE_SCHEMA_VERSION:
            raise ExplanationError(
                f"unsupported override schema_version {self.schema_version!r}")
        if tuple(self.authorizes) != OVERRIDE_AUTHORIZES:
            raise ExplanationError(
                "an override receipt authorizes eligible candidate selection and "
                "nothing else")
        if self.scope != OVERRIDE_SCOPE:
            raise ExplanationError(f"override scope must be {OVERRIDE_SCOPE!r}")
        if self.authorized_target_id == self.original_target_id:
            raise ExplanationError(
                "an override must steer to a different target than the selection")
        if f"{self.authorized_target_id}/{self.authorized_profile_id}" not in self.considered_eligible:
            raise ExplanationError(
                "the authorized target must be one of the recorded eligible candidates")
        for code in self.rationale:
            if code not in KNOWN_REASON_CODES:
                raise ExplanationError(f"unknown override rationale code {code!r}")

    def core(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "override_id": self.override_id,
            "operator_id": self.operator_id,
            "justification": self.justification,
            "requested_at": self.requested_at,
            "original_target_id": self.original_target_id,
            "original_profile_id": self.original_profile_id,
            "authorized_target_id": self.authorized_target_id,
            "authorized_profile_id": self.authorized_profile_id,
            "explanation_hash": self.explanation_hash,
            "decision_hash": self.decision_hash,
            "dispatch_receipt_hash": self.dispatch_receipt_hash,
            "policy_ref": self.policy_ref,
            "request_fingerprint": self.request_fingerprint,
            "rationale": list(self.rationale),
            "considered_eligible": list(self.considered_eligible),
            "hard_refusals_checked": list(self.hard_refusals_checked),
            "original_still_eligible": self.original_still_eligible,
            "canonical_evidence_verified": self.canonical_evidence_verified,
            "evidence_refs": list(self.evidence_refs),
            "idempotency_key": self.idempotency_key,
            "authorizes": list(self.authorizes),
            "scope": self.scope,
        }

    @property
    def override_hash(self) -> str:
        return self.receipt_hash

    def verify(self) -> bool:
        """Whether this receipt still seals exactly its own fields."""
        return override_receipt_hash_is_valid(self.to_dict())

    def to_dict(self) -> dict:
        payload = self.core()
        payload["receipt_hash"] = self.receipt_hash
        return payload

    @classmethod
    def seal(cls, **kwargs: Any) -> "OverrideAuditReceipt":
        """Mint the receipt: derive the id and hash from the content itself.

        Re-issuing the same intent (same operator, justification, evidence,
        targets and instant) yields the same ``override_id`` and ``receipt_hash``,
        so a retry cannot fabricate a second authorization.
        """
        known = {f.name for f in fields(cls)} - {"override_id", "receipt_hash"}
        unknown = set(kwargs) - known
        if unknown:
            raise ExplanationError(
                f"unknown override receipt field(s): {sorted(unknown)}")
        # ``Any`` on purpose: every value is validated by ``__post_init__``, and
        # a narrower inferred type makes ``**payload`` a type-check target for
        # tooling that cannot see through the comprehension.
        payload: Any = {
            name: (tuple(value) if isinstance(value, (list, tuple)) else value)
            for name, value in kwargs.items()}
        provisional = cls(override_id="", **payload)
        core = provisional.core()
        override_id = f"override-{_sha256_hex(_canonical(core))[:24]}"
        sealed = cls(override_id=override_id, **payload)
        digest = _sha256_hex(_canonical(sealed.core()))
        return cls(override_id=override_id, receipt_hash=digest, **payload)


def override_receipt_hash_is_valid(payload: Mapping[str, Any]) -> bool:
    """Whether ``receipt_hash`` seals exactly these fields."""
    if not isinstance(payload, Mapping):
        return False
    try:
        recorded = str(payload.get("receipt_hash") or "")
        core = {key: value for key, value in dict(payload).items()
                if key != "receipt_hash"}
    except Exception:
        return False
    return bool(recorded) and _sha256_hex(_canonical(core)) == recorded
