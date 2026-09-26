"""Human-approval security boundary for the Argo agent-crew. Owns:

  (a) ``_redact(text, owner)``: masks secret-shaped substrings and the owner's
      endpoint api_keys in every crew sink before it leaves the process,
      without mangling legitimate content.
  (b) ``_normalize_tool`` + ``needs_gate``: which tool calls must wait for
      human approval. Read-only mode offers only a read allowlist; write mode
      gates all mutators plus bash/python.
  (c) A race-free approval gate: one asyncio.Lock guards the in-RAM registries;
      ``open_gate`` inserts the row and registers the Event under the lock
      before the id is emitted, ``wait_for_approval`` re-checks the row before
      awaiting, and ``resolve_gate`` updates and signals under the same lock.

Must not import ``crew_orchestrator`` (cycle).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Value-level redactor: `_is_sensitive_path` only blocks paths, so this is the
# only defence for secrets in command strings and other sinks.

_MASK = "[REDACTED]"

# Longer/more-specific shapes first so a Bearer token isn't half-masked.
_SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    # Authorization: Bearer <token>  /  bare "Bearer xxxxx"
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    # Odysseus API tokens: ody_<base64-ish>
    re.compile(r"\body_[A-Za-z0-9._\-]{8,}"),
    # OpenAI-style keys: sk-<rest>  (incl. sk-proj-, sk-ant-, etc.)
    re.compile(r"\bsk-[A-Za-z0-9._\-]{8,}"),
    # Anthropic keys: sk-ant-... already caught above; also bare anthropic... keys
    re.compile(r"(?i)\banthropic[A-Za-z0-9._\-]{8,}"),
    # Vendor tokens, anchored on the prefix so prose is left alone.
    # Groq: gsk_<rest>
    re.compile(r"\bgsk_[A-Za-z0-9._\-]{8,}"),
    # GitHub PAT (ghp_) / OAuth (gho_) / user-to-server (ghu_) /
    # server-to-server (ghs_) / refresh (ghr_) tokens.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    # Slack tokens: xoxb-/xoxa-/xoxp-/xoxr-/xoxs-<rest>
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}"),
    # AWS access key id: AKIA + 16 uppercase-alnum.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # key=value / key: value for api_key, authorization, token, password,
    # secret, access-key; masks only the value, keeps the label.
    re.compile(
        r"(?i)(\"?(?:api[_-]?key|authorization|auth[_-]?token|access[_-]?token|"
        r"access[_-]?key|secret|token|password|passwd|pwd)\"?\s*[:=]\s*\"?)"
        r"([^\s\"',&]{6,})"
    ),
)

# How many key/value matches use a 2-group (keep-key, mask-value) substitution.
_KV_PATTERN = _SECRET_PATTERNS[-1]


def _resolve_owner_endpoint_keys(owner) -> list[str]:
    """Plaintext ``api_key`` literals of the owner's enabled endpoints, including
    shared (NULL-owner) ones. Best-effort: any failure yields []."""
    if not owner:
        return []
    keys: list[str] = []
    try:
        from core.database import SessionLocal, ModelEndpoint
        from src.auth_helpers import owner_filter

        db = SessionLocal()
        try:
            q = db.query(ModelEndpoint).filter(ModelEndpoint.is_enabled == True)  # noqa: E712
            q = owner_filter(q, ModelEndpoint, owner)
            for ep in q.all():
                k = getattr(ep, "api_key", None)
                if k and isinstance(k, str) and len(k) >= 6:
                    keys.append(k)
        finally:
            db.close()
    except Exception as e:  # never let redaction failure crash a sink
        logger.debug("crew_approvals: endpoint-key resolve failed: %r", e)
    # Longest first so a key that prefixes another is fully masked.
    return sorted(set(keys), key=len, reverse=True)


def _redact(text, owner) -> str:
    """Mask secret-shaped substrings and owner endpoint keys. Non-strings pass
    through unchanged, so any sink can be wrapped."""
    if not isinstance(text, str) or not text:
        return text
    out = text

    # Exact owner keys first, so keys without a generic shape are still scrubbed.
    for key in _resolve_owner_endpoint_keys(owner):
        if key and key in out:
            out = out.replace(key, _MASK)

    for pat in _SECRET_PATTERNS:
        if pat is _KV_PATTERN:
            out = pat.sub(lambda m: m.group(1) + _MASK, out)
        else:
            out = pat.sub(_MASK, out)
    return out


# Read-only allowlist offered to default crew roles; never gates. Every name is
# spelled out, no globs.
READ_ONLY_ALLOWLIST: frozenset[str] = frozenset({
    "read_file",
    "search_files",
    "find_files",
    "list_dir",
    "get_project",
    "web_search",
    "web_fetch",
    "suggest_document",
})

# Mutators that always gate in write mode. Email tools appear bare and as
# `mcp__email__<name>`, the form integrations execute as.
_MUTATOR_TOOLS: frozenset[str] = frozenset({
    # filesystem / project mutation
    "write_file", "edit_file", "revert_file", "set_project",
    # documents
    "create_document", "update_document", "edit_document",
    "manage_documents",
    # sessions / inter-agent
    "create_session", "send_to_session", "chat_with_model", "manage_session",
    # higher-level orchestration surfaces
    "pipeline", "ui_control", "ask_teacher",
    # the manage_* family
    "manage_tasks", "manage_skills", "manage_notes", "manage_calendar",
    "manage_memory", "manage_endpoints", "manage_mcp", "manage_webhooks",
    "manage_tokens", "manage_settings",
    # media generation
    "generate_image", "edit_image",
    # research
    "trigger_research", "manage_research",
    # contacts / chat search
    "resolve_contact", "manage_contact", "search_chats",
    # vault (secret material)
    "vault_get", "vault_unlock",
    # app surface
    "app_api",
    # email — bare names …
    "send_email", "reply_to_email", "bulk_email",
    "archive_email", "delete_email", "mark_email_read",
    # … and the mcp__email__ remapped forms (normalized form also handled)
    "mcp__email__send_email", "mcp__email__reply_to_email",
    "mcp__email__bulk_email", "mcp__email__archive_email",
    "mcp__email__delete_email", "mcp__email__mark_email_read",
    # model lifecycle
    "download_model", "serve_model", "serve_preset",
    "stop_served_model", "cancel_download", "adopt_served_model",
})

# Tools whose JSON `content` carries an `action`: gate only on a WRITE action.
_ACTION_AWARE_TOOLS: frozenset[str] = frozenset({"manage_research", "manage_memory"})
# Pure-read actions for those tools — do NOT gate when the action is one of these.
_READ_ACTIONS: frozenset[str] = frozenset({
    "list", "search", "read", "open", "view", "get",
})


def _normalize_tool(tool: str) -> str:
    """Strip ``mcp__<server>__`` so integration calls match bare-name rules."""
    if not isinstance(tool, str):
        return ""
    if tool.startswith("mcp__"):
        parts = tool.split("__", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]
    return tool


def _extract_action(content: str) -> Optional[str]:
    """Lowercased `action` from a tool block's content (JSON, else first line), or None."""
    if not isinstance(content, str) or not content.strip():
        return None
    body = content.strip()
    if body[:1] in "{[":
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                a = data.get("action")
                if isinstance(a, str) and a.strip():
                    return a.strip().lower()
        except (ValueError, TypeError):
            pass
    first = body.split("\n", 1)[0].strip()
    if first and first[:1] not in "{[":
        return first.lower()
    return None


def needs_gate(tool: str, content: str, write_mode: bool, owner) -> bool:
    """True iff this tool call must wait for human approval.

      * any ``mcp__*`` tool, ``_ADMIN_TOOLS``, ``api_call``, ``_MUTATOR_TOOLS``;
      * ``bash``/``python`` in write mode only;
      * action-aware tools don't gate on a pure-read action.

    Defensive regardless of mode, though read-only mode offers only the allowlist.
    """
    raw = tool if isinstance(tool, str) else ""
    norm = _normalize_tool(raw)

    # Checked before the mutator rule so a manage_memory `list` isn't gated.
    if norm in _ACTION_AWARE_TOOLS:
        action = _extract_action(content)
        if action in _READ_ACTIONS:
            return False
        return True

    if raw.startswith("mcp__"):
        return True

    try:
        from src.tool_execution import _ADMIN_TOOLS
        if raw in _ADMIN_TOOLS or norm in _ADMIN_TOOLS:
            return True
    except Exception:
        pass

    # Arbitrary outbound HTTP: not in _ADMIN_TOOLS but external-effecting.
    if norm == "api_call":
        return True

    if raw in _MUTATOR_TOOLS or norm in _MUTATOR_TOOLS:
        return True

    if norm in {"bash", "python"} and write_mode:
        return True

    return False


# One lock guards both registries; per-run namespacing keeps cleanup O(run).

_gate_lock = asyncio.Lock()
# crew_run_id -> {approval_id: asyncio.Event}
_EVENTS: Dict[str, Dict[str, asyncio.Event]] = {}
# approval_id -> "approved" | "rejected" | "expired"
_DECISIONS: Dict[str, str] = {}


def _now() -> datetime:
    return datetime.utcnow()


async def open_gate(
    *,
    crew_run_id: str,
    owner: str,
    agent_id: Optional[str],
    tool: str,
    content,
    risk: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> str:
    """Park a side-effecting tool call: insert the row and register its Event
    under the lock before returning the approval_id, so a racing ``/approve``
    always finds both. ``content`` is redacted before persist.
    """
    from core.database import SessionLocal, CrewApproval

    approval_id = uuid.uuid4().hex
    redacted = _redact(_to_text(content), owner)
    norm = _normalize_tool(tool)

    async with _gate_lock:
        # Register the Event first so a racing resolve/wait sees it.
        _EVENTS.setdefault(crew_run_id, {})[approval_id] = asyncio.Event()
        db = SessionLocal()
        try:
            row = CrewApproval(
                id=approval_id,
                crew_run_id=crew_run_id,
                owner=owner,
                agent_id=agent_id,
                conversation_id=conversation_id,
                tool=norm,
                action_args=redacted,
                risk=risk,
                status="pending",
                created_at=_now(),
            )
            db.add(row)
            db.commit()
        except Exception:
            # Roll back the in-RAM registration so no phantom Event leaks.
            _EVENTS.get(crew_run_id, {}).pop(approval_id, None)
            db.rollback()
            raise
        finally:
            db.close()

    # Best-effort notification; the SSE event is the source of truth.
    try:
        from src.event_bus import get_task_scheduler
        sched = get_task_scheduler()
        if sched is not None:
            sched.add_notification(
                "Crew approval", "pending",
                task_id=approval_id, owner=owner,
                body=f"{norm} awaiting the Oracle's seal",
            )
    except Exception:
        pass

    return approval_id


async def wait_for_approval(approval_id: str, crew_run_id: str, timeout: float) -> str:
    """Block until resolved; return ``"approved"`` | ``"rejected"`` | ``"expired"``.

    Re-checks the DB row before awaiting, since it may already be decided.
    """
    from core.database import SessionLocal, CrewApproval

    async with _gate_lock:
        dec = _DECISIONS.get(approval_id)
        if dec is not None:
            return dec
        # Read under the lock so a concurrent resolve can't be missed.
        db = SessionLocal()
        try:
            row = db.query(CrewApproval).filter(CrewApproval.id == approval_id).first()
            if row is not None and row.status != "pending":
                result = row.status if row.status in ("approved", "rejected", "expired") else "expired"
                _DECISIONS.setdefault(approval_id, result)
                return result
        finally:
            db.close()
        event = _EVENTS.get(crew_run_id, {}).get(approval_id)

    if event is None:
        # No Event registered (lost/swept): expired, not a hang.
        return _DECISIONS.get(approval_id, "expired")

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        await expire_gate(approval_id, crew_run_id)
        return "expired"

    return _DECISIONS.get(approval_id, "expired")


async def resolve_gate(approval_id: str, decision: str, decided_by: str) -> str:
    """Atomically record a human decision and wake the waiter.

    Returns ``"approved"``/``"rejected"``, ``"conflict"`` (already terminal,
    409) or ``"not_found"``. The DB update and Event set share the lock, so the
    decision lands exactly once.
    """
    from core.database import SessionLocal, CrewApproval, CrewRun

    dec = "approved" if str(decision).strip().lower() in ("approve", "approved", "yes", "true") else "rejected"

    async with _gate_lock:
        db = SessionLocal()
        try:
            row = db.query(CrewApproval).filter(CrewApproval.id == approval_id).first()
            if row is None:
                return "not_found"
            if row.status != "pending":
                return "conflict"  # already terminal — caller returns 409
            # Only decidable while the parent run is still live.
            run = db.query(CrewRun).filter(CrewRun.id == row.crew_run_id).first()
            if run is not None and run.status not in ("running", "blocked"):
                return "conflict"
            row.status = dec
            row.decided_by = decided_by
            row.decided_at = _now()
            db.commit()
            crew_run_id = row.crew_run_id
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

        _DECISIONS[approval_id] = dec
        event = _EVENTS.get(crew_run_id, {}).get(approval_id)
        if event is not None:
            event.set()

    return dec


async def expire_gate(approval_id: str, crew_run_id: str) -> None:
    """Expire one pending gate and wake its waiter. Idempotent."""
    from core.database import SessionLocal, CrewApproval

    async with _gate_lock:
        db = SessionLocal()
        try:
            row = db.query(CrewApproval).filter(CrewApproval.id == approval_id).first()
            if row is not None and row.status == "pending":
                row.status = "expired"
                row.decided_at = _now()
                db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
        _DECISIONS.setdefault(approval_id, "expired")
        event = _EVENTS.get(crew_run_id, {}).get(approval_id)
        if event is not None:
            event.set()


async def expire_run_gates(crew_run_id: str) -> None:
    """Run-exit cleanup: wake every pending Event for the run and expire its
    pending rows so no worker is left parked, then drop the registry."""
    from core.database import SessionLocal, CrewApproval

    async with _gate_lock:
        db = SessionLocal()
        try:
            rows = (
                db.query(CrewApproval)
                .filter(CrewApproval.crew_run_id == crew_run_id, CrewApproval.status == "pending")
                .all()
            )
            for row in rows:
                row.status = "expired"
                row.decided_at = _now()
            if rows:
                db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
        events = _EVENTS.pop(crew_run_id, {})
        for aid, event in events.items():
            _DECISIONS.setdefault(aid, "expired")
            try:
                event.set()
            except Exception:
                pass


def sweep_orphaned_gates() -> int:
    """Expire pending rows whose Event no longer exists (e.g. after a restart).
    Returns the count. The startup orphan sweep already does this in the DB."""
    from core.database import SessionLocal, CrewApproval, CrewRun

    expired = 0
    db = SessionLocal()
    try:
        rows = db.query(CrewApproval).filter(CrewApproval.status == "pending").all()
        for row in rows:
            live = row.crew_run_id in _EVENTS and row.id in _EVENTS[row.crew_run_id]
            if live:
                continue
            run = db.query(CrewRun).filter(CrewRun.id == row.crew_run_id).first()
            if run is None or run.status not in ("running", "blocked"):
                row.status = "expired"
                row.decided_at = _now()
                expired += 1
        if expired:
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
    return expired


def _to_text(content) -> str:
    """dict/list → JSON, else str(), for redaction and persistence."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(content, default=str)
    except Exception:
        return str(content)
