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


def _redact_obj(obj):
    """Deep-redact every string value in a JSON-ish structure via
    routing_redaction.redact_text, preserving shape. Applied to the OUTBOUND
    coordinator payload so a credential that slipped into a task's title /
    objective / inputs / constraints is masked before it is transmitted to the
    coordinator model — the same pre-prompt scrub routing_context does before
    any remote-eligible worker prompt (spec Section 9)."""
    from src.routing_redaction import redact_text

    if isinstance(obj, str):
        return redact_text(obj)[0]
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_obj(v) for v in obj]
    return obj


def coordinator_endpoint_permits_sensitivity(task, client) -> bool:
    """Section 9 data-locality gate for the coordinator seat: a task whose
    data_sensitivity ranks ABOVE the policy's remoteSensitivityCeiling may only
    have its payload sent to a LOCAL coordinator endpoint. Mirrors exactly the
    hard filter routing_engine.route_task applies to worker endpoints — the
    /coordinator/decide path is a new surface that ships task content to a
    model, so it must honour the same fail-closed boundary. An unresolvable
    endpoint URL (client._chat_url is None) is NOT local, so restricted/secret
    data is never sent to an unverifiable destination."""
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
    failure — that degrades to deterministic/safe_scout.

    Section 9 data-locality gate: if the task's sensitivity ranks above the
    policy's remote ceiling AND the coordinator endpoint is not local, the
    payload is NEVER transmitted — we skip the model call and degrade to the
    deterministic (local-only) router, mirroring the hard filter
    routing_engine.route_task applies to worker endpoints. The outbound payload
    is also credential-redacted before it leaves the process (defense in depth
    for a secret that slipped into a non-secret task)."""
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
        # Fail closed: over-ceiling data must not reach a non-local coordinator.
        # Skip the model call entirely and let the wrapper fall to the
        # deterministic (local-only) tier; record why in the audit trail.
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
    # When the endpoint was locality-blocked, do NOT wire repair_fn: the schema
    # repair tier would call that same excluded remote endpoint (with the schema
    # + validation errors — not the task payload, so not a data leak, but a
    # pointless network call to an endpoint we just decided must not serve this
    # task). Go straight to the deterministic local tier.
    repair_fn = None if locality_blocked else client.repair_fn
    result = wrap_coordinator_output(
        raw, gctx, repair_fn=repair_fn, deterministic_fn=deterministic_fn
    )
    # Redact BEFORE store/return: validationErrors/auditNotes can carry
    # model-controlled fragments (parse_decision interpolates offending enum
    # values) and decide_error can echo an endpoint's error body — neither goes
    # through the raw_output redaction path, so scrub them here to keep the
    # redact-before-store bar for every persisted/returned field.
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
