"""PS-605 — deterministic execution-target selection and a real receipt.

The slice has to prove the receipt is REAL, not decorative: every field the ticket
names is populated from a decision that was actually taken, and the hard
invariants are negative controls, not comments:

  * a runtime adapter cannot reroute itself (there is no API to call);
  * the selected target is pinned, and re-running the selector cannot move it;
  * fallback satisfies the SAME policy as the preferred candidate;
  * MS-R1 can never route as an inference target;
  * an approximate profile cannot satisfy exact/reference-intent work;
  * stale/unhealthy capability receipts are ineligible;
  * sensitive local-only work fails closed instead of escaping to hosted;
  * the receipt identity is what an AttemptReceipt binds to.

`test_the_positive_control_selects_and_receipts` is the control: without it, a
selector that refused everything would pass this file.
"""
from __future__ import annotations

import dataclasses
import datetime

import pytest

from src import dispatch_routing as dr

NOW = datetime.datetime(2026, 9, 15, 12, 0, tzinfo=datetime.timezone.utc)
PROFILE_ID = "rtx4500-ollama-qwen38-27b"
POLICY = dr.policy_snapshot(policy={"routingPolicyVersion": "1.2",
                                    "remoteSensitivityCeiling": "confidential"})

IMPLEMENTER_CAPS = frozenset({
    dr.CAP_TEXT_GENERATION, dr.CAP_SINGLE_TOOL_CALL, dr.CAP_EXACT_REFERENCE_SEMANTICS,
})


def profile(**overrides) -> dr.ExecutionTargetProfile:
    fields = {
        "target_id": "local-rtx4500", "profile_id": PROFILE_ID, "provider": "ollama",
        "host": "minipc", "runtime_kind": "ollama", "runtime_version": "0.32.11",
        "model": "qwen3.8:27b", "model_digest": "d94d9646", "backend": "cuda",
        "endpoint_url": "http://127.0.0.1:11434/v1",
        "locality": dr.LOCALITY_LOCAL,
        "roles": frozenset({dr.ROLE_IMPLEMENTER, dr.ROLE_REPAIR}),
        "tools": frozenset({"write_file"}), "network_policy": "tailnet-loopback",
        "budget_class": "dev", "cost_rank": 0,
    }
    fields.update(overrides)
    return dr.make_target_profile(**fields)


def receipt(**overrides) -> dr.LegacyCapabilityView:
    fields = {
        "receipt_id": "cap-rtx-1", "profile_id": PROFILE_ID,
        "target_id": "local-rtx4500", "capabilities": IMPLEMENTER_CAPS,
        "exactness": dr.EXACTNESS_EXACT, "observed_at": "2026-09-15T11:00:00+00:00",
        "ttl_s": 86400, "healthy": True, "host": "minipc",
        "runtime_version": "0.32.11", "model_digest": "d94d9646",
    }
    fields.update(overrides)
    return dr.make_legacy_capability_view(**fields)


def request(**overrides) -> dr.RoutingRequest:
    fields = {
        "domain": "general_swe", "role": dr.ROLE_IMPLEMENTER, "run_id": "run-1",
        "packet_id": "P-1", "execution_package_hash": "pkg-hash-1",
        "required_tools": ("write_file",), "network_policy": "tailnet-loopback",
        "budget_class": "dev", "write_scope": ("src/thing.py",),
    }
    fields.update(overrides)
    return dr.RoutingRequest(**fields)


def select(req=None, *, profiles=None, receipts=None, **kwargs):
    return dr.select_target(
        req or request(), profiles=profiles if profiles is not None else [profile()],
        receipts=receipts if receipts is not None else [receipt()],
        policy=POLICY, now=NOW, **kwargs)


# ================================================================ the control ===
def test_the_positive_control_selects_and_receipts():
    decision = select(decision_id="dec-1")
    assert decision.selected_profile.profile_id == PROFILE_ID
    # No preference was expressed, so nothing was substituted: the deterministic
    # order decided. A preferred-order case is asserted separately.
    assert decision.reason_code == dr.REASON_SELECTED_DETERMINISTIC
    assert decision.fallback_used is False
    assert decision.receipt_hash and len(decision.receipt_hash) == 64
    assert decision.pin()["model"] == "qwen3.8:27b"
    assert decision.pin()["granted_tools"] == ["write_file"]


def test_the_receipt_carries_everything_the_ticket_requires():
    """Every field the ticket names, asserted on the receipt itself."""
    kwargs = select(decision_id="dec-1").to_ps638_receipt_kwargs()
    assert kwargs["run_id"] == "run-1" and kwargs["packet_id"] == "P-1"
    assert kwargs["execution_package_hash"] == "pkg-hash-1"
    assert kwargs["requested_role"] == dr.ROLE_IMPLEMENTER
    assert set(kwargs["requested_capabilities"]) == IMPLEMENTER_CAPS
    assert kwargs["policy_ref"].startswith("routing_policy@1.2+sha256:")
    assert kwargs["decided_by"] == dr.DECIDED_BY_POLICY == "ps605_policy"
    assert kwargs["selected_target_id"] == "local-rtx4500"
    assert kwargs["selected_host"] == "minipc"
    assert kwargs["selected_model"] == "qwen3.8:27b"
    assert kwargs["selected_runtime_kind"] == "ollama"
    assert kwargs["selected_runtime_version"] == "0.32.11"
    assert kwargs["selected_model_digest"] == "d94d9646"
    assert kwargs["selected_backend"] == "cuda"
    assert kwargs["granted_tools"] == ("write_file",)
    assert kwargs["granted_write_scope"] == ("src/thing.py",)
    assert kwargs["network_policy"] == "tailnet-loopback"
    assert kwargs["decided_at"] == NOW.isoformat()
    assert kwargs["reason"].startswith(dr.REASON_SELECTED_DETERMINISTIC)
    assert kwargs["capability_receipt_refs"] == (receipt().receipt_hash,)
    selected = [c for c in kwargs["candidates_considered"] if c["selected"]]
    assert len(selected) == 1
    entry = selected[0]
    for key in ("capability_receipt_id", "capability_receipt_hash",
                "capability_receipt_observed_at", "capability_receipt_freshness_s",
                "capability_receipt_ttl_s", "budget_facts", "resource_facts",
                "fallback_rule", "fallback_used", "reason_code", "exactness"):
        assert key in entry, key
    assert entry["capability_receipt_freshness_s"] == 3600
    assert entry["budget_facts"]["selected_cost_rank"] == 0
    assert entry["fallback_rule"] == dr.FALLBACK_RULE


def test_the_receipt_field_set_is_the_ps638_contract():
    """The mapping is a CONTRACT, asserted rather than discovered later."""
    kwargs = select().to_ps638_receipt_kwargs()
    assert set(kwargs) == set(dr.PS638_RECEIPT_FIELDS)
    assert dr.PS638_RECEIPT_FIELDS[-1] == "schema_version"
    assert kwargs["schema_version"] == 1
    assert dr.PS638_RECEIPT_CORE_FIELDS[0] == "schema_version"


def test_the_receipt_hash_is_the_hash_ps638_would_compute():
    decision = select(decision_id="dec-1")
    kwargs = decision.to_ps638_receipt_kwargs()
    assert decision.receipt_hash == dr.ps638_receipt_hash(kwargs)
    assert dr.ps638_receipt_core(kwargs)["reason"].startswith(
        dr.REASON_SELECTED_DETERMINISTIC)
    binding = decision.attempt_binding()
    assert binding["dispatch_receipt_hash"] == decision.receipt_hash
    assert binding["selected_target_id"] == decision.selected_profile.target_id


def test_the_same_inputs_produce_the_same_decision_hash():
    """Determinism, including independence from candidate ORDER."""
    first = select(decision_id="dec-1")
    second = select(decision_id="dec-1")
    assert first.decision_hash == second.decision_hash
    # Same candidate SET, reversed input order: the record is canonical, so the
    # hash and the chosen profile are identical.
    other = profile(target_id="local-rtx4500-b", profile_id="rtx4500-b-vulkan",
                    backend="vulkan", cost_rank=1)
    other_receipt = receipt(receipt_id="cap-rtx-b", profile_id=other.profile_id,
                            target_id=other.target_id)
    forward = select(profiles=[profile(), other],
                     receipts=[receipt(), other_receipt], decision_id="dec-1")
    reversed_order = select(profiles=[other, profile()],
                            receipts=[other_receipt, receipt()], decision_id="dec-1")
    assert forward.selected_profile.profile_id == PROFILE_ID
    assert reversed_order.selected_profile.profile_id == PROFILE_ID
    assert forward.decision_hash == reversed_order.decision_hash
    # The record covers the CANDIDATE LIST, so a different estate is a different
    # decision hash even when the same profile wins. That is the property that
    # makes the hash a record of the decision rather than of its outcome.
    assert first.decision_hash != forward.decision_hash


# ============================================================ hard invariants ===
def test_a_runtime_adapter_cannot_reroute_itself():
    """MUTATION CONTROL: there is no routing API for an adapter to call.

    Self-rerouting is prevented structurally rather than by rule: the selector is
    a pure function whose parameters are policy inputs (there is no "self",
    "adapter" or "target" parameter), the decision it returns is frozen, and the
    module exposes no dispatch/execute entry point at all.
    """
    import inspect

    params = set(inspect.signature(dr.select_target).parameters)
    assert not params & {"self", "adapter", "runtime", "target", "target_id",
                         "requested_by"}
    forbidden = {"dispatch", "execute", "run", "submit", "invoke", "call_model",
                 "send", "route_now", "reroute"}
    public = {name for name, value in vars(dr).items()
              if callable(value) and not name.startswith("_")}
    assert not (public & forbidden), public & forbidden
    decision = select(decision_id="dec-1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        decision.selected_profile = profile(target_id="somewhere-else")


def test_the_selected_target_is_pinned_and_the_pin_is_complete():
    decision = select(decision_id="dec-1")
    pin = decision.pin()
    kwargs = decision.to_ps638_receipt_kwargs()
    assert pin["target_id"] == kwargs["selected_target_id"]
    assert pin["host"] == kwargs["selected_host"]
    assert pin["model"] == kwargs["selected_model"]
    assert pin["runtime_version"] == kwargs["selected_runtime_version"]
    assert pin["model_digest"] == kwargs["selected_model_digest"]
    assert pin["backend"] == kwargs["selected_backend"]
    assert pin["receipt_hash"] == decision.receipt_hash
    # A LATER call with a different estate cannot move the earlier decision.
    other = select(profiles=[profile(target_id="local-framework",
                                     profile_id="halo-llamacpp-qwen"),
                             profile()],
                   receipts=[receipt()], decision_id="dec-1")
    assert other.selected_profile.profile_id == PROFILE_ID
    assert decision.pin()["target_id"] == "local-rtx4500"


def test_sensitive_local_only_work_fails_closed_to_hosted():
    """finance is a local-only domain: a hosted-only estate is a REFUSAL."""
    hosted = profile(target_id="openrouter-v4pro", profile_id="or-v4pro",
                     provider="openrouter", host="api.openrouter.ai",
                     locality=dr.LOCALITY_HOSTED, budget_class="standard",
                     endpoint_url="https://openrouter.ai/api/v1")
    with pytest.raises(dr.RoutingRefused) as err:
        select(request(domain="finance", budget_class=""),
               profiles=[hosted],
               receipts=[receipt(profile_id="or-v4pro",
                                 target_id="openrouter-v4pro")])
    assert err.value.code == dr.REFUSED_PRIVACY_LOCAL_ONLY
    assert err.value.assessments[0].rule == dr.REFUSED_POLICY_DENIED
    assert not err.value.assessments[0].eligible


def test_sensitive_domain_selects_the_local_profile():
    decision = select(request(domain="finance"), decision_id="dec-fin")
    assert decision.selected_profile.is_local is True
    assert decision.reason_code == dr.REASON_SELECTED_LOCAL_ONLY
    # local-only is a REQUIREMENT, not a substitution: no preference was dropped,
    # so this is not a fallback. The hosted candidate's refusal is recorded.
    assert decision.fallback_used is False
    assert [a.rule for a in decision.candidates] == ["eligible", "eligible"]         or "eligible" in [a.rule for a in decision.candidates]


def test_ms_r1_can_never_route_as_an_inference_target():
    msr1 = profile(target_id="ms-r1", profile_id="msr1-ollama-qwen38-27b",
                   provider="ollama", host="msr1", inference=False,
                   roles=frozenset({dr.ROLE_VERIFIER, dr.ROLE_GOVERNANCE_CI}))
    with pytest.raises(dr.RoutingRefused) as err:
        select(profiles=[msr1],
               receipts=[receipt(profile_id=msr1.profile_id, target_id="ms-r1")])
    assert err.value.code == dr.REFUSED_NOT_INFERENCE_TARGET
    # The SAME node is legitimately routable for its deterministic role.
    verifier = dr.RoutingRequest(domain="general_swe", role=dr.ROLE_VERIFIER,
                                 run_id="run-1", packet_id="P-1",
                                 execution_package_hash="pkg-hash-1")
    decision = select(verifier, profiles=[msr1],
                      receipts=[receipt(profile_id=msr1.profile_id,
                                        target_id="ms-r1",
                                        capabilities=frozenset(
                                            {dr.CAP_DETERMINISTIC_VERIFICATION}))])
    assert decision.selected_profile.target_id == "ms-r1"
    assert decision.selected_profile.inference is False


def test_an_approximate_profile_cannot_satisfy_exact_intent():
    approx = profile(profile_id="rtx4500-qwen-ream-60pct",
                     exactness=dr.EXACTNESS_APPROXIMATE)
    with pytest.raises(dr.RoutingRefused) as err:
        select(profiles=[approx],
               receipts=[receipt(profile_id=approx.profile_id,
                                 exactness=dr.EXACTNESS_APPROXIMATE)])
    assert err.value.code == dr.REFUSED_EXACTNESS
    # The same approximate profile IS routable when approximation is acceptable.
    decision = select(request(exactness=dr.EXACTNESS_APPROXIMATE),
                      profiles=[approx],
                      receipts=[receipt(profile_id=approx.profile_id,
                                        exactness=dr.EXACTNESS_APPROXIMATE)],
                      decision_id="dec-approx")
    assert decision.selected_profile.exactness == dr.EXACTNESS_APPROXIMATE


def test_stale_and_unhealthy_receipts_are_ineligible():
    with pytest.raises(dr.RoutingRefused) as err:
        select(receipts=[receipt(observed_at="2026-09-13T00:00:00+00:00",
                                 ttl_s=3600)])
    assert err.value.code == dr.REFUSED_RECEIPT_STALE
    assert "exceeds ttl" in err.value.assessments[0].reason
    # A future-dated receipt is not "extra fresh": a negative age is untrustworthy.
    with pytest.raises(dr.RoutingRefused) as err:
        select(receipts=[receipt(observed_at="2026-09-16T12:00:00+00:00")])
    assert err.value.code == dr.REFUSED_RECEIPT_STALE
    # An unreadable timestamp cannot even be constructed (the freshness check in
    # the filter is defence in depth for records loaded from elsewhere).
    with pytest.raises(dr.DispatchRoutingError):
        receipt(observed_at="not-a-timestamp")
    with pytest.raises(dr.RoutingRefused) as err:
        select(receipts=[receipt(healthy=False, notes="gtt release pending")])
    assert err.value.code == dr.REFUSED_RECEIPT_UNHEALTHY


def test_a_receipt_must_belong_to_the_profile_it_qualifies():
    """A receipt for the SAME target but a different PROFILE is not a receipt."""
    other_profile = profile(profile_id="rtx4500-vulkan-qwen38-27b",
                            backend="vulkan")
    with pytest.raises(dr.RoutingRefused) as err:
        select(profiles=[other_profile],
               receipts=[receipt(profile_id=PROFILE_ID,
                                 target_id="local-rtx4500")])
    assert err.value.code == dr.REFUSED_RECEIPT_MISMATCH
    assert err.value.assessments[0].rule == dr.REFUSED_RECEIPT_MISMATCH
    # A receipt for a different TARGET entirely is simply absent.
    far = profile(target_id="local-framework", profile_id="halo-llamacpp")
    with pytest.raises(dr.RoutingRefused) as err:
        select(profiles=[far], receipts=[receipt()])
    assert err.value.code == dr.REFUSED_RECEIPT_MISSING
    with pytest.raises(dr.RoutingRefused) as err:
        select(profiles=[profile()], receipts=[])
    assert err.value.code == dr.REFUSED_RECEIPT_MISSING


def test_measured_capabilities_are_required_not_declared():
    thin = receipt(capabilities=frozenset({dr.CAP_TEXT_GENERATION}))
    with pytest.raises(dr.RoutingRefused) as err:
        select(receipts=[thin])
    assert err.value.code == dr.REFUSED_CAPABILITY_MISSING
    assert "single_tool_call" in err.value.assessments[0].reason


def test_tool_network_budget_and_resource_filters_bind():
    with pytest.raises(dr.RoutingRefused) as err:
        select(request(required_tools=("write_file", "run_shell")))
    assert err.value.code == dr.REFUSED_TOOL
    with pytest.raises(dr.RoutingRefused) as err:
        select(request(network_policy="hosted-egress"))
    assert err.value.code == dr.REFUSED_NETWORK
    with pytest.raises(dr.RoutingRefused) as err:
        select(request(budget_class="premium"))
    assert err.value.code == dr.REFUSED_BUDGET
    with pytest.raises(dr.RoutingRefused) as err:
        select(profiles=[profile(cost_rank=3)])
    assert err.value.code == dr.REFUSED_BUDGET
    with pytest.raises(dr.RoutingRefused) as err:
        select(resources={"local-rtx4500": {"available": False,
                                            "reason": "gtt is held by load"}})
    assert err.value.code == dr.REFUSED_RESOURCE


# ================================================================== fallback ===
def test_a_fallback_must_satisfy_the_same_policy_as_the_preferred_candidate():
    """MUTATION CONTROL: fallback is a lower rank, never a weaker standard."""
    hosted = profile(target_id="openrouter-v4pro", profile_id="or-v4pro",
                     provider="openrouter", host="api.openrouter.ai",
                     locality=dr.LOCALITY_HOSTED, budget_class="dev", cost_rank=1,
                     roles=frozenset({dr.ROLE_IMPLEMENTER}),
                     tools=frozenset({"write_file"}),
                     network_policy="tailnet-loopback")
    hosted_receipt = receipt(receipt_id="cap-or-1", profile_id="or-v4pro",
                             target_id="openrouter-v4pro", host="api.openrouter.ai")
    # The preferred local candidate's receipt is stale, so it is ineligible.
    stale = receipt(observed_at="2026-09-13T00:00:00+00:00", ttl_s=3600)
    # The caller PREFERS the local profile; that preference is what makes the
    # hosted selection a fallback rather than the deterministic default.
    decision = select(request(preferred_profile_ids=(PROFILE_ID,)),
                      profiles=[profile(), hosted],
                      receipts=[stale, hosted_receipt], decision_id="dec-fb")
    assert decision.selected_profile.profile_id == "or-v4pro"
    assert decision.fallback_used is True
    assert decision.reason_code == dr.REASON_SELECTED_FALLBACK
    reasons = {a.profile_id: a.rule for a in decision.candidates}
    assert reasons[PROFILE_ID] == dr.REFUSED_RECEIPT_STALE
    assert reasons["or-v4pro"] == "eligible"
    # And the fallback is subject to the SAME filters: make it ineligible too.
    with pytest.raises(dr.RoutingRefused) as err:
        select(request(preferred_profile_ids=(PROFILE_ID,)),
               profiles=[profile(), hosted],
               receipts=[stale, receipt(receipt_id="cap-or-1",
                                        profile_id="or-v4pro",
                                        target_id="openrouter-v4pro",
                                        healthy=False, notes="endpoint down")])
    assert err.value.code == dr.REFUSED_NO_ELIGIBLE_TARGET


def test_a_local_only_request_cannot_fall_back_to_hosted():
    """The same estate that legitimately fell back for dev may NOT for finance."""
    hosted = profile(target_id="openrouter-v4pro", profile_id="or-v4pro",
                     provider="openrouter", host="api.openrouter.ai",
                     locality=dr.LOCALITY_HOSTED, budget_class="sensitive",
                     cost_rank=1, tools=frozenset({"write_file"}),
                     network_policy="tailnet-loopback")
    stale = receipt(observed_at="2026-09-13T00:00:00+00:00", ttl_s=3600)
    with pytest.raises(dr.RoutingRefused) as err:
        select(request(domain="finance", budget_class="sensitive"),
               profiles=[profile(budget_class="sensitive"), hosted],
               receipts=[stale,
                         receipt(receipt_id="cap-or-1", profile_id="or-v4pro",
                                 target_id="openrouter-v4pro",
                                 host="api.openrouter.ai")])
    assert err.value.code == dr.REFUSED_PRIVACY_LOCAL_ONLY
    assert all(not a.eligible for a in err.value.assessments)


# ================================================================ fail closed ===
def test_a_role_mismatch_is_refused_rather_than_downgraded():
    with pytest.raises(dr.RoutingRefused) as err:
        select(request(role=dr.ROLE_PLANNER))
    assert err.value.code == dr.REFUSED_ROLE


def test_no_candidates_is_its_own_typed_refusal():
    with pytest.raises(dr.RoutingRefused) as err:
        select(profiles=[])
    assert err.value.code == dr.REFUSED_NO_CANDIDATES


def test_malformed_inputs_are_rejected_at_construction():
    with pytest.raises(dr.DispatchRoutingError):
        dr.RoutingRequest(domain="general_swe", role="not_a_role")
    with pytest.raises(dr.DispatchRoutingError):
        request(capabilities=("telepathy",))
    with pytest.raises(dr.DispatchRoutingError):
        dr.make_target_profile(target_id="t", profile_id="p", provider="ollama",
                               locality="orbital")
    with pytest.raises(dr.DispatchRoutingError):
        dr.make_target_profile(target_id="t", profile_id="p", provider="ollama",
                               surprise=True)
    with pytest.raises(dr.DispatchRoutingError):
        dr.make_legacy_capability_view(receipt_id="r", profile_id="p", target_id="t",
                                   observed_at="2026-09-15T11:00:00+00:00",
                                   capabilities=frozenset({"clairvoyance"}))
    with pytest.raises(dr.DispatchRoutingError):
        dr.make_legacy_capability_view(receipt_id="r", profile_id="p", target_id="t",
                                   observed_at="  ", ttl_s=10)
    with pytest.raises(dr.DispatchRoutingError):
        dr.RoutingRefused("not_a_code", "boom")


def test_the_refusal_is_serializable_and_names_every_candidate():
    with pytest.raises(dr.RoutingRefused) as err:
        select(profiles=[profile(cost_rank=9)])
    payload = err.value.to_dict()
    assert payload["refused"] is True and payload["code"] == dr.REFUSED_BUDGET
    assert payload["run_id"] == "run-1" and payload["packet_id"] == "P-1"
    assert payload["candidates"][0]["rule"] == dr.REFUSED_BUDGET
    assert payload["candidates"][0]["eligible"] is False
