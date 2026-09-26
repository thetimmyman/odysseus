
"""
Routing harness control surface: coordinator wrapper and audit archive,
routing/budget previews, versioned policy, model profiles, generated-test
registry and verification, escalation, break-glass overrides, reliability.

Every endpoint requires an admin cookie (no bearer or internal-tool loopback),
since these routes decide where code and sensitive context execute.
Break-glass endpoints additionally require security_admin.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Any, Dict, List, Optional

from core.database import (
    CoordinatorAudit,
    CoordinatorBenchmarkResult,
    CoordinatorBenchmarkRun,
    EmergencyOverride,
    GeneratedTest,
    KnowledgeBaseEntry,
    ModelEndpoint,
    RoutingModelProfile,
    RoutingModelRun,
    RoutingRun,
    RoutingTask,
    SessionLocal,
    WorkflowReliabilitySignal,
)
from core.middleware import require_security_admin
from src.auth_helpers import get_current_user
from src import routing_policy
from src.auth_helpers import require_admin_cookie
from src.routing_budget import (
    check_general_budget,
    check_premium_budget,
    load_budget_config,
    spend_summary,
)
from src.routing_context import build_context_bundle
from src.routing_coordinator import (
    GateContext,
    SCHEMA_VERSION,
    wrap_coordinator_output,
)
from src.routing_coordinator_client import CoordinatorClient
from src.routing_coordinator_decide import (
    ExternalProviderError,
    build_deterministic_route,
    generate_and_wrap_decision,
)
from src.routing_benchmark import (
    BenchmarkEndpointError,
    MAX_REPLAYS,
    default_replays,
    execute_benchmark,
)
from src.routing_engine import route_task
from src.routing_escalation import (
    build_emergency_override,
    EscalationContext,
    EscalationSignal,
    evaluate_escalation,
    Risk,
)
from src.routing_redaction import redact_text
from src.routing_sandbox import is_command_allowed
from src.routing_verification import TestAuthority, is_blocking_eligible, verify_model_run
from src.routing_reliability import (
    Confounders,
    compute_signal,
    ReliabilityInput,
)
from src.routing_knowledge import (
    KnowledgeTransitionError,
    create_draft,
    draft_from_run,
    entry_to_dict,
    expire_entry,
    reject_entry,
    retrieve_validated,
    supersede_entry,
    validate_entry,
)
from src.routing_task_io import task_kwargs_from_json
from src.secret_storage import hmac_sign

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/harness", tags=["routing-harness"])


class WrapRequest(BaseModel):
    task_id: str
    raw_coordinator_output: str
    # The data-policy exception must be granted per call, never assumed.
    remote_exception_approved: bool = False
    # None = compute from the live budget checks for this task.
    budget_ok: Optional[bool] = None
    backend_available: bool = True
    # Fail closed: an approval-requiring decision without this flag is rejected.
    approval_satisfied: bool = False
    sandbox_ok: bool = True


class DecideRequest(BaseModel):
    """Generate a coordinator decision for a stored task from the resident endpoint."""
    task_id: str
    remote_exception_approved: bool = False
    budget_ok: Optional[bool] = None
    backend_available: bool = True
    # Fail closed: an approval-requiring decision without this flag is diverted.
    approval_satisfied: bool = False
    sandbox_ok: bool = True


class BenchmarkRunRequest(BaseModel):
    """Run the coordinator benchmark against a registered ModelEndpoint.
    replays is capped server-side (fixtures*replays LLM calls). fixtures_path is
    not exposed over HTTP: an arbitrary path is a 500 and fan-out lever; the CLI
    takes paths."""
    endpoint_name: str
    replays: Optional[int] = None
    model: Optional[str] = None


class TaskRefRequest(BaseModel):
    """An inline task dict (never persisted) or an existing RoutingTask id."""
    task: Optional[Dict[str, Any]] = None
    task_id: Optional[str] = None


class PolicyPublishRequest(BaseModel):
    policy: Dict[str, Any]


class PolicyRollbackRequest(BaseModel):
    archive: str


class RegistryCreateRequest(BaseModel):
    id: str
    model: str
    roles: List[str] = Field(default_factory=list)
    model_endpoint_id: Optional[str] = None
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    input_cost_per_mtok: float = 0.0
    output_cost_per_mtok: float = 0.0
    is_free: bool = False
    is_premium: bool = False
    enabled: bool = True
    notes: Optional[str] = None


class RegistryPatchRequest(BaseModel):
    roles: Optional[List[str]] = None
    enabled: Optional[bool] = None
    input_cost_per_mtok: Optional[float] = None
    output_cost_per_mtok: Optional[float] = None
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    notes: Optional[str] = None
    is_free: Optional[bool] = None
    is_premium: Optional[bool] = None
    model_endpoint_id: Optional[str] = None


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


class GeneratedTestCreateRequest(BaseModel):
    task_id: str
    authority: str  # a src.routing_verification.TestAuthority value
    command: str    # must pass the sandbox allowlist (is_command_allowed)
    origin_model_run_id: Optional[str] = None
    notes: Optional[str] = None


class VerifyRequest(BaseModel):
    model_run_id: str
    allow_dirty: bool = False
    # None = task.verification_mode, else infer_mode(task).
    mode: Optional[str] = None


class ReliabilityRequest(BaseModel):
    subject_type: str
    subject_id: str
    period_start: str
    period_end: str
    normalized_verification_failure_rate: float
    lesson_review_participation_rate: float = 1.0
    avg_validated_lesson_quality: float = 0.0
    confounders: Dict[str, bool] = Field(default_factory=dict)


class KnowledgeCreateRequest(BaseModel):
    title: str
    body: str
    # Required non-empty (400): ids/paths/results grounding the lesson.
    evidence: List[Any] = Field(default_factory=list)
    category: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    source_task_id: Optional[str] = None
    source_model_run_id: Optional[str] = None
    created_by: str = "human"


class KnowledgeValidateRequest(BaseModel):
    # Re-validating an expired entry needs an explicit human decision.
    revalidate_expired: bool = False


class KnowledgeSupersedeRequest(BaseModel):
    replacement_id: str


class KnowledgeExpireRequest(BaseModel):
    rationale: str


class KnowledgeDraftFromRunRequest(BaseModel):
    model_run_id: str


def _now():
    return datetime.now(timezone.utc)


def _body_fields(model: BaseModel, exclude_unset: bool = False) -> dict:
    # Support pydantic v1 .dict() and v2 .model_dump().
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_unset=exclude_unset)
    return model.dict(exclude_unset=exclude_unset)


def _resolve_task(db, body: TaskRefRequest):
    """Resolve a preview body to an existing RoutingTask, or a transient
    (never added or committed) row built from an inline task dict."""
    if body.task_id:
        row = db.get(RoutingTask, body.task_id)
        if not row:
            raise HTTPException(404, f"no task with id {body.task_id!r}")
        return row
    if isinstance(body.task, dict):
        return RoutingTask(**task_kwargs_from_json(body.task))
    raise HTTPException(400, "provide either task (inline dict) or task_id")


# Tier-2 fallback route builder, shared with /coordinator/decide.
_deterministic_route = build_deterministic_route


def setup_routing_harness_routes():

    @router.post("/coordinator/wrap")
    def coordinator_wrap(req: WrapRequest, request: Request):
        """Deterministic wrapper around a coordinator decision."""
        require_admin_cookie(request)
        policy = routing_policy.load_policy()
        max_bytes = int(policy.get("rawOutputMaxBytes") or 262144)
        if len(req.raw_coordinator_output.encode("utf-8")) > max_bytes:
            raise HTTPException(413, f"raw_coordinator_output exceeds rawOutputMaxBytes ({max_bytes})")

        client = CoordinatorClient.from_policy(policy)
        repair_fn = client.repair_fn if client.is_llm_backed() else None

        db = SessionLocal()
        try:
            task = db.get(RoutingTask, req.task_id) if req.task_id else None

            budget_ok = req.budget_ok
            if budget_ok is None:
                # Same non-overridable check odysseus-run applies to every
                # candidate; no known task means nothing to charge yet.
                budget_ok = check_general_budget(db)["allowed"] if task is not None else True

            deterministic_fn = None
            if task is not None:
                def deterministic_fn(_task_id: str, _task=task, _db=db):
                    return _deterministic_route(_db, _task)

            gctx = GateContext(
                remote_exception_approved=req.remote_exception_approved,
                budget_ok=budget_ok,
                backend_available=req.backend_available,
                approval_satisfied=req.approval_satisfied,
                sandbox_ok=req.sandbox_ok,
                task_id=req.task_id,
            )
            result = wrap_coordinator_output(
                req.raw_coordinator_output, gctx,
                repair_fn=repair_fn, deterministic_fn=deterministic_fn,
            )

            # Redact before storage so a pasted credential never persists; the
            # HMAC covers the redacted text actually on disk.
            red, applied = redact_text(req.raw_coordinator_output)
            pv = routing_policy.policy_versions()
            audit = CoordinatorAudit(
                id=str(uuid.uuid4()),
                task_id=req.task_id,
                schema_version=SCHEMA_VERSION,
                raw_output=red,
                validation_errors=json.dumps(result.validationErrors),
                fallback_path=result.fallbackPath,
                applied_fallback=result.appliedFallback,
                audit_notes=json.dumps(result.auditNotes),
                parsed_ok=result.ok and result.decision is not None,
                policy_versions=json.dumps(pv),
                redaction_applied=applied,
                hmac=hmac_sign(red),
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
            "policyVersions": pv,
        }

    @router.get("/coordinator/audit")
    def coordinator_audit_list(request: Request, limit: int = 50, task_id: Optional[str] = None):
        require_admin_cookie(request)
        limit = max(1, min(int(limit), 500))
        db = SessionLocal()
        try:
            q = db.query(CoordinatorAudit)
            if task_id:
                q = q.filter(CoordinatorAudit.task_id == task_id)
            rows = q.order_by(CoordinatorAudit.created_at.desc(), CoordinatorAudit.id.desc()).limit(limit).all()
            return [{
                "id": r.id,
                "task_id": r.task_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "parsed_ok": r.parsed_ok,
                "fallback_path": r.fallback_path,
                "applied_fallback": r.applied_fallback,
                "schema_version": r.schema_version,
            } for r in rows]
        finally:
            db.close()

    @router.get("/coordinator/audit/{audit_id}")
    def coordinator_audit_get(audit_id: str, request: Request):
        require_admin_cookie(request)
        db = SessionLocal()
        try:
            r = db.get(CoordinatorAudit, audit_id)
            if not r:
                raise HTTPException(404, "audit row not found")
            return {
                "id": r.id,
                "task_id": r.task_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "schema_version": r.schema_version,
                "parsed_ok": r.parsed_ok,
                "fallback_path": r.fallback_path,
                "applied_fallback": r.applied_fallback,
                "raw_output": r.raw_output,
                "validation_errors": json.loads(r.validation_errors) if r.validation_errors else [],
                "audit_notes": json.loads(r.audit_notes) if r.audit_notes else [],
                "policy_versions": json.loads(r.policy_versions) if r.policy_versions else None,
                "hmac": r.hmac,
                "redaction_applied": r.redaction_applied,
            }
        finally:
            db.close()

    @router.post("/coordinator/decide")
    def coordinator_decide(req: DecideRequest, request: Request):
        """Generate a decision from the resident coordinator endpoint and run it
        through the same wrapper and audit archive as /coordinator/wrap. 400 when
        the provider is 'external'. Endpoint failures degrade to the safe tier and
        are audited, never a 500."""
        require_admin_cookie(request)
        policy = routing_policy.load_policy()
        client = CoordinatorClient.from_policy(policy)
        if not client.is_llm_backed():
            raise HTTPException(
                400,
                "coordinator provider is 'external'; POST the decision to "
                "/coordinator/wrap instead",
            )
        db = SessionLocal()
        try:
            task = db.get(RoutingTask, req.task_id)
            if not task:
                raise HTTPException(404, f"no task with id {req.task_id!r}")
            budget_ok = req.budget_ok
            if budget_ok is None:
                budget_ok = check_general_budget(db)["allowed"]
            try:
                out = generate_and_wrap_decision(
                    db, task, client,
                    remote_exception_approved=req.remote_exception_approved,
                    budget_ok=budget_ok,
                    backend_available=req.backend_available,
                    approval_satisfied=req.approval_satisfied,
                    sandbox_ok=req.sandbox_ok,
                )
            except ExternalProviderError as e:
                raise HTTPException(400, str(e))
            return out
        finally:
            db.close()

    def _benchmark_run_out(r: CoordinatorBenchmarkRun) -> dict:
        return {
            "id": r.id,
            "endpoint_name": r.endpoint_name,
            "model": r.model,
            "replays": r.replays,
            "fixtures_count": r.fixtures_count,
            "passed_all_gates": r.passed_all_gates,
            "gates": json.loads(r.gates) if r.gates else {},
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }

    @router.post("/coordinator/benchmark")
    def coordinator_benchmark_run(req: BenchmarkRunRequest, request: Request):
        """Replay the fixture suite against a ModelEndpoint, enforce hard gates and
        persist a CoordinatorBenchmarkRun. replays is capped at MAX_REPLAYS.
        Reports the verdict only; never flips coordinator.provider."""
        require_admin_cookie(request)
        policy = routing_policy.load_policy()
        replays = req.replays if req.replays is not None else default_replays(policy)
        replays = max(1, min(int(replays), MAX_REPLAYS))
        db = SessionLocal()
        try:
            try:
                summary = execute_benchmark(
                    db, req.endpoint_name, replays=replays,
                    fixtures_path=None, model=req.model,
                    base_policy=policy,
                )
            except BenchmarkEndpointError as e:
                raise HTTPException(400, str(e))
            return summary
        finally:
            db.close()

    @router.get("/coordinator/benchmark")
    def coordinator_benchmark_list(request: Request, limit: int = 50,
                                   endpoint_name: Optional[str] = None):
        require_admin_cookie(request)
        limit = max(1, min(int(limit), 500))
        db = SessionLocal()
        try:
            q = db.query(CoordinatorBenchmarkRun)
            if endpoint_name:
                q = q.filter(CoordinatorBenchmarkRun.endpoint_name == endpoint_name)
            rows = q.order_by(
                CoordinatorBenchmarkRun.created_at.desc(),
                CoordinatorBenchmarkRun.id.desc(),
            ).limit(limit).all()
            return [_benchmark_run_out(r) for r in rows]
        finally:
            db.close()

    @router.get("/coordinator/benchmark/{run_id}")
    def coordinator_benchmark_get(run_id: str, request: Request):
        require_admin_cookie(request)
        db = SessionLocal()
        try:
            r = db.get(CoordinatorBenchmarkRun, run_id)
            if not r:
                raise HTTPException(404, "benchmark run not found")
            results = (
                db.query(CoordinatorBenchmarkResult)
                .filter(CoordinatorBenchmarkResult.run_id == run_id)
                .order_by(CoordinatorBenchmarkResult.created_at, CoordinatorBenchmarkResult.id)
                .all()
            )
            out = _benchmark_run_out(r)
            out["per_dimension"] = json.loads(r.per_dimension) if r.per_dimension else {}
            out["policy_versions"] = json.loads(r.policy_versions) if r.policy_versions else None
            out["per_fixture"] = [{
                "fixture_id": x.fixture_id,
                "dimension": x.dimension,
                "replays": x.replays,
                "agreement": x.agreement,
                "detail": json.loads(x.detail) if x.detail else None,
            } for x in results]
            return out
        finally:
            db.close()

    @router.post("/route/preview")
    def route_preview(body: TaskRefRequest, request: Request):
        """Ranked candidates as `odysseus-route preview`, without persisting."""
        require_admin_cookie(request)
        db = SessionLocal()
        try:
            task = _resolve_task(db, body)
            bundle = build_context_bundle(task)
            decision = route_task(db, task, bundle)
            return {
                "task_id": task.id,
                "context_token_estimate": bundle["metadata"]["token_estimate"],
                "candidates": decision["candidates"],
                # Pass route_task's data-policy verdict through.
                "dataPolicy": decision["dataPolicy"],
            }
        finally:
            db.close()

    @router.post("/budget/preview")
    def budget_preview(body: TaskRefRequest, request: Request):
        """Budget check results plus current spend, as if this task ran now."""
        require_admin_cookie(request)
        db = SessionLocal()
        try:
            task = _resolve_task(db, body)
            general = check_general_budget(db)
            premium = check_premium_budget(db)
            return {
                "task_id": task.id,
                "allowed": general["allowed"],
                "general": general,
                "premium": premium,
                "premiumAllowance": {
                    "taskAllowsPremium": bool(task.allow_premium_models),
                    "budgetAllowsPremium": premium["allowed"],
                },
                "taskCapUsd": task.max_cost_usd,
                "spend": spend_summary(db),
                "policyVersions": routing_policy.policy_versions(),
            }
        finally:
            db.close()

    @router.get("/policy")
    def policy_get(request: Request):
        require_admin_cookie(request)
        return {
            "policy": routing_policy.load_policy(),
            "policyVersions": routing_policy.policy_versions(),
        }

    def _authorize_policy_candidate(request: Request, candidate: dict):
        """Shared authorization for publish and rollback, so they can't drift.
        Returns (actor, danger_changed).

        Changing a danger-zone value requires security_admin alone: stacking it
        with require_admin_cookie would be uncallable, since admins never hold
        security_admin. Safe-only changes need the admin cookie. Also enforces
        that coordinator.provider=endpoint names an enabled ModelEndpoint."""
        current = routing_policy.load_policy()
        danger_changed = routing_policy.danger_zone_changes(candidate, current)
        if danger_changed:
            require_security_admin(request)
            actor = get_current_user(request) or "security_admin"
        else:
            actor = require_admin_cookie(request) or "admin"

        # Else the coordinator would silently degrade to a missing endpoint.
        coord = candidate.get("coordinator") if isinstance(candidate, dict) else None
        if isinstance(coord, dict) and coord.get("provider") == "endpoint":
            name = coord.get("endpointName")
            if not name:
                raise HTTPException(
                    400, "coordinator.provider='endpoint' requires coordinator.endpointName")
            db = SessionLocal()
            try:
                ep = db.query(ModelEndpoint).filter(
                    ModelEndpoint.name == name,
                    ModelEndpoint.is_enabled == True,  # noqa: E712
                ).first()
            finally:
                db.close()
            if not ep:
                raise HTTPException(
                    400, f"coordinator.endpointName {name!r} does not resolve to an "
                         "enabled ModelEndpoint")
        return actor, danger_changed

    @router.post("/policy/publish")
    def policy_publish(body: PolicyPublishRequest, request: Request):
        actor, danger_changed = _authorize_policy_candidate(request, body.policy)
        try:
            stored = routing_policy.publish_policy(body.policy, actor=actor)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {
            "policy": stored,
            "policyVersions": routing_policy.policy_versions(),
            "dangerZoneChanged": danger_changed,
        }

    @router.get("/policy/versions")
    def policy_versions_list(request: Request):
        require_admin_cookie(request)
        return {
            "versions": routing_policy.list_policy_versions(),
            "policyVersions": routing_policy.policy_versions(),
        }

    @router.post("/policy/rollback")
    def policy_rollback(body: PolicyRollbackRequest, request: Request):
        # A rollback can re-introduce a danger-zone value, so gate on the
        # archive's content before applying, exactly like a publish.
        try:
            candidate = routing_policy.read_policy_version(body.archive)
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        actor, danger_changed = _authorize_policy_candidate(request, candidate)
        try:
            stored = routing_policy.rollback_policy(body.archive, actor=actor or "admin")
        except FileNotFoundError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {
            "policy": stored,
            "policyVersions": routing_policy.policy_versions(),
            "dangerZoneChanged": danger_changed,
        }

    def _profile_out(p: RoutingModelProfile, ep: Optional[ModelEndpoint]) -> dict:
        return {
            "id": p.id,
            "model": p.model,
            "roles": json.loads(p.roles) if p.roles else [],
            "context_window": p.context_window,
            "max_output_tokens": p.max_output_tokens,
            "input_cost_per_mtok": p.input_cost_per_mtok,
            "output_cost_per_mtok": p.output_cost_per_mtok,
            "is_free": p.is_free,
            "is_premium": p.is_premium,
            "enabled": p.enabled,
            "notes": p.notes,
            "endpoint": {"id": ep.id, "name": ep.name} if ep else None,
        }

    @router.get("/registry")
    def registry_list(request: Request):
        require_admin_cookie(request)
        db = SessionLocal()
        try:
            rows = (
                db.query(RoutingModelProfile, ModelEndpoint)
                .outerjoin(ModelEndpoint, RoutingModelProfile.model_endpoint_id == ModelEndpoint.id)
                .order_by(RoutingModelProfile.id)
                .all()
            )
            return [_profile_out(p, ep) for p, ep in rows]
        finally:
            db.close()

    @router.post("/registry")
    def registry_create(body: RegistryCreateRequest, request: Request):
        require_admin_cookie(request)
        db = SessionLocal()
        try:
            if db.get(RoutingModelProfile, body.id):
                raise HTTPException(409, f"profile id {body.id!r} already exists")
            fields = _body_fields(body)
            fields["roles"] = json.dumps(fields.get("roles") or [])
            row = RoutingModelProfile(**fields)
            db.add(row)
            db.commit()
            db.refresh(row)
            ep = db.get(ModelEndpoint, row.model_endpoint_id) if row.model_endpoint_id else None
            return _profile_out(row, ep)
        finally:
            db.close()

    @router.patch("/registry/{profile_id}")
    def registry_patch(profile_id: str, body: RegistryPatchRequest, request: Request):
        require_admin_cookie(request)
        db = SessionLocal()
        try:
            row = db.get(RoutingModelProfile, profile_id)
            if not row:
                raise HTTPException(404, "profile not found")
            updates = _body_fields(body, exclude_unset=True)
            if "roles" in updates and updates["roles"] is not None:
                updates["roles"] = json.dumps(updates["roles"])
            for field, value in updates.items():
                setattr(row, field, value)
            db.commit()
            db.refresh(row)
            ep = db.get(ModelEndpoint, row.model_endpoint_id) if row.model_endpoint_id else None
            return _profile_out(row, ep)
        finally:
            db.close()

    @router.delete("/registry/{profile_id}")
    def registry_delete(profile_id: str, request: Request):
        require_admin_cookie(request)
        db = SessionLocal()
        try:
            row = db.get(RoutingModelProfile, profile_id)
            if not row:
                raise HTTPException(404, "profile not found")
            # Historical scores join on the profile; disable instead of deleting.
            referenced = db.query(RoutingModelRun).filter(
                RoutingModelRun.model_profile_id == profile_id
            ).first()
            if referenced:
                raise HTTPException(400, "profile has recorded model runs — disable instead of delete")
            db.delete(row)
            db.commit()
            return {"deleted": profile_id}
        finally:
            db.close()

    @router.get("/budget/summary")
    def budget_summary(request: Request):
        """Per-run spend aggregates plus configured caps, for the dashboard."""
        require_admin_cookie(request)
        cfg = load_budget_config()
        now = datetime.utcnow()
        windows = {
            "daily": now - timedelta(hours=24),
            "weekly": now - timedelta(days=7),
            "monthly": now - timedelta(days=30),
        }
        db = SessionLocal()
        try:
            periods = {}
            for name, since in windows.items():
                rows = db.query(RoutingRun).filter(RoutingRun.created_at >= since).all()
                periods[name] = {
                    "spend_usd": round(sum(r.spend_total_usd or 0.0 for r in rows), 4),
                    "premium_spend_usd": round(sum(r.spend_premium_usd or 0.0 for r in rows), 4),
                    "cap_usd": cfg.get(f"{name}_max_usd"),
                    "premium_cap_usd": cfg.get(f"premium_{name}_max_usd"),
                    "runs": len(rows),
                }
            return {
                "periods": periods,
                "policyVersions": routing_policy.policy_versions(),
            }
        finally:
            db.close()

    # Exact error strings run_hard_gates() persists into validation_errors, so
    # policyViolationRate is derived from stored data.
    _GATE_ERRORS = (
        "premium_over_budget",
        "restricted_data_remote_blocked",
        "approval_gate_unsatisfied",
        "sandbox_constraint_violated",
    )
    _GATE_ERROR_PREFIXES = ("backend_unavailable:",)

    def _is_gate_error(err: str) -> bool:
        return err in _GATE_ERRORS or any(err.startswith(p) for p in _GATE_ERROR_PREFIXES)

    def _metric(numerator, denominator, note=None, scale=None):
        """{value, numerator, denominator, note?}; value is None on an empty denominator."""
        if denominator:
            value = numerator / denominator
            if scale is not None:
                value = round(value, scale)
        else:
            value = None
        out = {"value": value, "numerator": numerator, "denominator": denominator}
        if note:
            out["note"] = note
        return out

    @router.get("/observability")
    def observability(request: Request, days: int = 30):
        """Operational metrics over ?days=N (default 30, clamped 1..365), from
        persisted rows only. Unsupported metrics return value=null with a note."""
        require_admin_cookie(request)
        days = max(1, min(int(days), 365))
        # created_at columns are naive-UTC (datetime.utcnow defaults).
        since = datetime.utcnow() - timedelta(days=days)
        db = SessionLocal()
        try:
            model_runs = db.query(RoutingModelRun).filter(
                RoutingModelRun.created_at >= since).all()
            total_cost = 0.0
            accepted_patches = 0
            for mr in model_runs:
                total_cost += mr.cost_usd or 0.0
                try:
                    scores = json.loads(mr.scores) if mr.scores else {}
                except Exception:
                    scores = {}
                verification = scores.get("verification") if isinstance(scores, dict) else None
                if isinstance(verification, dict) and verification.get("patch_accepted"):
                    accepted_patches += 1
            cost_metric = _metric(
                round(total_cost, 4), accepted_patches, scale=4,
                note="total RoutingModelRun.cost_usd in window / count of model runs "
                     "whose persisted scores.verification.patch_accepted is true",
            )

            audits = db.query(CoordinatorAudit).filter(
                CoordinatorAudit.created_at >= since).all()
            total_audits = len(audits)
            parsed_ok = 0
            fell_back = 0
            gate_blocked = 0
            approval_required_accepted = 0
            approval_misses = 0
            raw_unparseable = 0
            for a in audits:
                if a.parsed_ok:
                    parsed_ok += 1
                if a.fallback_path and a.fallback_path != "none":
                    fell_back += 1
                try:
                    errs = json.loads(a.validation_errors) if a.validation_errors else []
                except Exception:
                    errs = []
                errs = [e for e in errs if isinstance(e, str)]
                if any(_is_gate_error(e) for e in errs):
                    gate_blocked += 1
                # approvalGateMissRate: only accepted decisions can have executed.
                # Rows whose redacted raw output no longer parses are excluded
                # (counted in the note), not guessed at.
                if a.parsed_ok and a.fallback_path in (None, "none", "repair"):
                    try:
                        raw = json.loads(a.raw_output) if a.raw_output else None
                    except Exception:
                        raw = None
                    if not isinstance(raw, dict):
                        raw_unparseable += 1
                        continue
                    appr = raw.get("approvalRecommendation") or {}
                    if isinstance(appr, dict) and appr.get("required") is True:
                        approval_required_accepted += 1
                        if "approval_gate_unsatisfied" in errs:
                            approval_misses += 1

            schema_metric = _metric(
                parsed_ok, total_audits, scale=3,
                note="CoordinatorAudit.parsed_ok (strict-schema-valid decisions) / all "
                     "archived coordinator decisions in window",
            )
            fallback_metric = _metric(
                fell_back, total_audits, scale=3,
                note='audit rows with fallback_path != "none" (repair | deterministic | '
                     "safe_scout) / all archived decisions in window",
            )
            policy_metric = _metric(
                gate_blocked, total_audits, scale=3,
                note="audit rows whose persisted validation_errors contain a "
                     "run_hard_gates() error (premium_over_budget, "
                     "restricted_data_remote_blocked, approval_gate_unsatisfied, "
                     "sandbox_constraint_violated, backend_unavailable:*) / all archived "
                     "decisions in window — i.e. syntactically-legal decisions that "
                     "recommended a policy-illegal route and were blocked",
            )
            approval_note = (
                "accepted decisions (parsed_ok, fallback_path none|repair) whose archived "
                "raw decision requires approval but whose audit trail records "
                "approval_gate_unsatisfied — structurally 0 because run_hard_gates diverts "
                "such decisions to fallback before execution; a nonzero value flags a gate "
                "bypass bug"
            )
            if raw_unparseable:
                approval_note += (f"; {raw_unparseable} accepted row(s) excluded — archived "
                                  "raw output not parseable as JSON (redaction/free text)")
            approval_metric = _metric(
                approval_misses, approval_required_accepted, scale=3, note=approval_note,
            )

            # flakyTestRate: only the latest run per model run is kept, with no
            # base commit, so flips can't be attributed to flakiness.
            flaky_metric = {
                "value": None, "numerator": None, "denominator": None,
                "note": "insufficient data model: scores.verification stores only the "
                        "latest verification per model run and no base commit, so "
                        "pass/fail flips across repeated runs of the same task cannot be "
                        "distinguished from patch differences or repo drift; returning "
                        "null rather than a fabricated rate",
            }

            return {
                "days": days,
                "windowStart": since.isoformat(),
                "metrics": {
                    "costPerSuccessfulPatchUsd": cost_metric,
                    "coordinatorSchemaValidityRate": schema_metric,
                    "coordinatorFallbackRate": fallback_metric,
                    "policyViolationRate": policy_metric,
                    "approvalGateMissRate": approval_metric,
                    "flakyTestRate": flaky_metric,
                },
                "policyVersions": routing_policy.policy_versions(),
            }
        finally:
            db.close()

    @router.post("/escalation/evaluate")
    def escalation_evaluate(req: EscalationRequest, request: Request):
        """Premium escalation gate policy."""
        require_admin_cookie(request)
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
        """Break-glass. security_admin is the sole gate (admins never hold it, so
        stacking require_admin_cookie could never pass). The requester named in
        the body may differ from the approver."""
        require_security_admin(request)
        approver = get_current_user(request)
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
        """Deactivate an override (status flip, never overwrite). security_admin only."""
        require_security_admin(request)
        actor = get_current_user(request)
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
        """List active, non-expired overrides. Viewable by admin or security_admin
        (disjoint roles), so the creator can see and revoke it."""
        try:
            require_admin_cookie(request)
        except HTTPException:
            require_security_admin(request)
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

    def _generated_test_out(t: GeneratedTest) -> dict:
        return {
            "id": t.id,
            "task_id": t.task_id,
            "authority": t.authority,
            "command": t.command,
            "origin_model_run_id": t.origin_model_run_id,
            "promoted": t.promoted,
            "promoted_by": t.promoted_by,
            "promoted_at": t.promoted_at.isoformat() if t.promoted_at else None,
            "notes": t.notes,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            # The only rows verification lets flip `passed`.
            "blocking_eligible": is_blocking_eligible(t),
        }

    @router.get("/tests")
    def generated_tests_list(request: Request, task_id: Optional[str] = None):
        require_admin_cookie(request)
        db = SessionLocal()
        try:
            q = db.query(GeneratedTest)
            if task_id:
                q = q.filter(GeneratedTest.task_id == task_id)
            rows = q.order_by(GeneratedTest.created_at, GeneratedTest.id).all()
            return [_generated_test_out(t) for t in rows]
        finally:
            db.close()

    @router.post("/tests")
    def generated_tests_create(body: GeneratedTestCreateRequest, request: Request):
        """Register a test. Generated rows start unpromoted (advisory). The command
        is allowlist-checked here and again at run time, so tightening fails closed."""
        require_admin_cookie(request)
        valid_authorities = {a.value for a in TestAuthority}
        if body.authority not in valid_authorities:
            raise HTTPException(
                400, f"invalid authority {body.authority!r} (allowed: "
                     f"{'|'.join(sorted(valid_authorities))})")
        if not is_command_allowed(body.command):
            raise HTTPException(
                400, f"command {body.command!r} is not allowed by the sandbox "
                     "command allowlist (policy sandbox.allowedCommands)")
        db = SessionLocal()
        try:
            if not db.get(RoutingTask, body.task_id):
                raise HTTPException(404, f"no task with id {body.task_id!r}")
            row = GeneratedTest(
                id=str(uuid.uuid4()),
                task_id=body.task_id,
                authority=body.authority,
                command=body.command,
                origin_model_run_id=body.origin_model_run_id,
                promoted=False,
                notes=body.notes,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _generated_test_out(row)
        finally:
            db.close()

    def _append_audit_note(row: GeneratedTest, entry: str) -> None:
        row.notes = f"{row.notes}\n{entry}" if row.notes else entry

    @router.post("/tests/{test_id}/promote")
    def generated_tests_promote(test_id: str, request: Request):
        """Human authority grant: a promoted test becomes blocking-eligible.
        Persisted and recorded in the notes trail."""
        actor = require_admin_cookie(request) or "admin"
        db = SessionLocal()
        try:
            row = db.get(GeneratedTest, test_id)
            if not row:
                raise HTTPException(404, "generated test not found")
            now = _now()
            row.promoted = True
            row.promoted_by = actor
            row.promoted_at = now
            _append_audit_note(row, f"[{now.isoformat()}] promoted by {actor}")
            db.commit()
            db.refresh(row)
            return _generated_test_out(row)
        finally:
            db.close()

    @router.post("/tests/{test_id}/demote")
    def generated_tests_demote(test_id: str, request: Request):
        """Revoke a promotion; promoted_by/at are cleared, the notes trail keeps history."""
        actor = require_admin_cookie(request) or "admin"
        db = SessionLocal()
        try:
            row = db.get(GeneratedTest, test_id)
            if not row:
                raise HTTPException(404, "generated test not found")
            row.promoted = False
            row.promoted_by = None
            row.promoted_at = None
            _append_audit_note(row, f"[{_now().isoformat()}] demoted by {actor}")
            db.commit()
            db.refresh(row)
            return _generated_test_out(row)
        finally:
            db.close()

    @router.post("/verify")
    def harness_verify(body: VerifyRequest, request: Request):
        """Run mode-aware verification for an archived model run.

        Verification runs docker on the host; inside the container it reports
        docker_unavailable as an infrastructure_error (passed stays False but is
        never scored as a broken patch)."""
        require_admin_cookie(request)
        db = SessionLocal()
        try:
            model_run = db.get(RoutingModelRun, body.model_run_id)
            if not model_run:
                raise HTTPException(404, f"no model run with id {body.model_run_id!r}")
            run = db.get(RoutingRun, model_run.run_id)
            task = db.get(RoutingTask, run.task_id) if run else None
            if not task:
                raise HTTPException(404, "could not resolve the model run's task (run/task deleted?)")
            try:
                result = verify_model_run(
                    db, task, model_run,
                    mode=body.mode, allow_dirty=body.allow_dirty,
                )
            except ValueError as e:
                raise HTTPException(400, str(e))
            except RuntimeError as e:
                # Caller error (e.g. dirty tree), not a verification verdict.
                raise HTTPException(409, str(e))
            return {"model_run_id": model_run.id, "verification": result}
        finally:
            db.close()

    @router.get("/model-runs/{model_run_id}/verification")
    def model_run_verification(model_run_id: str, request: Request):
        """Return the stored verification block without re-running it.
        404 if the model run is missing or never verified."""
        require_admin_cookie(request)
        db = SessionLocal()
        try:
            model_run = db.get(RoutingModelRun, model_run_id)
            if not model_run:
                raise HTTPException(404, f"no model run with id {model_run_id!r}")
            try:
                scores = json.loads(model_run.scores) if model_run.scores else {}
            except Exception:
                scores = {}
            verification = scores.get("verification") if isinstance(scores, dict) else None
            if not isinstance(verification, dict):
                raise HTTPException(
                    404, "no persisted verification for this model run — run "
                         "`odysseus-exec verify` (host) or POST /api/harness/verify first")
            run = db.get(RoutingRun, model_run.run_id)
            return {
                "model_run_id": model_run.id,
                "run_id": model_run.run_id,
                "task_id": run.task_id if run else None,
                "verification": verification,
            }
        finally:
            db.close()

    @router.post("/reliability/signal")
    def reliability_signal(req: ReliabilityRequest, request: Request):
        """Compute and persist an advisory review-readiness signal."""
        require_admin_cookie(request)
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

    # Lessons are advisory only: nothing here may gate or veto any routing or
    # verification decision (enforced by tests/test_routing_knowledge.py).
    @router.get("/knowledge")
    def knowledge_list(request: Request, status: Optional[str] = None,
                       category: Optional[str] = None, limit: int = 100):
        """List entries of any status for curation; drafts first (validation queue)."""
        require_admin_cookie(request)
        limit = max(1, min(int(limit), 500))
        db = SessionLocal()
        try:
            q = db.query(KnowledgeBaseEntry)
            if status:
                q = q.filter(KnowledgeBaseEntry.status == status)
            if category:
                q = q.filter(KnowledgeBaseEntry.category == category)
            rows = q.order_by(KnowledgeBaseEntry.created_at.desc(),
                              KnowledgeBaseEntry.id.desc()).limit(limit).all()
            # Drafts first, then the rest; stable sort keeps newest-first.
            rows.sort(key=lambda r: 0 if r.status == "draft" else 1)
            return [entry_to_dict(r) for r in rows]
        finally:
            db.close()

    @router.get("/knowledge/retrieve")
    def knowledge_retrieve(request: Request, category: Optional[str] = None,
                           tag: Optional[str] = None,
                           task_type: Optional[str] = None, limit: int = 20):
        """Advisory retrieval: validated entries only, labelled advisory. Never an
        input to gates or verification."""
        require_admin_cookie(request)
        db = SessionLocal()
        try:
            items = retrieve_validated(db, category=category, tag=tag,
                                       task_type=task_type, limit=limit)
            return {
                "advisory": True,
                "note": "knowledge entries are advisory context, never policy",
                "items": items,
            }
        finally:
            db.close()

    @router.post("/knowledge")
    def knowledge_create(body: KnowledgeCreateRequest, request: Request):
        """Create a draft lesson. Evidence is required (400)."""
        require_admin_cookie(request)
        db = SessionLocal()
        try:
            try:
                row = create_draft(
                    db, title=body.title, body=body.body,
                    evidence=body.evidence, category=body.category,
                    tags=body.tags or None,
                    source_task_id=body.source_task_id,
                    source_model_run_id=body.source_model_run_id,
                    created_by=body.created_by,
                )
            except ValueError as e:
                raise HTTPException(400, str(e))
            return entry_to_dict(row)
        finally:
            db.close()

    @router.post("/knowledge/draft-from-run")
    def knowledge_draft_from_run(body: KnowledgeDraftFromRunRequest, request: Request):
        """Assemble a draft lesson from a model run's artifacts (no LLM call),
        with evidence pre-filled. Edited by a human before validation."""
        require_admin_cookie(request)
        db = SessionLocal()
        try:
            model_run = db.get(RoutingModelRun, body.model_run_id)
            if not model_run:
                raise HTTPException(404, f"no model run with id {body.model_run_id!r}")
            try:
                row = draft_from_run(db, model_run)
            except ValueError as e:
                raise HTTPException(400, str(e))
            return entry_to_dict(row)
        finally:
            db.close()

    def _knowledge_transition(entry_id, fn, *args, **kwargs):
        """Shared 404/409/400 mapping for the lifecycle actions."""
        db = SessionLocal()
        try:
            try:
                row = fn(db, entry_id, *args, **kwargs)
            except LookupError as e:
                raise HTTPException(404, str(e))
            except KnowledgeTransitionError as e:
                raise HTTPException(409, str(e))
            except ValueError as e:
                raise HTTPException(400, str(e))
            return entry_to_dict(row)
        finally:
            db.close()

    @router.post("/knowledge/{entry_id}/validate")
    def knowledge_validate(entry_id: str, request: Request,
                           body: Optional[KnowledgeValidateRequest] = None):
        """draft -> validated. Re-validating an expired entry requires the
        revalidate_expired flag."""
        actor = require_admin_cookie(request) or "admin"
        reval = bool(body and body.revalidate_expired)
        return _knowledge_transition(entry_id, validate_entry, actor,
                                     revalidate_expired=reval)

    @router.post("/knowledge/{entry_id}/reject")
    def knowledge_reject(entry_id: str, request: Request):
        """draft -> rejected (terminal)."""
        actor = require_admin_cookie(request) or "admin"
        return _knowledge_transition(entry_id, reject_entry, actor)

    @router.post("/knowledge/{entry_id}/supersede")
    def knowledge_supersede(entry_id: str, body: KnowledgeSupersedeRequest,
                            request: Request):
        """validated -> superseded, with the replacement link (terminal)."""
        actor = require_admin_cookie(request) or "admin"
        return _knowledge_transition(entry_id, supersede_entry, actor,
                                     body.replacement_id)

    @router.post("/knowledge/{entry_id}/expire")
    def knowledge_expire(entry_id: str, body: KnowledgeExpireRequest,
                         request: Request):
        """validated -> expired; rationale required."""
        actor = require_admin_cookie(request) or "admin"
        return _knowledge_transition(entry_id, expire_entry, actor,
                                     body.rationale)

    return router
