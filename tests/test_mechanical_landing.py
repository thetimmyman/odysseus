import hashlib
import subprocess

import pytest

from src.attempt_receipt import ArtifactRef, make_attempt_receipt, make_verification_receipt
from src.execution_package import (
    DECIDED_BY_EXPLICIT_PIN, VerificationPlan, build_execution_package,
    make_dispatch_receipt, seal_verifier_digests,
)
from src.evidence_package import validate_evidence_package, seal_evidence_package
from src.mechanical_landing import (
    LandingPolicy, LandingRefusalCode, LandingRefused, LandingStrategy,
    JiraReconciliationAdapter, RepositoryLandingAdapter, SemanticAcceptance,
    evaluate_landing_eligibility, landing_receipt_hash_is_valid,
    land_exact_candidate, make_semantic_acceptance, prove_landed_equivalence,
)
from src.source_snapshot import take_source_snapshot
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
    (root / ARTIFACT).write_text("def thing():\n    return 'A'\n")
    (root / VERIFIER).write_text("def test_x():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


PACKET = {
    "packet_id": "P-1", "objective": "implement the candidate",
    "contract": "def thing() -> str", "role": "local_implementer",
    "write_scope": [ARTIFACT],
    "interface": [{"name": "thing", "required": True,
                   "type_hint": "callable", "semantics": "candidate function"}],
    "test_command": f"python3 -m pytest {VERIFIER} -q",
    "acceptance_criteria": ["returns the candidate value"],
    "negative_control": "a broken implementation must FAIL",
    "stop_conditions": ["contract is ambiguous"],
}


def _artifact_ref(path, content):
    data = content.encode()
    return ArtifactRef(artifact_id=str(path), sha256=hashlib.sha256(data).hexdigest(),
                       size=len(data), storage_uri=f"file://{path}")


def make_real_run(root, *, out_of_scope=False, candidate_value="B"):
    source_a = take_source_snapshot(str(root), base_sha="HEAD",
                                    relevant_paths=[ARTIFACT, VERIFIER])
    plan = VerificationPlan(
        verifier_id=VERIFIER, command=f"python3 -m pytest {VERIFIER} -q",
        verifier_paths=[VERIFIER], verifier_digests=seal_verifier_digests(str(root), [VERIFIER]),
        positive_control="known-good passes", negative_control="broken fails")
    package = build_execution_package(PACKET, source=source_a, verification=plan,
                                      run_id="r-1", jira_key="PS-578",
                                      allowed_tools=("write_file",))
    dispatch = make_dispatch_receipt(
        receipt_id="d-1", execution_package_hash=package.package_hash,
        run_id="r-1", packet_id="P-1", selected_target_id="local",
        selected_host="test", selected_model="fixture", decided_by=DECIDED_BY_EXPLICIT_PIN,
        reason="controlled fixture", granted_tools=("write_file",),
        granted_write_scope=(ARTIFACT,), decided_at="2026-09-16T00:00:00+00:00")

    evidence_dir = root.parent / "evidence"
    evidence_dir.mkdir()
    context = render_worker_context(PACKET, max_chars=4000)
    context_path = evidence_dir / "context.txt"
    context_path.write_text(context)
    context_ref = _artifact_ref(context_path, context)
    output_path = evidence_dir / "output.txt"
    output_path.write_text(candidate_value)
    output_ref = _artifact_ref(output_path, candidate_value)

    # Worker changes the tracked artifact: this is candidate B, not input A.
    (root / ARTIFACT).write_text(f"def thing():\n    return '{candidate_value}'\n")
    source_b = take_source_snapshot(str(root), base_sha="HEAD",
                                    relevant_paths=[ARTIFACT, VERIFIER])
    actual = [ARTIFACT] + (["escape.txt"] if out_of_scope else [])
    attempt = make_attempt_receipt(
        receipt_id="a-1", run_id="r-1", packet_id="P-1", attempt=1,
        execution_package_hash=package.package_hash,
        dispatch_receipt_hash=dispatch.receipt_hash, target_id="local", host="test",
        model="fixture", context_projection_hash=hashlib.sha256(context.encode()).hexdigest(),
        rendered_context_ref=context_ref, output_ref=output_ref,
        actual_write_set=tuple(actual), declared_write_set=(ARTIFACT,))
    stdout_path = evidence_dir / "stdout.txt"
    stdout_path.write_text("1 passed\n")
    stdout_ref = _artifact_ref(stdout_path, "1 passed\n")
    verification = make_verification_receipt(
        receipt_id="v-1", run_id="r-1", packet_id="P-1", attempt=1,
        execution_package_hash=package.package_hash, verifier_id=VERIFIER,
        normalized_command=f"python3 -m pytest {VERIFIER} -q",
        source_snapshot_digest=source_b.snapshot_digest,
        exit_code=0,
        verifier_digest=plan.digest_of(VERIFIER), verifier_paths=(VERIFIER,),
        worktree=str(root), ended_at="2026-09-16T00:00:01+00:00",
        stdout_ref=stdout_ref, tests_collected=1, tests_executed=1,
        tests_passed=1, tests_failed=0, requirement_ids=REQUIREMENTS,
        control_id="negative_control", control_expected="FAIL", control_observed="FAIL",
        control_passed=True)
    package_payload = seal_evidence_package(
        evidence_package_id="ev-1", execution_package=package,
        dispatch_receipts=(dispatch,), attempt_receipts=(attempt,),
        verification_receipts=(verification,)).to_dict()
    return package_payload, source_a, source_b, plan, verification


def acceptance_for(package, source):
    return make_semantic_acceptance(
        acceptance_id="accept-1", reviewer_id="reviewer-1",
        evidence_package_hash=package["evidence_package_hash"],
        candidate_source_digest=source.snapshot_digest, candidate_head_sha=source.head_sha,
        candidate_diff_digest=source.tracked_diff_digest,
        observed_at="2026-09-16T00:00:02+00:00", accepted_at="2026-09-16T00:00:03+00:00")


def test_real_candidate_producing_path_uses_verified_output_B(repo):
    package, source_a, source_b, _, _ = make_real_run(repo)
    assert source_a.snapshot_digest != source_b.snapshot_digest
    assert package["execution_package"]["source"]["snapshot_digest"] == source_a.snapshot_digest
    assert validate_evidence_package(package, current_source=source_b.to_dict()).ok
    result = evaluate_landing_eligibility(
        package, acceptance_for(package, source_b), current_source=source_b.to_dict(),
        policy=LandingPolicy((LandingStrategy.SQUASH,), ()), strategy=LandingStrategy.SQUASH)
    assert result.eligible


def test_acceptance_of_input_A_is_not_acceptance_of_verified_B(repo):
    package, source_a, source_b, _, _ = make_real_run(repo)
    result = evaluate_landing_eligibility(
        package, acceptance_for(package, source_a), current_source=source_b.to_dict(),
        policy=LandingPolicy((LandingStrategy.SQUASH,), ()))
    assert LandingRefusalCode.ACCEPTANCE_SOURCE_MISMATCH.value in result.codes


def test_current_C_after_acceptance_B_refuses(repo):
    package, _, source_b, _, _ = make_real_run(repo)
    current_c = dict(source_b.to_dict(), snapshot_digest="c" * 64)
    result = evaluate_landing_eligibility(
        package, acceptance_for(package, source_b), current_source=current_c,
        policy=LandingPolicy((LandingStrategy.SQUASH,), ()))
    assert LandingRefusalCode.EVIDENCE_STALE.value in result.codes
    assert LandingRefusalCode.CANDIDATE_CHANGED.value in result.codes


def test_real_out_of_scope_attempt_refuses(repo):
    package, _, source_b, _, _ = make_real_run(repo, out_of_scope=True)
    result = evaluate_landing_eligibility(
        package, acceptance_for(package, source_b), current_source=source_b.to_dict(),
        policy=LandingPolicy((LandingStrategy.SQUASH,), ()))
    assert LandingRefusalCode.WRITE_SCOPE_MISMATCH.value in result.codes


def test_real_invalid_and_stale_evidence_refuse(repo):
    package, _, source_b, _, _ = make_real_run(repo)
    invalid = dict(package, evidence_package_hash="0" * 64)
    result = evaluate_landing_eligibility(
        invalid, acceptance_for(package, source_b), current_source=source_b.to_dict(),
        policy=LandingPolicy((LandingStrategy.SQUASH,), ()))
    assert LandingRefusalCode.EVIDENCE_INVALID.value in result.codes
    (repo / ARTIFACT).write_text("def thing():\n    return 'C'\n")
    stale = take_source_snapshot(str(repo), base_sha="HEAD",
                                 relevant_paths=[ARTIFACT, VERIFIER])
    result = evaluate_landing_eligibility(
        package, acceptance_for(package, source_b), current_source=stale.to_dict(),
        policy=LandingPolicy((LandingStrategy.SQUASH,), ()), strategy=LandingStrategy.SQUASH)
    assert LandingRefusalCode.EVIDENCE_STALE.value in result.codes


def test_governance_receipt_must_bind_candidate(repo):
    package, source_a, source_b, _, _ = make_real_run(repo)
    wrong = make_verification_receipt(
        receipt_id="g-1", run_id="r-1", packet_id="P-1", attempt=1,
        execution_package_hash=package["execution_package"]["package_hash"],
        verifier_id="negative_control", normalized_command="governance-check",
        source_snapshot_digest=source_a.snapshot_digest, exit_code=0,
        ended_at="2026-09-16T00:00:04+00:00", control_id="negative_control",
        control_passed=True)
    result = evaluate_landing_eligibility(
        package, acceptance_for(package, source_b), current_source=source_b.to_dict(),
        governance=(wrong,), policy=LandingPolicy((LandingStrategy.SQUASH,), ("negative_control",)),
        strategy=LandingStrategy.SQUASH)
    assert LandingRefusalCode.GOVERNANCE_NOT_GREEN.value in result.codes


class Repo(RepositoryLandingAdapter):
    def __init__(self, source, landed): self.source, self.landed, self.calls = source, landed, 0
    def current_source(self): return self.source
    def land(self, strategy): self.calls += 1; return self.landed


class Jira(JiraReconciliationAdapter):
    def __init__(self, fail=False): self.fail = fail
    def reconcile(self, receipt):
        if self.fail: raise RuntimeError("jira unavailable")
        return {"ok": True, "issue": "PS-578", "transition": "bookkeeping"}


def test_receipt_is_complete_revalidatable_and_preserves_adapter_result(repo):
    package, _, source_b, _, _ = make_real_run(repo)
    acceptance = acceptance_for(package, source_b)
    landed = {"head_sha": "squashed", "diff_digest": source_b.tracked_diff_digest,
              "repository": "repo", "destination_branch": "main",
              "pr_number": 12, "pr_url": "https://example.test/pr/12", "merge_result": "merged"}
    receipt = land_exact_candidate(
        evidence_package=package, acceptance=acceptance,
        repository=Repo(source_b.to_dict(), landed), jira=Jira(),
        current_source=source_b.to_dict(), governance=(),
        policy=LandingPolicy((LandingStrategy.SQUASH,), ()), strategy=LandingStrategy.SQUASH)
    payload = receipt.to_dict()
    assert landing_receipt_hash_is_valid(payload)
    assert payload["evidence_package_id"] == "ev-1"
    assert payload["semantic_acceptance_id"] == "accept-1"
    assert payload["landed_result"]["pr_number"] == 12
    assert payload["deployment_implication"] == "none"
    payload["landed_head"] = "tampered"
    assert not landing_receipt_hash_is_valid(payload)


def test_jira_failure_keeps_truthful_partial_receipt(repo):
    package, _, source_b, _, _ = make_real_run(repo)
    receipt = land_exact_candidate(
        evidence_package=package, acceptance=acceptance_for(package, source_b),
        repository=Repo(source_b.to_dict(), {"head_sha": "squashed",
                  "diff_digest": source_b.tracked_diff_digest}), jira=Jira(fail=True),
        current_source=source_b.to_dict(), governance=(),
        policy=LandingPolicy((LandingStrategy.SQUASH,), ()), strategy=LandingStrategy.SQUASH)
    assert receipt.reconciliation_state == "REPOSITORY_LANDED_JIRA_FAILED"
    assert receipt.reconciliation_error_code == "JIRA_RECONCILIATION_FAILED"
    assert receipt.landed_head == "squashed"
    assert receipt.deployment_implication == "none"


def test_equivalence_and_strategy_controls(repo):
    package, _, source_b, _, _ = make_real_run(repo)
    acceptance = acceptance_for(package, source_b)
    assert prove_landed_equivalence(acceptance,
        {"head_sha": "squashed", "diff_digest": source_b.tracked_diff_digest},
        LandingStrategy.SQUASH).equivalent
    assert not prove_landed_equivalence(acceptance,
        {"head_sha": "squashed", "diff_digest": "extra"},
        LandingStrategy.SQUASH).equivalent
    result = evaluate_landing_eligibility(
        package, acceptance, current_source=source_b.to_dict(),
        policy=LandingPolicy((LandingStrategy.FAST_FORWARD,), ()), strategy=LandingStrategy.SQUASH)
    assert LandingRefusalCode.LANDING_STRATEGY_NOT_ALLOWED.value in result.codes
