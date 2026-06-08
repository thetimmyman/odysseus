"""Argo agent-crew routes — the network-facing surface for the crew MVP.

These endpoints define/list/update an Argo crew, launch a crew "voyage"
(``run_crew``), stream/reconnect/stop it, and drive the async human-approval
gate (the Oracle's seal).

SECURITY MODEL (multi-user, network-exposed app — Tier-1 admin-owner only):
  * OWNER-SCOPE EVERYTHING. Every row this router touches (Crew, CrewRun,
    CrewApproval) is loaded and its ``owner`` compared to
    ``effective_user(request)``. A cross-owner (or missing) row 404s — we
    NEVER 403-leak the existence of another owner's object.
  * agent_runs.subscribe(key) has NO owner check (it fans the run buffer to any
    subscriber, agent_runs.py:158). The crew_run_id IS that key. So EVERY
    endpoint that calls ``agent_runs.subscribe(crew_run_id)`` FIRST loads the
    CrewRun row and asserts ``row.owner == effective_user`` (404 otherwise) —
    otherwise another owner's full transcript (prompts, worker output, approval
    cards, Mnemosyne digests) would replay. crew_run_id is an unguessable uuid4.
  * MUTATING + APPROVAL routes require an ADMIN COOKIE session
    (``require_admin_cookie`` — rejects bearer/api/internal-tool, Decision A /
    hardening fix #4). Read-only listing/status/stream are owner-scoped only.
  * The per-run session is owner-stamped; a client-supplied session_id is
    ownership-verified (cross-owner 404) and rejected if it has owner=None
    (a legacy/shared session would otherwise resolve a project_root for the
    wrong principal — tool_execution._get_session_project_root only refuses
    cross-owner when BOTH owners are non-None).

Mirrors routes/git_routes.py for the setup_*_routes()->APIRouter shape, the
_require_admin/effective_user owner gate, and the _owner_root owner-compare;
mirrors routes/chat_routes.py for StreamingResponse(agent_runs.subscribe(...)).
"""

import json
import logging
import uuid
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src import agent_runs, bg_jobs
from src.auth_helpers import effective_user, require_admin_cookie

logger = logging.getLogger(__name__)


# --- request bodies ---------------------------------------------------------
class _RoleBody(BaseModel):
    name: str
    role_kind: Optional[str] = None        # "planner" | "worker" | "critic"
    model: Optional[str] = None
    endpoint_url: Optional[str] = None
    personality: Optional[str] = None
    enabled_tools: Optional[List[str]] = None   # per-role allowlist (built-in TOOL_TAGS subset)
    max_steps: Optional[int] = None


class _CrewBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    roles: Optional[List[_RoleBody]] = None
    max_agents: Optional[int] = None
    max_total_rounds: Optional[int] = None
    token_budget: Optional[int] = None
    wall_clock_s: Optional[int] = None


class _CrewUpdateBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    max_agents: Optional[int] = None
    max_total_rounds: Optional[int] = None
    token_budget: Optional[int] = None
    wall_clock_s: Optional[int] = None


class _RunBody(BaseModel):
    prompt: str
    crew_id: Optional[str] = None
    session_id: Optional[str] = None
    write_mode: bool = False


class _ApproveBody(BaseModel):
    approval_id: str
    decision: str                          # "approved" | "rejected" (or yes/no/true)


def setup_crew_routes() -> APIRouter:
    router = APIRouter(prefix="/api/crew", tags=["crew"])

    # ── owner-compare helpers ────────────────────────────────────────────────
    def _load_crew_owned(db, crew_id: str, owner: str):
        """Load a Crew the caller owns, or 404. Never reveals cross-owner
        existence (same 404 for missing and wrong-owner)."""
        from core.database import Crew
        row = db.query(Crew).filter(Crew.id == crew_id).first()
        if row is None or row.owner != owner:
            raise HTTPException(404, "Crew not found")
        return row

    def _load_run_owned(db, run_id: str, owner: str):
        """Load a CrewRun the caller owns, or 404. MUST be called before any
        agent_runs.subscribe(run_id) — subscribe has no owner check."""
        from core.database import CrewRun
        row = db.query(CrewRun).filter(CrewRun.id == run_id).first()
        if row is None or row.owner != owner:
            raise HTTPException(404, "Crew run not found")
        return row

    def _crew_dict(c) -> dict:
        return {
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "is_active": c.is_active,
            "max_agents": c.max_agents,
            "max_total_rounds": c.max_total_rounds,
            "token_budget": c.token_budget,
            "wall_clock_s": c.wall_clock_s,
        }

    # ── crew CRUD ────────────────────────────────────────────────────────────
    @router.post("")
    def create_crew(request: Request, body: _CrewBody):
        """Define a crew + its roles (as crew_id-stamped CrewMembers).
        Admin-cookie only (Tier-1 admin-owner) + owner-stamped."""
        require_admin_cookie(request)
        owner = effective_user(request)
        if not owner:
            raise HTTPException(403, "Authentication required")
        from core.database import SessionLocal, Crew, CrewMember

        crew_id = uuid.uuid4().hex
        db = SessionLocal()
        try:
            crew = Crew(
                id=crew_id,
                owner=owner,                         # security anchor
                name=(body.name or "The Argo"),
                description=body.description,
                is_active=True,
                max_agents=body.max_agents,
                max_total_rounds=body.max_total_rounds,
                token_budget=body.token_budget,
                wall_clock_s=body.wall_clock_s,
            )
            db.add(crew)

            roles_out = []
            for r in (body.roles or []):
                member = CrewMember(
                    id=uuid.uuid4().hex,
                    owner=owner,                     # same owner as the crew
                    name=r.name,
                    model=r.model,
                    endpoint_url=r.endpoint_url,
                    personality=r.personality,
                    enabled_tools=(json.dumps(r.enabled_tools)
                                   if r.enabled_tools is not None else None),
                    crew_id=crew_id,
                    role_kind=r.role_kind,
                    is_active=True,
                )
                db.add(member)
                roles_out.append({"id": member.id, "name": member.name,
                                  "role_kind": member.role_kind})
            db.commit()
            db.refresh(crew)
            out = _crew_dict(crew)
            out["roles"] = roles_out
            return out
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            logger.error(f"create_crew failed: {e}")
            raise HTTPException(500, "Failed to create crew")
        finally:
            db.close()

    @router.get("")
    def list_crews(request: Request):
        """List the caller's own crews (owner-scoped)."""
        owner = effective_user(request)
        if not owner:
            raise HTTPException(403, "Authentication required")
        from core.database import SessionLocal, Crew
        db = SessionLocal()
        try:
            rows = (db.query(Crew)
                    .filter(Crew.owner == owner)
                    .order_by(Crew.created_at.desc())
                    .all())
            return {"crews": [_crew_dict(c) for c in rows]}
        finally:
            db.close()

    @router.put("/{crew_id}")
    def update_crew(request: Request, crew_id: str, body: _CrewUpdateBody):
        """Update a crew. Admin-cookie only + owner-compare (404 cross-owner)."""
        require_admin_cookie(request)
        owner = effective_user(request)
        if not owner:
            raise HTTPException(403, "Authentication required")
        from core.database import SessionLocal
        db = SessionLocal()
        try:
            crew = _load_crew_owned(db, crew_id, owner)
            for field in ("name", "description", "is_active", "max_agents",
                          "max_total_rounds", "token_budget", "wall_clock_s"):
                val = getattr(body, field)
                if val is not None:
                    setattr(crew, field, val)
            db.commit()
            db.refresh(crew)
            return _crew_dict(crew)
        except HTTPException:
            db.rollback()
            raise
        except Exception as e:
            db.rollback()
            logger.error(f"update_crew failed: {e}")
            raise HTTPException(500, "Failed to update crew")
        finally:
            db.close()

    # ── run a voyage ─────────────────────────────────────────────────────────
    @router.post("/run")
    async def run_crew_route(request: Request, body: _RunBody):
        """Launch a crew voyage and stream the multiplexed SSE.

        Admin-cookie only. Mints an UNGUESSABLE crew_run_id, resolves an
        owner-stamped per-run session, owner-verifies the crew (if any), starts
        the run, then OWNER-COMPARES the CrewRun before subscribing."""
        require_admin_cookie(request)
        owner = effective_user(request)
        if not owner:
            raise HTTPException(403, "Authentication required")

        prompt = (body.prompt or "").strip()
        if not prompt:
            raise HTTPException(400, "prompt is required")

        from core.database import SessionLocal, Crew, CrewMember
        from src.crew_orchestrator import run_crew, CrewBudget

        # Owner-verify the crew (if one is named) + gather its roles.
        roles: List[dict] = []
        budget_kwargs: dict = {}
        crew_id = body.crew_id
        if crew_id:
            db = SessionLocal()
            try:
                crew = _load_crew_owned(db, crew_id, owner)
                if crew.max_agents is not None:
                    budget_kwargs["max_agents"] = crew.max_agents
                if crew.max_total_rounds is not None:
                    budget_kwargs["max_total_rounds"] = crew.max_total_rounds
                if crew.token_budget is not None:
                    budget_kwargs["token_budget"] = crew.token_budget
                if crew.wall_clock_s is not None:
                    budget_kwargs["wall_clock_s"] = crew.wall_clock_s
                members = (db.query(CrewMember)
                           .filter(CrewMember.crew_id == crew_id,
                                   CrewMember.owner == owner)
                           .order_by(CrewMember.sort_order)
                           .all())
                for m in members:
                    enabled = None
                    if m.enabled_tools:
                        try:
                            enabled = json.loads(m.enabled_tools)
                        except Exception:
                            enabled = None
                    roles.append({
                        "crew_member_id": m.id,
                        "name": m.name,
                        "role_kind": m.role_kind,
                        "model_spec": m.model,
                        "enabled_tools": enabled,
                    })
            finally:
                db.close()

        # Resolve / mint the owner-stamped per-run session that confines the run.
        session_id = _resolve_run_session(request, owner, body.session_id)

        # Unguessable parent-stream key.
        crew_run_id = uuid.uuid4().hex

        budget = CrewBudget(**budget_kwargs) if budget_kwargs else None

        # Persist the owner-stamped CrewRun row SYNCHRONOUSLY, before we hand
        # the generator to agent_runs.start(). run_crew()'s body (which would
        # otherwise be the first writer of this row) only executes inside the
        # scheduled task — which has NOT run yet at the point we owner-compare
        # below — so without this synchronous INSERT _load_run_owned() would
        # 404 deterministically. The orchestrator's _persist_crew_run() is a
        # GET-OR-UPDATE (no duplicate insert) and will continue from this row.
        from datetime import datetime as _dt
        db = SessionLocal()
        try:
            from core.database import CrewRun as _CrewRun
            db.add(_CrewRun(
                id=crew_run_id, owner=owner, prompt=prompt, status="running",
                session_id=session_id, started_at=_dt.utcnow(), crew_id=crew_id,
            ))
            db.commit()
        finally:
            db.close()

        merged = run_crew(
            owner,
            prompt,
            write_mode=bool(body.write_mode),
            crew_id=crew_id,
            roles=(roles or None),
            crew_run_id=crew_run_id,
            session_id=session_id,
            budget=budget,
            recursion_depth=1,
        )
        agent_runs.start(crew_run_id, merged)

        # Owner-compare the CrewRun row we persisted above BEFORE we subscribe
        # (subscribe has no owner check). The row now exists synchronously, so
        # this no longer 404s before streaming. 404 on any mismatch.
        db = SessionLocal()
        try:
            _load_run_owned(db, crew_run_id, owner)
        finally:
            db.close()

        return StreamingResponse(agent_runs.subscribe(crew_run_id),
                                 media_type="text/event-stream")

    @router.get("/run/{run_id}/stream")
    async def run_stream(request: Request, run_id: str):
        """Authenticated reconnect to a live/finished crew run.

        FIRST load the CrewRun and assert owner == effective_user (404), THEN
        subscribe — agent_runs.subscribe has NO owner check."""
        owner = effective_user(request)
        if not owner:
            raise HTTPException(403, "Authentication required")
        from core.database import SessionLocal
        db = SessionLocal()
        try:
            _load_run_owned(db, run_id, owner)   # 404 missing/cross-owner
        finally:
            db.close()
        return StreamingResponse(agent_runs.subscribe(run_id),
                                 media_type="text/event-stream")

    @router.post("/run/{run_id}/stop")
    def run_stop(request: Request, run_id: str):
        """Stop a crew run: owner-compare the CrewRun, then cancel the run and
        kill any background jobs spawned under its session.

        Admin-cookie only — stop is a MUTATING action, so it is desktop-admin
        gated for consistency with /approve (fix #5b), in addition to the
        owner-compare below."""
        require_admin_cookie(request)
        owner = effective_user(request)
        if not owner:
            raise HTTPException(403, "Authentication required")
        from core.database import SessionLocal
        db = SessionLocal()
        try:
            run = _load_run_owned(db, run_id, owner)
            session_id = run.session_id
        finally:
            db.close()
        stopped = agent_runs.stop(run_id)
        killed: List[str] = []
        if session_id:
            try:
                killed = bg_jobs.kill_for_session(session_id)
            except Exception as e:
                logger.warning(f"kill_for_session({session_id}) failed: {e}")
        return {"ok": True, "stopped": stopped, "killed_jobs": killed}

    @router.get("/run/{run_id}/approvals")
    def run_approvals(request: Request, run_id: str):
        """List pending approvals for a run (owner-scoped). Used for phone /
        reconnect pickup of the Oracle's seal."""
        owner = effective_user(request)
        if not owner:
            raise HTTPException(403, "Authentication required")
        from core.database import SessionLocal, CrewApproval
        db = SessionLocal()
        try:
            _load_run_owned(db, run_id, owner)   # 404 missing/cross-owner
            rows = (db.query(CrewApproval)
                    .filter(CrewApproval.crew_run_id == run_id,
                            CrewApproval.status == "pending")
                    .order_by(CrewApproval.created_at)
                    .all())
            return {"approvals": [{
                "id": a.id,
                "agent_id": a.agent_id,
                "tool": a.tool,
                "risk": a.risk,
                "action_args": a.action_args,   # already secret-redacted at INSERT
                "status": a.status,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            } for a in rows]}
        finally:
            db.close()

    @router.post("/approve")
    async def approve(request: Request, body: _ApproveBody):
        """Decide a parked approval (the Oracle's seal).

        Admin-cookie only (Decision A / fix #4 — bearer rejected). Owner-compare
        the approval's parent CrewRun (404 cross-owner), then resolve the gate
        atomically: 409 if already terminal, 404 if the row vanished."""
        admin_user = require_admin_cookie(request)
        owner = effective_user(request)
        if not owner:
            raise HTTPException(403, "Authentication required")

        approval_id = (body.approval_id or "").strip()
        if not approval_id:
            raise HTTPException(400, "approval_id is required")
        decision = (body.decision or "").strip()
        if not decision:
            raise HTTPException(400, "decision is required")

        from core.database import SessionLocal, CrewApproval, CrewRun
        from src.crew_approvals import resolve_gate

        # Owner-compare via the approval's parent CrewRun. 404 (never 403-leak)
        # on a missing approval, a missing run, or a cross-owner run.
        db = SessionLocal()
        try:
            appr = (db.query(CrewApproval)
                    .filter(CrewApproval.id == approval_id).first())
            if appr is None or appr.owner != owner:
                raise HTTPException(404, "Approval not found")
            run = (db.query(CrewRun)
                   .filter(CrewRun.id == appr.crew_run_id).first())
            if run is None or run.owner != owner:
                raise HTTPException(404, "Approval not found")
        finally:
            db.close()

        outcome = await resolve_gate(approval_id, decision, decided_by=admin_user)
        if outcome == "not_found":
            raise HTTPException(404, "Approval not found")
        if outcome == "conflict":
            raise HTTPException(409, "Approval is no longer decidable")
        return {"ok": True, "approval_id": approval_id, "decision": outcome}

    # ── voyage log (DB-backed history; the SSE buffer is evicted after 180s) ──
    @router.get("/runs")
    def list_runs(request: Request, limit: int = 30):
        """List the caller's recent crew voyages (owner-scoped, newest first).

        The voyage LOG. agent_runs evicts a finished run's in-memory replay
        buffer 180s after the last subscriber leaves (_EVICT_GRACE_S), so the
        history must come from the persisted CrewRun rows, never the SSE buffer."""
        owner = effective_user(request)
        if not owner:
            raise HTTPException(403, "Authentication required")
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 30
        from core.database import SessionLocal, CrewRun
        db = SessionLocal()
        try:
            rows = (db.query(CrewRun)
                    .filter(CrewRun.owner == owner)
                    .order_by(CrewRun.started_at.desc())
                    .limit(limit)
                    .all())
            out = []
            for r in rows:
                res = r.result or ""
                out.append({
                    "id": r.id,
                    "crew_id": r.crew_id,
                    "prompt": r.prompt,
                    "status": r.status,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    "tokens_used": r.tokens_used,
                    "result_preview": (res[:280] + ("…" if len(res) > 280 else "")) if res else None,
                })
            return {"runs": out}
        finally:
            db.close()

    @router.get("/run/{run_id}")
    def run_detail(request: Request, run_id: str):
        """Full stored detail for one voyage (owner-scoped). Renders a finished
        or evicted run statically — the live SSE buffer is gone after 180s.
        Joins the per-agent CrewAgentRun rows for the Argonaut result cards."""
        owner = effective_user(request)
        if not owner:
            raise HTTPException(403, "Authentication required")
        from core.database import SessionLocal, CrewAgentRun
        db = SessionLocal()
        try:
            run = _load_run_owned(db, run_id, owner)   # 404 missing/cross-owner
            plan = None
            if run.plan:
                try:
                    plan = json.loads(run.plan)
                except Exception:
                    plan = None
            agents = (db.query(CrewAgentRun)
                      .filter(CrewAgentRun.crew_run_id == run_id)
                      .order_by(CrewAgentRun.started_at)
                      .all())
            return {
                "id": run.id,
                "crew_id": run.crew_id,
                "prompt": run.prompt,
                "status": run.status,
                "started_at": run.started_at.isoformat() if run.started_at else None,
                "finished_at": run.finished_at.isoformat() if run.finished_at else None,
                "tokens_used": run.tokens_used,
                "result": run.result,
                "error": run.error,
                "plan": plan,
                "agents": [{
                    "agent_id": a.agent_id,
                    "role": a.role,
                    "subtask": a.subtask,
                    "status": a.status,
                    "rounds": a.rounds,
                    "tokens_used": a.tokens_used,
                    "model": a.model,
                    "result": a.result,
                    "error": a.error,
                } for a in agents],
            }
        finally:
            db.close()

    return router


# ── per-run session resolution (owner-stamped) ──────────────────────────────
def _resolve_run_session(request: Request, owner: str,
                         session_id: Optional[str]) -> str:
    """Return an owner-stamped session_id that will confine the crew run.

    If the client supplied one, ownership-verify it (cross-owner 404) AND reject
    a None-owner (legacy/shared) session — _get_session_project_root only refuses
    cross-owner when BOTH owners are non-None, so a None-owner target would
    otherwise resolve a project_root for the wrong principal. If none supplied,
    mint a fresh owner-stamped session."""
    from core.database import SessionLocal, Session as DbSession

    if session_id and str(session_id).strip():
        session_id = str(session_id).strip()
        db = SessionLocal()
        try:
            row = (db.query(DbSession.owner)
                   .filter(DbSession.id == session_id).first())
        finally:
            db.close()
        if row is None:
            raise HTTPException(404, "Session not found")
        if row.owner is None or row.owner != owner:
            # None-owner (shared/legacy) or cross-owner: refuse with 404 (no leak).
            raise HTTPException(404, "Session not found")
        return session_id

    # Mint a fresh, owner-stamped per-run session.
    from core.models import _session_manager as sm
    if sm is None:
        raise HTTPException(503, "Session manager unavailable")
    new_id = uuid.uuid4().hex
    try:
        from src.endpoint_resolver import resolve_endpoint
        url, model, _headers = resolve_endpoint("utility", owner=owner)
    except Exception:
        url, model = None, None
    sm.create_session(
        session_id=new_id,
        name="Argo crew run",
        endpoint_url=(url or ""),
        model=(model or ""),
        owner=owner,                       # security anchor
    )
    return new_id
