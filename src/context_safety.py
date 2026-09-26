"""Per-turn context-safety lifecycle for the multi-round agent loop.

The headroom check runs before every working-model call and refuses the call
when there isn't room to generate (and, for reasoning models, to think).

    effective_context_window  — the model's real context window; the ceiling,
                                never an input budget.
    safe_input_tokens         — estimated input tokens, inflated by
                                estimation_safety_factor (the estimator
                                underestimates).
    generation_reserve        — tokens reserved for output + reasoning: max of
                                an absolute floor, a pct of context and the
                                requested max_tokens.
    hard_input_fraction       — max fraction of the window input may occupy;
                                above it the turn is CONTEXT_BLOCKED.

Invariant (check_context_headroom):

    safe_input_tokens + generation_reserve <= effective_context_window
    AND
    safe_input_tokens <= hard_input_fraction * effective_context_window

If either fails after compaction/trim, the model must not be called.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from src.model_context import estimate_tokens, get_context_length

logger = logging.getLogger(__name__)

# Conservative defaults for reasoning-capable local Qwen; raise once telemetry
# proves the real limits.
DEFAULT_COMPACTION_THRESHOLD = 0.62
DEFAULT_HARD_INPUT_FRACTION = 0.70
DEFAULT_GEN_RESERVE_PCT = 0.25
DEFAULT_GEN_RESERVE_ABSOLUTE = 4096
DEFAULT_ESTIMATION_SAFETY_FACTOR = 1.10

STATE_OK = "ok"
STATE_COMPACT = "compact"
STATE_BLOCKED = "CONTEXT_BLOCKED"


@dataclass
class ContextSafetyResult:
    """Outcome of a context-safety check for one model turn."""

    state: str
    effective_context_window: int
    safe_input_tokens: int
    generation_reserve: int
    compacted: bool
    reason: str


def compute_generation_reserve(
    effective_context_window: int,
    *,
    max_tokens: int,
    absolute_reserve: int = DEFAULT_GEN_RESERVE_ABSOLUTE,
    pct_reserve: float = DEFAULT_GEN_RESERVE_PCT,
) -> int:
    """Tokens that must stay free: max of the floor, pct of window and ``max_tokens``."""
    by_pct = int(effective_context_window * pct_reserve)
    reserve = max(absolute_reserve, by_pct)
    reserve = max(reserve, max_tokens or 0)
    return reserve


def estimate_safe_input(
    messages: List[Dict],
    safety_factor: float = DEFAULT_ESTIMATION_SAFETY_FACTOR,
) -> int:
    """Conservative input-token estimate (inflated by safety_factor)."""
    return int(estimate_tokens(messages) * safety_factor)


def check_context_headroom(
    messages: List[Dict],
    effective_context_window: int,
    *,
    max_tokens: int,
    hard_input_fraction: float = DEFAULT_HARD_INPUT_FRACTION,
    absolute_reserve: int = DEFAULT_GEN_RESERVE_ABSOLUTE,
    pct_reserve: float = DEFAULT_GEN_RESERVE_PCT,
    safety_factor: float = DEFAULT_ESTIMATION_SAFETY_FACTOR,
) -> ContextSafetyResult:
    """Pure headroom check returning STATE_OK or STATE_BLOCKED. An unknown window
    (<= 0) can't be enforced, so it is OK."""
    if effective_context_window <= 0:
        return ContextSafetyResult(
            state=STATE_OK,
            effective_context_window=0,
            safe_input_tokens=0,
            generation_reserve=max_tokens or absolute_reserve,
            compacted=False,
            reason="unknown context window; headroom check skipped",
        )

    safe_input = estimate_safe_input(messages, safety_factor)
    gen_reserve = compute_generation_reserve(
        effective_context_window,
        max_tokens=max_tokens,
        absolute_reserve=absolute_reserve,
        pct_reserve=pct_reserve,
    )
    input_fraction = safe_input / effective_context_window

    headroom_ok = (safe_input + gen_reserve) <= effective_context_window
    input_ok = input_fraction <= hard_input_fraction

    if headroom_ok and input_ok:
        return ContextSafetyResult(
            state=STATE_OK,
            effective_context_window=effective_context_window,
            safe_input_tokens=safe_input,
            generation_reserve=gen_reserve,
            compacted=False,
            reason="headroom ok",
        )

    reason_parts = []
    if not headroom_ok:
        reason_parts.append(
            "input({})+reserve({})={} > window({})".format(
                safe_input, gen_reserve, safe_input + gen_reserve,
                effective_context_window,
            )
        )
    if not input_ok:
        reason_parts.append(
            "input fraction {:.3f} > hard limit {}".format(
                input_fraction, hard_input_fraction,
            )
        )
    return ContextSafetyResult(
        state=STATE_BLOCKED,
        effective_context_window=effective_context_window,
        safe_input_tokens=safe_input,
        generation_reserve=gen_reserve,
        compacted=False,
        reason="; ".join(reason_parts),
    )


async def enforce_context_safety(
    messages: List[Dict],
    effective_context_window: int,
    *,
    model: str,
    endpoint_url: str,
    headers: Optional[Dict],
    owner: Optional[str],
    max_tokens: int,
    compaction_threshold: float = DEFAULT_COMPACTION_THRESHOLD,
    hard_input_fraction: float = DEFAULT_HARD_INPUT_FRACTION,
    absolute_reserve: int = DEFAULT_GEN_RESERVE_ABSOLUTE,
    pct_reserve: float = DEFAULT_GEN_RESERVE_PCT,
    safety_factor: float = DEFAULT_ESTIMATION_SAFETY_FACTOR,
    _compact_fn=None,
) -> ContextSafetyResult:
    """Per-turn lifecycle: check -> compact -> trim -> recheck.

    Mutates ``messages`` in place. Callers must not invoke the model when the
    result is STATE_BLOCKED. ``_compact_fn`` is a test seam.
    """
    if effective_context_window <= 0:
        try:
            effective_context_window = get_context_length(endpoint_url, model) or 0
        except Exception:
            effective_context_window = 0

    result = check_context_headroom(
        messages,
        effective_context_window,
        max_tokens=max_tokens,
        hard_input_fraction=hard_input_fraction,
        absolute_reserve=absolute_reserve,
        pct_reserve=pct_reserve,
        safety_factor=safety_factor,
    )
    # Recover proactively once pressure crosses the compaction threshold, even
    # while still safe; waiting until the hard limit leaves trim unable to recover.
    _over_compaction_threshold = (
        result.safe_input_tokens
        >= compaction_threshold * effective_context_window
    )
    if result.state == STATE_OK and not _over_compaction_threshold:
        return result

    # If the reserve alone fills the window no input fits, so skip straight to
    # blocking rather than compacting for nothing.
    if result.generation_reserve >= effective_context_window:
        return result

    # Compact (expensive, threshold-gated), then always trim (cheap), then
    # recheck. maybe_compact gets the same effective window so gates can't diverge.
    did_compact = False
    try:
        # Lazy import avoids a cycle and lets tests patch maybe_compact.
        from src.context_compactor import maybe_compact, trim_for_context

        if _over_compaction_threshold:
            if _compact_fn is not None:
                compacted_msgs, new_ctx, was_compacted = await _compact_fn(
                    None, endpoint_url, model, list(messages), headers, owner=owner,
                )
            else:
                # Use the effective window, not the raw serving window, or the
                # 0.85 gate would never fire.
                compacted_msgs, new_ctx, was_compacted = await maybe_compact(
                    None, endpoint_url, model, list(messages), headers,
                    owner=owner, context_length=effective_context_window,
                )
            if was_compacted:
                did_compact = True
                if new_ctx > 0:
                    effective_context_window = new_ctx
                messages[:] = compacted_msgs

        # Trim so the inflated estimate satisfies both recheck conditions,
        # targeting the smaller bound in raw tokens:
        #   (a) input bound:   safety_factor * estimate <= target_fraction * W
        #   (b) headroom bound: safety_factor * estimate <= W - generation_reserve
        _target_fraction = min(compaction_threshold, hard_input_fraction)
        _gen_reserve_now = compute_generation_reserve(
            effective_context_window,
            max_tokens=max_tokens,
            absolute_reserve=absolute_reserve,
            pct_reserve=pct_reserve,
        )
        _feasible_input = min(
            _target_fraction * effective_context_window,
            effective_context_window - _gen_reserve_now,
        )
        _trim_target_raw = int(
            max(0.0, _feasible_input) / max(safety_factor, 1.0)
        )
        _trim_reserve = max(0, effective_context_window - _trim_target_raw)
        messages[:] = trim_for_context(
            list(messages),
            effective_context_window,
            reserve_tokens=_trim_reserve,
        )
    except Exception as e:
        logger.warning("[context-safety] compaction/trim failed: %s", e)
        # Fall through to the recheck; with compaction failed we block.

    result2 = check_context_headroom(
        messages,
        effective_context_window,
        max_tokens=max_tokens,
        hard_input_fraction=hard_input_fraction,
        absolute_reserve=absolute_reserve,
        pct_reserve=pct_reserve,
        safety_factor=safety_factor,
    )
    return ContextSafetyResult(
        state=result2.state if result2.state == STATE_OK else STATE_BLOCKED,
        effective_context_window=effective_context_window,
        safe_input_tokens=result2.safe_input_tokens,
        generation_reserve=result2.generation_reserve,
        compacted=did_compact,
        reason=result2.reason,
    )
