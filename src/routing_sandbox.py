"""Sandboxed verification commands in a throwaway Docker container.

No network, one rw mount (the temp worktree at /work), tmpfs /tmp, all
capabilities dropped, resource and time limits, truncated output. Commands run
without a shell and must match the allowlist. Every attempt, including denied
ones, is recorded as a ToolCallRecord."""
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from src import routing_policy
from src.routing_workdir import data_root
from src.constants import ROUTING_SANDBOX_ARTIFACT_DIRNAME, ROUTING_SANDBOX_OWNERSHIP_SUFFIX

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


class _ClientCleanupUnresolved(RuntimeError):
    def __init__(self, primary_error):
        super().__init__('owned sandbox client cleanup unresolved')
        self.primary_error = primary_error


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
                   artifacts_dir: Optional[str] = None, timeout_s: Optional[int] = None) -> dict:
    """Run one allowlisted command in the network-less container.

    "error" == "docker_unavailable" is an infrastructure failure, not a command
    failure, and must not be scored against the patch."""
    cfg = _sandbox_config(policy)
    if timeout_s is not None:
        if type(timeout_s) is not int or timeout_s <= 0:
            raise ValueError("sandbox packet deadline must be positive")
        cfg["wallClockSeconds"] = min(cfg["wallClockSeconds"], timeout_s)
    if policy_decision_id is None:
        policy_decision_id = _default_policy_decision_id()
    started_at = _utcnow()

    result = {
        "allowed": False, "exit_code": None, "timed_out": False, "error": None,
        "stdout_path": None, "stderr_path": None,
        "stdout_truncated": False, "stderr_truncated": False,
        "tool_call_record_id": None, "container_name": None, "cmd": cmd,
        "timeout_s": cfg["wallClockSeconds"],
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
        "--entrypoint", "",
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

    max_bytes = int(cfg["maxOutputBytes"])
    if not 0 < max_bytes <= 1048576:
        raise ValueError("sandbox output budget unavailable")
    if artifacts_dir is None:
        artifacts_dir = os.path.join(data_root(), "routing", "runs", ROUTING_SANDBOX_ARTIFACT_DIRNAME)
    os.makedirs(artifacts_dir, mode=0o700, exist_ok=True)
    ownership = Path(artifacts_dir) / (container_name + ROUTING_SANDBOX_OWNERSHIP_SUFFIX)
    result.update(container_id=None, image_id=None, cleanup_confirmed=False,
                  cleanup_state="NOT_CREATED", ownership_path=str(ownership))
    deadline = time.monotonic() + cfg["wallClockSeconds"]
    stdout_bytes, stderr_bytes = b"", b""
    cid = None
    created = False
    observed = None
    client_cleanup_unknown = False
    nonce = uuid.uuid4().hex
    labels = {"odysseus.sandbox.nonce": nonce, "odysseus.sandbox.run": run_id or "unbound"}

    def remaining():
        value = deadline - time.monotonic()
        if value <= 0:
            raise subprocess.TimeoutExpired("sandbox", cfg["wallClockSeconds"])
        return value

    def call(arguments, *, limit=65536, cleanup=False):
        capture = _bounded_docker(["docker", *arguments], timeout=min(3, remaining()), limit=limit)
        if capture["timed_out"]:
            raise subprocess.TimeoutExpired(arguments, cfg["wallClockSeconds"])
        if capture["stdout_truncated"] or capture["stderr_truncated"]:
            raise ValueError("sandbox control output exceeded budget")
        return capture

    try:
        # Resolve an already installed image first: create never pulls or
        # silently substitutes a new mutable tag after the observation.
        image = call(["image", "inspect", "--format", "{{.Id}}", cfg["image"]])
        image_id = image["stdout"].decode().strip()
        if image["code"] != 0 or re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
            raise ValueError("sandbox image unavailable")
        result["image_id"] = image_id
        image_index = argv.index(cfg["image"])
        argv[image_index] = image_id
        argv[1:3] = ["create"]
        argv[2:2] = [item for key, value in labels.items() for item in ("--label", key + "=" + value)]
        # Intent is durable BEFORE create. A lost create acknowledgement stays
        # unknown: no start, retry, name lookup or name-based destruction.
        intent = {"schema": "sandbox-ownership-v1", "name": container_name,
                  "labels": labels, "image_id": image_id, "create_argv": argv,
                  "worktree": os.path.realpath(worktree_path), "state": "CREATE_REQUESTED"}
        _ownership_record(ownership, intent)
        created = True
        create = call(argv[1:])
        candidate = create["stdout"].decode().strip()
        if create["code"] != 0 or re.fullmatch(r"[0-9a-f]{64}", candidate) is None:
            raise ValueError("sandbox create acknowledgement unavailable")
        cid = result["container_id"] = candidate
        observed = _inspect_owned(call, cid, intent)
        _ownership_record(ownership.with_name(ownership.stem + ".created.json"),
                          {"container_id": cid, "inspection": observed})
        attached = _bounded_docker(["docker", "start", "--attach", "--", cid],
                                   timeout=max(0.001, remaining() - min(5, cfg['wallClockSeconds'] / 2)), limit=max_bytes)
        stdout_bytes, stderr_bytes = attached["stdout"], attached["stderr"]
        result["stdout_truncated"] = attached["stdout_truncated"]
        result["stderr_truncated"] = attached["stderr_truncated"]
        result["timed_out"] = attached["timed_out"]
        if attached["timed_out"]:
            result["error"] = f"wall clock limit of {cfg['wallClockSeconds']}s exceeded"
        elif attached["stdout_truncated"] or attached["stderr_truncated"]:
            result["error"] = "sandbox output limit exceeded"
        else:
            state = _inspect_owned(call, cid, intent)
            if state["State"].get("Running") is not False or type(state["State"].get("ExitCode")) is not int:
                raise ValueError("sandbox completion unavailable")
            result["exit_code"] = state["State"]["ExitCode"]
            if attached["code"] != result["exit_code"]:
                raise ValueError("sandbox client/container exit mismatch")
    except _ClientCleanupUnresolved:
        client_cleanup_unknown = True
        result['error'] = 'sandbox client cleanup unresolved'
    except FileNotFoundError:
        result["error"] = "docker_unavailable"
    except subprocess.TimeoutExpired:
        result["timed_out"] = True
        result["error"] = f"wall clock limit of {cfg['wallClockSeconds']}s exceeded"
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        result["error"] = "sandbox ownership or execution unavailable"
    finally:
        if cid is not None and observed is not None and not client_cleanup_unknown:
            try:
                # Recheck all ownership/isolation facts before destructive
                # controls. Never address a name, guessed or replaced CID.
                _inspect_owned(lambda args: call(args, cleanup=True), cid, intent)
                removed = call(["rm", "--force", "--", cid], cleanup=True)
                absent = call(["container", "ls", "--all", "--no-trunc", "--filter", "id=" + cid,
                               "--format", "{{.ID}}"], cleanup=True)
                result["cleanup_confirmed"] = removed["code"] == 0 and absent["code"] == 0 and absent["stdout"] == b""
            except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.TimeoutExpired):
                result["cleanup_confirmed"] = False
            result["cleanup_state"] = "OBSERVED_REMOVED" if result["cleanup_confirmed"] else "UNKNOWN"
        elif created or client_cleanup_unknown:
            result["cleanup_state"] = "UNKNOWN"
        if result["cleanup_confirmed"]:
            _ownership_record(ownership.with_name(ownership.stem + ".closed.json"),
                              {"container_id": cid, "cleanup_state": result["cleanup_state"]})
        elif created and result['error'] is None:
            result['error'] = 'sandbox cleanup unresolved'

    stdout_path = os.path.join(artifacts_dir, f"{container_name}.stdout.log")
    stderr_path = os.path.join(artifacts_dir, f"{container_name}.stderr.log")
    for path, raw in ((stdout_path, stdout_bytes), (stderr_path, stderr_bytes)):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
    result["stdout_path"], result["stderr_path"] = stdout_path, stderr_path

    result["tool_call_record_id"] = _record_tool_call(
        db, run_id=run_id, cmd=cmd, worktree_path=worktree_path,
        allowed=True, started_at=started_at, completed_at=_utcnow(),
        exit_code=result["exit_code"], stdout_path=stdout_path,
        stderr_path=stderr_path, policy_decision_id=policy_decision_id,
    )
    return result


def _ownership_record(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _inspect_owned(call, cid, intent):
    capture = call(['inspect', '--', cid])
    rows = json.loads(capture['stdout'])
    if capture['code'] != 0 or type(rows) is not list or len(rows) != 1:
        raise ValueError('sandbox inspection unavailable')
    row = rows[0]
    host, config = row['HostConfig'], row['Config']
    mounts = row['Mounts']
    argv = intent['create_argv']
    image_index = argv.index(intent['image_id'])
    user = argv[argv.index('--user') + 1] if '--user' in argv else ''
    if (row['Id'] != cid or row['Name'] != '/' + intent['name'] or row['Image'] != intent['image_id']
            or any(config.get('Labels', {}).get(key) != value for key, value in intent['labels'].items())
            or config.get('Cmd') != argv[image_index+1:]
            or config.get('Entrypoint') not in (None, []) or config.get('User', '') != user
            or host.get('NetworkMode') != 'none' or host.get('Privileged') is not False
            or host.get('AutoRemove') is not False or host.get('CapDrop') != ['ALL']
            or 'no-new-privileges' not in host.get('SecurityOpt', [])
            or host.get('PidsLimit') != int(argv[argv.index('--pids-limit')+1])
            or host.get('Memory') != int(float(argv[argv.index('--memory')+1][:-1]) * 1024**3)
            or host.get('NanoCpus') != int(float(argv[argv.index('--cpus')+1]) * 1e9)
            or host.get('Tmpfs') != {'/tmp': ''} or host.get('Binds') != [argv[argv.index('-v')+1]]
            or config.get('WorkingDir') != '/work'
            or len([mount for mount in mounts if mount.get('Type') == 'bind']) != 1
            or any(not (mount.get('Type') == 'bind' and mount.get('Source') == intent['worktree']
                        and mount.get('Destination') == '/work' and mount.get('RW') is True)
                   and not (mount.get('Type') == 'tmpfs' and mount.get('Destination') == '/tmp') for mount in mounts)):
        raise ValueError('sandbox ownership or isolation mismatch')
    return row


def _bounded_docker(argv, *, timeout, limit):
    """Cap before buffering; close owned pipes and reap independently of EOF.

    The client has its own session. Keep its leader unreaped until the owned
    group is stopped, so its PID cannot be reused for a different group.
    Container termination remains the separate exact-CID protocol above.
    """
    deadline = time.monotonic() + timeout
    capture_deadline = deadline - min(1, timeout / 2)
    streams, truncated = [bytearray(), bytearray()], [False, False]
    process, selector = None, None
    timed_out = False
    interrupted, previous = [], {}
    primary_error = None
    cleanup_unknown = False

    def defer(signum, _frame):
        interrupted.append(signum)

    try:
        # Worker threads cannot receive Python signal handlers; their owner
        # controls cancellation. Defer main-thread interrupts through custody.
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous[signum] = signal.signal(signum, defer)
        if interrupted:
            raise InterruptedError('owned sandbox client interrupted')
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True, bufsize=0,
            env={key: value for key, value in os.environ.items() if key in ('HOME', 'XDG_RUNTIME_DIR')}
                | {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        selector = selectors.DefaultSelector()
        for index, stream in enumerate((process.stdout, process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, index)
        while True:
            if interrupted:
                raise InterruptedError('owned sandbox client interrupted')
            ended = os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if ended is not None and not selector.get_map():
                break
            remaining = capture_deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if not selector.get_map():
                time.sleep(min(0.01, remaining))
            for key, _event in selector.select(min(0.05, remaining)):
                index = key.data
                chunk = os.read(key.fd, min(8192, limit + 1 - len(streams[index])))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                available = limit - len(streams[index])
                streams[index].extend(chunk[:available])
                if len(chunk) > available:
                    truncated[index] = True
            if any(truncated):
                break
    except BaseException as failure:
        primary_error = failure
    finally:
        try:
            try:
                if selector is not None:
                    try:
                        selector.close()
                    except BaseException as failure:
                        primary_error = primary_error or failure
                        cleanup_unknown = True
            finally:
                if process is not None:
                    try:
                        # Attempt each close independently. A failure retains
                        # HOLD but cannot skip owned group termination/reaping.
                        for stream in (process.stdout, process.stderr):
                            if stream is not None:
                                try:
                                    stream.close()
                                except BaseException as failure:
                                    primary_error = primary_error or failure
                                    cleanup_unknown = True
                    finally:
                        try:
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                            except BaseException as failure:
                                primary_error = primary_error or failure
                                cleanup_unknown = True
                        finally:
                            try:
                                process.wait(timeout=max(0.001, deadline - time.monotonic()))
                            except BaseException as failure:
                                primary_error = primary_error or failure
                                cleanup_unknown = True
        finally:
            for signum, handler in previous.items():
                try:
                    signal.signal(signum, handler)
                except BaseException as failure:
                    primary_error = primary_error or failure
                    cleanup_unknown = True
    if cleanup_unknown:
        raise _ClientCleanupUnresolved(primary_error) from primary_error
    if primary_error is not None:
        raise primary_error
    if interrupted:
        raise InterruptedError('owned sandbox client interrupted')
    return {'code': process.returncode, 'stdout': bytes(streams[0]), 'stderr': bytes(streams[1]),
            'stdout_truncated': truncated[0], 'stderr_truncated': truncated[1], 'timed_out': timed_out}
