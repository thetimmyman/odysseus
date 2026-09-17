"""PS-578 deterministic landing over PS-638 sealed evidence.

ExecutionPackage.source is input provenance. A writable run's accepted
candidate is the source snapshot named by its passing VerificationReceipt(s).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, Tuple

from src.attempt_receipt import recompute_outcome, verification_receipt_hash_is_valid
from src.evidence_package import (
    EVIDENCE_PACKAGE_HASH_MISMATCH, SOURCE_CHANGED_AFTER_VERIFICATION,
    SOURCE_SNAPSHOT_DIGEST_INVALID, WRITE_OUTSIDE_AUTHORIZED_SCOPE,
    validate_evidence_package,
)
from src.source_snapshot import snapshot_digest_is_valid


class LandingRefusalCode(str, Enum):
    EVIDENCE_NOT_VERIFIED = "EVIDENCE_NOT_VERIFIED"
    EVIDENCE_STALE = "EVIDENCE_STALE"
    EVIDENCE_INVALID = "EVIDENCE_INVALID"
    SEMANTIC_ACCEPTANCE_MISSING = "SEMANTIC_ACCEPTANCE_MISSING"
    ACCEPTANCE_SOURCE_MISMATCH = "ACCEPTANCE_SOURCE_MISMATCH"
    CANDIDATE_CHANGED = "CANDIDATE_CHANGED"
    GOVERNANCE_NOT_GREEN = "GOVERNANCE_NOT_GREEN"
    WRITE_SCOPE_MISMATCH = "WRITE_SCOPE_MISMATCH"
    UNRESOLVED_REWORK = "UNRESOLVED_REWORK"
    LANDING_STRATEGY_NOT_ALLOWED = "LANDING_STRATEGY_NOT_ALLOWED"
    LANDED_TREE_NOT_EQUIVALENT = "LANDED_TREE_NOT_EQUIVALENT"
    JIRA_RECONCILIATION_FAILED = "JIRA_RECONCILIATION_FAILED"


class LandingRefused(ValueError):
    def __init__(self, code: LandingRefusalCode, detail: str = "") -> None:
        self.code, self.detail = code, detail
        super().__init__(f"{code.value}: {detail}" if detail else code.value)


class LandingStrategy(str, Enum):
    FAST_FORWARD = "fast-forward"
    MERGE = "merge"
    REBASE = "rebase"
    SQUASH = "squash"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class SemanticAcceptance:
    acceptance_id: str
    reviewer_id: str
    evidence_package_hash: str
    candidate_source_digest: str
    candidate_head_sha: str
    candidate_tree_sha: str = ""
    candidate_diff_digest: str = ""
    disposition: str = "ACCEPTED"
    observed_at: str = ""
    accepted_at: str = ""
    policy_ref: str = ""
    role: str = "semantic_reviewer"
    acceptance_hash: str = field(default="")

    def __post_init__(self) -> None:
        for name in ("acceptance_id", "reviewer_id", "evidence_package_hash",
                     "candidate_source_digest", "candidate_head_sha",
                     "observed_at", "accepted_at"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"semantic acceptance {name} must be non-empty")
        if self.disposition not in {"ACCEPTED", "REWORK", "BLOCKED", "REJECTED"}:
            raise ValueError(f"unknown semantic acceptance disposition: {self.disposition}")

    def core(self) -> dict:
        return {k: getattr(self, k) for k in (
            "acceptance_id", "reviewer_id", "evidence_package_hash",
            "candidate_source_digest", "candidate_head_sha", "candidate_tree_sha",
            "candidate_diff_digest", "disposition", "observed_at", "accepted_at",
            "policy_ref", "role")}

    def to_dict(self) -> dict:
        return {**self.core(), "acceptance_hash": self.acceptance_hash}


def make_semantic_acceptance(**kwargs: Any) -> SemanticAcceptance:
    kwargs.pop("acceptance_hash", None)
    record = SemanticAcceptance(**kwargs)
    return SemanticAcceptance(**{**record.core(), "acceptance_hash": _digest(record.core())})


@dataclass(frozen=True)
class LandingEligibility:
    eligible: bool
    reasons: Tuple[LandingRefused, ...] = ()

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(reason.code.value for reason in self.reasons)

    def require(self) -> None:
        if not self.eligible:
            raise self.reasons[0]


@dataclass(frozen=True)
class LandingPolicy:
    allowed_strategies: Tuple[LandingStrategy, ...] = (LandingStrategy.FAST_FORWARD,)
    required_checks: Tuple[str, ...] = ()


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    return {}


def _verified_candidate_digest(evidence_package: Mapping[str, Any]) -> str | None:
    receipts = evidence_package.get("verification_receipts") or ()
    digests = {str(r.get("source_snapshot_digest") or "") for r in receipts
               if recompute_outcome(r) == "PASS"}
    digests.discard("")
    return next(iter(digests)) if len(digests) == 1 else None


def _candidate_matches(source: Mapping[str, Any], acceptance: SemanticAcceptance) -> bool:
    """Match only fields owned by canonical SourceSnapshotIdentity.

    ``candidate_tree_sha`` is repository landing material, not a field of the
    PS-638 snapshot. It is intentionally checked only by equivalence proof.
    """
    return (source.get("snapshot_digest") == acceptance.candidate_source_digest
            and source.get("head_sha") == acceptance.candidate_head_sha
            and (not acceptance.candidate_diff_digest or
                 source.get("tracked_diff_digest") == acceptance.candidate_diff_digest))


def _validation_failures(validation: Any) -> list[LandingRefused]:
    codes = {issue.code for issue in validation.issues}
    failures: list[LandingRefused] = []
    if SOURCE_CHANGED_AFTER_VERIFICATION in codes:
        failures.append(LandingRefused(LandingRefusalCode.EVIDENCE_STALE,
                                       "canonical evidence no longer matches current source"))
    if WRITE_OUTSIDE_AUTHORIZED_SCOPE in codes:
        failures.append(LandingRefused(LandingRefusalCode.WRITE_SCOPE_MISMATCH,
                                       "AttemptReceipt recorded an out-of-scope write"))
    if {EVIDENCE_PACKAGE_HASH_MISMATCH, SOURCE_SNAPSHOT_DIGEST_INVALID} & codes:
        failures.append(LandingRefused(LandingRefusalCode.EVIDENCE_INVALID,
                                       "canonical evidence integrity failed"))
    if not failures:
        failures.append(LandingRefused(LandingRefusalCode.EVIDENCE_NOT_VERIFIED,
                                       validation.explain()))
    return failures


def _governance_ok(record: Any, candidate_digest: str, check_id: str) -> bool:
    payload = _mapping(record)
    identity = str(payload.get("control_id") or payload.get("verifier_id") or "")
    return (identity == check_id
            and payload.get("source_snapshot_digest") == candidate_digest
            and bool(str(payload.get("ended_at") or payload.get("started_at") or "").strip())
            and verification_receipt_hash_is_valid(payload)
            and recompute_outcome(payload) == "PASS")


def _canonical_source_is_valid(source: Mapping[str, Any]) -> bool:
    """Treat every malformed source payload as invalid, including schema errors."""
    try:
        return snapshot_digest_is_valid(dict(source))
    except (TypeError, ValueError, KeyError):
        return False


def evaluate_landing_eligibility(
    evidence_package: Mapping[str, Any], acceptance: SemanticAcceptance | None, *,
    current_source: Mapping[str, Any], governance: Sequence[Any] = (),
    policy: LandingPolicy = LandingPolicy(),
    strategy: LandingStrategy = LandingStrategy.FAST_FORWARD,
    unresolved_dispositions: Sequence[str] = (),
) -> LandingEligibility:
    """Pure deterministic evaluation; no merge, Jira, or deployment side effect."""
    failures: list[LandingRefused] = []
    if not _canonical_source_is_valid(current_source):
        failures.append(LandingRefused(
            LandingRefusalCode.EVIDENCE_INVALID,
            "current_source is not a valid canonical SourceSnapshotIdentity"))
    validation = validate_evidence_package(evidence_package, current_source=current_source)
    if not validation.ok:
        failures.extend(_validation_failures(validation))

    verified_digest = _verified_candidate_digest(evidence_package)
    if verified_digest is None:
        failures.append(LandingRefused(LandingRefusalCode.EVIDENCE_NOT_VERIFIED,
                                       "no single passing canonical verification source"))
    elif current_source.get("snapshot_digest") != verified_digest:
        failures.append(LandingRefused(LandingRefusalCode.CANDIDATE_CHANGED,
                                       "current source is not the verified output candidate"))

    if acceptance is None:
        failures.append(LandingRefused(LandingRefusalCode.SEMANTIC_ACCEPTANCE_MISSING))
    else:
        if acceptance.disposition != "ACCEPTED":
            failures.append(LandingRefused(LandingRefusalCode.UNRESOLVED_REWORK,
                                           acceptance.disposition))
        if acceptance.evidence_package_hash != evidence_package.get("evidence_package_hash"):
            failures.append(LandingRefused(LandingRefusalCode.ACCEPTANCE_SOURCE_MISMATCH,
                                           "different EvidencePackage"))
        if acceptance.acceptance_hash != _digest(acceptance.core()):
            failures.append(LandingRefused(LandingRefusalCode.ACCEPTANCE_SOURCE_MISMATCH,
                                           "acceptance hash mismatch"))
        if verified_digest != acceptance.candidate_source_digest:
            failures.append(LandingRefused(LandingRefusalCode.ACCEPTANCE_SOURCE_MISMATCH,
                                           "acceptance is not bound to verified output"))
        if not _candidate_matches(current_source, acceptance):
            failures.append(LandingRefused(LandingRefusalCode.CANDIDATE_CHANGED,
                                           "candidate changed after acceptance"))

    available = {_mapping(item).get("control_id") or _mapping(item).get("verifier_id"): item
                 for item in governance}
    for check_id in policy.required_checks:
        if not _governance_ok(available.get(check_id), verified_digest or "", check_id):
            failures.append(LandingRefused(LandingRefusalCode.GOVERNANCE_NOT_GREEN, check_id))
    if unresolved_dispositions:
        failures.append(LandingRefused(LandingRefusalCode.UNRESOLVED_REWORK,
                                       ",".join(unresolved_dispositions)))
    if strategy not in policy.allowed_strategies:
        failures.append(LandingRefused(LandingRefusalCode.LANDING_STRATEGY_NOT_ALLOWED,
                                       strategy.value))
    return LandingEligibility(not failures, tuple(failures))


@dataclass(frozen=True)
class EquivalenceResult:
    equivalent: bool
    reason: str
    accepted_sha: str
    landed_sha: str

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def prove_landed_equivalence(acceptance: SemanticAcceptance, landed: Mapping[str, Any],
                            strategy: LandingStrategy) -> EquivalenceResult:
    landed_sha = str(landed.get("head_sha", ""))
    if strategy == LandingStrategy.FAST_FORWARD and landed_sha == acceptance.candidate_head_sha:
        return EquivalenceResult(True, "exact accepted commit identity",
                                 acceptance.candidate_head_sha, landed_sha)
    if strategy == LandingStrategy.FAST_FORWARD:
        return EquivalenceResult(False, "fast-forward changed commit identity",
                                 acceptance.candidate_head_sha, landed_sha)
    if not acceptance.candidate_tree_sha and not acceptance.candidate_diff_digest:
        return EquivalenceResult(False, "no accepted material tree/diff identity",
                                 acceptance.candidate_head_sha, landed_sha)
    same_tree = (not acceptance.candidate_tree_sha or
                 landed.get("tree_sha") == acceptance.candidate_tree_sha)
    same_diff = (not acceptance.candidate_diff_digest or
                 landed.get("diff_digest", landed.get("tracked_diff_digest"))
                 == acceptance.candidate_diff_digest)
    return EquivalenceResult(bool(same_tree and same_diff),
                             "equivalent material tree/diff" if same_tree and same_diff
                             else "material tree/diff differs",
                             acceptance.candidate_head_sha, landed_sha)


class RepositoryLandingAdapter(Protocol):
    def land(self, strategy: LandingStrategy) -> Mapping[str, Any]: ...
    def current_source(self) -> Mapping[str, Any]: ...


class JiraReconciliationAdapter(Protocol):
    def reconcile(self, receipt: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class LandingReceipt:
    evidence_package_id: str
    evidence_package_hash: str
    semantic_acceptance_id: str
    semantic_acceptance_hash: str
    accepted_candidate: Mapping[str, Any]
    landing_strategy: str
    pre_land_governance: Tuple[Mapping[str, Any], ...]
    landed_result: Mapping[str, Any]
    landed_head: str
    landed_tree_sha: str
    equivalence: EquivalenceResult
    repository: str
    destination_branch: str
    jira_result: Mapping[str, Any]
    reconciliation_state: str
    reconciliation_error_code: str = ""
    deployment_implication: str = "none"
    created_at: str = field(default_factory=_now)
    receipt_hash: str = ""

    def core(self) -> dict:
        return {
            "evidence_package_id": self.evidence_package_id,
            "evidence_package_hash": self.evidence_package_hash,
            "semantic_acceptance_id": self.semantic_acceptance_id,
            "semantic_acceptance_hash": self.semantic_acceptance_hash,
            "accepted_candidate": dict(self.accepted_candidate),
            "landing_strategy": self.landing_strategy,
            "pre_land_governance": [dict(item) for item in self.pre_land_governance],
            "landed_result": dict(self.landed_result),
            "landed_head": self.landed_head,
            "landed_tree_sha": self.landed_tree_sha,
            "equivalence": self.equivalence.to_dict(),
            "repository": self.repository,
            "destination_branch": self.destination_branch,
            "jira_result": dict(self.jira_result),
            "reconciliation_state": self.reconciliation_state,
            "reconciliation_error_code": self.reconciliation_error_code,
            "deployment_implication": self.deployment_implication,
            "created_at": self.created_at,
        }

    def to_dict(self) -> dict:
        return {**self.core(), "receipt_hash": self.receipt_hash}


def landing_receipt_hash_is_valid(payload: Mapping[str, Any]) -> bool:
    if not isinstance(payload, Mapping) or not payload.get("receipt_hash"):
        return False
    return _digest({k: v for k, v in payload.items() if k != "receipt_hash"}) == payload["receipt_hash"]


def land_exact_candidate(*, evidence_package: Mapping[str, Any], acceptance: SemanticAcceptance,
                        repository: RepositoryLandingAdapter, jira: JiraReconciliationAdapter | None,
                        current_source: Mapping[str, Any], governance: Sequence[Any],
                        policy: LandingPolicy, strategy: LandingStrategy,
                        unresolved_dispositions: Sequence[str] = ()) -> LandingReceipt:
    eligibility = evaluate_landing_eligibility(
        evidence_package, acceptance, current_source=current_source,
        governance=governance, policy=policy, strategy=strategy,
        unresolved_dispositions=unresolved_dispositions)
    eligibility.require()
    if repository.current_source() != current_source:
        raise LandingRefused(LandingRefusalCode.CANDIDATE_CHANGED,
                             "source changed at landing boundary")
    landed = dict(repository.land(strategy))
    equivalence = prove_landed_equivalence(acceptance, landed, strategy)
    if not equivalence.equivalent:
        raise LandingRefused(LandingRefusalCode.LANDED_TREE_NOT_EQUIVALENT,
                             equivalence.reason)

    provisional = LandingReceipt(
        evidence_package_id=str(evidence_package.get("evidence_package_id", "")),
        evidence_package_hash=str(evidence_package["evidence_package_hash"]),
        semantic_acceptance_id=acceptance.acceptance_id,
        semantic_acceptance_hash=acceptance.acceptance_hash,
        accepted_candidate={"source_digest": acceptance.candidate_source_digest,
                            "head_sha": acceptance.candidate_head_sha,
                            "tree_sha": acceptance.candidate_tree_sha,
                            "diff_digest": acceptance.candidate_diff_digest},
        landing_strategy=strategy.value,
        pre_land_governance=tuple(_mapping(item) for item in governance),
        landed_result=landed, landed_head=str(landed.get("head_sha", "")),
        landed_tree_sha=str(landed.get("tree_sha", "")), equivalence=equivalence,
        repository=str(landed.get("repository", "")),
        destination_branch=str(landed.get("destination_branch", "")),
        jira_result={}, reconciliation_state="PENDING")

    jira_result: Mapping[str, Any] = {}
    state, error_code = "REPOSITORY_LANDED_JIRA_PENDING", ""
    if jira is not None:
        try:
            jira_result = jira.reconcile(provisional.to_dict())
            state = "RECONCILED"
        except Exception as exc:
            jira_result = {"ok": False, "error": str(exc)}
            state = "REPOSITORY_LANDED_JIRA_FAILED"
            error_code = LandingRefusalCode.JIRA_RECONCILIATION_FAILED.value
    final = LandingReceipt(**{**provisional.__dict__, "jira_result": jira_result,
                              "reconciliation_state": state,
                              "reconciliation_error_code": error_code})
    return LandingReceipt(**{**final.__dict__, "receipt_hash": _digest(final.core())})
