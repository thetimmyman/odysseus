
"""
routes/routing_harness_routes.py — v0.5 harness control surface.

Exposes the coordinator deterministic wrapper, escalation evaluation,
emergency override (break-glass, security_admin-gated), and the workflow
reliability monitor. All persistence lands in core/database.py models.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from core.database import (
    CoordinatorAudit,
    EmergencyOverride,
    SessionLocal,
    WorkflowReliabilitySignal,
)
from core.middleware import require_security_admin
from src.auth_helpers import get_current_user
from src.routing_coordinator import (
    GateContext,
    SCHEMA_VERSION,
    wrap_coordinator_output,
)
from src.routing_escalation import (
    build_emergency_override,
    EscalationContext,
    EscalationSignal,
    evaluate_escalation,
    Risk,
)
from src.routing_reliability import (
    Confounders,
    compute_signal,
    ReliabilityInput,
    ReviewAction,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/harness", tags=["routing-harness"])


# ---------- request bodies ----------
class WrapRequest(BaseModel):
    task_id: str
    raw_coordinator_output: str
    data_policy_allows_remote: bool = True
    budget_ok: bool = True
    backend_available: bool = True
    approval_satisfied: bool = True
    sandbox_ok: bool = True


class EscalationRequest(BaseModel):
    task_id: str
    risk: str
    cheaper_attempts: int
    max_cheaper_attempts: int = 2
    signal: Dict[str, bool] = Field(default_factory=dict)
    est_premium_cost_usd: float = 0.0
    budget_remaining_usd: Optional[float] = None
    data_policy_allows_premium: bool = True
    approval_satisfied: bool = False


class EmergencyOverrideRequest(BaseModel):
    requested_by: str
    reason: str
    ttl_minutes: int = 60
    forced_backend: str = "human_only_emergency"


class ReliabilityRequest(BaseModel):
    subject_type: str
    subject_id: str
    period_start: str
    period_end: str
    normalized_verification_failure_rate: float
    lesson_review_participation_rate: float = 1.0
    avg_validated_lesson_quality: float = 0.0
    confounders: Dict[str, bool] = Field(default_factory=dict)


def _now():
    return datetime.now(timezone.utc)


def setup_routing_harness_routes():
    """No external deps needed; closure just groups the handlers."""

    @router.post("/coordinator/wrap")
    def coordinator_wrap(req: WrapRequest, request: Request):
        """Section 8: deterministic wrapper around a coordinator decision."""
        gctx = GateContext(
            data_policy_allows_remote=req.data_policy_allows_remote,
            budget_ok=req.budget_ok,
            backend_available=req.backend_available,
            approval_satisfied=req.approval_satisfied,
            sandbox_ok=req.sandbox_ok,
            task_id=req.task_id,
        )
        result = wrap_coordinator_output(req.raw_coordinator_output, gctx)
        # Archive raw output + outcome (Section 6/8 audit requirement).
        db = SessionLocal()
        try:
            audit = CoordinatorAudit(
                id=str(uuid.uuid4()),
                task_id=req.task_id,
                schema_version=SCHEMA_VERSION,
                raw_output=req.raw_coordinator_output,
                validation_errors=json.dumps(result.validationErrors),
                fallback_path=result.fallbackPath,
                applied_fallback=result.appliedFallback,
                audit_notes=json.dumps(result.auditNotes),
                parsed_ok=result.ok and result.decision is not None,
            )
            db.add(audit)
            db.commit()
            audit_id = audit.id
        finally:
            db.close()
        return {
            "ok": result.ok,
            "appliedFallback": result.appliedFallback,
            "fallbackPath": result.fallbackPath,
            "validationErrors": result.validationErrors,
            "auditNotes": result.auditNotes,
            "route": result.route,
            "auditId": audit_id,
        }

    @router.post("/escalation/evaluate")
    def escalation_evaluate(req: EscalationRequest, request: Request):
        """Section 11: premium escalation gate policy."""
        try:
            risk = Risk(req.risk)
        except ValueError:
            raise HTTPException(400, f"invalid risk: {req.risk}")
        signal = EscalationSignal(
            tests_still_fail=req.signal.get("tests_still_fail", False),
            safe_patching_failed=req.signal.get("safe_patching_failed", False),
            cheap_models_disagree=req.signal.get("cheap_models_disagree", False),
            best_cheap_run_below_threshold=req.signal.get("best_cheap_run_below_threshold", False),
            reviewer_requested_escalation=req.signal.get("reviewer_requested_escalation", False),
        )
        ctx = EscalationContext(
            task_id=req.task_id,
            risk=risk,
            cheaper_attempts=req.cheaper_attempts,
            max_cheaper_attempts=req.max_cheaper_attempts,
            signal=signal,
            est_premium_cost_usd=req.est_premium_cost_usd,
            budget_remaining_usd=req.budget_remaining_usd,
            data_policy_allows_premium=req.data_policy_allows_premium,
            approval_satisfied=req.approval_satisfied,
        )
        verdict = evaluate_escalation(ctx)
        return {
            "allowed": verdict.allowed,
            "requiresApproval": verdict.requires_approval,
            "reasons": verdict.reasons,
        }

    @router.post("/emergency/override")
    def emergency_override(req: EmergencyOverrideRequest, request: Request):
        """Section 14 break-glass. security_admin approval required.
        The approver is the authenticated security_admin; the requester is
        named in the body (may differ — e.g. an on-call engineer asking)."""
        require_security_admin(request)
        approver = getattr(request.state, "current_user", None)
        if not approver:
            raise HTTPException(403, "security_admin only")
        override = build_emergency_override(
            requested_by=req.requested_by,
            approved_by=approver,
            reason=req.reason,
            ttl_minutes=req.ttl_minutes,
        )
        db = SessionLocal()
        try:
            row = EmergencyOverride(
                id=str(uuid.uuid4()),
                requested_by=override.requested_by,
                approved_by=override.approved_by,
                reason=override.reason,
                forced_backend=override.forced_backend.value,
                expires_at=override.expires_at,
                active=True,
                post_mortem_required=True,
            )
            db.add(row)
            db.commit()
            rid = row.id
        finally:
            db.close()
        return {"id": rid, **override.to_dict()}

    @router.post("/emergency/{override_id}/revoke")
    def emergency_revoke(override_id: str, request: Request):
        """Deactivate an emergency override (status flip, never overwrite)."""
        require_security_admin(request)
        actor = getattr(request.state, "current_user", None)
        db = SessionLocal()
        try:
            row = db.query(EmergencyOverride).filter_by(id=override_id).first()
            if not row:
                raise HTTPException(404, "override not found")
            if not row.active:
                return {"id": override_id, "active": False, "alreadyInactive": True}
            row.active = False
            row.deactivated_at = _now()
            row.deactivated_by = actor
            db.commit()
        finally:
            db.close()
        return {"id": override_id, "active": False, "postMortemRequired": True}

    @router.get("/emergency/active")
    def emergency_active(request: Request):
        """List non-expired, still-active overrides (TTL enforced here too)."""
        now = _now()
        db = SessionLocal()
        try:
            rows = db.query(EmergencyOverride).filter_by(active=True).all()
            out = []
            for r in rows:
                expired = r.expires_at.replace(tzinfo=timezone.utc) <= now
                if expired:
                    continue
                out.append({
                    "id": r.id, "requestedBy": r.requested_by,
                    "approvedBy": r.approved_by, "reason": r.reason,
                    "forcedBackend": r.forced_backend,
                    "expiresAt": r.expires_at.isoformat(),
                    "postMortemRequired": r.post_mortem_required,
                })
        finally:
            db.close()
        return out

    @router.post("/reliability/signal")
    def reliability_signal(req: ReliabilityRequest, request: Request):
        """Section 13: compute + persist an advisory review-readiness signal."""
        conf = Confounders(
            flaky_tests_observed=req.confounders.get("flaky_tests_observed", False),
            high_risk_task_mix=req.confounders.get("high_risk_task_mix", False),
            model_failure_spike=req.confounders.get("model_failure_spike", False),
            legacy_hotspot_touched=req.confounders.get("legacy_hotspot_touched", False),
        )
        inp = ReliabilityInput(
            subject_type=req.subject_type,
            subject_id=req.subject_id,
            period_start=req.period_start,
            period_end=req.period_end,
            normalized_verification_failure_rate=req.normalized_verification_failure_rate,
            lesson_review_participation_rate=req.lesson_review_participation_rate,
            avg_validated_lesson_quality=req.avg_validated_lesson_quality,
            confounders=conf,
        )
        sig = compute_signal(inp)
        db = SessionLocal()
        try:
            row = WorkflowReliabilitySignal(
                id=str(uuid.uuid4()),
                subject_type=sig.subject_type,
                subject_id=sig.subject_id,
                period_start=sig.period_start,
                period_end=sig.period_end,
                normalized_verification_failure_rate=sig.normalized_verification_failure_rate,
                lesson_review_participation_rate=sig.lesson_review_participation_rate,
                avg_validated_lesson_quality=sig.avg_validated_lesson_quality,
                confounders=json.dumps(sig.confounders),
                recommended_action=sig.recommended_action,
            )
            db.add(row)
            db.commit()
            rid = row.id
        finally:
            db.close()
        return {"id": rid, **sig.to_dict()}

    return router
