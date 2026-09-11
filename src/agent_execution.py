"""src/agent_execution.py — pinned execution identity for the agent loop.

Reliability invariant this module exists to enforce:

    One agent execution run has ONE pinned execution target.

Once a run starts on a specific provider / model / endpoint / runtime, every
working-model invocation of that run — reasoning rounds, tool calls, tool-result
continuations, test/fix/test iterations and ordinary retries — must use that
exact execution identity. The identity may be replaced ONLY by an explicit,
recorded transition (provider fallback, model escalation, operator-approved
reroute), which is represented as a NEW :class:`AgentExecutionTarget` carrying
``previous_execution_id`` and ``transition_reason``.

It is deliberately tiny and dependency-light. Provider detection / context
window / reasoning-capability lookups live in ``llm_core`` and
``model_context``; this module only *captures* their results into an immutable
value object so the loop never has to re-derive them mid-run, plus the pure
helpers used to classify a provider failure and to order a fallback pool so a
same-model / different-provider switch is preferred over a model change.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

#: Failure classes surfaced on telemetry. A provider/runtime outage is NEVER
#: model-quality evidence — callers must keep these separate from model or
#: reasoning failure (governance) signals.
FAILURE_TRANSIENT = "provider_transient"
FAILURE_PERMANENT = "provider_permanent"
FAILURE_UNKNOWN = "provider_unknown"

#: Tool-call format the pinned target speaks. Native = OpenAI-style
#: ``tool_calls``; fenced = the model copies fenced-block examples from the
#: prompt. Pinned per execution so round 5 can never be invoked under round-1
#: tool assumptions for a different backend.
TOOL_PROFILE_NATIVE = "native-tools"
TOOL_PROFILE_FENCED = "fenced-tools"

#: Execution modes a pinned target can describe. "agent" is the multi-round
#: tool loop; the others are recorded for child/adjacent executions (the
#: completion verifier is a fresh-context child of the same pinned target).
EXECUTION_MODE_AGENT = "agent"
EXECUTION_MODE_CHAT = "chat"
EXECUTION_MODE_VERIFIER = "verifier"

#: Upstream HTTP statuses that are worth an ordinary retry on the SAME target
#: before any fallback transition is considered. Mirrors the retryable set
#: llm_core's non-streaming path already retries.
_TRANSIENT_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})

_STATUS_RE = re.compile(r'"status"\s*:\s*(\d{3})')
_TEXT_RE = re.compile(r'"(?:text|error)"\s*:\s*"((?:[^"\\]|\\.)*)"')

#: Provider -> runtime label. The label is free-form on purpose: a future
#: execution may be a local OpenAI-compatible HTTP server, a native API, a
#: Cline agent runtime, or a CLI (Claude Code / Codex / Google subscription).
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
    """Provider-agnostic model identity for same-model comparison.

    Strips a vendor namespace so ``openrouter``'s ``deepseek/deepseek-v4-pro``
    and a direct ``deepseek-v4-pro`` compare equal. Pure/local — no network.
    """
    text = (model or "").strip().lower()
    if "/" in text:
        text = text.rsplit("/", 1)[-1]
    return text


def headers_fingerprint(headers) -> str:
    """Stable, non-secret fingerprint of the auth headers in play.

    Raw headers are never persisted in telemetry (they carry bearer keys);
    only this digest is recorded so a transition to a different credential set
    on the same URL is still distinguishable in the audit trail.
    """
    if not headers:
        return ""
    try:
        canonical = json.dumps(headers, sort_keys=True, default=str)
    except Exception:
        canonical = str(headers)
    return hashlib.sha256(canonical.encode("utf-8", errors="replace")).hexdigest()[:16]


def detect_provider(endpoint_url: str) -> str:
    """Provider id for ``endpoint_url``; degrades safely when llm_core is
    unavailable (import-related, not a network call)."""
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
    """'local' when the endpoint is loopback/RFC1918/LAN host, else 'remote'.

    Only a recorded routing/budget hint on the identity; it never changes the
    target itself.
    """
    try:
        from src.model_context import is_local_endpoint

        return "local" if is_local_endpoint(endpoint_url or "") else "remote"
    except Exception:
        return "remote"


@dataclass(frozen=True)
class AgentExecutionTarget:
    """Immutable pinned execution identity for one agent execution.

    Field names are intentionally generic (``runtime``, ``execution_mode``)
    rather than "HTTP endpoint"-specific, so a future execution backed by a CLI
    or agent runtime fits without a redesign. ``context_window`` /
    ``tool_profile`` / ``reasoning_mode`` pin the *assumptions* the loop may
    make for this execution.
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
        """Audit/telemetry shape (safe to emit over SSE — no secrets)."""
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
    """Resolve once and freeze an execution identity.

    Called exactly once before round 1 and (only) again when the harness makes
    an explicit transition decision; ``previous``/``transition_reason`` record
    that transition. Never called inside the round loop for the working model.
    """
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
    """Classify a provider/runtime failure (NOT a model reasoning failure).

    ``error`` may be an ``event: error`` SSE chunk, an Exception, or a status
    int. Transient failures are retry-worthy on the same target; permanent
    failures are not.
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
    """Order a fallback pool as 'same model / different provider' first.

    Concretely: DeepSeek V4 Pro @ ClinePass -> DeepSeek V4 Pro @ OpenRouter is
    preferred over DeepSeek V4 Pro -> Kimi K3. Order is otherwise preserved
    (operator configuration wins within each group).
    """
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
    """Pick the next explicit-transition target from the fallback pool.

    Same-model / different-provider candidates are tried first, then the rest.
    Routes already attempted by this run (or identical to the current route)
    are skipped so a transition always actually changes the identity.
    """
    used = list(used_routes or [])
    for cand in prefer_same_model_order(pinned_model, pool):
        route = (cand[0], cand[1])
        if any(_is_same_route(route, u) for u in used):
            continue
        if _is_same_route(route, current_route):
            continue
        return cand
    return None
