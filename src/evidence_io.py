"""Content-addressed evidence retention and regenerated human projections.

No lifecycle authority or scheduler lives here. The bundle pins the canonical
package and the URI-to-blob projection; blobs are never rewritten or collected.
Consumers can relocate a bundle without changing any sealed receipt hash.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from src.attempt_receipt import recompute_outcome

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
    blob_dir = root / "blobs"
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
        content = (path.parent / "blobs" / digest).read_bytes()
        if hashlib.sha256(content).hexdigest() != digest or len(content) != ref["size"]:
            raise ValueError("retained artifact hash/size mismatch")
        artifacts[uri] = content
    return bundle["evidence_package"], artifacts


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
