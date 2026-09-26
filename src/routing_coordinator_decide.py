"""Server-side coordinator decision generation.

A generated decision goes through the same wrapper and audit path as a pasted
one. Endpoint failures become empty raw output, which falls down the fallback
chain instead of returning a 500.
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
    """coordinator.provider is 'external', so decisions are not generated here."""


def _redact_obj(obj):
    """Deep-redact every string, so a credential in task fields never reaches the model."""
    from src.routing_redaction import redact_text

    if isinstance(obj, str):
        return redact_text(obj)[0]
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    return obj


def coordinator_endpoint_permits_sensitivity(task, client) -> bool:
    """Data above remoteSensitivityCeiling may go only to a local coordinator,
    matching the worker filter. An unresolvable URL is not local."""
    from src.routing_engine import (
        _SENSITIVITY_RANK,
        _endpoint_is_local,
        _remote_ceiling_rank,
    )

    sensitivity = getattr(task, "data_sensitivity", None) or "internal"
    needs_local_only = _SENSITIVITY_RANK.get(sensitivity, 1) > _remote_ceiling_rank()
    if not needs_local_only:
        return True
    return _endpoint_is_local(getattr(client, "_chat_url", None))


def build_deterministic_route(db, task) -> Optional[Dict[str, Any]]:
    """Shape route_task() candidates like a coordinator final route, so every tier
    yields one schema."""
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
    """Archive like /coordinator/wrap: redact before storage, then HMAC the redacted text."""
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
    """Generate, wrap and archive a decision from an endpoint-backed client.

    Returns the /coordinator/wrap shape plus redacted `generatedRaw` and
    `decideError`. Endpoint failures degrade rather than raise. Over-ceiling
    data is never sent to a non-local endpoint, and the payload is redacted."""
    from src import routing_policy
    from src.routing_redaction import redact_text
    from src.routing_task_io import task_payload_from_row

    if not client.is_llm_backed():
        raise ExternalProviderError(
            "coordinator provider is 'external'; POST the decision to "
            "/coordinator/wrap instead"
        )

    decide_error: Optional[str] = None
    locality_blocked = not coordinator_endpoint_permits_sensitivity(task, client)
    if locality_blocked:
        # Over-ceiling data must not reach a non-local coordinator: skip the call.
        raw = ""
        decide_error = (
            "coordinator_remote_blocked: task data_sensitivity "
            f"{getattr(task, 'data_sensitivity', None) or 'internal'!r} exceeds the "
            "remote ceiling and the coordinator endpoint is not local; payload was "
            "not transmitted"
        )
    else:
        payload = _redact_obj(task_payload_from_row(task))
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
    # A locality-blocked endpoint must not serve repair either.
    repair_fn = None if locality_blocked else client.repair_fn
    result = wrap_coordinator_output(
        raw, gctx, repair_fn=repair_fn, deterministic_fn=deterministic_fn
    )
    # validationErrors/auditNotes/decide_error can echo model or endpoint text,
    # so redact them before storing or returning.
    if decide_error:
        red_err = redact_text(decide_error)[0]
        result.auditNotes = list(result.auditNotes) + [f"decide_failed:{red_err}"]
        decide_error = red_err
    result.validationErrors = [redact_text(e)[0] for e in result.validationErrors]
    result.auditNotes = [redact_text(n)[0] for n in result.auditNotes]

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
