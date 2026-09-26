"""Local Qwen worker pool: target registry + measured capability.

Each property has a negative control:

1. the fleet is THREE distinctly-identified targets, not one ``local_qwen``;
2. capability is MEASURED, and a declared tool capability is not proof;
3. selection refuses rather than downgrades (capability + health negatives);
4. two writers are never dispatched into the same write scope.
"""
import json

import pytest

from src import local_targets as lt


def _model(*, name="qwen3.8:27b", quant="Q4_K_M", ctx=262144, caps=("completion",)):
    return {
        "name": name,
        "model": name,
        "digest": "deadbeef",
        "size": 17741872154,
        "details": {
            "family": "qwen35",
            "parameter_size": "27.3B",
            "quantization_level": quant,
            "context_length": ctx,
        },
        "capabilities": list(caps),
    }


def _raw(*, reachable=True, version="0.33.3", model=True, tool_calls=1,
         tool_ok=True, loaded=0, model_kwargs=None, **extra):
    """A raw observation shaped exactly like OllamaInspector.inspect() output."""
    raw = {"reachable": reachable, "failure_classes": []}
    if not reachable:
        raw["failure_classes"] = ["runtime_unreachable"]
        raw.update(extra)
        return raw
    raw["version"] = version
    if model is True:
        raw["model"] = _model(**(model_kwargs or {}))
    elif model is False:
        raw["failure_classes"] = ["model_absent"]
        raw["available_models"] = ["other:1b"]
    raw["ps"] = {"models": [{"name": "qwen3.8:27b", "size_vram": 17_000_000_000}] * loaded}
    if tool_ok is not None:
        raw["tool_proof"] = {
            "ok": tool_ok,
            "tool_calls": tool_calls if tool_ok else 0,
            "error": "" if tool_ok else "connection reset",
        }
    raw.update(extra)
    return raw


def _records(**overrides):
    """Measured records for the two REACHABLE targets, keyed by target_id.

    Defaults mirror a real probe: the RTX target is healthy but has NO tool
    capability, the MS-R1 target has tools.
    """
    rtx = lt.build_capability(
        lt.target_by_id(lt.TARGET_RTX_4500),
        _raw(version="0.32.11", tool_calls=0, model_kwargs={"quant": "UD-Q4_K_XL"}),
    )
    msr1 = lt.build_capability(
        lt.target_by_id(lt.TARGET_MSR1),
        _raw(version="0.33.3", tool_calls=1),
    )
    got = {lt.TARGET_RTX_4500: rtx, lt.TARGET_MSR1: msr1}
    for tid, kwargs in overrides.items():
        got[tid] = lt.build_capability(lt.target_by_id(tid), _raw(**kwargs))
    return got


def test_fleet_is_three_distinct_targets_not_one_label():
    specs = lt.registered_targets()
    assert len(specs) == 3
    assert len({s.target_id for s in specs}) == 3
    assert len({s.ssh_host for s in specs}) == 3
    assert "local_qwen" not in {s.target_id for s in specs}


def test_every_registered_target_has_a_stable_id_and_host():
    for spec in lt.registered_targets():
        assert spec.target_id
        assert spec.ssh_host
        assert spec.endpoint.startswith("http")
        # Local inference is the local-only privacy class by default.
        assert spec.privacy_class == lt.PRIVACY_LOCAL_ONLY
    assert lt.target_by_id(lt.TARGET_RTX_4500).ssh_host == "minipc"
    assert lt.target_by_id(lt.TARGET_MSR1).ssh_host == "msr1"
    assert lt.target_by_id(lt.TARGET_FRAMEWORK).ssh_host == "framework"


def test_unknown_target_id_is_none_not_a_guess():
    assert lt.target_by_id("local_qwen") is None
    assert lt.target_by_id("") is None


def test_transport_is_ssh_when_the_endpoint_is_loopback_only():
    assert lt.target_by_id(lt.TARGET_RTX_4500).transport == lt.TRANSPORT_SSH
    routable = lt.LocalTargetSpec(
        target_id="local-x", label="x", ssh_host="", endpoint="http://10.0.0.9:11434",
        model="m",
    )
    assert routable.transport == lt.TRANSPORT_HTTP



def test_declared_tools_without_a_proven_tool_call_is_not_proven():
    """NEGATIVE CONTROL: declared tools are not proven tools.

    A runtime that advertises ``tools`` in its capability list but answers the
    tool question without emitting ``tool_calls`` must NOT be treated as
    tool-capable.
    """
    rec = lt.build_capability(
        lt.target_by_id(lt.TARGET_MSR1),
        _raw(tool_calls=0, model_kwargs={"caps": ("completion", "tools", "thinking")}),
    )
    assert rec.declared_capabilities == ("completion", "tools", "thinking")
    assert rec.native_tools is False
    assert "tools_declared_but_unproven" in rec.failure_classes
    assert lt.CAP_NATIVE_TOOLS not in rec.proven_capabilities()
    assert rec.satisfies([lt.CAP_NATIVE_TOOLS]) is False


def test_a_proven_tool_call_promotes_the_capability():
    """POSITIVE CONTROL for the case above."""
    rec = lt.build_capability(lt.target_by_id(lt.TARGET_MSR1), _raw(tool_calls=1))
    assert rec.native_tools is True
    assert lt.CAP_NATIVE_TOOLS in rec.proven_capabilities()
    assert rec.satisfies([lt.CAP_NATIVE_TOOLS]) is True


def test_unprobed_tools_are_unproven_not_incapable():
    rec = lt.build_capability(lt.target_by_id(lt.TARGET_RTX_4500), _raw(tool_ok=None))
    assert rec.native_tools is None
    assert "tools_unproven" in rec.failure_classes
    # ...but an unproven channel still refuses a tool-required packet.
    assert rec.satisfies([lt.CAP_NATIVE_TOOLS]) is False


def test_tool_probe_transport_failure_is_unproven_not_a_model_verdict():
    rec = lt.build_capability(lt.target_by_id(lt.TARGET_RTX_4500), _raw(tool_ok=False))
    assert rec.native_tools is None
    assert "tool_probe_failed" in rec.failure_classes


def test_tool_less_target_records_the_real_reason():
    rec = lt.build_capability(
        lt.target_by_id(lt.TARGET_RTX_4500),
        _raw(tool_calls=0, model_kwargs={"caps": ("completion", "vision")}),
    )
    assert "no_native_tools_declared" in rec.failure_classes
    assert "tools_declared_but_unproven" not in rec.failure_classes


def test_safe_working_context_is_never_inferred_from_the_declared_maximum():
    rec = lt.build_capability(lt.target_by_id(lt.TARGET_RTX_4500), _raw())
    assert rec.declared_context == 262144
    assert rec.safe_working_context is None
    measured = lt.build_capability(
        lt.target_by_id(lt.TARGET_RTX_4500), _raw(safe_working_context=32768)
    )
    assert measured.safe_working_context == 32768


def test_identity_fields_are_captured_per_target():
    rec = lt.build_capability(
        lt.target_by_id(lt.TARGET_RTX_4500),
        _raw(version="0.32.11", model_kwargs={"quant": "UD-Q4_K_XL"}),
    )
    assert rec.runtime_version == "0.32.11"
    assert rec.quantization == "UD-Q4_K_XL"
    assert rec.model_id == "qwen3.8:27b"
    assert rec.spec.ssh_host == "minipc"


def test_queue_depth_and_resident_vram_are_read_from_ps():
    rec = lt.build_capability(lt.target_by_id(lt.TARGET_MSR1), _raw(loaded=1))
    assert rec.queue_depth == 1
    assert rec.size_vram_bytes == 17_000_000_000


def test_served_context_is_read_from_ps_and_is_not_the_declared_maximum():
    """Measured: the RTX node declares 262144 and SERVES 32768.

    A packet sized from the declared number would be dispatched into a window
    that does not exist, so the two must never be conflated.
    """
    raw = _raw()
    raw["ps"] = {"models": [{"name": "qwen3.8:27b", "size_vram": 18721286388,
                             "context_length": 32768}]}
    rec = lt.build_capability(lt.target_by_id(lt.TARGET_RTX_4500), raw)
    assert rec.declared_context == 262144
    assert rec.served_context == 32768
    assert rec.to_dict()["served_context"] == 32768


def test_served_context_is_none_when_nothing_is_resident():
    rec = lt.build_capability(lt.target_by_id(lt.TARGET_RTX_4500), _raw(loaded=0))
    assert rec.served_context is None


def test_unreachable_node_is_a_record_not_a_dropped_row():
    """A node we cannot reach must stay VISIBLE in the fleet snapshot."""
    rec = lt.build_capability(lt.target_by_id(lt.TARGET_FRAMEWORK), _raw(reachable=False))
    assert rec.health == lt.HEALTH_UNREACHABLE
    assert "runtime_unreachable" in rec.failure_classes
    snap = lt.fleet_snapshot([rec])
    assert snap["fleet_size"] == 1
    assert snap["unreachable"] == 1
    assert snap["targets"][0]["target_id"] == lt.TARGET_FRAMEWORK


def test_missing_model_is_degraded_not_healthy():
    rec = lt.build_capability(lt.target_by_id(lt.TARGET_MSR1), _raw(model=False))
    assert rec.health == lt.HEALTH_DEGRADED
    assert lt.CAP_READONLY_ANALYSIS not in rec.proven_capabilities()


def test_build_capability_is_deterministic_from_stored_evidence():
    raw = _raw(tool_calls=1)
    a = lt.build_capability(lt.target_by_id(lt.TARGET_MSR1), raw, probed_at="T")
    b = lt.build_capability(lt.target_by_id(lt.TARGET_MSR1), raw, probed_at="T")
    assert a.to_dict() == b.to_dict()


def test_snapshot_is_json_serializable():
    snap = lt.fleet_snapshot(list(_records().values()))
    assert json.loads(json.dumps(snap))["fleet_size"] == 2



def test_tool_required_packet_never_selects_a_tool_less_target():
    """CAPABILITY NEGATIVE. Both nodes healthy; only one can call a tool."""
    recs = _records()
    chosen = lt.select_local_target([lt.CAP_NATIVE_TOOLS], records=list(recs.values()))
    assert chosen.target_id == lt.TARGET_MSR1
    assert chosen.native_tools is True
    assert chosen.health == lt.HEALTH_HEALTHY


def test_tool_required_packet_refuses_when_only_tool_less_targets_are_healthy():
    """CAPABILITY NEGATIVE, no false fallback: refuse, never downgrade."""
    recs = _records()
    only_rtx = [recs[lt.TARGET_RTX_4500]]
    with pytest.raises(lt.LocalTargetUnavailable) as exc:
        lt.select_local_target([lt.CAP_NATIVE_TOOLS], records=only_rtx)
    assert "local-rtx4500" in str(exc.value)
    assert "native_tools" in str(exc.value)


def test_read_only_analysis_still_routes_to_the_tool_less_target():
    """Bounded usefulness: a no-tools node is not dead weight."""
    recs = _records()
    only_rtx = [recs[lt.TARGET_RTX_4500]]
    chosen = lt.select_local_target([lt.CAP_READONLY_ANALYSIS], records=only_rtx)
    assert chosen.target_id == lt.TARGET_RTX_4500


def test_health_negative_routes_to_the_other_healthy_target():
    """HEALTH NEGATIVE: force one node unhealthy; work goes to the other.

    Adversarial on purpose: the unhealthy node is given the BEST fitness score,
    so this test fails if the health filter is ever dropped. Without it the
    scheduler would happily dispatch to a node it just proved unreachable.
    """
    recs = _records()
    recs[lt.TARGET_RTX_4500].health = lt.HEALTH_UNREACHABLE
    recs[lt.TARGET_RTX_4500].decode_tok_s = 500.0   # would win on fitness alone
    recs[lt.TARGET_RTX_4500].queue_depth = 0
    recs[lt.TARGET_MSR1].decode_tok_s = 3.0
    chosen = lt.select_local_target(records=list(recs.values()))
    assert chosen.target_id == lt.TARGET_MSR1


def test_all_targets_unhealthy_is_a_typed_refusal_with_reasons():
    recs = _records()
    for rec in recs.values():
        rec.health = lt.HEALTH_UNREACHABLE
    with pytest.raises(lt.LocalTargetUnavailable) as exc:
        lt.select_local_target(records=list(recs.values()))
    assert "unreachable" in str(exc.value)
    assert len(exc.value.reasons) == 2


def test_collision_negative_same_write_scope_is_never_double_dispatched():
    """Two idle nodes are not permission to put two writers in one worktree."""
    recs = _records()
    held = {lt.TARGET_MSR1: "packet-1"}
    with pytest.raises(lt.LocalTargetUnavailable) as exc:
        lt.select_local_target([lt.CAP_NATIVE_TOOLS], records=list(recs.values()),
                               write_scope="packet-1", held_write_scopes=held)
    assert "write scope" in str(exc.value)


def test_write_scope_collision_refuses_when_no_other_target_qualifies():
    recs = _records()
    held = {lt.TARGET_MSR1: "packet-1", lt.TARGET_RTX_4500: "packet-1"}
    with pytest.raises(lt.LocalTargetUnavailable) as exc:
        lt.select_local_target([lt.CAP_NATIVE_TOOLS], records=list(recs.values()),
                               write_scope="packet-1", held_write_scopes=held)
    assert "write scope" in str(exc.value)


def test_a_different_write_scope_does_not_block_a_target():
    recs = _records()
    held = {lt.TARGET_MSR1: "packet-1"}
    chosen = lt.select_local_target([lt.CAP_NATIVE_TOOLS], records=list(recs.values()),
                                    write_scope="packet-2", held_write_scopes=held)
    assert chosen.target_id == lt.TARGET_MSR1



def test_an_idle_target_beats_a_busier_faster_one():
    """Prefer idle capacity, not a fixed host order."""
    recs = _records()
    recs[lt.TARGET_MSR1].queue_depth = 0
    recs[lt.TARGET_MSR1].decode_tok_s = 3.0
    fw = lt.build_capability(lt.target_by_id(lt.TARGET_FRAMEWORK), _raw(tool_calls=1))
    fw.decode_tok_s = 9.0   # much faster...
    fw.queue_depth = 1      # ...but already busy
    recs[lt.TARGET_FRAMEWORK] = fw
    chosen = lt.select_local_target([lt.CAP_NATIVE_TOOLS], records=list(recs.values()))
    assert chosen.target_id == lt.TARGET_MSR1


def test_throughput_breaks_a_tie_between_two_idle_targets():
    recs = _records()
    fw = lt.build_capability(lt.target_by_id(lt.TARGET_FRAMEWORK), _raw(tool_calls=1))
    fw.decode_tok_s = 9.0
    recs[lt.TARGET_FRAMEWORK] = fw
    recs[lt.TARGET_MSR1].decode_tok_s = 3.0
    chosen = lt.select_local_target([lt.CAP_NATIVE_TOOLS], records=list(recs.values()))
    assert chosen.target_id == lt.TARGET_FRAMEWORK


def test_an_unmeasured_target_never_outranks_a_measured_one():
    recs = _records()
    fw = lt.build_capability(lt.target_by_id(lt.TARGET_FRAMEWORK), _raw(tool_calls=1))
    fw.decode_tok_s = None
    recs[lt.TARGET_FRAMEWORK] = fw
    recs[lt.TARGET_MSR1].decode_tok_s = 3.0
    chosen = lt.select_local_target([lt.CAP_NATIVE_TOOLS], records=list(recs.values()))
    assert chosen.target_id == lt.TARGET_MSR1


def test_tie_break_is_deterministic_regardless_of_input_order():
    recs = _records()
    for rec in recs.values():
        rec.queue_depth = 0
        rec.decode_tok_s = 1.0
    first = lt.select_local_target(records=list(recs.values())).target_id
    again = lt.select_local_target(records=list(reversed(list(recs.values())))).target_id
    assert first == again == lt.TARGET_MSR1  # "local-msr1" sorts before "local-rtx4500"


def test_unknown_required_capability_fails_closed():
    with pytest.raises(ValueError):
        lt.select_local_target(["tool_use_maybe"], records=list(_records().values()))


def test_unknown_pin_is_a_typed_refusal():
    with pytest.raises(lt.LocalTargetUnavailable):
        lt.select_local_target(pin="local_qwen", records=list(_records().values()))


def test_explicit_pin_selects_only_that_target():
    recs = _records()
    chosen = lt.select_local_target([lt.CAP_NATIVE_TOOLS], records=list(recs.values()),
                                    pin=lt.TARGET_MSR1)
    assert chosen.target_id == lt.TARGET_MSR1


def test_selection_returns_the_measured_identity_not_a_generic_label():
    recs = _records()
    chosen = lt.select_local_target([lt.CAP_NATIVE_TOOLS], records=list(recs.values()))
    payload = chosen.to_dict()
    assert payload["target_id"] == "local-msr1"
    assert payload["ssh_host"] == "msr1"
    assert payload["model_id"] == "qwen3.8:27b"
    assert payload["runtime_version"] == "0.33.3"
    assert payload["quantization"] == "Q4_K_M"
    assert payload["privacy_class"] == "local-only"


def test_caller_can_exclude_a_target_explicitly():
    recs = _records()
    with pytest.raises(lt.LocalTargetUnavailable):
        lt.select_local_target([lt.CAP_NATIVE_TOOLS], records=list(recs.values()),
                               exclude=[lt.TARGET_MSR1])



class _FakeInspector(lt.OllamaInspector):
    """Canned API replies, so the inspector's own logic is tested with no node.

    Every test below is about what the inspector DOES with an answer — retry,
    failure classification, name matching — not about the network.
    """

    def __init__(self, replies, **kw):
        super().__init__(timeout=1, **kw)
        self.replies = replies
        self.calls = []

    def api(self, spec, path, body=None):
        self.calls.append((path, body))
        reply = self.replies.get(path, {"ok": False, "http": "", "body": {},
                                        "err": "no reply configured"})
        if callable(reply):
            return reply(path, body)
        return reply


def _ok(body):
    return {"ok": True, "http": "200", "body": body, "err": ""}


def _fail(err="boom"):
    return {"ok": False, "http": "", "body": {}, "err": err}


def _probe_with(replies, spec=None, **kw):
    spec = spec or lt.target_by_id(lt.TARGET_RTX_4500)
    return lt.probe_target(spec, inspector=_FakeInspector(replies, **kw))


def test_inspector_reports_an_unreachable_node_without_raising():
    rec = _probe_with({"/api/version": _fail("ssh: connect timed out")})
    assert rec.health == lt.HEALTH_UNREACHABLE
    assert "runtime_unreachable" in rec.failure_classes
    assert rec.runtime_version == ""


def test_inspector_reports_missing_model_against_the_real_inventory():
    replies = {
        "/api/version": _ok({"version": "0.33.3"}),
        "/api/tags": _ok({"models": [{"name": "llama3:8b", "model": "llama3:8b",
                                      "details": {}, "capabilities": ["completion"]}]}),
    }
    rec = _probe_with(replies)
    assert rec.health == lt.HEALTH_DEGRADED
    assert "model_absent" in rec.failure_classes
    assert rec.evidence["available_models"] == ["llama3:8b"]


def test_inspector_reads_a_proven_tool_call_from_the_chat_reply():
    replies = {
        "/api/version": _ok({"version": "0.33.3"}),
        "/api/tags": _ok({"models": [_model(caps=("completion", "tools"))]}),
        "/api/ps": _ok({"models": []}),
        "/api/chat": _ok({"message": {"tool_calls": [{"function": {"name": "add_numbers"}}]}}),
    }
    rec = _probe_with(replies, spec=lt.target_by_id(lt.TARGET_MSR1))
    assert rec.native_tools is True
    assert rec.health == lt.HEALTH_HEALTHY


def test_inspector_retries_the_tool_question_without_the_think_key():
    """A runtime that predates `think` is not thereby tool-less."""
    state = {"n": 0}

    def chat(path, body):
        state["n"] += 1
        if state["n"] == 1:
            assert "think" in body          # first attempt asks WITH the key
            return _fail('unknown field "think"')
        assert "think" not in body          # retry drops it
        return _ok({"message": {"tool_calls": [{"function": {"name": "add_numbers"}}]}})

    replies = {
        "/api/version": _ok({"version": "0.32.11"}),
        "/api/tags": _ok({"models": [_model()]}),
        "/api/ps": _ok({"models": []}),
        "/api/chat": chat,
    }
    rec = _probe_with(replies)
    assert state["n"] == 2
    assert rec.native_tools is True


def test_inspector_with_tool_probing_disabled_asks_no_tool_question():
    replies = {
        "/api/version": _ok({"version": "0.33.3"}),
        "/api/tags": _ok({"models": [_model(caps=("completion", "tools"))]}),
        "/api/ps": _ok({"models": []}),
    }
    inspector = _FakeInspector(replies, probe_tools=False)
    rec = lt.probe_target(lt.target_by_id(lt.TARGET_MSR1), inspector=inspector)
    assert not any(path == "/api/chat" for path, _ in inspector.calls)
    assert rec.native_tools is None
    assert "tools_unproven" in rec.failure_classes


def test_inspector_matches_a_model_by_its_model_field_too():
    entry = _model()
    entry["name"] = ""
    entry["model"] = "qwen3.8:27b"
    replies = {
        "/api/version": _ok({"version": "0.33.3"}),
        "/api/tags": _ok({"models": [entry]}),
        "/api/ps": _ok({"models": []}),
        "/api/chat": _ok({"message": {"tool_calls": []}}),
    }
    rec = _probe_with(replies)
    assert rec.model_id == "qwen3.8:27b"
    assert rec.native_tools is False


def test_probe_fleet_keeps_a_dead_node_in_the_snapshot():
    """The blocked Framework node must appear, not vanish, when it is down."""
    replies = {
        "/api/version": _fail("Connection timed out"),
        "/api/tags": _ok({"models": [_model(caps=("completion", "tools"))]}),
        "/api/ps": _ok({"models": []}),
        "/api/chat": _ok({"message": {"tool_calls": [{"function": {"name": "add_numbers"}}]}}),
    }
    recs = lt.probe_fleet(inspector=_FakeInspector(replies))
    snap = lt.fleet_snapshot(list(recs))
    assert snap["fleet_size"] == 3
    assert snap["unreachable"] == 3  # the fake makes every node unreachable
    assert {t["target_id"] for t in snap["targets"]} == {
        lt.TARGET_RTX_4500, lt.TARGET_MSR1, lt.TARGET_FRAMEWORK
    }


def test_applied_timings_make_selection_fitness_driven():
    """The load-bearing reason apply_timings exists.

    Measured: with no throughput recorded, the fitness tie-break
    falls through to ``target_id`` and a tool-required packet selects the
    2.83 tok/s ARM node over the 37.1 tok/s GPU node. Feeding the harness's
    measured decode rates back in must flip that choice — and the flip must be
    caused by the MEASUREMENT, not by reordering the registry.
    """
    recs = _records()
    for rec in recs.values():
        rec.queue_depth = 0
    # Fitness-blind: deterministic ID order wins, and it is the SLOWER node.
    assert lt.select_local_target(records=list(recs.values())).target_id == lt.TARGET_MSR1
    lt.apply_timings(recs[lt.TARGET_RTX_4500], {"decode_tok_s": 37.1})
    lt.apply_timings(recs[lt.TARGET_MSR1], {"decode_tok_s": 2.83})
    assert lt.select_local_target(records=list(recs.values())).target_id == lt.TARGET_RTX_4500


def test_apply_timings_never_erases_a_measured_value_with_none():
    rec = lt.build_capability(lt.target_by_id(lt.TARGET_RTX_4500),
                              _raw(timings={"decode_tok_s": 37.1, "ttft_s": 0.4}))
    assert rec.decode_tok_s == 37.1
    lt.apply_timings(rec, {"prefill_tok_s": 70.05, "decode_tok_s": None})
    assert rec.decode_tok_s == 37.1          # unchanged
    assert rec.prefill_tok_s == 70.05
    assert rec.ttft_s == 0.4


def test_apply_timings_on_an_empty_mapping_is_a_no_op():
    rec = lt.build_capability(lt.target_by_id(lt.TARGET_RTX_4500), _raw())
    before = rec.to_dict()
    lt.apply_timings(rec, {})
    assert rec.to_dict() == before


def test_probe_fleet_attaches_harness_timings_by_target_id():
    replies = {
        "/api/version": _ok({"version": "0.33.3"}),
        "/api/tags": _ok({"models": [_model(caps=("completion", "tools"))]}),
        "/api/ps": _ok({"models": []}),
        "/api/chat": _ok({"message": {"tool_calls": [{"function": {"name": "add_numbers"}}]}}),
    }
    recs = lt.probe_fleet(
        inspector=_FakeInspector(replies),
        timings_by_target={lt.TARGET_RTX_4500: {"decode_tok_s": 37.1, "cold_load_s": 53.2}},
    )
    by_id = {r.target_id: r for r in recs}
    assert by_id[lt.TARGET_RTX_4500].decode_tok_s == 37.1
    assert by_id[lt.TARGET_RTX_4500].cold_load_s == 53.2
    assert by_id[lt.TARGET_MSR1].decode_tok_s is None
