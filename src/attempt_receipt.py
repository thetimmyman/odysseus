"""src/attempt_receipt.py — AttemptReceipt + VerificationReceipt (PS-638).

The other half of the envelope: what actually happened.

The rules this module enforces mechanically, because prose cannot:

**Attempt 2 never overwrites attempt 1.** A receipt is frozen and
content-addressed per attempt. There is no mutable "current attempt" to
overwrite, so "final green erased an earlier red" is not a mistake a caller can
make here — PS-638 hardening item 4 requires that every retry survive.

**A verdict is DERIVED, never asserted.** ``VerificationReceipt`` has no
``passed`` field to set. The outcome comes from ``exit_code`` plus capture
completeness, so a nonzero command cannot become PASS because the surrounding
text looked reassuring, and a truncated log cannot become PASS either — an
absence inside a truncated capture is not an absence. Both directions are
deliberately symmetric: truncation poisons a PASS exactly as much as it poisons
a claim of absence.

**A pre-existing failure claim needs a baseline, not an opinion.** PS-638
requires an exact-base receipt with a matching normalized failure identity. The
receipt can carry the claim; only :func:`classify_against_baseline` decides, and
it returns SAME / CHANGED / NEW from fingerprints — never from a sentence.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields
from typing import Any, Mapping, Optional, Sequence, Tuple

from src.evidence_contract import (
    CLAIM_BLOCKED,
    CLAIM_FAIL,
    CLAIM_INCONCLUSIVE,
    CLAIM_PASS,
    INDEPENDENCE_HARNESS_HIDDEN,
    KNOWN_INDEPENDENCE,
    RequirementClaim,
)

ATTEMPT_RECEIPT_SCHEMA_VERSION = 1
VERIFICATION_RECEIPT_SCHEMA_VERSION = 1

#: Failure classes, kept separate for the same reason the ledger keeps them
#: separate: an outage is not a task verdict, and a sizing error is neither.
FAILURE_CLASS_TECHNICAL = "technical"
FAILURE_CLASS_POLICY = "policy"
FAILURE_CLASS_RUNTIME_PROVIDER = "runtime_provider"
FAILURE_CLASS_INFRA = "infra"
FAILURE_CLASS_CONTEXT = "context"

KNOWN_FAILURE_CLASSES = frozenset({
    FAILURE_CLASS_TECHNICAL, FAILURE_CLASS_POLICY, FAILURE_CLASS_RUNTIME_PROVIDER,
    FAILURE_CLASS_INFRA, FAILURE_CLASS_CONTEXT,
})

#: Baseline comparison, per PS-638 hardening item 6.
BASELINE_SAME = "SAME"
BASELINE_CHANGED = "CHANGED"
BASELINE_NEW = "NEW"
BASELINE_UNPROVEN = "UNPROVEN"


class ReceiptError(ValueError):
    """Raised when a receipt cannot represent what it was asked to represent."""


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def context_projection_digest(rendered_context: str) -> str:
    """Digest of the EXACT text handed to the worker.

    One function, used by both the producer and the validator, so the hash in a
    receipt and the hash recomputed from the rendered artifact cannot be derived
    two different ways.
    """
    return _sha256_hex((rendered_context or "").encode("utf-8"))


def artifact_ref(artifact_id: str, content: str | bytes, *,
                 media_type: str = "text/plain", storage_uri: str = "") -> "ArtifactRef":
    """Build a content-addressed reference from in-memory content."""
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    return ArtifactRef(artifact_id=artifact_id, sha256=_sha256_hex(data),
                       media_type=media_type, size=len(data),
                       storage_uri=storage_uri)


# ---------------------------------------------------------------- artifact ---
@dataclass(frozen=True)
class ArtifactRef:
    """A content-addressed artifact reference.

    ``storage_uri`` is a PROJECTION: where the bytes happen to live right now.
    ``sha256`` is the identity. That split is what lets an evidence package
    survive a file being moved, and what makes an artifact hash mismatch a
    detectable event rather than a broken link.
    """

    artifact_id: str
    sha256: str
    media_type: str = "text/plain"
    size: int = 0
    storage_uri: str = ""

    def __post_init__(self) -> None:
        if not str(self.artifact_id or "").strip():
            raise ReceiptError("artifact_id must be non-empty")
        if not str(self.sha256 or "").strip():
            raise ReceiptError("artifact sha256 must be non-empty")

    def to_dict(self) -> dict:
        return {"artifact_id": self.artifact_id, "sha256": self.sha256,
                "media_type": self.media_type, "size": self.size,
                "storage_uri": self.storage_uri}


def _seal_record(cls, core: dict, hash_field: str, exclude: Sequence[str] = ()):
    """Build a frozen record, sealing a digest over its own ``core()``.

    ``exclude`` drops fields that are DERIVED rather than recorded. A derived
    field must not be part of the identity: if it were, the same fact could be
    written two ways and produce two different hashes, and a reader could not
    tell an edited verdict from a recomputed one.
    """
    provisional = cls(**core)
    sealed = {k: v for k, v in provisional.core().items() if k not in exclude}
    digest = _sha256_hex(_canonical(sealed))
    return cls(**{**core, hash_field: digest})


def _check_hash(payload: Mapping[str, Any], hash_field: str,
                exclude: Sequence[str] = ()) -> bool:
    if not isinstance(payload, Mapping) or not payload.get(hash_field):
        return False
    core = {k: v for k, v in payload.items()
            if k != hash_field and k not in exclude}
    return _sha256_hex(_canonical(core)) == payload[hash_field]


# ----------------------------------------------------------------- attempt ---
@dataclass(frozen=True)
class AttemptReceipt:
    """One actual attempt. Immutable, and never replaced by a later one.

    ``repair_of`` names the attempt this one repairs (0 for a fresh dispatch),
    which is what makes a repair chain reconstructible instead of inferred from
    timestamps.
    """

    receipt_id: str
    run_id: str
    packet_id: str
    attempt: int
    execution_package_hash: str
    dispatch_receipt_hash: str
    target_id: str
    host: str
    model: str
    context_projection_hash: str
    started_at: str = ""
    ended_at: str = ""
    elapsed_s: float = 0.0
    generation_token: str = ""
    runtime_kind: str = ""
    runtime_version: str = ""
    model_version: str = ""
    rendered_context_ref: Optional[ArtifactRef] = None
    output_ref: Optional[ArtifactRef] = None
    requested_context: int = 0
    served_context: int = 0
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_invocations: Tuple[Mapping[str, Any], ...] = ()
    declared_write_set: Tuple[str, ...] = ()
    actual_write_set: Tuple[str, ...] = ()
    artifact_refs: Tuple[ArtifactRef, ...] = ()
    failure_class: str = ""
    repair_of: int = 0
    schema_version: int = ATTEMPT_RECEIPT_SCHEMA_VERSION
    receipt_hash: str = field(default="")

    def __post_init__(self) -> None:
        for name in ("receipt_id", "run_id", "packet_id",
                     "execution_package_hash", "dispatch_receipt_hash",
                     "target_id", "host", "model", "context_projection_hash"):
            if not str(getattr(self, name) or "").strip():
                raise ReceiptError(f"attempt receipt {name} must be non-empty")
        if not isinstance(self.attempt, int) or self.attempt < 1:
            raise ReceiptError("attempt must be an integer >= 1")
        if self.failure_class and self.failure_class not in KNOWN_FAILURE_CLASSES:
            raise ReceiptError(
                f"unknown failure_class {self.failure_class!r}; "
                f"known: {sorted(KNOWN_FAILURE_CLASSES)}")
        if self.repair_of and self.repair_of >= self.attempt:
            raise ReceiptError(
                f"repair_of={self.repair_of} must reference an EARLIER attempt "
                f"than attempt={self.attempt}")

    # ---------------------------------------------------------- write scope ---
    @property
    def writes_outside_scope(self) -> Tuple[str, ...]:
        """Actual writes that were not declared. Empty is the only good answer."""
        return tuple(sorted(p for p in self.actual_write_set
                            if p not in self.declared_write_set))

    @property
    def is_repair(self) -> bool:
        return self.repair_of > 0

    @property
    def produced_artifacts(self) -> bool:
        return bool(self.artifact_refs or self.actual_write_set)

    def core(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "packet_id": self.packet_id,
            "attempt": self.attempt,
            "repair_of": self.repair_of,
            "execution_package_hash": self.execution_package_hash,
            "dispatch_receipt_hash": self.dispatch_receipt_hash,
            "generation_token": self.generation_token,
            "target_id": self.target_id,
            "host": self.host,
            "model": self.model,
            "runtime_kind": self.runtime_kind,
            "runtime_version": self.runtime_version,
            "model_version": self.model_version,
            "context_projection_hash": self.context_projection_hash,
            "rendered_context_ref": (self.rendered_context_ref.to_dict()
                                     if self.rendered_context_ref else None),
            "output_ref": self.output_ref.to_dict() if self.output_ref else None,
            "requested_context": self.requested_context,
            "served_context": self.served_context,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed_s": self.elapsed_s,
            "finish_reason": self.finish_reason,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "tool_invocations": [dict(t) for t in self.tool_invocations],
            "declared_write_set": list(self.declared_write_set),
            "actual_write_set": list(self.actual_write_set),
            "artifact_refs": [a.to_dict() for a in self.artifact_refs],
            "failure_class": self.failure_class,
        }

    def to_dict(self) -> dict:
        payload = self.core()
        payload["receipt_hash"] = self.receipt_hash
        return payload


def make_attempt_receipt(**kwargs: Any) -> AttemptReceipt:
    """Build an attempt receipt, normalizing its sequence fields."""
    known = {f.name for f in fields(AttemptReceipt)}
    unknown = set(kwargs) - known
    if unknown:
        raise ReceiptError(f"unknown attempt receipt field(s): {sorted(unknown)}")
    payload = dict(kwargs)
    payload.pop("receipt_hash", None)
    for name in ("tool_invocations", "declared_write_set", "actual_write_set",
                 "artifact_refs"):
        if name in payload and payload[name] is not None:
            payload[name] = tuple(payload[name])
    return _seal_record(AttemptReceipt, payload, "receipt_hash")


def attempt_receipt_hash_is_valid(payload: Mapping[str, Any]) -> bool:
    """True when a serialized attempt receipt's hash matches its own fields."""
    return _check_hash(payload, "receipt_hash")


# ------------------------------------------------------------ verification ---
@dataclass(frozen=True)
class VerificationReceipt:
    """One deterministic check, with its identity and its capture honesty.

    There is deliberately NO ``passed`` field. The verdict is :attr:`outcome`,
    derived from ``exit_code`` and capture completeness, so it cannot be set to
    something the command did not produce.
    """

    receipt_id: str
    run_id: str
    packet_id: str
    attempt: int
    execution_package_hash: str
    verifier_id: str
    normalized_command: str
    source_snapshot_digest: str
    exit_code: int
    verifier_digest: str = ""
    verifier_paths: Tuple[str, ...] = ()
    worktree: str = ""
    host: str = ""
    container: str = ""
    timeout_s: int = 0
    started_at: str = ""
    ended_at: str = ""
    elapsed_s: float = 0.0
    stdout_ref: Optional[ArtifactRef] = None
    stderr_ref: Optional[ArtifactRef] = None
    stdout_complete: bool = True
    stderr_complete: bool = True
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    tests_collected: Optional[int] = None
    tests_executed: Optional[int] = None
    tests_passed: Optional[int] = None
    tests_failed: Optional[int] = None
    tests_skipped: Optional[int] = None
    expected: str = ""
    observed: str = ""
    control_id: str = ""
    control_expected: str = ""
    control_observed: str = ""
    control_passed: Optional[bool] = None
    write_set_reconciliation: Tuple[str, ...] = ()
    tool_versions: Tuple[Tuple[str, str], ...] = ()
    proof_class: str = INDEPENDENCE_HARNESS_HIDDEN
    requirement_ids: Tuple[str, ...] = ()
    blocked_reason: str = ""
    failure_fingerprint: str = ""
    claimed_preexisting: bool = False
    baseline_receipt_hash: str = ""
    baseline_source_digest: str = ""
    baseline_failure_fingerprint: str = ""
    schema_version: int = VERIFICATION_RECEIPT_SCHEMA_VERSION
    receipt_hash: str = field(default="")

    def __post_init__(self) -> None:
        for name in ("receipt_id", "run_id", "packet_id", "execution_package_hash",
                     "verifier_id", "normalized_command", "source_snapshot_digest"):
            if not str(getattr(self, name) or "").strip():
                raise ReceiptError(
                    f"verification receipt {name} must be non-empty")
        if not isinstance(self.attempt, int) or self.attempt < 1:
            raise ReceiptError("verification attempt must be an integer >= 1")
        if self.proof_class not in KNOWN_INDEPENDENCE:
            raise ReceiptError(
                f"unknown proof_class {self.proof_class!r}; "
                f"known: {sorted(KNOWN_INDEPENDENCE)}")
        if not isinstance(self.exit_code, int):
            raise ReceiptError("exit_code must be an int")
        # Contradiction gates: states a receipt must not be able to represent.
        if self.exit_code == 0 and (self.tests_failed or 0) > 0:
            raise ReceiptError(
                "exit_code 0 cannot accompany failing tests: "
                f"tests_failed={self.tests_failed}")
        if self.claimed_preexisting and not self.failure_fingerprint:
            raise ReceiptError(
                "a pre-existing claim requires a failure_fingerprint to match "
                "against the baseline")
        for name in ("tests_collected", "tests_executed", "tests_passed",
                     "tests_failed", "tests_skipped"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, int) or value < 0):
                raise ReceiptError(f"{name} must be None or a non-negative int")

    # -------------------------------------------------------------- verdict ---
    @property
    def capture_complete(self) -> bool:
        """Both streams fully captured. A truncated capture proves neither way."""
        return bool(self.stdout_complete and self.stderr_complete)

    @property
    def outcome(self) -> str:
        """PASS / FAIL / BLOCKED / INCONCLUSIVE — DERIVED, never asserted.

        Order matters: BLOCKED first (nothing was judged), then a nonzero exit
        code (a failure is a failure whatever the prose says), then incomplete
        capture (which cannot support a PASS any more than it can support an
        absence claim), and only then PASS.
        """
        if self.blocked_reason.strip():
            return CLAIM_BLOCKED
        if self.exit_code != 0:
            return CLAIM_FAIL
        if not self.capture_complete:
            return CLAIM_INCONCLUSIVE
        return CLAIM_PASS

    @property
    def passes(self) -> bool:
        return self.outcome == CLAIM_PASS

    @property
    def baseline_classification(self) -> str:
        """SAME / CHANGED / NEW / UNPROVEN — from fingerprints, never prose."""
        return classify_against_baseline(self)

    def claim_for(self, requirement_id: str) -> RequirementClaim:
        return RequirementClaim(
            requirement_id=requirement_id,
            receipt_id=self.receipt_hash or self.receipt_id,
            outcome=self.outcome, proof_class=self.proof_class,
            detail=(self.blocked_reason or self.observed)[:160])

    def claims(self) -> Tuple[RequirementClaim, ...]:
        """One closure claim per requirement this receipt says it addresses."""
        return tuple(self.claim_for(req) for req in self.requirement_ids)

    def verifier_identity_matches(self, verifier_id: str,
                                  verifier_digest: str) -> bool:
        """Both the verifier's NAME and its content identity must agree."""
        return (self.verifier_id == verifier_id
                and bool(verifier_digest)
                and self.verifier_digest == verifier_digest)

    def core(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "packet_id": self.packet_id,
            "attempt": self.attempt,
            "execution_package_hash": self.execution_package_hash,
            "verifier_id": self.verifier_id,
            "verifier_digest": self.verifier_digest,
            "verifier_paths": list(self.verifier_paths),
            "source_snapshot_digest": self.source_snapshot_digest,
            "worktree": self.worktree,
            "host": self.host,
            "container": self.container,
            "normalized_command": self.normalized_command,
            "exit_code": self.exit_code,
            "timeout_s": self.timeout_s,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "elapsed_s": self.elapsed_s,
            "stdout_ref": self.stdout_ref.to_dict() if self.stdout_ref else None,
            "stderr_ref": self.stderr_ref.to_dict() if self.stderr_ref else None,
            "stdout_complete": self.stdout_complete,
            "stderr_complete": self.stderr_complete,
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "tests_collected": self.tests_collected,
            "tests_executed": self.tests_executed,
            "tests_passed": self.tests_passed,
            "tests_failed": self.tests_failed,
            "tests_skipped": self.tests_skipped,
            "expected": self.expected,
            "observed": self.observed,
            "control_id": self.control_id,
            "control_expected": self.control_expected,
            "control_observed": self.control_observed,
            "control_passed": self.control_passed,
            "write_set_reconciliation": list(self.write_set_reconciliation),
            "tool_versions": [list(t) for t in self.tool_versions],
            "proof_class": self.proof_class,
            "requirement_ids": list(self.requirement_ids),
            "blocked_reason": self.blocked_reason,
            "failure_fingerprint": self.failure_fingerprint,
            "claimed_preexisting": self.claimed_preexisting,
            "baseline_receipt_hash": self.baseline_receipt_hash,
            "baseline_source_digest": self.baseline_source_digest,
            "baseline_failure_fingerprint": self.baseline_failure_fingerprint,
            "outcome": self.outcome,
        }

    def to_dict(self) -> dict:
        payload = self.core()
        payload["receipt_hash"] = self.receipt_hash
        return payload


def make_verification_receipt(**kwargs: Any) -> VerificationReceipt:
    """Build a verification receipt, normalizing its sequence fields."""
    known = {f.name for f in fields(VerificationReceipt)}
    unknown = set(kwargs) - known
    if unknown:
        raise ReceiptError(
            f"unknown verification receipt field(s): {sorted(unknown)}")
    payload = dict(kwargs)
    payload.pop("receipt_hash", None)
    for name in ("verifier_paths", "write_set_reconciliation", "tool_versions",
                 "requirement_ids"):
        if name in payload and payload[name] is not None:
            payload[name] = tuple(payload[name])
    return _seal_record(VerificationReceipt, payload, "receipt_hash",
                        exclude=("outcome",))


def verification_receipt_hash_is_valid(payload: Mapping[str, Any]) -> bool:
    """True when a serialized verification receipt's hash matches its fields.

    ``outcome`` is NOT part of the hash: it is derived from ``exit_code`` and
    capture completeness, so including it would let two representations of the
    same fact disagree. It is recomputed on read instead — which is exactly why
    an edited ``outcome`` is detectable rather than authoritative.
    """
    return _check_hash(payload, "receipt_hash", exclude=("outcome",))


def recompute_outcome(receipt: Mapping[str, Any]) -> str:
    """Derive PASS / FAIL / BLOCKED / INCONCLUSIVE from the raw fields."""
    if str(receipt.get("blocked_reason") or "").strip():
        return CLAIM_BLOCKED
    if int(receipt.get("exit_code") or 0) != 0:
        return CLAIM_FAIL
    if not (receipt.get("stdout_complete", True)
            and receipt.get("stderr_complete", True)):
        return CLAIM_INCONCLUSIVE
    return CLAIM_PASS


def classify_against_baseline(receipt: VerificationReceipt) -> str:
    """SAME / CHANGED / NEW / UNPROVEN from fingerprints, never from prose.

    ``UNPROVEN`` is the state a bare claim lands in. PS-638 requires an
    exact-base receipt to support "this failure was pre-existing"; a receipt that
    says so without naming a baseline hash, a baseline source and a baseline
    fingerprint has not supported anything, and must not read as NEW (which
    would silently drop the claim) or as SAME (which would accept it).
    """
    if not receipt.claimed_preexisting:
        return BASELINE_NEW
    if not (receipt.baseline_receipt_hash.strip()
            and receipt.baseline_source_digest.strip()
            and receipt.baseline_failure_fingerprint.strip()):
        return BASELINE_UNPROVEN
    if receipt.baseline_failure_fingerprint.strip() == \
            receipt.failure_fingerprint.strip() and receipt.failure_fingerprint:
        return BASELINE_SAME
    return BASELINE_CHANGED




