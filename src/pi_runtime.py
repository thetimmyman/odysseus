"""Odysseus execution adapter for Pi, the only boundary between the two planes.

Transport is Pi RPC mode (``pi --mode rpc``, JSON lines over stdin/stdout).
Pi owns its inner loop and context lifecycle, so no Odysseus compaction is
injected. Pi gets no routing/budget/policy authority, and failures are explicit
terminal states with no silent re-route.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

from src import pi_config, pi_event_map, pi_executions
from src.pi_executions import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_PROVIDER_FAILURE,
    STATUS_RUNTIME_FAILURE,
    STATUS_RUNNING,
    STATUS_TOOL_FAILURE,
)

logger = logging.getLogger(__name__)

#: Outer wall-clock ceiling only; not a context manager.
DEFAULT_MAX_SECONDS = int(os.environ.get("ODYSSEUS_PI_MAX_SECONDS", "5400"))

RESPONSE_TIMEOUT_S = float(os.environ.get("ODYSSEUS_PI_RESPONSE_TIMEOUT_S", "30"))

CANCEL_GRACE_S = float(os.environ.get("ODYSSEUS_PI_CANCEL_GRACE_S", "10"))

#: Grace after ``agent_end`` before it counts as the whole run's end. Newer Pi
#: follows every attempt's ``agent_end`` with ``auto_retry_start`` or
#: ``agent_settled``; if neither arrives, ``agent_end`` alone ended the run.
SETTLE_GRACE_S = float(os.environ.get("ODYSSEUS_PI_SETTLE_GRACE_S", "1.0"))


def _now() -> float:
    return time.time()


def find_pi_session_file(session_id: Optional[str], session_dir: Optional[str] = None) -> Optional[str]:
    """Locate ``<timestamp>_<session-id>.jsonl``; a disk lookup works even when a
    run ends before a ``get_session_stats`` round trip."""
    if not session_id:
        return None
    root = session_dir or pi_config.session_dir()
    if not os.path.isdir(root):
        return None
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if name.endswith(".jsonl") and session_id in name:
                return os.path.join(dirpath, name)
    return None


#: Provider/model error markers in Pi's stderr, which Pi merges into the RPC stream.
_PROVIDER_ERROR_HINTS = (
    "not found", "404", "unauthorized", "authentication", "api key",
    "invalid model", "no such model", "model not available", "provider error",
)


def _stderr_provider_error(lines: List[str]) -> bool:
    blob = "\n".join(lines).lower()
    return any(hint in blob for hint in _PROVIDER_ERROR_HINTS)


def _message_error(message: Any) -> Optional[str]:
    """Provider/model error text on an assistant message, if any."""
    if not isinstance(message, dict):
        return None
    err = message.get("errorMessage")
    if err or message.get("stopReason") == "error":
        return str(err) if err else "error"
    return None


def _raw_error(raw: Dict[str, Any]) -> Optional[str]:
    """Provider/model error text from ``stopReason: "error"`` + ``errorMessage``, if any."""
    err = _message_error(raw.get("message"))
    if err:
        return err
    messages = raw.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            err = _message_error(msg)
            if err:
                return err
    return None


class WorktreeMismatch(RuntimeError):
    """Pi was not in the assigned worktree; raised before the prompt is sent.

    Fails closed against stale or foreign repository state; the record is kept
    as ``worktree_mismatch`` for audit.
    """

    def __init__(self, reason: str, *, execution_id: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.execution_id = execution_id


def worktree_git_state(worktree: Optional[str]) -> Dict[str, Any]:
    """Best-effort, read-only git snapshot for reporting; git errors yield empty fields.

    Enforcement uses :func:`worktree_identity`, which fails closed.
    """
    out: Dict[str, Any] = {
        "branch": None, "head": None, "toplevel": None, "is_repo": False,
        "changed_files": [], "diff_stat": None,
    }
    if not worktree or not os.path.isdir(worktree):
        return out

    def _git(*args: str) -> Optional[str]:
        try:
            proc = subprocess.run(
                ["git", "-C", worktree, *args],
                capture_output=True, text=True, timeout=20,
            )
            return proc.stdout if proc.returncode == 0 else None
        except Exception:
            return None

    inside = _git("rev-parse", "--is-inside-work-tree")
    out["is_repo"] = bool(inside and inside.strip() == "true")
    toplevel = _git("rev-parse", "--show-toplevel")
    if toplevel:
        out["toplevel"] = toplevel.strip()
    head = _git("rev-parse", "HEAD")
    if head:
        out["head"] = head.strip()
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    if branch:
        out["branch"] = branch.strip()
    status = _git("status", "--porcelain")
    if status:
        changed: List[str] = []
        for line in status.splitlines():
            if len(line) <= 3:
                continue
            path = line[3:].strip().strip('"')
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            changed.append(path)
        out["changed_files"] = changed
    stat = _git("diff", "--stat")
    if stat:
        out["diff_stat"] = stat.strip()[:8000]
    return out


def worktree_identity(worktree: Optional[str]) -> Dict[str, Any]:
    """Return the worktree's git identity, or raise :class:`WorktreeMismatch`.

    The toplevel must be the assigned path itself: a parent repo would expose
    state that is not this task's.
    """
    if not worktree:
        raise WorktreeMismatch("no worktree assigned")
    real = os.path.realpath(worktree)
    if not os.path.isdir(real):
        raise WorktreeMismatch(f"assigned worktree does not exist: {real!r}")
    state = worktree_git_state(real)
    if not state["is_repo"]:
        raise WorktreeMismatch(f"assigned worktree is not a git repository: {real!r}")
    toplevel = os.path.realpath(state["toplevel"] or "")
    if toplevel != real:
        raise WorktreeMismatch(
            f"work-tree toplevel {toplevel!r} is not the assigned worktree {real!r}"
        )
    if not state["head"]:
        raise WorktreeMismatch(f"assigned worktree has no resolvable HEAD: {real!r}")
    return {
        "worktree": real,
        "toplevel": toplevel,
        "branch": state["branch"],
        "head": state["head"],
    }


def git_is_ancestor(worktree: str, ancestor_sha: str, ref: str) -> bool:
    """Accepts a branch that advanced from the assigned SHA, not an unrelated lineage."""
    if not worktree or not ancestor_sha or not ref:
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", worktree, "merge-base", "--is-ancestor", ancestor_sha, ref],
            capture_output=True, text=True, timeout=20,
        )
        return proc.returncode == 0
    except Exception:
        return False


def process_cwd(pid: Optional[int]) -> Optional[str]:
    """Best-effort process cwd (``/proc`` or ``lsof``); ``None`` elsewhere."""
    if not pid:
        return None
    if sys.platform.startswith("linux"):
        try:
            return os.path.realpath(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            return None
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                capture_output=True, text=True, timeout=10,
            )
            for line in proc.stdout.splitlines():
                if line.startswith("n"):
                    return os.path.realpath(line[1:])
        except Exception:
            return None
    return None


def pi_session_cwd(session_file: Optional[str]) -> Optional[str]:
    """The ``cwd`` in a Pi session header; on resume Pi would adopt it, so it is checked."""
    if not session_file or not os.path.isfile(session_file):
        return None
    try:
        with open(session_file, "r", encoding="utf-8") as fh:
            for _ in range(5):  # header is first, but tolerate leading noise
                line = fh.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("cwd"):
                    return os.path.realpath(str(entry["cwd"]))
    except OSError:
        return None
    return None


class PiExecution:

    __slots__ = ("execution_id", "proc", "reader", "responses", "stderr_tail",
                 "session_id", "session_file", "started", "last_activity",
                 "watchdog", "abort_requested", "seen_files", "seen_tests",
                 "final_text", "cancelled", "last_failure", "saw_retry",
                 "run_finished", "keep_alive", "assigned_worktree", "expected_sha",
                 "settled", "settle_timer", "retry_exhausted", "last_agent_end_at",
                 "last_attempt_error")

    def __init__(self, execution_id: str, proc: "asyncio.subprocess.Process") -> None:
        self.execution_id = execution_id
        self.proc = proc
        self.reader: Optional[asyncio.Task] = None
        self.watchdog: Optional[asyncio.Task] = None
        self.responses: Dict[str, asyncio.Future] = {}
        self.stderr_tail: List[str] = []
        self.session_id: Optional[str] = None
        self.session_file: Optional[str] = None
        self.started = _now()
        self.last_activity = _now()
        self.abort_requested = False
        self.cancelled = False
        self.seen_files: List[str] = []
        self.seen_tests: List[str] = []
        self.final_text: str = ""
        self.last_failure: Optional[str] = None
        self.saw_retry = False
        #: An ``auto_retry_end`` reported ``success == False`` (retries used up).
        self.retry_exhausted = False
        #: Set only on a definitive run end: ``agent_settled``, or on older Pi
        #: ``agent_end``/process exit. Otherwise ``agent_end`` is an attempt end.
        self.run_finished = False
        self.settled = False
        #: Grace timer armed by ``agent_end`` for older Pi without agent_settled.
        self.settle_timer: Optional[asyncio.Task] = None
        self.last_agent_end_at: Optional[float] = None
        #: Latest attempt's error only, so a retry-then-success ends clean.
        self.last_attempt_error: Optional[str] = None
        #: Interactive executions stay alive for ``send``/steer; delegated task
        #: runs are stopped once the run finishes.
        self.keep_alive = False
        #: Assigned worktree realpath; every binding check compares against it.
        self.assigned_worktree: Optional[str] = None
        #: HEAD at assignment, so a resumed run can prove the same repository state.
        self.expected_sha: Optional[str] = None

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None
class PiRuntime:
    """One Pi RPC process per execution, spawned in the assigned worktree."""

    def __init__(self) -> None:
        self._execs: Dict[str, PiExecution] = {}

    def _command(self, provider: str, model_id: str, session_file: Optional[str]) -> List[str]:
        cmd = [
            pi_config.pi_bin(),
            "--mode", "rpc",
            "--provider", provider,
            "--model", pi_config.model_spec(provider, model_id),
            "--session-dir", pi_config.session_dir(),
        ]
        if session_file:
            cmd += ["--session", session_file]
        return cmd

    async def _spawn(self, worktree: str, provider: str, model_id: str,
                     session_file: Optional[str] = None) -> PiExecution:
        pi_config.ensure_pi_model_config(provider, model_id)
        os.makedirs(pi_config.session_dir(), exist_ok=True)
        cmd = self._command(provider, model_id, session_file)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=worktree,
            env=pi_config.pi_environment(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            limit=1024 * 1024,
        )
        return PiExecution("", proc)

    def _write(self, handle: PiExecution, payload: Dict[str, Any]) -> None:
        if handle.proc.stdin is None or handle.proc.returncode is not None:
            return
        handle.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))

    async def _request(self, handle: PiExecution, payload: Dict[str, Any],
                       timeout: float = RESPONSE_TIMEOUT_S) -> Optional[Dict[str, Any]]:
        req_id = payload.get("id") or uuid.uuid4().hex[:12]
        payload["id"] = req_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        handle.responses[req_id] = fut
        try:
            self._write(handle, payload)
            if handle.proc.stdin is not None:
                await handle.proc.stdin.drain()
        except Exception as exc:  # runtime already gone
            logger.debug("[pi] request write failed for %s: %s", req_id, exc)
            return None
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            handle.responses.pop(req_id, None)

    async def start(
        self,
        task: str,
        worktree: str,
        model: Optional[str] = None,
        constraints: Optional[List[str]] = None,
        *,
        repo_path: Optional[str] = None,
        odysseus_run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        jira_ticket: Optional[str] = None,
        provider: Optional[str] = None,
        execution_id: Optional[str] = None,
        keep_alive: bool = False,
        run_id: Optional[str] = None,
        packet_id: Optional[str] = None,
        attempt: Optional[int] = None,
        execution_package_hash: Optional[str] = None,
        dispatch_receipt_hash: Optional[str] = None,
        target_id: Optional[str] = None,
        host: Optional[str] = None,
        runtime_kind: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Start a delegated Pi execution and return its record.

        The caller picks the model; the adapter never does. The worktree binding
        is verified before the prompt is sent, raising :class:`WorktreeMismatch`
        on failure.
        """
        identity = worktree_identity(worktree)
        assigned = identity["worktree"]

        if provider:
            resolved_provider = provider
            resolved_model = model or pi_config.DEFAULT_PI_MODEL_ID
        else:
            resolved_provider, resolved_model = pi_config.resolve_model(model)

        record = pi_executions.create_execution(
            task=task,
            worktree=assigned,
            repo_path=repo_path or assigned,
            repo_toplevel=identity["toplevel"],
            base_commit=identity["head"],
            branch=identity["branch"],
            model=resolved_model,
            provider=resolved_provider,
            runtime=pi_config.RUNTIME_PI,
            odysseus_run_id=odysseus_run_id,
            task_id=task_id,
            jira_ticket=jira_ticket,
            constraints=constraints,
            execution_id=execution_id,
            run_id=run_id,
            packet_id=packet_id,
            attempt=attempt,
            execution_package_hash=execution_package_hash,
            dispatch_receipt_hash=dispatch_receipt_hash,
            target_id=target_id,
            host=host,
            runtime_kind=runtime_kind,
        )
        eid = record["execution_id"]

        # Spawn in the assigned cwd only, never a remembered or inherited one.
        handle = await self._spawn(assigned, resolved_provider, resolved_model)
        handle.execution_id = eid
        handle.keep_alive = keep_alive
        handle.assigned_worktree = assigned
        handle.expected_sha = identity["head"]
        self._execs[eid] = handle
        handle.reader = asyncio.create_task(self._pump(eid))
        handle.watchdog = asyncio.create_task(self._watch(eid))

        state = await self._request(handle, {"type": "get_state"})
        if state and isinstance(state.get("data"), dict):
            handle.session_id = state["data"].get("sessionId")
            handle.session_file = handle.session_file or find_pi_session_file(handle.session_id)
            pi_executions.update_execution(
                eid,
                pi_session_id=handle.session_id,
                pi_session_file=handle.session_file,
            )

        # A Pi outside the assigned worktree never receives the prompt.
        await self._verify_worktree_binding(eid, handle, phase="start")

        await self._request(handle, {"type": "prompt",
                                     "message": self._compose_prompt(task, constraints)})
        return pi_executions.get_execution(eid) or record

    async def start_from_pin(
        self,
        pin: Dict[str, Any],
        *,
        task: str,
        worktree: str,
        attempt: int = 1,
        run_id: str = "",
        packet_id: str = "",
        execution_package_hash: str = "",
        repo_path: Optional[str] = None,
        odysseus_run_id: Optional[str] = None,
        task_id: Optional[str] = None,
        jira_ticket: Optional[str] = None,
        constraints: Optional[List[str]] = None,
        execution_id: Optional[str] = None,
        keep_alive: bool = False,
    ) -> Dict[str, Any]:
        """Start from a ``BoundDispatch.pin_for`` pin, checking its binding first.

        This does not authenticate a caller-built dict; pass the canonical pin.
        """
        if not isinstance(pin, dict):
            raise ValueError("a complete canonical dispatch pin is required")
        runtime_kind = pin.get("runtime_kind")
        if not isinstance(runtime_kind, str) or runtime_kind.strip() != "pi":
            raise ValueError("a Pi adapter must never execute a pin that is not a Pi target")
        required_text = (
            "provider", "model", "target_id", "profile_id",
            "receipt_hash", "run_id", "packet_id", "execution_package_hash",
        )
        for field in required_text:
            value = pin.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"dispatch pin is missing {field}")
        if not isinstance(pin.get("host"), str):
            raise ValueError("dispatch pin is missing host")
        provider = pin["provider"].strip()
        model = pin["model"].strip()
        runtime_kind = runtime_kind.strip()
        if not re.fullmatch(r"[a-f0-9]{64}", pin["receipt_hash"]):
            raise ValueError("dispatch pin receipt hash is invalid")
        if not re.fullmatch(r"[a-f0-9]{64}", pin["execution_package_hash"]):
            raise ValueError("dispatch pin execution package hash is invalid")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError("attempt must be a positive integer")
        if (run_id != pin["run_id"] or packet_id != pin["packet_id"]
                or execution_package_hash != pin["execution_package_hash"]):
            raise ValueError("caller run, packet, or package binding differs from the dispatch pin")
        if "attempt" in pin and pin["attempt"] != attempt:
            raise ValueError("caller attempt differs from the dispatch pin")
        return await self.start(
            task=task,
            worktree=worktree,
            model=model,
            provider=provider,
            run_id=run_id,
            packet_id=packet_id,
            attempt=attempt,
            execution_package_hash=execution_package_hash,
            dispatch_receipt_hash=pin.get("receipt_hash"),
            target_id=pin.get("target_id"),
            host=pin.get("host"),
            runtime_kind=runtime_kind,
            repo_path=repo_path,
            odysseus_run_id=odysseus_run_id,
            task_id=task_id,
            jira_ticket=jira_ticket,
            constraints=constraints,
            execution_id=execution_id,
            keep_alive=keep_alive,
        )

    @staticmethod
    def _compose_prompt(task: str, constraints: Optional[List[str]]) -> str:
        """Deliberately thin: Pi discovers the repo itself."""
        lines = [task.strip()]
        if constraints:
            lines.append("")
            lines.append("Constraints:")
            lines.extend(f"- {c}" for c in constraints if str(c).strip())
        return "\n".join(lines)
    async def _verify_worktree_binding(self, execution_id: str, handle: PiExecution,
                                       phase: str) -> Dict[str, Any]:
        """Prove Pi runs in the assigned worktree: process cwd, session-header cwd
        and git identity must all match. Any failure is fatal."""
        record = pi_executions.get_execution(execution_id) or {}
        assigned = handle.assigned_worktree or record.get("assigned_worktree") \
            or record.get("worktree")
        if not assigned:
            await self._refuse_worktree(execution_id, handle, "no assigned worktree on the execution")
            raise WorktreeMismatch("no assigned worktree on the execution",
                                   execution_id=execution_id)
        assigned = os.path.realpath(assigned)

        evidence: Dict[str, Any] = {"phase": phase, "assigned_worktree": assigned}
        problems: List[str] = []

        live_cwd = process_cwd(getattr(handle.proc, "pid", None))
        evidence["process_cwd"] = live_cwd
        if live_cwd is not None and os.path.realpath(live_cwd) != assigned:
            problems.append(
                f"pi process cwd {os.path.realpath(live_cwd)!r} != assigned worktree {assigned!r}")

        session_file = handle.session_file or record.get("pi_session_file") \
            or find_pi_session_file(handle.session_id or record.get("pi_session_id"))
        session_cwd = pi_session_cwd(session_file)
        evidence["pi_session_file"] = session_file
        evidence["pi_session_cwd"] = session_cwd
        if session_cwd is not None and os.path.realpath(session_cwd) != assigned:
            problems.append(
                f"pi session cwd {os.path.realpath(session_cwd)!r} != assigned worktree {assigned!r}")

        try:
            identity = worktree_identity(assigned)
            evidence["repo_toplevel"] = identity["toplevel"]
            evidence["branch"] = identity["branch"]
            evidence["head"] = identity["head"]
        except WorktreeMismatch as exc:
            problems.append(str(exc))

        expected_sha = handle.expected_sha or record.get("starting_sha")
        actual_sha = evidence.get("head")
        if expected_sha and actual_sha:
            same = (actual_sha.startswith(expected_sha[:12])
                    or expected_sha.startswith(actual_sha[:12]))
            if same:
                evidence["head_matches_starting_sha"] = True
            elif git_is_ancestor(assigned, expected_sha, actual_sha):
                # Advanced from the assigned SHA on the same lineage: accepted.
                evidence["head_matches_starting_sha"] = False
                evidence["head_descends_from_starting_sha"] = True
            else:
                evidence["head_matches_starting_sha"] = False
                problems.append(
                    f"worktree HEAD {actual_sha[:12]} is neither the assigned starting SHA "
                    f"{expected_sha[:12]} nor descended from it")

        if problems:
            reason = f"worktree assignment not honored ({phase}): " + "; ".join(problems)
            await self._refuse_worktree(execution_id, handle, reason, phase=phase)
            raise WorktreeMismatch(reason, execution_id=execution_id)

        pi_executions.update_execution(
            execution_id,
            worktree_verified=True,
            actual_worktree=live_cwd or session_cwd or assigned,
            pi_session_cwd=session_cwd,
            verification=evidence,
        )
        return evidence

    def _record_refusal(self, execution_id: str, reason: str, phase: str) -> None:
        """Record a worktree refusal so it can never be mistaken for a completed run."""
        record = pi_executions.get_execution(execution_id) or {}
        refusals = list(record.get("refusals") or [])
        refusals.append({"phase": phase, "reason": reason})
        pi_executions.update_execution(
            execution_id,
            refusals=refusals,
            worktree_verified=False,
            previous_status=record.get("status"),
        )
        pi_executions.finish_execution(
            execution_id, pi_executions.STATUS_WORKTREE_MISMATCH,
            failure_class="worktree_mismatch",
            failure_reason=reason,
        )
        pi_executions.append_event(
            execution_id,
            {"type": pi_event_map.FAILURE, "failure": "worktree_mismatch",
             "phase": phase, "reason": reason},
        )

    async def _refuse_worktree(self, execution_id: str, handle: PiExecution, reason: str,
                              phase: str = "start") -> None:
        handle.cancelled = True  # never report this as a normal completion
        try:
            if handle.proc.returncode is None:
                handle.proc.terminate()
        except ProcessLookupError:
            pass
        for task in (handle.reader, handle.watchdog):
            if task is not None and not task.done():
                task.cancel()
        self._record_refusal(execution_id, reason, phase)
        logger.warning("[pi] refusing execution %s: %s", execution_id, reason)



    async def _pump(self, execution_id: str) -> None:
        handle = self._execs.get(execution_id)
        if handle is None or handle.proc.stdout is None:
            return
        stdout = handle.proc.stdout
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    raw = json.loads(text)
                except ValueError:
                    # Non-JSON output is merged stderr: kept as a diagnostic tail, not an event.
                    handle.stderr_tail.append(text[:500])
                    del handle.stderr_tail[:-40]
                    continue
                handle.last_activity = _now()
                self._handle_raw(execution_id, handle, raw)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[pi] pump error for %s: %s", execution_id, exc)
        finally:
            rc = handle.proc.returncode
            if rc is None:
                try:
                    rc = await handle.proc.wait()
                except Exception:
                    rc = -1
            self._finalize(execution_id, handle, rc)

    def _handle_raw(self, execution_id: str, handle: PiExecution, raw: Dict[str, Any]) -> None:
        kind = raw.get("type")

        if kind == "response":
            req_id = raw.get("id")
            fut = handle.responses.get(req_id) if req_id else None
            if fut is not None and not fut.done():
                fut.set_result(raw)
            data = raw.get("data") if isinstance(raw.get("data"), dict) else None
            # A prompt rejected before acceptance is a provider failure, not a task failure.
            if raw.get("command") == "prompt" and raw.get("success") is False:
                handle.last_failure = "provider_failure"
                handle.stderr_tail.append(
                    ("prompt rejected: " + json.dumps(raw.get("error") or raw)[:400]))
            if raw.get("command") == "get_session_stats" and data:
                patch: Dict[str, Any] = {}
                if data.get("sessionId"):
                    handle.session_id = data["sessionId"]
                    patch["pi_session_id"] = data["sessionId"]
                if data.get("sessionFile"):
                    handle.session_file = data["sessionFile"]
                    patch["pi_session_file"] = data["sessionFile"]
                if patch:
                    pi_executions.update_execution(execution_id, **patch)
            return

        # Any event after ``agent_end`` proves it was only an attempt end.
        self._cancel_settle_grace(handle)

        for mapped in pi_event_map.map_pi_event(raw):
            pi_executions.append_event(execution_id, mapped)
            if mapped["type"] == pi_event_map.COMPLETION and mapped.get("text"):
                handle.final_text = mapped["text"]
            if mapped["type"] == pi_event_map.FAILURE:
                handle.last_failure = mapped.get("failure") or "failure"

        if kind == "agent_settled":
            handle.settled = True
            handle.run_finished = True
            if not handle.keep_alive and handle.proc.returncode is None:
                asyncio.create_task(self._graceful_stop(handle))
        elif kind == "agent_start":
            handle.run_finished = False
            handle.last_attempt_error = None
        elif kind == "auto_retry_start":
            handle.saw_retry = True
            handle.run_finished = False
        elif kind == "auto_retry_end":
            if raw.get("success") is False:
                handle.retry_exhausted = True
        elif kind == "agent_end":
            # One attempt's end. Older Pi has no ``agent_settled``, so a grace
            # timer treats it as the run end only if nothing follows.
            handle.run_finished = False
            handle.last_agent_end_at = _now()
            self._schedule_settle_grace(execution_id, handle)

        # Keep only the most recent attempt's error.
        err = _raw_error(raw)
        if err:
            handle.last_attempt_error = err[:500]

        if kind == "tool_execution_start":
            for path in pi_event_map.files_from_event(raw):
                if path not in handle.seen_files:
                    handle.seen_files.append(path)
            command = pi_event_map.command_from_event(raw)
            if command and pi_event_map.is_test_command(command) and command not in handle.seen_tests:
                handle.seen_tests.append(command)

        self._refresh_counters(execution_id, handle, kind)

    def _refresh_counters(self, execution_id: str, handle: PiExecution, kind: Optional[str]) -> None:
        if kind not in ("turn_start", "tool_execution_start"):
            return
        record = pi_executions.get_execution(execution_id)
        if not record:
            return
        if kind == "turn_start":
            record["turn_count"] = int(record.get("turn_count") or 0) + 1
        else:
            record["tool_call_count"] = int(record.get("tool_call_count") or 0) + 1
        record["files_changed"] = list(handle.seen_files)
        record["tests_run"] = list(handle.seen_tests)
        pi_executions.save_execution(record)

    def _finalize(self, execution_id: str, handle: PiExecution, returncode: int) -> None:
        """Map the process exit into an explicit terminal state."""
        self._cancel_settle_grace(handle)
        record = pi_executions.get_execution(execution_id)
        if record is None:
            return
        git_state = worktree_git_state(record.get("worktree"))
        session_id = handle.session_id or record.get("pi_session_id")
        session_file = handle.session_file or find_pi_session_file(session_id)
        session_cwd = pi_session_cwd(session_file) or record.get("pi_session_cwd")
        pi_executions.update_execution(
            execution_id,
            files_changed=git_state.get("changed_files") or record.get("files_changed") or [],
            tests_run=handle.seen_tests,
            pi_session_id=session_id,
            pi_session_file=session_file,
            pi_session_cwd=session_cwd,
            diff_stat=git_state.get("diff_stat"),
            head_after=git_state.get("head"),
        )

        # Pi may write its session file only after start, so recheck its cwd here.
        assigned = record.get("assigned_worktree") or record.get("worktree")
        if (record.get("status") not in pi_executions.TERMINAL_STATUSES
                and session_cwd and assigned
                and os.path.realpath(session_cwd) != os.path.realpath(assigned)):
            reason = (
                f"worktree assignment not honored: pi recorded session cwd "
                f"{os.path.realpath(session_cwd)!r} instead of the assigned {os.path.realpath(assigned)!r}"
            )
            self._record_refusal(execution_id, reason, "post-run")
            return

        if record.get("status") in pi_executions.TERMINAL_STATUSES:
            return
        if handle.abort_requested or handle.cancelled:
            pi_executions.finish_execution(execution_id, STATUS_CANCELLED,
                                           failure_reason="cancelled by operator")
            return

        tail = "\n".join(handle.stderr_tail[-10:])[:2000]
        # Failure evidence wins: Pi can exit rc=0 after a provider failure, so
        # neither ``run_finished`` nor rc==0 alone means success.
        has_output = bool((handle.final_text or "").strip())

        # ``provider_failure`` here only means a rejected prompt: nothing ran.
        if handle.last_failure == "provider_failure":
            pi_executions.finish_execution(
                execution_id, STATUS_PROVIDER_FAILURE,
                failure_class="provider_failure",
                failure_reason=(f"prompt rejected by pi. {tail}").strip(),
            )
            return

        meaningful = has_output or int(record.get("tool_call_count") or 0) > 0
        settled_ok = handle.run_finished or returncode == 0
        provider_evidence = (
            handle.saw_retry
            or handle.retry_exhausted
            or bool(handle.last_attempt_error)
            or _stderr_provider_error(handle.stderr_tail)
        )

        # Provider failure with no usable output must precede any completion check.
        if not has_output and provider_evidence:
            pi_executions.finish_execution(
                execution_id, STATUS_PROVIDER_FAILURE,
                failure_class="provider_failure",
                failure_reason=(
                    "provider/model failure with no assistant output "
                    f"(exit code {returncode}). "
                    f"{handle.last_attempt_error or ''} {tail}"
                ).strip(),
            )
            return
        # A tool error is terminal only when the run produced no usable output.
        if not has_output and handle.last_failure == "tool_failure" and not settled_ok:
            pi_executions.finish_execution(
                execution_id, STATUS_TOOL_FAILURE,
                failure_class="tool_failure",
                failure_reason=f"tool failure ended the run. {tail}".strip(),
            )
            return
        if not settled_ok:
            if provider_evidence:
                failure_class, status = "provider_failure", STATUS_PROVIDER_FAILURE
            elif handle.last_failure == "tool_failure":
                failure_class, status = "tool_failure", STATUS_TOOL_FAILURE
            else:
                failure_class, status = "runtime_failure", STATUS_RUNTIME_FAILURE
            pi_executions.finish_execution(
                execution_id, status,
                failure_class=failure_class,
                failure_reason=f"pi exited with code {returncode}. {tail}".strip(),
            )
            return
        # A settled run with no text and no tool call is a no-op, not a success.
        if meaningful:
            pi_executions.finish_execution(
                execution_id, STATUS_COMPLETED,
                result=(handle.final_text or "")[:20000] or None,
            )
            return
        pi_executions.finish_execution(
            execution_id, STATUS_RUNTIME_FAILURE,
            failure_class="runtime_failure",
            failure_reason=(
                "pi settled with no assistant output, no tool call and no "
                f"error evidence (exit code {returncode}). {tail}"
            ).strip(),
        )

    def _cancel_settle_grace(self, handle: PiExecution) -> None:
        task = handle.settle_timer
        handle.settle_timer = None
        if task is not None and not task.done():
            task.cancel()

    def _schedule_settle_grace(self, execution_id: str, handle: PiExecution,
                               delay: Optional[float] = None) -> None:
        """Arm the older-Pi fallback: ``agent_end`` with nothing after it."""
        self._cancel_settle_grace(handle)
        handle.settle_timer = asyncio.create_task(
            self._settle_after_grace(execution_id, handle,
                                     SETTLE_GRACE_S if delay is None else delay))

    async def _settle_after_grace(self, execution_id: str, handle: PiExecution,
                                  delay: float) -> None:
        """Treat ``agent_end`` as the run end when no retry/settle follows (older Pi)."""
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        handle.settle_timer = None
        if handle.settled or handle.abort_requested or handle.cancelled:
            return
        if handle.proc.returncode is not None:
            return  # process already gone; _finalize owns the outcome
        handle.run_finished = True
        if not handle.keep_alive:
            await self._graceful_stop(handle)

    async def _graceful_stop(self, handle: PiExecution, delay: float = 1.5) -> None:
        """Stop a finished Pi, with grace to flush the session JSONL resume needs."""
        await asyncio.sleep(delay)
        if handle.proc.returncode is None:
            try:
                handle.proc.terminate()
            except ProcessLookupError:
                pass

    async def _watch(self, execution_id: str) -> None:
        """Enforce the outer wall-clock limit; Pi keeps its own context lifecycle."""
        handle = self._execs.get(execution_id)
        if handle is None:
            return
        try:
            while _now() - handle.started < DEFAULT_MAX_SECONDS:
                await asyncio.sleep(5)
                if not handle.alive:
                    return
            await self.cancel(execution_id, reason="execution time limit reached")
        except asyncio.CancelledError:
            return

    def events(self, execution_id: str, since: int = 0) -> List[Dict[str, Any]]:
        return pi_executions.read_events(execution_id, since=since)

    def status(self, execution_id: str) -> Dict[str, Any]:
        """Current lifecycle state, including live runtime liveness."""
        record = pi_executions.get_execution(execution_id)
        if record is None:
            return {"execution_id": execution_id, "status": "unknown"}
        handle = self._execs.get(execution_id)
        record = dict(record)
        record["runtime_alive"] = bool(handle and handle.alive)
        return record

    async def send(self, execution_id: str, message: str,
                   streaming_behavior: Optional[str] = None) -> bool:
        """Send a follow-up; mid-run Pi rejects it without a ``streamingBehavior``."""
        handle = self._execs.get(execution_id)
        if handle is None or not handle.alive:
            return False
        payload: Dict[str, Any] = {"type": "prompt", "message": message}
        if streaming_behavior in ("steer", "followUp"):
            payload["streamingBehavior"] = streaming_behavior
        resp = await self._request(handle, payload)
        ok = bool(resp and resp.get("success"))
        return ok

    async def cancel(self, execution_id: str, reason: str = "operator cancel") -> bool:
        """Cancel via Pi's ``abort``, terminating the process if it does not respond."""
        handle = self._execs.get(execution_id)
        if handle is None:
            return False
        handle.abort_requested = True
        handle.cancelled = True
        if handle.alive:
            await self._request(handle, {"type": "abort"}, timeout=CANCEL_GRACE_S)
        deadline = _now() + CANCEL_GRACE_S
        while handle.alive and _now() < deadline:
            await asyncio.sleep(0.25)
        if handle.alive:
            try:
                handle.proc.terminate()
            except ProcessLookupError:
                pass
            deadline = _now() + 5
            while handle.alive and _now() < deadline:
                await asyncio.sleep(0.25)
            if handle.alive:
                try:
                    handle.proc.kill()
                except ProcessLookupError:
                    pass
        if handle.watchdog and not handle.watchdog.done():
            handle.watchdog.cancel()
        record = pi_executions.get_execution(execution_id)
        if record and record.get("status") not in pi_executions.TERMINAL_STATUSES:
            pi_executions.finish_execution(execution_id, STATUS_CANCELLED,
                                           failure_class="cancelled",
                                           failure_reason=reason)
        return True

    def result(self, execution_id: str) -> Optional[Dict[str, Any]]:
        """Terminal result summary for an execution (None if unknown)."""
        record = pi_executions.get_execution(execution_id)
        if record is None:
            return None
        events = pi_executions.read_events(execution_id)
        final_text = record.get("result")
        if not final_text:
            for ev in reversed(events):
                if ev.get("type") == pi_event_map.COMPLETION and ev.get("text"):
                    final_text = ev["text"]
                    break
        tests = [
            ev["command"] for ev in events
            if ev.get("type") == pi_event_map.TEST_EXECUTION and ev.get("command")
        ]
        return {
            "execution_id": execution_id,
            "odysseus_run_id": record.get("odysseus_run_id"),
            "pi_session_id": record.get("pi_session_id"),
            "status": record.get("status"),
            "failure_class": record.get("failure_class"),
            "failure_reason": record.get("failure_reason"),
            "final_text": final_text,
            "files_changed": record.get("files_changed") or [],
            "tests_run": record.get("tests_run") or tests,
            "worktree": record.get("worktree"),
            "assigned_worktree": record.get("assigned_worktree") or record.get("worktree"),
            "actual_worktree": record.get("actual_worktree"),
            "worktree_verified": record.get("worktree_verified"),
            "repo": record.get("repo") or record.get("repo_path"),
            "repo_toplevel": record.get("repo_toplevel"),
            "branch": record.get("branch"),
            "base_commit": record.get("base_commit"),
            "starting_sha": record.get("starting_sha") or record.get("base_commit"),
            "pi_session_cwd": record.get("pi_session_cwd"),
            "verification": record.get("verification"),
            "diff_stat": record.get("diff_stat"),
            "turn_count": record.get("turn_count"),
            "tool_call_count": record.get("tool_call_count"),
            "started_at": record.get("started_at"),
            "ended_at": record.get("ended_at"),
        }

    async def resume(self, execution_id: str, message: Optional[str] = None,
                     streaming_behavior: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Resume an execution with ``--session`` on its own session file.

        The worktree comes only from the execution record; a session recorded in
        another directory raises ``WorktreeMismatch``.
        """
        record = pi_executions.get_execution(execution_id)
        if record is None:
            return None
        handle = self._execs.get(execution_id)
        if handle is not None and handle.alive:
            if message:
                await self.send(execution_id, message, streaming_behavior)
            return self.status(execution_id)

        assigned = record.get("assigned_worktree") or record.get("worktree")
        if not assigned:
            raise WorktreeMismatch(
                "execution has no assigned worktree to resume into",
                execution_id=execution_id,
            )
        assigned = os.path.realpath(assigned)
        try:
            identity = worktree_identity(assigned)
        except WorktreeMismatch as exc:
            self._record_refusal(execution_id, str(exc), "resume-precheck")
            raise

        session_file = record.get("pi_session_file")
        # Refuse before spawning: Pi would adopt the session's remembered cwd.
        if session_file:
            recorded_cwd = pi_session_cwd(session_file)
            if recorded_cwd is not None and os.path.realpath(recorded_cwd) != assigned:
                reason = (
                    "resume refused: pi session "
                    f"{os.path.realpath(recorded_cwd)!r} is bound to a different worktree than the "
                    f"assigned {assigned!r}"
                )
                self._record_refusal(execution_id, reason, "resume-precheck")
                raise WorktreeMismatch(reason, execution_id=execution_id)

        provider = record.get("provider") or pi_config.DEFAULT_PI_PROVIDER
        model_id = record.get("model") or pi_config.DEFAULT_PI_MODEL_ID
        new_handle = await self._spawn(assigned, provider, model_id, session_file=session_file)
        new_handle.execution_id = execution_id
        new_handle.session_file = session_file
        new_handle.assigned_worktree = assigned
        new_handle.expected_sha = record.get("starting_sha") or identity["head"]
        self._execs[execution_id] = new_handle
        new_handle.reader = asyncio.create_task(self._pump(execution_id))
        new_handle.watchdog = asyncio.create_task(self._watch(execution_id))

        state = await self._request(new_handle, {"type": "get_state"})
        if state and isinstance(state.get("data"), dict):
            new_handle.session_id = state["data"].get("sessionId")
            pi_executions.update_execution(execution_id, pi_session_id=new_handle.session_id)

        await self._verify_worktree_binding(execution_id, new_handle, phase="resume")

        pi_executions.update_execution(
            execution_id,
            status=STATUS_RUNNING,
            failure_class=None,
            failure_reason=None,
        )
        if message:
            await self._request(new_handle, {"type": "prompt", "message": message})
        return self.status(execution_id)

    async def shutdown(self) -> None:
        """Terminate every live Pi execution (process teardown only)."""
        for execution_id in list(self._execs):
            handle = self._execs.get(execution_id)
            if handle and handle.alive:
                try:
                    handle.proc.terminate()
                except ProcessLookupError:
                    pass
        self._execs.clear()


_RUNTIME: Optional[PiRuntime] = None


def get_pi_runtime() -> PiRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = PiRuntime()
    return _RUNTIME
