"""PS-632 — the canonical TargetCapabilityReceipt, its store, and its consumers.

The properties worth reading these tests for:

* a receipt identifies an exact EXECUTION PROFILE, not a host, and a materially
  different runtime/model/context is a different profile that inherits nothing;
* declared metadata never satisfies a requirement that demands a measurement;
* freshness has THREE clocks (liveness, semantic qualification, material identity
  drift) and they do not leak into each other;
* the store is append-only, atomic, hash-verified, and FAILS CLOSED on corruption;
* PS-605 consumes persisted receipts, and the hash of the receipt it used is what
  ends up bound into the dispatch evidence.

Every negative control in the slice's list has a test named after it.
"""
from __future__ import annotations

import datetime
import json
import pytest

from src import dispatch_routing as dr
from src import local_target_routing as ltr
from src.local_targets import (
    ContextProfile,
    DEFAULT_HEALTH_TTL_S, DEFAULT_QUALIFICATION_TTL_S, HEALTH_HEALTHY,
    HEALTH_UNREACHABLE, INVALIDATED_EXPIRED, INVALIDATED_FUTURE,
    INVALIDATED_IDENTITY_DRIFT, INVALIDATED_SAFE_CONTEXT_UNMEASURED,
    INVALIDATED_UNHEALTHY, ROLE_INFERENCE, ROLE_VERIFIER, TOOLS_DECLARED_ONLY,
    TOOLS_PROVEN, LocalTargetSpec, build_capability, execution_profile_id,
    make_target_capability_receipt, receipt_from_capability,
    target_capability_receipt_hash_is_valid)
from src.target_capability_store import (
    CapabilityStoreError, TargetCapabilityStore)

NOW = datetime.datetime.now(datetime.timezone.utc)
DIGEST = "d94d964641c751ddc0ae3d905770095e1c13bc2615964fdc41d0e89ccdc26f28"

PACKET = {
    "packet_id": "P-PS632-1", "objective": "do the thing",
    "role": "local_implementer", "write_scope": ["src/thing.py"],
    "target_requirements": ["native_tools"], "base_sha": "HEAD",
}

RAW = {
    "reachable": True, "version": "0.32.11",
    "model": {"name": "qwen3.8:27b", "digest": DIGEST, "size": 18854541537,
              "details": {"context_length": 262144, "quantization_level": "Q4_K_XL",
                          "family": "qwen3"},
              "capabilities": []},
    "ps": {"models": [{"name": "qwen3.8:27b", "size_vram": 20401094656,
                       "context_length": 32768}]},
    "tool_proof": {"ok": True, "tool_calls": 1},
    "timings": {"decode_tok_s": 37.1, "ttft_s": 0.4},
    "runtime_options": {"num_ctx": 32768, "num_gpu_layers": 99,
                        "thinking": False},
}

#: The runtime ADVERTISES tools and the probe never established a call: the exact
#: shape that must not satisfy a measured requirement.
DECLARED_ONLY = {**RAW, "tool_proof": None,
                 "model": {**RAW["model"], "capabilities": ["completion", "tools"]}}


def spec(target_id="local-rtx4500", *, roles=(ROLE_INFERENCE,),
         qualification_ref="ps632-measured:test", ssh_host="minipc"):
    return LocalTargetSpec(target_id=target_id, label=target_id, ssh_host=ssh_host,
                           endpoint="http://127.0.0.1:11434", model="qwen3.8:27b",
                           roles=tuple(roles), qualification_ref=qualification_ref)


def record(raw=None, *, spec_=None, probed_at=""):
    return build_capability(spec_ or spec(), raw or RAW,
                            probed_at=probed_at or NOW.isoformat())


def receipt(*, raw=None, spec_=None, safe=32768, configured=32768,
            observed_at="", ttl_s=DEFAULT_QUALIFICATION_TTL_S,
            health_ttl_s=DEFAULT_HEALTH_TTL_S, probed_at=""):
    return receipt_from_capability(
        record(raw, spec_=spec_, probed_at=probed_at),
        configured_context=configured, safe_working_context=safe,
        safe_context_source="test measurement", backend="cuda",
        ttl_s=ttl_s, health_ttl_s=health_ttl_s,
        observed_at=observed_at or NOW.isoformat(),
        roles=(spec_ or spec()).roles,
        qualification_ref=(spec_ or spec()).qualification_ref)


def store(tmp_path) -> TargetCapabilityStore:
    return TargetCapabilityStore(str(tmp_path / "target_capabilities"))


# ============================================================ the canonical receipt ===
def test_the_receipt_separates_the_host_from_the_execution_profile():
    one = receipt()
    two = receipt(spec_=spec("local-framework", ssh_host="framework"))
    assert one.host_id == "local-rtx4500" and one.host_id != two.host_id
    # Two profiles on ONE host are different profiles, and neither inherits.
    same_host_other_backend = receipt_from_capability(
        record(), configured_context=32768, safe_working_context=32768,
        backend="vulkan")
    assert same_host_other_backend.host_id == one.host_id
    assert same_host_other_backend.profile_id != one.profile_id
    assert (same_host_other_backend.identity_digest()
            != one.identity_digest())


def test_exact_digest_and_quantization_are_exposed_and_authoritative():
    r = receipt()
    assert r.model.digest == DIGEST
    assert r.model.quantization == "Q4_K_XL"
    assert r.model.family == "qwen3"
    assert r.model.size_bytes == 18854541537
    assert r.runtime.version == "0.32.11"
    assert r.runtime.backend == "cuda"
    assert r.context.configured_context == 32768
    assert r.context.served_context == 32768          # measured from /api/ps
    assert r.context.safe_working_context == 32768    # measurement, not the 262144
    assert r.context.safe_working_context != r.model.declared_context
    assert r.limits.decode_tok_s == 37.1
    assert r.limits.vram_resident_bytes == 20401094656
    assert r.context.options.get("num_ctx") == 32768


def test_the_receipt_hash_covers_its_own_content():
    r = receipt()
    assert r.receipt_hash and target_capability_receipt_hash_is_valid(r.to_dict())
    tampered = r.to_dict()
    tampered["profile_id"] = "somewhere-else"
    assert target_capability_receipt_hash_is_valid(tampered) is False


def test_declared_capabilities_are_recorded_but_never_measured():
    r = receipt(raw=DECLARED_ONLY)
    assert r.capabilities.tool_semantics == TOOLS_DECLARED_ONLY
    assert "tools" in r.capabilities.declared
    assert "native_tools" not in r.capabilities.measured
    assert r.capabilities.measured == ("readonly_analysis",)
    assert receipt().capabilities.tool_semantics == TOOLS_PROVEN
    assert "native_tools" in receipt().capabilities.measured


def test_an_unmeasured_safe_context_is_recorded_as_unqualified():
    r = receipt(safe=0)
    assert r.qualification_state(now=NOW) == INVALIDATED_SAFE_CONTEXT_UNMEASURED
    assert r.model.declared_context == 262144        # still recorded, as DECLARED


# ================================================================== freshness clocks ===
def test_the_three_clocks_are_independent():
    old = (NOW - datetime.timedelta(days=2)).isoformat()
    fresh_health = (NOW - datetime.timedelta(seconds=10)).isoformat()
    r = make_target_capability_receipt(
        **{**receipt(observed_at=old, ttl_s=3600).to_dict(),
           "health_checked_at": fresh_health})
    # The semantic qualification expired; liveness did not. A heartbeat cannot
    # resurrect a qualification, and it cannot extend one either.
    assert r.qualification_state(now=NOW) == INVALIDATED_EXPIRED
    assert r.health_state(now=NOW) == "live"


def test_an_expired_receipt_is_ineligible():
    r = receipt(observed_at=(NOW - datetime.timedelta(days=8)).isoformat())
    assert r.qualification_state(now=NOW) == INVALIDATED_EXPIRED


def test_a_future_dated_receipt_is_invalid():
    r = receipt(observed_at=(NOW + datetime.timedelta(minutes=5)).isoformat())
    # The QUALIFICATION clock rejects it. Liveness is a separate field, and a
    # future observation does not make a stale heartbeat look fresh.
    assert r.qualification_state(now=NOW) == INVALIDATED_FUTURE
    future_heartbeat = make_target_capability_receipt(
        **{**r.to_dict(), "health_checked_at": r.observed_at})
    assert future_heartbeat.health_state(now=NOW) == INVALIDATED_FUTURE


def test_stale_liveness_is_not_qualification_but_blocks_dispatch():
    r = make_target_capability_receipt(
        **{**receipt().to_dict(),
           "health_checked_at": (NOW - datetime.timedelta(hours=1)).isoformat()})
    assert r.qualification_state(now=NOW) == "valid"
    assert r.health_state(now=NOW) == "liveness_expired"


def test_material_identity_drift_invalidates_prior_qualification():
    r = receipt()
    drifted = receipt(raw={**RAW, "model": {**RAW["model"], "digest": "deadbeef" * 8}})
    assert drifted.profile_id != r.profile_id
    assert (drifted.identity_digest() != r.identity_digest())
    assert r.qualification_state(
        now=NOW, current_identity_digest=drifted.identity_digest()
    ) == INVALIDATED_IDENTITY_DRIFT
    assert r.qualification_ok(now=NOW) is True


def test_a_profile_id_changes_when_any_material_input_changes():
    base = execution_profile_id(host_id="local-rtx4500", runtime_kind="ollama",
                                backend="cuda", model_alias="qwen3.8:27b",
                                quantization="Q4_K_XL", safe_working_context=32768,
                                model_digest=DIGEST)
    assert base == execution_profile_id(
        host_id="local-rtx4500", runtime_kind="ollama", backend="cuda",
        model_alias="qwen3.8:27b", quantization="Q4_K_XL",
        safe_working_context=32768, model_digest=DIGEST)
    base_kwargs = dict(host_id="local-rtx4500", runtime_kind="ollama",
                       backend="cuda", model_alias="qwen3.8:27b",
                       quantization="Q4_K_XL", safe_working_context=32768,
                       model_digest=DIGEST)
    for changed in ({"backend": "vulkan"}, {"quantization": "Q8_0"},
                    {"safe_working_context": 131072},
                    {"model_digest": "0" * 64}, {"runtime_kind": "llama.cpp"}):
        assert execution_profile_id(**{**base_kwargs, **changed}) != base


# ================================================================ the store ===
def test_the_store_appends_and_keeps_history(tmp_path):
    s = store(tmp_path)
    first = receipt()
    s.append(first)
    second = receipt(raw={**RAW, "model": {**RAW["model"], "digest": "aa" * 32}})
    s.append(second, supersedes=first.receipt_hash)
    entries = s.entries()
    # Both receipts are kept, oldest first; the superseding one carries its own
    # hash (which covers the supersedes field) rather than the pre-append one.
    assert len(entries) == 2
    assert entries[0].receipt_hash == first.receipt_hash
    current = s.current(second.profile_id)
    assert current.receipt_hash != first.receipt_hash
    assert current.receipt_hash == entries[1].receipt_hash
    assert current.supersedes == first.receipt_hash
    assert current.model.digest == "aa" * 32
    assert s.verify()["ok"] is True


def test_the_current_receipt_is_deterministic_and_recorded_in_the_index(tmp_path):
    s = store(tmp_path)
    s.append(receipt(observed_at=(NOW - datetime.timedelta(hours=1)).isoformat()))
    first = s.current(receipt(observed_at=(NOW - datetime.timedelta(hours=1))
                              .isoformat()).profile_id)
    newest = receipt(observed_at=NOW.isoformat())
    s.append(newest, supersedes=first.receipt_hash)
    current = s.current(newest.profile_id)
    index = json.loads((tmp_path / "target_capabilities" / "current.json").read_text())
    assert index[current.profile_id]["receipt_hash"] == current.receipt_hash
    assert index[current.profile_id]["previous_receipt_hash"] == first.receipt_hash


def test_a_corrupt_line_fails_closed(tmp_path):
    s = store(tmp_path)
    s.append(receipt())
    with open(s.receipts_path, "a", encoding="utf-8") as handle:
        handle.write('{"profile_id": "not-a-receipt"}\n')
    with pytest.raises(CapabilityStoreError):
        s.entries()
    assert s.verify()["ok"] is False


def test_a_tampered_receipt_fails_closed(tmp_path):
    s = store(tmp_path)
    s.append(receipt())
    line = json.loads(open(s.receipts_path).read().strip())
    line["model"]["digest"] = "0" * 64                     # content changed, hash not
    with open(s.receipts_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(line, sort_keys=True) + "\n")
    with pytest.raises(CapabilityStoreError) as err:
        s.entries()
    assert "receipt_hash does not cover" in str(err.value)


def test_an_index_entry_whose_receipt_is_missing_fails_closed(tmp_path):
    s = store(tmp_path)
    s.append(receipt())
    index_path = tmp_path / "target_capabilities" / "current.json"
    index = json.loads(index_path.read_text())
    for entry in index.values():
        entry["receipt_hash"] = "f" * 64
    index_path.write_text(json.dumps(index))
    with pytest.raises(CapabilityStoreError):
        s.current(next(iter(index)))
    report = s.verify()
    assert report["ok"] is False and "missing from the ledger" in report["problems"][0]


def test_a_missing_store_is_empty_not_corrupt(tmp_path):
    s = store(tmp_path)
    assert s.entries() == ()
    assert s.current("nothing-here") is None
    assert s.verify() == {"ok": True, "receipts": 0, "profiles": [], "problems": []}


def test_mark_invalidated_appends_evidence_and_never_deletes(tmp_path):
    s = store(tmp_path)
    r = receipt()
    s.append(r)
    invalidated = s.mark_invalidated(r.profile_id, INVALIDATED_IDENTITY_DRIFT)
    assert invalidated.invalidation_reason == INVALIDATED_IDENTITY_DRIFT
    assert len(s.entries()) == 2
    assert s.current(r.profile_id).qualification_state(now=NOW) == INVALIDATED_IDENTITY_DRIFT


# ========================================================= routing consumes the store ===
def _inputs(s, **kwargs):
    return ltr.persisted_routing_inputs(s, now=kwargs.pop("now", NOW), **kwargs)


def test_a_fresh_receipt_routes_and_binds_its_own_hash(tmp_path):
    s = store(tmp_path)
    r = receipt()
    s.append(r)
    inputs = _inputs(s)
    assert inputs.target_ids() == ("local-rtx4500",)
    assert inputs.profiles[0].profile_id == r.profile_id        # exact profile
    assert inputs.profiles[0].model_digest == DIGEST
    assert inputs.profiles[0].network_policy == r.network_class
    assert inputs.receipts[0].provenance == dr.PROVENANCE_MEASURED
    assert inputs.receipts[0].source_receipt_hash == r.receipt_hash
    bound = ltr.resolve_persisted_dispatch(
        inputs, packet=PACKET, role="local_implementer",
        execution_package_hash="pkg", run_id="r1", decision_id="d1")
    kwargs = bound.decision.to_ps638_receipt_kwargs()
    assert bound.decision.selected_profile.target_id == "local-rtx4500"
    # The PS-632 receipt identity is what the receipt REFERS to.
    assert kwargs["capability_receipt_refs"] == (r.receipt_hash,)


# ---------------------------------------------------------------- negative controls ---
def test_control_1_mutating_the_model_digest_after_sealing_invalidates(tmp_path):
    s = store(tmp_path)
    s.append(receipt())
    line = json.loads(open(s.receipts_path).read().strip())
    line["model"]["digest"] = "0" * 64
    open(s.receipts_path, "w").write(json.dumps(line, sort_keys=True) + "\n")
    # The store's own audit refuses it...
    with pytest.raises(CapabilityStoreError) as err:
        s.entries()
    assert "receipt_hash does not cover" in str(err.value)
    # ...and routing refuses the host rather than reading past the corruption.
    inputs = _inputs(s)
    assert inputs.target_ids() == ()
    assert "capability store unusable" in inputs.skipped[0]["reason"]


def test_control_2_mutating_runtime_or_profile_identity_invalidates(tmp_path):
    s = store(tmp_path)
    r = receipt()
    s.append(r)
    drifted = make_target_capability_receipt(
        **{**r.to_dict(), "runtime": {**r.runtime.to_dict(), "version": "9.9.9"}})
    assert drifted.identity_digest() != r.identity_digest()
    assert drifted.profile_id != r.profile_id
    s2 = store(tmp_path / "second")
    s2.append(drifted)
    # The re-measured runtime is a new profile and routes as one; the OLD receipt
    # cannot be used to describe it.
    assert _inputs(s2).profiles[0].profile_id == drifted.profile_id
    assert r.qualification_state(
        now=NOW, current_identity_digest=drifted.identity_digest()
    ) == INVALIDATED_IDENTITY_DRIFT


def test_control_3_an_expired_receipt_is_ineligible(tmp_path):
    s = store(tmp_path)
    s.append(receipt(observed_at=(NOW - datetime.timedelta(days=9)).isoformat()))
    inputs = _inputs(s)
    assert inputs.target_ids() == ()
    assert INVALIDATED_EXPIRED in inputs.skipped[0]["reason"]


def test_control_4_a_future_dated_receipt_is_ineligible(tmp_path):
    s = store(tmp_path)
    s.append(receipt(observed_at=(NOW + datetime.timedelta(hours=1)).isoformat()))
    inputs = _inputs(s)
    assert inputs.target_ids() == ()
    assert INVALIDATED_FUTURE in inputs.skipped[0]["reason"]


def test_control_5_a_missing_receipt_fails_closed(tmp_path):
    s = store(tmp_path)
    assert _inputs(s).target_ids() == ()
    assert "no persisted capability receipt" in _inputs(s).skipped[0]["reason"]
    with pytest.raises(ltr.FleetRoutingError):
        ltr.resolve_persisted_dispatch(
            _inputs(s), packet=PACKET, role="local_implementer")


def test_control_6_a_corrupt_store_fails_closed(tmp_path):
    s = store(tmp_path)
    s.append(receipt())
    with open(s.receipts_path, "a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    inputs = _inputs(s)
    assert inputs.target_ids() == ()
    assert "capability store unusable" in inputs.skipped[0]["reason"]


def test_control_7_removing_a_measured_capability_stops_tool_work(tmp_path):
    s = store(tmp_path)
    s.append(receipt(raw=DECLARED_ONLY))
    inputs = _inputs(s)
    # The host is healthy and qualified, but it no longer PROVES a tool call.
    with pytest.raises(dr.RoutingRefused) as err:
        ltr.resolve_persisted_dispatch(
            inputs, packet=PACKET, role="local_implementer",
            execution_package_hash="pkg", run_id="r1")
    rules = {a.profile_id: a.rule for a in err.value.assessments}
    assert set(rules.values()) <= {dr.REFUSED_ROLE, dr.REFUSED_CAPABILITY_MISSING,
                                   dr.REFUSED_TOOL}


def test_control_8_declared_only_cannot_satisfy_a_measured_requirement(tmp_path):
    s = store(tmp_path)
    s.append(receipt(raw=DECLARED_ONLY))
    inputs = _inputs(s)
    assert ltr.requirement_capabilities(["native_tools"]) == (dr.CAP_SINGLE_TOOL_CALL,)
    assert dr.CAP_SINGLE_TOOL_CALL not in inputs.receipts[0].capabilities
    # A read-only packet, however, is still served by the same node.
    read_only = {**PACKET, "write_scope": [], "target_requirements":
                 ["readonly_analysis"]}
    bound = ltr.resolve_persisted_dispatch(
        inputs, packet=read_only, role="read_only_analyst",
        execution_package_hash="pkg", run_id="r2")
    assert bound.decision.selected_profile.target_id == "local-rtx4500"


def test_control_9_msr1_cannot_route_inference(tmp_path):
    s = store(tmp_path)
    # Even a perfectly good receipt for the ARM node does not make it a worker.
    s.append(receipt(spec_=spec("local-msr1", roles=(ROLE_VERIFIER,),
                                qualification_ref="ps637-verifier-role",
                                ssh_host="msr1")))
    inputs = _inputs(s)
    assert inputs.target_ids() == ()
    reasons = {entry["target_id"]: entry["reason"] for entry in inputs.skipped}
    assert "inference role" in reasons["local-msr1"]
    with pytest.raises(ltr.FleetRoutingError):
        ltr.resolve_persisted_dispatch(
            inputs, packet=PACKET, role="local_implementer")


def test_control_9b_the_real_registry_routes_only_the_rtx_host(tmp_path):
    from src.local_targets import registered_targets

    s = store(tmp_path)
    s.append(receipt())
    inputs = _inputs(s, specs=registered_targets())
    assert inputs.target_ids() == ("local-rtx4500",)
    reasons = {entry["target_id"]: entry["reason"] for entry in inputs.skipped}
    assert "inference role" in reasons["local-msr1"]
    assert "unqualified" in reasons["local-framework"]


def test_control_10_a_receipt_not_bound_into_the_evidence_is_detectable(tmp_path):
    s = store(tmp_path)
    r = receipt()
    s.append(r)
    bound = ltr.resolve_persisted_dispatch(
        _inputs(s), packet=PACKET, role="local_implementer",
        execution_package_hash="pkg", run_id="r1", decision_id="d-bound")
    kwargs = bound.decision.to_ps638_receipt_kwargs()
    # A receipt the evidence does not refer to cannot be substituted silently: the
    # dispatch receipt's refs are the ONLY receipts the decision relied on...
    other = receipt(raw={**RAW, "model": {**RAW["model"], "digest": "bb" * 32}})
    assert other.receipt_hash not in kwargs["capability_receipt_refs"]
    assert kwargs["capability_receipt_refs"] == (r.receipt_hash,)
    # ...and the receipt hash covers those refs, so editing them is visible.
    mutated = {**kwargs, "capability_receipt_refs": (other.receipt_hash,)}
    assert dr.ps638_receipt_hash(mutated) != dr.ps638_receipt_hash(kwargs)


def test_the_persisted_path_reads_only_and_selects_nothing(tmp_path):
    s = store(tmp_path)
    _inputs(s)
    assert list(s.entries()) == []


def test_pre_refinement_receipt_round_trips_without_rehashing_history():
    old = receipt(safe=32768, configured=262144)
    line = old.to_dict()
    assert "engine_demonstrated_context" not in line["context"]
    rebuilt = make_target_capability_receipt(**line)
    assert rebuilt.receipt_hash == old.receipt_hash


def test_refined_context_fields_round_trip():
    raw = receipt(safe=32768, configured=262144).to_dict()
    raw["context"] = ContextProfile(
        configured_context=262144, served_context=65536,
        safe_working_context=32768,
        safe_context_source="engine-demonstrated; semantic-verified",
        engine_demonstrated_context=32768,
        semantic_verified_context=19760,
        options={"parallel": 4}).to_dict()
    refined = make_target_capability_receipt(**raw)
    rebuilt = make_target_capability_receipt(**refined.to_dict())
    assert rebuilt.context.engine_demonstrated_context == 32768
    assert rebuilt.context.semantic_verified_context == 19760
    assert rebuilt.receipt_hash == refined.receipt_hash


def test_store_fails_closed_when_reconstruction_diverges(tmp_path, monkeypatch):
    s = store(tmp_path)
    s.append(receipt())
    original = ContextProfile.to_dict

    def always_emit(self):
        out = dict(original(self))
        out["engine_demonstrated_context"] = self.engine_demonstrated_context or 0
        return out

    monkeypatch.setattr(ContextProfile, "to_dict", always_emit)
    with pytest.raises(CapabilityStoreError, match="does not round-trip"):
        s.entries()
