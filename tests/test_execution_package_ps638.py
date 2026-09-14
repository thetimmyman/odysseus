"""PS-638 — ExecutionPackage + DispatchDecisionReceipt.

The two properties worth reading the tests for:

* a writable package with no declared interface cannot be BUILT, so an
  under-specified packet never reaches a runtime;
* a dispatch receipt cannot claim routing policy it did not consult, and cannot
  carry a policy reference when it was an operator pin.
"""
import subprocess

import pytest

from src.execution_package import (
    DECIDED_BY_EXPLICIT_PIN,
    DECIDED_BY_POLICY,
    Budgets,
    ExecutionPackageError,
    VerificationPlan,
    build_execution_package,
    make_dispatch_receipt,
    package_hash_is_valid,
    seal_verifier_digests,
)
from src.source_snapshot import take_source_snapshot


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


@pytest.fixture()
def source(repo):
    return take_source_snapshot(str(repo), base_sha="HEAD",
                                relevant_paths=["src/thing.py"])


@pytest.fixture()
def plan(repo):
    return VerificationPlan(
        verifier_id="tests/t.py",
        command="python3 -m pytest tests/t.py -q",
        verifier_paths=["tests/t.py"],
        verifier_digests=seal_verifier_digests(str(repo), ["tests/t.py"]),
        positive_control="the known-good fixture passes",
        negative_control="a non-alphabetical order must FAIL")


PACKET = {
    "packet_id": "P-1",
    "objective": "implement the thing to the declared contract",
    "contract": "def thing(graph: dict) -> list",
    "role": "local_implementer",
    "write_scope": ["src/thing.py"],
    "interface": [{"name": "graph", "required": True,
                   "type_hint": "dict[str, list[str]]",
                   "semantics": "node -> successors"}],
    "test_command": "python3 -m pytest tests/t.py -q",
    "acceptance_criteria": ["respects every edge"],
    "negative_control": "a non-alphabetical order must FAIL",
    "stop_conditions": ["contract is ambiguous"],
    "base_sha": "HEAD",
}


def build(packet, source, plan, **kwargs):
    """Seal a package with the shipped builder, overriding nothing by default."""
    return build_execution_package(packet, source=source, verification=plan,
                                   run_id="r-1", jira_key="PS-638",
                                   allowed_tools=("write_file",), **kwargs)


# --------------------------------------------------------------- the packet ---
def test_a_packageable_packet_seals_with_a_valid_hash(source, plan):
    pkg = build(PACKET, source, plan)
    payload = pkg.to_dict()

    assert package_hash_is_valid(payload) is True
    assert payload["package_id"] == "P-1:r-1"
    assert payload["jira_key"] == "PS-638"
    assert payload["execution_role"] == "local_implementer"
    assert payload["interface_digest"] == pkg.interface_digest


def test_the_package_interface_matches_the_packet_interface(source, plan):
    from src.work_packet import interface_digest_of

    pkg = build(PACKET, source, plan)
    assert pkg.interface_digest == interface_digest_of(PACKET["interface"])
    assert pkg.normalized_interface == (
        "graph | required | dict[str, list[str]] -- node -> successors",)


def test_a_writable_packet_without_an_interface_is_refused(source, plan):
    packet = {k: v for k, v in PACKET.items() if k != "interface"}
    with pytest.raises(ExecutionPackageError) as exc:
        build(packet, source, plan)
    assert "interface must be non-empty" in str(exc.value)


def test_a_packet_without_an_objective_is_refused(source, plan):
    packet = dict(PACKET, objective="   ")
    with pytest.raises(ExecutionPackageError) as exc:
        build(packet, source, plan)
    assert "objective" in str(exc.value)


def test_a_default_requirement_set_is_derived(source, plan):
    pkg = build(PACKET, source, plan)
    ids = [r.requirement_id for r in pkg.evidence_requirements]
    assert ids == ["deterministic_verification", "source_binding",
                   "negative_control", "scope_check"]


def test_requirements_can_be_supplied_explicitly(source, plan):
    from src.evidence_contract import (KIND_TARGET_BINDING, EvidenceRequirement,
                                      INDEPENDENCE_INDEPENDENT_VERIFIER)

    custom = (EvidenceRequirement(
        requirement_id="target", kind=KIND_TARGET_BINDING, vantage="observer",
        expected_verifier="an independent runner",
        independence=INDEPENDENCE_INDEPENDENT_VERIFIER),)
    pkg = build(PACKET, source, plan, evidence_requirements=custom)
    assert [r.requirement_id for r in pkg.evidence_requirements] == ["target"]


def test_a_bare_string_interface_is_accepted_as_one_key(source, plan):
    """Declaring an interface must never be the harder path."""
    pkg = build(dict(PACKET, interface="graph"), source, plan)
    assert pkg.normalized_interface == ("graph | required",)


def test_an_interface_name_with_whitespace_is_refused(source, plan):
    with pytest.raises(ExecutionPackageError) as exc:
        build(dict(PACKET, interface="not a key"), source, plan)
    assert "no whitespace" in str(exc.value) or "whitespace" in str(exc.value)


def test_duplicate_interface_names_are_refused(source, plan):
    with pytest.raises(ExecutionPackageError) as exc:
        build(dict(PACKET, interface=["graph", "graph"]), source, plan)
    assert "duplicate" in str(exc.value)


# ------------------------------------------------------------------ scopes ---
def test_write_scope_may_be_narrowed(source, plan):
    packet = dict(PACKET, write_scope=["src/thing.py", "src/other.py"])
    pkg = build(packet, source, plan, write_scope=["src/thing.py"])
    assert pkg.write_scope == ("src/thing.py",)


def test_write_scope_may_not_be_widened(source, plan):
    with pytest.raises(ExecutionPackageError) as exc:
        build(PACKET, source, plan, write_scope=["src/thing.py", "/etc/passwd"])
    assert "widens" in str(exc.value)


# ------------------------------------------------------------------ hashing ---
def test_the_hash_is_stable_for_the_same_inputs(source, plan):
    first = build(PACKET, source, plan).package_hash
    second = build(PACKET, source, plan).package_hash
    assert first == second


def test_a_changed_objective_changes_the_hash(source, plan):
    base = build(PACKET, source, plan).package_hash
    other = build(dict(PACKET, objective="something else"),
                  source, plan).package_hash
    assert base != other


def test_a_changed_interface_changes_the_hash(source, plan):
    base = build(PACKET, source, plan).package_hash
    other = build(dict(PACKET, interface=[
        {"name": "graph", "required": True, "type_hint": "dict",
         "semantics": "different"}]),
        source, plan).package_hash
    assert base != other


def test_a_changed_source_changes_the_hash(repo, source, plan):
    base = build(PACKET, source, plan).package_hash
    (repo / "src" / "thing.py").write_text("VALUE = 2\n")
    moved = take_source_snapshot(str(repo), base_sha="HEAD",
                                relevant_paths=["src/thing.py"])
    assert build(PACKET, moved, plan).package_hash != base


def test_a_mutated_package_stops_verifying(source, plan):
    payload = build(PACKET, source, plan).to_dict()
    payload["objective"] = "quietly reworded"
    assert package_hash_is_valid(payload) is False


def test_a_missing_hash_is_not_valid(source, plan):
    payload = build(PACKET, source, plan).to_dict()
    payload.pop("package_hash")
    assert package_hash_is_valid(payload) is False


# ------------------------------------------------------- dispatch provenance ---
def _dispatch(pkg, **overrides):
    kwargs = dict(
        receipt_id="d-1",
        execution_package_hash=pkg.package_hash,
        run_id=pkg.run_id,
        packet_id=pkg.packet_id,
        selected_target_id="local-rtx4500",
        selected_host="minipc",
        selected_model="qwen3.8:27b",
        decided_by=DECIDED_BY_EXPLICIT_PIN,
        reason="operator pinned the node for the controlled experiment",
        granted_tools=("write_file",),
        granted_write_scope=("src/thing.py",),
        decided_at="2026-09-14T00:00:00+00:00",
    )
    kwargs.update(overrides)
    return make_dispatch_receipt(**kwargs)


def test_an_explicit_pin_is_recorded_as_such(source, plan):
    receipt = _dispatch(build(PACKET, source, plan))
    assert receipt.decided_by == DECIDED_BY_EXPLICIT_PIN
    assert receipt.policy_ref == ""
    assert receipt.receipt_hash


def test_a_policy_claim_without_a_policy_revision_is_refused(source, plan):
    pkg = build(PACKET, source, plan)
    with pytest.raises(ExecutionPackageError) as exc:
        _dispatch(pkg, decided_by=DECIDED_BY_POLICY)
    assert "requires a policy_ref" in str(exc.value)


def test_a_pin_may_not_carry_policy_authority(source, plan):
    pkg = build(PACKET, source, plan)
    with pytest.raises(ExecutionPackageError) as exc:
        _dispatch(pkg, policy_ref="routing-policy@r7")
    assert "must not carry a policy_ref" in str(exc.value)


def test_a_policy_receipt_with_a_revision_is_representable(source, plan):
    pkg = build(PACKET, source, plan)
    receipt = _dispatch(pkg, decided_by=DECIDED_BY_POLICY,
                        policy_ref="routing-policy@r7")
    assert receipt.decided_by == DECIDED_BY_POLICY


def test_dispatch_receipts_are_deterministic_for_the_same_inputs(source, plan):
    pkg = build(PACKET, source, plan)
    assert _dispatch(pkg).receipt_hash == _dispatch(pkg).receipt_hash


def test_a_dispatch_receipt_missing_a_target_is_refused(source, plan):
    pkg = build(PACKET, source, plan)
    with pytest.raises(ExecutionPackageError):
        _dispatch(pkg, selected_target_id="  ")


def test_an_unknown_dispatch_field_is_refused(source, plan):
    pkg = build(PACKET, source, plan)
    with pytest.raises(ExecutionPackageError):
        _dispatch(pkg, surprise="x")


# ------------------------------------------------------------------ budgets ---
def test_budgets_default_to_a_bounded_envelope(source, plan):
    pkg = build(PACKET, source, plan)
    assert pkg.budgets.max_attempts == 3
    assert pkg.budgets.max_repair_chars == 6000


def test_a_negative_budget_is_refused():
    with pytest.raises(ExecutionPackageError):
        Budgets(max_attempts=-1)


def test_budgets_are_part_of_the_package_hash(source, plan):
    base = build(PACKET, source, plan).package_hash
    smaller = build(PACKET, source, plan, budgets=Budgets(max_attempts=1))
    assert smaller.package_hash != base


# -------------------------------------------------------- verification plan ---
def test_a_plan_without_a_verifier_identity_is_refused():
    with pytest.raises(ExecutionPackageError):
        VerificationPlan(verifier_id="", command="pytest -q")


def test_a_plan_without_a_command_is_refused():
    with pytest.raises(ExecutionPackageError):
        VerificationPlan(verifier_id="tests/t.py", command=" ")


def test_sealed_verifier_digests_hash_the_real_artifact(repo):
    digests = seal_verifier_digests(str(repo), ["tests/t.py"])
    assert len(digests) == 1
    assert len(digests[0][1]) == 64
    (repo / "tests" / "t.py").write_text("def test_x():\n    assert False\n")
    moved = seal_verifier_digests(str(repo), ["tests/t.py"])
    assert moved[0][1] != digests[0][1]


def test_a_package_requires_a_verification_plan(source):
    with pytest.raises(ExecutionPackageError):
        build_execution_package(PACKET, source=source, verification=None,
                                run_id="r-1")


def test_a_package_requires_a_run_id(source, plan):
    with pytest.raises(ExecutionPackageError):
        build_execution_package(PACKET, source=source, verification=plan,
                                run_id="")



