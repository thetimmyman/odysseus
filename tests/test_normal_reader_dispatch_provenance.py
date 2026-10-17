"""Optional provenance accompanies actual normal dispatch, outside its receipts."""
import builtins
import dataclasses
from datetime import datetime, timezone
import json

import pytest

from src import dispatch_boundary as boundary
from src import dispatch_routing as routing
from src import local_target_routing as fleet
from src import routing_policy
from src.local_targets import (
    CapabilityEvidence, ContextProfile, HostBaseline, LocalTargetSpec,
    ModelIdentity, RuntimeIdentity, make_target_capability_receipt,
)
from src.target_capability_store import TargetCapabilityStore


NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


@pytest.fixture
def estate(tmp_path, monkeypatch):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({
        "routingPolicyVersion": "synthetic-v1", "remoteSensitivityCeiling": "confidential",
        "domains": {"personal": {"localOnly": True}},
        "synthetic_unknown_key": "private-policy-canary",
    }))
    monkeypatch.setattr(routing_policy, "POLICY_PATH", str(policy))
    monkeypatch.setattr(routing_policy, "BAKED_POLICY_PATH", str(tmp_path / "absent-seed"))
    monkeypatch.setattr(routing_policy, "_cache", None)
    from src import offer_economics
    monkeypatch.setattr(offer_economics, "configured_quote", lambda **kwargs: None)
    receipt = make_target_capability_receipt(
        host_id="synthetic-target", profile_id="derived", observed_at=NOW.isoformat(),
        runtime=RuntimeIdentity(provider="ollama", runtime_kind="ollama",
                                endpoint_url="http://127.0.0.1:1", endpoint_type="http",
                                version="synthetic"),
        model=ModelIdentity(model_id="synthetic-model", digest="b" * 64),
        context=ContextProfile(safe_working_context=32768, safe_context_source="synthetic-only"),
        host=HostBaseline(host_id="synthetic-target"), roles=("inference",),
        qualification_ref="synthetic-only",
        capabilities=CapabilityEvidence(measured=("readonly_analysis",)),
        health="healthy", health_checked_at=NOW.isoformat(),
    )
    store = TargetCapabilityStore(str(tmp_path / "store"))
    store.append(receipt)
    spec = LocalTargetSpec(target_id=receipt.host_id, label="synthetic", ssh_host="",
                           endpoint=receipt.runtime.endpoint_url, model=receipt.model.model_id,
                           roles=("inference",), qualification_ref="synthetic-only")
    inputs = fleet.persisted_routing_inputs(store, specs=(spec,), now=NOW)
    assert len(inputs.profiles) == 1
    request = routing.RoutingRequest(
        domain="personal", role=routing.ROLE_APPROXIMATE_ANALYST,
        run_id="synthetic-run", packet_id="synthetic-packet",
        exactness=routing.EXACTNESS_APPROXIMATE, sensitivity="restricted", local_only=True,
        capabilities=(routing.CAP_TEXT_GENERATION,), max_cost_rank=0,
    )
    opens = []
    original_open = builtins.open

    def tracked_open(path, *args, **kwargs):
        if str(path) == str(policy):
            opens.append(str(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", tracked_open)
    return inputs, request, store, policy, opens


def _call(kind, estate, **kwargs):
    inputs, request, store, _, _ = estate
    if kind == "boundary":
        return boundary.resolve_from_estate(
            boundary.TargetEstate(profiles=inputs.profiles, receipts=inputs.receipts),
            request, capability_store=store, now=NOW, decision_id="synthetic-decision", **kwargs)
    if kind == "selection":
        return routing.select_target(request, profiles=inputs.profiles, receipts=inputs.receipts,
                                     now=NOW, decision_id="synthetic-decision", **kwargs)
    return routing.policy_snapshot(**kwargs)


@pytest.mark.parametrize("kind", ["boundary", "selection", "snapshot"])
def test_provenance_preserves_public_outputs_hashes_and_content_reads(estate, monkeypatch, kind):
    legacy = _call(kind, estate)
    legacy_opens = list(estate[-1])
    estate[-1].clear()
    monkeypatch.setattr(routing_policy, "_cache", None)
    sink = {"old_private_value": "must-not-survive"}
    observed = _call(kind, estate, provenance_sink=sink)
    assert dataclasses.asdict(observed) == dataclasses.asdict(legacy)
    assert estate[-1] == legacy_opens
    assert "old_private_value" not in sink
    assert "private-policy-canary" not in json.dumps(sink)
    if kind == "snapshot":
        assert sink["kind"] == "loaded"
        assert sink["load"]["origin"]["path"] == str(estate[3])
    else:
        selection = sink["selection"] if kind == "boundary" else sink
        decision = observed.decision if kind == "boundary" else observed
        assert selection["selected"] == {
            "target_id": decision.selected_profile.target_id,
            "profile_id": decision.selected_profile.profile_id,
        }
        assert selection["domain"]["branch"] == "configured_entry"
        assert selection["candidates"][0]["route"]["ceiling"]["load"]["origin"]["path"] == str(estate[3])


def test_caller_supplied_snapshot_is_not_relabelled_as_file(estate):
    snapshot = routing.policy_snapshot(policy={"routingPolicyVersion": "supplied"})
    sink = {}
    _call("selection", estate, policy=snapshot, provenance_sink=sink)
    assert sink["snapshot"] == {"kind": "caller_supplied"}


def test_refusal_clears_old_success_and_preserves_original_exception(estate):
    request = estate[1]
    with pytest.raises(routing.RoutingRefused) as original:
        routing.select_target(request, profiles=(), decision_id="synthetic-empty", now=NOW)
    sink = {"selected": {"target_id": "stale-target"}}
    with pytest.raises(routing.RoutingRefused) as changed:
        routing.select_target(request, profiles=(), decision_id="synthetic-empty", now=NOW,
                              provenance_sink=sink)
    assert changed.value.code == original.value.code
    assert str(changed.value) == str(original.value)
    assert "selected" not in sink


def test_unsupported_sink_cannot_change_normal_dispatch(estate, monkeypatch):
    class ThrowingDict(dict):
        def clear(self):
            raise RuntimeError("private-sink-canary")

    original = _call("boundary", estate)
    monkeypatch.setattr(routing_policy, "_cache", None)
    changed = _call("boundary", estate, provenance_sink=ThrowingDict())
    assert dataclasses.asdict(changed) == dataclasses.asdict(original)


@pytest.mark.parametrize("kind", ["boundary", "selection"])
def test_opt_in_does_not_re_evaluate_supplied_policy_truth(estate, kind):
    checks = []

    class CountedPolicy(routing.PolicySnapshot):
        def __bool__(self):
            checks.append(True)
            return True

    policy = CountedPolicy(version="supplied", policy_hash="a" * 64)
    original = _call(kind, estate, policy=policy)
    count = len(checks)
    checks.clear()
    observed = _call(kind, estate, policy=policy, provenance_sink={})
    assert dataclasses.asdict(original) == dataclasses.asdict(observed)
    assert len(checks) == count
