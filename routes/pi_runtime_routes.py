"""Operator routes for delegated Pi runs: start, events, status, send, cancel,
resume, result. Nothing here lets a caller change routing policy, budgets or
governance. Admin-only, since it launches an agent that edits a worktree.
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from src import pi_config, pi_executions
from src.pi_runtime import WorktreeMismatch, get_pi_runtime

logger = logging.getLogger(__name__)


class StartBody(BaseModel):
    task: str
    worktree: str
    model: Optional[str] = None
    provider: Optional[str] = None
    constraints: Optional[List[str]] = None
    task_id: Optional[str] = None
    jira_ticket: Optional[str] = None
    odysseus_run_id: Optional[str] = None


class SendBody(BaseModel):
    message: str
    streaming_behavior: Optional[str] = None


class ResumeBody(BaseModel):
    message: Optional[str] = None


def _require_admin(request: Request) -> None:
    """Reject non-admin callers."""
    auth_manager = getattr(request.app.state, "auth_manager", None)
    if not auth_manager:
        return  # no auth configured: trusted localhost dev only
    user = getattr(request.state, "current_user", None)
    if user == "internal-tool":
        return
    if not user or user == "api":
        raise HTTPException(403, "Admin only")
    if not auth_manager.is_admin(user):
        raise HTTPException(403, "Admin only")


def _which(binary: str) -> Optional[str]:
    if not binary:
        return None
    for entry in (os.environ.get("PATH") or "").split(os.pathsep):
        candidate = os.path.join(entry, binary)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def setup_pi_runtime_routes() -> APIRouter:
    router = APIRouter(prefix="/api/pi", tags=["pi-runtime"])

    @router.get("/runtime")
    async def runtime_info(request: Request):
        """Runtime selection + local execution target (no secrets)."""
        _require_admin(request)
        provider, model_id = pi_config.resolve_model(None)
        binary = pi_config.pi_bin()
        return {
            "runtime": pi_config.execution_runtime(),
            "runtimes": [pi_config.RUNTIME_NATIVE, pi_config.RUNTIME_PI],
            "pi_bin": binary,
            "pi_bin_present": bool(os.path.exists(binary) or _which(binary)),
            "provider": provider,
            "model": model_id,
            "session_dir": pi_config.session_dir(),
            "models_config": pi_config.models_config_path(),
        }

    @router.post("/executions")
    async def start_execution(request: Request, body: StartBody):
        _require_admin(request)
        runtime = get_pi_runtime()
        try:
            record = await runtime.start(
                task=body.task,
                worktree=body.worktree,
                model=body.model,
                provider=body.provider,
                constraints=body.constraints,
                task_id=body.task_id,
                jira_ticket=body.jira_ticket,
                odysseus_run_id=body.odysseus_run_id,
            )
        except WorktreeMismatch as exc:
            # Fail closed: the assignment was not honored, so no task was sent.
            raise HTTPException(
                409,
                {"error": "worktree_mismatch", "reason": exc.reason,
                 "execution_id": exc.execution_id},
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return record

    @router.get("/executions")
    async def list_executions(request: Request, limit: int = Query(50, ge=1, le=500)):
        _require_admin(request)
        return {"executions": pi_executions.list_executions(limit=limit)}

    @router.get("/executions/{execution_id}")
    async def get_execution(request: Request, execution_id: str):
        _require_admin(request)
        status = get_pi_runtime().status(execution_id)
        if status.get("status") == "unknown":
            raise HTTPException(404, "Unknown execution")
        return status

    @router.get("/executions/{execution_id}/events")
    async def get_events(request: Request, execution_id: str,
                         since: int = Query(0, ge=0)):
        _require_admin(request)
        if pi_executions.get_execution(execution_id) is None:
            raise HTTPException(404, "Unknown execution")
        return {"events": get_pi_runtime().events(execution_id, since=since)}

    @router.get("/executions/{execution_id}/result")
    async def get_result(request: Request, execution_id: str):
        _require_admin(request)
        result = get_pi_runtime().result(execution_id)
        if result is None:
            raise HTTPException(404, "Unknown execution")
        return result

    @router.post("/executions/{execution_id}/send")
    async def send_message(request: Request, execution_id: str, body: SendBody):
        _require_admin(request)
        ok = await get_pi_runtime().send(execution_id, body.message, body.streaming_behavior)
        if not ok:
            raise HTTPException(409, "Execution is not running")
        return {"sent": True}

    @router.post("/executions/{execution_id}/cancel")
    async def cancel_execution(request: Request, execution_id: str,
                               reason: str = Query("operator cancel")):
        _require_admin(request)
        ok = await get_pi_runtime().cancel(execution_id, reason=reason)
        if not ok:
            raise HTTPException(404, "Unknown live execution")
        return {"cancelled": True}

    @router.post("/executions/{execution_id}/resume")
    async def resume_execution(request: Request, execution_id: str, body: ResumeBody):
        _require_admin(request)
        try:
            status = await get_pi_runtime().resume(execution_id, message=body.message)
        except WorktreeMismatch as exc:
            # A resume into a worktree other than the assigned one is refused.
            raise HTTPException(
                409,
                {"error": "worktree_mismatch", "reason": exc.reason,
                 "execution_id": exc.execution_id or execution_id},
            )
        if status is None:
            raise HTTPException(404, "Unknown execution")
        return status

    return router

