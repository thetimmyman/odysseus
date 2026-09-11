"""src/pi_runtime.py — Odysseus execution adapter for Pi Coding.

This is the *only* boundary between the two planes. Odysseus decides what work
should happen, who should do it and under what policy/budget; this adapter hands
the task to Pi and reports back what Pi did.

    execution = pi_runtime.start(task=..., worktree=..., model=..., constraints=...)
    events    = pi_runtime.events(execution["execution_id"])
    pi_runtime.send(execution["execution_id"], message)
    pi_runtime.cancel(execution["execution_id"])
    result    = pi_runtime.result(execution["execution_id"])

Transport is Pi's supported programmatic interface: RPC mode
(``pi --mode rpc``), strict JSON-lines over stdin/stdout, verified against the
installed ``@earendil-works/pi-coding-agent`` 0.74.2 protocol. No terminal
scraping, no in-process Node bridge.

Deliberate non-responsibilities (see the architectural boundary):

  * Pi owns the inner coding loop, reasoning/tool rounds, repository
    exploration, filesystem/shell/git operations and its own context lifecycle
    (including compaction). This adapter never injects Odysseus's
    ``context_compactor`` into a Pi run.
  * Pi never gets Odysseus routing/budget/policy authority — it is launched with
    a minimal environment (``pi_config.pi_environment``) and a models.json that
    lists only the local execution model.
  * A failure is reported as an explicit terminal state. There is no silent
    re-route to another model/runtime in this integration.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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

#: Outer execution wall-clock ceiling (Odysseus-side limit, NOT a context
#: manager). Pi's inner loop is unconstrained by us up to this bound.
DEFAULT_MAX_SECONDS = int(os.environ.get("ODYSSEUS_PI_MAX_SECONDS", "5400"))

#: How long to wait for a command response before declaring the runtime wedged.
RESPONSE_TIMEOUT_S = float(os.environ.get("ODYSSEUS_PI_RESPONSE_TIMEOUT_S", "30"))

#: Grace period after ``abort`` before the process is killed outright.
CANCEL_GRACE_S = float(os.environ.get("ODYSSEUS_PI_CANCEL_GRACE_S", "10"))


def _now() -> float:
    return time.time()


def find_pi_session_file(session_id: Optional[str], session_dir: Optional[str] = None) -> Optional[str]:
    """Locate Pi's session JSONL for ``session_id`` under the session dir.

    Pi stores sessions as ``<timestamp>_<session-id>.jsonl`` in its session
    directory (verified against 0.74.2). Looking the file up on disk is
    deterministic even when a run ends faster than a ``get_session_stats`` round
    trip — the file is what makes resume possible.
    """
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


class WorktreeMismatch(RuntimeError):
    """Pi was not (or would not be) operating in the worktree Odysseus assigned.

    Raised BEFORE the task prompt is sent: fail closed rather than let a coding
    agent run against a stale, remembered, or foreign repository state. The
    execution record is persisted with ``status == "worktree_mismatch"`` and
    ``worktree_verified == False`` so the refusal is auditable.
    """

    def __init__(self, reason: str, *, execution_id: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.execution_id = execution_id


def worktree_git_state(worktree: Optional[str]) -> Dict[str, Any]:
    """Read-only git snapshot of the assigned worktree.

    Gives Odysseus the repository identity (toplevel), branch, base commit and
    the final diff summary without ever committing or pushing (the routing
    harness' hard rule). Best-effort on the reporting fields: any git failure
    degrades to empty fields rather than blocking a run. Callers whose job is to
    *enforce* assignment use :func:`worktree_identity`, which fails closed.
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
    """Assert the assigned worktree is a usable git work tree; return identity.

    Fails closed (raises :class:`WorktreeMismatch`) when the path is missing, is
    not a git repository, or the work-tree toplevel is not the assigned path
    itself — a nested/parent repo would let Pi operate on repository state that
    is not this task's.
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
    """True when ``ancestor_sha`` is an ancestor of ``ref`` in this work tree.

    Used to accept a branch that legitimately advanced from the SHA recorded at
    assignment while still refusing a HEAD that belongs to an unrelated lineage.
    """
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
    """Best-effort OS-level working directory of a live process.

    Linux: ``/proc/<pid>/cwd``. macOS: ``lsof``. Other platforms: ``None`` —
    callers then rely on Pi's own session-recorded cwd, which Pi writes itself
    and is therefore Pi-reported rather than Odysseus-assumed.
    """
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
    """The ``cwd`` Pi itself recorded in a session file's header.

    Pi writes this header at session creation, so it is Pi-reported state that
    can be checked against the assigned worktree without trusting Pi's
    defaults. A session header pointing elsewhere is exactly the stale
    remembered worktree this adapter must refuse — including on resume, where
    Pi would otherwise adopt the session's remembered directory.
    """
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
    """Live state for one Pi subprocess (not persisted — the record is)."""

    __slots__ = ("execution_id", "proc", "reader", "responses", "stderr_tail",
                 "session_id", "session_file", "started", "last_activity",
                 "watchdog", "abort_requested", "seen_files", "seen_tests",
                 "final_text", "cancelled", "last_failure", "saw_retry",
                 "run_finished", "keep_alive", "assigned_worktree", "expected_sha")

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
        #: Failure classification signals for the terminal state (section 14).
        self.last_failure: Optional[str] = None
        self.saw_retry = False
        #: Set when Pi reports the run finished (``agent_end``). A real Pi RPC
        #: process stays alive after a run, so this — not process exit — is what
        #: marks a delegated one-shot execution complete.
        self.run_finished = False
        #: Interactive executions stay alive for ``send``/steer; delegated task
        #: runs are stopped once the run finishes.
        self.keep_alive = False
        #: The worktree Odysseus assigned to THIS execution (realpath). Pi may
        #: operate only here; every binding check compares against it.
        self.assigned_worktree: Optional[str] = None
        #: HEAD recorded at assignment time (kept so a resumed run still proves
        #: it is operating on this task's repository state).
        self.expected_sha: Optional[str] = None

    @property
    def alive(self) -> bool:
        return self.proc.returncode is None
class PiRuntime:
    """Manages Pi RPC subprocesses on behalf of the Odysseus control plane.

    One live process per execution id. The process is spawned in the *assigned*
    worktree supplied by Odysseus — Pi never chooses repository state itself.
    """

    def __init__(self) -> None:
        self._execs: Dict[str, PiExecution] = {}

    # -- process plumbing ---------------------------------------------------
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
        """Send a command and await its correlated ``response``."""
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

    # -- start --------------------------------------------------------------
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
    ) -> Dict[str, Any]:
        """Start a delegated Pi execution and return its Odysseus record.

        ``model``/``provider`` are supplied by the caller (routing policy) — the
        adapter never picks a model by itself. ``worktree`` is required: Pi is an
        execution plane, not a repository selector.

        Worktree assignment is enforced, not assumed: the assigned path is
        validated as a git work tree, Pi is launched with exactly that cwd, and
        the live binding is verified BEFORE the task prompt is sent. A mismatch
        raises :class:`WorktreeMismatch` (fail closed) with the execution record
        left in ``worktree_mismatch`` for audit.
        """
        # Fail closed before anything is spawned: the assignment itself must be
        # a real, self-contained git work tree.
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
        )
        eid = record["execution_id"]

        # Pi is spawned with the ASSIGNED cwd. Nothing else is consulted — not
        # Pi's remembered session, not whatever directory the Odysseus process
        # happens to be in, and never a previously used worktree.
        handle = await self._spawn(assigned, resolved_provider, resolved_model)
        handle.execution_id = eid
        handle.keep_alive = keep_alive
        handle.assigned_worktree = assigned
        handle.expected_sha = identity["head"]
        self._execs[eid] = handle
        handle.reader = asyncio.create_task(self._pump(eid))
        handle.watchdog = asyncio.create_task(self._watch(eid))

        # Capture Pi's session identity immediately (get_state returns sessionId
        # before any prompt).
        state = await self._request(handle, {"type": "get_state"})
        if state and isinstance(state.get("data"), dict):
            handle.session_id = state["data"].get("sessionId")
            handle.session_file = handle.session_file or find_pi_session_file(handle.session_id)
            pi_executions.update_execution(
                eid,
                pi_session_id=handle.session_id,
                pi_session_file=handle.session_file,
            )

        # Bind check happens BEFORE the task is handed over. A Pi that landed
        # anywhere other than the assigned worktree never receives the prompt.
        await self._verify_worktree_binding(eid, handle, phase="start")

        await self._request(handle, {"type": "prompt",
                                     "message": self._compose_prompt(task, constraints)})
        return pi_executions.get_execution(eid) or record

    @staticmethod
    def _compose_prompt(task: str, constraints: Optional[List[str]]) -> str:
        """The task handoff. Deliberately thin: Pi discovers the repo itself."""
        lines = [task.strip()]
        if constraints:
            lines.append("")
            lines.append("Constraints:")
            lines.extend(f"- {c}" for c in constraints if str(c).strip())
        return "\n".join(lines)
    # -- worktree assignment enforcement ------------------------------------
    async def _verify_worktree_binding(self, execution_id: str, handle: PiExecution,
                                       phase: str) -> Dict[str, Any]:
        """Prove Pi is operating in the worktree Odysseus assigned to this run.

        Three independent checks, all compared against the ASSIGNED path:

        1. OS-level process cwd (``/proc/<pid>/cwd`` on Linux, ``lsof`` on macOS) —
           catches a Pi that chdir'd somewhere else, e.g. into a remembered
           session directory.
        2. Pi's own session-header ``cwd`` — Pi-reported state written by Pi
           itself, available on every platform, and on resume it is exactly the
           remembered directory Pi would adopt.
        3. The git identity of the assigned path (toplevel / branch / HEAD) —
           read-only, via :func:`worktree_identity`.

        Every failure is fatal: the task prompt is never sent.
        """
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

        # 1. OS-level process cwd.
        live_cwd = process_cwd(getattr(handle.proc, "pid", None))
        evidence["process_cwd"] = live_cwd
        if live_cwd is not None and os.path.realpath(live_cwd) != assigned:
            problems.append(
                f"pi process cwd {os.path.realpath(live_cwd)!r} != assigned worktree {assigned!r}")

        # 2. Pi's own recorded session cwd.
        session_file = handle.session_file or record.get("pi_session_file") \
            or find_pi_session_file(handle.session_id or record.get("pi_session_id"))
        session_cwd = pi_session_cwd(session_file)
        evidence["pi_session_file"] = session_file
        evidence["pi_session_cwd"] = session_cwd
        if session_cwd is not None and os.path.realpath(session_cwd) != assigned:
            problems.append(
                f"pi session cwd {os.path.realpath(session_cwd)!r} != assigned worktree {assigned!r}")

        # 3. Git identity of the assigned path itself.
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
                # The branch legitimately advanced from the assigned starting
                # SHA (the task's own lineage) — acceptable, and recorded.
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
        """Persist a fail-closed worktree refusal as the execution's state.

        The refusal is always visible: ``refusals[]`` accumulates every refused
        attempt (with its phase), ``previous_status`` preserves what the record
        said before, and the execution's status becomes ``worktree_mismatch`` so
        the operator cannot mistake a refused run for a completed one.
        """
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
        """Terminate a mis-bound Pi and record the fail-closed outcome."""
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



    # -- event pump ---------------------------------------------------------
    async def _pump(self, execution_id: str) -> None:
        """Read Pi's JSONL stdout until the process ends."""
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
                    # Non-JSON stdout (Pi shares stderr into stdout here) is kept
                    # as a bounded diagnostic tail, never as an event.
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
            # A prompt rejected BEFORE acceptance (unknown model, bad provider
            # credential, malformed request) is a provider/model failure, not a
            # task failure.
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

        # Pi events -> Odysseus execution events.
        for mapped in pi_event_map.map_pi_event(raw):
            pi_executions.append_event(execution_id, mapped)
            if mapped["type"] == pi_event_map.COMPLETION and mapped.get("text"):
                handle.final_text = mapped["text"]
            if mapped["type"] == pi_event_map.FAILURE:
                handle.last_failure = mapped.get("failure") or "failure"

        if kind == "auto_retry_start":
            handle.saw_retry = True

        if kind == "agent_end":
            # Real Pi keeps its RPC process alive after a run; record completion
            # here and, for a delegated one-shot run, stop the child so the
            # execution reaches an explicit terminal state.
            handle.run_finished = True
            if not handle.keep_alive and handle.proc.returncode is None:
                asyncio.create_task(self._graceful_stop(handle))

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
        """Map the process exit into an explicit Odysseus terminal state."""
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

        # Late assignment check: Pi may only write its session file once the run
        # starts, so the live binding check at start time can miss it. If the
        # recorded session cwd turns out to be a different worktree, this run is
        # refused rather than reported as a success.
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
        if handle.run_finished or returncode == 0:
            # Pi reported the run complete (``agent_end``) or exited cleanly.
            pi_executions.finish_execution(
                execution_id, STATUS_COMPLETED,
                result=(handle.final_text or "")[:20000] or None,
            )
            return
        tail = "\n".join(handle.stderr_tail[-10:])[:2000]
        # Distinguish the failure classes the operator needs (section 14). A
        # provider outage / rejected prompt or an exhausted auto-retry is a
        # provider failure; a failed tool that ended the run is a tool failure;
        # anything else is an opaque runtime failure. Never a silent reroute.
        if handle.last_failure == "provider_failure" or handle.saw_retry:
            pi_executions.finish_execution(
                execution_id, STATUS_PROVIDER_FAILURE,
                failure_class="provider_failure",
                failure_reason=f"pi exited with code {returncode}. {tail}".strip(),
            )
            return
        if handle.last_failure == "tool_failure":
            pi_executions.finish_execution(
                execution_id, STATUS_TOOL_FAILURE,
                failure_class="tool_failure",
                failure_reason=f"pi exited with code {returncode}. {tail}".strip(),
            )
            return
        pi_executions.finish_execution(
            execution_id, STATUS_RUNTIME_FAILURE,
            failure_class="runtime_failure",
            failure_reason=f"pi exited with code {returncode}. {tail}".strip(),
        )

    async def _graceful_stop(self, handle: PiExecution, delay: float = 1.5) -> None:
        """Stop a Pi process whose run has finished.

        The grace period lets Pi flush its session JSONL (what makes resume
        possible) before the process goes away.
        """
        await asyncio.sleep(delay)
        if handle.proc.returncode is None:
            try:
                handle.proc.terminate()
            except ProcessLookupError:
                pass

    async def _watch(self, execution_id: str) -> None:
        """Enforce the OUTER execution wall-clock limit.

        This is an execution limit only. Pi keeps ownership of its inner context
        lifecycle; Odysseus does not inject a second compaction system.
        """
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

    # -- public adapter API -------------------------------------------------
    def events(self, execution_id: str, since: int = 0) -> List[Dict[str, Any]]:
        """Mapped Pi events observed for this execution (observability)."""
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
        """Send a follow-up message to a running execution.

        Pi requires an explicit ``streamingBehavior`` (``steer`` / ``followUp``)
        while it is mid-run; otherwise the prompt is rejected.
        """
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
        """Cancel an active Pi execution.

        Prefers Pi's own ``abort`` command (it stops the inner loop cleanly and
        Pi reports the aborted turn); falls back to terminating the process if
        the runtime does not respond within the grace period.
        """
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
            # Worktree assignment identity (Odysseus-owned).
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
        """Resume an interrupted execution against its own Pi session.

        Pi sessions are files; resuming relaunches Pi with ``--session`` pointed
        at the SAME session file, so continuation is the same execution rather
        than an unrelated new chat/task.

        The worktree is taken from the EXECUTION RECORD and nothing else — a
        resume can never be pointed at a different worktree, and it will not
        proceed when the recorded Pi session belongs to another directory
        (``WorktreeMismatch``, fail closed). Pi's remembered session cwd is
        treated as untrusted input to be verified, never as the launch context.
        """
        record = pi_executions.get_execution(execution_id)
        if record is None:
            return None
        handle = self._execs.get(execution_id)
        if handle is not None and handle.alive:
            if message:
                await self.send(execution_id, message, streaming_behavior)
            return self.status(execution_id)

        # Odysseus owns the assignment: the record's assigned worktree, exactly
        # as it was set when the execution was created.
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
        # Refuse BEFORE spawning when the recorded session belongs to a
        # different directory: Pi would adopt that remembered cwd on resume.
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

        # Same fail-closed binding check as a fresh start, before any follow-up
        # message is delivered.
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
    """Process-wide Pi runtime adapter."""
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = PiRuntime()
    return _RUNTIME

