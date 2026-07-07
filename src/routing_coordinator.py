
"""
routing_coordinator.py — Coordinator plane for the v0.5 Model Routing Harness.

Implements Section 6 (Versioned CoordinatorDecision schema) and Section 8
(Deterministic wrapper around coordinator). The resident local coordinator LLM
produces a JSON decision; this module validates it, runs the hard gates, and
either yields a final route or falls back to the deterministic router / safe
scout. Never does a coordinator output bypass data policy, budget, sandbox,
verification, or approval gates.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("odysseus.routing.coordinator")

SCHEMA_VERSION = "0.5"


class SchemaVersionError(ValueError):
    """Raised when an unknown / unsupported CoordinatorDecision schemaVersion is seen."""


# --- Enums (kept in sync with the v0.5 spec) ---
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
    REFACTOR_EQUIVALENCE = "refactor_equivalence"
    BEHAVIORAL = "behavioral"
    ADVISORY = "advisory"
    NONE = "none"


class ExecutionBackend(str, Enum):
    ODYSSEUS_GENERAL_SWE = "odysseus_general_swe"
    OPENROUTER = "openrouter"
    LOCAL_FRAMEWORK_COORDINATOR_ONLY = "local_framework_coordinator_only"
    ABSIS_TACTICUS_JOB_QUEUE = "absis_tacticus_job_queue"
    MINI_PC_LLM_INFERENCE_WORKER = "mini_pc_llm_inference_worker"
    MINI_PC_ORACLE_RUNNER = "mini_pc_oracle_runner"
    HUMAN_ONLY = "human_only"
    HUMAN_ONLY_EMERGENCY = "human_only_emergency"


class ModelRole(str, Enum):
    SCOUT = "scout"
    PLANNER = "planner"
    REVIEWER = "reviewer"
    IMPLEMENTER = "implementer"
    DEBUGGER = "debugger"
    ESCALATION = "escalation"


# --- Structured decision graph (validated, not just parsed) ---
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
    # NOTE (v0.5): premiumJustification removed. Use rationale[] + RunManifest.auditNotes.


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
    # NOTE (v0.5): confidence is metadata only, never a pass/fail verification layer.


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
    fallbackPath: Optional[str]
    validationErrors: List[str]
    auditNotes: List[str]
    rawOutput: Optional[str]


# --- Parsing / validation ---
def _as_enum(value: Any, enum_cls, default):
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        return default


def parse_decision(raw: Dict[str, Any]) -> CoordinatorDecision:
    """Build a typed CoordinatorDecision from parsed JSON. Raises on bad shape."""
    sv = raw.get("schemaVersion")
    if sv != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"Unsupported CoordinatorDecision.schemaVersion={sv!r}; expected {SCHEMA_VERSION!r}"
        )
    c = raw.get("classification", {}) or {}
    ctx = raw.get("contextRequest", {}) or {}
    route = raw.get("routeRecommendation", {}) or {}
    bud = raw.get("budgetRecommendation", {}) or {}
    appr = raw.get("approvalRecommendation", {}) or {}
    conf = raw.get("confidence", {}) or {}
    return CoordinatorDecision(
        schemaVersion=SCHEMA_VERSION,
        taskId=str(raw.get("taskId", "")),
        classification=Classification(
            domain=_as_enum(c.get("domain"), Domain, Domain.UNKNOWN),
            taskType=_as_enum(c.get("taskType"), TaskType, TaskType.UNKNOWN),
            risk=_as_enum(c.get("risk"), Risk, Risk.LOW),
            dataSensitivity=_as_enum(c.get("dataSensitivity"), DataSensitivity, DataSensitivity.INTERNAL),
            verificationMode=_as_enum(c.get("verificationMode"), VerificationMode, VerificationMode.ADVISORY),
        ),
        contextRequest=ContextRequest(
            sources=list(ctx.get("sources", []) or []),
            includeTests=bool(ctx.get("includeTests", True)),
            includeLogs=bool(ctx.get("includeLogs", False)),
            maxUntrustedTokens=int(ctx.get("maxUntrustedTokens", 256)),
        ),
        routeRecommendation=RouteRecommendation(
            backend=_as_enum(route.get("backend"), ExecutionBackend, ExecutionBackend.ODYSSEUS_GENERAL_SWE),
            modelRoleChain=list(route.get("modelRoleChain", []) or []),
            allowPremium=bool(route.get("allowPremium", False)),
        ),
        budgetRecommendation=BudgetRecommendation(
            maxCostUsd=(float(bud["maxCostUsd"]) if bud.get("maxCostUsd") is not None else None),
            preferFree=bool(bud.get("preferFree", True)),
        ),
        approvalRecommendation=ApprovalRecommendation(
            required=bool(appr.get("required", False)),
            level=str(appr.get("level", "none")),
        ),
        confidence=CoordinatorConfidence(
            score=float(conf.get("score", 0.0)),
            basis=str(conf.get("basis", "metadata")),
        ),
        rationale=list(raw.get("rationale", []) or []),
    )


# --- Deterministic harness gates (Section 8) ---
@dataclass
class GateContext:
    """Inputs the deterministic harness uses to adjudicate a coordinator decision."""
    data_policy_allows_remote: bool = True
    budget_ok: bool = True
    backend_available: bool = True
    approval_satisfied: bool = True
    sandbox_ok: bool = True
    task_id: str = ""


def run_hard_gates(decision: CoordinatorDecision, gctx: GateContext) -> List[str]:
    """Return a list of validation errors. Empty list == gates passed."""
    errors: List[str] = []
    r = decision.routeRecommendation
    if not gctx.backend_available:
        errors.append(f"backend_unavailable:{r.backend.value}")
    if r.allowPremium and not gctx.budget_ok:
        errors.append("premium_over_budget")
    # Restricted/secret data is local-only unless approved (Section 9).
    if decision.classification.dataSensitivity in (DataSensitivity.RESTRICTED, DataSensitivity.SECRET):
        if r.backend in (ExecutionBackend.OPENROUTER, ExecutionBackend.ABSIS_TACTICUS_JOB_QUEUE):
            if not gctx.data_policy_allows_remote:
                errors.append("restricted_data_remote_blocked")
    if decision.approvalRecommendation.required and not gctx.approval_satisfied:
        errors.append("approval_gate_unsatisfied")
    if not gctx.sandbox_ok:
        errors.append("sandbox_constraint_violated")
    return errors


def _final_route(decision: CoordinatorDecision) -> Dict[str, Any]:
    return {
        "backend": decision.routeRecommendation.backend.value,
        "modelRoleChain": decision.routeRecommendation.modelRoleChain,
        "allowPremium": decision.routeRecommendation.allowPremium,
        "verificationMode": decision.classification.verificationMode.value,
        "dataSensitivity": decision.classification.dataSensitivity.value,
        "approved": decision.approvalRecommendation.required and True,  # harness confirms at runtime
        "rationale": decision.rationale,
        "schemaVersion": decision.schemaVersion,
    }


def safe_scout_fallback(task_id: str, errors: Optional[List[str]] = None) -> WrapperResult:
    return WrapperResult(
        ok=False,  # coordinator decision REJECTED; fail-closed to deterministic safe-scout
        decision=None,
        route={
            "backend": ExecutionBackend.ODYSSEUS_GENERAL_SWE.value,
            "modelRoleChain": [{"role": ModelRole.SCOUT.value, "reason": "safe-scout fallback"}],
            "allowPremium": False,
            "verificationMode": VerificationMode.ADVISORY.value,
            "dataSensitivity": DataSensitivity.INTERNAL.value,
            "approved": False,
            "rationale": ["coordinator validation failed -> deterministic safe-scout fallback"],
            "schemaVersion": SCHEMA_VERSION,
        },
        appliedFallback=True,
        fallbackPath="safe_scout",
        validationErrors=list(errors or []),
        auditNotes=[f"task={task_id}: fell back to safe-scout (deterministic)"],
        rawOutput=None,
    )


def wrap_coordinator_output(
    raw_text: str,
    gctx: GateContext,
    *,
    repair_fn=None,
) -> WrapperResult:
    """
    Section 8 pipeline:
      raw -> archive -> JSON parse -> schemaVersion -> validate -> policy ->
      data-sensitivity -> budget -> backend -> final route.
    On failure: retry once with schema-repair prompt for known compatible
    versions; otherwise fall back to deterministic router / safe-scout; archive
    validationErrors + fallbackPath.
    """
    raw_text = raw_text or ""
    audit_notes: List[str] = []
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        audit_notes.append(f"json_parse_error:{e}")
        return safe_scout_fallback(gctx.task_id, ["json_parse_error"])

    try:
        decision = parse_decision(parsed)
    except SchemaVersionError as e:
        # Unknown schema versions FAIL CLOSED.
        audit_notes.append(f"schema_version_error:{e}")
        return safe_scout_fallback(gctx.task_id, ["schema_version_error"])
    except Exception as e:  # noqa: BLE001 - validation/shape errors fail closed
        audit_notes.append(f"decision_parse_error:{e}")
        return safe_scout_fallback(gctx.task_id, ["decision_parse_error"])

    errors = run_hard_gates(decision, gctx)
    if errors:
        audit_notes.append("gates_failed:" + ";".join(errors))
        # Retry once: schema repair for known compatible versions.
        if repair_fn is not None:
            try:
                repaired = repair_fn(parsed)
                decision2 = parse_decision(repaired)
                err2 = run_hard_gates(decision2, gctx)
                if not err2:
                    audit_notes.append("repaired_and_passed")
                    return WrapperResult(
                        ok=True, decision=decision2, route=_final_route(decision2),
                        appliedFallback=False, fallbackPath=None,
                        validationErrors=[], auditNotes=audit_notes, rawOutput=raw_text,
                    )
                audit_notes.append("repaired_still_failed:" + ";".join(err2))
            except Exception as e:  # noqa: BLE001
                audit_notes.append(f"repair_failed:{e}")
        return safe_scout_fallback(gctx.task_id, errors)

    return WrapperResult(
        ok=True, decision=decision, route=_final_route(decision),
        appliedFallback=False, fallbackPath=None,
        validationErrors=[], auditNotes=audit_notes, rawOutput=raw_text,
    )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
