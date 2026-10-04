
"""
Coordinator plane: strict CoordinatorDecision validation and hard gates.

Unknown enum values or missing fields reject the decision rather than coercing
to a default. A rejected decision walks the fallback chain:

    repair (one retry, known-compatible schema versions only)
      -> deterministic router (routing_engine.route_task, injected)
        -> safe scout

Coordinator output never bypasses data policy, budget, sandbox, verification or
approval gates.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("odysseus.routing.coordinator")

SCHEMA_VERSION = "0.5"


class SchemaVersionError(ValueError):
    """Unknown schemaVersion; fails closed with no repair attempt."""


class DecisionValidationError(ValueError):
    """Strict field validation failed; carries every error for the audit archive."""

    def __init__(self, errors: List[str]):
        super().__init__("; ".join(errors))
        self.errors = list(errors)


class Domain(str, Enum):
    GENERAL_SWE = "general_swe"
    TACTICUS_ANALYTICS = "tacticus_analytics"
    INFRA = "infra"
    DATA_ANALYSIS = "data_analysis"
    DOCUMENTATION = "documentation"
    UNKNOWN = "unknown"


class TaskType(str, Enum):
    BUG_DEBUG = "bug_debug"
    CI_TRIAGE = "ci_triage"
    FEATURE_PLAN = "feature_plan"
    FEATURE_REVIEW = "feature_review"
    IMPLEMENTATION = "implementation"
    RELEASE_READINESS = "release_readiness"
    DIFF_REVIEW = "diff_review"
    BENCHMARK = "benchmark"
    UNKNOWN = "unknown"


class Risk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    RELEASE_BLOCKING = "release_blocking"


class DataSensitivity(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"
    SECRET = "secret"


class VerificationMode(str, Enum):
    """Only REFACTOR_EQUIVALENCE proves behaviour; ANALYSIS_ONLY never accepts a patch."""
    REGRESSION_GUARD = "regression_guard"
    BUG_FIX = "bug_fix"
    FEATURE_ADDITION = "feature_addition"
    REFACTOR_EQUIVALENCE = "refactor_equivalence"
    SECURITY_FIX = "security_fix"
    ANALYSIS_ONLY = "analysis_only"


class ExecutionBackend(str, Enum):
    ODYSSEUS_GENERAL_SWE = "odysseus_general_swe"
    OPENROUTER = "openrouter"
    LOCAL_FRAMEWORK_COORDINATOR_ONLY = "local_framework_coordinator_only"
    ABSIS_TACTICUS_JOB_QUEUE = "absis_tacticus_job_queue"
    MINI_PC_LLM_INFERENCE_WORKER = "mini_pc_llm_inference_worker"
    MINI_PC_ORACLE_RUNNER = "mini_pc_oracle_runner"
    HUMAN_ONLY = "human_only"
    HUMAN_ONLY_EMERGENCY = "human_only_emergency"


# Backends whose execution leaves the local machine. Restricted/secret data
# routed here is blocked unless an explicit, recorded policy exception exists.
REMOTE_BACKENDS = (
    ExecutionBackend.OPENROUTER,
    ExecutionBackend.ABSIS_TACTICUS_JOB_QUEUE,
    ExecutionBackend.MINI_PC_LLM_INFERENCE_WORKER,
    ExecutionBackend.MINI_PC_ORACLE_RUNNER,
)


class ModelRole(str, Enum):
    SCOUT = "scout"
    PLANNER = "planner"
    REVIEWER = "reviewer"
    IMPLEMENTER = "implementer"
    DEBUGGER = "debugger"
    ESCALATION = "escalation"


APPROVAL_LEVELS = ("none", "reviewer", "admin", "security_admin")


@dataclass
class Classification:
    domain: Domain
    taskType: TaskType
    risk: Risk
    dataSensitivity: DataSensitivity
    verificationMode: VerificationMode


@dataclass
class ContextRequest:
    sources: List[str] = field(default_factory=list)
    includeTests: bool = True
    includeLogs: bool = False
    maxUntrustedTokens: int = 256


@dataclass
class RouteRecommendation:
    backend: ExecutionBackend
    modelRoleChain: List[Dict[str, Any]] = field(default_factory=list)
    allowPremium: bool = False


@dataclass
class BudgetRecommendation:
    maxCostUsd: Optional[float] = None
    preferFree: bool = True


@dataclass
class ApprovalRecommendation:
    required: bool = False
    level: str = "none"  # none | reviewer | admin | security_admin


@dataclass
class CoordinatorConfidence:
    score: float = 0.0
    basis: str = "metadata"
    # Confidence is metadata only, never a verification layer.


@dataclass
class CoordinatorDecision:
    schemaVersion: str
    taskId: str
    classification: Classification
    contextRequest: ContextRequest
    routeRecommendation: RouteRecommendation
    budgetRecommendation: BudgetRecommendation
    approvalRecommendation: ApprovalRecommendation
    confidence: CoordinatorConfidence
    rationale: List[str]


@dataclass
class WrapperResult:
    ok: bool
    decision: Optional[CoordinatorDecision]
    route: Optional[Dict[str, Any]]
    appliedFallback: bool
    fallbackPath: str  # none | repair | deterministic | safe_scout
    validationErrors: List[str]
    auditNotes: List[str]
    rawOutput: Optional[str]


def _enum_strict(value: Any, enum_cls, field_name: str, errors: List[str]):
    """Strict enum coercion: unknown or missing values are errors, returning None."""
    if value is None:
        errors.append(f"missing_required_field:{field_name}")
        return None
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        allowed = "|".join(e.value for e in enum_cls)
        errors.append(f"invalid_enum_value:{field_name}={value!r} (allowed: {allowed})")
        return None


def _bool_strict(value: Any, default: bool, field_name: str, errors: List[str]) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    errors.append(f"invalid_type:{field_name} must be a boolean, got {type(value).__name__}")
    return default


def parse_decision(raw: Dict[str, Any]) -> CoordinatorDecision:
    """Build a typed CoordinatorDecision from parsed JSON, strictly.

    Optional sections may be omitted, but when present they are type-checked,
    so a typo'd enum still fails closed.
    """
    if not isinstance(raw, dict):
        raise DecisionValidationError(["decision_not_an_object"])

    sv = raw.get("schemaVersion")
    if sv != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"Unsupported CoordinatorDecision.schemaVersion={sv!r}; expected {SCHEMA_VERSION!r}"
        )

    errors: List[str] = []

    task_id = raw.get("taskId")
    if not task_id or not isinstance(task_id, str):
        errors.append("missing_required_field:taskId")
        task_id = ""

    c = raw.get("classification")
    if not isinstance(c, dict):
        errors.append("missing_required_field:classification")
        c = {}
    domain = _enum_strict(c.get("domain"), Domain, "classification.domain", errors)
    task_type = _enum_strict(c.get("taskType"), TaskType, "classification.taskType", errors)
    risk = _enum_strict(c.get("risk"), Risk, "classification.risk", errors)
    sensitivity = _enum_strict(c.get("dataSensitivity"), DataSensitivity,
                               "classification.dataSensitivity", errors)
    ver_mode = _enum_strict(c.get("verificationMode"), VerificationMode,
                            "classification.verificationMode", errors)

    route = raw.get("routeRecommendation")
    if not isinstance(route, dict):
        errors.append("missing_required_field:routeRecommendation")
        route = {}
    backend = _enum_strict(route.get("backend"), ExecutionBackend,
                           "routeRecommendation.backend", errors)
    chain_in = route.get("modelRoleChain", [])
    chain: List[Dict[str, Any]] = []
    if not isinstance(chain_in, list):
        errors.append("invalid_type:routeRecommendation.modelRoleChain must be a list")
    else:
        for i, item in enumerate(chain_in):
            if not isinstance(item, dict):
                errors.append(f"invalid_type:modelRoleChain[{i}] must be an object")
                continue
            role = _enum_strict(item.get("role"), ModelRole, f"modelRoleChain[{i}].role", errors)
            reason = item.get("reason")
            if not reason or not isinstance(reason, str):
                errors.append(f"missing_required_field:modelRoleChain[{i}].reason")
            entry: Dict[str, Any] = {"role": role.value if role else None, "reason": reason}
            if item.get("modelPreference") is not None:
                if isinstance(item["modelPreference"], str):
                    entry["modelPreference"] = item["modelPreference"]
                else:
                    errors.append(f"invalid_type:modelRoleChain[{i}].modelPreference must be a string")
            unknown = set(item) - {"role", "reason", "modelPreference"}
            if unknown:
                errors.append(f"unknown_field:modelRoleChain[{i}].{sorted(unknown)[0]}")
            chain.append(entry)
    allow_premium = _bool_strict(route.get("allowPremium"), False,
                                 "routeRecommendation.allowPremium", errors)

    ctx = raw.get("contextRequest") or {}
    if not isinstance(ctx, dict):
        errors.append("invalid_type:contextRequest must be an object")
        ctx = {}
    sources = ctx.get("sources", [])
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        errors.append("invalid_type:contextRequest.sources must be a list of strings")
        sources = []
    include_tests = _bool_strict(ctx.get("includeTests"), True, "contextRequest.includeTests", errors)
    include_logs = _bool_strict(ctx.get("includeLogs"), False, "contextRequest.includeLogs", errors)
    max_untrusted = ctx.get("maxUntrustedTokens", 256)
    if not isinstance(max_untrusted, int) or isinstance(max_untrusted, bool) or not (0 <= max_untrusted <= 8192):
        errors.append("invalid_value:contextRequest.maxUntrustedTokens must be an int in [0, 8192]")
        max_untrusted = 256

    bud = raw.get("budgetRecommendation") or {}
    if not isinstance(bud, dict):
        errors.append("invalid_type:budgetRecommendation must be an object")
        bud = {}
    max_cost = bud.get("maxCostUsd")
    if max_cost is not None:
        if isinstance(max_cost, (int, float)) and not isinstance(max_cost, bool) and max_cost >= 0:
            max_cost = float(max_cost)
        else:
            errors.append("invalid_value:budgetRecommendation.maxCostUsd must be a non-negative number")
            max_cost = None
    prefer_free = _bool_strict(bud.get("preferFree"), True, "budgetRecommendation.preferFree", errors)
    unknown_bud = set(bud) - {"maxCostUsd", "preferFree"}
    if unknown_bud:
        errors.append(f"unknown_field:budgetRecommendation.{sorted(unknown_bud)[0]}")

    appr = raw.get("approvalRecommendation") or {}
    if not isinstance(appr, dict):
        errors.append("invalid_type:approvalRecommendation must be an object")
        appr = {}
    appr_required = _bool_strict(appr.get("required"), False, "approvalRecommendation.required", errors)
    appr_level = appr.get("level", "none")
    if appr_level not in APPROVAL_LEVELS:
        errors.append(f"invalid_enum_value:approvalRecommendation.level={appr_level!r} "
                      f"(allowed: {'|'.join(APPROVAL_LEVELS)})")
        appr_level = "none"
    unknown_appr = set(appr) - {"required", "level"}
    if unknown_appr:
        errors.append(f"unknown_field:approvalRecommendation.{sorted(unknown_appr)[0]}")

    conf = raw.get("confidence") or {}
    if not isinstance(conf, dict):
        errors.append("invalid_type:confidence must be an object")
        conf = {}
    score = conf.get("score", 0.0)
    if not isinstance(score, (int, float)) or isinstance(score, bool) or not (0.0 <= float(score) <= 1.0):
        errors.append("invalid_value:confidence.score must be a number in [0, 1]")
        score = 0.0

    rationale = raw.get("rationale", [])
    if not isinstance(rationale, list) or not all(isinstance(r, str) for r in rationale):
        errors.append("invalid_type:rationale must be a list of strings")
        rationale = []

    if errors:
        raise DecisionValidationError(errors)

    return CoordinatorDecision(
        schemaVersion=SCHEMA_VERSION,
        taskId=task_id,
        classification=Classification(
            domain=domain, taskType=task_type, risk=risk,
            dataSensitivity=sensitivity, verificationMode=ver_mode,
        ),
        contextRequest=ContextRequest(
            sources=list(sources), includeTests=include_tests,
            includeLogs=include_logs, maxUntrustedTokens=max_untrusted,
        ),
        routeRecommendation=RouteRecommendation(
            backend=backend, modelRoleChain=chain, allowPremium=allow_premium,
        ),
        budgetRecommendation=BudgetRecommendation(maxCostUsd=max_cost, preferFree=prefer_free),
        approvalRecommendation=ApprovalRecommendation(required=appr_required, level=appr_level),
        confidence=CoordinatorConfidence(score=float(score), basis=str(conf.get("basis", "metadata"))),
        rationale=list(rationale),
    )


@dataclass
class GateContext:
    """Gate inputs; restricted/secret data stays local unless
    `remote_exception_approved` is explicitly granted."""
    remote_exception_approved: bool = False
    budget_ok: bool = True
    backend_available: bool = True
    approval_satisfied: bool = True
    sandbox_ok: bool = True
    task_id: str = ""


def run_hard_gates(decision: CoordinatorDecision, gctx: GateContext) -> List[str]:
    errors: List[str] = []
    r = decision.routeRecommendation
    if not gctx.backend_available:
        errors.append(f"backend_unavailable:{r.backend.value}")
    if r.allowPremium and not gctx.budget_ok:
        errors.append("premium_over_budget")
    # Restricted/secret stays local unless an exception was affirmatively granted.
    if decision.classification.dataSensitivity in (DataSensitivity.RESTRICTED, DataSensitivity.SECRET):
        if r.backend in REMOTE_BACKENDS and not gctx.remote_exception_approved:
            errors.append("restricted_data_remote_blocked")
    if decision.approvalRecommendation.required and not gctx.approval_satisfied:
        errors.append("approval_gate_unsatisfied")
    if not gctx.sandbox_ok:
        errors.append("sandbox_constraint_violated")
    return errors


def _final_route(decision: CoordinatorDecision, gctx: GateContext) -> Dict[str, Any]:
    required = decision.approvalRecommendation.required
    return {
        "backend": decision.routeRecommendation.backend.value,
        "modelRoleChain": decision.routeRecommendation.modelRoleChain,
        "allowPremium": decision.routeRecommendation.allowPremium,
        "verificationMode": decision.classification.verificationMode.value,
        "dataSensitivity": decision.classification.dataSensitivity.value,
        "approvalRequired": required,
        # True only when no approval is needed, or the gate was actually
        # satisfied (run_hard_gates already rejected required-but-unsatisfied).
        "approved": gctx.approval_satisfied if required else True,
        "rationale": decision.rationale,
        "schemaVersion": decision.schemaVersion,
    }


def safe_scout_fallback(task_id: str, errors: Optional[List[str]] = None,
                        audit_notes: Optional[List[str]] = None,
                        raw_output: Optional[str] = None) -> WrapperResult:
    """Terminal fail-closed route: local scout, analysis only, nothing approved."""
    return WrapperResult(
        ok=False,  # coordinator decision REJECTED; fail-closed to safe-scout
        decision=None,
        route={
            "backend": ExecutionBackend.ODYSSEUS_GENERAL_SWE.value,
            "modelRoleChain": [{"role": ModelRole.SCOUT.value, "reason": "safe-scout fallback"}],
            "allowPremium": False,
            "verificationMode": VerificationMode.ANALYSIS_ONLY.value,
            "dataSensitivity": DataSensitivity.INTERNAL.value,
            "approvalRequired": False,
            "approved": False,
            "rationale": ["coordinator validation failed -> deterministic safe-scout fallback"],
            "schemaVersion": SCHEMA_VERSION,
        },
        appliedFallback=True,
        fallbackPath="safe_scout",
        validationErrors=list(errors or []),
        auditNotes=list(audit_notes or []) + [f"task={task_id}: fell back to safe-scout"],
        rawOutput=raw_output,
    )


def _deterministic_fallback(task_id: str, deterministic_fn, errors: List[str],
                            audit_notes: List[str], raw_output: Optional[str]) -> WrapperResult:
    """Fallback to the deterministic router, then to safe-scout."""
    if deterministic_fn is None:
        audit_notes.append("deterministic_router_unavailable")
        return safe_scout_fallback(task_id, errors, audit_notes, raw_output)
    try:
        route = deterministic_fn(task_id)
    except Exception as e:  # noqa: BLE001 — any router failure fails closed
        audit_notes.append(f"deterministic_router_error:{e}")
        return safe_scout_fallback(task_id, errors, audit_notes, raw_output)
    if not route:
        audit_notes.append("deterministic_router_no_route")
        return safe_scout_fallback(task_id, errors, audit_notes, raw_output)
    audit_notes.append(f"task={task_id}: fell back to deterministic router")
    return WrapperResult(
        ok=False, decision=None, route=route,
        appliedFallback=True, fallbackPath="deterministic",
        validationErrors=list(errors), auditNotes=audit_notes, rawOutput=raw_output,
    )


def wrap_coordinator_output(
    raw_text: str,
    gctx: GateContext,
    *,
    repair_fn: Optional[Callable[[str, List[str]], Optional[str]]] = None,
    deterministic_fn: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
) -> WrapperResult:
    """
    Parse, strictly validate and gate a raw decision into a final route.

    On failure: one `repair_fn` retry (known schema versions with field errors
    only; never for policy failures), then `deterministic_fn`, then safe-scout.
    Every path records fallbackPath and validationErrors.
    """
    raw_text = raw_text or ""
    audit_notes: List[str] = []

    def _parse(text: str):
        try:
            parsed_json = json.loads(text)
        except json.JSONDecodeError as e:
            return None, [f"json_parse_error:{e}"], False
        try:
            return parse_decision(parsed_json), [], False
        except SchemaVersionError as e:
            return None, [f"schema_version_error:{e}"], True
        except DecisionValidationError as e:
            return None, [f"decision_validation_error:{err}" for err in e.errors], False

    decision, errors, version_bad = _parse(raw_text)

    if decision is None and not version_bad and repair_fn is not None:
        audit_notes.append("repair_attempted")
        try:
            repaired_text = repair_fn(raw_text, errors)
        except Exception as e:  # noqa: BLE001
            repaired_text = None
            audit_notes.append(f"repair_fn_error:{e}")
        if repaired_text:
            decision2, errors2, version_bad2 = _parse(repaired_text)
            if decision2 is not None and not version_bad2:
                gate_errors2 = run_hard_gates(decision2, gctx)
                if not gate_errors2:
                    audit_notes.append("repaired_and_passed")
                    return WrapperResult(
                        ok=True, decision=decision2, route=_final_route(decision2, gctx),
                        appliedFallback=True, fallbackPath="repair",
                        validationErrors=[], auditNotes=audit_notes, rawOutput=raw_text,
                    )
                audit_notes.append("repaired_but_gates_failed:" + ";".join(gate_errors2))
                errors = errors + gate_errors2
            else:
                audit_notes.append("repair_still_invalid:" + ";".join(errors2))
                errors = errors + errors2

    if decision is None:
        audit_notes.append("validation_failed:" + ";".join(errors))
        return _deterministic_fallback(gctx.task_id, deterministic_fn, errors,
                                       audit_notes, raw_text)

    gate_errors = run_hard_gates(decision, gctx)
    if gate_errors:
        # Repair can't make an illegal route legal, so skip to the deterministic tier.
        audit_notes.append("gates_failed:" + ";".join(gate_errors))
        return _deterministic_fallback(gctx.task_id, deterministic_fn, gate_errors,
                                       audit_notes, raw_text)

    return WrapperResult(
        ok=True, decision=decision, route=_final_route(decision, gctx),
        appliedFallback=False, fallbackPath="none",
        validationErrors=[], auditNotes=audit_notes, rawOutput=raw_text,
    )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
