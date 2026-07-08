"""src/routing_coordinator_client.py — coordinator invocation interface.

Two providers, selected by routing_policy's coordinator.provider:

  "external" (default): the coordinator LLM runs OUTSIDE this process (a
    Claude/Codex session, a human, a script) and POSTs its decision to
    /api/harness/coordinator/wrap — decide() is deliberately unimplemented
    and repair is unavailable, so the deterministic wrapper falls straight
    to the deterministic router on a bad decision.

  "endpoint": a registered ModelEndpoint (resolved BY NAME from the policy)
    produces the decision via llm_call. Wired but DELIBERATELY not the
    default: the Phase 8 benchmark selects which model earns the coordinator
    seat — until then nothing should silently start spending tokens on
    coordination just because an endpoint exists.

Endpoint DB/network failures never crash the wrap path: repair_fn returns
None and decide() raises a clear RuntimeError, both of which the route layer
treats as fallback-to-deterministic.
"""
import json
import logging
from typing import Any, Dict, List, Optional

from src.routing_coordinator import (
    APPROVAL_LEVELS,
    DataSensitivity,
    Domain,
    ExecutionBackend,
    ModelRole,
    Risk,
    SCHEMA_VERSION,
    TaskType,
    VerificationMode,
)

logger = logging.getLogger("odysseus.routing.coordinator_client")


def _enum_values(enum_cls) -> str:
    return "|".join(e.value for e in enum_cls)


def _schema_description() -> str:
    """Compact CoordinatorDecision v0.5 schema for the system prompt. The
    allowed-value lists are built from the routing_coordinator enums at call
    time so prompt and validator can never drift apart."""
    return (
        "Schema (all fields shown; schemaVersion/taskId/classification/routeRecommendation required):\n"
        "{\n"
        f'  "schemaVersion": "{SCHEMA_VERSION}",\n'
        '  "taskId": "<string>",\n'
        '  "classification": {\n'
        f'    "domain": "{_enum_values(Domain)}",\n'
        f'    "taskType": "{_enum_values(TaskType)}",\n'
        f'    "risk": "{_enum_values(Risk)}",\n'
        f'    "dataSensitivity": "{_enum_values(DataSensitivity)}",\n'
        f'    "verificationMode": "{_enum_values(VerificationMode)}"\n'
        "  },\n"
        '  "contextRequest": {"sources": ["<string>"], "includeTests": true, "includeLogs": false, "maxUntrustedTokens": 256},\n'
        '  "routeRecommendation": {\n'
        f'    "backend": "{_enum_values(ExecutionBackend)}",\n'
        f'    "modelRoleChain": [{{"role": "{_enum_values(ModelRole)}", "reason": "<string>", "modelPreference": "<optional string>"}}],\n'
        '    "allowPremium": false\n'
        "  },\n"
        '  "budgetRecommendation": {"maxCostUsd": <number|null>, "preferFree": true},\n'
        f'  "approvalRecommendation": {{"required": false, "level": "{"|".join(APPROVAL_LEVELS)}"}},\n'
        '  "confidence": {"score": <0..1>, "basis": "<string>"},\n'
        '  "rationale": ["<string>"]\n'
        "}\n"
        "Enum fields accept exactly ONE of the |-separated values. "
        "Unknown fields are rejected. Reply with the JSON object only — no prose, no code fences."
    )


_SYSTEM_PROMPT_PREFIX = (
    "You are the routing coordinator. Reply with ONLY a JSON object conforming "
    f"to CoordinatorDecision schemaVersion {SCHEMA_VERSION}.\n\n"
)


class CoordinatorClient:
    def __init__(self, provider: str, policy: dict):
        self.provider = provider
        self.policy = policy or {}
        self._coord = (self.policy.get("coordinator") or {})
        self._chat_url: Optional[str] = None
        self._headers: Optional[Dict[str, str]] = None
        self._resolve_error: Optional[str] = None
        if self.provider == "endpoint":
            self._resolve_endpoint()

    @classmethod
    def from_policy(cls, policy: dict) -> "CoordinatorClient":
        coord = (policy or {}).get("coordinator") or {}
        return cls(provider=coord.get("provider") or "external", policy=policy or {})

    def is_llm_backed(self) -> bool:
        return self.provider == "endpoint"

    # -- endpoint provider plumbing ------------------------------------------
    def _resolve_endpoint(self) -> None:
        """Resolve coordinator.endpointName to a chat URL + auth headers.
        Failure is recorded, not raised: construction happens on the wrap
        request path, where a misconfigured coordinator must degrade to the
        deterministic tier rather than 500 the whole endpoint."""
        name = self._coord.get("endpointName")
        if not name:
            self._resolve_error = "policy coordinator.endpointName is not set"
            return
        try:
            from core.database import ModelEndpoint, SessionLocal
            from src.endpoint_resolver import build_chat_url, build_headers, resolve_endpoint_runtime

            db = SessionLocal()
            try:
                ep = db.query(ModelEndpoint).filter(
                    ModelEndpoint.name == name,
                    ModelEndpoint.is_enabled == True,  # noqa: E712
                ).first()
                if not ep:
                    self._resolve_error = f"no enabled ModelEndpoint named {name!r}"
                    return
                base, api_key = resolve_endpoint_runtime(ep)
                self._chat_url = build_chat_url(base)
                self._headers = build_headers(api_key, base)
            finally:
                db.close()
        except Exception as e:  # noqa: BLE001 — any resolution failure degrades, never crashes
            self._resolve_error = f"endpoint resolution failed: {e}"

    def _call(self, messages: List[Dict[str, Any]], temperature: float) -> str:
        if self._chat_url is None:
            raise RuntimeError(f"coordinator endpoint unavailable: {self._resolve_error}")
        from src.llm_core import llm_call

        return llm_call(
            url=self._chat_url,
            model=self._coord.get("model"),
            messages=messages,
            temperature=temperature,
            max_tokens=int(self._coord.get("maxTokens") or 2048),
            headers=self._headers,
            bypass_cache=True,
        )

    # -- public interface ------------------------------------------------------
    def decide(self, task_payload: dict) -> str:
        """Ask the coordinator model for a raw decision (unvalidated text —
        wrap_coordinator_output owns validation and fallback)."""
        if self.provider != "endpoint":
            raise NotImplementedError("external provider receives decisions via API")
        system = _SYSTEM_PROMPT_PREFIX + _schema_description()
        try:
            return self._call(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": json.dumps(task_payload)},
                ],
                temperature=float(self._coord.get("temperature") if self._coord.get("temperature") is not None else 0.1),
            )
        except RuntimeError:
            raise
        except Exception as e:
            raise RuntimeError(f"coordinator decide() call failed: {e}") from e

    def repair_fn(self, raw_text: str, errors: List[str]) -> Optional[str]:
        """One schema-repair retry (Section 8 tier 1). Endpoint provider only;
        temperature 0 because this is transcription-to-schema, not judgement.
        Returns None on any failure so the wrapper walks on to the
        deterministic tier."""
        if self.provider != "endpoint":
            return None
        system = (
            _SYSTEM_PROMPT_PREFIX
            + _schema_description()
            + "\n\nThe previous output failed validation. Fix ONLY the listed problems; "
            "do not change any other field or invent new values."
        )
        user = (
            "Validation errors:\n"
            + "\n".join(f"- {e}" for e in (errors or []))
            + "\n\nPrevious output:\n"
            + (raw_text or "")
        )
        try:
            return self._call(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0.0,
            )
        except Exception as e:  # noqa: BLE001 — repair is best-effort by design
            logger.warning("coordinator repair call failed: %s", e)
            return None
