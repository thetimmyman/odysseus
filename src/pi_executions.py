"""src/pi_executions.py — Odysseus-side execution records for Pi runs.

Odysseus remains the source of truth for task routing and lifecycle; Pi's own
session id is *execution-runtime state*. This module keeps the mapping between
the two plus everything an operator needs to audit a delegated run:

    odysseus_run_id, task_id, jira_ticket, worktree, repo_path, base_commit,
    branch, model, provider, runtime, pi_session_id, pi_session_file,
    started_at, ended_at, status, failure_class, failure_reason, result,
    files_changed, tests_run

Storage is file-based under ``<DATA_DIR>/pi/executions/`` (one JSON record per
execution plus an append-only JSONL event ledger), matching the routing harness'
per-run artifact-archive convention rather than adding a schema migration to the
core DB. Records contain no credentials: the environment Pi runs under is built
by :mod:`src.pi_config` and is never persisted here.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json

from src.pi_config import data_root

# --- lifecycle states (section 14: distinct, never silently rerouted) -------
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_RUNTIME_FAILURE = "runtime_failure"
STATUS_PROVIDER_FAILURE = "provider_failure"
STATUS_TOOL_FAILURE = "tool_failure"
STATUS_TASK_FAILURE = "task_failure"
STATUS_INPUT_REQUIRED = "input_required"
#: Pi could not be bound to the worktree Odysseus assigned (its process cwd, or
#: its remembered session cwd, pointed somewhere else). Fail-closed outcome: the
#: task prompt was NOT sent.
STATUS_WORKTREE_MISMATCH = "worktree_mismatch"

TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED, STATUS_CANCELLED, STATUS_RUNTIME_FAILURE,
    STATUS_PROVIDER_FAILURE, STATUS_TOOL_FAILURE, STATUS_TASK_FAILURE,
    STATUS_INPUT_REQUIRED, STATUS_WORKTREE_MISMATCH,
})

#: Failure class -> terminal status. Provider timeouts/outages are NOT model
#: quality evidence and are kept separate from task failure, matching
#: ``src/agent_execution.py``'s failure taxonomy. ``worktree_mismatch`` is an
#: assignment/governance failure, never a model or reasoning signal.
FAILURE_STATUS = {
    "provider_failure": STATUS_PROVIDER_FAILURE,
    "provider_transient": STATUS_PROVIDER_FAILURE,
    "provider_permanent": STATUS_PROVIDER_FAILURE,
    "runtime_failure": STATUS_RUNTIME_FAILURE,
    "tool_failure": STATUS_TOOL_FAILURE,
    "task_failure": STATUS_TASK_FAILURE,
    "input_required": STATUS_INPUT_REQUIRED,
    "worktree_mismatch": STATUS_WORKTREE_MISMATCH,
}


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def executions_root() -> str:
    return os.path.join(data_root(), "executions")


def record_path(execution_id: str) -> str:
    return os.path.join(executions_root(), f"{execution_id}.json")


def events_path(execution_id: str) -> str:
    return os.path.join(executions_root(), f"{execution_id}.events.jsonl")


def new_execution_id() -> str:
    """Odysseus-side execution id (uuid4 hex; safe as a path component)."""
    return uuid.uuid4().hex


def create_execution(
    *,
    task: Optional[str] = None,
    worktree: Optional[str] = None,
    repo_path: Optional[str] = None,
    repo_toplevel: Optional[str] = None,
    base_commit: Optional[str] = None,
    branch: Optional[str] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    runtime: str = "pi",
    odysseus_run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    jira_ticket: Optional[str] = None,
    constraints: Optional[List[str]] = None,
    execution_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Create and persist a new execution record in ``running`` state."""
    eid = execution_id or new_execution_id()
    assigned = os.path.realpath(worktree) if worktree else None
    record: Dict[str, Any] = {
        "execution_id": eid,
        "odysseus_run_id": odysseus_run_id or eid,
        "task_id": task_id,
        "jira_ticket": jira_ticket,
        "task": task or "",
        "constraints": list(constraints or []),
        # --- worktree assignment: Odysseus owns this, Pi may only operate here ---
        "worktree": assigned,
        #: The path Odysseus explicitly assigned (kept separate from ``worktree``
        #: so a later mismatch can never be mistaken for the assignment).
        "assigned_worktree": assigned,
        "repo": os.path.realpath(repo_path) if repo_path else assigned,
        "repo_path": os.path.realpath(repo_path) if repo_path else assigned,
        "repo_toplevel": repo_toplevel,
        "branch": branch,
        "base_commit": base_commit,
        #: HEAD at assignment time — the SHA a resumed run must still match.
        "starting_sha": base_commit,
        "worktree_verified": False,
        "actual_worktree": None,
        "verification": {},
        #: Every fail-closed worktree refusal for this execution (with phase).
        "refusals": [],
        "previous_status": None,
        "model": model,
        "provider": provider,
        "runtime": runtime,
        "pi_session_id": None,
        "pi_session_file": None,
        "pi_session_cwd": None,
        "started_at": _utc_iso(),
        "ended_at": None,
        "status": STATUS_RUNNING,
        "failure_class": None,
        "failure_reason": None,
        "result": None,
        "files_changed": [],
        "tests_run": [],
        "turn_count": 0,
        "tool_call_count": 0,
    }
    save_execution(record)
    return record


def save_execution(record: Dict[str, Any]) -> None:
    atomic_write_json(record_path(record["execution_id"]), record, indent=2)


def get_execution(execution_id: str) -> Optional[Dict[str, Any]]:
    path = record_path(execution_id)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def list_executions(limit: int = 50) -> List[Dict[str, Any]]:
    """Most-recent-first execution records (bounded)."""
    try:
        names = [n for n in os.listdir(executions_root()) if n.endswith(".json")]
    except OSError:
        return []
    records: List[Dict[str, Any]] = []
    for name in names:
        rec = get_execution(name[: -len(".json")])
        if rec:
            records.append(rec)
    records.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return records[: max(1, limit)]


def update_execution(execution_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Merge ``fields`` into a stored record and persist it atomically."""
    record = get_execution(execution_id)
    if record is None:
        return None
    record.update(fields)
    save_execution(record)
    return record


def append_event(execution_id: str, event: Dict[str, Any]) -> None:
    """Append one mapped event to the execution's JSONL ledger.

    Append-only and flushed immediately so an operator can watch a live run;
    it is also the record that survives a hard runtime crash.
    """
    path = events_path(execution_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    line = json.dumps({**event, "recorded_at": _utc_iso()}, default=str)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()


def read_events(execution_id: str, since: int = 0) -> List[Dict[str, Any]]:
    """Read the ledger from index ``since`` (0-based) onward."""
    path = events_path(execution_id)
    events: List[Dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for idx, line in enumerate(fh):
                if idx < since:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return events


def finish_execution(
    execution_id: str,
    status: str,
    *,
    failure_class: Optional[str] = None,
    failure_reason: Optional[str] = None,
    result: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Move an execution to a terminal state with an explicit reason."""
    if status not in TERMINAL_STATUSES:
        raise ValueError(f"not a terminal status: {status!r}")
    return update_execution(
        execution_id,
        status=status,
        ended_at=_utc_iso(),
        failure_class=failure_class,
        failure_reason=failure_reason,
        result=result,
    )

