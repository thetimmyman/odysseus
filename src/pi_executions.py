"""Odysseus-side execution records for Pi runs, mapping run id to Pi session.

File-based under ``<DATA_DIR>/pi/executions/``: one JSON record plus an
append-only JSONL event ledger per execution. Records never hold credentials.
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json

from src.pi_config import data_root

# Lifecycle states are distinct and never silently rerouted.
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"
STATUS_RUNTIME_FAILURE = "runtime_failure"
STATUS_PROVIDER_FAILURE = "provider_failure"
STATUS_TOOL_FAILURE = "tool_failure"
STATUS_TASK_FAILURE = "task_failure"
STATUS_INPUT_REQUIRED = "input_required"
#: Pi's cwd or session cwd was not the assigned worktree; the prompt was not sent.
STATUS_WORKTREE_MISMATCH = "worktree_mismatch"

TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED, STATUS_CANCELLED, STATUS_RUNTIME_FAILURE,
    STATUS_PROVIDER_FAILURE, STATUS_TOOL_FAILURE, STATUS_TASK_FAILURE,
    STATUS_INPUT_REQUIRED, STATUS_WORKTREE_MISMATCH,
})

#: Failure class -> terminal status. Provider outages and ``worktree_mismatch``
#: are not model-quality evidence, so they stay separate from task failure.
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
    run_id: Optional[str] = None,
    packet_id: Optional[str] = None,
    attempt: Optional[int] = None,
    execution_package_hash: Optional[str] = None,
    dispatch_receipt_hash: Optional[str] = None,
    target_id: Optional[str] = None,
    host: Optional[str] = None,
    runtime_kind: Optional[str] = None,
    runtime: str = "pi",
    odysseus_run_id: Optional[str] = None,
    task_id: Optional[str] = None,
    jira_ticket: Optional[str] = None,
    constraints: Optional[List[str]] = None,
    execution_id: Optional[str] = None,
) -> Dict[str, Any]:
    eid = execution_id or new_execution_id()
    assigned = os.path.realpath(worktree) if worktree else None
    record: Dict[str, Any] = {
        "execution_id": eid,
        "odysseus_run_id": odysseus_run_id or eid,
        "task_id": task_id,
        "jira_ticket": jira_ticket,
        "task": task or "",
        "constraints": list(constraints or []),
        "worktree": assigned,
        #: Kept separate from ``worktree`` so a mismatch is never taken as the assignment.
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
        "refusals": [],
        "previous_status": None,
        "model": model,
        "provider": provider,
        # Dispatch binding fields (see PS638_ATTEMPT_BINDING_FIELDS).
        "run_id": run_id,
        "packet_id": packet_id,
        "attempt": attempt,
        "execution_package_hash": execution_package_hash,
        "dispatch_receipt_hash": dispatch_receipt_hash,
        "target_id": target_id,
        "host": host,
        "runtime_kind": runtime_kind,
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
    record = get_execution(execution_id)
    if record is None:
        return None
    record.update(fields)
    save_execution(record)
    return record


def append_event(execution_id: str, event: Dict[str, Any]) -> None:
    """Append one event to the JSONL ledger, flushed so it survives a runtime crash."""
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

