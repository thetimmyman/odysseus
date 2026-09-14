"""PS-638 — EvidencePackage validator: the 15 required fail-closed rejections.

Each negative case is expressed as a realistic MIS-AUTHORING — a package built
the way a careless producer would build it — and then sealed properly. That
ordering matters: a case that only trips the package hash would prove the hash
works, not that the rule works. So every fixture below is internally consistent
and still rejected for one specific, named reason.

``test_the_positive_fixture_is_verified`` is the control: without it, a validator
that rejected everything would pass this file.
"""
import hashlib
import subprocess

import pytest

from src.attempt_receipt import (
    ArtifactRef,
    make_attempt_receipt,
    make_verification_receipt,
)
from src.evidence_contract import INDEPENDENCE_HARNESS_HIDDEN
from src.evidence_package import (
    ARTIFACT_HASH_MISMATCH,
    ARTIFACT_UNAVAILABLE,
    CLAIMED_PASS_WITH_NONZERO_VERIFIER,
    CONTEXT_PROJECTION_MISSING_INTERFACE,
    DISPATCH_TARGET_MISMATCH,
    EVIDENCE_PACKAGE_HASH_MISMATCH,
    INTERFACE_CHANGED_AFTER_SEALING,
    MISSING_REQUIRED_NEGATIVE_CONTROL,
    PREEXISTING_FAILURE_WITHOUT_BASELINE,
    RETRY_HISTORY_OMITTED,
    SECRET_SHAPED_FIXTURE_LEAK,
    SOURCE_CHANGED_AFTER_VERIFICATION,
    SOURCE_IDENTITY_MISSING_OR_AMBIGUOUS,
    VERIFIER_IDENTITY_MISMATCH,
    WORKER_AUTHORED_EVIDENCE_NOT_INDEPENDENT,
    WRITABLE_PACKAGE_MISSING_INTERFACE,
    WRITE_OUTSIDE_AUTHORIZED_SCOPE,
    compute_evidence_package_hash,
    find_secret_shaped,
    reseal_evidence_payload,
    seal_evidence_package,
    validate_evidence_package,
)
from src.execution_package import (
    DECIDED_BY_EXPLICIT_PIN,
    VerificationPlan,
    build_execution_package,
    make_dispatch_receipt,
    seal_verifier_digests,
)
from src.source_snapshot import source_snapshot_from_dict, take_source_snapshot
from src.worker_context import render_worker_context

ARTIFACT = "src/thing.py"
VERIFIER = "tests/t.py"
REQUIREMENTS = ("deterministic_verification", "source_binding",
                "negative_control", "scope_check")


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
    (root / "src" / "thing.py").write_text("def thing(graph):\n    return []\n")
    (root / "tests" / "t.py").write_text("def test_x():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


PACKET = {
    "packet_id": "P-1",
    "objective": "implement the deterministic orderer to the declared contract",
    "contract": "def thing(graph: dict) -> list",
    "role": "local_implementer",
    "write_scope": [ARTIFACT],
    "interface": [{"name": "graph", "required": True,
                   "type_hint": "dict[str, list[str]]",
                   "semantics": "node -> successor nodes"}],
    "test_command": f"python3 -m pytest {VERIFIER} -q",
    "acceptance_criteria": ["respects every edge"],
    "negative_control": "a non-alphabetical order must FAIL",
    "stop_conditions": ["contract is ambiguous"],
    "base_sha": "HEAD",
}


def _write_artifact(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    data = text.encode("utf-8")
    ref = ArtifactRef(artifact_id=name, sha256=hashlib.sha256(data).hexdigest(),
                      size=len(data), storage_uri=f"file://{path}")
    return ref.to_dict(), data


def make_run(root, **opts):
    """Build a complete, sealed package; each opt models ONE authoring mistake."""
    source = take_source_snapshot(str(root), base_sha="HEAD",
                                  relevant_paths=[ARTIFACT, VERIFIER])
    plan = VerificationPlan(
        verifier_id=VERIFIER, command=f"python3 -m pytest {VERIFIER} -q",
        verifier_paths=[VERIFIER],
        verifier_digests=seal_verifier_digests(str(root), [VERIFIER]),
        positive_control="the known-good fixture passes",
        negative_control="a non-alphabetical order must FAIL")
    package = build_execution_package(
        opts.get("packet", PACKET), source=source, verification=plan,
        run_id="r-1", jira_key="PS-638", allowed_tools=("write_file",))

    dispatch = make_dispatch_receipt(
        receipt_id="d-1", execution_package_hash=package.package_hash,
        run_id="r-1", packet_id="P-1", selected_target_id="local-rtx4500",
        selected_host="minipc", selected_model="qwen3.8:27b",
        decided_by=DECIDED_BY_EXPLICIT_PIN,
        reason=("operator pinned the node for the controlled experiment"
                + ("; token = AbCdEf1234567890"
                   if opts.get("payload_secret") else "")),
        granted_tools=("write_file",), granted_write_scope=(ARTIFACT,),
        decided_at="2026-09-14T00:00:00+00:00")

    context_packet = dict(opts.get("packet", PACKET))
    if opts.get("interface_in_context") is False:
        context_packet.pop("interface")
    text = render_worker_context(context_packet,
                                 max_chars=opts.get("context_max_chars", 4000))
    if opts.get("context_secret"):
        text += "\nNOTES: api_key=AbCdEf1234567890\n"
    ctx_ref, ctx_bytes = _write_artifact(root, "evidence/context-1.txt", text)
    if opts.get("artifact_digest_override"):
        ctx_ref = dict(ctx_ref, sha256=opts["artifact_digest_override"])
    if opts.get("artifact_storage_broken"):
        ctx_ref = dict(ctx_ref, storage_uri="file:///nonexistent/context.txt")
    context_ref = None if opts.get("context_ref_missing") else ctx_ref

    attempts = []
    for number in opts.get("attempts", (1,)):
        attempts.append(make_attempt_receipt(
            receipt_id=f"a-{number}", run_id="r-1", packet_id="P-1",
            attempt=number, repair_of=0 if number == 1 else number - 1,
            execution_package_hash=package.package_hash,
            dispatch_receipt_hash=opts.get("dispatch_hash",
                                           dispatch.receipt_hash),
            target_id=opts.get("target_id", "local-rtx4500"),
            host=opts.get("host", "minipc"),
            model=opts.get("model", "qwen3.8:27b"),
            context_projection_hash=hashlib.sha256(ctx_bytes).hexdigest(),
            rendered_context_ref=(ArtifactRef(**context_ref)
                                  if context_ref else None),
            output_ref=ArtifactRef(**ctx_ref),
            actual_write_set=tuple(opts.get("actual_write_set", (ARTIFACT,))),
            declared_write_set=(ARTIFACT,), served_context=32768,
            finish_reason="stop"))

    stdout_ref, _ = _write_artifact(root, "evidence/stdout-1.txt", "8 passed\n")
    verification_kwargs = dict(
        run_id="r-1", packet_id="P-1", attempt=1,
        execution_package_hash=package.package_hash, verifier_id=VERIFIER,
        normalized_command=f"python3 -m pytest {VERIFIER} -q",
        verifier_paths=(VERIFIER,), stdout_ref=ArtifactRef(**stdout_ref),
        stdout_complete=opts.get("stdout_complete", True),
        tests_collected=8, tests_executed=8, tests_passed=8,
        tests_failed=opts.get("tests_failed", 0),
        requirement_ids=opts.get("requirement_ids", REQUIREMENTS),
        proof_class=opts.get("proof_class", INDEPENDENCE_HARNESS_HIDDEN),
        control_id="negative_control" if opts.get("control", True) else "",
        control_expected="FAIL on a non-alphabetical order",
        control_observed="FAIL",
        control_passed=(opts.get("control_passed", True)
                        if opts.get("control", True) else None),
        failure_fingerprint=opts.get("failure_fingerprint", ""),
        claimed_preexisting=opts.get("claimed_preexisting", False),
        baseline_receipt_hash=opts.get("baseline_receipt_hash", ""),
        baseline_source_digest=opts.get("baseline_source_digest", ""),
        baseline_failure_fingerprint=opts.get(
            "baseline_failure_fingerprint", ""))

    verifications = [make_verification_receipt(
        receipt_id="v-1", exit_code=opts.get("exit_code", 0),
        source_snapshot_digest=opts.get("receipt_source_digest",
                                        source.snapshot_digest),
        verifier_digest=opts.get("verifier_digest", plan.digest_of(VERIFIER)),
        **verification_kwargs)]
    if opts.get("extra_receipt_source_digest"):
        verifications.append(make_verification_receipt(
            receipt_id="v-2", exit_code=opts.get("exit_code", 0),
            source_snapshot_digest=opts["extra_receipt_source_digest"],
            verifier_digest=opts.get("verifier_digest", plan.digest_of(VERIFIER)),
            **verification_kwargs))

    payload = seal_evidence_package(
        evidence_package_id="ev-1", execution_package=package,
        dispatch_receipts=(dispatch,), attempt_receipts=tuple(attempts),
        verification_receipts=tuple(verifications),
        waivers=opts.get("waivers")).to_dict()

    return _apply_edits(payload, package.to_dict(), opts), source


def _apply_edits(payload, package_payload, opts):
    """Intentional edits, each resealed so the SEMANTIC rule is what fires.

    A fixture that only tripped the package hash would prove the hash works, not
    that the rule works.
    """
    edited = False
    if opts.get("drop_interface"):
        package_payload = dict(package_payload, interface=[],
                               interface_digest="")
        edited = True
    if opts.get("bend_interface"):
        package_payload = dict(
            package_payload,
            interface=list(package_payload["interface"]) + ["extra | optional"])
        edited = True
    if opts.get("drop_base_sha"):
        stripped = dict(package_payload["source"], base_sha="")
        package_payload = dict(
            package_payload,
            source=source_snapshot_from_dict(stripped).to_dict())
        edited = True
    if edited:
        payload = reseal_evidence_payload(
            dict(payload, execution_package=package_payload))
    if opts.get("recorded_outcome"):
        receipts = [dict(r) for r in payload["verification_receipts"]]
        receipts[0]["outcome"] = opts["recorded_outcome"]
        payload = reseal_evidence_payload(
            dict(payload, verification_receipts=receipts))
    if opts.get("no_attempts"):
        payload = reseal_evidence_payload(dict(payload, attempt_receipts=[]))
    if opts.get("tamper_after_seal"):
        package_payload = dict(package_payload, objective="quietly reworded")
        payload = dict(payload, execution_package=package_payload)
    return payload


# =============================================================== the control ===
def test_the_positive_fixture_is_verified(repo):
    """Without this, a validator that rejected everything would pass this file."""
    payload, _source = make_run(repo)
    result = validate_evidence_package(payload)

    assert result.ok is True, result.explain()
    assert result.issues == ()
    states = {s.requirement_id: s.state for s in result.requirement_states}
    assert states["deterministic_verification"] == "SATISFIED"
    assert states["source_binding"] == "SATISFIED"
    assert states["negative_control"] == "SATISFIED"
    assert states["scope_check"] == "SATISFIED"


def test_the_positive_fixture_is_verified_against_its_own_source(repo):
    payload, source = make_run(repo)
    result = validate_evidence_package(payload,
                                      current_source=source.to_dict())
    assert result.ok is True, result.explain()


def test_an_artifact_declared_under_seals_is_checked_like_any_other(repo):
    """MUTATION CONTROL for the seal-ref extension (PS-635 G2).

    A seal is part of the package's own evidence, so a reference it declares must
    be loadable and hash-correct. Without this the manager seam could seal a
    planner input/output that nobody could ever produce — evidence in name only.
    """
    payload, _source = make_run(repo)
    good_ref = dict(payload["attempt_receipts"][0]["rendered_context_ref"])
    payload = reseal_evidence_payload(
        dict(payload, seals=[{"seal_id": "manager_seam-1",
                              "input_ref": dict(good_ref),
                              "output_ref": dict(good_ref)}]))
    assert validate_evidence_package(payload).ok is True

    payload = reseal_evidence_payload(dict(
        payload,
        seals=[{"seal_id": "manager_seam-1",
                "input_ref": dict(good_ref, sha256="0" * 64)}]))
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert ARTIFACT_HASH_MISMATCH in result.codes


# ==================================================== the 15 required cases ===
# 1. missing / ambiguous source identity
def test_missing_source_base_sha_is_rejected(repo):
    payload, _ = make_run(repo, drop_base_sha=True)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(SOURCE_IDENTITY_MISSING_OR_AMBIGUOUS), result.explain()


# 2. writable package missing interface
def test_a_writable_package_with_no_interface_is_rejected(repo):
    payload, _ = make_run(repo, drop_interface=True)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(WRITABLE_PACKAGE_MISSING_INTERFACE), result.explain()


# 3. interface changed after packet sealing
def test_an_interface_changed_after_sealing_is_rejected(repo):
    payload, _ = make_run(repo, bend_interface=True)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(INTERFACE_CHANGED_AFTER_SEALING), result.explain()


# 4. context projection does not contain the sealed interface
def test_a_context_without_the_sealed_interface_is_rejected(repo):
    payload, _ = make_run(repo, interface_in_context=False)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(CONTEXT_PROJECTION_MISSING_INTERFACE), result.explain()


def test_a_truncated_context_that_cuts_the_interface_is_rejected(repo):
    """The bound must not silently remove the one section that matters."""
    payload, _ = make_run(repo, context_max_chars=60)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(CONTEXT_PROJECTION_MISSING_INTERFACE), result.explain()


def test_an_attempt_with_no_rendered_context_is_rejected(repo):
    from src.evidence_package import RENDERED_CONTEXT_MISSING

    payload, _ = make_run(repo, context_ref_missing=True)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(RENDERED_CONTEXT_MISSING), result.explain()


# 5. artifact hash mismatch
def test_an_artifact_hash_mismatch_is_rejected(repo):
    payload, _ = make_run(repo, artifact_digest_override="0" * 64)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(ARTIFACT_HASH_MISMATCH), result.explain()


def test_an_unretrievable_artifact_is_rejected(repo):
    payload, _ = make_run(repo, artifact_storage_broken=True)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(ARTIFACT_UNAVAILABLE), result.explain()


# 6. verifier identity / hash mismatch
def test_a_verifier_digest_that_is_not_the_sealed_one_is_rejected(repo):
    payload, _ = make_run(repo, verifier_digest="f" * 64)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(VERIFIER_IDENTITY_MISMATCH), result.explain()


def test_a_verifier_that_was_never_planned_is_rejected(repo):
    payload, _ = make_run(repo)
    receipts = [dict(r) for r in payload["verification_receipts"]]
    receipts[0]["verifier_id"] = "tests/somewhere_else.py"
    result = validate_evidence_package(
        reseal_evidence_payload(dict(payload, verification_receipts=receipts)))
    assert result.ok is False
    assert result.has(VERIFIER_IDENTITY_MISMATCH), result.explain()


# 7. claimed PASS with a non-zero required verifier result
def test_a_recorded_pass_over_a_nonzero_exit_code_is_rejected(repo):
    payload, _ = make_run(repo, exit_code=1, tests_failed=1,
                          recorded_outcome="PASS")
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(CLAIMED_PASS_WITH_NONZERO_VERIFIER), result.explain()


def test_a_zero_exit_code_with_failing_tests_cannot_even_be_written(repo):
    from src.attempt_receipt import ReceiptError

    with pytest.raises(ReceiptError):
        make_verification_receipt(
            receipt_id="v-x", run_id="r", packet_id="p", attempt=1,
            execution_package_hash="h", verifier_id=VERIFIER,
            normalized_command="pytest -q", source_snapshot_digest="d",
            exit_code=0, tests_failed=3)


def test_an_incomplete_capture_cannot_claim_a_pass(repo):
    payload, _ = make_run(repo, stdout_complete=False)
    result = validate_evidence_package(payload)
    assert result.ok is False
    # The pass cannot be derived, so the mandatory requirement never closes.
    from src.evidence_package import REQUIREMENT_FAILED, REQUIREMENT_UNRESOLVED

    assert result.has(REQUIREMENT_FAILED) or result.has(REQUIREMENT_UNRESOLVED)
    states = {s.requirement_id: s.state for s in result.requirement_states}
    assert states["deterministic_verification"] != "SATISFIED"


# 8. missing required negative control
def test_a_declared_negative_control_that_never_ran_is_rejected(repo):
    payload, _ = make_run(repo, control=False)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(MISSING_REQUIRED_NEGATIVE_CONTROL), result.explain()


def test_a_negative_control_that_did_not_discriminate_is_rejected(repo):
    payload, _ = make_run(repo, control_passed=False)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(MISSING_REQUIRED_NEGATIVE_CONTROL), result.explain()


# 9. actual write outside authorized scope
def test_a_write_outside_the_authorized_scope_is_rejected(repo):
    payload, _ = make_run(repo, actual_write_set=(ARTIFACT, "src/elsewhere.py"))
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(WRITE_OUTSIDE_AUTHORIZED_SCOPE), result.explain()


# 10. execution target differs from dispatch authorization
def test_an_attempt_on_an_unauthorized_target_is_rejected(repo):
    payload, _ = make_run(repo, target_id="local-msr1", host="msr1")
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(DISPATCH_TARGET_MISMATCH), result.explain()


def test_an_attempt_citing_an_unknown_dispatch_receipt_is_rejected(repo):
    payload, _ = make_run(repo, dispatch_hash="0" * 64)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(DISPATCH_TARGET_MISMATCH), result.explain()


# 11. source changes after verification
def test_a_source_that_moved_after_verification_is_rejected(repo):
    payload, source = make_run(repo)
    (repo / "src" / "thing.py").write_text("def thing(graph):\n    return [1]\n")
    moved = take_source_snapshot(str(repo), base_sha="HEAD",
                                 relevant_paths=[ARTIFACT, VERIFIER])
    result = validate_evidence_package(payload,
                                      current_source=moved.to_dict())
    assert result.ok is False
    assert result.has(SOURCE_CHANGED_AFTER_VERIFICATION), result.explain()


def test_a_verification_on_a_different_snapshot_is_not_rejected_when_a_write_explains_it(repo):
    """A writable run changes the tree by design; the difference must be explicable.

    This is the shape EVERY real writable run has: the package seals the INPUT
    identity, and verification necessarily happens after the worker wrote. If the
    validator demanded equality, honest evidence could not exist.
    """
    payload, _ = make_run(repo, receipt_source_digest="d" * 64)
    result = validate_evidence_package(payload)
    assert not result.has(SOURCE_CHANGED_AFTER_VERIFICATION), result.explain()


def test_a_verified_tree_that_differs_with_no_recorded_write_is_rejected(repo):
    payload, _ = make_run(repo, receipt_source_digest="d" * 64,
                          actual_write_set=())
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(SOURCE_CHANGED_AFTER_VERIFICATION), result.explain()


def test_verifications_on_different_trees_are_allowed_for_a_repair_run(repo):
    """A repair run verifies a DIFFERENT tree after each attempt, by design.

    This rule was withdrawn on live evidence (2026-09-14): the first real
    two-attempt run produced two different verified-tree digests, and a rule
    demanding they agree would have rejected correct evidence — or pushed authors
    into recording one digest for several trees, which is the falsehood the check
    exists to catch.
    """
    payload, _ = make_run(repo, extra_receipt_source_digest="e" * 64,
                          attempts=(1, 2))
    result = validate_evidence_package(payload)
    assert not result.has(SOURCE_CHANGED_AFTER_VERIFICATION), result.explain()


# 12. retry history omitted to make a final result look cleaner
def test_attempts_that_skip_the_first_are_rejected(repo):
    payload, _ = make_run(repo, attempts=(2,))
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(RETRY_HISTORY_OMITTED), result.explain()


def test_a_gap_in_the_retry_history_is_rejected(repo):
    payload, _ = make_run(repo, attempts=(1, 3))
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(RETRY_HISTORY_OMITTED), result.explain()


def test_verifications_with_no_attempt_at_all_are_rejected(repo):
    from src.evidence_package import NO_ATTEMPTS

    payload, _ = make_run(repo, no_attempts=True)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(NO_ATTEMPTS), result.explain()


def test_contiguous_attempts_are_accepted(repo):
    payload, _ = make_run(repo, attempts=(1, 2))
    result = validate_evidence_package(payload)
    assert not result.has(RETRY_HISTORY_OMITTED), result.explain()


# 13. claimed pre-existing failure without baseline proof
def test_a_preexisting_claim_without_a_baseline_is_rejected(repo):
    payload, _ = make_run(repo, exit_code=1, tests_failed=1,
                          failure_fingerprint="abc123",
                          claimed_preexisting=True)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(PREEXISTING_FAILURE_WITHOUT_BASELINE), result.explain()


def test_a_preexisting_claim_with_a_different_mechanism_is_rejected(repo):
    payload, _ = make_run(
        repo, exit_code=1, tests_failed=1, failure_fingerprint="newfingerprint",
        claimed_preexisting=True, baseline_receipt_hash="b" * 64,
        baseline_source_digest="s" * 64,
        baseline_failure_fingerprint="oldfingerprint")
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(PREEXISTING_FAILURE_WITHOUT_BASELINE), result.explain()


def test_a_preexisting_claim_with_a_matching_baseline_is_not_rejected(repo):
    fingerprint = "samefingerprint"
    payload, _ = make_run(
        repo, exit_code=1, tests_failed=1, failure_fingerprint=fingerprint,
        claimed_preexisting=True, baseline_receipt_hash="b" * 64,
        baseline_source_digest="s" * 64,
        baseline_failure_fingerprint=fingerprint)
    result = validate_evidence_package(payload)
    assert not result.has(PREEXISTING_FAILURE_WITHOUT_BASELINE), result.explain()


# 14. worker-authored-only evidence satisfying an independent-proof requirement
def test_worker_authored_proof_cannot_close_an_independent_requirement(repo):
    payload, _ = make_run(repo, proof_class="WORKER_AUTHORED")
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(WORKER_AUTHORED_EVIDENCE_NOT_INDEPENDENT), result.explain()
    states = {s.requirement_id: s.state for s in result.requirement_states}
    assert states["deterministic_verification"] == "FAILED"


# 15. prohibited secret-shaped fixture leaking into the package
def test_a_secret_shaped_fixture_in_the_projection_is_rejected(repo):
    payload, _ = make_run(repo, context_secret=True)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(SECRET_SHAPED_FIXTURE_LEAK), result.explain()
    # The report must name WHERE, never WHAT.
    for issue in result.issues:
        if issue.code == SECRET_SHAPED_FIXTURE_LEAK:
            assert "AbCdEf1234567890" not in issue.detail


def test_secret_shaped_values_are_detected_by_shape_not_by_key():
    assert find_secret_shaped({"a": "sk-" + "x" * 24})
    assert find_secret_shaped({"a": "ghp_" + "y" * 30})
    assert find_secret_shaped({"a": "token = " + "z" * 20})
    assert find_secret_shaped({"a": "Authorization: Bearer " + "q" * 20})
    assert find_secret_shaped({"a": "just an ordinary sentence"}) == ()


def test_a_secret_shaped_value_in_the_package_payload_is_rejected(repo):
    """A leak into a RECEIPT is a different path from a leak into a projection."""
    payload, _ = make_run(repo, payload_secret=True)
    assert find_secret_shaped(payload), "fixture did not actually leak"

    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(SECRET_SHAPED_FIXTURE_LEAK), result.explain()


# ================================================== further fail-closed rules ===
def test_a_package_edited_after_sealing_is_rejected(repo):
    payload, _ = make_run(repo, tamper_after_seal=True)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(EVIDENCE_PACKAGE_HASH_MISMATCH), result.explain()


def test_a_tampered_requirement_state_is_rejected(repo):
    from src.evidence_package import REQUIREMENT_STATE_TAMPERED

    honest, _ = make_run(repo, exit_code=1, tests_failed=1)
    assert validate_evidence_package(honest).ok is False

    states = [dict(s) for s in honest["requirement_states"]]
    assert any(s["state"] == "FAILED" for s in states)
    for state in states:
        state["state"] = "SATISFIED"
    lying = reseal_evidence_payload(dict(honest, requirement_states=states))

    result = validate_evidence_package(lying)
    assert result.ok is False
    assert result.has(REQUIREMENT_STATE_TAMPERED), result.explain()


def test_an_unresolved_mandatory_requirement_blocks_verification(repo):
    from src.evidence_package import REQUIREMENT_UNRESOLVED

    payload, _ = make_run(repo, requirement_ids=("deterministic_verification",))
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(REQUIREMENT_UNRESOLVED), result.explain()


def test_a_failed_mandatory_requirement_blocks_verification(repo):
    from src.evidence_package import REQUIREMENT_FAILED

    payload, _ = make_run(repo, exit_code=1, tests_failed=1)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.has(REQUIREMENT_FAILED), result.explain()


def test_an_explicit_waiver_is_the_only_route_to_not_applicable(repo):
    """NOT_APPLICABLE requires a recorded reason; absence is never a waiver."""
    delivered = ("deterministic_verification", "negative_control", "scope_check")
    payload, _ = make_run(repo, requirement_ids=delivered, waivers={
        "source_binding": "this packet declares no source dependency"})

    result = validate_evidence_package(payload)
    states = {s.requirement_id: s.state for s in result.requirement_states}
    assert states["source_binding"] == "NOT_APPLICABLE"
    assert states["deterministic_verification"] == "SATISFIED"
    assert result.ok is True, result.explain()


def test_a_requirement_nobody_waived_stays_unresolved(repo):
    from src.evidence_package import REQUIREMENT_UNRESOLVED

    delivered = ("deterministic_verification", "negative_control", "scope_check")
    payload, _ = make_run(repo, requirement_ids=delivered)
    result = validate_evidence_package(payload)

    states = {s.requirement_id: s.state for s in result.requirement_states}
    assert states["source_binding"] == "UNRESOLVED"
    assert result.has(REQUIREMENT_UNRESOLVED), result.explain()


def test_a_package_that_is_not_a_mapping_is_rejected():
    result = validate_evidence_package(["not", "a", "package"])
    assert result.ok is False
    assert result.explain()


def test_the_validator_returns_a_named_reason_not_a_bare_false(repo):
    payload, _ = make_run(repo, drop_interface=True)
    result = validate_evidence_package(payload)
    assert result.ok is False
    assert result.codes
    assert all(isinstance(code, str) and code for code in result.codes)
    assert "REJECTED:" in result.explain()





