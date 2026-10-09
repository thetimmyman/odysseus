"""Rejected native functions remain errors, with bounded, correctly paired feedback."""
import asyncio
import copy
import json

import pytest

import src.agent_loop as al
from src.tool_policy import build_effective_tool_policy


def native(name="run_command", call_id="bad", arguments=None):
    return {"id": call_id, "name": name,
            "arguments": json.dumps(arguments if arguments is not None else {"command": "npm test"})}


def valid(call_id="good"):
    return native("web_search", call_id, {"query": "synthetic fixture"})


@pytest.fixture
def harness(monkeypatch):
    # Keep the real loop, converter, policy and result association. Only model
    # and successful tool I/O are synthetic; no auth/authz gate is replaced.
    import src.settings as settings
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None)
    monkeypatch.setattr(al, "_build_system_prompt", lambda messages, *a, **kw: (list(messages), []))
    executions = []
    teacher_calls = []

    async def execute(block, **kwargs):
        executions.append(block)
        return block.tool_type, {"output": "synthetic success", "exit_code": 0}

    monkeypatch.setattr(al, "execute_tool_block", execute)
    import src.teacher_escalation as teacher

    async def no_teacher(**kwargs):
        teacher_calls.append(kwargs)
        if False:
            yield ""

    monkeypatch.setattr(teacher, "run_teacher_inline", no_teacher)

    def run(rounds, **kwargs):
        calls = []

        async def provider(candidates, messages, **stream_kwargs):
            index = len(calls)
            calls.append(copy.deepcopy(messages))
            assert index < len(rounds), "unexpected extra model invocation"
            text, native_calls = rounds[index]
            if text:
                yield "data: " + json.dumps({"delta": text}) + "\n\n"
            if native_calls:
                yield "data: " + json.dumps({"type": "tool_calls", "calls": native_calls}) + "\n\n"
            yield "data: [DONE]\n\n"

        monkeypatch.setattr(al, "stream_llm_with_fallback", provider)
        kwargs.setdefault("max_rounds", len(rounds))
        kwargs.setdefault("max_tool_calls", 12)
        kwargs.setdefault("relevant_tools", {"web_search"})
        kwargs.setdefault("context_length", 131072)

        async def collect():
            return [chunk async for chunk in al.stream_agent_loop(
                "http://model.example/v1/chat/completions", "qwen3-synthetic",
                [{"role": "user", "content": "Perform this bounded fixture task."}], **kwargs)]

        chunks = asyncio.run(collect())
        events = [json.loads(c[6:]) for c in chunks
                  if c.startswith("data: ") and not c.startswith("data: [DONE]")]
        return events, calls

    return run, executions, teacher_calls


def metrics(events):
    return next(e["data"] for e in events if e.get("type") == "metrics")


def outputs(events):
    return [e for e in events if e.get("type") == "tool_output"]


def feedback(messages):
    return [m for m in messages if m.get("role") == "tool"]


def test_unknown_only_is_visible_saved_and_fed_back(harness):
    run, executed, _ = harness
    events, calls = run([("", [native()]), ("The requested tool is unavailable.", [])])
    assert executed == []
    assert len(calls) == 2
    error = outputs(events)[0]
    assert error["tool"] == "run_command"
    assert error["tool_call_id"] == "bad"
    assert error["exit_code"] == 1
    assert error["error_code"] == "native_call_conversion_failed"
    assert "Nothing was executed" in error["output"]
    starts = [e for e in events if e.get("type") == "tool_start"]
    assert len(starts) == 1
    assert starts[0]["tool"] == "run_command"
    assert starts[0]["rejected"] is True
    result = feedback(calls[1])[0]
    assert result["tool_call_id"] == "bad"
    assert "run_command" in result["content"]
    assert "Nothing was executed" in result["content"]
    assert metrics(events)["tool_events"][0]["error_code"] == error["error_code"]
    assistant = next(m for m in calls[1] if m.get("tool_calls"))
    assert assistant["content"] is None
    assert assistant["tool_calls"][0]["id"] == "bad"


@pytest.mark.parametrize("native_calls", [[native(), valid()], [valid(), native()]])
def test_mixed_batches_preserve_each_original_result_slot(harness, native_calls):
    run, executed, _ = harness
    events, calls = run([("", native_calls), ("Finished with one rejected call.", [])])
    assert [b.tool_type for b in executed] == ["web_search"]
    assert [e["tool_call_id"] for e in outputs(events)] == [c["id"] for c in native_calls]
    paired = feedback(calls[1])
    assert [r["tool_call_id"] for r in paired] == [c["id"] for c in native_calls]
    for call, result in zip(native_calls, paired):
        assert result["content"]
        if call["id"] == "bad":
            assert "Nothing was executed" in result["content"]
            assert "synthetic success" not in result["content"]
        else:
            assert "synthetic success" in result["content"]
            assert "Nothing was executed" not in result["content"]


def test_normal_recovery_retains_failure_without_changing_target(harness):
    run, executed, _ = harness
    events, calls = run([("", [native()]), ("", [valid()]), ("Verification complete.", [])])
    assert len(calls) == 3
    assert [b.tool_type for b in executed] == ["web_search"]
    saved = metrics(events)
    assert [e["exit_code"] for e in saved["tool_events"]] == [1, 0]
    assert "unfinished_reason" not in saved
    assert len(saved["executions"]) == 1
    assert saved["executions"][0]["rounds"] == [1, 2, 3]


def test_rejection_after_prior_progress_cannot_silently_finish(harness):
    run, executed, _ = harness
    events, calls = run([("I inspected the fixture.", [valid()]), ("", [native()])])
    assert len(calls) == 2
    assert len(executed) == 1
    assert [e["exit_code"] for e in metrics(events)["tool_events"]] == [0, 1]
    reply = "".join(e.get("delta", "") for e in events)
    assert "I inspected the fixture." in reply
    assert "requested action is unfinished" in reply
    assert metrics(events)["unfinished_reason"] == "native_call_conversion_failed"


def test_repeated_rejections_share_existing_two_retry_bound(harness):
    run, executed, teacher = harness
    events, calls = run([("", [native(call_id=f"bad{i}")]) for i in range(3)], max_rounds=10)
    assert executed == []
    assert teacher == []
    assert len(calls) == 3  # initial attempt plus two bounded retries
    saved = metrics(events)
    assert len(saved["tool_events"]) == 3
    assert all(e["exit_code"] == 1 for e in saved["tool_events"])
    assert saved["unfinished_reason"] == "native_call_conversion_failed"
    assert saved["tool_events"][-1]["unfinished"] is True
    assert "requested action is unfinished" in "".join(e.get("delta", "") for e in events)
    assert "unfinished" in saved["round_texts"][-1]
    assert not any(e.get("type") == "rounds_exhausted" for e in events)


@pytest.mark.parametrize("kwargs", [{"max_rounds": 1}, {"max_rounds": 10, "max_tool_calls": 1}])
def test_rejection_at_turn_limit_is_explicitly_unfinished(harness, kwargs):
    run, executed, teacher = harness
    events, calls = run([("", [native()])], **kwargs)
    assert len(calls) == 1
    assert executed == []
    assert teacher == []
    assert metrics(events)["unfinished_reason"] == "native_call_conversion_failed"
    assert "unfinished" in "".join(e.get("delta", "") for e in events)
    assert any(e.get("type") == "rounds_exhausted" for e in events) == (kwargs["max_rounds"] == 1)


def test_native_batch_budget_refuses_remaining_slots_without_executor(harness):
    run, executed, _ = harness
    events, calls = run([("", [valid("first"), native("web_search", "second", {"query": "other"}), native()])],
                       max_rounds=10, max_tool_calls=1)
    assert len(calls) == 1
    assert len(executed) == 1
    assert [e["tool_call_id"] for e in outputs(events)] == ["first", "second", "bad"]
    assert [e["exit_code"] for e in outputs(events)] == [0, 1, 1]
    assert outputs(events)[1]["error_code"] == "tool_budget_exhausted"
    assert len([e for e in events if e.get("type") == "budget_exceeded"]) == 1
    assert metrics(events)["unfinished_reason"] == "native_call_conversion_failed"


def test_disabled_known_tool_stays_blocked_before_executor(harness):
    run, executed, _ = harness
    policy = build_effective_tool_policy(disabled_tools={"web_search"})
    events, calls = run([("", [valid()]), ("The tool is disabled.", [])], tool_policy=policy)
    assert len(calls) == 2
    assert executed == []
    assert outputs(events)[0]["exit_code"] == 1
    assert "disabled" in outputs(events)[0]["output"].lower()
    assert "native_call_conversion_failed" not in str(outputs(events))


def test_rejected_native_does_not_execute_fenced_prose(harness):
    run, executed, _ = harness
    events, _ = run([("```bash\necho must-not-run\n```", [native()]), ("Tool unavailable.", [])])
    assert executed == []
    assert [e["tool"] for e in outputs(events)] == ["run_command"]


def test_force_answer_rejection_does_not_execute_or_add_a_synthesis_call(harness):
    run, executed, teacher = harness
    # Five identical tool rounds trip the existing loop breaker; the next
    # round is tool-free. A provider violating that must still fail visibly.
    rounds = [("", [valid(f"good{i}")]) for i in range(5)]
    rounds.append(("", [native(), valid("final")]))
    events, calls = run(rounds, max_rounds=10)
    assert len(calls) == 6
    assert len(executed) == 4  # breaker discards round5 before execution
    assert teacher == []
    assert [e["tool_call_id"] for e in outputs(events)[-2:]] == ["bad", "final"]
    assert [e["error_code"] for e in outputs(events)[-2:]] == [
        "native_call_conversion_failed", "tools_not_allowed_in_final_answer"]
    assert metrics(events)["unfinished_reason"] == "native_call_conversion_failed"


def test_malformed_arguments_are_not_dropped(harness):
    run, executed, _ = harness
    bad = native("web_search")
    bad["arguments"] = "{invalid"
    events, calls = run([("", [bad]), ("Arguments could not be parsed.", [])])
    assert executed == []
    assert outputs(events)[0]["exit_code"] == 1
    assert "Nothing was executed" in feedback(calls[1])[0]["content"]


def test_plain_final_answer_is_unchanged(harness):
    run, executed, _ = harness
    events, calls = run([("Here is the answer.", [])])
    assert len(calls) == 1
    assert executed == []
    assert outputs(events) == []
    assert "unfinished_reason" not in metrics(events)
    assert not any(e.get("type") == "rounds_exhausted" for e in events)


def test_rejection_metadata_survives_actual_session_save_and_reload(harness, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    import core.database as database
    import core.models as models
    import core.session_manager as sessions
    from routes.chat_helpers import save_assistant_response

    engine = create_engine("sqlite:///:memory:")
    database.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", session_factory)
    monkeypatch.setattr(sessions, "SessionLocal", session_factory)
    manager = sessions.SessionManager()
    monkeypatch.setattr(models, "_session_manager", manager)
    sess = manager.create_session("native-error-fixture", "fixture", "http://model.example/v1", "qwen3-synthetic")
    run, _, _ = harness
    events, _ = run([("", [native()])], max_rounds=1)
    text = "".join(e.get("delta", "") for e in events)
    save_assistant_response(sess, manager, sess.id, text, metrics(events))
    restored = sessions.SessionManager().get_session(sess.id)
    assert restored is not None
    saved = restored.history[-1]
    assert "requested action is unfinished" in saved.content
    assert saved.metadata["unfinished_reason"] == "native_call_conversion_failed"
    assert saved.metadata["tool_events"][0]["tool_call_id"] == "bad"
    assert saved.metadata["tool_events"][0]["exit_code"] == 1
    engine.dispose()
