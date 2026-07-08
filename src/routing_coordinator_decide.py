"""src/routing_coordinator_decide.py — server-side coordinator decision
generation (spec Phase 8, closing the coordinator loop).

The /coordinator/wrap route only WRAPS a decision that was produced elsewhere
(external provider). This module produces one FROM the resident coordinator
endpoint and runs it through the exact same deterministic wrapper + audit
archive path, so an LLM-generated decision that is malformed or policy-illegal
falls back truthfully and is audited identically to a pasted one.

Consumed by:
  - routes.routing_harness_routes POST /api/harness/coordinator/decide
  - scripts/odysseus-coordinator decide  (host-side)

Both build a CoordinatorClient.from_policy(policy); this module DOES NOT rewrite
that client — it consumes decide()/repair_fn(). Endpoint DB/network failures
degrade to the deterministic tier (never a 500): decide() raising is caught and
turned into empty raw output, which the wrapper treats as a parse failure and
routes down the fallback chain.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

from src.routing_coordinator import (
    SCHEMA_VERSION,
    GateContext,
    wrap_coordinator_output,
)


class ExternalProviderError(RuntimeError):
    """coordinator.provider is 'external' — decisions arrive via /coordinator/wrap,
    not generated here. The route maps this to a 400; the CLI to a nonzero exit."""


def build_deterministic_route(db, task) -> Optional[Dict[str, Any]]:
    """Section 8 tier-2 fallback route builder: shape routing_engine.route_task()'s
    top candidates like a validated coordinator final route so downstream
    consumers see one route schema regardless of which tier produced it. Shared
    by the /coordinator/wrap and /coordinator/decide paths (single source of
    truth — imported by routes.routing_harness_routes)."""
    from src.routing_context import build_context_bundle
    from src.routing_engine import ROLE_BY_TASK, route_task

    bundle = build_context_bundle(task)
    candidates = route_task(db, task, bundle)["candidates"][:3]
    if not candidates:
        return None
    desired = ROLE_BY_TASK.get(task.task_type, ["scout"])
    chain = []
    for cand in candidates:
        roles = cand.get("roles") or []
        role = next((r for r in desired if r in roles), roles[0] if roles else "scout")
        chain.append({
            "role": role,
            "reason": "; ".join(cand.get("reasons") or []) or "ranked candidate",
            "modelPreference": cand.get("model"),
        })
    return {
        "backend": "odysseus_general_swe",
        "modelRoleChain": chain,
        "allowPremium": False,
        "verificationMode": task.verification_mode or "analysis_only",
        "dataSensitivity": task.data_sensitivity or "internal",
        "approvalRequired": False,
        "approved": False,
        "rationale": ["deterministic router fallback"],
        "schemaVersion": SCHEMA_VERSION,
    }


def _persist_audit(db, task_id: str, raw_output: str, result, policy_versions: dict):
    # Returns (audit_id, redacted_raw, redaction_applied).
    """Archive a generated decision identically to /coordinator/wrap: redact
    BEFORE storage, HMAC the redacted text, stamp policy versions + parsed_ok +
    fallback_path (WP6 observability reads exactly these columns)."""
    from core.database import CoordinatorAudit
    from src.routing_redaction import redact_text
    from src.secret_storage import hmac_sign

    red, applied = redact_text(raw_output or "")
    audit = CoordinatorAudit(
        id=str(uuid.uuid4()),
        task_id=task_id,
        schema_version=SCHEMA_VERSION,
        raw_output=red,
        validation_errors=json.dumps(result.validationErrors),
        fallback_path=result.fallbackPath,
        applied_fallback=result.appliedFallback,
        audit_notes=json.dumps(result.auditNotes),
        parsed_ok=result.ok and result.decision is not None,
        policy_versions=json.dumps(policy_versions),
        redaction_applied=applied,
        hmac=hmac_sign(red),
    )
    db.add(audit)
    db.commit()
    return audit.id, red, applied


def generate_and_wrap_decision(
    db,
    task,
    client,
    *,
    remote_exception_approved: bool = False,
    budget_ok: bool = True,
    backend_available: bool = True,
    approval_satisfied: bool = False,
    sandbox_ok: bool = True,
) -> Dict[str, Any]:
    """Generate a coordinator decision for `task` from the resident endpoint,
    wrap it through the deterministic gates + fallback chain, and archive it.

    `client` must be an endpoint-backed CoordinatorClient (is_llm_backed()); the
    caller enforces that and maps ExternalProviderError to 400/nonzero. Returns
    the same shape as /coordinator/wrap plus `generatedRaw` (REDACTED — never the
    verbatim model text) and `decideError` (set when the endpoint call itself
    failed and we degraded to the fallback chain). Never raises on an endpoint
    failure — that degrades to deterministic/safe_scout."""
    from src import routing_policy
    from src.routing_task_io import task_payload_from_row

    if not client.is_llm_backed():
        raise ExternalProviderError(
            "coordinator provider is 'external'; POST the decision to "
            "/coordinator/wrap instead"
        )

    payload = task_payload_from_row(task)
    decide_error: Optional[str] = None
    try:
        raw = client.decide(payload)
    except Exception as e:  # noqa: BLE001 — endpoint failure degrades, never 500s
        raw = ""
        decide_error = str(e)

    def deterministic_fn(_task_id: str) -> Optional[Dict[str, Any]]:
        return build_deterministic_route(db, task)

    gctx = GateContext(
        remote_exception_approved=remote_exception_approved,
        budget_ok=budget_ok,
        backend_available=backend_available,
        approval_satisfied=approval_satisfied,
        sandbox_ok=sandbox_ok,
        task_id=task.id,
    )
    result = wrap_coordinator_output(
        raw, gctx, repair_fn=client.repair_fn, deterministic_fn=deterministic_fn
    )
    if decide_error:
        result.auditNotes = list(result.auditNotes) + [f"decide_failed:{decide_error}"]

    pv = routing_policy.policy_versions()
    audit_id, redacted_raw, _applied = _persist_audit(db, task.id, raw, result, pv)

    return {
        "ok": result.ok,
        "appliedFallback": result.appliedFallback,
        "fallbackPath": result.fallbackPath,
        "validationErrors": result.validationErrors,
        "auditNotes": result.auditNotes,
        "route": result.route,
        "auditId": audit_id,
        "policyVersions": pv,
        "generatedRaw": redacted_raw,
        "decideError": decide_error,
    }
