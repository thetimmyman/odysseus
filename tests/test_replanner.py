"""Hermetic controls for the pure bounded advisory gate."""
from dataclasses import replace

import pytest

from src.replanner import (
    MAX_APPROACH_CHARS, PlannerInput, approach_digest, derive_planner_input,
    envelope_digest,
    make_manager_proposal, parse_proposal_text, validate_proposal,
)
from src.repair_packet import parse_verification_failure
from src.dispatch_routing import ps638_receipt_hash
from tests.test_local_worker_loop import _bound, _package


def _input(**overrides):
    values = dict(
        run_id="run", packet_id="packet", package_hash="a" * 64,
        dispatch_hash="b" * 64, boundary="stall",
        allowed_kinds=("approach_switch", "escalate", "next_packet", "replan", "stop"),
        attempts=(), verifications=(),
        evidence_index=("c" * 64,), failure_fingerprint="d" * 16,
        failure_excerpt="bounded diagnostic", interface=("value | required",),
        interface_digest="e" * 64, objective="synthetic objective", contract="synthetic contract",
        acceptance_criteria=("criterion",), write_scope=("src/x.py",), read_scope=(),
        source_digest="f" * 64, execution_role="local_implementer",
        profile_id="profile", endpoint_identity="endpoint",
        runtime_identity=(("provider", "\"local\""),),
        target_id="target", locality="local", host="local", model="fixture-model",
        allowed_tools=("write_file",), network_policy="offline", capabilities=(),
        policy_ref="policy@fixture", capability_receipt_refs=("g" * 64,),
        capacity_receipt_refs=(), verifier_id="pytest", verifier_digests=(("tests/test_x.py", "h" * 64),),
        attempts_used=2, max_attempts=3, budget_remaining=1)
    values.update(overrides)
    return PlannerInput(**values)


def _proposal(plan_input, **overrides):
    values = dict(
        proposal_id="advice-1", run_id=plan_input.run_id, packet_id=plan_input.packet_id,
        kind="approach_switch", rationale="Change the next implementation approach.",
        approach="Use the declared contract directly.",
        evidence_refs=(plan_input.evidence_index[0],))
    values.update(overrides)
    return make_manager_proposal(**values)


def test_proposal_is_sealed_bounded_and_input_digest_is_stable():
    plan = _input()
    assert plan.input_digest == _input().input_digest
    assert envelope_digest(plan) == envelope_digest(_input())
    proposal = _proposal(plan)
    assert validate_proposal(proposal, plan_input=plan).ok
    assert not validate_proposal(replace(proposal, approach="tampered"), plan_input=plan).ok
    with pytest.raises(ValueError, match="approach exceeds"):
        make_manager_proposal(
            proposal_id="p", run_id="run", packet_id="packet", kind="replan",
            rationale="bounded rationale", approach="x" * (MAX_APPROACH_CHARS + 1))


def test_per_kind_schemas_allow_only_their_approach_shape():
    plan = _input()
    next_packet = _proposal(plan, kind="next_packet", approach="")
    assert validate_proposal(next_packet, plan_input=plan).ok
    stop = _proposal(plan, kind="stop", approach="do something")
    assert validate_proposal(stop, plan_input=plan).code == "shape_invalid"
    missing_approach = _proposal(plan, kind="replan", approach="")
    assert validate_proposal(missing_approach, plan_input=plan).code == "shape_invalid"


@pytest.mark.parametrize("field,code", [
    ("accept", "authority_requested"), ("land", "authority_requested"),
    ("route", "routing_refused"), ("jira_comment", "tracker_mutation_refused"),
    ("widen_scope", "scope_widening"), ("widen_permissions", "permission_widening"),
    ("skip_verification", "verification_weakened"), ("extend_budget", "budget_widening"),
    ("change_interface", "interface_drift"), ("change_source", "source_drift"),
    ("change_objective", "identity_drift"), ("unrecognized", "unknown_action"),
])
def test_requested_action_families_fail_closed(field, code):
    plan = _input()
    proposal = _proposal(plan, requested_actions=(field,))
    assert validate_proposal(proposal, plan_input=plan).code == code


@pytest.mark.parametrize("field,code", [
    ("accept", "authority_requested"), ("land", "authority_requested"),
    ("route", "routing_refused"), ("jira", "tracker_mutation_refused"),
    ("allowed_tools", "permission_widening"), ("network_policy", "permission_widening"),
    ("write_scope", "scope_widening"), ("read_scope", "scope_widening"),
    ("verifier_digest", "verification_weakened"), ("max_attempts", "budget_widening"),
    ("interface", "interface_drift"), ("source_snapshot", "source_drift"),
    ("objective", "identity_drift"), ("unknown_field", "schema_invalid"),
])
def test_packet_delta_authority_families_fail_closed(field, code):
    plan = _input()
    proposal = _proposal(plan, packet_delta={field: "changed"})
    assert validate_proposal(proposal, plan_input=plan).code == code


def test_tampered_run_evidence_and_repeated_approach_are_refused():
    plan = _input(prior_approach_digests=("x",))
    assert validate_proposal(_proposal(plan, run_id="other"), plan_input=plan).code == "run_mismatch"
    assert validate_proposal(_proposal(plan, evidence_refs=("z" * 64,)), plan_input=plan).code == "evidence_unbound"
    proposal = _proposal(plan)
    plan = _input(prior_approach_digests=(approach_digest(proposal.approach),))
    assert validate_proposal(proposal, plan_input=plan).code == "approach_repeated"


def test_raw_parser_refuses_unknown_keys_authority_kinds_and_hash_tampering():
    import json

    plan = _input()
    value = _proposal(plan).to_dict()
    value["accept"] = True
    with pytest.raises(ValueError, match="unknown proposal field"):
        parse_proposal_text(json.dumps(value))
    value = _proposal(plan).to_dict()
    value["proposal_hash"] = "0" * 64
    with pytest.raises(ValueError, match="proposal hash"):
        parse_proposal_text(json.dumps(value))
    value = _proposal(plan).to_dict()
    value["kind"] = "accept"
    with pytest.raises(ValueError, match="unknown proposal kind"):
        parse_proposal_text(json.dumps(value))
    value = _proposal(plan).to_dict()
    value["input_digest"] = "i" * 64
    with pytest.raises(ValueError, match="unknown proposal field"):
        parse_proposal_text(json.dumps(value))


def test_derived_policy_projection_requires_exact_package_dispatch_envelope():
    package = _package(max_attempts=3)
    payload = package.to_dict()
    bound = _bound(package)
    failure = parse_verification_failure("pytest -q", 1,
                                         "FAILED tests/test_x.py::test_x - AssertionError: bad")
    plan = derive_planner_input(payload, bound, attempts=(), verifications=(),
                                failure=failure, failure_excerpt=failure.excerpt,
                                budget_remaining=1)
    assert plan.allowed_tools == tuple(payload["allowed_tools"])
    assert plan.network_policy == payload["network_policy"]
    assert plan.verifier_id == payload["verification"]["verifier_id"]
    changed = replace(bound.decision, network_policy="online")
    changed = replace(changed, receipt_hash=ps638_receipt_hash(changed.to_ps638_receipt_kwargs()))
    with pytest.raises(ValueError, match="envelope differs"):
        derive_planner_input(payload, replace(bound, decision=changed), attempts=(),
                             verifications=(), failure=failure, failure_excerpt=failure.excerpt,
                             budget_remaining=1)
