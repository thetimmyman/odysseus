"""Coordinator invocation, selected by coordinator.provider.

"external" (default): decisions are POSTed to /api/harness/coordinator/wrap;
decide() and repair are unavailable. "endpoint": a named ModelEndpoint decides
via llm_call; not the default, so no tokens are spent until one is chosen.

Endpoint failures never crash the wrap path; they fall back to deterministic.
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
    """Schema for the system prompt, built from the enums so it can't drift from the validator."""
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

    def _resolve_endpoint(self) -> None:
        """Resolve the endpoint; failure is recorded, not raised, so wrap degrades instead of 500ing."""
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

    def decide(self, task_payload: dict) -> str:
        """Raw, unvalidated decision text; wrap_coordinator_output validates it."""
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
        """One schema-repair retry at temperature 0 (transcription, not judgement);
        None on failure."""
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
