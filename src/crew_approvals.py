"""crew_approvals.py — the Oracle's seal (Argo Tier-1 approval subsystem).

This module is the human-oversight security boundary for the Argo agent-crew.
It owns three concerns and NOTHING else:

  (a) ``_redact(text, owner)`` — a PATTERN secret-redactor applied to every
      crew sink (SSE payloads, CrewApproval.action_args, Hermes bodies,
      Mnemosyne writes) BEFORE the value leaves the process. It masks
      secret-SHAPED substrings only (Bearer/ody_/sk-/anthropic.../api_key=)
      plus the owner's own resolved endpoint api_key literals — it does not
      mangle legitimate content.

  (b) ``_normalize_tool`` + ``needs_gate`` — the classifier that decides which
      tool calls must park behind a human approval. Default crew roles are
      offered an ENUMERATED read-only allowlist, so in read-only mode nothing
      gates; in write mode the full mutator set (plus bash/python) gates.

  (c) The RACE-FREE approval gate — a singleflight pattern copied from
      ``task_scheduler``: a module-level ``asyncio.Lock`` guarding the in-RAM
      Event/decision registries, with ``open_gate`` inserting the DB row AND
      registering the Event UNDER THE LOCK before the approval id is emitted,
      ``wait_for_approval`` re-checking the DB row before awaiting, and
      ``resolve_gate`` updating the row + signalling the Event under the same
      lock (409 if the row is already terminal). ``expire_gate`` is the
      try/finally-friendly cleanup the orchestrator calls on run exit.

DELIBERATELY kept OUT of ``tool_execution.py`` to avoid import cycles — other
modules import only the lightweight classifier (``needs_gate``/``_redact``).
It MUST NOT import ``crew_orchestrator`` (cycle); it may import from
``core.database``, ``src.tool_execution`` (for ``_ADMIN_TOOLS``),
``src.ai_interaction``/``endpoint_resolver`` (for endpoint keys), and stdlib.
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

# ---------------------------------------------------------------------------
# (a) Secret redactor — pattern + owner-endpoint-key masking
# ---------------------------------------------------------------------------
# A planted secret must appear in NO sink (SSE event, blackboard, Hermes body,
# CrewApproval.action_args). `_is_sensitive_path` only blocks file PATHS, not
# secret VALUES in command strings, so this value-level redactor is the only
# defence. It masks secret-SHAPED substrings only — it never touches legit
# content — plus the owner's resolved endpoint api_key literals.

_MASK = "[REDACTED]"

# Order matters: longer / more-specific shapes first so a Bearer token isn't
# half-masked by the bare token rule. Each pattern masks the SECRET portion.
_SECRET_PATTERNS: tuple[re.Pattern, ...] = (
    # Authorization: Bearer <token>  /  bare "Bearer xxxxx"
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"),
    # Odysseus API tokens: ody_<base64-ish>
    re.compile(r"\body_[A-Za-z0-9._\-]{8,}"),
    # OpenAI-style keys: sk-<rest>  (incl. sk-proj-, sk-ant-, etc.)
    re.compile(r"\bsk-[A-Za-z0-9._\-]{8,}"),
    # Anthropic keys: sk-ant-... already caught above; also bare anthropic... keys
    re.compile(r"(?i)\banthropic[A-Za-z0-9._\-]{8,}"),
    # --- vendor token shapes (fix #4): mask third-party secrets a write-mode
    #     worker might handle. Anchored on the vendor prefix so normal prose is
    #     left alone. ---
    # Groq: gsk_<rest>
    re.compile(r"\bgsk_[A-Za-z0-9._\-]{8,}"),
    # GitHub PAT (ghp_) / OAuth (gho_) / user-to-server (ghu_) /
    # server-to-server (ghs_) / refresh (ghr_) tokens.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    # Slack tokens: xoxb-/xoxa-/xoxp-/xoxr-/xoxs-<rest>
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{8,}"),
    # AWS access key id: AKIA + 16 uppercase-alnum.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # key=value forms: api_key=..., api-key: ..., authorization: ..., plus the
    # generic token/password/secret/access-key family (fix #4). Masks just the
    # VALUE after the delimiter (=, :, or "key: ") up to a whitespace / quote /
    # comma / ampersand boundary; the label is kept.
    re.compile(
        r"(?i)(\"?(?:api[_-]?key|authorization|auth[_-]?token|access[_-]?token|"
        r"access[_-]?key|secret|token|password|passwd|pwd)\"?\s*[:=]\s*\"?)"
        r"([^\s\"',&]{6,})"
    ),
)

# How many key/value matches use a 2-group (keep-key, mask-value) substitution.
_KV_PATTERN = _SECRET_PATTERNS[-1]


def _resolve_owner_endpoint_keys(owner) -> list[str]:
    """Return the literal ``api_key`` strings of the owner's ENABLED endpoints.

    Resolved through the same owner-scoped query the model resolver uses
    (``owner_filter`` includes NULL-owner / shared endpoints), so a shared
    endpoint's key is masked too. ``EncryptedText`` decrypts on read, so these
    are plaintext literals to scrub. Best-effort: any failure yields []."""
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
    # Mask longest-first so a key that is a prefix of another is handled right.
    return sorted(set(keys), key=len, reverse=True)


def _redact(text, owner) -> str:
    """Mask secret-shaped substrings (and the owner's endpoint api_keys) in a
    FULL serialized string. Safe on non-string / None — returns the value
    unchanged so callers can wrap any sink without guarding the type."""
    if not isinstance(text, str) or not text:
        return text
    out = text

    # 1. Owner's literal endpoint api_keys — exact-substring mask first, so a
    #    key that does not match a generic shape is still scrubbed.
    for key in _resolve_owner_endpoint_keys(owner):
        if key and key in out:
            out = out.replace(key, _MASK)

    # 2. Generic secret SHAPES.
    for pat in _SECRET_PATTERNS:
        if pat is _KV_PATTERN:
            # keep the "api_key=" label, mask only the value
            out = pat.sub(lambda m: m.group(1) + _MASK, out)
        else:
            out = pat.sub(_MASK, out)
    return out


# ---------------------------------------------------------------------------
# (b) Tool classifier — normalized name + read-only allowlist + needs_gate
# ---------------------------------------------------------------------------

# The ENUMERATED read-only allowlist. A default ("read/scratch") crew role is
# OFFERED exactly these; in read-only mode nothing here ever trips a gate.
# No globs (no `list_*`) — every name is spelled out on purpose.
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

# Native mutators that ALWAYS gate in write-mode (everything that writes,
# sends, spends, or changes scope). Email tools appear here in BOTH bare and
# `mcp__email__` forms because integrations execute as `mcp__email__<name>`.
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
    """Strip the ``mcp__<server>__`` prefix so email/integration side effects
    (which execute as ``mcp__email__<name>``) match their bare-name rules.
    Leaves a non-mcp tool, or a malformed mcp name, untouched."""
    if not isinstance(tool, str):
        return ""
    if tool.startswith("mcp__"):
        parts = tool.split("__", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]
    return tool


def _extract_action(content: str) -> Optional[str]:
    """Best-effort parse of the `action` field from a tool block's content.

    Tries JSON first (function-call style: {"action": "..."}); falls back to
    the first non-empty line (fenced manage_* parsers use line 0 = action).
    Returns the lowercased action, or None if it can't be determined."""
    if not isinstance(content, str) or not content.strip():
        return None
    body = content.strip()
    # JSON form
    if body[:1] in "{[":
        try:
            data = json.loads(body)
            if isinstance(data, dict):
                a = data.get("action")
                if isinstance(a, str) and a.strip():
                    return a.strip().lower()
        except (ValueError, TypeError):
            pass
    # Fenced/first-line form (e.g. _parse_manage_memory uses line 0 as action)
    first = body.split("\n", 1)[0].strip()
    if first and first[:1] not in "{[":
        return first.lower()
    return None


def needs_gate(tool: str, content: str, write_mode: bool, owner) -> bool:
    """Return True iff this tool call must park behind the Oracle's seal.

    Gating policy (admin crew):
      * ANY ``mcp__*`` tool (reaches external integrations) → gate.
      * Every name in the live ``_ADMIN_TOOLS`` set → gate.
      * ``api_call`` (arbitrary outbound HTTP; NOT in _ADMIN_TOOLS) → gate.
      * The full ``_MUTATOR_TOOLS`` set → gate.
      * In WRITE mode only, ``bash``/``python`` also gate.
      * Action-aware (``manage_research``/``manage_memory``): a pure-read
        action (list/search/read/open/view/get) does NOT gate.

    In READ-ONLY mode (``write_mode`` False) the worker is only offered the
    enumerated read-only allowlist, so nothing here should fire — but the
    classifier is defensive regardless of what name it is handed.
    """
    raw = tool if isinstance(tool, str) else ""
    norm = _normalize_tool(raw)

    # Action-aware tools: don't gate a pure-read action. Checked before the
    # blanket mutator rule so a manage_memory `list` is not gated.
    if norm in _ACTION_AWARE_TOOLS:
        action = _extract_action(content)
        if action in _READ_ACTIONS:
            return False
        return True

    # ANY MCP call on an admin crew reaches an external integration → gate.
    if raw.startswith("mcp__"):
        return True

    # The live admin-tool set (app_api, manage_*, serve_*, download_model …).
    try:
        from src.tool_execution import _ADMIN_TOOLS
        if raw in _ADMIN_TOOLS or norm in _ADMIN_TOOLS:
            return True
    except Exception:
        # Fall through to the static sets if the import is unavailable.
        pass

    # Arbitrary outbound HTTP — not in _ADMIN_TOOLS, but external-effecting.
    if norm == "api_call":
        return True

    # The full native mutator set (write/send/spend/scope).
    if raw in _MUTATOR_TOOLS or norm in _MUTATOR_TOOLS:
        return True

    # bash/python: only the path to git push / rm / curl-POST — gate in write
    # mode. (In read-only mode these aren't offered at all.)
    if norm in {"bash", "python"} and write_mode:
        return True

    return False


# ---------------------------------------------------------------------------
# (c) Race-free approval gate (singleflight pattern, copied from task_scheduler)
# ---------------------------------------------------------------------------
# One module-level lock guards BOTH registries. Per-run namespacing keeps
# cleanup O(run) and lets concurrent runs never collide on an approval id.

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
    """Park a side-effecting tool call: INSERT the CrewApproval row AND register
    its asyncio.Event UNDER THE LOCK, BEFORE the approval id is returned/emitted.

    Returns the approval_id. ``content`` is redacted before persist. The caller
    emits the approval id on the SSE stream AFTER this returns, so a ``/approve``
    that races in immediately can never hit a missing Event (it is already
    registered) and ``resolve_gate`` will find a 'pending' row to flip.
    """
    from core.database import SessionLocal, CrewApproval

    approval_id = uuid.uuid4().hex
    redacted = _redact(_to_text(content), owner)
    norm = _normalize_tool(tool)

    async with _gate_lock:
        # 1. Register the Event FIRST (in RAM) so a racing resolve/wait sees it.
        _EVENTS.setdefault(crew_run_id, {})[approval_id] = asyncio.Event()
        # 2. Persist the pending row (redacted args) atomically with the Event.
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
            # Roll the in-RAM registration back so we don't leak a phantom Event.
            _EVENTS.get(crew_run_id, {}).pop(approval_id, None)
            db.rollback()
            raise
        finally:
            db.close()

    # 3. Best-effort completion notification for phone/remote pickup. The SSE
    #    event is the source of truth, so a missing scheduler just skips this.
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
    """Block until the gate is resolved, then return the terminal decision:
    ``"approved"`` | ``"rejected"`` | ``"expired"``.

    Re-checks the DB row status BEFORE awaiting the Event — the row may have
    been decided (or expired by a sweep / a racing resolve) between
    ``open_gate`` and here, in which case there is no wakeup to wait for.
    """
    from core.database import SessionLocal, CrewApproval

    # Fast path: already decided?
    async with _gate_lock:
        dec = _DECISIONS.get(approval_id)
        if dec is not None:
            return dec
        # Pull the row status under the lock so we don't miss a concurrent
        # resolve that set the row but whose Event we then await.
        db = SessionLocal()
        try:
            row = db.query(CrewApproval).filter(CrewApproval.id == approval_id).first()
            if row is not None and row.status != "pending":
                # Row already terminal — mirror into _DECISIONS and return.
                result = row.status if row.status in ("approved", "rejected", "expired") else "expired"
                _DECISIONS.setdefault(approval_id, result)
                return result
        finally:
            db.close()
        event = _EVENTS.get(crew_run_id, {}).get(approval_id)

    if event is None:
        # No Event registered (lost / swept) — treat as expired, not a hang.
        return _DECISIONS.get(approval_id, "expired")

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        # Deadline — expire the gate so the row + Event become terminal.
        await expire_gate(approval_id, crew_run_id)
        return "expired"

    return _DECISIONS.get(approval_id, "expired")


async def resolve_gate(approval_id: str, decision: str, decided_by: str) -> str:
    """Atomically record a human decision and wake the waiter.

    ``decision`` is normalized to ``"approved"``/``"rejected"``. Returns a
    status string for the caller:
      * ``"approved"`` / ``"rejected"`` — applied.
      * ``"conflict"`` — the row is already terminal (caller should signal 409).
      * ``"not_found"`` — no such approval row.

    The DB update AND the Event set happen under the same lock so a decision
    lands exactly once and the waiter can never miss the wakeup.
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

        # Set the result + wake the waiter under the SAME lock.
        _DECISIONS[approval_id] = dec
        event = _EVENTS.get(crew_run_id, {}).get(approval_id)
        if event is not None:
            event.set()

    return dec


async def expire_gate(approval_id: str, crew_run_id: str) -> None:
    """Mark a single pending gate ``expired`` and wake its waiter. Idempotent —
    safe to call on an already-decided gate (it just no-ops the DB update)."""
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
    """try/finally cleanup for the orchestrator: ALWAYS resolve every pending
    Event for ``crew_run_id`` on run exit (success, error, cancel) and mark the
    matching pending CrewApproval rows ``expired`` — so a worker parked on
    ``wait_for_approval`` is never abandoned. Then drop the per-run registry."""
    from core.database import SessionLocal, CrewApproval

    async with _gate_lock:
        # 1. DB: expire every still-pending row for this run.
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
        # 2. RAM: wake + drop every Event for this run.
        events = _EVENTS.pop(crew_run_id, {})
        for aid, event in events.items():
            _DECISIONS.setdefault(aid, "expired")
            try:
                event.set()
            except Exception:
                pass


def sweep_orphaned_gates() -> int:
    """Optional callable: expire phantom ``pending`` rows whose Event no longer
    exists in RAM (e.g. after a restart). The server-start orphan sweep is
    already a DB migration from step 1; this is exposed only for convenience
    and does NOT duplicate it. Returns the count expired. Synchronous (no Event
    to signal — by definition there is none)."""
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
    """Serialize a parked tool block's content to a string for redaction +
    persistence. dict/list → JSON; everything else → str()."""
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    try:
        return json.dumps(content, default=str)
    except Exception:
        return str(content)
