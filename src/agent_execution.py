"""Pinned execution identity for the agent loop.

Invariant: one agent execution run has one pinned execution target. Every
working-model call in a run (rounds, tool continuations, retries) uses that
identity; it changes only via an explicit recorded transition (fallback,
escalation, approved reroute), represented as a new AgentExecutionTarget with
``previous_execution_id`` and ``transition_reason``.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

#: A provider/runtime outage is never model-quality evidence; keep these
#: separate from model or reasoning failure signals.
FAILURE_TRANSIENT = "provider_transient"
FAILURE_PERMANENT = "provider_permanent"
FAILURE_UNKNOWN = "provider_unknown"

#: Tool-call format the pinned target speaks (native ``tool_calls`` or fenced
#: blocks), pinned so later rounds never assume a different backend.
TOOL_PROFILE_NATIVE = "native-tools"
TOOL_PROFILE_FENCED = "fenced-tools"

#: "agent" is the multi-round tool loop; others record child executions (e.g.
#: the completion verifier).
EXECUTION_MODE_AGENT = "agent"
EXECUTION_MODE_CHAT = "chat"
EXECUTION_MODE_VERIFIER = "verifier"

#: Statuses worth an ordinary retry on the same target before any fallback;
#: mirrors llm_core's non-streaming retry set.
_TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

_STATUS_RE = re.compile(r'"status"\s*:\s*(\d{3})')
_TEXT_RE = re.compile(r'"(?:text|error)"\s*:\s*"((?:[^"\\]|\\.)*)"')

#: Provider -> runtime label; free-form so HTTP, native API, agent runtime or
#: CLI backends all fit.
_RUNTIME_BY_PROVIDER = {
    "ollama": "ollama",
    "anthropic": "anthropic-api",
    "chatgpt-subscription": "chatgpt-subscription",
    "copilot": "copilot",
}
_DEFAULT_RUNTIME = "openai-compatible"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _model_identity(model: str) -> str:
    """Provider-agnostic model id (strips a vendor namespace) for same-model comparison."""
    text = (model or "").strip().lower()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text


def headers_fingerprint(headers) -> str:
    """Non-secret digest of the auth headers, so credential changes on the same
    URL are visible in the audit trail without persisting bearer keys."""
    if not headers:
        return ""
    try:
        canonical = json.dumps(headers, sort_keys=True, default=str)
    except Exception:
        canonical = str(headers)
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()[:16]


def detect_provider(endpoint_url: str) -> str:
    """Provider id for ``endpoint_url``; degrades safely if llm_core is unavailable."""
    try:
        from src.llm_core import _detect_provider

        return _detect_provider(endpoint_url or "") or "openai"
    except Exception:
        return "openai"


def _runtime_for_provider(provider: str) -> str:
    return _RUNTIME_BY_PROVIDER.get(provider, _DEFAULT_RUNTIME)


def _reasoning_mode(model: str) -> str:
    try:
        from src.llm_core import _supports_thinking

        return "thinking" if _supports_thinking(model or "") else "standard"
    except Exception:
        return "standard"


def _budget_domain(endpoint_url: str) -> str:
    """'local' for loopback/RFC1918/LAN hosts, else 'remote'. A hint only."""
    try:
        from src.model_context import is_local_endpoint

        return "local" if is_local_endpoint(endpoint_url or "") else "remote"
    except Exception:
        return "remote"


@dataclass(frozen=True)
class AgentExecutionTarget:
    """Immutable pinned execution identity for one agent execution.

    Field names are generic so CLI or agent-runtime backends fit.
    ``context_window``/``tool_profile``/``reasoning_mode`` pin the loop's
    assumptions for this execution.
    """

    execution_id: str
    provider: str
    model: str
    endpoint_url: str
    runtime: str
    execution_mode: str
    context_window: int
    tool_profile: str
    reasoning_mode: str
    budget_domain: str
    selected_at: str
    selection_reason: str
    headers_key: str = ""
    session_id: str = ""
    previous_execution_id: Optional[str] = None
    transition_reason: Optional[str] = None

    @property
    def is_transition(self) -> bool:
        return bool(self.previous_execution_id)

    def to_dict(self) -> dict:
        """Audit/telemetry shape; safe to emit over SSE (no secrets)."""
        return {
            "execution_id": self.execution_id,
            "provider": self.provider,
            "model": self.model,
            "endpoint": self.endpoint_url,
            "runtime": self.runtime,
            "execution_mode": self.execution_mode,
            "context_window": self.context_window,
            "tool_profile": self.tool_profile,
            "reasoning_mode": self.reasoning_mode,
            "budget_domain": self.budget_domain,
            "selected_at": self.selected_at,
            "selection_reason": self.selection_reason,
            "headers_key": self.headers_key,
            "session_id": self.session_id,
            "previous_execution_id": self.previous_execution_id,
            "transition_reason": self.transition_reason,
        }


def build_execution_target(
    *,
    endpoint_url: str,
    model: str,
    headers=None,
    context_window: int = 0,
    is_api_model: bool = False,
    execution_mode: str = EXECUTION_MODE_AGENT,
    selection_reason: str = "initial_resolution",
    session_id: str = "",
    previous: Optional[AgentExecutionTarget] = None,
    transition_reason: Optional[str] = None,
) -> AgentExecutionTarget:
    """Resolve and freeze an execution identity. Called once before round 1 and
    again only on an explicit transition; never inside the round loop."""
    provider = detect_provider(endpoint_url)
    return AgentExecutionTarget(
        execution_id=str(uuid.uuid4()),
        provider=provider,
        model=model or "",
        endpoint_url=endpoint_url or "",
        runtime=_runtime_for_provider(provider),
        execution_mode=execution_mode,
        context_window=int(context_window or 0),
        tool_profile=TOOL_PROFILE_NATIVE if is_api_model else TOOL_PROFILE_FENCED,
        reasoning_mode=_reasoning_mode(model or ""),
        budget_domain=_budget_domain(endpoint_url),
        selected_at=_utc_iso(),
        selection_reason=selection_reason,
        headers_key=headers_fingerprint(headers),
        session_id=session_id or "",
        previous_execution_id=(previous.execution_id if previous else None),
        transition_reason=transition_reason,
    )


def _extract_status(error) -> Optional[int]:
    """Best-effort HTTP status from an SSE error chunk, an Exception, or an int."""
    if isinstance(error, int):
        return error
    if isinstance(error, str):
        m = _STATUS_RE.search(error)
        return int(m.group(1)) if m else None
    status = getattr(error, "status_code", None)
    if status is None:
        resp = getattr(error, "response", None)
        status = getattr(resp, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def classify_provider_failure(error) -> str:
    """Classify a provider/runtime failure (not a model reasoning failure).
    Transient failures are retry-worthy on the same target; permanent ones are not.
    """
    status = _extract_status(error)
    if status is not None:
        return FAILURE_TRANSIENT if status in _TRANSIENT_STATUS else FAILURE_PERMANENT
    text = str(error or "").lower()
    if any(k in text for k in (
        "timeout", "timed out", "cannot reach", "network error",
        "connection", "connect", "temporarily", "unavailable", "overloaded",
    )):
        return FAILURE_TRANSIENT
    return FAILURE_UNKNOWN


def summarize_provider_error(error) -> str:
    """Short human reason for the audit trail / SSE notice."""
    if not error:
        return "provider unavailable"
    text = error if isinstance(error, str) else str(error)
    m = _TEXT_RE.search(text)
    reason = m.group(1) if m else ""
    status = _extract_status(error)
    if not reason:
        reason = text.strip().splitlines()[0] if text.strip() else "provider unavailable"
    if status:
        return f"HTTP {status}: {reason}"[:200]
    return reason[:200]


def _is_same_route(left: Tuple[str, str], right: Tuple[str, str]) -> bool:
    return (
        (left[0] or "").strip() == (right[0] or "").strip()
        and _model_identity(left[1]) == _model_identity(right[1])
    )


def prefer_same_model_order(
    pinned_model: str,
    candidates: Sequence[Tuple[str, str, dict]],
) -> List[Tuple[str, str, dict]]:
    """Order a fallback pool with same-model/different-provider routes first,
    otherwise preserving operator order."""
    pinned = _model_identity(pinned_model)
    same: List[Tuple[str, str, dict]] = []
    other: List[Tuple[str, str, dict]] = []
    for c in candidates or []:
        if not c or len(c) < 2 or not c[0] or not c[1]:
            continue
        (same if _model_identity(c[1]) == pinned else other).append(tuple(c))
    return same + other


def select_transition_target(
    *,
    pinned_model: str,
    current_route: Tuple[str, str],
    pool: Sequence[Tuple[str, str, dict]],
    used_routes: Iterable[Tuple[str, str]],
) -> Optional[Tuple[str, str, dict]]:
    """Pick the next transition target: same-model routes first, skipping
    routes already tried so a transition always changes the identity."""
    used = list(used_routes or [])
    for cand in prefer_same_model_order(pinned_model, pool):
        route = (cand[0], cand[1])
        if any(_is_same_route(route, u) for u in used):
            continue
        if _is_same_route(route, current_route):
            continue
        return cand
    return None
