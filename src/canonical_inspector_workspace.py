"""Existing finite workspace operations without application database dependencies.

The app owns authorization, admission, routing and leases. This worker reads
only its fixed private manifest and preserves the sealed workspace checks.
Construction/import performs no workspace, schema or provider operation.
"""
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import time

from src.inspector_errors import InspectorUnavailable
from src.constants import (CANONICAL_INSPECTOR_WORK_ROOT, CANONICAL_TMOS_VERIFICATION_DIRNAME,
                           CANONICAL_TMOS_MANIFEST_FILE)
from src.execution_package import package_hash_is_valid


def manifest_digest(manifest):
    return hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _private_bytes(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > limit):
            raise InspectorUnavailable("inspector unavailable")
        raw = stream.read(limit + 1)
        fields = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_uid,
                                value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if fields(info) != fields(os.fstat(stream.fileno())) or fields(info) != fields(Path(path).lstat()):
            raise InspectorUnavailable("inspector unavailable")
    if len(raw) > limit or len(raw) != info.st_size:
        raise InspectorUnavailable("inspector unavailable")
    return raw


def _manifest_file(path):
    from src.local_measurement_process import decode_json
    return decode_json(_private_bytes(path, 131072))


def _container_ended(result):
    """Consume the existing sandbox's exact-CID native cleanup observation."""
    return (result.get('cleanup_confirmed') is True and result.get('cleanup_state') == 'OBSERVED_REMOVED'
            and type(result.get('container_id')) is str and re.fullmatch(r'[0-9a-f]{64}', result['container_id']) is not None
            and type(result.get('image_id')) is str and re.fullmatch(r'sha256:[0-9a-f]{64}', result['image_id']) is not None)


def workspace_operation(operation, *, manifest_path=CANONICAL_TMOS_MANIFEST_FILE):
    """One admitted workspace operation, called only inside the owning capture.

    Repository and verifier paths come from the private admitted manifest.
    The input cannot select a repository, shell or arbitrary command.
    """
    from src.local_measurement_process import checked_workspace_operation
    from src.routing_workdir import create_worktree, worktrees_root, apply_patch
    from src.routing_patch import validate_patch_shape
    from src.source_snapshot import take_source_snapshot, source_snapshot_from_dict

    try:
        value = checked_workspace_operation(operation)
        manifest = _manifest_file(manifest_path)
        if (manifest.get("schema") != "canonical-tmos-owner-v1"
                or manifest_digest(manifest) != value["manifest_digest"]):
            raise InspectorUnavailable("inspector unavailable")
        template = manifest["packages"][value["packet_id"]]
        if (not package_hash_is_valid(template) or template["packet_id"] != value["packet_id"]
                or re.fullmatch(r"TMOS-[1-9][0-9]*", template["ticket_key"]) is None):
            raise InspectorUnavailable("inspector unavailable")
        original = source_snapshot_from_dict(template["source"])
        relevant = tuple(dict.fromkeys((*template["write_scope"], *template["read_scope"],
                                        *template["verification"]["verifier_paths"])))
        if value["operation"] == "prepare":
            before = take_source_snapshot(original.repo_root, base_sha=original.base_sha,
                                          relevant_paths=relevant)
            if (not before.is_clean or not before.is_complete or before.to_dict() != original.to_dict()):
                raise InspectorUnavailable("inspector unavailable")
            jail = Path(worktrees_root())
            if not jail.exists():
                jail.mkdir(parents=True, mode=0o700)
            info = jail.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise InspectorUnavailable("inspector unavailable")
            worktree = create_worktree(original.repo_root, value["run_id"], base_ref=original.head_sha)
            Path(worktree).chmod(0o700)
            source = take_source_snapshot(worktree, base_sha=original.head_sha, relevant_paths=relevant)
            if (not source.is_clean or not source.is_complete or not source.detached
                    or source.head_sha != original.head_sha or source.repo_identity != original.repo_identity
                    or source.relevant_digests != original.relevant_digests):
                raise InspectorUnavailable("inspector unavailable")
            return {"source": source.to_dict(), "cleanup_confirmed": True}
        package = value["package"]
        mutable = {"source", "run_id", "package_id", "package_hash"}
        target = Path(worktrees_root()) / value["run_id"]
        if (type(package) is not dict or set(package) != set(template)
                or not package_hash_is_valid(package) or package["run_id"] != value["run_id"]
                or any(package[key] != template[key] for key in set(template) - mutable)):
            raise InspectorUnavailable("inspector unavailable")
        initial = source_snapshot_from_dict(package["source"])
        if (Path(initial.worktree) != target or Path(initial.repo_root) != target
                or not initial.is_clean or not initial.is_complete or not initial.detached
                or initial.head_sha != original.head_sha or initial.base_sha != original.head_sha
                or initial.repo_identity != original.repo_identity or initial.relevant_digests != original.relevant_digests):
            raise InspectorUnavailable("inspector unavailable")
        current = take_source_snapshot(str(target), base_sha=original.head_sha, relevant_paths=relevant)
        if not current.is_complete or current.head_sha != original.head_sha:
            raise InspectorUnavailable("inspector unavailable")
        if value["operation"] == "snapshot":
            return {"source": current.to_dict(), "cleanup_confirmed": True}
        if value["operation"] == "apply":
            if current.to_dict() != initial.to_dict() or type(value["patch"]) is not str:
                raise InspectorUnavailable("inspector unavailable")
            shape = validate_patch_shape(value["patch"], str(target))
            if (not shape["allowed"] or not shape["changed_files"]
                    or set(shape["changed_files"]) - set(package["write_scope"])):
                raise InspectorUnavailable("inspector unavailable")
            applied = apply_patch(str(target), value["patch"], allowed_paths=package["write_scope"])
            if applied.get("applied") is not True or set(applied["changed_files"]) - set(package["write_scope"]):
                raise InspectorUnavailable("inspector unavailable")
            source = take_source_snapshot(str(target), base_sha=original.head_sha, relevant_paths=relevant)
            return {"source": source.to_dict(), "changed_files": applied["changed_files"], "cleanup_confirmed": True}
        if value["operation"] == "verify":
            from src import routing_policy
            from src.dispatch_routing import policy_snapshot
            from src.execution_package import seal_verifier_digests
            from src.routing_sandbox import run_in_sandbox

            policy = routing_policy.load_policy()
            if policy_snapshot(policy=policy).policy_hash != value["policy_hash"]:
                raise InspectorUnavailable("inspector unavailable")
            plan = package["verification"]
            if value["timeout_s"] > min(plan["timeout_s"], package["budgets"]["time_seconds"]):
                raise InspectorUnavailable("inspector unavailable")
            measured = seal_verifier_digests(str(target), plan["verifier_paths"])
            if not measured or dict(measured) != dict(plan["verifier_digests"]):
                raise InspectorUnavailable("inspector unavailable")
            directory = Path(CANONICAL_INSPECTOR_WORK_ROOT) / value["run_id"] / CANONICAL_TMOS_VERIFICATION_DIRNAME
            directory.mkdir(parents=True, mode=0o700, exist_ok=False)
            started, tick = datetime.now(timezone.utc).isoformat(), time.monotonic()
            result = run_in_sandbox(str(target), plan["command"], policy=policy,
                run_id=value["run_id"], policy_decision_id=value["policy_hash"], artifacts_dir=str(directory),
                timeout_s=value["timeout_s"])
            if (result.get("allowed") is not True or type(result.get("exit_code")) is not int
                    or result.get("timed_out") is not False or result.get("error") is not None
                    or result.get("stdout_truncated") is not False or result.get("stderr_truncated") is not False):
                raise InspectorUnavailable("inspector unavailable")
            for name in ("stdout_path", "stderr_path"):
                if Path(result[name]).resolve().parent != directory.resolve():
                    raise InspectorUnavailable("inspector unavailable")
            stdout, stderr = (_private_bytes(result[name], 65536) for name in ("stdout_path", "stderr_path"))
            after = take_source_snapshot(str(target), base_sha=original.head_sha, relevant_paths=relevant)
            if after.to_dict() != current.to_dict() or dict(seal_verifier_digests(str(target), plan["verifier_paths"])) != dict(measured):
                raise InspectorUnavailable("inspector unavailable")
            return {"source": after.to_dict(), "verifier_digests": [list(row) for row in measured],
                    "normalized_command": plan["command"], "exit_code": result["exit_code"],
                    "stdout_b64": base64.b64encode(stdout).decode(), "stderr_b64": base64.b64encode(stderr).decode(),
                    "started_at": started, "ended_at": datetime.now(timezone.utc).isoformat(),
                    "elapsed_s": time.monotonic() - tick, "host": socket.gethostname(),
                    "timeout_s": result["timeout_s"],
                    "container_id": result.get('container_id'), "image_id": result.get('image_id'),
                    "cleanup_confirmed": _container_ended(result)}
        raise InspectorUnavailable("inspector unavailable")
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        raise InspectorUnavailable("inspector unavailable") from None
