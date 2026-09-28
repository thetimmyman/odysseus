"""Typed, mechanically closable evidence requirements.

``ExecutionPackage`` carries explicit EvidenceRequirement IDs, and validation
computes SATISFIED / FAILED / BLOCKED / NOT_APPLICABLE for each from receipts.

* UNRESOLVED is a fifth, non-terminal state for requirements no receipt
  addresses; the validator refuses to seal over a mandatory one. NOT_APPLICABLE
  needs an explicit recorded waiver, never mere absence.
* Independence is compared: a WORKER_AUTHORED receipt can't satisfy a
  requirement demanding independent proof, even if it says PASS.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence, Tuple

REQUIREMENT_SCHEMA_VERSION = 1

#: Proof already produced by an authoritative process that predates this work.
INDEPENDENCE_EXISTING_AUTHORITATIVE = "EXISTING_AUTHORITATIVE"
#: Proof owned by the harness and never shown to the worker (a hidden test).
INDEPENDENCE_HARNESS_HIDDEN = "HARNESS_HIDDEN"
#: Proof produced by a verifier that is not the worker (another host/runner).
INDEPENDENCE_INDEPENDENT_VERIFIER = "INDEPENDENT_VERIFIER"
#: Proof the worker wrote about its own work. Legitimate evidence; NOT an
#: independent acceptance proof for that same work.
INDEPENDENCE_WORKER_AUTHORED = "WORKER_AUTHORED"

KNOWN_INDEPENDENCE = frozenset({
    INDEPENDENCE_EXISTING_AUTHORITATIVE, INDEPENDENCE_HARNESS_HIDDEN,
    INDEPENDENCE_INDEPENDENT_VERIFIER, INDEPENDENCE_WORKER_AUTHORED,
})

#: The classes that may close a requirement which demands independence.
INDEPENDENT_CLASSES = frozenset({
    INDEPENDENCE_EXISTING_AUTHORITATIVE, INDEPENDENCE_HARNESS_HIDDEN,
    INDEPENDENCE_INDEPENDENT_VERIFIER,
})

KIND_DETERMINISTIC_VERIFICATION = "deterministic_verification"
KIND_POSITIVE_CONTROL = "positive_control"
KIND_NEGATIVE_CONTROL = "negative_control"
KIND_SCOPE_CHECK = "scope_check"
KIND_CONTEXT_DELIVERY = "context_delivery"
KIND_SOURCE_BINDING = "source_binding"
KIND_TARGET_BINDING = "target_binding"
KIND_BASELINE_COMPARISON = "baseline_comparison"
KIND_RETRY_PRESERVATION = "retry_preservation"
KIND_SECRET_HYGIENE = "secret_hygiene"
KIND_PACKAGE_INTEGRITY = "package_integrity"

KNOWN_KINDS = frozenset({
    KIND_DETERMINISTIC_VERIFICATION, KIND_POSITIVE_CONTROL, KIND_NEGATIVE_CONTROL,
    KIND_SCOPE_CHECK, KIND_CONTEXT_DELIVERY, KIND_SOURCE_BINDING,
    KIND_TARGET_BINDING, KIND_BASELINE_COMPARISON, KIND_RETRY_PRESERVATION,
    KIND_SECRET_HYGIENE, KIND_PACKAGE_INTEGRITY,
})

STATE_SATISFIED = "SATISFIED"
STATE_FAILED = "FAILED"
STATE_BLOCKED = "BLOCKED"
STATE_NOT_APPLICABLE = "NOT_APPLICABLE"
#: Non-terminal: nothing has addressed this requirement yet. The validator
#: refuses to seal over it.
STATE_UNRESOLVED = "UNRESOLVED"

KNOWN_STATES = frozenset({STATE_SATISFIED, STATE_FAILED, STATE_BLOCKED,
                          STATE_NOT_APPLICABLE, STATE_UNRESOLVED})

TERMINAL_STATES = frozenset({STATE_SATISFIED, STATE_FAILED, STATE_BLOCKED,
                             STATE_NOT_APPLICABLE})


class EvidenceContractError(ValueError):
    """Raised when a requirement, state or closure is not representable."""


def _clean(value: object, field_name: str, *, required: bool = True) -> str:
    text = "" if value is None else str(value).strip()
    if required and not text:
        raise EvidenceContractError(f"{field_name} must be non-empty")
    return text


@dataclass(frozen=True)
class EvidenceRequirement:
    """One piece of proof a sealed package must produce. ``freshness_rule`` is set
    only for live requirements (probes, measurements); test results are timeless
    for a fixed source."""

    requirement_id: str
    kind: str
    vantage: str
    expected_verifier: str
    independence: str = INDEPENDENCE_HARNESS_HIDDEN
    freshness_rule: str = ""
    allow_blocked: bool = False
    mandatory: bool = True

    def __post_init__(self) -> None:
        _clean(self.requirement_id, "requirement_id")
        if self.kind not in KNOWN_KINDS:
            raise EvidenceContractError(
                f"unknown requirement kind {self.kind!r}; "
                f"known: {sorted(KNOWN_KINDS)}")
        _clean(self.vantage, "vantage")
        _clean(self.expected_verifier, "expected_verifier")
        if self.independence not in KNOWN_INDEPENDENCE:
            raise EvidenceContractError(
                f"unknown independence class {self.independence!r}; "
                f"known: {sorted(KNOWN_INDEPENDENCE)}")

    @property
    def requires_independence(self) -> bool:
        """True when only an independent class may close this requirement."""
        return self.independence != INDEPENDENCE_WORKER_AUTHORED

    def accepts(self, proof_class: str) -> bool:
        """Whether a receipt carrying ``proof_class`` may close this one."""
        if not self.requires_independence:
            return proof_class in KNOWN_INDEPENDENCE
        return proof_class in INDEPENDENT_CLASSES

    def to_dict(self) -> dict:
        return {
            "requirement_id": self.requirement_id,
            "kind": self.kind,
            "vantage": self.vantage,
            "expected_verifier": self.expected_verifier,
            "independence": self.independence,
            "freshness_rule": self.freshness_rule,
            "allow_blocked": self.allow_blocked,
            "mandatory": self.mandatory,
        }


@dataclass(frozen=True)
class RequirementState:
    """The computed closure of one requirement. Derived, never asserted."""

    requirement_id: str
    state: str
    receipt_id: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        if self.state not in KNOWN_STATES:
            raise EvidenceContractError(
                f"unknown requirement state {self.state!r}; "
                f"known: {sorted(KNOWN_STATES)}")

    @property
    def resolved(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def satisfied(self) -> bool:
        return self.state == STATE_SATISFIED

    def to_dict(self) -> dict:
        return {"requirement_id": self.requirement_id, "state": self.state,
                "receipt_id": self.receipt_id, "detail": self.detail}


def requirement_index(requirements: Iterable[EvidenceRequirement]
                      ) -> Tuple[EvidenceRequirement, ...]:
    """Requirements in stable order. Duplicate ids are refused, not merged, since
    one id could then be both SATISFIED and FAILED."""
    items = tuple(requirements or ())
    seen: set = set()
    for req in items:
        if not isinstance(req, EvidenceRequirement):
            raise EvidenceContractError(
                f"expected EvidenceRequirement, got {type(req).__name__}")
        if req.requirement_id in seen:
            raise EvidenceContractError(
                f"duplicate requirement id: {req.requirement_id!r}")
        seen.add(req.requirement_id)
    return items


CLAIM_PASS = "PASS"
CLAIM_FAIL = "FAIL"
CLAIM_BLOCKED = "BLOCKED"
#: The receipt ran but couldn't decide; distinct from nobody trying.
CLAIM_INCONCLUSIVE = "INCONCLUSIVE"

KNOWN_CLAIM_OUTCOMES = frozenset({CLAIM_PASS, CLAIM_FAIL, CLAIM_BLOCKED,
                                  CLAIM_INCONCLUSIVE})


@dataclass(frozen=True)
class RequirementClaim:
    """What one receipt says about one requirement. ``proof_class`` may differ from
    the class the requirement demands; closure catches that."""

    requirement_id: str
    receipt_id: str
    outcome: str
    proof_class: str = INDEPENDENCE_HARNESS_HIDDEN
    detail: str = ""

    def __post_init__(self) -> None:
        _clean(self.requirement_id, "requirement_id")
        _clean(self.receipt_id, "receipt_id")
        if self.outcome not in KNOWN_CLAIM_OUTCOMES:
            raise EvidenceContractError(
                f"unknown claim outcome {self.outcome!r}; "
                f"known: {sorted(KNOWN_CLAIM_OUTCOMES)}")
        if self.proof_class not in KNOWN_INDEPENDENCE:
            raise EvidenceContractError(
                f"unknown proof class {self.proof_class!r}; "
                f"known: {sorted(KNOWN_INDEPENDENCE)}")

    def to_dict(self) -> dict:
        return {"requirement_id": self.requirement_id, "receipt_id": self.receipt_id,
                "outcome": self.outcome, "proof_class": self.proof_class,
                "detail": self.detail}


def close_requirements(
    requirements: Sequence[EvidenceRequirement],
    claims: Sequence[RequirementClaim],
    *,
    waivers: Mapping[str, str] | None = None,
) -> Tuple[RequirementState, ...]:
    """Reduce each requirement to exactly one state, deterministically.

    Precedence, highest first:

      1. an accepted PASS      -> SATISFIED
      2. any FAIL              -> FAILED
      3. a recorded waiver     -> NOT_APPLICABLE
      4. any BLOCKED           -> BLOCKED  (allowed or not; the validator judges)
      5. otherwise             -> UNRESOLVED

    FAIL outranks a waiver: a judgement must not erase a measurement. A PASS from
    an unaccepted proof class is FAILED with the class named, so worker-authored
    proof can't be laundered into independent acceptance.
    """
    items = requirement_index(requirements)
    waivers = dict(waivers or {})
    by_requirement: dict = {}
    for claim in claims or ():
        if not isinstance(claim, RequirementClaim):
            raise EvidenceContractError(
                f"expected RequirementClaim, got {type(claim).__name__}")
        by_requirement.setdefault(claim.requirement_id, []).append(claim)

    states = []
    for req in items:
        relevant = sorted(by_requirement.get(req.requirement_id, ()),
                          key=lambda c: (c.receipt_id, c.outcome))
        accepted_pass = next(
            (c for c in relevant
             if c.outcome == CLAIM_PASS and req.accepts(c.proof_class)), None)
        if accepted_pass is not None:
            states.append(RequirementState(
                req.requirement_id, STATE_SATISFIED,
                receipt_id=accepted_pass.receipt_id,
                detail=f"PASS by {accepted_pass.proof_class}"))
            continue

        failed = next((c for c in relevant if c.outcome == CLAIM_FAIL), None)
        if failed is not None:
            states.append(RequirementState(
                req.requirement_id, STATE_FAILED, receipt_id=failed.receipt_id,
                detail=failed.detail or "verifier reported FAIL"))
            continue

        rejected = next((c for c in relevant if c.outcome == CLAIM_PASS), None)
        if rejected is not None:
            states.append(RequirementState(
                req.requirement_id, STATE_FAILED, receipt_id=rejected.receipt_id,
                detail=(f"PASS carried by {rejected.proof_class}, which cannot "
                        f"close a {req.independence} requirement")))
            continue

        if req.requirement_id in waivers:
            states.append(RequirementState(
                req.requirement_id, STATE_NOT_APPLICABLE,
                detail=str(waivers[req.requirement_id])))
            continue

        blocked = next((c for c in relevant if c.outcome == CLAIM_BLOCKED), None)
        if blocked is not None:
            states.append(RequirementState(
                req.requirement_id, STATE_BLOCKED, receipt_id=blocked.receipt_id,
                detail=(blocked.detail or "verifier reported BLOCKED") +
                       ("" if req.allow_blocked else
                        " (BLOCKED is not an allowed disposition for this "
                        "requirement)")))
            continue

        states.append(RequirementState(
            req.requirement_id, STATE_UNRESOLVED,
            detail="no receipt addressed this requirement"))
    return tuple(states)


def states_by_id(states: Iterable[RequirementState]) -> dict:
    return {state.requirement_id: state for state in states or ()}


def unresolved_mandatory(
    requirements: Sequence[EvidenceRequirement],
    states: Sequence[RequirementState],
) -> Tuple[str, ...]:
    """Mandatory requirements that are UNRESOLVED, in stable order."""
    index = states_by_id(states)
    return tuple(req.requirement_id for req in requirement_index(requirements)
                 if req.mandatory
                 and index.get(req.requirement_id,
                               RequirementState(req.requirement_id,
                                                STATE_UNRESOLVED)).state
                 == STATE_UNRESOLVED)


def unsatisfied_mandatory(
    requirements: Sequence[EvidenceRequirement],
    states: Sequence[RequirementState],
) -> Tuple[str, ...]:
    """Mandatory requirements that are UNRESOLVED, FAILED or BLOCKED; the same
    rule the validator applies, so the two can't disagree."""
    index = states_by_id(states)
    gap = {STATE_UNRESOLVED, STATE_FAILED, STATE_BLOCKED}
    return tuple(req.requirement_id for req in requirement_index(requirements)
                 if req.mandatory
                 and index.get(
                     req.requirement_id,
                     RequirementState(req.requirement_id, STATE_UNRESOLVED)).state
                 in gap)
