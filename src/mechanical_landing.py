"""PS-578 deterministic mechanical landing boundary.

This module deliberately contains no semantic judgement and no deployment
authority.  It consumes PS-638's sealed EvidencePackage plus an independently
source-bound semantic acceptance, revalidates both immediately before a
repository adapter is called, and records exactly what happened.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Protocol, Sequence, Tuple

from src.evidence_package import validate_evidence_package
from src.source_snapshot import SourceSnapshotIdentity, snapshot_digest_is_valid


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
    """A reviewer decision; prose is deliberately not an acceptance proof."""

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
class GovernanceResult:
    check_id: str
    status: str
    current: bool = True
    observed_at: str = ""
    result_hash: str = ""


@dataclass(frozen=True)
class LandingEligibility:
    eligible: bool
    reasons: Tuple[LandingRefused, ...] = ()

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(r.code.value for r in self.reasons)

    def require(self) -> None:
        if not self.eligible:
            raise self.reasons[0]


@dataclass(frozen=True)
class LandingPolicy:
    allowed_strategies: Tuple[LandingStrategy, ...] = (LandingStrategy.FAST_FORWARD,)
    required_checks: Tuple[str, ...] = ()


def _source(package: Mapping[str, Any]) -> Mapping[str, Any]:
    return package.get("execution_package", {}).get("source", {})


def _same_candidate(source: Mapping[str, Any], acceptance: SemanticAcceptance) -> bool:
    return (source.get("snapshot_digest") == acceptance.candidate_source_digest
            and source.get("head_sha") == acceptance.candidate_head_sha
            and (not acceptance.candidate_tree_sha or
                 source.get("tree_sha", acceptance.candidate_tree_sha) == acceptance.candidate_tree_sha)
            and (not acceptance.candidate_diff_digest or
                 source.get("tracked_diff_digest") == acceptance.candidate_diff_digest))


def evaluate_landing_eligibility(
    evidence_package: Mapping[str, Any],
    acceptance: SemanticAcceptance | None,
    *,
    current_source: Mapping[str, Any],
    governance: Sequence[GovernanceResult] = (),
    policy: LandingPolicy = LandingPolicy(),
    strategy: LandingStrategy = LandingStrategy.FAST_FORWARD,
    unresolved_dispositions: Sequence[str] = (),
) -> LandingEligibility:
    """Pure, side-effect-free eligibility decision."""
    failures: list[LandingRefused] = []
    if evidence_package.get("invalidated") is True:
        failures.append(LandingRefused(LandingRefusalCode.EVIDENCE_INVALID,
                                       "package is explicitly invalidated"))
    if evidence_package.get("stale") is True:
        failures.append(LandingRefused(LandingRefusalCode.EVIDENCE_STALE,
                                       "package is explicitly stale"))
    validation = validate_evidence_package(evidence_package, current_source=current_source)
    if not validation.ok:
        codes = {issue.code for issue in validation.issues}
        if "source_changed_after_verification" in codes:
            failures.append(LandingRefused(LandingRefusalCode.EVIDENCE_STALE, "source changed after verification"))
        elif "evidence_package_hash_mismatch" in codes:
            failures.append(LandingRefused(LandingRefusalCode.EVIDENCE_INVALID, "package hash mismatch"))
        else:
            failures.append(LandingRefused(LandingRefusalCode.EVIDENCE_NOT_VERIFIED, validation.explain()))
    if acceptance is None:
        failures.append(LandingRefused(LandingRefusalCode.SEMANTIC_ACCEPTANCE_MISSING))
    else:
        if acceptance.disposition != "ACCEPTED":
            failures.append(LandingRefused(LandingRefusalCode.UNRESOLVED_REWORK, acceptance.disposition))
        if acceptance.evidence_package_hash != evidence_package.get("evidence_package_hash"):
            failures.append(LandingRefused(LandingRefusalCode.ACCEPTANCE_SOURCE_MISMATCH, "different EvidencePackage"))
        if not acceptance.acceptance_hash or acceptance.acceptance_hash != _digest(acceptance.core()):
            failures.append(LandingRefused(LandingRefusalCode.ACCEPTANCE_SOURCE_MISMATCH, "acceptance hash mismatch"))
        if not _same_candidate(_source(evidence_package), acceptance):
            failures.append(LandingRefused(LandingRefusalCode.ACCEPTANCE_SOURCE_MISMATCH, "candidate identity differs"))
        if not _same_candidate(current_source, acceptance):
            failures.append(LandingRefused(LandingRefusalCode.CANDIDATE_CHANGED, "candidate changed after acceptance"))
    if not snapshot_digest_is_valid(dict(current_source)):
        failures.append(LandingRefused(LandingRefusalCode.CANDIDATE_CHANGED, "current source identity is not sealed"))
    checks = {g.check_id: g for g in governance}
    for check_id in policy.required_checks:
        result = checks.get(check_id)
        if result is None or result.status.upper() != "GREEN" or not result.current:
            failures.append(LandingRefused(LandingRefusalCode.GOVERNANCE_NOT_GREEN, check_id))
    if unresolved_dispositions:
        failures.append(LandingRefused(LandingRefusalCode.UNRESOLVED_REWORK, ",".join(unresolved_dispositions)))
    if strategy not in policy.allowed_strategies:
        failures.append(LandingRefused(LandingRefusalCode.LANDING_STRATEGY_NOT_ALLOWED, strategy.value))
    declared = set(evidence_package.get("execution_package", {}).get("write_scope", ()))
    actual = set(evidence_package.get("execution_package", {}).get("actual_write_set", ()))
    if actual - declared:
        failures.append(LandingRefused(LandingRefusalCode.WRITE_SCOPE_MISMATCH, repr(sorted(actual - declared))))
    return LandingEligibility(not failures, tuple(failures))


@dataclass(frozen=True)
class EquivalenceResult:
    equivalent: bool
    reason: str
    accepted_sha: str
    landed_sha: str


def prove_landed_equivalence(acceptance: SemanticAcceptance, landed: Mapping[str, Any],
                            strategy: LandingStrategy) -> EquivalenceResult:
    """Compare material identity, never commit SHA, for SHA-changing strategies."""
    landed_sha = str(landed.get("head_sha", ""))
    if landed_sha == acceptance.candidate_head_sha and strategy == LandingStrategy.FAST_FORWARD:
        return EquivalenceResult(True, "exact accepted commit identity", acceptance.candidate_head_sha, landed_sha)
    if strategy == LandingStrategy.FAST_FORWARD and landed_sha != acceptance.candidate_head_sha:
        return EquivalenceResult(False, "fast-forward changed commit identity", acceptance.candidate_head_sha, landed_sha)
    if not acceptance.candidate_tree_sha and not acceptance.candidate_diff_digest:
        return EquivalenceResult(False, "no material tree/diff identity was accepted", acceptance.candidate_head_sha, landed_sha)
    same_tree = (not acceptance.candidate_tree_sha or
                 landed.get("tree_sha") == acceptance.candidate_tree_sha)
    same_diff = (not acceptance.candidate_diff_digest or
                 landed.get("diff_digest", landed.get("tracked_diff_digest")) == acceptance.candidate_diff_digest)
    return EquivalenceResult(bool(same_tree and same_diff),
                             "equivalent material tree/diff" if same_tree and same_diff else "material tree/diff differs",
                             acceptance.candidate_head_sha, landed_sha)


class RepositoryLandingAdapter(Protocol):
    def land(self, strategy: LandingStrategy) -> Mapping[str, Any]: ...
    def current_source(self) -> Mapping[str, Any]: ...


class JiraReconciliationAdapter(Protocol):
    def reconcile(self, receipt: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class LandingReceipt:
    evidence_package_hash: str
    semantic_acceptance_hash: str
    accepted_candidate_head: str
    accepted_source_digest: str
    accepted_tree_sha: str
    accepted_diff_digest: str
    landing_strategy: str
    pre_land_governance: Tuple[Mapping[str, Any], ...]
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
        return {k: (v.to_dict() if hasattr(v, "to_dict") else v) for k, v in self.__dict__.items()
                if k not in ("receipt_hash", "equivalence")} | {"equivalence": self.equivalence.__dict__}

    def to_dict(self) -> dict:
        return {**self.core(), "receipt_hash": self.receipt_hash}


def land_exact_candidate(*, evidence_package: Mapping[str, Any], acceptance: SemanticAcceptance,
                        repository: RepositoryLandingAdapter, jira: JiraReconciliationAdapter | None,
                        current_source: Mapping[str, Any], governance: Sequence[GovernanceResult],
                        policy: LandingPolicy, strategy: LandingStrategy,
                        unresolved_dispositions: Sequence[str] = ()) -> LandingReceipt:
    """Revalidate immediately before mutation, land, prove, then reconcile Jira."""
    eligibility = evaluate_landing_eligibility(evidence_package, acceptance,
        current_source=current_source, governance=governance, policy=policy,
        strategy=strategy, unresolved_dispositions=unresolved_dispositions)
    eligibility.require()
    # The adapter's read is the TOCTOU boundary; no mutation occurs until this passes.
    if repository.current_source() != current_source:
        raise LandingRefused(LandingRefusalCode.CANDIDATE_CHANGED, "source changed at landing boundary")
    landed = dict(repository.land(strategy))
    equivalence = prove_landed_equivalence(acceptance, landed, strategy)
    if not equivalence.equivalent:
        raise LandingRefused(LandingRefusalCode.LANDED_TREE_NOT_EQUIVALENT, equivalence.reason)
    provisional = LandingReceipt(
        evidence_package_hash=str(evidence_package["evidence_package_hash"]),
        semantic_acceptance_hash=acceptance.acceptance_hash,
        accepted_candidate_head=acceptance.candidate_head_sha,
        accepted_source_digest=acceptance.candidate_source_digest,
        accepted_tree_sha=acceptance.candidate_tree_sha,
        accepted_diff_digest=acceptance.candidate_diff_digest,
        landing_strategy=strategy.value,
        pre_land_governance=tuple(g.__dict__ for g in governance),
        landed_head=str(landed.get("head_sha", "")),
        landed_tree_sha=str(landed.get("tree_sha", "")),
        equivalence=equivalence, repository=str(landed.get("repository", "")),
        destination_branch=str(landed.get("destination_branch", "")),
        jira_result={}, reconciliation_state="PENDING")
    jira_result: Mapping[str, Any] = {}
    state = "REPOSITORY_LANDED_JIRA_PENDING"
    error_code = ""
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
