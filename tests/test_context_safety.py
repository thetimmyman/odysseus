"""Emergency context-safety lifecycle tests (#1234).

Proves the eight required behaviours for the per-turn context headroom
enforcement that was missing from the multi-round agent loop:

 1. Tool output pushes a multi-round run across the compaction threshold ->
    compaction occurs before the next call.
 2. Context is checked on every round, not just round 1.
 3. max_tokens=4096 never results in a 2048-token output reservation.
 4. Reasoning-capable Qwen receives a large configured generation reserve.
 5. A turn cannot begin above the configured hard input percentage.
 6. If compaction cannot create enough headroom, the provider call is blocked
    with CONTEXT_BLOCKED.
 7. Repeated tool-heavy rounds compact and continue without reaching 98-99%.
 8. A simulated long-reasoning completion has sufficient reserved headroom and
    does not begin from a near-full prompt.
"""

import asyncio
import json
from unittest.mock import MagicMock

import pytest

import src.context_safety as cs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect(gen):
    async def _run():
        return [c async for c in gen]
    return asyncio.run(_run())


def _events(chunks):
    out = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                out.append(json.loads(c[6:]))
            except Exception:
                pass
    return out


def _patch_loop(monkeypatch, *, window, tool_result_chars, max_rounds,
                max_tokens=4096, compact_works=True, settings_overrides=None):
    """Wire a hermetic but real multi-round ``stream_agent_loop`` run.

    The fake model emits a *distinct* fenced bash block every round (so the
    stall/loop-breaker never trips) and tool output is sized by
    ``tool_result_chars`` to push context pressure. Real token estimation is
    kept in place; only the provider, the tool executor, and compaction are
    stubbed.

    Returns ``(run, checks, compact_state, model_calls)``:
      run()         -> decoded SSE events
      checks        -> one dict per per-round safety check (pre-estimate + decision)
      compact_state -> {"calls": N} compaction attempts
      model_calls   -> {"n": N} real model invocations
    """
    import src.agent_loop as al
    import src.context_compactor as cc

    overrides = dict(settings_overrides or {})
    monkeypatch.setattr(al, "get_setting",
                        lambda key, default=None: overrides.get(key, default),
                        raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)

    async def _fake_exec(block, *a, **k):
        return ("bash", {"output": "A" * tool_result_chars, "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    # Teacher escalation is a post-loop side channel; keep it out of the way.
    try:
        import src.teacher_escalation as te

        async def _no_teacher(*a, **k):
            if False:
                yield ""
        monkeypatch.setattr(te, "run_teacher_inline", _no_teacher, raising=False)
    except Exception:
        pass

    model_calls = {"n": 0}

    async def _fake_stream(_candidates, messages, **kwargs):
        model_calls["n"] += 1
        cmd = f"echo step-{model_calls['n']}"
        yield 'data: ' + json.dumps({"delta": f"```bash\n{cmd}\n```"}) + "\n\n"
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream,
                        raising=False)

    compact_state = {"calls": 0}

    async def _fake_maybe_compact(session, endpoint_url, model, msgs,
                                  headers=None, owner=None):
        compact_state["calls"] += 1
        if not compact_works:
            return list(msgs), window, False
        out = [m for m in msgs if m.get("role") == "system"][:1]
        out.append({"role": "system", "content": "[compacted summary]"})
        convo = [m for m in msgs if m.get("role") != "system"]
        out.extend(convo[-2:])
        return out, window, True
    monkeypatch.setattr(cc, "maybe_compact", _fake_maybe_compact, raising=False)

    real_enforce = al.enforce_context_safety
    checks = []

    async def _spy(msgs, eff_window, **kwargs):
        factor = kwargs.get("safety_factor", cs.DEFAULT_ESTIMATION_SAFETY_FACTOR)
        pre = cs.estimate_safe_input(msgs, factor)
        result = await real_enforce(msgs, eff_window, **kwargs)
        # enforce_context_safety mutates `msgs` in place, so this second
        # estimate is the prompt size the model ACTUALLY starts from.
        post = cs.estimate_safe_input(msgs, factor)
        checks.append({
            "pre_input": pre,
            "post_input": post,
            "state": result.state,
            "reserve": result.generation_reserve,
            "compacted": result.compacted,
            "window": result.effective_context_window,
            "messages": len(msgs),
        })
        return result

    monkeypatch.setattr(al, "enforce_context_safety", _spy, raising=False)

    def run():
        gen = al.stream_agent_loop(
            "http://x/v1", "m",
            [{"role": "system", "content": "You are an agent."},
             {"role": "user", "content": "run several tool steps"}],
            max_rounds=max_rounds,
            relevant_tools={"bash"},
            context_length=window,
            max_tokens=max_tokens,
        )
        return _events(_collect(gen))

    return run, checks, compact_state, model_calls


# ---------------------------------------------------------------------------
# Budget semantics / reserve (defect 2, 3, 4)
# ---------------------------------------------------------------------------

def test_max_tokens_4096_never_reserves_only_2048():
    """Defect 2: the old cap `min(max(max_tokens or 1024, 512), 2048)` reserved
    2048 while requesting max_tokens=4096 — self-defeating. The reserve must
    never drop below the requested output size."""
    for window in (8192, 32768, 131072):
        reserve = cs.compute_generation_reserve(window, max_tokens=4096)
        assert reserve >= 4096, (window, reserve)
        assert reserve != 2048


def test_reasoning_qwen_gets_a_large_generation_reserve():
    """Defect 3/4: a reasoning-capable local Qwen must reserve a *large* chunk
    of the window for output + internal reasoning, not just max_tokens."""
    window = 32768
    reserve = cs.compute_generation_reserve(window, max_tokens=4096)
    assert reserve == int(window * cs.DEFAULT_GEN_RESERVE_PCT)  # 25% of 32k
    assert reserve >= int(window * 0.25)
    # And the same reserve is what the headroom check applies.
    result = cs.check_context_headroom(
        [{"role": "user", "content": "hi"}], window, max_tokens=4096
    )
    assert result.generation_reserve >= int(window * 0.25)


def test_generation_reserve_is_never_below_max_tokens():
    """Reasoning reserve is not merely max_tokens, but it is never *less*."""
    window = 4096
    assert cs.compute_generation_reserve(window, max_tokens=4096) >= 4096
    # A big absolute/pct floor also wins when it exceeds max_tokens.
    assert cs.compute_generation_reserve(
        32768, max_tokens=512, absolute_reserve=8192, pct_reserve=0.25
    ) == 8192


def test_estimation_safety_factor_inflates_input_estimate():
    """Defect 4 (approx estimator): estimates must be inflated by the safety
    factor so the approximate `chars*0.3` heuristic is not trusted as exact."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "x" * 3000},
    ]
    raw = cs.estimate_safe_input(msgs, safety_factor=1.0)
    inflated = cs.estimate_safe_input(msgs, safety_factor=1.10)
    assert inflated > raw
    assert inflated == int(raw * 1.10)


def test_hard_input_fraction_blocks_a_turn():
    """Requirement 5: a turn cannot begin with input above the configured hard
    percentage even when the reserve would still fit."""
    window = 16384
    # ~95% of the window of raw content (safe estimate lands well above 0.70).
    raw = "x" * int(window * 0.95 / 0.3)
    result = cs.check_context_headroom(
        [{"role": "system", "content": "sys"},
         {"role": "user", "content": raw}],
        window,
        max_tokens=512,
    )
    assert result.state == cs.STATE_BLOCKED
    assert "hard limit" in result.reason


def test_unknown_window_is_not_falsely_blocked():
    result = cs.check_context_headroom(
        [{"role": "user", "content": "x" * 100000}], 0, max_tokens=4096
    )
    assert result.state == cs.STATE_OK


# ---------------------------------------------------------------------------
# enforce_context_safety lifecycle (requirements 4-7)
# ---------------------------------------------------------------------------

def _enforce(messages, window, *, max_tokens=4096, compact_fn=None,
             **kwargs):
    return asyncio.run(cs.enforce_context_safety(
        messages,
        window,
        model="qwen3-27b",
        endpoint_url="http://x/v1",
        headers=None,
        owner=None,
        max_tokens=max_tokens,
        _compact_fn=compact_fn,
        **kwargs,
    ))


def test_enforce_compacts_and_succeeds_when_headroom_is_recoverable():
    """Requirement 4/7: over the compaction threshold -> compact -> trim ->
    recheck -> OK."""
    window = 32768
    messages = [{"role": "system", "content": "sys"}]
    messages += [{"role": "user", "content": "x" * 12000} for _ in range(10)]

    async def _compact(session, endpoint_url, model, msgs, headers=None,
                       owner=None):
        return [{"role": "system", "content": "[summary]"}], window, True

    result = _enforce(messages, window, max_tokens=2048, compact_fn=_compact)
    assert result.state == cs.STATE_OK
    assert result.compacted is True


def test_enforce_blocks_when_compaction_and_trim_cannot_create_headroom():
    """Requirement 6: after compaction + trim there is still no headroom ->
    CONTEXT_BLOCKED (never invoke the model)."""
    window = 32768
    # A _protected message is counted against the budget but never dropped or
    # truncated by trim_for_context, so neither compaction nor trimming can
    # recover the headroom.
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "P" * 100000, "_protected": True},
    ]

    async def _compact_that_cannot_help(session, endpoint_url, model, msgs,
                                        headers=None, owner=None):
        return list(msgs), window, False

    result = _enforce(messages, window, max_tokens=4096,
                      compact_fn=_compact_that_cannot_help)
    assert result.state == cs.STATE_BLOCKED
    assert result.safe_input_tokens + result.generation_reserve \
        > result.effective_context_window


def test_enforce_blocks_when_reserve_alone_consumes_window():
    """Requirement 5/6: when the generation reserve alone claims the window,
    the invariant `input + reserve <= window` is unsatisfiable -> block."""
    window = 4096
    messages = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "hello"}]
    result = _enforce(messages, window, max_tokens=4096)
    assert result.state == cs.STATE_BLOCKED
    assert result.state == "CONTEXT_BLOCKED"



def test_compaction_threshold_gates_the_expensive_summarizer_but_trim_recovers():
    """The configured compaction threshold decides when the (LLM-costing)
    summariser runs; below it the cheap, deterministic trim still has to
    recover the turn — otherwise a recoverable turn would be blocked."""
    window = 8192
    # 3 user messages ~4500 chars -> safe estimate ~0.55 of the window. With the
    # 4096-token reserve (50% of this small window) headroom fails, but the
    # input fraction is BELOW the 0.62 compaction threshold.
    messages = [{"role": "system", "content": "sys"}]
    messages += [{"role": "user", "content": "y" * 4500} for _ in range(3)]
    before = len(messages)

    calls = {"compact": 0}

    async def _compact(session, endpoint_url, model, msgs, headers=None,
                       owner=None):
        calls["compact"] += 1
        return list(msgs), window, True

    result = _enforce(messages, window, max_tokens=2048, compact_fn=_compact)

    assert calls["compact"] == 0, "expensive summariser ran below the threshold"
    assert result.compacted is False
    # ...but the cheap trim still ran and recovered the turn.
    assert result.state == cs.STATE_OK
    assert len(messages) < before


def test_context_blocked_reports_round_and_reason_for_telemetry():
    """CONTEXT_BLOCKED must carry machine-readable detail (thresholds are
    meant to be raised later once telemetry proves the real limits)."""
    window = 4096
    result = _enforce(
        [{"role": "system", "content": "sys"},
         {"role": "user", "content": "hi"}],
        window, max_tokens=4096,
    )
    assert result.state == cs.STATE_BLOCKED
    assert result.effective_context_window == window
    assert result.generation_reserve >= window
    assert result.reason


# ---------------------------------------------------------------------------
# Multi-round agent-loop wiring (defect 1, requirements 1/2/7/8)
# ---------------------------------------------------------------------------

def test_context_is_checked_on_every_round_not_just_round_1(monkeypatch):
    """Defect 1 / requirement 2: context maintenance runs before EVERY model
    invocation, so one safety check per round."""
    run, checks, _, model_calls = _patch_loop(
        monkeypatch, window=131072, tool_result_chars=200, max_rounds=5,
    )
    run()
    assert model_calls["n"] == 5
    assert len(checks) == 5, checks
    assert all(c["state"] == cs.STATE_OK for c in checks)


def test_tool_output_pushes_run_past_threshold_and_compacts_before_next_call(
        monkeypatch):
    """Requirement 1: a tool result that pushes the run over the compaction
    threshold triggers compaction BEFORE the next model invocation."""
    run, checks, compact_state, model_calls = _patch_loop(
        monkeypatch, window=16384, tool_result_chars=12000, max_rounds=6,
        max_tokens=2048,
    )
    run()
    assert compact_state["calls"] >= 1, "compaction never ran"
    # The very first round had plenty of room -> it must NOT have compacted.
    assert checks[0]["compacted"] is False
    # Compaction happened on a later round, driven by appended tool output.
    assert any(c["compacted"] for c in checks), checks
    assert model_calls["n"] == 6


def test_repeated_tool_heavy_rounds_compact_and_never_reach_death_zone(
        monkeypatch):
    """Requirement 7: repeated tool-heavy rounds keep compacting and continue
    instead of climbing to the 98-99% context death zone."""
    run, checks, compact_state, model_calls = _patch_loop(
        monkeypatch, window=16384, tool_result_chars=12000, max_rounds=12,
        max_tokens=2048,
    )
    events = run()
    assert compact_state["calls"] >= 2, compact_state
    assert model_calls["n"] == 12
    assert not any(e.get("type") == "error" for e in events), events
    for c in checks:
        assert c["state"] == cs.STATE_OK, c
        # The prompt the model ACTUALLY started from is always under the hard
        # limit and always leaves the generation reserve free. The pre-check
        # pressure may legitimately spike when a big tool result lands — that
        # spike is exactly what triggers compaction.
        assert c["post_input"] <= cs.DEFAULT_HARD_INPUT_FRACTION * c["window"], c
        assert c["post_input"] + c["reserve"] <= c["window"], c
    # And the run never climbs into the 98-99% death zone that killed the
    # original tool-heavy runs.
    assert max(c["post_input"] / c["window"] for c in checks) < 0.90


def test_loop_blocks_with_context_blocked_and_never_calls_model(monkeypatch):
    """Requirement 5/6: when no headroom can be created (reserve == window),
    the loop emits CONTEXT_BLOCKED and does NOT invoke the model."""
    run, checks, compact_state, model_calls = _patch_loop(
        monkeypatch, window=4096, tool_result_chars=100, max_rounds=3,
        max_tokens=4096,
    )
    events = run()
    assert model_calls["n"] == 0, "model was invoked despite CONTEXT_BLOCKED"
    errs = [e for e in events if e.get("type") == "error"]
    assert errs, events
    assert "CONTEXT_BLOCKED" in json.dumps(errs[0])
    assert errs[0]["context_state"]["round"] == 1
    assert checks and checks[0]["state"] == cs.STATE_BLOCKED
    # The user sees WHY, and the end-of-loop empty-response guard did not
    # overwrite the real reason.
    deltas = "".join(e.get("delta", "") for e in events)
    assert "CONTEXT_BLOCKED" in deltas, deltas
    assert "returned an empty response" not in deltas, deltas
    # No "Continue" affordance is offered for a deliberate block.
    assert not any(e.get("type") == "rounds_exhausted" for e in events), events


def test_reasoning_model_turn_starts_with_reserved_generation_headroom(
        monkeypatch):
    """Requirement 8: a reasoning-capable turn must not begin from a near-full
    prompt — every invocation keeps a large reserve free (>=25% here)."""
    window = 32768
    run, checks, _, _ = _patch_loop(
        monkeypatch, window=window, tool_result_chars=200, max_rounds=3,
    )
    run()
    assert checks
    for c in checks:
        assert c["reserve"] >= int(window * 0.25), c
        # The invariant the model actually started from: the prompt that goes
        # to the provider plus the generation/reasoning reserve must fit.
        assert c["post_input"] + c["reserve"] <= c["window"], c
        # And it must not begin from a near-full prompt.
        assert c["post_input"] <= cs.DEFAULT_HARD_INPUT_FRACTION * c["window"], c


def test_thresholds_and_reserves_are_configurable_not_hardcoded(monkeypatch):
    """Requirements: thresholds/reserve/safety factor come from settings."""
    # Raise the limits via settings; the loop must honour them (no error even
    # with the same tool-heavy load that compacts under the conservative
    # defaults).
    run, checks, _, model_calls = _patch_loop(
        monkeypatch, window=8192, tool_result_chars=2000, max_rounds=6,
        max_tokens=1024,
        settings_overrides={
            "agent_context_compaction_threshold": 0.90,
            "agent_context_hard_input_fraction": 0.95,
            "agent_generation_reserve_pct": 0.01,
            "agent_generation_reserve_absolute": 256,
            "agent_token_estimation_safety_factor": 1.0,
        },
    )
    events = run()
    assert model_calls["n"] == 6
    assert not any(e.get("type") == "error" for e in events), events
    # reserve is the max(absolute, pct*window, max_tokens) = max(256, 82, 1024)
    assert all(c["reserve"] == 1024 for c in checks), checks

