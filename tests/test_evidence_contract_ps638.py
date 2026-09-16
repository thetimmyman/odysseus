"""PS-638 — evidence requirements: closure is computed, never asserted.

The precedence order is the whole design, so most of this file is about which
fact wins when two disagree. A closure that let a judgement erase a measurement,
or let absence read as a waiver, would make the validator downstream decorative.
"""
import pytest

from src.evidence_contract import (
    CLAIM_BLOCKED,
    CLAIM_FAIL,
    CLAIM_INCONCLUSIVE,
    CLAIM_PASS,
    INDEPENDENCE_EXISTING_AUTHORITATIVE,
    INDEPENDENCE_HARNESS_HIDDEN,
    INDEPENDENCE_INDEPENDENT_VERIFIER,
    INDEPENDENCE_WORKER_AUTHORED,
    KIND_DETERMINISTIC_VERIFICATION,
    KIND_TARGET_BINDING,
    STATE_BLOCKED,
    STATE_FAILED,
    STATE_NOT_APPLICABLE,
    STATE_SATISFIED,
    STATE_UNRESOLVED,
    EvidenceContractError,
    EvidenceRequirement,
    RequirementClaim,
    close_requirements,
    requirement_index,
    states_by_id,
    unresolved_mandatory,
    unsatisfied_mandatory,
)

INDEPENDENT = EvidenceRequirement(
    requirement_id="verification", kind=KIND_DETERMINISTIC_VERIFICATION,
    vantage="harness worktree", expected_verifier="tests/t.py",
    independence=INDEPENDENCE_HARNESS_HIDDEN)

WORKER_OK = EvidenceRequirement(
    requirement_id="selfcheck", kind=KIND_TARGET_BINDING, vantage="worker",
    expected_verifier="the worker's own notes",
    independence=INDEPENDENCE_WORKER_AUTHORED, mandatory=False)


def claim(requirement_id, outcome, proof_class=INDEPENDENCE_HARNESS_HIDDEN,
          receipt_id="r-1", detail=""):
    return RequirementClaim(requirement_id=requirement_id, receipt_id=receipt_id,
                           outcome=outcome, proof_class=proof_class,
                           detail=detail)


def state_of(states, requirement_id):
    return states_by_id(states)[requirement_id].state


# ------------------------------------------------------------ construction ---
def test_an_unknown_kind_is_refused():
    with pytest.raises(EvidenceContractError):
        EvidenceRequirement(requirement_id="x", kind="vibes", vantage="v",
                            expected_verifier="someone")


def test_an_unknown_independence_class_is_refused():
    with pytest.raises(EvidenceContractError):
        EvidenceRequirement(requirement_id="x",
                            kind=KIND_DETERMINISTIC_VERIFICATION, vantage="v",
                            expected_verifier="someone", independence="TRUST_ME")


def test_an_empty_requirement_id_is_refused():
    with pytest.raises(EvidenceContractError):
        EvidenceRequirement(requirement_id="  ",
                            kind=KIND_DETERMINISTIC_VERIFICATION, vantage="v",
                            expected_verifier="someone")


def test_a_missing_vantage_is_refused():
    with pytest.raises(EvidenceContractError):
        EvidenceRequirement(requirement_id="x",
                            kind=KIND_DETERMINISTIC_VERIFICATION, vantage="",
                            expected_verifier="someone")


def test_duplicate_requirement_ids_are_refused():
    with pytest.raises(EvidenceContractError):
        requirement_index([INDEPENDENT, INDEPENDENT])


def test_an_unknown_claim_outcome_is_refused():
    with pytest.raises(EvidenceContractError):
        claim("verification", "PROBABLY")


def test_an_unknown_proof_class_is_refused():
    with pytest.raises(EvidenceContractError):
        claim("verification", CLAIM_PASS, proof_class="SELF")


# ---------------------------------------------------------------- accepts ---
def test_an_independent_requirement_refuses_worker_authored_proof():
    assert INDEPENDENT.accepts(INDEPENDENCE_HARNESS_HIDDEN) is True
    assert INDEPENDENT.accepts(INDEPENDENCE_INDEPENDENT_VERIFIER) is True
    assert INDEPENDENT.accepts(INDEPENDENCE_WORKER_AUTHORED) is False


def test_a_worker_authored_requirement_accepts_any_known_class():
    assert WORKER_OK.accepts(INDEPENDENCE_WORKER_AUTHORED) is True
    assert WORKER_OK.accepts(INDEPENDENCE_EXISTING_AUTHORITATIVE) is True


# ---------------------------------------------------------------- closure ---
def test_no_claim_leaves_a_requirement_unresolved():
    states = close_requirements([INDEPENDENT], [])
    assert state_of(states, "verification") == STATE_UNRESOLVED
    assert states_by_id(states)["verification"].resolved is False


def test_an_accepted_pass_satisfies():
    states = close_requirements([INDEPENDENT], [claim("verification", CLAIM_PASS)])
    assert state_of(states, "verification") == STATE_SATISFIED


def test_a_fail_is_recorded_as_failed():
    states = close_requirements([INDEPENDENT], [claim("verification", CLAIM_FAIL)])
    assert state_of(states, "verification") == STATE_FAILED


def test_a_block_is_recorded_as_blocked():
    states = close_requirements([INDEPENDENT], [claim("verification", CLAIM_BLOCKED)])
    state = states_by_id(states)["verification"]
    assert state.state == STATE_BLOCKED
    assert "not an allowed disposition" in state.detail


def test_a_block_is_allowed_only_when_declared():
    tolerant = EvidenceRequirement(
        requirement_id="verification", kind=KIND_DETERMINISTIC_VERIFICATION,
        vantage="v", expected_verifier="x", independence=INDEPENDENCE_HARNESS_HIDDEN,
        allow_blocked=True)
    states = close_requirements([tolerant], [claim("verification", CLAIM_BLOCKED)])
    state = states_by_id(states)["verification"]
    assert state.state == STATE_BLOCKED
    assert "not an allowed disposition" not in state.detail


def test_an_inconclusive_claim_leaves_the_requirement_unresolved():
    states = close_requirements([INDEPENDENT],
                                [claim("verification", CLAIM_INCONCLUSIVE)])
    assert state_of(states, "verification") == STATE_UNRESOLVED


def test_an_accepted_pass_outranks_a_fail():
    states = close_requirements(
        [INDEPENDENT],
        [claim("verification", CLAIM_FAIL, receipt_id="a"),
         claim("verification", CLAIM_PASS, receipt_id="b")])
    assert state_of(states, "verification") == STATE_SATISFIED


def test_a_fail_outranks_a_waiver():
    """A judgement must not erase a measurement."""
    states = close_requirements(
        [INDEPENDENT], [claim("verification", CLAIM_FAIL)],
        waivers={"verification": "we decided it did not apply"})
    assert state_of(states, "verification") == STATE_FAILED


def test_a_waiver_is_the_only_route_to_not_applicable():
    states = close_requirements([INDEPENDENT], [],
                               waivers={"verification": "not relevant here"})
    state = states_by_id(states)["verification"]
    assert state.state == STATE_NOT_APPLICABLE
    assert state.detail == "not relevant here"


def test_worker_authored_proof_cannot_satisfy_an_independent_requirement():
    states = close_requirements(
        [INDEPENDENT],
        [claim("verification", CLAIM_PASS, proof_class=INDEPENDENCE_WORKER_AUTHORED)])
    state = states_by_id(states)["verification"]
    assert state.state == STATE_FAILED
    assert "WORKER_AUTHORED" in state.detail


def test_an_independent_verifier_can_satisfy_an_independent_requirement():
    states = close_requirements(
        [INDEPENDENT],
        [claim("verification", CLAIM_PASS,
               proof_class=INDEPENDENCE_INDEPENDENT_VERIFIER)])
    assert state_of(states, "verification") == STATE_SATISFIED


def test_a_worker_authored_requirement_can_be_satisfied_by_the_worker():
    states = close_requirements(
        [WORKER_OK],
        [claim("selfcheck", CLAIM_PASS, proof_class=INDEPENDENCE_WORKER_AUTHORED)])
    assert state_of(states, "selfcheck") == STATE_SATISFIED


def test_closure_is_order_independent():
    first = close_requirements(
        [INDEPENDENT],
        [claim("verification", CLAIM_FAIL, receipt_id="a"),
         claim("verification", CLAIM_PASS, receipt_id="b")])
    second = close_requirements(
        [INDEPENDENT],
        [claim("verification", CLAIM_PASS, receipt_id="b"),
         claim("verification", CLAIM_FAIL, receipt_id="a")])
    assert state_of(first, "verification") == state_of(second, "verification")


def test_unresolved_mandatory_lists_only_mandatory_gaps():
    states = close_requirements([INDEPENDENT, WORKER_OK], [])
    assert unresolved_mandatory([INDEPENDENT, WORKER_OK], states) == \
        ("verification",)
    assert unsatisfied_mandatory([INDEPENDENT, WORKER_OK], states) == \
        ("verification",)


def test_satisfied_and_waived_are_not_reported_as_gaps():
    states = close_requirements([INDEPENDENT, WORKER_OK],
                               [claim("selfcheck", CLAIM_PASS,
                                      proof_class=INDEPENDENCE_WORKER_AUTHORED)],
                               waivers={"verification": "waived for this run"})
    assert unresolved_mandatory([INDEPENDENT, WORKER_OK], states) == ()
    assert unsatisfied_mandatory([INDEPENDENT, WORKER_OK], states) == ()


def test_an_unknown_state_cannot_be_constructed():
    from src.evidence_contract import RequirementState

    with pytest.raises(EvidenceContractError):
        RequirementState("x", "PROBABLY_FINE")
