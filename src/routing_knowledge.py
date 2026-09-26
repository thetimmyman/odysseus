"""Knowledge base lifecycle: evidence-grounded lessons from routing runs.

Invariant: entries are advisory context only and must never gate a routing,
budget or verification decision. Retrieved items carry an explicit advisory
label, and tests assert no other src/ module imports the retrieval surface.

Lifecycle (human/admin actions, recorded on the row and in audit_log):

    draft      -> validated   validate_entry(actor)
    draft      -> rejected    reject_entry(actor)
    validated  -> superseded  supersede_entry(actor, replacement_id)
    validated  -> expired     expire_entry(actor, rationale)   # rationale required
    expired    -> validated   validate_entry(actor, revalidate_expired=True)

Anything else raises KnowledgeTransitionError. Re-validating an expired entry
must be an explicit human decision, never implicit.

Every entry needs non-empty grounding evidence (ValueError otherwise).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("odysseus.routing.knowledge")

STATUSES = ("draft", "validated", "rejected", "superseded", "expired")

# The explicit advisory label stamped on every retrieved entry.
ADVISORY_NOTE = "knowledge entries are advisory context, never policy"


class KnowledgeTransitionError(Exception):
    """An illegal lifecycle transition was requested."""


def _now() -> datetime:
    # Naive UTC, matching every created_at default in core/database.py.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _append_audit(row, entry: str) -> None:
    line = f"[{_now().isoformat()}] {entry}"
    row.audit_log = f"{row.audit_log}\n{line}" if row.audit_log else line


def _validate_evidence(evidence) -> List[Any]:
    if not isinstance(evidence, list) or not evidence:
        raise ValueError(
            "evidence is required and must be a non-empty list of grounding "
            "references (run ids / model-run ids / manifest ids / artifact "
            "paths / verification results)")
    return evidence


def entry_to_dict(row) -> Dict[str, Any]:
    def _loads(text, default):
        try:
            v = json.loads(text) if text else default
        except Exception:
            return default
        return v if isinstance(v, type(default)) else default

    return {
        "id": row.id,
        "title": row.title,
        "body": row.body,
        "status": row.status,
        "category": row.category,
        "tags": _loads(row.tags, []),
        "evidence": _loads(row.evidence, []),
        "source_task_id": row.source_task_id,
        "source_model_run_id": row.source_model_run_id,
        "created_by": row.created_by,
        "validated_by": row.validated_by,
        "validated_at": row.validated_at.isoformat() if row.validated_at else None,
        "superseded_by_id": row.superseded_by_id,
        "expires_rationale": row.expires_rationale,
        "expired_at": row.expired_at.isoformat() if row.expired_at else None,
        "audit_log": row.audit_log,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def create_draft(db, *, title: str, body: str, evidence: List[Any],
                 category: Optional[str] = None, tags: Optional[List[str]] = None,
                 source_task_id: Optional[str] = None,
                 source_model_run_id: Optional[str] = None,
                 created_by: str = "human"):
    """Create a draft; an ungrounded lesson (empty evidence) is never persisted."""
    from core.database import KnowledgeBaseEntry

    if not (title or "").strip():
        raise ValueError("title is required")
    if not (body or "").strip():
        raise ValueError("body is required")
    evidence = _validate_evidence(evidence)

    row = KnowledgeBaseEntry(
        id=str(uuid.uuid4()),
        title=title.strip(),
        body=body,
        status="draft",
        category=category,
        tags=json.dumps(list(tags)) if tags else None,
        evidence=json.dumps(evidence),
        source_task_id=source_task_id,
        source_model_run_id=source_model_run_id,
        created_by=created_by or "human",
    )
    _append_audit(row, f"created as draft by {row.created_by}")
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _get_entry(db, entry_id: str):
    from core.database import KnowledgeBaseEntry

    row = db.get(KnowledgeBaseEntry, entry_id)
    if not row:
        raise LookupError(f"no knowledge entry with id {entry_id!r}")
    return row


def validate_entry(db, entry_id: str, actor: str, *,
                   revalidate_expired: bool = False):
    """draft -> validated; expired -> validated only with revalidate_expired=True."""
    row = _get_entry(db, entry_id)
    if row.status == "expired" and revalidate_expired:
        _append_audit(row, f"re-validated from expired by {actor} "
                           f"(previous expiry rationale: {row.expires_rationale!r})")
        row.expires_rationale = None
        row.expired_at = None
    elif row.status == "draft":
        _append_audit(row, f"validated by {actor}")
    else:
        raise KnowledgeTransitionError(
            f"cannot validate an entry in status {row.status!r} "
            "(only draft, or expired with an explicit revalidate_expired flag)")
    row.status = "validated"
    row.validated_by = actor
    row.validated_at = _now()
    db.commit()
    db.refresh(row)
    return row


def reject_entry(db, entry_id: str, actor: str):
    row = _get_entry(db, entry_id)
    if row.status != "draft":
        raise KnowledgeTransitionError(
            f"cannot reject an entry in status {row.status!r} (only draft)")
    row.status = "rejected"
    _append_audit(row, f"rejected by {actor}")
    db.commit()
    db.refresh(row)
    return row


def supersede_entry(db, entry_id: str, actor: str, replacement_id: str):
    """validated -> superseded; the replacement must exist and differ."""
    row = _get_entry(db, entry_id)
    if row.status != "validated":
        raise KnowledgeTransitionError(
            f"cannot supersede an entry in status {row.status!r} (only validated)")
    if not replacement_id:
        raise ValueError("replacement_id is required to supersede an entry")
    if replacement_id == entry_id:
        raise ValueError("an entry cannot supersede itself")
    try:
        _get_entry(db, replacement_id)
    except LookupError:
        raise ValueError(f"replacement entry {replacement_id!r} does not exist")
    row.status = "superseded"
    row.superseded_by_id = replacement_id
    _append_audit(row, f"superseded by entry {replacement_id} (action by {actor})")
    db.commit()
    db.refresh(row)
    return row


def expire_entry(db, entry_id: str, actor: str, rationale: str):
    """validated -> expired; rationale is required."""
    row = _get_entry(db, entry_id)
    if row.status != "validated":
        raise KnowledgeTransitionError(
            f"cannot expire an entry in status {row.status!r} (only validated)")
    if not (rationale or "").strip():
        raise ValueError("a rationale is required to expire an entry")
    row.status = "expired"
    row.expires_rationale = rationale.strip()
    row.expired_at = _now()
    _append_audit(row, f"expired by {actor}: {rationale.strip()}")
    db.commit()
    db.refresh(row)
    return row


def draft_from_run(db, model_run):
    """Template a draft lesson from a model run's archived artifacts (no LLM call).

    Evidence is auto-populated so the draft is grounded from birth. The origin
    model's lesson-gen aggregate is quoted for reviewers only, never routing."""
    from core.database import RoutingRun, RoutingTask, RoutingModelProfile, RunManifestRecord
    from src.routing_scoring import model_lesson_gen_by_task

    run = db.get(RoutingRun, model_run.run_id) if model_run.run_id else None
    task = db.get(RoutingTask, run.task_id) if run else None
    profile = (db.get(RoutingModelProfile, model_run.model_profile_id)
               if model_run.model_profile_id else None)
    model_label = profile.model if profile else (model_run.model_profile_id or "unknown-model")

    try:
        artifacts = json.loads(model_run.artifacts) if model_run.artifacts else {}
    except Exception:
        artifacts = {}
    if not isinstance(artifacts, dict):
        artifacts = {}
    try:
        scores = json.loads(model_run.scores) if model_run.scores else {}
    except Exception:
        scores = {}
    verification = scores.get("verification") if isinstance(scores, dict) else None
    if not isinstance(verification, dict):
        verification = None

    evidence: List[Dict[str, Any]] = [{"type": "model_run", "id": model_run.id}]
    if run:
        evidence.append({"type": "run", "id": run.id})
    if task:
        evidence.append({"type": "task", "id": task.id})
    manifest = None
    if run:
        manifest = (db.query(RunManifestRecord)
                    .filter(RunManifestRecord.run_id == run.id)
                    .order_by(RunManifestRecord.created_at.desc())
                    .first())
    if manifest:
        evidence.append({"type": "run_manifest", "id": manifest.id})
    for kind in ("response_text_path", "summary_path", "patch_path",
                 "prompt_path", "verification_path"):
        if artifacts.get(kind):
            evidence.append({"type": "artifact", "kind": kind, "path": artifacts[kind]})
    if verification is not None:
        evidence.append({
            "type": "verification",
            "model_run_id": model_run.id,
            "mode": verification.get("mode"),
            "passed": verification.get("passed"),
            "patch_accepted": verification.get("patch_accepted"),
        })

    lesson_gen_line = "Origin model lesson-gen score (advisory): not yet scored"
    if model_run.model_profile_id and task:
        try:
            rows = model_lesson_gen_by_task(
                db, model_profile_id=model_run.model_profile_id,
                task_type=task.task_type)
            if rows and rows[0].get("avg_lesson_gen_score") is not None:
                r = rows[0]
                lesson_gen_line = (
                    f"Origin model lesson-gen score (advisory): "
                    f"{r['avg_lesson_gen_score']} across "
                    f"{r['lesson_scored_runs']} scored run(s)")
        except Exception:  # aggregate failure must never block a draft
            logger.debug("lesson-gen aggregate lookup failed", exc_info=True)

    title = f"Lesson: {task.title}" if task else f"Lesson from model run {model_run.id}"
    outcome = ("completed" if model_run.completed else
               "errored" if model_run.errored else
               "rate-limited" if model_run.rate_limited else "incomplete")
    lines = [
        "## Context",
        f"- Task: {task.title if task else 'unknown'} "
        f"(type: {task.task_type if task else 'unknown'}, id: {task.id if task else 'unknown'})",
        f"- Objective: {task.objective if task else 'unknown'}",
        f"- Model: {model_label} (model run {model_run.id}, outcome: {outcome})",
        f"- {lesson_gen_line}",
        "",
        "## What happened",
        f"- Run summary: {run.summary if run and run.summary else '(none recorded)'}",
    ]
    if verification is not None:
        lines.append(
            f"- Verification: mode={verification.get('mode')}, "
            f"passed={verification.get('passed')}, "
            f"patch_accepted={verification.get('patch_accepted')}")
    else:
        lines.append("- Verification: none persisted for this model run")
    lines += [
        "",
        "## Artifacts",
    ]
    artifact_lines = [f"- {k}: {v}" for k, v in artifacts.items() if v]
    lines += artifact_lines or ["- (no archived artifacts recorded)"]
    lines += [
        "",
        "## Lesson (EDIT BEFORE VALIDATION)",
        "- <what should future runs in this area do differently, and why?>",
        "",
        "NOTE: this draft was assembled mechanically from the run's archived",
        "artifacts — no model wrote it. A human or a lesson-generator model",
        "must replace the Lesson section with the actual takeaway before this",
        "entry can be validated. Knowledge entries are advisory context only,",
        "never policy.",
    ]

    return create_draft(
        db,
        title=title,
        body="\n".join(lines),
        evidence=evidence,
        category=task.task_type if task else None,
        source_task_id=task.id if task else None,
        source_model_run_id=model_run.id,
        created_by=model_label,
    )


def retrieve_validated(db, *, category: Optional[str] = None,
                       tag: Optional[str] = None,
                       task_type: Optional[str] = None,
                       limit: int = 20) -> List[Dict[str, Any]]:
    """Validated entries only, newest first, each labelled advisory.

    The only sanctioned retrieval surface; decision code must never import it."""
    from core.database import KnowledgeBaseEntry, RoutingTask

    limit = max(1, min(int(limit), 200))
    q = db.query(KnowledgeBaseEntry).filter(KnowledgeBaseEntry.status == "validated")
    if category:
        q = q.filter(KnowledgeBaseEntry.category == category)
    if task_type:
        q = (q.join(RoutingTask, KnowledgeBaseEntry.source_task_id == RoutingTask.id)
             .filter(RoutingTask.task_type == task_type))
    rows = q.order_by(KnowledgeBaseEntry.validated_at.desc(),
                      KnowledgeBaseEntry.created_at.desc()).all()

    out = []
    for row in rows:
        entry = entry_to_dict(row)
        if tag and tag not in (entry.get("tags") or []):
            continue
        out.append({
            "advisory": True,
            "note": ADVISORY_NOTE,
            "entry": entry,
        })
        if len(out) >= limit:
            break
    return out
