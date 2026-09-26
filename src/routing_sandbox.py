"""Sandboxed verification commands in a throwaway Docker container.

No network, one rw mount (the temp worktree at /work), tmpfs /tmp, all
capabilities dropped, resource and time limits, truncated output. Commands run
without a shell and must match the allowlist. Every attempt, including denied
ones, is recorded as a ToolCallRecord."""
import hashlib
import os
import shlex
import subprocess
import uuid
from datetime import datetime, timezone
from typing import Optional

from src import routing_policy
from src.routing_workdir import data_root

DEFAULT_SANDBOX = {
    "image": "python:3.12-slim",
    "cpus": 2,
    "memoryGb": 4,
    "pidsLimit": 256,
    "wallClockSeconds": 600,
    "maxOutputBytes": 1048576,
    # SELinux relabel for the bind mount; ignored elsewhere. "" disables.
    "mountLabel": "z",
    # Run as the host uid:gid: with --cap-drop ALL, container-root can't write the
    # host-owned worktree. Set False where uid mapping doesn't apply (rootless).
    "runAsHostUser": True,
    "allowedCommands": [
        "pytest",
        "python -m pytest",
        "python -m py_compile",
        "npm test",
        "node --check",
        "ruff check",
        "eslint",
        "tsc --noEmit",
        "make test",
    ],
}

# No shell expands these, but rejecting them keeps hostile input failing closed.
_SHELL_METACHARACTERS = (";", "|", "&", "`", "$(", ">", "<", "\n", "\r")


def _utcnow() -> datetime:
    """Naive UTC, matching core.database.utcnow_naive."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sandbox_config(policy: Optional[dict] = None) -> dict:
    """policy["sandbox"] merged per-key over defaults, so a partial policy stays safe."""
    if policy is None:
        policy = routing_policy.load_policy()
    overrides = policy.get("sandbox") or {}
    cfg = dict(DEFAULT_SANDBOX)
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    return cfg


def is_command_allowed(cmd: str, policy: Optional[dict] = None) -> bool:
    """No shell metacharacters, and cmd equals an allowed entry or starts with entry + " "."""
    if not cmd or not cmd.strip():
        return False
    if any(meta in cmd for meta in _SHELL_METACHARACTERS):
        return False
    normalized = " ".join(cmd.split())
    for entry in _sandbox_config(policy)["allowedCommands"]:
        if normalized == entry or normalized.startswith(entry + " "):
            return True
    return False


def _record_tool_call(db, *, run_id, cmd, worktree_path, allowed, started_at,
                      completed_at, exit_code, stdout_path, stderr_path,
                      policy_decision_id) -> Optional[str]:
    """Persist one ToolCallRecord; None when no db session was provided."""
    if db is None:
        return None
    from core.database import ToolCallRecord

    record_id = str(uuid.uuid4())
    db.add(ToolCallRecord(
        id=record_id,
        run_id=run_id,
        tool_name="sandbox_cmd",
        args_hash=hashlib.sha256(f"{cmd}\0{worktree_path}".encode("utf-8")).hexdigest(),
        allowed=allowed,
        started_at=started_at,
        completed_at=completed_at,
        exit_code=exit_code,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        policy_decision_id=policy_decision_id,
    ))
    db.commit()
    return record_id


def _default_policy_decision_id() -> str:
    try:
        return routing_policy.policy_versions()["routingPolicyVersion"]
    except Exception:
        return "unversioned"


def run_in_sandbox(worktree_path: str, cmd: str, policy: Optional[dict] = None,
                   run_id: Optional[str] = None, db=None,
                   policy_decision_id: Optional[str] = None,
                   artifacts_dir: Optional[str] = None) -> dict:
    """Run one allowlisted command in the network-less container.

    "error" == "docker_unavailable" is an infrastructure failure, not a command
    failure, and must not be scored against the patch."""
    cfg = _sandbox_config(policy)
    if policy_decision_id is None:
        policy_decision_id = _default_policy_decision_id()
    started_at = _utcnow()

    result = {
        "allowed": False, "exit_code": None, "timed_out": False, "error": None,
        "stdout_path": None, "stderr_path": None,
        "stdout_truncated": False, "stderr_truncated": False,
        "tool_call_record_id": None, "container_name": None, "cmd": cmd,
    }

    if not is_command_allowed(cmd, policy):
        result["error"] = "command_not_allowed"
        result["tool_call_record_id"] = _record_tool_call(
            db, run_id=run_id, cmd=cmd, worktree_path=worktree_path,
            allowed=False, started_at=started_at, completed_at=_utcnow(),
            exit_code=None, stdout_path=None, stderr_path=None,
            policy_decision_id=policy_decision_id,
        )
        return result

    result["allowed"] = True
    container_name = f"routing-sbx-{uuid.uuid4().hex[:12]}"
    result["container_name"] = container_name
    label = str(cfg.get("mountLabel") or "").strip()
    mount = f"{os.path.realpath(worktree_path)}:/work:rw"
    if label:
        mount += f",{label}"
    argv = [
        "docker", "run", "--rm",
        "--name", container_name,
        "--network", "none",
        "--cpus", str(cfg["cpus"]),
        "--memory", f"{cfg['memoryGb']}g",
        "--pids-limit", str(cfg["pidsLimit"]),
        "-v", mount,
        "-w", "/work",
        "--tmpfs", "/tmp",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
    ]
    if cfg.get("runAsHostUser", True) and hasattr(os, "getuid"):
        # HOME=/tmp keeps home-cache writes off the read-only container root.
        argv += ["--user", f"{os.getuid()}:{os.getgid()}", "--env", "HOME=/tmp"]
    argv += [
        cfg["image"],
        *shlex.split(cmd),
    ]

    stdout_bytes = b""
    stderr_bytes = b""
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=cfg["wallClockSeconds"])
        result["exit_code"] = proc.returncode
        stdout_bytes = proc.stdout or b""
        stderr_bytes = proc.stderr or b""
    except subprocess.TimeoutExpired as e:
        # A timeout kills only the docker client; the container must be killed by name.
        result["timed_out"] = True
        result["error"] = f"wall clock limit of {cfg['wallClockSeconds']}s exceeded"
        stdout_bytes = e.stdout or b""
        stderr_bytes = e.stderr or b""
        try:
            subprocess.run(["docker", "kill", container_name], capture_output=True, timeout=30)
        except Exception:
            pass  # best effort; --rm reaps it once the daemon notices
    except FileNotFoundError:
        result["error"] = "docker_unavailable"
        result["tool_call_record_id"] = _record_tool_call(
            db, run_id=run_id, cmd=cmd, worktree_path=worktree_path,
            allowed=True, started_at=started_at, completed_at=_utcnow(),
            exit_code=None, stdout_path=None, stderr_path=None,
            policy_decision_id=policy_decision_id,
        )
        return result

    max_bytes = int(cfg["maxOutputBytes"])
    result["stdout_truncated"] = len(stdout_bytes) > max_bytes
    result["stderr_truncated"] = len(stderr_bytes) > max_bytes

    if artifacts_dir is None:
        artifacts_dir = os.path.join(data_root(), "routing", "runs", "_sandbox")
    os.makedirs(artifacts_dir, exist_ok=True)
    stdout_path = os.path.join(artifacts_dir, f"{container_name}.stdout.log")
    stderr_path = os.path.join(artifacts_dir, f"{container_name}.stderr.log")
    with open(stdout_path, "wb") as f:
        f.write(stdout_bytes[:max_bytes])
    with open(stderr_path, "wb") as f:
        f.write(stderr_bytes[:max_bytes])
    result["stdout_path"] = stdout_path
    result["stderr_path"] = stderr_path

    result["tool_call_record_id"] = _record_tool_call(
        db, run_id=run_id, cmd=cmd, worktree_path=worktree_path,
        allowed=True, started_at=started_at, completed_at=_utcnow(),
        exit_code=result["exit_code"], stdout_path=stdout_path,
        stderr_path=stderr_path, policy_decision_id=policy_decision_id,
    )
    return result
