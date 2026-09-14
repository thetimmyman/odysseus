"""src/evidence_package.py — sealed EvidencePackage + fail-closed validator (PS-638).

The last stage of the chain: a package that binds intent, dispatch provenance,
attempts, deterministic verifications and requirement closure into one
content-addressed envelope, plus a validator that decides VERIFIED or not.

Design rules this module enforces, each of which exists because the alternative
was measured somewhere in this project:

* **Fail closed, and say WHY.** The validator returns named reason codes, never a
  bare ``False``. A rejection a human cannot act on is a rejection that gets
  weakened the next time it fires.
* **Recompute, do not read.** ``outcome``, requirement states and hashes are all
  recomputed from the raw fields. Anything recorded is compared against the
  recomputation, so a package whose summary disagrees with its receipts is
  rejected rather than believed.
* **No requirement, no VERIFIED.** Mandatory requirements that are UNRESOLVED,
  FAILED, or BLOCKED-when-BLOCKED-is-not-allowed stop the package. A package
  cannot become VERIFIED while mandatory proof is missing.
* **Trends are evidence.** Attempt numbers must be contiguous from 1 and every
  receipt hash must verify, so an earlier red cannot be dropped to make a final
  green look cleaner.
* **Provenance is compared.** A PASS carried by worker-authored proof cannot
  close a requirement that demands independence, even when the receipt is
  genuine.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Tuple

from src.attempt_receipt import (
    BASELINE_CHANGED,
    BASELINE_SAME,
    BASELINE_UNPROVEN,
)
from src.evidence_contract import (
    CLAIM_BLOCKED,
    CLAIM_FAIL,
    CLAIM_INCONCLUSIVE,
    CLAIM_PASS,
    INDEPENDENCE_WORKER_AUTHORED,
    STATE_BLOCKED,
    STATE_FAILED,
    STATE_UNRESOLVED,
    EvidenceRequirement,
    RequirementClaim,
    RequirementState,
    close_requirements,
)
from src.source_snapshot import snapshot_digest_is_valid

EVIDENCE_PACKAGE_SCHEMA_VERSION = 1

# ------------------------------------------------------------------ reasons ---
# Structural integrity
EVIDENCE_PACKAGE_HASH_MISMATCH = "evidence_package_hash_mismatch"
EXECUTION_PACKAGE_HASH_MISMATCH = "execution_package_hash_mismatch"
RECEIPT_HASH_MISMATCH = "receipt_hash_mismatch"
SOURCE_SNAPSHOT_DIGEST_INVALID = "source_snapshot_digest_invalid"
SOURCE_IDENTITY_MISSING_OR_AMBIGUOUS = "source_identity_missing_or_ambiguous"
ARTIFACT_HASH_MISMATCH = "artifact_hash_mismatch"
ARTIFACT_UNAVAILABLE = "artifact_unavailable"

# Packet / contract
WRITABLE_PACKAGE_MISSING_INTERFACE = "writable_package_missing_interface"
INTERFACE_CHANGED_AFTER_SEALING = "interface_changed_after_sealing"
CONTEXT_PROJECTION_MISSING_INTERFACE = "context_projection_missing_interface"
RENDERED_CONTEXT_MISSING = "rendered_context_missing"

# Verification quality
VERIFIER_IDENTITY_MISMATCH = "verifier_identity_mismatch"
CLAIMED_PASS_WITH_NONZERO_VERIFIER = "claimed_pass_with_nonzero_verifier"
MISSING_REQUIRED_NEGATIVE_CONTROL = "missing_required_negative_control"
WRITE_OUTSIDE_AUTHORIZED_SCOPE = "write_outside_authorized_scope"
DISPATCH_TARGET_MISMATCH = "dispatch_target_mismatch"
SOURCE_CHANGED_AFTER_VERIFICATION = "source_changed_after_verification"
RETRY_HISTORY_OMITTED = "retry_history_omitted"
PREEXISTING_FAILURE_WITHOUT_BASELINE = "preexisting_failure_without_baseline"
WORKER_AUTHORED_EVIDENCE_NOT_INDEPENDENT = "worker_authored_evidence_not_independent"
SECRET_SHAPED_FIXTURE_LEAK = "secret_shaped_fixture_leak"
NO_ATTEMPTS = "no_attempts"
REQUIREMENT_STATE_TAMPERED = "requirement_state_tampered"
MALFORMED_RECORD = "malformed_record"

# Closure
REQUIREMENT_UNRESOLVED = "requirement_unresolved"
REQUIREMENT_FAILED = "requirement_failed"
REQUIREMENT_BLOCKED_NOT_ALLOWED = "requirement_blocked_not_allowed"

KNOWN_REASONS = frozenset({
    EVIDENCE_PACKAGE_HASH_MISMATCH, EXECUTION_PACKAGE_HASH_MISMATCH,
    RECEIPT_HASH_MISMATCH, SOURCE_SNAPSHOT_DIGEST_INVALID,
    SOURCE_IDENTITY_MISSING_OR_AMBIGUOUS, ARTIFACT_HASH_MISMATCH,
    ARTIFACT_UNAVAILABLE, WRITABLE_PACKAGE_MISSING_INTERFACE,
    INTERFACE_CHANGED_AFTER_SEALING, CONTEXT_PROJECTION_MISSING_INTERFACE,
    RENDERED_CONTEXT_MISSING, VERIFIER_IDENTITY_MISMATCH,
    CLAIMED_PASS_WITH_NONZERO_VERIFIER, MISSING_REQUIRED_NEGATIVE_CONTROL,
    WRITE_OUTSIDE_AUTHORIZED_SCOPE, DISPATCH_TARGET_MISMATCH,
    SOURCE_CHANGED_AFTER_VERIFICATION, RETRY_HISTORY_OMITTED,
    PREEXISTING_FAILURE_WITHOUT_BASELINE,
    WORKER_AUTHORED_EVIDENCE_NOT_INDEPENDENT, SECRET_SHAPED_FIXTURE_LEAK,
    NO_ATTEMPTS, REQUIREMENT_STATE_TAMPERED, REQUIREMENT_UNRESOLVED,
    REQUIREMENT_FAILED, REQUIREMENT_BLOCKED_NOT_ALLOWED, MALFORMED_RECORD,
})

#: Secret-shaped strings. Deliberately conservative: these are shapes that
#: should never appear in a packet, projection or package payload at all, so a
#: hit is a defect in how the fixture was authored, not a tuning problem.
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token)\s*[:=]\s*"
               r"['\"]?[A-Za-z0-9/+_-]{12,}"),
    re.compile(r"(?i)\bauthorization\s*:\s*bearer\s+\S{12,}"),
)


class EvidencePackageError(ValueError):
    """Raised when an evidence package cannot be sealed or is unreadable."""


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def find_secret_shaped(payload: object, *, prefix: str = "") -> Tuple[str, ...]:
    """Paths whose value looks like a credential. Walks dicts, lists and strings.

    Returning PATHS rather than matches matters: the report must not repeat the
    secret it is complaining about into a log or a Jira comment.
    """
    hits = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            hits.extend(find_secret_shaped(value, prefix=f"{prefix}/{key}"))
    elif isinstance(payload, (list, tuple)):
        for index, value in enumerate(payload):
            hits.extend(find_secret_shaped(value, prefix=f"{prefix}[{index}]"))
    elif isinstance(payload, str):
        for pattern in _SECRET_PATTERNS:
            if pattern.search(payload):
                hits.append(prefix or "<value>")
                break
    return tuple(hits)


def default_artifact_loader(ref: Mapping[str, Any]) -> Optional[bytes]:
    """Read an artifact's bytes from its ``storage_uri``, or None if unreadable."""
    uri = str((ref or {}).get("storage_uri") or "")
    if not uri or not uri.startswith("file://"):
        return None
    path = uri[len("file://"):]
    try:
        with open(path, "rb") as handle:
            return handle.read()
    except OSError:
        return None


@dataclass(frozen=True)
class ValidationIssue:
    """One named reason a package is not VERIFIED."""

    code: str
    detail: str = ""
    subject: str = ""

    def __post_init__(self) -> None:
        if self.code not in KNOWN_REASONS:
            raise EvidencePackageError(
                f"unknown validation reason code {self.code!r}; "
                f"known: {sorted(KNOWN_REASONS)}")

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail, "subject": self.subject}

    def __str__(self) -> str:
        where = f" [{self.subject}]" if self.subject else ""
        return f"{self.code}{where}: {self.detail}" if self.detail else \
            f"{self.code}{where}"


@dataclass(frozen=True)
class ValidationResult:
    """VERIFIED or not, with every reason that contributed."""

    ok: bool
    issues: Tuple[ValidationIssue, ...] = ()
    requirement_states: Tuple[RequirementState, ...] = ()

    @property
    def codes(self) -> Tuple[str, ...]:
        return tuple(issue.code for issue in self.issues)

    def has(self, code: str) -> bool:
        """True when a specific named reason was raised."""
        return code in self.codes

    def explain(self) -> str:
        if self.ok:
            return "VERIFIED"
        return "REJECTED: " + "; ".join(str(i) for i in self.issues)

    def to_dict(self) -> dict:
        return {"ok": self.ok, "issues": [i.to_dict() for i in self.issues],
                "requirement_states": [s.to_dict() for s in self.requirement_states]}


# ------------------------------------------------------------ the package ---
@dataclass(frozen=True)
class EvidencePackage:
    """A sealed, content-addressed envelope.

    Sub-records are held in their SERIALIZED form, not as live objects. That is
    deliberate: the package is what gets written down and validated later, and
    validating a re-serialized object graph would let a field that fails to
    survive serialization pass in memory and fail on disk.
    """

    evidence_package_id: str
    execution_package: Mapping[str, Any]
    dispatch_receipts: Tuple[Mapping[str, Any], ...] = ()
    attempt_receipts: Tuple[Mapping[str, Any], ...] = ()
    verification_receipts: Tuple[Mapping[str, Any], ...] = ()
    requirement_states: Tuple[Mapping[str, Any], ...] = ()
    waivers: Mapping[str, str] = field(default_factory=dict)
    seals: Tuple[Mapping[str, Any], ...] = ()
    sealed_at: str = ""
    schema_version: int = EVIDENCE_PACKAGE_SCHEMA_VERSION
    evidence_package_hash: str = field(default="")

    def __post_init__(self) -> None:
        if not str(self.evidence_package_id or "").strip():
            raise EvidencePackageError("evidence_package_id must be non-empty")
        if not isinstance(self.execution_package, Mapping) or \
                not self.execution_package:
            raise EvidencePackageError(
                "an evidence package must embed its ExecutionPackage")

    @property
    def run_id(self) -> str:
        return str(self.execution_package.get("run_id", ""))

    @property
    def package_id(self) -> str:
        return str(self.execution_package.get("package_id", ""))

    @property
    def execution_package_hash(self) -> str:
        return str(self.execution_package.get("package_hash", ""))

    def core(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "evidence_package_id": self.evidence_package_id,
            "sealed_at": self.sealed_at,
            "execution_package": dict(self.execution_package),
            "dispatch_receipts": [dict(r) for r in self.dispatch_receipts],
            "attempt_receipts": [dict(r) for r in self.attempt_receipts],
            "verification_receipts": [dict(r) for r in self.verification_receipts],
            "requirement_states": [dict(s) for s in self.requirement_states],
            "waivers": dict(self.waivers),
            "seals": [dict(s) for s in self.seals],
        }

    def to_dict(self) -> dict:
        payload = self.core()
        payload["evidence_package_hash"] = self.evidence_package_hash
        return payload


def compute_evidence_package_hash(core: Mapping[str, Any]) -> str:
    return _sha256_hex(_canonical(dict(core)))


def evidence_package_hash_is_valid(payload: Mapping[str, Any]) -> bool:
    """True when a serialized evidence package's hash matches its own fields."""
    if not isinstance(payload, Mapping) or not payload.get("evidence_package_hash"):
        return False
    core = {k: v for k, v in payload.items() if k != "evidence_package_hash"}
    return compute_evidence_package_hash(core) == payload["evidence_package_hash"]


def requirement_from_dict(payload: Mapping[str, Any]) -> EvidenceRequirement:
    """Rebuild an EvidenceRequirement from its serialized form (strictly)."""
    if not isinstance(payload, Mapping):
        raise EvidencePackageError(
            "evidence requirement must be a mapping, got "
            f"{type(payload).__name__}")
    known = set(EvidenceRequirement.__dataclass_fields__)
    unknown = set(payload) - known
    if unknown:
        raise EvidencePackageError(
            f"unknown evidence requirement field(s): {sorted(unknown)}")
    return EvidenceRequirement(**dict(payload))


def seal_evidence_package(
    *,
    evidence_package_id: str,
    execution_package: Any,
    verification_receipts: Sequence[Any] = (),
    dispatch_receipts: Sequence[Any] = (),
    attempt_receipts: Sequence[Any] = (),
    waivers: Optional[Mapping[str, str]] = None,
    seals: Sequence[Mapping[str, Any]] = (),
    sealed_at: str = "",
) -> EvidencePackage:
    """Seal a package, COMPUTING requirement closure from the receipts.

    Closure is computed here rather than accepted as an argument, so a caller
    cannot hand in a favourable set of states. ``waivers`` is the only way to
    reach NOT_APPLICABLE, and it requires a reason per requirement.
    """
    if not hasattr(execution_package, "to_dict"):
        raise EvidencePackageError(
            "execution_package must be an ExecutionPackage (or expose to_dict())")
    package_payload = execution_package.to_dict()

    requirements = tuple(
        requirement_from_dict(r)
        for r in (package_payload.get("evidence_requirements") or ()))
    claims = []
    for receipt in verification_receipts:
        claims.extend(receipt.claims())
    states = close_requirements(requirements, claims, waivers=waivers or {})

    return _seal_evidence(
        evidence_package_id=evidence_package_id,
        execution_package=package_payload,
        dispatch_receipts=tuple(r.to_dict() for r in dispatch_receipts),
        attempt_receipts=tuple(r.to_dict() for r in attempt_receipts),
        verification_receipts=tuple(r.to_dict() for r in verification_receipts),
        requirement_states=tuple(s.to_dict() for s in states),
        waivers=dict(waivers or {}),
        seals=tuple(dict(s) for s in seals),
        sealed_at=sealed_at or utc_now(),
    )


def _seal_evidence(**core) -> EvidencePackage:
    core = {k: v for k, v in core.items() if k != "evidence_package_hash"}
    core["schema_version"] = EVIDENCE_PACKAGE_SCHEMA_VERSION
    provisional = EvidencePackage(**core)
    digest = compute_evidence_package_hash(provisional.core())
    return EvidencePackage(**{**core, "evidence_package_hash": digest})


def reseal_evidence_payload(payload: Mapping[str, Any]) -> dict:
    """Recompute the package hash after an INTENTIONAL, recorded edit.

    For migrations and for fixtures that deliberately model a mis-authored
    package: it makes a payload internally consistent so a rejection names the
    semantic rule instead of the hash. It confers no legitimacy on the edit — a
    package resealed here is still invalid for whatever reason the edit created.

    Note the deliberate consequence: receipts already inside the payload keep
    citing the OLD package hash, so a resealed package also reports them as
    detached. Nothing here silently re-points them, because a receipt that
    followed its package around a change would not be evidence of anything. A
    real migration re-emits the receipts.
    """
    from src.execution_package import compute_package_hash

    result = dict(payload)
    package = dict(result.get("execution_package") or {})
    if package:
        package.pop("package_hash", None)
        package["package_hash"] = compute_package_hash(package)
        result["execution_package"] = package
    result.pop("evidence_package_hash", None)
    result["evidence_package_hash"] = compute_evidence_package_hash(result)
    return result


# ---------------------------------------------------------------- validator ---
def _load(loader, ref: Mapping[str, Any]) -> Optional[bytes]:
    try:
        return loader(ref)
    except Exception:
        return None


def _iter_artifact_refs(payload: Mapping[str, Any]
                        ) -> Iterable[Tuple[str, Mapping[str, Any]]]:
    """Every content-addressed reference in a package, with a locator label."""
    for index, receipt in enumerate(payload.get("attempt_receipts") or ()):
        for key in ("rendered_context_ref", "output_ref"):
            ref = receipt.get(key)
            if isinstance(ref, Mapping):
                yield f"attempt[{index}].{key}", ref
        for sub, ref in enumerate(receipt.get("artifact_refs") or ()):
            if isinstance(ref, Mapping):
                yield f"attempt[{index}].artifact_refs[{sub}]", ref
    for index, receipt in enumerate(payload.get("verification_receipts") or ()):
        for key in ("stdout_ref", "stderr_ref"):
            ref = receipt.get(key)
            if isinstance(ref, Mapping):
                yield f"verification[{index}].{key}", ref


def _check_artifacts(payload: Mapping[str, Any], extensions: Mapping[str, bytes],
                     issues: list) -> None:
    """Every declared artifact must be loadable and hash-correct.

    Extensions lets a caller supply bytes the package references but that live
    only in memory during the run (a ledger that has been rotated, say). Sealed
    evidence that cannot produce its own bytes is not evidence, so an
    unloadable reference is a rejection, not a warning.
    """
    for label, ref in _iter_artifact_refs(payload):
        digest = str(ref.get("sha256") or "")
        uri = str(ref.get("storage_uri") or "")
        data = extensions.get(uri) if uri and uri in extensions else None
        if data is None:
            data = _load(default_artifact_loader, ref)
        if data is None:
            issues.append(ValidationIssue(
                ARTIFACT_UNAVAILABLE,
                "artifact bytes are not retrievable; a declared artifact that "
                "cannot be produced proves nothing",
                subject=label))
            continue
        if _sha256_hex(data) != digest:
            issues.append(ValidationIssue(
                ARTIFACT_HASH_MISMATCH,
                "loaded bytes do not match the declared sha256",
                subject=label))
            continue
        # A projection is part of the package's evidence even though its bytes
        # live in a file, so it is scanned here rather than only in the payload.
        text = data.decode("utf-8", "replace")
        leaked = find_secret_shaped(text, prefix=label)
        if leaked:
            issues.append(ValidationIssue(
                SECRET_SHAPED_FIXTURE_LEAK,
                "secret-shaped content appears in a sealed artifact; paths only, "
                "values deliberately not repeated",
                subject=label))


def _check_receipt_hashes(payload: Mapping[str, Any], issues: list) -> None:
    from src.attempt_receipt import (
        attempt_receipt_hash_is_valid,
        verification_receipt_hash_is_valid,
    )

    for index, receipt in enumerate(payload.get("attempt_receipts") or ()):
        if not attempt_receipt_hash_is_valid(receipt):
            issues.append(ValidationIssue(
                RECEIPT_HASH_MISMATCH, "attempt receipt hash does not match its "
                "own fields", subject=f"attempt_receipts[{index}]"))
    for index, receipt in enumerate(payload.get("verification_receipts") or ()):
        if not verification_receipt_hash_is_valid(receipt):
            issues.append(ValidationIssue(
                RECEIPT_HASH_MISMATCH, "verification receipt hash does not match "
                "its own fields", subject=f"verification_receipts[{index}]"))
    package = payload.get("execution_package") or {}
    from src.execution_package import package_hash_is_valid

    if not package_hash_is_valid(package):
        issues.append(ValidationIssue(
            EXECUTION_PACKAGE_HASH_MISMATCH,
            "the execution package hash does not match its own fields",
            subject="execution_package"))


def _check_source(package: Mapping[str, Any], issues: list) -> str:
    """Source identity must be present, self-consistent and COMPLETE."""
    from src.source_snapshot import snapshot_digest_is_valid

    source = package.get("source")
    if not isinstance(source, Mapping):
        issues.append(ValidationIssue(
            SOURCE_IDENTITY_MISSING_OR_AMBIGUOUS,
            "execution package carries no source snapshot at all"))
        return ""
    missing = [k for k in ("repo_root", "head_sha", "base_sha", "snapshot_digest")
               if not str(source.get(k) or "").strip()]
    if missing:
        issues.append(ValidationIssue(
            SOURCE_IDENTITY_MISSING_OR_AMBIGUOUS,
            f"source identity is incomplete: missing {sorted(missing)}"))
    if source.get("truncated_paths") or source.get("diff_truncated"):
        issues.append(ValidationIssue(
            SOURCE_IDENTITY_MISSING_OR_AMBIGUOUS,
            "source snapshot was truncated by a byte cap, so it identifies only "
            "part of the tree it claims to identify"))
    if not snapshot_digest_is_valid(dict(source)):
        issues.append(ValidationIssue(
            SOURCE_SNAPSHOT_DIGEST_INVALID,
            "source snapshot digest does not match its own fields"))
        return ""
    return str(source.get("snapshot_digest", ""))


def _check_packet_contract(package: Mapping[str, Any], issues: list) -> None:
    """A writable package must declare an interface, and keep declaring it."""
    from src.work_packet import interface_digest_from_normalized

    write_scope = package.get("write_scope") or []
    interface = package.get("interface") or []
    if write_scope and not interface:
        issues.append(ValidationIssue(
            WRITABLE_PACKAGE_MISSING_INTERFACE,
            "a package with a write scope declares no interface; a worker cannot "
            "read keys that were never named"))
    if interface:
        recomputed = interface_digest_from_normalized(interface)
        recorded = str(package.get("interface_digest") or "")
        if not recorded or recorded != recomputed:
            issues.append(ValidationIssue(
                INTERFACE_CHANGED_AFTER_SEALING,
                f"recorded interface_digest {recorded!r} does not match the "
                f"interface in the package ({recomputed})"))


def _check_attempts(payload: Mapping[str, Any], package: Mapping[str, Any],
                    issues: list) -> None:
    """Attempts must be contiguous, in scope, and on the authorized target."""
    attempts = list(payload.get("attempt_receipts") or ())
    dispatches = list(payload.get("dispatch_receipts") or ())
    package_hash = str(package.get("package_hash") or "")
    authorized_scope = [str(p) for p in (package.get("write_scope") or ())]
    granted: list = []
    for dispatch in dispatches:
        granted.extend(str(p) for p in (dispatch.get("granted_write_scope") or ()))
    effective_scope = [p for p in granted if p] or authorized_scope

    if not attempts and payload.get("verification_receipts"):
        issues.append(ValidationIssue(
            NO_ATTEMPTS, "verification receipts exist but no attempt was recorded"))

    if attempts:
        numbers = sorted(int(a.get("attempt") or 0) for a in attempts)
        if numbers != list(range(1, len(numbers) + 1)):
            issues.append(ValidationIssue(
                RETRY_HISTORY_OMITTED,
                f"attempt numbers {numbers} are not contiguous from 1; an earlier "
                "attempt was dropped or renumbered"))

    authorized_targets = {
        (str(d.get("selected_target_id", "")), str(d.get("selected_host", "")),
         str(d.get("selected_model", ""))) for d in dispatches}
    dispatch_hashes = {str(d.get("receipt_hash", "")) for d in dispatches}

    for index, dispatch in enumerate(dispatches):
        if str(dispatch.get("execution_package_hash") or "") != package_hash:
            issues.append(ValidationIssue(
                DISPATCH_TARGET_MISMATCH,
                "dispatch receipt authorizes a different execution package",
                subject=f"dispatch_receipts[{index}]"))
        if str(dispatch.get("run_id") or "") not in ("", str(package.get("run_id"))):
            issues.append(ValidationIssue(
                DISPATCH_TARGET_MISMATCH,
                "dispatch receipt belongs to a different run",
                subject=f"dispatch_receipts[{index}]"))

    for index, attempt in enumerate(attempts):
        label = f"attempt_receipts[{index}]"
        if str(attempt.get("execution_package_hash") or "") != package_hash:
            issues.append(ValidationIssue(
                DISPATCH_TARGET_MISMATCH,
                "attempt receipt was made against a different execution package",
                subject=label))
        if dispatch_hashes and str(attempt.get("dispatch_receipt_hash") or "") \
                not in dispatch_hashes:
            issues.append(ValidationIssue(
                DISPATCH_TARGET_MISMATCH,
                "attempt receipt cites a dispatch receipt that is not in this "
                "package", subject=label))
        actual_target = (str(attempt.get("target_id", "")),
                         str(attempt.get("host", "")),
                         str(attempt.get("model", "")))
        if authorized_targets and actual_target not in authorized_targets:
            issues.append(ValidationIssue(
                DISPATCH_TARGET_MISMATCH,
                f"attempt ran on {actual_target} which no dispatch receipt "
                f"authorized (authorized: {sorted(authorized_targets)})",
                subject=label))
        actual_writes = [str(p) for p in (attempt.get("actual_write_set") or ())]
        outside = sorted(p for p in actual_writes if p not in effective_scope)
        if outside:
            issues.append(ValidationIssue(
                WRITE_OUTSIDE_AUTHORIZED_SCOPE,
                f"wrote outside the authorized scope: {outside} "
                f"(authorized: {effective_scope})", subject=label))


def _recompute_closure(payload: Mapping[str, Any], package: Mapping[str, Any]):
    """Recompute requirement closure from the raw receipts. Never trusted."""
    requirements = tuple(
        requirement_from_dict(r)
        for r in (package.get("evidence_requirements") or ()))
    claims = []
    for receipt in payload.get("verification_receipts") or ():
        for requirement_id in receipt.get("requirement_ids") or ():
            claims.append(RequirementClaim(
                requirement_id=str(requirement_id),
                receipt_id=str(receipt.get("receipt_hash")
                               or receipt.get("receipt_id") or ""),
                outcome=_recomputed_outcome(receipt),
                proof_class=str(receipt.get("proof_class")
                                or INDEPENDENCE_WORKER_AUTHORED),
                detail=str(receipt.get("blocked_reason")
                           or receipt.get("observed") or "")[:160]))
    states = close_requirements(requirements, claims,
                                waivers=payload.get("waivers") or {})
    return requirements, claims, states


def _recomputed_outcome(receipt: Mapping[str, Any]) -> str:
    """The verdict DERIVED from exit code and capture completeness.

    Delegates to ``src.attempt_receipt.recompute_outcome`` so there is ONE
    definition. A validator with its own copy could drift from the receipt's own
    verdict, and the drift would be invisible in exactly the case that matters —
    a receipt whose recorded outcome disagrees with its exit code.
    """
    from src.attempt_receipt import recompute_outcome

    return recompute_outcome(receipt)


def _check_verifications(payload: Mapping[str, Any], package: Mapping[str, Any],
                         source_digest: str, issues: list,
                         current_source: Optional[Mapping[str, Any]] = None) -> None:
    """Verify identity, verdict honesty, and WHICH TREE each receipt ran against.

    Source rule, and the reason it is not "receipt must equal the package":

    a writable run changes the tree BY DESIGN. The package's ``source`` is the
    INPUT identity sealed before dispatch; verification necessarily happens after
    the worker wrote, so the two legitimately differ. Requiring equality would
    either be unsatisfiable on every real run or push authors into recording the
    input digest on a receipt that measured something else — a lie that would
    then look like evidence.

    So the rule is: the receipts must agree with EACH OTHER, the caller's
    ``current_source`` must agree with them, and a verified tree that differs
    from the sealed input must be EXPLAINED by a recorded write. An unexplained
    difference is rejected.
    """
    verification = package.get("verification") or {}
    planned_id = str(verification.get("verifier_id") or "")
    planned_digests = {str(d) for _path, d in
                       (verification.get("verifier_digests") or ())}
    receipts = list(payload.get("verification_receipts") or ())
    negative_declared = bool(str(package.get("negative_control") or "").strip())
    control_seen = False

    for index, receipt in enumerate(receipts):
        label = f"verification_receipts[{index}]"
        if planned_id and str(receipt.get("verifier_id") or "") != planned_id:
            issues.append(ValidationIssue(
                VERIFIER_IDENTITY_MISMATCH,
                f"verifier {receipt.get('verifier_id')!r} is not the planned "
                f"verifier {planned_id!r}", subject=label))
        if planned_digests:
            digest = str(receipt.get("verifier_digest") or "")
            if digest not in planned_digests:
                issues.append(ValidationIssue(
                    VERIFIER_IDENTITY_MISMATCH,
                    "the verifier artifact that ran does not match the digest "
                    "sealed in the plan before execution", subject=label))
        derived = _recomputed_outcome(receipt)
        recorded = str(receipt.get("outcome") or "")
        if recorded and recorded != derived:
            issues.append(ValidationIssue(
                CLAIMED_PASS_WITH_NONZERO_VERIFIER,
                f"recorded outcome {recorded!r} disagrees with the derived "
                f"outcome {derived!r} from exit_code "
                f"{receipt.get('exit_code')!r} and capture completeness",
                subject=label))
        if receipt.get("claimed_preexisting"):
            baseline = _baseline_classification(receipt)
            if baseline != BASELINE_SAME:
                issues.append(ValidationIssue(
                    PREEXISTING_FAILURE_WITHOUT_BASELINE,
                    "a pre-existing failure was claimed but the baseline is "
                    f"{baseline}: an exact-base receipt with the same normalized "
                    "failure fingerprint is required", subject=label))
        if str(receipt.get("control_id") or "").strip():
            control_seen = True
            if receipt.get("control_passed") is False:
                issues.append(ValidationIssue(
                    MISSING_REQUIRED_NEGATIVE_CONTROL,
                    "the declared negative control did not behave as required",
                    subject=label))

    if negative_declared and not control_seen:
        issues.append(ValidationIssue(
            MISSING_REQUIRED_NEGATIVE_CONTROL,
            "the package declares a negative control but no verification receipt "
            "records one running"))

    _check_verified_source(payload, source_digest, receipts, current_source, issues)


def _check_verified_source(payload: Mapping[str, Any], source_digest: str,
                           receipts: Sequence[Mapping[str, Any]],
                           current_source: Optional[Mapping[str, Any]],
                           issues: list) -> None:
    """Which tree each verification ran against, and whether it still applies.

    Two rules — and NOT a third one, which was WITHDRAWN on live evidence:

    * a verified tree that differs from the sealed input must be EXPLAINED by a
      recorded write;
    * a caller-supplied ``current_source`` must be one of the trees that were
      actually verified.

    The withdrawn rule was "all verification receipts must agree with each other".
    The first real two-attempt run showed why that is wrong: a repair run
    verifies a DIFFERENT tree after each attempt by design — that is what a
    repair IS. Keeping the rule would have rejected correct evidence, and worse,
    it would have pushed authors to record one shared digest for several
    different trees, which is precisely the falsehood the check exists to catch.
    """
    verified = {str(r.get("source_snapshot_digest") or "") for r in receipts}
    verified.discard("")
    if not verified:
        return

    if source_digest and source_digest not in verified:
        wrote = any((attempt.get("actual_write_set") or ())
                    for attempt in payload.get("attempt_receipts") or ())
        if not wrote:
            issues.append(ValidationIssue(
                SOURCE_CHANGED_AFTER_VERIFICATION,
                "verification ran against a different tree than the sealed input, "
                "and no attempt recorded a write that could explain the difference",
                subject="verification_receipts"))

    if current_source is not None:
        current = (current_source.get("snapshot_digest", "")
                   if isinstance(current_source, Mapping) else "")
        if not current or current not in verified:
            issues.append(ValidationIssue(
                SOURCE_CHANGED_AFTER_VERIFICATION,
                "the source measured now is not any tree this evidence verified; "
                "the evidence no longer applies", subject="current_source"))



def _baseline_classification(receipt: Mapping[str, Any]) -> str:
    if not (str(receipt.get("baseline_receipt_hash") or "").strip()
            and str(receipt.get("baseline_source_digest") or "").strip()
            and str(receipt.get("baseline_failure_fingerprint") or "").strip()):
        return BASELINE_UNPROVEN
    if str(receipt.get("baseline_failure_fingerprint") or "").strip() == \
            str(receipt.get("failure_fingerprint") or "").strip() and \
            str(receipt.get("failure_fingerprint") or "").strip():
        return BASELINE_SAME
    return BASELINE_CHANGED


def _check_context_projection(payload: Mapping[str, Any], package: Mapping[str, Any],
                              extensions: Mapping[str, bytes], issues: list) -> None:
    """The sealed interface must be VISIBLE in every worker context.

    This is the check that makes a declared interface non-decorative. It runs
    against every attempt, not just the first, because PS-638 requires a repair
    attempt to carry the same immutable interface — a repair context that drops
    or paraphrases it would reintroduce the exact failure this contract exists to
    prevent.
    """
    interface = [str(line) for line in (package.get("interface") or ())]
    for index, attempt in enumerate(payload.get("attempt_receipts") or ()):
        label = f"attempt_receipts[{index}]"
        ref = attempt.get("rendered_context_ref")
        if not isinstance(ref, Mapping):
            issues.append(ValidationIssue(
                RENDERED_CONTEXT_MISSING,
                "the attempt records no rendered-context artifact, so what the "
                "worker was shown cannot be checked", subject=label))
            continue
        uri = str(ref.get("storage_uri") or "")
        data = extensions.get(uri) if uri and uri in extensions else None
        if data is None:
            data = _load(default_artifact_loader, ref)
        if data is None:
            continue  # already reported as ARTIFACT_UNAVAILABLE
        text = data.decode("utf-8", "replace")
        derived = _sha256_hex(data)
        if derived != str(ref.get("sha256") or ""):
            issues.append(ValidationIssue(
                ARTIFACT_HASH_MISMATCH,
                "the rendered context on disk is not the one that was sealed",
                subject=f"{label}.rendered_context_ref"))
        if derived != str(attempt.get("context_projection_hash") or ""):
            issues.append(ValidationIssue(
                ARTIFACT_HASH_MISMATCH,
                "context_projection_hash does not match the rendered context",
                subject=f"{label}.context_projection_hash"))
        missing = [line for line in interface if line not in text]
        if missing:
            issues.append(ValidationIssue(
                CONTEXT_PROJECTION_MISSING_INTERFACE,
                f"the sealed interface is not present in the worker context: "
                f"{missing}", subject=label))


def _check_independence(requirements: Sequence[EvidenceRequirement],
                        claims: Sequence[RequirementClaim], issues: list) -> None:
    """A PASS carried only by worker-authored proof cannot close an independent req."""
    by_requirement: dict = {}
    for claim in claims:
        by_requirement.setdefault(claim.requirement_id, []).append(claim)
    for req in requirements:
        if not req.requires_independence:
            continue
        passes = [c for c in by_requirement.get(req.requirement_id, ())
                  if c.outcome == CLAIM_PASS]
        if passes and not any(req.accepts(c.proof_class) for c in passes):
            issues.append(ValidationIssue(
                WORKER_AUTHORED_EVIDENCE_NOT_INDEPENDENT,
                f"requirement {req.requirement_id!r} demands "
                f"{req.independence} but every PASS came from "
                f"{sorted({c.proof_class for c in passes})}",
                subject=req.requirement_id))


def _check_closure(requirements: Sequence[EvidenceRequirement],
                   states: Sequence[RequirementState],
                   recorded_states: Sequence[Mapping[str, Any]],
                   issues: list) -> None:
    """Recorded requirement states must EQUAL what the receipts imply."""
    derived = {state.requirement_id: state.state for state in states}
    recorded = {str(s.get("requirement_id", "")): str(s.get("state", ""))
                for s in (recorded_states or ())}
    for requirement_id, state in sorted(derived.items()):
        if requirement_id not in recorded:
            issues.append(ValidationIssue(
                REQUIREMENT_STATE_TAMPERED,
                "the sealed package omits this requirement's state, so its "
                "closure cannot be compared", subject=requirement_id))
        elif recorded[requirement_id] != state:
            issues.append(ValidationIssue(
                REQUIREMENT_STATE_TAMPERED,
                f"sealed state {recorded[requirement_id]!r} disagrees with the "
                f"state the receipts imply ({state!r})", subject=requirement_id))
    for requirement_id in sorted(set(recorded) - set(derived)):
        issues.append(ValidationIssue(
            REQUIREMENT_STATE_TAMPERED,
            "the package records a state for a requirement it does not declare",
            subject=requirement_id))

    for req in requirements:
        if not req.mandatory:
            continue
        state = derived.get(req.requirement_id, STATE_UNRESOLVED)
        if state == STATE_UNRESOLVED:
            issues.append(ValidationIssue(
                REQUIREMENT_UNRESOLVED,
                "a mandatory requirement was never addressed by any receipt",
                subject=req.requirement_id))
        elif state == STATE_FAILED:
            issues.append(ValidationIssue(
                REQUIREMENT_FAILED, "a mandatory requirement is FAILED",
                subject=req.requirement_id))
        elif state == STATE_BLOCKED:
            issues.append(ValidationIssue(
                REQUIREMENT_BLOCKED_NOT_ALLOWED,
                "a mandatory requirement is BLOCKED; a mandatory requirement must "
                "be satisfied, not excused", subject=req.requirement_id))


def validate_evidence_package(
    payload: Mapping[str, Any],
    *,
    current_source: Optional[Mapping[str, Any]] = None,
    artifact_extensions: Optional[Mapping[str, bytes]] = None,
) -> ValidationResult:
    """Decide VERIFIED or not, with a named reason for every rejection.

    ``current_source`` is how "the source changed after verification" becomes
    checkable: pass a fresh snapshot measured NOW, and evidence sealed against a
    different tree is invalidated rather than silently reused. ``artifact_
    extensions`` supplies bytes for artifacts referenced by a non-file URI.
    """
    issues: list = []
    extensions = dict(artifact_extensions or {})
    if not isinstance(payload, Mapping):
        return ValidationResult(False, (ValidationIssue(
            MALFORMED_RECORD,
            f"evidence package must be a mapping, got {type(payload).__name__}"),))

    if not evidence_package_hash_is_valid(payload):
        issues.append(ValidationIssue(
            EVIDENCE_PACKAGE_HASH_MISMATCH,
            "the sealed package hash does not match its own fields"))

    hits = find_secret_shaped(payload)
    if hits:
        issues.append(ValidationIssue(
            SECRET_SHAPED_FIXTURE_LEAK,
            "secret-shaped content appears in the package at "
            f"{list(hits[:5])}{' (+more)' if len(hits) > 5 else ''}; paths only, "
            "values deliberately not repeated"))

    package = payload.get("execution_package") or {}
    if not isinstance(package, Mapping):
        issues.append(ValidationIssue(
            MALFORMED_RECORD, "execution_package must be a mapping"))
        return ValidationResult(False, tuple(issues))

    _check_receipt_hashes(payload, issues)
    source_digest = _check_source(package, issues)

    _check_packet_contract(package, issues)
    _check_attempts(payload, package, issues)
    _check_verifications(payload, package, source_digest, issues, current_source)
    _check_artifacts(payload, extensions, issues)
    _check_context_projection(payload, package, extensions, issues)

    try:
        requirements, claims, states = _recompute_closure(payload, package)
    except Exception as exc:
        issues.append(ValidationIssue(
            MALFORMED_RECORD, f"requirement closure could not be computed: {exc}"))
        return ValidationResult(False, tuple(issues))

    _check_independence(requirements, claims, issues)
    _check_closure(requirements, states, payload.get("requirement_states") or (),
                   issues)

    return ValidationResult(ok=not issues, issues=tuple(issues),
                            requirement_states=states)








