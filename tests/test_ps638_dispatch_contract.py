"""PS-638 × PS-605 — the dispatch-receipt contract, proven against the REAL types.

This is the cross-lineage test the integration plan named. It exists because both
sides independently froze a receipt shape and a hash rule, and two implementations
of "the same" hash agree only if someone checks:

    make_dispatch_receipt(**decision.to_ps638_receipt_kwargs())

plus the chain the PS-635 loop depends on:

    ExecutionPackage hash -> DispatchDecisionReceipt hash
    -> AttemptReceipt.dispatch_receipt_hash -> the target that was invoked

Nothing here re-implements PS-638 or PS-605: the receipt is PS-638's own builder,
the decision is PS-605's own selector, and every assertion is about AGREEMENT
between them (including the canonicalization, which is byte-sensitive).
"""
from __future__ import annotations

import subprocess

import pytest

from src import dispatch_boundary as dbd
from src import dispatch_routing as dr
from src.attempt_receipt import (
    attempt_receipt_hash_is_valid, context_projection_digest, make_attempt_receipt)
from src.execution_package import (
    Budgets, VerificationPlan, build_execution_package, make_dispatch_receipt,
    package_hash_is_valid, seal_verifier_digests)
from src.source_snapshot import take_source_snapshot
from tests.test_dispatch_boundary import _db, _seed


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Tester")
    (root / "src" / "thing.py").write_text("VALUE = 1\n")
    (root / "tests" / "t.py").write_text("def test_x():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


PACKET = {
    "packet_id": "P-INTEGRATION-1",
    "objective": "implement the thing to the declared contract",
    "contract": "def thing(graph: dict) -> list",
    "role": "local_implementer",
    "write_scope": ["src/thing.py"],
    "interface": [{"name": "graph", "required": True, "type_hint": "dict",
                   "semantics": "node -> successors"}],
    "test_command": "python3 -m pytest tests/t.py -q",
    "acceptance_criteria": ["respects every edge"],
    "negative_control": "a non-alphabetical order must FAIL",
    "stop_conditions": ["contract is ambiguous"],
    "base_sha": "HEAD",
    "target_requirements": ["native_tools", "readonly_analysis"],
}


@pytest.fixture()
def ep(repo):
    """The REAL PS-638 ExecutionPackage this decision must bind to."""
    plan = VerificationPlan(
        verifier_id="tests/t.py", command="python3 -m pytest tests/t.py -q",
        verifier_paths=["tests/t.py"],
        verifier_digests=seal_verifier_digests(str(repo), ["tests/t.py"]),
        positive_control="the known-good fixture passes",
        negative_control="a non-alphabetical order must FAIL")
    return build_execution_package(
        PACKET, source=take_source_snapshot(str(repo), base_sha="HEAD",
                                            relevant_paths=["src/thing.py"]),
        verification=plan, run_id="run-integration-1", jira_key="PS-605",
        allowed_tools=("write_file",), network_policy="tailnet-loopback",
        budgets=Budgets(context_tokens=32768, output_tokens=4096,
                        time_seconds=600, max_attempts=1))


def _bound_decision(ep, *, decision_id="dec-integration"):
    """A real PS-605 decision bound to the REAL package identity."""
    db = _db()
    task = _seed(db)
    return dbd.resolve_dispatch(
        db, task, [{"profile_id": "p-rtx", "model": "", "roles": [],
                    "score": 1.0, "estimated_cost_usd": 0.0, "reasons": []}],
        # The estate's declared role name for a tool-capable worker; the harness
        # derives the same role from proven capabilities (see local_target_routing).
        domain="general_swe", role=dr.ROLE_IMPLEMENTER_ROUTER,
        capabilities=(dr.CAP_TEXT_GENERATION, dr.CAP_SINGLE_TOOL_CALL),
        required_tools=("write_file",), network_policy="tailnet-loopback",
        # The registry's own network-class name, supplied the way the integration
        # adapter supplies it (PS-632 owns the vocabulary for local targets).
        network_classes={"p-rtx": "tailnet-loopback"},
        execution_package_hash_=ep.package_hash, run_id="run-integration-1",
        packet_id=PACKET["packet_id"], decision_id=decision_id)


# ------------------------------------------------- canonicalization agreement ---
def test_the_canonical_form_agrees_with_ps638_byte_for_byte():
    """A hash over a differently-escaped payload is a DIFFERENT hash."""
    import src.execution_package as epkg

    payload = {"a": "em—dash", "z": {"nested": [1, True, None, "ünïcode"]}, "m": 1.5}
    assert dbd._canonical(payload) == epkg._canonical(payload)
    assert dr._canonical(payload) == epkg._canonical(payload)


def test_a_real_decision_fills_exactly_the_ps638_receipt_fields(ep):
    bound = _bound_decision(ep)
    kwargs = bound.decision.to_ps638_receipt_kwargs()
    # Exactly PS-638's fields: make_dispatch_receipt refuses unknown ones, and a
    # missing one would be a KeyError in the field list.
    assert set(kwargs) == set(dr.PS638_RECEIPT_FIELDS)
    receipt = make_dispatch_receipt(**kwargs)
    assert receipt.decided_by == dr.DECIDED_BY_POLICY
    assert receipt.policy_ref == bound.policy.policy_ref
    assert receipt.execution_package_hash == ep.package_hash
    assert receipt.packet_id == PACKET["packet_id"]
    assert receipt.run_id == "run-integration-1"
    assert receipt.selected_target_id == "profile:p-rtx"
    assert receipt.selected_host and receipt.selected_model
    assert receipt.requested_capabilities  # the packet's requirement class


def test_the_ps605_hash_equals_ps638s_own_receipt_hash(ep):
    kwargs = _bound_decision(ep).decision.to_ps638_receipt_kwargs()
    receipt = make_dispatch_receipt(**kwargs)
    # PS-605's re-derivation, PS-638's own stamp, and a re-derivation from the
    # SEALED dict must all be one hash.
    assert dbd.ps638_receipt_hash(kwargs) == receipt.receipt_hash
    assert dbd.ps638_receipt_hash(receipt.to_dict()) == receipt.receipt_hash
    assert receipt.receipt_hash == receipt.identity


def test_changing_the_selected_target_after_sealing_changes_the_hash(ep):
    receipt = make_dispatch_receipt(
        **_bound_decision(ep).decision.to_ps638_receipt_kwargs())
    for field, value in (("selected_target_id", "profile:elsewhere"),
                         ("selected_model", "some-other-model"),
                         ("selected_host", "some-other-host"),
                         ("policy_ref", "routing_policy@9.9+sha256:deadbeef"),
                         ("network_policy", "hosted-egress")):
        mutated = receipt.to_dict()
        mutated[field] = value
        assert dbd.ps638_receipt_hash(mutated) != receipt.receipt_hash, field


# ------------------------------------------------------------- attempt chain ---
def _attempt_for(ep, dispatch, *, hash_override="", **overrides):
    payload = {
        "receipt_id": "attempt-run-integration-1-1", "run_id": "run-integration-1",
        "packet_id": PACKET["packet_id"], "attempt": 1,
        "execution_package_hash": ep.package_hash,
        "dispatch_receipt_hash": hash_override or dispatch.receipt_hash,
        "target_id": dispatch.selected_target_id, "host": dispatch.selected_host,
        "model": dispatch.selected_model,
        "runtime_kind": dispatch.selected_runtime_kind,
        "runtime_version": dispatch.selected_runtime_version,
        "context_projection_hash": context_projection_digest("ctx"),
        "requested_context": 32768, "served_context": 32768}
    payload.update(overrides)
    return make_attempt_receipt(**payload)


def test_the_attempt_binds_to_the_exact_dispatch_receipt(ep):
    bound = _bound_decision(ep)
    dispatch = make_dispatch_receipt(**bound.decision.to_ps638_receipt_kwargs())
    attempt = _attempt_for(ep, dispatch)
    assert attempt_receipt_hash_is_valid(attempt.to_dict())
    # The chain, in the direction the evidence layer reads it.
    assert attempt.dispatch_receipt_hash == dispatch.receipt_hash
    assert dispatch.execution_package_hash == ep.package_hash
    assert package_hash_is_valid(ep.to_dict())
    assert attempt.target_id == bound.decision.selected_profile.target_id
    assert attempt.model == bound.decision.selected_profile.model
    assert attempt.host == bound.decision.selected_profile.host


def test_a_rebound_attempt_is_detectably_unbound(ep):
    bound = _bound_decision(ep)
    dispatch = make_dispatch_receipt(**bound.decision.to_ps638_receipt_kwargs())
    other = make_dispatch_receipt(
        **{**bound.decision.to_ps638_receipt_kwargs(),
           "reason": "a different decision entirely"})
    attempt = _attempt_for(ep, dispatch, hash_override=other.receipt_hash)
    # Both records are individually valid PS-638 records...
    assert attempt_receipt_hash_is_valid(attempt.to_dict())
    # ...and the BINDING is still detectably broken, which is the property the
    # evidence layer must CHECK rather than assume.
    assert attempt.dispatch_receipt_hash != dispatch.receipt_hash


def test_a_policy_receipt_cannot_be_built_without_a_selected_target(ep):
    """The frozen contract CANNOT represent "no target was selected".

    Recorded as a test because it is the PS-605 refusal gap: a refusal has no
    selected target, and this builder refuses empty ones by design.
    """
    from src.execution_package import ExecutionPackageError

    kwargs = _bound_decision(ep).decision.to_ps638_receipt_kwargs()
    for field in ("selected_target_id", "selected_host", "selected_model"):
        with pytest.raises(ExecutionPackageError):
            make_dispatch_receipt(**{**kwargs, field: ""})

