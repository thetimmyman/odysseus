"""PS-635 G2 — the manager/replanner seam, its gate, and its negative controls.

Hermetic: no model is called, no node is contacted. The advisor is injected, so
the seam's own behaviour (projection, schema, validation, ledger recording, and
what the loop is willing to do with a validated proposal) is tested directly
rather than inferred from a slow live run.

Every refusal this file asserts is a NEGATIVE CONTROL named after the rule it
protects, so deleting a gate fails a test instead of quietly widening what an
advisory model is allowed to do.
"""
import dataclasses
import os
import tempfile

import pytest

from src import replanner as R
from src.execution_ledger import (
    ExecutionLedger,
    LedgerError,
    RESULT_ACCEPTED_CANDIDATE,
    RESULT_ESCALATE,
)
from src.local_worker_loop import DispatchOutcome, PinnedTarget, run_bounded
from src.work_packet import interface_digest_of

PACKET = {
    "packet_id": "G2-T",
    "objective": "implement the thing",
    "role": "local_implementer",
    "write_scope": ["src/thing.py"],
    "read_scope": ["src/other.py"],
    "interface": [{"name": "payload", "required": True, "type_hint": "dict",
                   "semantics": "the input mapping"}],
    "test_command": "python3 -m pytest tests/test_thing.py -q",
    "contract": "implement thing(payload) -> dict",
    "acceptance_criteria": ["criterion one"],
    "negative_control": "an implementation that ignores the input must FAIL",
    "stop_conditions": ["the contract is ambiguous"],
    "base_sha": "abc1234",
}

POLICY = R.PlannerPolicy(
    allowed_tools=("write_file",), network_policy="tailnet-loopback",
    capabilities=("text_generation",), verifier_id="tests/test_thing.py",
    verifier_digest="d" * 64)

PINNED = PinnedTarget(target_id="local-rtx4500", host="minipc",
                      model="qwen3.8:27b", runtime_version="0.32.11",
                      worktree="/tmp/wt", served_context=32768)

FAIL_OUTPUT = (
    "=========================== short test summary info ============================\n"
    "FAILED tests/test_thing.py::test_render - AssertionError: assert False\n"
    "1 failed, 4 passed, 1 warning in 0.16s\n")


@pytest.fixture()
def ledger():
    return ExecutionLedger(os.path.join(tempfile.mkdtemp(), "ledger.jsonl"))


def seed(ledger, *, run_id="r1", attempts=2,
         fingerprints=("fp-aaa", "fp-aaa", "fp-aaa"),
         terminal="", status=RESULT_ESCALATE):
    """A canonical run state: identity, attempts, verdicts, repairs, decision.

    Written through the ledger's OWN write paths, so the fixture cannot produce
    a state the ledger would refuse.
    """
    ledger.record_run(run_id=run_id, packet_id=PACKET["packet_id"],
                      objective=PACKET["objective"], role="local_implementer",
                      target_id=PINNED.target_id, host=PINNED.host,
                      model=PINNED.model, runtime_version=PINNED.runtime_version,
                      worktree=PINNED.worktree, write_scope=PACKET["write_scope"],
                      base_sha=PACKET["base_sha"],
                      interface_digest=interface_digest_of(PACKET["interface"]))
    for number in range(1, attempts + 1):
        ledger.record_attempt(run_id=run_id, packet_id=PACKET["packet_id"],
                              attempt=number, target_id=PINNED.target_id,
                              host=PINNED.host, num_ctx=32768, served_context=32768,
                              elapsed_s=20.0, rounds=2,
                              artifacts=[PACKET["write_scope"][0]],
                              failure_class="technical", status="wrote the file")
        ledger.record_verification(run_id=run_id, packet_id=PACKET["packet_id"],
                                   test_command=PACKET["test_command"], passed=False,
                                   returncode=1, summary=["1 failed, 4 passed"],
                                   excerpt=FAIL_OUTPUT)
        ledger.record_repair(run_id=run_id, packet_id=PACKET["packet_id"],
                             attempt=number, failure_class="technical",
                             fingerprint=fingerprints[number - 1],
                             repair_packet={"kind": "repair"})
    if terminal:
        ledger.record_decision(run_id=run_id, packet_id=PACKET["packet_id"],
                               decision="escalate", reason="fixture", result=status)
    return ledger


def plan_input(ledger, *, boundary=R.BOUNDARY_STALL, run_id="r1", max_attempts=3,
               max_replans=1, replans_used=0, packet=None):
    return R.build_planner_input(
        ledger=ledger, run_id=run_id, packet=packet or PACKET, boundary=boundary,
        max_attempts=max_attempts, max_replans=max_replans,
        replans_used=replans_used, policy=POLICY)


def proposal(ledger, *, run_id="r1", packet_id=None, **overrides):
    """A schema-valid proposal citing a REAL canonical entry hash."""
    base = {
        "proposal_id": "prop-1", "run_id": run_id,
        "packet_id": packet_id or PACKET["packet_id"], "kind": "approach_switch",
        "rationale": "the same failure twice suggests the structure is wrong",
        "approach": "Build the mapping with a list of pairs, then materialise it.",
        "evidence_refs": [ledger.entries_for(run_id)[0].entry_hash],
    }
    base.update(overrides)
    return R.make_manager_proposal(**base)


def verdict(ledger, prop, *, boundary=R.BOUNDARY_STALL, packet=None, **kw):
    return R.validate_proposal(
        prop, plan_input=plan_input(ledger, boundary=boundary, **kw),
        packet=packet or PACKET, ledger=ledger)


def codes(v):
    return {c["check"]: c["code"] for c in v.checks if not c["ok"]}



# ------------------------------------------------------------------- schema ---
def test_a_proposal_is_sealed_and_its_hash_verifies():
    prop = R.make_manager_proposal(proposal_id="p", run_id="r1", packet_id="G2-T",
                                   kind="stop", rationale="the run is over")
    assert prop.proposal_hash and R.proposal_hash_is_valid(prop.to_dict())


def test_authority_is_unrepresentable_not_merely_refused():
    """The strongest control in the file: 'accept' is not a kind you can write.

    A gate that refuses "accept" can be loosened by a later edit; a schema with
    no such kind has to be changed on purpose, and that change is visible.
    """
    for kind in ("accept", "approve", "ship", "land", "merge", "deploy",
                 "route", "next", ""):
        with pytest.raises(R.ReplannerError):
            R.make_manager_proposal(proposal_id="p", run_id="r1", packet_id="G2-T",
                                    kind=kind, rationale="why not")


def test_a_proposal_needs_identity_reason_and_bounded_approach():
    with pytest.raises(R.ReplannerError):
        R.make_manager_proposal(proposal_id="", run_id="r1", packet_id="G2-T",
                                kind="stop", rationale="x")
    with pytest.raises(R.ReplannerError):
        R.make_manager_proposal(proposal_id="p", run_id="r1", packet_id="G2-T",
                                kind="stop", rationale="   ")
    with pytest.raises(R.ReplannerError):
        R.make_manager_proposal(proposal_id="p", run_id="r1", packet_id="G2-T",
                                kind="approach_switch", rationale="x",
                                approach="a" * (R.MAX_APPROACH_CHARS + 1))
    with pytest.raises(R.ReplannerError):
        R.make_manager_proposal(proposal_id="p", run_id="r1", packet_id="G2-T",
                                kind="stop", rationale="x", unexpected="boom")
    with pytest.raises(R.ReplannerError):
        R.make_manager_proposal(proposal_id="p", run_id="r1", packet_id="G2-T",
                                kind="stop", rationale="x", packet_delta=["nope"])
    with pytest.raises(R.ReplannerError):
        R.make_manager_proposal(proposal_id="p", run_id="r1", packet_id="G2-T",
                                kind="stop", rationale="x", schema_version=99)


def test_a_tampered_proposal_fails_its_own_hash():
    prop = R.make_manager_proposal(proposal_id="p", run_id="r1", packet_id="G2-T",
                                   kind="stop", rationale="done")
    tampered = prop.to_dict()
    tampered["kind"] = "replan"
    tampered["approach"] = "a different plan"
    assert R.proposal_hash_is_valid(tampered) is False


def test_parse_accepts_a_wrapped_reply_and_refuses_junk():
    good, _reason = R.parse_proposal_text(
        'Sure!\n```json\n{"kind": "stop", "rationale": "verified",'
        ' "evidence_refs": ["abc"]}\n```', run_id="r1", packet_id="G2-T")
    assert good is not None and good.kind == "stop"
    bad, reason = R.parse_proposal_text("no json at all", run_id="r1",
                                        packet_id="G2-T")
    assert bad is None and "no JSON" in reason
    bad, reason = R.parse_proposal_text('{"kind": "stop", "rationale": "x", '
                                        '"authority": "accept"}',
                                        run_id="r1", packet_id="G2-T")
    assert bad is None and "unknown field" in reason
    bad, reason = R.parse_proposal_text('{"kind": "accept", "rationale": "x"}',
                                        run_id="r1", packet_id="G2-T")
    assert bad is None and "not a proposal shape" in reason


# ------------------------------------------------------------ planner input ---
def test_the_boundary_decides_which_kinds_are_even_offered():
    assert R.allowed_kinds_for(R.BOUNDARY_PASS, budget_remaining=2,
                               replans_remaining=1) == (R.KIND_STOP,)
    assert set(R.allowed_kinds_for(R.BOUNDARY_STALL, budget_remaining=2,
                                   replans_remaining=1)) == set(R.KNOWN_KINDS)
    assert R.allowed_kinds_for(R.BOUNDARY_STALL, budget_remaining=0,
                               replans_remaining=1) == R.TERMINAL_KINDS
    assert R.allowed_kinds_for(R.BOUNDARY_STALL, budget_remaining=2,
                               replans_remaining=0) == R.TERMINAL_KINDS


def test_the_projection_carries_canonical_state_and_nothing_else(ledger):
    seed(ledger)
    plan = plan_input(ledger)
    core = plan.core()
    assert core["identity"]["target_id"] == "local-rtx4500"
    assert core["interface"]["fields"] == [
        "payload | required | dict -- the input mapping"]
    assert core["interface"]["digest"] == interface_digest_of(PACKET["interface"])
    assert core["budget"] == {"attempts_used": 2, "max_attempts": 3,
                              "budget_remaining": 1, "replans_used": 0,
                              "max_replans": 1}
    assert [v["passed"] for v in core["verifications"]] == [False, False]
    assert core["failure_fingerprints"] == ["fp-aaa", "fp-aaa"]
    assert len(core["evidence_index"]) == len(ledger.entries_for("r1"))
    rendered = R.render_planner_context(plan)
    assert "CANONICAL HISTORY" in rendered and "REPLY PROTOCOL" in rendered
    assert PACKET["test_command"] in rendered


def test_the_projection_is_a_fixed_schema_not_a_passthrough(ledger):
    """NEGATIVE CONTROL: a worker transcript cannot reach the manager.

    The ledger's own ``status`` field IS projected (it is canonical). What is
    asserted here is that nothing else is: the projection is a fixed set of keys,
    so a future field added to an attempt cannot leak worker prose by accident.
    """
    seed(ledger)
    plan = plan_input(ledger)
    core = plan.core()
    assert set(core) == {
        "schema_version", "run_id", "packet_id", "objective", "identity", "scope",
        "interface", "verification", "policy", "boundary", "budget", "attempts",
        "verifications", "failure_fingerprints", "prior_approach_digests",
        "last_failure_excerpt", "evidence_index"}
    for attempt in core["attempts"]:
        assert set(attempt) == {"attempt", "target_id", "model", "rounds",
                                "artifacts", "failure_class", "status", "elapsed_s"}
    for record in core["verifications"]:
        assert set(record) == {"attempt", "test_command", "passed", "returncode",
                               "summary"}


def test_the_projection_digest_is_stable_and_changes_with_state(ledger):
    seed(ledger)
    first = plan_input(ledger)
    assert first.input_digest == plan_input(ledger).input_digest
    seed(ledger, run_id="r2", fingerprints=("fp-aaa", "fp-bbb"))
    assert plan_input(ledger, run_id="r2").input_digest != first.input_digest


def test_the_planner_context_is_bounded_and_marks_truncation(ledger):
    seed(ledger, attempts=3)
    rendered = R.render_planner_context(plan_input(ledger), max_chars=600)
    assert len(rendered) <= 600
    assert rendered.endswith("[PLANNER CONTEXT TRUNCATED]")


# --------------------------------------------------------- negative controls ---
def test_a_valid_approach_switch_is_accepted(ledger):
    """The positive control: without this, every assertion below is vacuous."""
    seed(ledger)
    v = verdict(ledger, proposal(ledger))
    assert v.ok is True and v.code == ""
    assert v.approach_digest and len(v.approach_digest) == 16
    assert all(c["ok"] for c in v.checks)


def test_scope_widening_is_refused(ledger):
    seed(ledger)
    v = verdict(ledger, proposal(ledger, packet_delta={
        "write_scope": ["src/thing.py", "tests/test_thing.py"]}))
    assert v.ok is False and v.code == R.CODE_SCOPE_WIDENING


def test_permission_widening_is_refused(ledger):
    seed(ledger)
    v = verdict(ledger, proposal(ledger, packet_delta={
        "allowed_tools": ["write_file", "run_shell"]}))
    assert v.ok is False and v.code == R.CODE_PERMISSION_WIDENING


def test_an_approval_or_ship_request_is_refused(ledger):
    seed(ledger)
    for delta in ({"result": "ACCEPTED"}, {"lifecycle": "ACCEPTED"},
                  {"state": "DELIVERED"}):
        v = verdict(ledger, proposal(ledger, packet_delta=delta))
        assert v.ok is False and v.code == R.CODE_AUTHORITY_REQUESTED, delta
    v = verdict(ledger, proposal(ledger, requested_authority=("accept",)))
    assert v.ok is False and v.code == R.CODE_AUTHORITY_REQUESTED
    v = verdict(ledger, proposal(ledger, requested_actions=("approve", "ship")))
    assert v.ok is False and v.code == R.CODE_AUTHORITY_REQUESTED


def test_a_failed_gate_bypass_is_refused(ledger):
    seed(ledger)
    for delta in ({"test_command": "python3 -m pytest -q -k nothing"},
                  {"acceptance_criteria": []},
                  {"negative_control": ""}, {"skip_review": True},
                  {"review": "waived"}):
        v = verdict(ledger, proposal(ledger, packet_delta=delta))
        assert v.ok is False and v.code == R.CODE_VERIFICATION_WEAKENED, delta
    v = verdict(ledger, proposal(ledger,
                                 requested_actions=("bypass_verification",)))
    assert v.ok is False and v.code == R.CODE_VERIFICATION_WEAKENED


def test_routing_and_jira_mutation_are_refused(ledger):
    seed(ledger)
    v = verdict(ledger, proposal(ledger, packet_delta={"target_id": "local-halo"}))
    assert v.ok is False and v.code == R.CODE_ROUTING_REFUSED
    v = verdict(ledger, proposal(ledger, packet_delta={"jira_comment": "shipped"}))
    assert v.ok is False and v.code == R.CODE_JIRA_MUTATION_REFUSED


def test_interface_source_identity_and_budget_drift_are_refused(ledger):
    seed(ledger)
    v = verdict(ledger, proposal(ledger, packet_delta={"interface": ["other"]}))
    assert v.ok is False and v.code == R.CODE_INTERFACE_DRIFT
    v = verdict(ledger, proposal(ledger, packet_delta={"base_sha": "deadbeef"}))
    assert v.ok is False and v.code == R.CODE_SOURCE_DRIFT
    v = verdict(ledger, proposal(ledger, packet_delta={"objective": "do less"}))
    assert v.ok is False and v.code == R.CODE_IDENTITY_DRIFT
    v = verdict(ledger, proposal(ledger, packet_delta={"max_attempts": 9}))
    assert v.ok is False and v.code == R.CODE_BUDGET_WIDENING
    v = verdict(ledger, proposal(ledger, packet_delta={"sneaky_extra": 1}))
    assert v.ok is False and v.code == R.CODE_SCHEMA_INVALID


def test_a_malformed_proposal_never_reaches_the_gate(ledger):
    """Malformed is a SCHEMA outcome, not a semantic one — and it is recorded."""
    seed(ledger)
    parsed, reason = R.parse_proposal_text("I think we should try again",
                                           run_id="r1", packet_id="G2-T")
    assert parsed is None and reason
    v = verdict(ledger, R.make_manager_proposal(
        proposal_id="p2", run_id="r1", packet_id="G2-T", kind="approach_switch",
        rationale="x", approach="try again", evidence_refs=()))
    assert v.ok is False and v.code == R.CODE_EVIDENCE_UNBOUND


def test_a_repeated_approach_is_a_cycle_and_is_refused(ledger):
    seed(ledger)
    first = proposal(ledger)
    ledger.record_proposal(run_id="r1", packet_id=PACKET["packet_id"],
                          proposal=first.to_dict(),
                          verdict={"checks": [], "approach_digest":
                                   R.approach_digest(first.approach)})
    v = verdict(ledger, proposal(ledger))
    assert v.ok is False and v.code == R.CODE_APPROACH_REPEATED


def test_a_proposal_for_an_already_settled_run_is_refused(ledger):
    seed(ledger, terminal="escalate")
    v = verdict(ledger, proposal(ledger))
    assert v.ok is False and v.code == R.CODE_CYCLIC_PACKET


def test_an_invalid_packet_proposal_is_refused(ledger):
    """The gate re-validates the packet through the WorkPacket primitive."""
    seed(ledger)
    broken = dict(PACKET, test_command="")
    v = R.validate_proposal(proposal(ledger), plan_input=plan_input(ledger),
                            packet=broken, ledger=ledger)
    assert v.ok is False and v.code == R.CODE_PACKET_INVALID


def test_a_replan_after_a_pass_is_refused_by_the_kind_gate(ledger):
    """MUTATION CONTROL: remove the kind gate and this test fails."""
    seed(ledger, attempts=1, fingerprints=("fp-aaa",))
    v = verdict(ledger, proposal(ledger, kind="replan",
                                 approach="try the other structure"),
                boundary=R.BOUNDARY_PASS)
    assert v.ok is False and v.code == R.CODE_KIND_NOT_ALLOWED
    agreed = verdict(ledger, proposal(ledger, kind="stop", approach=""),
                     boundary=R.BOUNDARY_PASS)
    assert agreed.ok is True


def test_a_replan_must_carry_an_approach_and_stop_must_not(ledger):
    seed(ledger)
    v = verdict(ledger, proposal(ledger, kind="replan", approach=""))
    assert v.ok is False and v.code == R.CODE_SHAPE_INVALID
    v = verdict(ledger, proposal(ledger, kind="stop", approach="keep trying"))
    assert v.ok is False and v.code == R.CODE_SHAPE_INVALID


def test_a_proposal_about_another_run_is_refused(ledger):
    seed(ledger)
    prop = R.make_manager_proposal(
        proposal_id="p9", run_id="some-other-run", packet_id=PACKET["packet_id"],
        kind="approach_switch", rationale="wrong run", approach="different plan",
        evidence_refs=[ledger.entries_for("r1")[0].entry_hash])
    v = verdict(ledger, prop)
    assert v.ok is False and v.code == R.CODE_RUN_MISMATCH


def test_a_tampered_proposal_is_refused_by_the_gate_not_just_by_its_hash(ledger):
    seed(ledger)
    sealed = proposal(ledger)
    tampered = dataclasses.replace(sealed, kind="replan",
                                   approach="a wholly different plan")
    v = verdict(ledger, tampered)
    assert v.ok is False and v.code == R.CODE_PROPOSAL_HASH_MISMATCH


def test_budget_and_replan_allowance_are_refused_even_if_offered(ledger):
    """Defence in depth: a wider projection still cannot widen the budget."""
    seed(ledger, attempts=3)
    plan = dataclasses.replace(plan_input(ledger), allowed_kinds=R.STALL_KINDS,
                               budget_remaining=0)
    v = R.validate_proposal(proposal(ledger), plan_input=plan, packet=PACKET,
                            ledger=ledger)
    assert v.ok is False and v.code == R.CODE_BUDGET_EXHAUSTED

    plan = dataclasses.replace(plan_input(ledger, max_attempts=4),
                               allowed_kinds=R.STALL_KINDS, replans_used=1,
                               max_replans=1)
    v = R.validate_proposal(proposal(ledger), plan_input=plan, packet=PACKET,
                            ledger=ledger)
    assert v.ok is False and v.code == R.CODE_REPLANS_EXHAUSTED


def test_the_ledger_refuses_a_proposal_that_asks_for_authority(ledger):
    seed(ledger)
    with pytest.raises(LedgerError):
        ledger.record_proposal(run_id="r1", packet_id=PACKET["packet_id"],
                               proposal={"proposal_id": "p", "kind": "replan",
                                         "requested_actions": ["approve"]},
                               verdict={})


def test_the_ledger_refuses_a_proposal_carrying_a_lifecycle_result(ledger):
    seed(ledger)
    with pytest.raises(LedgerError):
        ledger.append("proposal", run_id="r1",
                      payload={"result": RESULT_ACCEPTED_CANDIDATE})
    with pytest.raises(LedgerError):
        ledger.append("proposal_refused", run_id="r1",
                      payload={"result": RESULT_ACCEPTED_CANDIDATE})


def test_proposals_appear_in_the_provenance_view(ledger):
    seed(ledger)
    prop = proposal(ledger)
    ledger.record_proposal(run_id="r1", packet_id=PACKET["packet_id"],
                          proposal=prop.to_dict(),
                          verdict={"checks": [], "approach_digest":
                                   R.approach_digest(prop.approach)})
    ledger.record_proposal_refusal(run_id="r1", packet_id=PACKET["packet_id"],
                                   proposal={}, code=R.CODE_SCHEMA_INVALID,
                                   detail="no JSON")
    provenance = ledger.provenance("r1")
    assert provenance["proposals"][0]["kind"] == "approach_switch"
    assert provenance["proposal_refusals"][0]["verdict_code"] == "schema_invalid"


# --------------------------------------------------------------- loop seam ---
class _Worker:
    """A scripted dispatcher that records every context it was handed."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.contexts = []

    def __call__(self, context, attempt):
        self.contexts.append(context)
        return self.outcomes[min(attempt - 1, len(self.outcomes) - 1)]


def _outcome(status="wrote the file", failure_class=""):
    return DispatchOutcome(artifacts=("src/thing.py",), failure_class=failure_class,
                           status=status, elapsed_s=20.0, rounds=1)


def _verifier(results):
    state = {"n": 0}

    def _fn():
        record = results[min(state["n"], len(results) - 1)]
        state["n"] += 1
        return record

    return _fn


def _pass():
    return {"test_command": PACKET["test_command"], "returncode": 0, "passed": True,
            "summary": ["5 passed"], "output": "5 passed"}


def _fail():
    return {"test_command": PACKET["test_command"], "returncode": 1, "passed": False,
            "summary": ["1 failed, 4 passed"], "output": FAIL_OUTPUT}


def _advisor_returning(**overrides):
    """An advisor proposing a legal approach_switch that cites canonical evidence.

    It reads the evidence hash out of the projection it was handed, exactly as a
    live manager does, so the test exercises the same grounding requirement.
    """
    def _fn(plan):
        fields = {
            "proposal_id": f"prop-{len(plan.attempts)}", "run_id": plan.run_id,
            "packet_id": plan.packet_id, "kind": "approach_switch",
            "rationale": "the same failure twice means the structure is wrong",
            "approach": "Use a list of (key, value) pairs, then materialise.",
            "evidence_refs": list(plan.evidence_index)[:1],
        }
        fields.update(overrides)
        return R.make_manager_proposal(**fields), ""
    return _fn


def _run(ledger, worker, verifier, *, advise=None, max_attempts=3, max_replans=1,
         run_id="r1"):
    return run_bounded(PACKET, run_id=run_id, ledger=ledger, pinned=PINNED,
                       dispatch=worker, verify=verifier, base_context="BASE CONTEXT",
                       max_attempts=max_attempts, read_changed_files=lambda _p: {},
                       advise=advise, max_replans=max_replans, planner_policy=POLICY)


def test_without_an_advisor_the_loop_behaves_exactly_as_g1_did(ledger):
    worker = _Worker([_outcome(), _outcome(), _outcome()])
    result = _run(ledger, worker, _verifier([_fail(), _fail(), _fail()]))
    assert result.result == RESULT_ESCALATE and result.attempts == 2
    assert len(worker.contexts) == 2
    assert ledger.proposals("r1") == [] and ledger.proposal_refusals("r1") == []


def test_without_an_advisor_no_proposal_entry_is_written(ledger):
    worker = _Worker([_outcome()])
    _run(ledger, worker, _verifier([_pass()]))
    assert [e.kind for e in ledger.entries_for("r1") if "proposal" in e.kind] == []


def test_a_validated_approach_switch_is_dispatched_with_the_note_attached(ledger):
    """The point of G2: a stall G1 escalated on becomes ONE bounded replan, and
    the manager's only visible effect is the approach note."""
    worker = _Worker([_outcome(), _outcome(), _outcome()])
    result = _run(ledger, worker, _verifier([_fail(), _fail(), _pass()]),
                  advise=_advisor_returning())

    assert result.result == RESULT_ACCEPTED_CANDIDATE
    assert result.attempts == 3 and len(worker.contexts) == 3
    third = worker.contexts[2]
    assert "MANAGER_APPROACH" in third
    assert "Use a list of (key, value) pairs" in third
    assert "REPAIR REQUEST" in third              # the deterministic evidence stays
    assert PACKET["contract"] in third            # and so does the contract
    assert "INTERFACE" in third                   # and the sealed interface

    proposals = ledger.proposals("r1")
    assert len(proposals) == 1
    assert proposals[0].payload["kind"] == "approach_switch"
    assert proposals[0].payload["accepted"] is True
    assert proposals[0].payload["approach_digest"]
    decisions = [e.payload["decision"] for e in ledger.entries_for("r1")
                 if e.kind == "decision"]
    assert decisions == ["next", "stop"]
    assert ledger.verify_chain() == (True, None)


def test_a_refused_proposal_leaves_the_g1_outcome_untouched(ledger):
    worker = _Worker([_outcome(), _outcome(), _outcome()])
    result = _run(ledger, worker, _verifier([_fail(), _fail(), _pass()]),
                  advise=_advisor_returning(packet_delta={
                      "write_scope": ["src/thing.py", "tests/test_thing.py"]}))

    assert result.result == RESULT_ESCALATE and result.attempts == 2
    assert len(worker.contexts) == 2                       # no third dispatch
    assert "manager advice refused: scope_widening" in result.reason
    refusals = ledger.proposal_refusals("r1")
    assert len(refusals) == 1
    assert refusals[0].payload["verdict_code"] == "scope_widening"


def test_the_loop_consults_the_manager_at_a_pass_and_does_not_obey_it(ledger):
    """MUTATION CONTROL: the PASS boundary ignores advice about the outcome."""
    worker = _Worker([_outcome()])
    result = _run(ledger, worker, _verifier([_pass()]),
                  advise=_advisor_returning(kind="replan",
                                            approach="keep going anyway"))
    assert result.result == RESULT_ACCEPTED_CANDIDATE and result.attempts == 1
    assert "manager advised" in result.reason
    refusals = ledger.proposal_refusals("r1")
    assert [r.payload["verdict_code"] for r in refusals] == ["kind_not_allowed"]
    assert ledger.proposals("r1") == []


def test_an_advisor_that_agrees_with_a_pass_is_recorded(ledger):
    worker = _Worker([_outcome()])
    _run(ledger, worker, _verifier([_pass()]),
         advise=lambda plan: (R.make_manager_proposal(
             proposal_id="p1", run_id=plan.run_id, packet_id=plan.packet_id,
             kind="stop", rationale="the verifier passed",
             evidence_refs=[plan.evidence_index[0]]), ""))
    proposals = ledger.proposals("r1")
    assert len(proposals) == 1 and proposals[0].payload["kind"] == "stop"


def test_an_advisor_that_returns_nothing_is_a_recorded_refusal(ledger):
    worker = _Worker([_outcome(), _outcome(), _outcome()])
    result = _run(ledger, worker, _verifier([_fail(), _fail(), _pass()]),
                  advise=lambda plan: (None, "the reply contained no JSON object"))
    assert result.result == RESULT_ESCALATE
    refusals = ledger.proposal_refusals("r1")
    assert len(refusals) == 1
    assert refusals[0].payload["verdict_code"] == "schema_invalid"


def test_an_advisor_that_raises_cannot_fail_the_run(ledger):
    def _explode(plan):
        raise RuntimeError("the manager's transport died")

    worker = _Worker([_outcome(), _outcome(), _outcome()])
    result = _run(ledger, worker, _verifier([_fail(), _fail(), _pass()]),
                  advise=_explode)
    assert result.result == RESULT_ESCALATE and result.attempts == 2
    assert ledger.proposal_refusals("r1")[0].payload["verdict_code"] == "schema_invalid"


def test_advice_cannot_buy_an_attempt_beyond_the_budget(ledger):
    """At the last attempt the replan is refused and the loop escalates."""
    worker = _Worker([_outcome(), _outcome(), _outcome()])
    result = _run(ledger, worker, _verifier([_fail(), _fail(), _pass()]),
                  advise=_advisor_returning(), max_attempts=2, max_replans=1)
    assert result.result == RESULT_ESCALATE and result.attempts == 2
    assert len(worker.contexts) == 2
    refusals = ledger.proposal_refusals("r1")
    assert [r.payload["verdict_code"] for r in refusals] == ["kind_not_allowed"]


def test_a_second_stall_escalates_because_the_replan_allowance_is_spent(ledger):
    """After one replan, the NEXT stall finds the allowance spent and escalates.

    Note what this also shows: after a replan resets the repeat detector, an
    identical failure is a NEW single failure, so a second stall needs two more
    identical failures. The loop is bounded either way — by the allowance and by
    the attempt budget — and this is the case where the allowance runs out first.
    """
    worker = _Worker([_outcome()] * 5)
    result = _run(ledger, worker,
                  _verifier([_fail(), _fail(), _fail(), _fail(), _pass()]),
                  advise=_advisor_returning(), max_attempts=4, max_replans=1)
    assert result.result == RESULT_ESCALATE and result.attempts == 4
    assert len(worker.contexts) == 4          # one replan happened, not two
    assert len(ledger.proposals("r1")) == 1
    assert [r.payload["verdict_code"] for r in ledger.proposal_refusals("r1")] == [
        "kind_not_allowed"]
