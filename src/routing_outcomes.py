"""Private, opt-in bridge from committed harness records to AI Usage events.

An HTTP completion is never acceptance. Profiles require explicit subscription
bindings, mixed-provider tasks are excluded, and accepted outcomes require both
executed blocking verification and an explicit reviewer attestation.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import stat
from datetime import datetime, timezone

PROVIDERS = {"codex", "claude-code", "command-code", "clinepass", "opencode-go"}


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _stamp(value=None):
    value = value or datetime.now(timezone.utc)
    if isinstance(value, str):
        return value
    return value.replace(tzinfo=value.tzinfo or timezone.utc).isoformat()


def _write(path, value):
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".outcome-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def meaningful_verification(result):
    """No vacuous all([]), analysis completeness, or infrastructure success."""
    if (not isinstance(result, dict) or result.get("mode") == "analysis_only"
            or result.get("passed") is not True or result.get("patch_applied") is not True
            or result.get("error") or result.get("infrastructure_error")):
        return False
    blocking = [x for x in result.get("layers", []) if x.get("blocking") is True]
    if not blocking or any(x.get("passed") is not True for x in blocking):
        return False
    required = {"regression_guard": {"existing_tests"},
                "bug_fix": {"original_failing_case", "existing_tests"},
                "feature_addition": {"existing_tests", "acceptance_tests"},
                "refactor_equivalence": {"existing_tests", "behavioral_equivalence"},
                "security_fix": {"security_assertions"}}.get(result.get("mode"))
    if required is None:
        return False
    executed = {layer.get("layer") for layer in blocking if not layer.get("skipped")
                and any(c.get("advisory") is not True for c in layer.get("commands", []))}
    if not required <= executed:
        return False
    commands = [c for layer in blocking if not layer.get("skipped")
                for c in layer.get("commands", []) if c.get("advisory") is not True]
    return bool(commands) and all(
        c.get("tool_call_record_id") and c.get("exit_code") == 0
        and not c.get("error") and not c.get("original_error")
        for c in commands)


def _config():
    path = os.environ.get("ODYSSEUS_USAGE_EXPORT_CONFIG")
    if not path:
        return None
    config = json.loads(Path(path).read_text())
    if not isinstance(config.get("profiles"), dict) or not config.get("cohort"):
        raise ValueError("usage export requires profiles and an explicit cohort")
    if any(x not in PROVIDERS for x in config["profiles"].values()):
        raise ValueError("unknown subscription provider binding")
    root = Path(config["directory"]).expanduser()
    if not root.is_absolute():
        raise ValueError("usage export directory must be absolute")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise ValueError("usage export directory must be private (0700)")
    return config, root


def export_task(db, task_id, *, acceptance=None):
    """Reconcile committed rows; optional explicit acceptance is never inferred.

    Single file transaction contains event history and deduplication state.
    events.jsonl is a replaceable projection: crashes are repaired by replay.
    """
    configured = _config()
    if configured is None:
        return {"enabled": False}
    config, root = configured
    from core.database import RoutingTask, RoutingRun, RoutingModelRun
    task = db.get(RoutingTask, task_id)
    if task is None:
        raise ValueError("task does not exist")
    runs = db.query(RoutingRun).filter(RoutingRun.task_id == task_id).all()
    rows = []
    for run in runs:
        rows.extend(db.query(RoutingModelRun).filter(RoutingModelRun.run_id == run.id).all())
    # Skipped/budget-blocked rows have no inference attempt and no latency.
    attempts = sorted([r for r in rows if json.loads(r.artifacts or "{}").get("inference_attempted") is True],
                      key=lambda r: (_stamp(r.created_at), r.id))
    bindings = {config["profiles"].get(r.model_profile_id) for r in attempts}
    lock = os.open(root / "export.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with os.fdopen(lock, "a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            path = root / "export-state.json"
            state = json.loads(path.read_text()) if path.exists() else {"tasks": {}, "events": []}
            previous = state["tasks"].get(task_id, {})
            events = state["events"]
            known = {e["event_id"] for e in events}
            prior_times = [e.get("occurred_at", e.get("started_at")) for e in events
                           if e["task_id"] == "odysseus:" + task_id]
            last_event_at = max((datetime.fromisoformat(t.replace("Z", "+00:00"))
                                 for t in prior_times), default=None)
            def emit(kind, key, **fields):
                nonlocal last_event_at
                event_id = _hash(["odysseus", task_id, kind, key])
                if event_id not in known:
                    time_field = "started_at" if kind == "task_registered" else "occurred_at"
                    at = datetime.fromisoformat(fields[time_field].replace("Z", "+00:00"))
                    # Ledger transitions are observed now; reconciliation may
                    # discover an older attempt after a recorded terminal event.
                    at = max(at, last_event_at) if last_event_at else at
                    fields[time_field] = at.isoformat()
                    last_event_at = at
                    events.append({"event_id": event_id, "type": kind,
                                   "task_id": "odysseus:" + task_id, **fields})
                    known.add(event_id)
            now = _stamp()
            snapshot = _hash([[r.id, json.loads(r.scores or "{}").get("verification")]
                              for r in attempts])
            unmapped = not attempts or None in bindings or len(bindings) != 1
            # Once mixed, never credit a task to whichever provider happened to
            # finish last. Reopen earlier credit before marking it abandoned.
            if previous.get("excluded") or unmapped:
                if previous.get("provider") and not previous.get("excluded"):
                    if previous.get("finalized"):
                        emit("task_reopened", snapshot, occurred_at=now)
                    emit("task_finalized", [snapshot, "mixed"], status="abandoned", occurred_at=now)
                    previous.update(excluded=True, finalized=True)
                elif attempts:
                    previous["excluded"] = True
                previous["reason"] = "mixed or unmapped subscription attribution"
            else:
                provider = next(iter(bindings))
                if previous.get("provider") and previous["provider"] != provider:
                    raise ValueError("subscription binding changed for an exported task")
                if previous.get("cohort") and previous["cohort"] != config["cohort"]:
                    raise ValueError("cohort changed for an exported task")
                if not previous.get("provider"):
                    emit("task_registered", "register", provider=provider,
                         cohort=config["cohort"], started_at=_stamp(task.created_at))
                if previous.get("finalized") and previous.get("snapshot") != snapshot:
                    emit("task_reopened", snapshot, occurred_at=now)
                    previous["finalized"] = False
                for row in attempts:
                    emit("turns_recorded", row.id, turns=1,
                         errors=int(json.loads(row.artifacts or "{}").get("upstream_error") is True),
                         occurred_at=_stamp(row.created_at))
                if acceptance is not None:
                    target = next((r for r in attempts if r.id == acceptance.get("model_run_id")), None)
                    verification = json.loads(target.scores or "{}").get("verification") if target else None
                    if not target or not meaningful_verification(verification):
                        raise ValueError("acceptance requires meaningful successful blocking verification")
                    from core.database import ToolCallRecord
                    check_ids = [c["tool_call_record_id"] for layer in verification["layers"]
                                 if layer.get("blocking") and not layer.get("skipped")
                                 for c in layer.get("commands", []) if not c.get("advisory")]
                    for check_id in check_ids:
                        check = db.get(ToolCallRecord, check_id)
                        if (check is None or check.run_id != target.run_id
                                or check.allowed is not True or check.exit_code != 0
                                or check.completed_at is None):
                            raise ValueError("verification check lacks a successful committed tool record")
                    from src.routing_verification import load_patch_text
                    patch = load_patch_text(target)
                    if not patch or hashlib.sha256(patch.encode()).hexdigest() != verification.get("patch_sha256"):
                        raise ValueError("accepted patch differs from verified patch")
                    if any(not isinstance(acceptance.get(key), str) or not acceptance[key].strip()
                           for key in ("reviewer", "reason")):
                        raise ValueError("reviewer identity and acceptance rationale are required")
                    acceptance_key = _hash(acceptance)
                    if previous.get("finalized") and previous.get("acceptance_key") != acceptance_key:
                        emit("task_reopened", [snapshot, acceptance_key], occurred_at=now)
                    # Seal metadata only: never include prompts, paths, commands,
                    # responses, credentials, or reviewer rationale in the export.
                    evidence = {"schema_version": 1, "task_id": "odysseus:" + task_id,
                        "model_run_id": target.id, "verification_sha256": _hash(verification),
                        "reviewer": acceptance["reviewer"],
                        "attestation_sha256": _hash(acceptance), "accepted_at": now,
                        "check_record_ids": check_ids,
                        "blocking_checks_passed": True, "semantic_acceptance": True}
                    raw = _bytes(evidence)
                    digest = hashlib.sha256(raw).hexdigest()
                    evidence_dir = root / "evidence"
                    evidence_dir.mkdir(mode=0o700, exist_ok=True)
                    evidence_path = evidence_dir / (digest + ".json")
                    _write(evidence_path, raw)
                    emit("task_finalized", [snapshot, "validated", _hash(acceptance)],
                         status="validated", occurred_at=now,
                         evidence_path=str(evidence_path), evidence_sha256=digest)
                    previous["finalized"] = True
                    previous["acceptance_key"] = acceptance_key
                elif not previous.get("finalized") and all(_failed(r) for r in attempts):
                    emit("task_finalized", [snapshot, "failed"], status="failed", occurred_at=now)
                    previous["finalized"] = True
                previous.update(provider=provider, snapshot=snapshot, cohort=config["cohort"])
            state["tasks"][task_id] = previous
            _write(path, _bytes(state))
            _write(root / "events.jsonl", b"".join(_bytes(e) + b"\n" for e in events))
            return {"enabled": True, "event_count": len(events), "task": previous}
    finally:
        pass


def export_after_commit(db, task_id):
    """Telemetry failures do not re-run already executed inference."""
    try:
        return export_task(db, task_id)
    except Exception:
        logging.getLogger(__name__).warning("AI Usage outcome export failed; reconcile committed rows", exc_info=False)
        return {"enabled": False, "error": "export failed"}


def _failed(row):
    if row.errored or row.rate_limited:
        # Transport/provider availability is reported as request errors, not
        # as failed semantic task outcomes or model-quality evidence.
        return False
    result = json.loads(row.scores or "{}").get("verification") or {}
    if result.get("passed") is not False or result.get("infrastructure_error"):
        return False
    return any(layer.get("blocking") is True and layer.get("passed") is False
               and any(c.get("exit_code") not in (None, 0) and not c.get("error")
                       and c.get("tool_call_record_id") and not c.get("advisory")
                       for c in layer.get("commands", []))
               for layer in result.get("layers", []))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Export committed outcome events or attest reviewed acceptance")
    parser.add_argument("action", choices=["reconcile", "accept"])
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--model-run-id")
    parser.add_argument("--reviewer")
    parser.add_argument("--reason")
    args = parser.parse_args()
    from core.database import SessionLocal
    db = SessionLocal()
    try:
        acceptance = None
        if args.action == "accept":
            if not all([args.model_run_id, args.reviewer, args.reason]):
                parser.error("accept requires --model-run-id, --reviewer and --reason")
            acceptance = {"model_run_id": args.model_run_id,
                          "reviewer": args.reviewer, "reason": args.reason}
        print(json.dumps(export_task(db, args.task_id, acceptance=acceptance)))
    finally:
        db.close()


if __name__ == "__main__":
    main()
