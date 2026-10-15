"""Content-addressed evidence retention and regenerated human projections.

No lifecycle authority or scheduler lives here. The bundle pins the canonical
package and the URI-to-blob projection; blobs are never rewritten or collected.
Consumers can relocate a bundle without changing any sealed receipt hash.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from src.attempt_receipt import recompute_outcome
from src.constants import EVIDENCE_BLOB_DIRNAME

from src.evidence_package import (
    _iter_artifact_refs, default_artifact_loader, evidence_package_hash_is_valid,
    find_secret_shaped, validate_evidence_package,
)


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode()


def _write_once(path, data):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or path.read_bytes() != data:
            raise ValueError("immutable evidence path already contains different bytes")
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def retain_evidence_bundle(directory, payload, *, artifacts=None):
    """Retain a package and every reachable artifact, including failed retries.

    Refuse missing, corrupt or secret-bearing bytes before publishing a bundle.
    Failed packages remain retainable for investigations. No evidence is promoted
    to VERIFIED by this storage operation.
    """
    if not evidence_package_hash_is_valid(payload) or find_secret_shaped(payload):
        raise ValueError("invalid or secret-bearing evidence package")
    blobs, manifest = {}, {}
    for subject, ref in _iter_artifact_refs(payload):
        uri = ref["storage_uri"]
        data = (artifacts or {}).get(uri)
        if data is None:
            data = default_artifact_loader(ref)
        if data is None or hashlib.sha256(data).hexdigest() != ref["sha256"] or len(data) != ref["size"]:
            raise ValueError("artifact missing or corrupt: " + subject)
        if find_secret_shaped(data.decode("utf-8", "replace")):
            raise ValueError("secret-bearing artifact: " + subject)
        blobs[ref["sha256"]] = data
        entry = manifest.setdefault(uri, {"sha256": ref["sha256"], "size": len(data), "consumers": []})
        if entry["sha256"] != ref["sha256"]:
            raise ValueError("artifact URI refers to different immutable content")
        entry["consumers"].append(subject)
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    blob_dir = root / EVIDENCE_BLOB_DIRNAME
    blob_dir.mkdir(exist_ok=True, mode=0o700)
    for digest, data in blobs.items():
        _write_once(blob_dir / digest, data)
    bundle = {"schema_version": 1, "evidence_package": payload, "artifacts": manifest}
    data = _encoded(bundle)
    path = root / (hashlib.sha256(data).hexdigest() + ".json")
    _write_once(path, data)
    return path


def read_evidence_bundle(path):
    path = Path(path)
    data = path.read_bytes()
    if path.name != hashlib.sha256(data).hexdigest() + ".json":
        raise ValueError("bundle hash mismatch")
    bundle = json.loads(data)
    if bundle.get("schema_version") != 1:
        raise ValueError("unsupported evidence bundle schema")
    artifacts = {}
    for uri, ref in bundle["artifacts"].items():
        digest = ref["sha256"]
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid content-addressed blob identity")
        content = (path.parent / EVIDENCE_BLOB_DIRNAME / digest).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest or len(content) != ref["size"]:
            raise ValueError("retained artifact hash/size mismatch")
        artifacts[uri] = content
    return bundle["evidence_package"], artifacts


class EvidenceBundleReadError(ValueError):
    """A retained bundle could not be read; messages exclude paths and contents."""


class EvidenceBundleNotFoundError(EvidenceBundleReadError):
    """The exact bundle or one of its retained blobs is absent."""


class EvidenceBundleLimitError(EvidenceBundleReadError):
    """A caller-selected byte, object-count or elapsed-time ceiling was reached."""


def _retained_hash(value):
    return (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


def _retained_limit(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= sys.maxsize:
        raise EvidenceBundleReadError("limits must be explicit positive bounded integers")
    return value


def _retained_deadline(deadline):
    if time.monotonic() >= deadline:
        raise EvidenceBundleLimitError("retained read elapsed-time ceiling reached")


@contextmanager
def _retained_directory(path, deadline, *, parent_fd=None):
    _retained_deadline(deadline)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                     dir_fd=parent_fd)
    except (OSError, TypeError, ValueError):
        raise EvidenceBundleReadError("cannot open retained directory without following links") from None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise EvidenceBundleReadError("retained directory must be owned and private mode 0700")
        _retained_deadline(deadline)
        yield fd
    finally:
        os.close(fd)


def _retained_metadata(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _retained_bytes(directory_fd, name, max_bytes, deadline, *, expected_size=None):
    _retained_deadline(deadline)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=directory_fd)
    except FileNotFoundError:
        raise EvidenceBundleNotFoundError("exact retained object is absent") from None
    except OSError:
        raise EvidenceBundleReadError("cannot open exact retained object") from None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600):
            raise EvidenceBundleReadError("retained object must be an owned single-link mode 0600 file")
        if expected_size is not None and before.st_size != expected_size:
            raise EvidenceBundleReadError("retained artifact size does not match manifest")
        if before.st_size < 0 or before.st_size > max_bytes:
            raise EvidenceBundleLimitError("retained object exceeds caller byte ceiling")
        chunks, total = [], 0
        while True:
            _retained_deadline(deadline)
            block = os.read(fd, min(64 * 1024, max_bytes - total + 1))
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                if expected_size is not None:
                    raise EvidenceBundleReadError("retained artifact grew beyond manifest size")
                raise EvidenceBundleLimitError("retained object exceeds caller byte ceiling")
            chunks.append(block)
        after = os.fstat(fd)
        if _retained_metadata(before) != _retained_metadata(after) or total != after.st_size:
            raise EvidenceBundleReadError("retained object changed during read")
        _retained_deadline(deadline)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _retained_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceBundleReadError("retained JSON contains duplicate keys")
        result[key] = value
    return result


def _retained_constant(_):
    raise EvidenceBundleReadError("retained JSON contains a non-finite number")


def _retained_manifest(bundle, max_blob_bytes, max_total_blob_bytes, max_artifacts, deadline):
    if (not isinstance(bundle, dict)
            or set(bundle) != {"schema_version", "evidence_package", "artifacts"}
            or type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1):
        raise EvidenceBundleReadError("unsupported retained bundle shape or schema")
    payload, manifest = bundle["evidence_package"], bundle["artifacts"]
    if not isinstance(payload, dict) or not isinstance(manifest, dict):
        raise EvidenceBundleReadError("retained package and manifest must be objects")
    if len(manifest) > max_artifacts:
        raise EvidenceBundleLimitError("retained manifest exceeds caller object ceiling")
    if not evidence_package_hash_is_valid(payload) or find_secret_shaped(payload):
        raise EvidenceBundleReadError("retained evidence package seal is invalid")
    expected = {}
    for label, ref in _iter_artifact_refs(payload):
        _retained_deadline(deadline)
        uri, digest, size = ref.get("storage_uri"), ref.get("sha256"), ref.get("size")
        if (not isinstance(uri, str) or not _retained_hash(digest)
                or type(size) is not int or size < 0):
            raise EvidenceBundleReadError("invalid retained artifact reference")
        entry = expected.setdefault(uri, {"sha256": digest, "size": size, "consumers": []})
        if entry["sha256"] != digest or entry["size"] != size:
            raise EvidenceBundleReadError("conflicting retained artifact references")
        entry["consumers"].append(label)
    if expected != manifest:
        raise EvidenceBundleReadError("retained manifest does not match package references")
    sizes = {}
    for entry in manifest.values():
        if (not isinstance(entry, dict) or set(entry) != {"sha256", "size", "consumers"}
                or type(entry["size"]) is not int):
            raise EvidenceBundleReadError("invalid retained manifest entry")
        digest, size = entry["sha256"], entry["size"]
        if size > max_blob_bytes:
            raise EvidenceBundleLimitError("retained blob exceeds caller byte ceiling")
        if sizes.setdefault(digest, size) != size:
            raise EvidenceBundleReadError("one retained digest declares conflicting sizes")
    if sum(sizes.values()) > max_total_blob_bytes:
        raise EvidenceBundleLimitError("retained blobs exceed caller total byte ceiling")
    _retained_deadline(deadline)
    return sizes


def read_retained_evidence_bundle(directory, bundle_hash, *, max_bundle_bytes,
                                  max_blob_bytes, max_total_blob_bytes, max_artifacts,
                                  max_read_seconds):
    """Read one exact retained bundle with explicit resource ceilings.

    The authorized backend supplies an existing trusted root and a lowercase
    bundle-file SHA-256 (not an EvidencePackage or outcome hash). Directories
    must be owned mode 0700; objects must be owned, single-link mode 0600 files.
    Opens are descriptor-anchored and reject final-component symlinks; ancestors
    of the trusted root remain the caller's responsibility. No discovery, URI
    loader, repair or filesystem write occurs.

    max_artifacts counts URI manifest entries. Per-blob and total blob bytes
    count distinct digests, which are read once even when several URIs alias
    them. Ceilings are checked before blob opens. The elapsed deadline is
    cooperative: checks refuse an expired result but cannot preempt blocked OS
    calls or JSON/hash computation. Hard cancellation needs caller isolation.

    This checks storage seals and manifest consistency, not authorization,
    canonical receipt joins or lifecycle acceptance. Failed/unverified retained
    packages remain readable; consumers must apply their canonical validators.
    """
    max_bundle_bytes = _retained_limit(max_bundle_bytes)
    max_blob_bytes = _retained_limit(max_blob_bytes)
    max_total_blob_bytes = _retained_limit(max_total_blob_bytes)
    max_artifacts = _retained_limit(max_artifacts)
    if (isinstance(max_read_seconds, bool) or not isinstance(max_read_seconds, (int, float))
            or not 0 < max_read_seconds <= sys.maxsize or not math.isfinite(max_read_seconds)):
        raise EvidenceBundleReadError("elapsed-time ceiling must be explicit positive finite seconds")
    if not _retained_hash(bundle_hash):
        raise EvidenceBundleReadError("exact lowercase bundle-file SHA-256 is required")
    if (not all(getattr(os, flag, 0) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK", "O_CLOEXEC"))
            or os.open not in os.supports_dir_fd):
        raise EvidenceBundleReadError("platform lacks anchored no-follow read operations")
    deadline = time.monotonic() + max_read_seconds
    try:
        with _retained_directory(directory, deadline) as root_fd:
            raw = _retained_bytes(root_fd, bundle_hash + ".json", max_bundle_bytes, deadline)
            if hashlib.sha256(raw).hexdigest() != bundle_hash:
                raise EvidenceBundleReadError("retained bundle hash mismatch")
            bundle = json.loads(raw.decode("utf-8"), object_pairs_hook=_retained_pairs,
                                parse_constant=_retained_constant)
            if _encoded(bundle) != raw:
                raise EvidenceBundleReadError("retained bundle JSON is not canonical")
            sizes = _retained_manifest(bundle, max_blob_bytes, max_total_blob_bytes,
                                       max_artifacts, deadline)
            blobs = {}
            if sizes:
                with _retained_directory(EVIDENCE_BLOB_DIRNAME, deadline, parent_fd=root_fd) as blobs_fd:
                    for digest, size in sizes.items():
                        content = _retained_bytes(blobs_fd, digest, size, deadline, expected_size=size)
                        if len(content) != size or hashlib.sha256(content).hexdigest() != digest:
                            raise EvidenceBundleReadError("retained artifact hash or size mismatch")
                        blobs[digest] = content
            artifacts = {uri: blobs[ref["sha256"]] for uri, ref in bundle["artifacts"].items()}
        _retained_deadline(deadline)
        return bundle["evidence_package"], artifacts
    except EvidenceBundleReadError:
        raise
    except OSError:
        raise EvidenceBundleReadError("retained filesystem operation failed") from None
    except (TypeError, ValueError, KeyError, AttributeError, OverflowError, RecursionError):
        raise EvidenceBundleReadError("retained bundle is malformed") from None


def project_evidence_summary(payload, *, artifacts=None, **observations):
    """Generate Markdown exclusively from canonical receipts and current validation."""
    validation = validate_evidence_package(payload, artifact_extensions=artifacts, **observations)
    lines = [f"Evidence: {payload['evidence_package_id']}",
             f"Hash: {payload['evidence_package_hash']}", f"State: {validation.state}",
             "Semantic acceptance: separate reviewer decision required", "", "Attempts:"]
    for attempt in payload["attempt_receipts"]:
        verdicts = [recompute_outcome(r) for r in payload['verification_receipts']
                    if r['attempt'] == attempt['attempt']]
        lines.append(f"- {attempt['attempt']}: {attempt['receipt_hash']} / {','.join(verdicts) or 'UNVERIFIED'}")
    lines.extend(["", "Requirements:"])
    lines.extend(f"- {state.requirement_id}: {state.state}" for state in validation.requirement_states)
    if validation.issues:
        lines.extend(["", "Validation reasons:"])
        lines.extend(f"- {issue.code}: {issue.subject}" for issue in validation.issues)
    result = "\n".join(lines) + "\n"
    if find_secret_shaped(result):
        raise ValueError("secret-shaped data in generated projection")
    return result
