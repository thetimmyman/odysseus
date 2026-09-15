"""PS-605 × PS-632 × harness — the integration seam, proven without a live node.

Three things are asserted here, all hermetically:

* the seam turns MEASURED registry records into routing inputs and nothing else —
  no selection, no fallback, no declared-only capability;
* the harness cannot hand-pin: its guards refuse a client that disagrees with the
  decision, and a refusal seals an auditable, package-bound record with NO
  AttemptReceipt;
* a sealed refusal is immutable (mutating it breaks its hash).

The live execution that exercises the same code paths end to end is separate; this
file is what runs on every commit.
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from src import dispatch_routing as dr
from src import local_target_routing as ltr
from src.local_targets import (
    DEFAULT_TARGETS, NETWORK_TAILNET, TARGET_RTX_4500, build_capability,
    target_by_id)

NOW = datetime.datetime.now(datetime.timezone.utc)
HARNESS = Path(__file__).resolve().parents[1] / "scripts" / "ps635-live" / "live_run.py"


def _live_run():
    """Import the harness script as a module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location("ps635_live_run", HARNESS)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("ps635_live_run", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def harness():
    return _live_run()


def _qualified_spec(target_id="local-test-1", *, ssh_host="testhost",
                   model="qwen3.8:27b", roles=None, qualification_ref="test-profile"):
    """A test-owned spec: translation tests must not depend on fleet policy."""
    from src.local_targets import (NETWORK_TAILNET, PRIVACY_LOCAL_ONLY,
                                   ROLE_INFERENCE, LocalTargetSpec)
    return LocalTargetSpec(
        target_id=target_id, label=target_id, ssh_host=ssh_host,
        endpoint="http://127.0.0.1:11434", model=model,
        privacy_class=PRIVACY_LOCAL_ONLY, network_class=NETWORK_TAILNET,
        roles=tuple(roles) if roles is not None else (ROLE_INFERENCE,),
        qualification_ref=qualification_ref)


RAW_TOOL_CAPABLE = {
    "reachable": True, "version": "0.32.11",
    "model": {"name": "qwen3.8:27b", "digest": "d94d9646",
              "details": {"context_length": 262144, "quantization_level": "Q4_K_XL"},
              "capabilities": []},
    "ps": {"models": [{"name": "qwen3.8:27b", "size_vram": 1024,
                       "context_length": 32768}]},
    "tool_proof": {"ok": True, "tool_calls": 1},
    "timings": {},
}

#: A node that DECLARES tools and provably cannot call them: declared-only must
#: never satisfy a requirement.
RAW_DECLARED_ONLY = {**RAW_TOOL_CAPABLE, "tool_proof": {"ok": True, "tool_calls": 0},
                     "model": {**RAW_TOOL_CAPABLE["model"],
                               "capabilities": ["completion", "tools"]}}

PACKET = {
    "packet_id": "P-SEAM-1", "objective": "do the thing", "role": "local_implementer",
    "write_scope": ["src/thing.py"], "target_requirements": ["native_tools"],
    "base_sha": "HEAD",
}


def _record(raw, *, spec=None, probed_at=""):
    return build_capability(spec or _qualified_spec(), raw,
                            probed_at=probed_at or NOW.isoformat())


# ============================================================== the translation ===
def test_a_measured_record_becomes_a_measured_receipt_bound_to_its_own_probe():
    record = _record(RAW_TOOL_CAPABLE)
    inputs = ltr.routing_inputs([record], now=NOW)
    assert len(inputs.profiles) == 1 and not inputs.skipped
    profile = inputs.profiles[0]
    receipt = inputs.receipts[0]
    assert profile.target_id == "local-test-1"
    assert profile.locality == dr.LOCALITY_LOCAL
    assert profile.inference is True
    assert profile.network_policy == NETWORK_TAILNET      # the registry's own name
    assert "write_file" in profile.tools                  # proven tool channel
    assert receipt.provenance == dr.PROVENANCE_MEASURED
    assert receipt.observed_at == record.last_probe       # its OWN probe time
    assert receipt.profile_id == profile.profile_id       # profile-specific
    assert receipt.capabilities == frozenset(
        {dr.CAP_TEXT_GENERATION, dr.CAP_SINGLE_TOOL_CALL,
         dr.CAP_EXACT_REFERENCE_SEMANTICS})
    assert receipt.model_digest == "d94d9646"


def test_a_declared_only_tool_claim_never_becomes_capability():
    record = _record(RAW_DECLARED_ONLY)
    inputs = ltr.routing_inputs([record], now=NOW)
    receipt = inputs.receipts[0]
    assert dr.CAP_SINGLE_TOOL_CALL not in receipt.capabilities
    assert "write_file" not in inputs.profiles[0].tools
    # A read-only node is still useful — the implementer role is what it loses.
    assert dr.ROLE_IMPLEMENTER not in inputs.profiles[0].roles
    assert dr.ROLE_SCOUT in inputs.profiles[0].roles


def test_an_unhealthy_or_unprobed_target_is_not_a_candidate():
    unhealthy = _record({**RAW_TOOL_CAPABLE, "reachable": False})
    inputs = ltr.routing_inputs([unhealthy], now=NOW)
    assert not inputs.profiles and not inputs.receipts
    assert inputs.skipped[0]["reason"].startswith("health=")

    unprobed = _record(RAW_TOOL_CAPABLE, probed_at="")
    inputs = ltr.routing_inputs([unprobed], now=unprobed and NOW)
    # build_capability stamps a probe time; an explicitly empty one must refuse.
    empty = type(unprobed)(spec=unprobed.spec, health=unprobed.health,
                           last_probe="", native_tools=True,
                           runtime_version="0.32.11")
    inputs = ltr.routing_inputs([empty], now=NOW)
    assert not inputs.profiles
    assert "no probe timestamp" in inputs.skipped[0]["reason"]


def test_unknown_requirements_fail_closed_instead_of_passing_quietly():
    with pytest.raises(ltr.FleetRoutingError):
        ltr.requirement_capabilities(["telepathy"])
    assert ltr.requirement_capabilities(["native_tools"]) == (dr.CAP_SINGLE_TOOL_CALL,)
    assert set(ltr.requirement_capabilities(["readonly_analysis"])) == {
        dr.CAP_TEXT_GENERATION, dr.CAP_EXACT_REFERENCE_SEMANTICS}


def test_the_seam_selects_nothing_by_itself():
    """`routing_inputs` returns inputs; only PS-605 turns them into a choice."""
    specs = [_qualified_spec(f"local-test-{i}", ssh_host=f"host{i}")
             for i in (1, 2, 3)]
    records = [_record(RAW_TOOL_CAPABLE, spec=spec) for spec in specs]
    inputs = ltr.routing_inputs(records, now=NOW)
    assert len(inputs.profiles) == 3
    assert [s["target_id"] for s in inputs.skipped] == []
    assert not hasattr(inputs, "selected")


# =================================================================== the routing ===
def test_the_packet_decides_what_the_request_asks_for():
    inputs = ltr.routing_inputs([_record(RAW_TOOL_CAPABLE)], now=NOW)
    request = ltr.request_for_packet(PACKET, role="local_implementer", inputs=inputs,
                                     execution_package_hash="abc", run_id="r1")
    assert request.local_only is True            # the worker path is local by design
    assert request.required_tools == ("write_file",)
    assert request.capabilities == (dr.CAP_SINGLE_TOOL_CALL,)
    assert request.network_policy == NETWORK_TAILNET
    assert request.packet_id == "P-SEAM-1"
    assert request.execution_package_hash == "abc"


def test_a_stated_preference_is_a_preference_not_a_pin(harness):
    specs = [_qualified_spec("local-test-a", ssh_host="hosta"),
             _qualified_spec("local-test-b", ssh_host="hostb")]
    records = [_record(RAW_TOOL_CAPABLE, spec=spec) for spec in specs]
    bound, _inputs = ltr.resolve_fleet_dispatch(
        records, packet=PACKET, role="local_implementer",
        execution_package_hash="abc", run_id="r1",
        preferred_target_id="local-test-b", decision_id="d-pref")
    assert bound.decision.selected_profile.target_id == "local-test-b"
    assert bound.decision.fallback_used is False

    # A preferred target that cannot be a candidate is skipped BEFORE the decision,
    # so PS-605 selects an eligible node and records the skip. The HARNESS is what
    # turns that into a refusal, because executing on a node the operator did not
    # name is not the harness's decision to make.
    broken = _record({**RAW_TOOL_CAPABLE, "reachable": False},
                     spec=_qualified_spec("local-test-b", ssh_host="hostb"))
    good = _record(RAW_TOOL_CAPABLE, spec=_qualified_spec("local-test-a",
                                                         ssh_host="hosta"))
    bound2, inputs2 = ltr.resolve_fleet_dispatch(
        [broken, good], packet=PACKET, role="local_implementer",
        execution_package_hash="abc", run_id="r1",
        preferred_target_id="local-test-b", decision_id="d-pref2")
    assert bound2.decision.selected_profile.target_id == "local-test-a"
    assert [s["target_id"] for s in inputs2.skipped] == ["local-test-b"]
    assert harness.preference_violation("local-test-b",
                                        bound2.decision.selected_profile.target_id)
    assert harness.preference_violation("local-test-a",
                                        bound2.decision.selected_profile.target_id) == ""


def test_a_tool_less_node_cannot_be_selected_for_a_writable_packet():
    record = _record(RAW_DECLARED_ONLY)
    with pytest.raises(dr.RoutingRefused) as err:
        ltr.resolve_fleet_dispatch([record], packet=PACKET, role="local_implementer",
                                   execution_package_hash="abc", run_id="r1")
    rules = {a.profile_id: a.rule for a in err.value.assessments}
    assert rules["local-test-1"] in (dr.REFUSED_ROLE, dr.REFUSED_CAPABILITY_MISSING,
                                     dr.REFUSED_TOOL)


# ============================================================= the refusal seal ===
def _sealed_refusal(tmp_path, harness, *, code="privacy_local_only_no_eligible_target"):
    refusal = {"code": code, "refused": True, "reason": "no eligible local target",
               "candidates": [{"profile_id": "local-test-1",
                               "rule": dr.REFUSED_CAPABILITY_MISSING}]}
    return harness.seal_dispatch_refusal(
        refusal, packet=PACKET, execution_package_hash="pkg-hash", run_id="run-x",
        records=[_record(RAW_TOOL_CAPABLE)], run_dir=tmp_path)


def test_a_refusal_seals_auditable_package_bound_evidence_with_no_attempt(tmp_path,
                                                                         harness):
    sealed = _sealed_refusal(tmp_path, harness)
    payload = json.loads(Path(sealed["path"]).read_text())
    assert payload["kind"] == "dispatch_refusal"
    assert payload["execution_package_hash"] == "pkg-hash"     # package-bound
    assert payload["packet_id"] == PACKET["packet_id"]
    assert payload["refusal"]["code"] == "privacy_local_only_no_eligible_target"
    assert payload["attempt_receipts"] == []                  # no attempt occurred
    assert harness.dispatch_refusal_is_intact(payload)         # immutable
    assert payload["fleet"][0]["target_id"] == "local-test-1"


def test_mutating_a_sealed_refusal_breaks_it(tmp_path, harness):
    sealed = _sealed_refusal(tmp_path, harness)
    payload = json.loads(Path(sealed["path"]).read_text())
    for field, value in (("execution_package_hash", "another-package"),
                         ("packet_id", "P-OTHER"),
                         ("run_id", "run-y"),
                         ("refusal", {"code": "something_else", "refused": True})):
        mutated = json.loads(json.dumps(payload))
        mutated[field] = value
        assert harness.dispatch_refusal_is_intact(mutated) is False, field
    mutated = json.loads(json.dumps(payload))
    mutated["attempt_receipts"] = [{"receipt_id": "invented"}]
    assert harness.dispatch_refusal_is_intact(mutated) is False


# ============================================================== the harness guards ===
def test_a_client_that_disagrees_with_the_decision_is_refused(harness):
    class Client:
        ssh_host = "testhost"
        model = "qwen3.8:27b"

    class WrongHost(Client):
        ssh_host = "somewhere-else"

    class WrongModel(Client):
        model = "qwen2.5:3b"

    inputs = ltr.routing_inputs([_record(RAW_TOOL_CAPABLE)], now=NOW)
    bound, _ = ltr.resolve_fleet_dispatch(
        [_record(RAW_TOOL_CAPABLE)], packet=PACKET, role="local_implementer",
        execution_package_hash="abc", run_id="r1", decision_id="d-guard")
    spec = _qualified_spec("local-test-1", ssh_host="minipc")
    assert harness.pin_matches_client(bound, spec, Client()) == (True, "")
    ok, why = harness.pin_matches_client(bound, spec, WrongHost())
    assert ok is False and "host" in why
    ok, why = harness.pin_matches_client(bound, spec, WrongModel())
    assert ok is False and "model" in why


def test_the_harness_never_writes_an_explicit_pin_receipt(harness):
    """The hand-pin path is gone: the harness has no operator-pin receipt builder."""
    source = HARNESS.read_text()
    assert "explicit_pin" not in source
    assert "DECIDED_BY_EXPLICIT_PIN" not in source
    assert "to_ps638_receipt_kwargs" in source


