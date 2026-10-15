"""Finite source-only orchestration, owner checks and minimized inspection.

There is no default live backend. Internal injection supports isolated source
tests only and does not qualify a production engine, filesystem, transport,
process boundary, capability observation, provider or restored audit state.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import time
import uuid

from fastapi import HTTPException

from src.auth_helpers import require_admin_cookie
from src.canonical_inspector_admission import (
    InspectorUnavailable, Principal, Publication, UnknownInspection,
)
from src.constants import (
    CANONICAL_INSPECTOR_ARTIFACTS, CANONICAL_INSPECTOR_BLOB_BYTES,
    CANONICAL_INSPECTOR_BUNDLE_BYTES, CANONICAL_INSPECTOR_READ_SECONDS,
    CANONICAL_INSPECTOR_RESPONSE_BYTES, CANONICAL_INSPECTOR_TOTAL_BLOB_BYTES,
    CANONICAL_PILOT_DISCLOSURE_GENERATION, CANONICAL_PILOT_ID,
    EVIDENCE_BLOB_DIRNAME,
)
from src.evidence_io import read_retained_evidence_bundle
from src.dispatch_routing import KNOWN_REFUSALS


READ_LIMITS = dict(max_bundle_bytes=CANONICAL_INSPECTOR_BUNDLE_BYTES,
                   max_blob_bytes=CANONICAL_INSPECTOR_BLOB_BYTES,
                   max_total_blob_bytes=CANONICAL_INSPECTOR_TOTAL_BLOB_BYTES,
                   max_artifacts=CANONICAL_INSPECTOR_ARTIFACTS,
                   max_read_seconds=CANONICAL_INSPECTOR_READ_SECONDS)


def request_principal(request):
    """Use the existing real cookie gate, then a locked fresh exact snapshot."""
    key = require_admin_cookie(request)
    manager = getattr(request.app.state, "auth_manager", None)
    snapshotter = getattr(manager, "snapshot_admin_account_epoch", None)
    snapshot = snapshotter(key) if snapshotter else None
    if snapshot is None:
        raise HTTPException(403, "Admin cookie session required")
    try:
        return Principal(*snapshot)
    except InspectorUnavailable:
        raise HTTPException(403, "Admin cookie session required") from None


def _reauthorize(owner, callback):
    try:
        if callback() != owner:
            raise UnknownInspection("inspection unavailable")
    except (HTTPException, InspectorUnavailable):
        raise UnknownInspection("inspection unavailable") from None


def _deadline(until):
    if time.monotonic() >= until:
        raise InspectorUnavailable("inspector unavailable")


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@contextmanager
def _directory(path, *, parent=None):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=parent)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise InspectorUnavailable("inspector unavailable")
        yield fd
    finally:
        os.close(fd)


def _sync_file(parent, name, digest, limit, until, *, expected=None):
    _deadline(until)
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1
                or before.st_size > limit or before.st_size < 0
                or (expected is not None and before.st_size != len(expected))):
            raise InspectorUnavailable("inspector unavailable")
        hashed, total = hashlib.sha256(), 0
        while True:
            _deadline(until)
            block = os.read(fd, min(65536, limit - total + 1))
            if not block:
                break
            if len(block) > limit - total:
                raise InspectorUnavailable("inspector unavailable")
            if expected is not None and block != expected[total:total + len(block)]:
                raise InspectorUnavailable("inspector unavailable")
            total += len(block)
            hashed.update(block)
        if (hashed.hexdigest() != digest or total != before.st_size
                or _identity(before) != _identity(os.fstat(fd))):
            raise InspectorUnavailable("inspector unavailable")
        # Existing equal-content files receive a FRESH acknowledgement too.
        os.fsync(fd)
        if _identity(before) != _identity(os.fstat(fd)):
            raise InspectorUnavailable("inspector unavailable")
        _deadline(until)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class RetentionAcknowledgement:
    bundle_hash: str
    evidence_hash: str
    payload: dict
    artifacts: dict


def acknowledge_retention(directory, bundle_hash, *, parent_directories):
    """Revalidate and sync ALL exact objects and a finite trusted parent chain.

    This creates nothing and never walks ancestors. The owner supplies an
    existing private root and the explicit adjacent private parent entries
    whose durability must be acknowledged. Actual filesystem crash guarantees
    remain unqualified. The deadline is cooperative, as for the retained reader.
    """
    until = time.monotonic() + CANONICAL_INSPECTOR_READ_SECONDS
    try:
        root = Path(directory)
        parents = tuple(Path(parent) for parent in parent_directories)
        if not 0 < len(parents) <= 4:
            raise InspectorUnavailable("inspector unavailable")
        current = root
        for parent in parents:
            if parent != current.parent or parent == parent.parent:
                raise InspectorUnavailable("inspector unavailable")
            current = parent
        payload, artifacts = read_retained_evidence_bundle(root, bundle_hash, **READ_LIMITS)
        unique = {hashlib.sha256(content).hexdigest(): content for content in artifacts.values()}
        with _directory(root) as root_fd:
            with _directory(EVIDENCE_BLOB_DIRNAME, parent=root_fd) as blobs_fd:
                for digest, content in unique.items():
                    _sync_file(blobs_fd, digest, digest, CANONICAL_INSPECTOR_BLOB_BYTES,
                               until, expected=content)
                os.fsync(blobs_fd)
            _sync_file(root_fd, bundle_hash + ".json", bundle_hash,
                       CANONICAL_INSPECTOR_BUNDLE_BYTES, until)
            os.fsync(root_fd)
        for parent in parents:
            with _directory(parent) as parent_fd:
                _deadline(until)
                os.fsync(parent_fd)
        _deadline(until)
        return RetentionAcknowledgement(bundle_hash, payload["evidence_package_hash"], payload, artifacts)
    except (OSError, ValueError, TypeError, KeyError):
        raise InspectorUnavailable("inspector unavailable") from None


def _minimized_projection(projection, publication):
    """Explicit nested allowlist; no raw projector/context/validation passthrough."""
    if (type(projection) is not dict
            or projection.get("schema") != "routing-inspection-projection-v1"
            or projection.get("authoritative") is not False):
        raise UnknownInspection("inspection unavailable")
    retained = projection.get("retained_evidence", {})
    if (retained.get("evidence_package_hash") != publication.evidence_hash
            or retained.get("attempt_receipt_hash") != publication.attempt_hash
            or projection.get("dispatch", {}).get("receipt_hash") != publication.dispatch_hash
            or projection.get("execution_package", {}).get("package_hash") != publication.package_hash):
        raise UnknownInspection("inspection unavailable")
    state = retained.get("validation", {}).get("state", "UNKNOWN")
    if state not in {"UNKNOWN", "UNVERIFIED", "VERIFIED", "FAILED"}:
        state = "UNKNOWN"
    candidates = projection.get("dispatch", {}).get("candidates_considered", "UNKNOWN")
    safe_candidates = []
    if candidates != "UNKNOWN":
        if type(candidates) is not list or len(candidates) > CANONICAL_INSPECTOR_ARTIFACTS:
            raise UnknownInspection("inspection unavailable")
        for row in candidates:
            if (type(row) is not dict or type(row.get("target_id")) is not str
                    or type(row.get("profile_id")) is not str
                    or len(row["target_id"].encode()) > 1024 or len(row["profile_id"].encode()) > 1024):
                raise UnknownInspection("inspection unavailable")
            safe_candidates.append({
                "target_id": row["target_id"],
                "profile_id": row["profile_id"],
                "eligible": row.get("eligible") if type(row.get("eligible")) is bool else None,
                "selected": row.get("selected") if type(row.get("selected")) is bool else None,
                "rule": row.get("rule") if row.get("rule") in KNOWN_REFUSALS | {"eligible"} else "UNKNOWN",
            })
    def observation_status(key):
        value = projection.get(key, {}).get("status")
        return value if value in {"OBSERVED", "UNKNOWN"} else "UNKNOWN"

    result = {"authoritative": False,
              "attempt_receipt_hash": publication.attempt_hash,
              "dispatch_receipt_hash": publication.dispatch_hash,
              "execution_package_hash": publication.package_hash,
              "evidence_package_hash": publication.evidence_hash,
              "verification_state": state,
              "routing": {"capability_state": observation_status("capabilities"),
                          "capacity_state": observation_status("capacity"),
                          "candidates": safe_candidates if candidates != "UNKNOWN" else "UNKNOWN"},
              "semantic_acceptance": "UNKNOWN"}
    raw = json.dumps(result, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > CANONICAL_INSPECTOR_RESPONSE_BYTES:
        raise UnknownInspection("inspection unavailable")
    return result


class InspectorService:
    """Trusted internal DI; route setup supplies no service in production."""

    def __init__(self, store, *, retained_root, projector, pilot_adapter=None,
                 retention_parents=(), restored_disclosure_generation=CANONICAL_PILOT_DISCLOSURE_GENERATION):
        self._store = store
        self._root = retained_root
        self._project = projector
        self._adapter = pilot_adapter
        self._parents = retention_parents
        self._generation = restored_disclosure_generation
        self._read = read_retained_evidence_bundle

    def inspect(self, owner, handle, reauthorize):
        try:
            _reauthorize(owner, reauthorize)
            if self._generation != CANONICAL_PILOT_DISCLOSURE_GENERATION:
                raise UnknownInspection("inspection unavailable")
            publication = self._store.resolve(owner, handle, self._generation)
            payload, artifacts = self._read(self._root, publication.bundle_hash, **READ_LIMITS)
            package = payload.get("execution_package", {})
            attempts = [item for item in payload.get("attempt_receipts", ())
                        if item.get("receipt_hash") == publication.attempt_hash]
            if (payload.get("evidence_package_hash") != publication.evidence_hash
                    or package.get("package_hash") != publication.package_hash
                    or package.get("run_id") != publication.run_id
                    or package.get("packet_id") != publication.packet_id
                    or len(attempts) != 1
                    or attempts[0].get("dispatch_receipt_hash") != publication.dispatch_hash):
                raise UnknownInspection("inspection unavailable")
            projected = self._project(payload, artifacts=artifacts,
                                      attempt_receipt_hash=publication.attempt_hash,
                                      now=datetime.now(timezone.utc))
            result = _minimized_projection(projected, publication)
            # New session/transaction inside resolve: never a stale ORM map or
            # the original long-lived SQLite snapshot after a slow file read.
            if self._store.resolve(owner, handle, self._generation) != publication:
                raise UnknownInspection("inspection unavailable")
            _reauthorize(owner, reauthorize)
            return result
        except (ValueError, OSError, TypeError, KeyError):
            raise UnknownInspection("inspection unavailable") from None

    async def admit(self, owner, pilot_id, key, reauthorize):
        from src.canonical_pilot_adapter import FixedSyntheticPilot
        if (pilot_id != CANONICAL_PILOT_ID or type(self._adapter) is not FixedSyntheticPilot
                or self._generation != CANONICAL_PILOT_DISCLOSURE_GENERATION):
            raise InspectorUnavailable("inspector unavailable")
        _reauthorize(owner, reauthorize)
        # The trusted injected source adapter must use the fixed pilot and
        # bounded collectors. No caller supplies a command/path/profile/URL.
        run_id = str(uuid.uuid4())
        prepared = await self._adapter.prepare(run_id)
        try:
            if (prepared.run_id != run_id or prepared.packet_id != CANONICAL_PILOT_ID
                    or prepared.config_digest != self._adapter.config_digest):
                raise InspectorUnavailable("inspector unavailable")
            _reauthorize(owner, reauthorize)
            claim = self._store.admit(owner, key, prepared)
            self._store.consume(claim, owner)
            _reauthorize(owner, reauthorize)
            result = await self._adapter.execute(claim)
            if result.cleanup_confirmed is not True:
                raise InspectorUnavailable("inspector unavailable")
            ack = acknowledge_retention(self._root, result.bundle_hash, parent_directories=self._parents)
            package = ack.payload.get("execution_package", {})
            if (package.get("package_hash") != prepared.package_hash
                    or package.get("run_id") != prepared.run_id
                    or package.get("packet_id") != prepared.packet_id):
                raise InspectorUnavailable("inspector unavailable")
            # Read-only canonical projector validates selected inner receipt and
            # dispatch joins; it grants no ownership or lifecycle authority.
            self._project(ack.payload, artifacts=ack.artifacts, attempt_receipt_hash=result.attempt_hash,
                          now=datetime.now(timezone.utc))
            attempts = [item for item in ack.payload.get("attempt_receipts", ())
                        if item.get("receipt_hash") == result.attempt_hash]
            if len(attempts) != 1:
                raise InspectorUnavailable("inspector unavailable")
            publication = Publication(ack.bundle_hash, ack.evidence_hash, result.attempt_hash,
                                      attempts[0]["dispatch_receipt_hash"], self._generation)
            _reauthorize(owner, reauthorize)
            handle = self._store.publish(claim, owner, publication)
            self._store.release_terminal(claim, cleanup_confirmed=True)
            _reauthorize(owner, reauthorize)
            return {"handle": handle, "status": "retained"}
        finally:
            # Replayed/failed preparation cannot accumulate process-local state.
            # A committed uncertain claim remains held in its native store.
            self._adapter.discard(prepared)

    def revoke(self, owner, handle, reauthorize):
        _reauthorize(owner, reauthorize)
        self._store.revoke(owner, handle)
        _reauthorize(owner, reauthorize)
        return {"status": "revoked"}
