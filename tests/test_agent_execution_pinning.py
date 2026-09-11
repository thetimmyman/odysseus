"""Execution-identity pinning for the agent loop.

Reliability invariant under test:

    An Odysseus agent execution has ONE pinned execution identity. The harness
    may replace that identity only through an explicit, auditable transition.

These tests drive a *real*, hermetic multi-round ``stream_agent_loop`` run
(only the provider stream, the tool executor and the teacher side-channel are
stubbed) and prove:

  A. every model invocation in a multi-round run uses the same
     provider/model/endpoint/execution id;
  B. a tool-result continuation reuses the original pinned target;
  C. an ordinary transient retry reuses the same target (a retry is not an
     escalation);
  D. a normal round never re-resolves the route (no routing engine / endpoint
     resolver call, no fallback candidate invoked);
  E. an approved fallback creates a NEW execution identity and records the
     transition;
  F. a failed provider with no approved fallback fails explicitly instead of
     silently switching models;
  G. context-window / tool-profile assumptions stay attached to the pinned
     target across rounds;
  H. telemetry identifies exactly which rounds ran under which target.
"""

import asyncio
import json

import pytest

import src.agent_execution as ae
import src.agent_loop as al


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


def _of_type(events, kind):
    return [e for e in events if e.get("type") == kind]


def _delta(text):
    return 'data: ' + json.dumps({"delta": text}) + "\n\n"


def _tool_round(cmd):
    return _delta(f"```bash\n{cmd}\n```")


_DONE = "data: [DONE]\n\n"


def _provider_error(status, text="down"):
    return 'event: error\ndata: ' + json.dumps({"status": status, "text": text}) + "\n\n"


def _patch_basics(monkeypatch, settings=None):
    """Keep the real loop body; stub settings, MCP, the tool executor and the
    post-loop teacher side-channel."""
    overrides = dict(settings or {})
    monkeypatch.setattr(
        al, "get_setting",
        lambda key, default=None: overrides.get(key, default),
        raising=False,
    )
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)

    async def _fake_exec(block, *a, **k):
        return ("bash", {"output": "ok", "exit_code": 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    try:
        import src.teacher_escalation as te

        async def _no_teacher(*a, **k):
            if False:
                yield ""
        monkeypatch.setattr(te, "run_teacher_inline", _no_teacher, raising=False)
    except Exception:
        pass


def _patch_stream(monkeypatch, script):
    """Stub ``al.stream_llm_with_fallback`` and record every invocation.

    ``script(call_index, candidates, messages, kwargs)`` returns the SSE chunks
    for that invocation. Returns the ``calls`` list, each entry carrying the
    exact candidate chain the loop asked for plus a snapshot of the messages.
    """
    calls = []

    async def _fake_stream(candidates, messages, **kwargs):
        idx = len(calls)
        calls.append({
            "candidates": [tuple(c) for c in candidates],
            "messages": list(messages),
            "kwargs": kwargs,
        })
        for chunk in script(idx, candidates, list(messages), kwargs):
            yield chunk

    monkeypatch.setattr(al, "stream_llm_with_fallback", _fake_stream, raising=False)
    return calls


def _routes(calls):
    return [c["candidates"][0][:2] for c in calls]


_PRIMARY = ("http://127.0.0.1:11434/v1", "qwen3.8-27b")
_BACKUP = ("http://backup.example/v1", "backup-model")


def _run(messages=None, **kwargs):
    kwargs.setdefault("max_rounds", 3)
    kwargs.setdefault("relevant_tools", {"bash"})
    kwargs.setdefault("context_length", 131072)
    kwargs.setdefault("max_tokens", 4096)
    gen = al.stream_agent_loop(
        _PRIMARY[0], _PRIMARY[1],
        messages or [{"role": "user", "content": "run several tool steps"}],
        **kwargs,
    )
    return _events(_collect(gen))


# ---------------------------------------------------------------------------
# Test A — same target across rounds
# ---------------------------------------------------------------------------

def test_a_same_execution_identity_across_rounds(monkeypatch):
    _patch_basics(monkeypatch)

    def script(idx, candidates, messages, kwargs):
        return [_tool_round(f"echo step-{idx + 1}"), _DONE]

    calls = _patch_stream(monkeypatch, script)
    events = _run()
    assert len(calls) == 3, calls
    assert set(_routes(calls)) == {_PRIMARY}, _routes(calls)
    # ... and never a multi-candidate chain (that is what allowed the silent
    # per-round reroute this patch removes).
    assert all(len(c["candidates"]) == 1 for c in calls), calls

    targets = _of_type(events, "execution_target")
    assert len(targets) == 1, events
    eid = targets[0]["data"]["execution_id"]
    assert targets[0]["data"]["model"] == _PRIMARY[1]
    assert targets[0]["data"]["endpoint"] == _PRIMARY[0]

    steps = _of_type(events, "agent_step")
    assert len(steps) == 3, steps
    assert {s.get("execution_id") for s in steps} == {eid}


# ---------------------------------------------------------------------------
# Test B — tool continuation
# ---------------------------------------------------------------------------

def test_b_tool_continuation_reuses_pinned_target(monkeypatch):
    _patch_basics(monkeypatch)

    def script(idx, candidates, messages, kwargs):
        if idx == 0:
            return [_tool_round("echo first"), _DONE]
        return [_delta("All done."), _DONE]

    calls = _patch_stream(monkeypatch, script)
    events = _run(max_rounds=2)

    assert len(calls) == 2
    # The tool result from round 1 was appended before round 2 reasoned.
    assert len(calls[1]["messages"]) > len(calls[0]["messages"])
    assert any("ok" in str(m.get("content")) for m in calls[1]["messages"]), calls[1]["messages"]
    # ... and round 2 still used the ORIGINAL pinned target.
    assert set(_routes(calls)) == {_PRIMARY}
    assert all(len(c["candidates"]) == 1 for c in calls)

    ids = {s.get("execution_id") for s in _of_type(events, "agent_step")}
    assert len(ids) == 1 and None not in ids


# ---------------------------------------------------------------------------
# Test C — retry identity
# ---------------------------------------------------------------------------

def test_c_transient_retry_reuses_same_target(monkeypatch):
    _patch_basics(monkeypatch)

    def script(idx, candidates, messages, kwargs):
        if idx < 2:
            return [_provider_error(503), _DONE]     # transient
        if idx == 2:
            return [_tool_round("echo recovered"), _DONE]
        return [_delta("All done."), _DONE]

    calls = _patch_stream(monkeypatch, script)
    events = _run(max_rounds=2)

    # 2 failed attempts + 1 successful attempt in round 1, then round 2.
    assert len(calls) == 4, calls
    assert set(_routes(calls)) == {_PRIMARY}, _routes(calls)

    retries = _of_type(events, "provider_retry")
    assert len(retries) == 2, retries
    assert {r["failure_class"] for r in retries} == {"provider_transient"}
    # A retry is NOT an escalation: no new execution identity was created.
    assert _of_type(events, "execution_transition") == []

    metrics = _of_type(events, "metrics")[0]["data"]
    assert len(metrics["executions"]) == 1
    assert metrics["executions"][0]["rounds"] == [1, 2]
    assert metrics["provider_failures"] == []


# ---------------------------------------------------------------------------
# Test D — no hidden rerouting
# ---------------------------------------------------------------------------

def test_d_normal_round_never_reroutes(monkeypatch):
    _patch_basics(monkeypatch)

    # Any attempt to ask the routing engine / endpoint resolver for a target
    # mid-run would be a regression.
    seen = []
    for modname, attr in (
        ("src.endpoint_resolver", "resolve_endpoint"),
        ("src.endpoint_resolver", "resolve_endpoint_by_id"),
        ("src.routing_engine", "route_task"),
    ):
        mod = __import__(modname, fromlist=[attr])
        monkeypatch.setattr(mod, attr, lambda *a, _n=f"{modname}.{attr}", **k: seen.append(_n))

    def script(idx, candidates, messages, kwargs):
        if idx == 0:
            return [_tool_round("echo one"), _DONE]
        return [_delta("All done."), _DONE]

    calls = _patch_stream(monkeypatch, script)
    events = _run(max_rounds=2, fallbacks=[_BACKUP + ({},)])

    assert seen == [], f"mid-run routing re-resolution: {seen}"
    # The configured fallback was NEVER used as a per-round candidate.
    assert set(_routes(calls)) == {_PRIMARY}, _routes(calls)
    assert all(len(c["candidates"]) == 1 for c in calls)
    assert _of_type(events, "execution_transition") == []
    assert _of_type(events, "fallback") == []

    metrics = _of_type(events, "metrics")[0]["data"]
    assert len(metrics["executions"]) == 1
    assert metrics["execution_id"] == metrics["executions"][0]["execution_id"]


# ---------------------------------------------------------------------------
# Test E — explicit fallback transition
# ---------------------------------------------------------------------------

def test_e_explicit_fallback_creates_new_execution(monkeypatch):
    _patch_basics(monkeypatch)

    def script(idx, candidates, messages, kwargs):
        if candidates[0][0] == _PRIMARY[0]:
            return [_provider_error(400, "bad request"), _DONE]   # permanent
        # The fallback target answers, then finishes on the next round.
        if candidates[0][1] == _BACKUP[1] and idx == 1:
            return [_tool_round("echo on-backup"), _DONE]
        return [_delta("All done on backup."), _DONE]

    calls = _patch_stream(monkeypatch, script)
    events = _run(max_rounds=3, fallbacks=[_BACKUP + ({},)])

    # One attempt on the primary, then every later invocation on the backup.
    assert _routes(calls)[0] == _PRIMARY, _routes(calls)
    assert set(_routes(calls)[1:]) == {_BACKUP}, _routes(calls)
    # A permanent failure is NOT retried on the same target.
    assert _of_type(events, "provider_retry") == []

    transitions = _of_type(events, "execution_transition")
    assert len(transitions) == 1, transitions
    tr = transitions[0]
    assert tr["from"]["execution_id"] != tr["to"]["execution_id"]
    assert tr["to"]["previous_execution_id"] == tr["from"]["execution_id"]
    assert tr["to"]["model"] == _BACKUP[1]
    assert tr["to"]["endpoint"] == _BACKUP[0]
    assert tr["failure_class"] == "provider_permanent"
    assert tr["to"]["selection_reason"] == "explicit_fallback"
    assert tr["reason"]

    # The client still learns which model actually answered.
    fb = _of_type(events, "fallback")
    assert fb and fb[0]["answered_by"] == _BACKUP[1]
    assert fb[0]["selected_model"] == _PRIMARY[1]

    targets = _of_type(events, "execution_target")
    assert len(targets) == 1
    assert targets[0]["data"]["execution_id"] == tr["from"]["execution_id"]

    metrics = _of_type(events, "metrics")[0]["data"]
    assert metrics["execution_id"] == tr["to"]["execution_id"]
    execs = {e["execution_id"]: e for e in metrics["executions"]}
    assert set(execs) == {tr["from"]["execution_id"], tr["to"]["execution_id"]}
    # The superseded target attempted round 1; the new one produced rounds 1-2.
    assert execs[tr["from"]["execution_id"]]["rounds"] == [1]
    assert execs[tr["to"]["execution_id"]]["rounds"] == [1, 2]
    assert metrics["provider_failures"][0]["failure_class"] == "provider_permanent"


# ---------------------------------------------------------------------------
# Test F — failed provider without fallback
# ---------------------------------------------------------------------------

def test_f_primary_failure_without_fallback_fails_explicitly(monkeypatch):
    _patch_basics(monkeypatch)

    def script(idx, candidates, messages, kwargs):
        return [_provider_error(503, "service unavailable"), _DONE]

    calls = _patch_stream(monkeypatch, script)
    events = _run(max_rounds=3)   # no fallbacks configured

    # Retries happened on the SAME target, then it gave up — never a switch.
    assert set(_routes(calls)) == {_PRIMARY}, _routes(calls)
    assert len(calls) == 3, calls            # 2 transient retries + 1 final attempt
    assert _of_type(events, "execution_transition") == []
    assert _of_type(events, "fallback") == []

    failures = _of_type(events, "provider_failure")
    assert len(failures) == 1, failures
    assert failures[0]["fallback_available"] is False
    assert failures[0]["failure_class"] == "provider_transient"
    assert failures[0]["model"] == _PRIMARY[1]

    # The turn ends explicitly with a visible message — no silent model swap.
    assert not _of_type(events, "rounds_exhausted")
    assert any(
        "Provider failure" in str(e.get("delta", ""))
        for e in events if "delta" in e
    ), events[-6:]

    metrics = _of_type(events, "metrics")[0]["data"]
    assert len(metrics["executions"]) == 1
    assert metrics["provider_failures"][0]["failure_class"] == "provider_transient"

# ---------------------------------------------------------------------------
# Test G — context/tool assumptions stay with the pinned target
# ---------------------------------------------------------------------------

def test_g_context_and_tool_profile_pinned(monkeypatch):
    _patch_basics(monkeypatch)

    real_enforce = al.enforce_context_safety
    seen = []

    async def _spy(msgs, window, **kwargs):
        seen.append({
            "model": kwargs.get("model"),
            "endpoint": kwargs.get("endpoint_url"),
            "window": window,
        })
        return await real_enforce(msgs, window, **kwargs)

    monkeypatch.setattr(al, "enforce_context_safety", _spy, raising=False)

    def script(idx, candidates, messages, kwargs):
        return [_tool_round(f"echo step-{idx + 1}"), _DONE]

    _patch_stream(monkeypatch, script)
    events = _run(max_rounds=3)

    target = _of_type(events, "execution_target")[0]["data"]
    assert target["context_window"] == 131072
    # Ollama OpenAI-compat (/v1 on :11434) must pin the fenced tool profile.
    assert target["tool_profile"] == ae.TOOL_PROFILE_FENCED

    # Every round's context accounting used the pinned target's window/model.
    assert [s["window"] for s in seen] == [131072] * 3, seen
    assert {s["model"] for s in seen} == {_PRIMARY[1]}
    assert {s["endpoint"] for s in seen} == {_PRIMARY[0]}

    metrics = _of_type(events, "metrics")[0]["data"]
    pinned = metrics["executions"][0]["target"]
    assert pinned["context_window"] == target["context_window"]
    assert pinned["tool_profile"] == target["tool_profile"]


# ---------------------------------------------------------------------------
# Test H — auditability (which rounds ran under which target)
# ---------------------------------------------------------------------------

def test_h_telemetry_maps_rounds_to_targets(monkeypatch):
    _patch_basics(monkeypatch)

    def script(idx, candidates, messages, kwargs):
        if candidates[0][0] == _PRIMARY[0]:
            return [_provider_error(400), _DONE]
        if idx == 1:
            return [_tool_round("echo b1"), _DONE]
        if idx == 2:
            return [_tool_round("echo b2"), _DONE]
        return [_delta("Backup finished."), _DONE]

    _patch_stream(monkeypatch, script)
    events = _run(max_rounds=4, fallbacks=[_BACKUP + ({},)])

    target = _of_type(events, "execution_target")[0]["data"]
    steps = _of_type(events, "agent_step")
    transitions = _of_type(events, "execution_transition")
    assert len(transitions) == 1
    assert transitions[0]["from"]["execution_id"] == target["execution_id"]
    new_id = transitions[0]["to"]["execution_id"]

    # Every agent_step names the execution that produced that round. The
    # transition happened inside round 1, so round 1 (and later) completed
    # under the NEW identity — actionably mapped in telemetry.
    step_ids = [s["execution_id"] for s in steps]
    assert step_ids[0] == new_id
    assert set(step_ids) == {new_id}

    metrics = _of_type(events, "metrics")[0]["data"]
    execs = {e["execution_id"]: e for e in metrics["executions"]}
    assert len(execs) == 2
    for eid, entry in execs.items():
        assert entry["rounds"], entry
        assert entry["target"]["execution_id"] == eid
    # The superseded target ATTEMPTED round 1; the new one produced 1..3.
    assert execs[target["execution_id"]]["rounds"] == [1]
    assert execs[new_id]["rounds"] == [1, 2, 3]
    assert metrics["execution_id"] == new_id

# ---------------------------------------------------------------------------
# Unit tests for the pinned-execution helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("chunk,expected", [
    ('event: error\ndata: {"status": 503, "text": "down"}\n\n', ae.FAILURE_TRANSIENT),
    ('event: error\ndata: {"status": 429, "text": "slow down"}\n\n', ae.FAILURE_TRANSIENT),
    ('event: error\ndata: {"status": 504, "text": "timeout"}\n\n', ae.FAILURE_TRANSIENT),
    ('event: error\ndata: {"status": 400, "text": "bad"}\n\n', ae.FAILURE_PERMANENT),
    ('event: error\ndata: {"status": 401, "text": "unauthorized"}\n\n', ae.FAILURE_PERMANENT),
    ('event: error\ndata: {"error": "Read timeout"}\n\n', ae.FAILURE_TRANSIENT),
    ("", ae.FAILURE_UNKNOWN),
])
def test_classify_provider_failure(chunk, expected):
    assert ae.classify_provider_failure(chunk) == expected


def test_classify_provider_failure_accepts_exceptions_and_status():
    assert ae.classify_provider_failure(503) == ae.FAILURE_TRANSIENT
    assert ae.classify_provider_failure(404) == ae.FAILURE_PERMANENT

    class _Err(Exception):
        status_code = 502
    assert ae.classify_provider_failure(_Err("boom")) == ae.FAILURE_TRANSIENT


def test_prefer_same_model_order_and_transition_selection():
    pool = [("a", "kimi-k3", {}), ("b", "openrouter/deepseek-v4-pro", {}),
            ("c", "deepseek-v4-pro", {})]
    ordered = ae.prefer_same_model_order("deepseek-v4-pro", pool)
    # Same model (across providers / namespaces) before a model change.
    assert [c[1] for c in ordered] == ["openrouter/deepseek-v4-pro", "deepseek-v4-pro", "kimi-k3"]

    # A route already used by the run is never re-selected.
    chosen = ae.select_transition_target(
        pinned_model="deepseek-v4-pro",
        current_route=("x", "deepseek-v4-pro"),
        pool=pool,
        used_routes=[("b", "openrouter/deepseek-v4-pro")],
    )
    assert chosen[1] == "deepseek-v4-pro" and chosen[0] == "c"

    # No approved fallback -> None (explicit failure, never a silent switch).
    assert ae.select_transition_target(
        pinned_model="deepseek-v4-pro",
        current_route=("x", "deepseek-v4-pro"),
        pool=[],
        used_routes=[],
    ) is None


def test_build_execution_target_is_immutable_and_records_transitions():
    first = ae.build_execution_target(
        endpoint_url="http://127.0.0.1:11434/v1", model="qwen3.8-27b",
        headers={"Authorization": "Bearer secret"}, context_window=131072,
        is_api_model=False,
    )
    assert first.previous_execution_id is None
    assert first.is_transition is False
    # Secrets never appear in the audit payload.
    assert "secret" not in json.dumps(first.to_dict())

    second = ae.build_execution_target(
        endpoint_url="https://api.example/v1", model="kimi-k3",
        context_window=65536, is_api_model=True,
        previous=first, transition_reason="provider_permanent: HTTP 400",
        selection_reason="explicit_fallback",
    )
    assert second.is_transition is True
    assert second.previous_execution_id == first.execution_id
    assert second.execution_id != first.execution_id
    assert second.tool_profile == ae.TOOL_PROFILE_NATIVE
    assert second.transition_reason.startswith("provider_permanent")

    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.model = "something-else"

# ---------------------------------------------------------------------------
# Completion criterion — a realistic multi-round Qwen agent stays pinned
# ---------------------------------------------------------------------------

def test_realistic_multi_round_qwen_run_stays_pinned(monkeypatch):
    """local / qwen3.8-27b / one endpoint: test -> fix -> test -> finish.

    The run starts on one local target, performs several tool calls, continues
    reasoning, runs the tests, and completes — never changing provider, model
    or endpoint implicitly. A same-model fallback pool is configured but, since
    nothing failed, is never touched.
    """
    _patch_basics(monkeypatch)
    tool_cmds = []

    async def _fake_exec(block, *a, **k):
        cmd = (block.content or "").strip()
        tool_cmds.append((block.tool_type, cmd))
        out = "1 failed, 2 passed" if "pytest" in cmd else "ok"
        return ("bash", {"output": out, "exit_code": 1 if "failed" in out else 0})
    monkeypatch.setattr(al, "execute_tool_block", _fake_exec, raising=False)

    rounds = [
        "```bash\npython -m pytest tests/\n```",       # run tests -> fail
        "```bash\nsed -i s/old/new/ src/thing.py\n```",  # fix
        "```bash\npython -m pytest tests/\n```",        # re-run tests -> pass
        "The tests pass now — done.",
    ]

    def script(idx, candidates, messages, kwargs):
        return [_delta(rounds[min(idx, len(rounds) - 1)]), _DONE]

    calls = _patch_stream(monkeypatch, script)
    same_model_pool = [("http://openrouter.example/v1", _PRIMARY[1], {})]
    events = _run(max_rounds=5, fallbacks=same_model_pool)

    # Four model invocations — one per round — all on the SAME local target.
    assert len(calls) == 4, calls
    assert set(_routes(calls)) == {_PRIMARY}, _routes(calls)
    assert all(len(c["candidates"]) == 1 for c in calls), calls
    # Round 2 saw the round-1 test failure in its history (reasoning continued).
    assert any("1 failed" in str(m.get("content")) for m in calls[1]["messages"])

    # The test/fix/test iterations actually ran as tools on that same target.
    assert sum(1 for _, cmd in tool_cmds if "pytest" in cmd) == 2, tool_cmds

    # Never rerouted: no transition, no fallback use, no provider failure.
    assert _of_type(events, "execution_transition") == []
    assert _of_type(events, "fallback") == []
    assert _of_type(events, "provider_failure") == []

    target = _of_type(events, "execution_target")[0]["data"]
    ids = {s["execution_id"] for s in _of_type(events, "agent_step")}
    assert ids == {target["execution_id"]}

    metrics = _of_type(events, "metrics")[0]["data"]
    assert len(metrics["executions"]) == 1
    assert metrics["executions"][0]["rounds"] == [1, 2, 3, 4]
    assert metrics["executions"][0]["target"]["model"] == _PRIMARY[1]
    assert metrics["executions"][0]["target"]["endpoint"] == _PRIMARY[0]

