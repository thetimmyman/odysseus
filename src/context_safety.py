"""Emergency context-safety lifecycle for the multi-round agent loop.

The agent loop used to check/trim context ONCE before round 1 and then
append tool results every round with no further check — so tool-heavy runs
grew the prompt until the model hit 98-99% context and died. This module
defines the budget semantics and the per-turn headroom check that MUST run
before *every* working-model invocation, and refuses to call the model when
there isn't room to generate (and, for reasoning models, to *think*).

Semantics — unambiguous names, because the old code confused them:

    effective_context_window  — the model's real context window (total tokens
                                the KV cache / API accepts). NEVER an input
                                budget. This is the hard ceiling everything
                                else is measured against.
    safe_input_tokens         — estimated input tokens, inflated by the
                                estimation_safety_factor because the estimator
                                (chars*0.3) is approximate and underestimates.
    generation_reserve        — tokens reserved for output + internal
                                reasoning. The larger of (absolute floor) and
                                (pct of context); never less than the caller's
                                requested max_tokens, because you obviously
                                cannot generate max_tokens if you reserve less.
    hard_input_fraction       — the maximum fraction of the context window that
                                input may occupy. Above this, even compaction
                                cannot save the turn -> CONTEXT_BLOCKED.

Hard invariant (enforced in check_context_headroom):

    safe_input_tokens + generation_reserve <= effective_context_window
    AND
    safe_input_tokens <= hard_input_fraction * effective_context_window

If either is false AFTER compaction/trim, the model must NOT be called.
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from src.model_context import estimate_tokens, get_context_length

logger = logging.getLogger(__name__)

# Conservative temporary defaults for reasoning-capable local Qwen.
# Intentionally low; raise after telemetry proves the real safe limits.
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
    """Tokens that must remain free for output + reasoning.

    Returns the largest of the absolute floor, the percentage of the context
    window, and the caller's requested ``max_tokens``.
    """
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
    """Pure headroom check. No compaction, no mutation.

    Returns a ContextSafetyResult whose ``state`` is either STATE_OK or
    STATE_BLOCKED. An unknown context window (<= 0) cannot be enforced, so
    it is treated as OK to preserve legacy behaviour for callers that
    cannot resolve their window.
    """
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
    """Full per-turn context lifecycle: check -> compact -> trim -> recheck.

    Mutates ``messages`` in place (compaction/trim replace its contents) and
    returns the final ContextSafetyResult. Callers MUST inspect ``.state``
    and MUST NOT invoke the model when it is STATE_BLOCKED.

    ``_compact_fn`` is a seam for hermetic tests; when omitted the real
    src.context_compactor.maybe_compact is used.
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
    # Proactive recovery: enter the lifecycle as soon as pressure CROSSES the
    # configured compaction threshold, even while the turn is still safe. The
    # old code returned immediately on STATE_OK, so compaction/trim only ever
    # ran AFTER the hard boundary was already crossed — by which point the
    # deterministic trim target (below) could no longer bring the prompt back
    # under that boundary, and a tool-heavy run climbed monotonically to
    # CONTEXT_BLOCKED instead of compacting (#1234 follow-up).
    _over_compaction_threshold = (
        result.safe_input_tokens
        >= compaction_threshold * effective_context_window
    )
    if result.state == STATE_OK and not _over_compaction_threshold:
        return result

    # Over the hard limit AND provably unsatisfiable: if the generation reserve
    # alone consumes the whole window, `input + reserve <= window` is false for
    # ANY input, so compacting/trimming would burn work to reach the same block.
    # (When STATE_OK this cannot hold: headroom_ok implies reserve < window.)
    if result.generation_reserve >= effective_context_window:
        return result

    # Attempt compaction (the EXPENSIVE path, gated on compaction_threshold),
    # then ALWAYS trim stale/evictable history (the cheap, deterministic path).
    # Below the threshold the trim alone is the recovery; above it
    # maybe_compact summarises the older half AND the trim still runs, and the
    # invariant is re-checked afterwards. maybe_compact is given the SAME
    # effective window budgeted here, so its own gate cannot silently diverge.
    did_compact = False
    try:
        # Imported here (not at module scope) to avoid a circular import and so
        # tests can patch src.context_compactor.maybe_compact.
        from src.context_compactor import maybe_compact, trim_for_context

        if _over_compaction_threshold:
            if _compact_fn is not None:
                compacted_msgs, new_ctx, was_compacted = await _compact_fn(
                    None, endpoint_url, model, list(messages), headers, owner=owner,
                )
            else:
                # Evaluate compaction against the SAME effective window the
                # safety guard budgets against — not the model's raw serving
                # window (which can be far larger, so the internal 0.85 gate
                # would never fire and compaction would be dead).
                compacted_msgs, new_ctx, was_compacted = await maybe_compact(
                    None, endpoint_url, model, list(messages), headers,
                    owner=owner, context_length=effective_context_window,
                )
            if was_compacted:
                did_compact = True
                if new_ctx > 0:
                    effective_context_window = new_ctx
                messages[:] = compacted_msgs

        # Trim stale/evictable history so the *inflated* estimate satisfies
        # BOTH viability conditions the recheck enforces, targeting the smaller
        # bound in RAW (pre-inflation) tokens:
        #   (a) input bound:   safety_factor * estimate <= target_fraction * W
        #   (b) headroom bound: safety_factor * estimate <= W - generation_reserve
        # BUDGET SEMANTICS: the old budget was
        # `(window - generation_reserve)/safety_factor` but the trim was only
        # reachable AFTER the hard limit was crossed, and with the default 25%
        # reserve that budget (0.68*window) is ABOVE the hard limit
        # (0.70/1.10 = 0.64*window) — so the trim could never fire before
        # CONTEXT_BLOCKED. Deriving the target from these explicit bounds makes
        # the trim the recovery that actually reduces context (#1234).
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

    # Recheck after compaction/trim.
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
