"""src/routing_sandbox.py — Phase 3 sandboxed command execution (spec Section
15): verification commands run inside a throwaway Docker container with
network disabled, a filesystem allowlist of exactly ONE mount (the temp
worktree at /work, rw) plus an ephemeral tmpfs /tmp, all capabilities
dropped, no-new-privileges, CPU/memory/pids limits from policy, a wall-clock
kill, and per-stream output-size truncation. No host secrets are mounted --
the container sees only the worktree copy.

Commands are exec'd from an argv list (shlex.split, NO shell), must match the
policy's sandbox.allowedCommands prefix allowlist, and shell metacharacters
are rejected outright. Every invocation attempt -- including DENIED ones --
is persisted as a ToolCallRecord row when a db session is provided, so the
audit trail shows what a run *tried* to run, not just what policy let
through. Nothing here can commit/merge/push: the container has no network,
no credentials, and only the disposable worktree is writable."""
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

# Rejected anywhere in a command string. Commands run WITHOUT a shell, so
# none of these would expand anyway -- but rejecting them outright (rather
# than letting e.g. `pytest; rm -rf /` reach pytest as literal args) keeps
# the allowlist decision legible and fails obviously-hostile input closed.
_SHELL_METACHARACTERS = (";", "|", "&", "`", "$(", ">", "<", "\n", "\r")


def _utcnow() -> datetime:
    """Naive UTC, matching core.database.utcnow_naive's convention for
    DateTime columns (datetime.utcnow is deprecated on 3.12+)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _sandbox_config(policy: Optional[dict] = None) -> dict:
    """The effective sandbox config: policy["sandbox"] merged over defaults
    (missing keys fall back per-key, so a partial policy stays safe)."""
    if policy is None:
        policy = routing_policy.load_policy()
    overrides = policy.get("sandbox") or {}
    cfg = dict(DEFAULT_SANDBOX)
    cfg.update({k: v for k, v in overrides.items() if v is not None})
    return cfg


def is_command_allowed(cmd: str, policy: Optional[dict] = None) -> bool:
    """True iff `cmd` carries no shell metacharacters and its normalized
    prefix matches a sandbox.allowedCommands entry (entry == cmd, or cmd
    starts with entry + " ")."""
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
    """Persist one ToolCallRecord (spec Section 15 audit row). Returns the
    row id, or None when no db session was provided."""
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
    """Run one allowlisted command inside the network-less container, the
    worktree mounted rw at /work as the ONLY host mount. Returns a dict:

        {"allowed", "exit_code", "timed_out", "error", "stdout_path",
         "stderr_path", "stdout_truncated", "stderr_truncated",
         "tool_call_record_id", "container_name", "cmd"}

    exit_code is None when the command never produced one (denied / timed out
    / docker missing). "error" == "docker_unavailable" means infrastructure
    failure, NOT command failure -- callers must not score it against the
    patch. stdout/stderr are truncated to sandbox.maxOutputBytes each and
    written under `artifacts_dir` (the run's archive dir; defaults to
    data_root()/routing/runs/_sandbox for ad-hoc calls)."""
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
        # Spec: DENIED attempts are recorded too, exit_code None.
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
    argv = [
        "docker", "run", "--rm",
        "--name", container_name,
        "--network", "none",
        "--cpus", str(cfg["cpus"]),
        "--memory", f"{cfg['memoryGb']}g",
        "--pids-limit", str(cfg["pidsLimit"]),
        "-v", f"{os.path.realpath(worktree_path)}:/work:rw",
        "-w", "/work",
        "--tmpfs", "/tmp",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
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
        # subprocess kills the docker CLIENT on expiry; the container itself
        # keeps running unless explicitly killed by name.
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
