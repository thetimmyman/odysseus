"""Synthetic controls for pure immutable outcome records."""
from __future__ import annotations

import hashlib
import json
import subprocess

import pytest

from src.attempt_receipt import ArtifactRef, make_attempt_receipt, make_verification_receipt
from src.evidence_contract import INDEPENDENCE_HARNESS_HIDDEN
from src.evidence_package import seal_evidence_package
from src.execution_outcomes import (
    OutcomeError, build_outcome_record, validate_outcome_record,
)
from src.execution_package import (
    DECIDED_BY_EXPLICIT_PIN, VerificationPlan, build_execution_package,
    make_dispatch_receipt, seal_verifier_digests,
)
from src.mechanical_landing import (
    LandingReceipt, LandingStrategy,
    make_semantic_acceptance, prove_landed_equivalence,
)
from src.source_snapshot import take_source_snapshot
from src.worker_context import render_worker_context

ARTIFACT = "src/synthetic.py"
VERIFIER = "tests/test_synthetic.py"
PACKET = {
    "packet_id": "packet-synthetic", "objective": "preserve a deterministic fixture",
    "contract": "def sample() -> int", "role": "local_implementer",
    "write_scope": [ARTIFACT],
    "interface": [{"name": "sample", "required": True, "type_hint": "callable",
                   "semantics": "synthetic interface"}],
    "test_command": "python -m pytest tests/test_synthetic.py -q",
    "acceptance_criteria": ["the fixture is deterministic"],
    "negative_control": "the changed output fails the control",
    "stop_conditions": ["fixture is ambiguous"],
}


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True)


def _ref(label, body):
    return ArtifactRef(artifact_id=label, sha256=hashlib.sha256(body).hexdigest(),
                       size=len(body), storage_uri=f"mem://{label}").to_dict()


def make_evidence(root, run_id="run-synthetic", *, exit_code=0, prompt_tokens=42,
                  selected_target="synthetic-target", selected_model="synthetic-model",
                  failure_class=None):
    root.mkdir()
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / ARTIFACT).write_text("def sample():\n    return 1\n")
    (root / VERIFIER).write_text("def test_sample():\n    assert True\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "synthetic@example.invalid")
    _git(root, "config", "user.name", "Synthetic")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "synthetic base")
    source_a = take_source_snapshot(str(root), base_sha="HEAD", relevant_paths=[ARTIFACT, VERIFIER])
    plan = VerificationPlan(verifier_id=VERIFIER,
                            command="python -m pytest tests/test_synthetic.py -q",
                            verifier_paths=(VERIFIER,),
                            verifier_digests=seal_verifier_digests(str(root), [VERIFIER]),
                            positive_control="synthetic passes", negative_control="synthetic fails")
    package = build_execution_package(PACKET, source=source_a, verification=plan,
                                      run_id=run_id, ticket_key="SYNTHETIC",
                                      allowed_tools=("write_file",))
    dispatch = make_dispatch_receipt(
        receipt_id=f"dispatch-{run_id}", execution_package_hash=package.package_hash,
        run_id=run_id, packet_id=PACKET["packet_id"], selected_target_id=selected_target,
        selected_host="synthetic-host", selected_model=selected_model,
        selected_runtime_kind="synthetic-runtime", selected_runtime_version="1",
        selected_model_digest="a" * 64, selected_backend="synthetic-backend",
        requested_role="local_implementer", decided_by=DECIDED_BY_EXPLICIT_PIN,
        reason="synthetic explicit pin", granted_tools=("write_file",),
        granted_write_scope=(ARTIFACT,), decided_at="2026-10-04T00:00:00+00:00")
    context = render_worker_context(PACKET, max_chars=4000).encode()
    output = b"synthetic output"
    stdout = b"1 passed\n"
    context_ref, output_ref, stdout_ref = (_ref("context", context), _ref("output", output),
                                           _ref("stdout", stdout))
    attempt = make_attempt_receipt(
        receipt_id=f"attempt-{run_id}", run_id=run_id, packet_id=PACKET["packet_id"],
        attempt=1, execution_package_hash=package.package_hash,
        dispatch_receipt_hash=dispatch.receipt_hash, target_id=selected_target,
        host="synthetic-host", model=selected_model,
        context_projection_hash=hashlib.sha256(context).hexdigest(),
        rendered_context_ref=ArtifactRef(**context_ref), output_ref=ArtifactRef(**output_ref),
        declared_write_set=(ARTIFACT,), actual_write_set=(ARTIFACT,), elapsed_s=12.5,
        prompt_tokens=prompt_tokens, completion_tokens=9, failure_class=failure_class,
        finish_reason="stop")
    (root / ARTIFACT).write_text("def sample():\n    return 2\n")
    source_b = take_source_snapshot(str(root), base_sha="HEAD", relevant_paths=[ARTIFACT, VERIFIER])
    verification = make_verification_receipt(
        receipt_id=f"verify-{run_id}", run_id=run_id, packet_id=PACKET["packet_id"],
        attempt=1, execution_package_hash=package.package_hash, verifier_id=VERIFIER,
        normalized_command="python -m pytest tests/test_synthetic.py -q",
        source_snapshot_digest=source_b.snapshot_digest, exit_code=exit_code,
        verifier_digest=plan.digest_of(VERIFIER), verifier_paths=(VERIFIER,),
        ended_at="2026-10-04T00:00:01+00:00", stdout_ref=ArtifactRef(**stdout_ref),
        tests_collected=1, tests_executed=1, tests_passed=1 if exit_code == 0 else 0,
        tests_failed=0 if exit_code == 0 else 1,
        requirement_ids=("deterministic_verification", "source_binding",
                         "negative_control", "scope_check"),
        proof_class=INDEPENDENCE_HARNESS_HIDDEN, control_id="negative_control",
        control_expected="FAIL", control_observed="FAIL", control_passed=True)
    evidence = seal_evidence_package(evidence_package_id=f"evidence-{run_id}",
                                     execution_package=package,
                                     dispatch_receipts=(dispatch,),
                                     attempt_receipts=(attempt,),
                                     verification_receipts=(verification,)).to_dict()
    extensions = {f"mem://{key}": value for key, value in
                  (("context", context), ("output", output), ("stdout", stdout))}
    return evidence, extensions, source_b, dispatch, attempt, verification


def execution_key(evidence):
    core = {"package_hash": evidence["execution_package"]["package_hash"],
            "run_id": evidence["execution_package"]["run_id"],
            "packet_id": evidence["execution_package"]["packet_id"],
            "dispatch_receipt_hashes": sorted(row["receipt_hash"] for row in evidence["dispatch_receipts"])}
    return hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def fact(value, ref, key):
    return {"status": "OBSERVED", "value": value, "source_ref": ref,
            "source_sha256": ref.split(":", 1)[1], "execution_key": key}


def make_facts(evidence, *, prompt_zero=False):
    key = execution_key(evidence)
    attempt_hash = evidence["attempt_receipts"][0]["receipt_hash"]
    metrics = {"ttft_s": [{"status": "UNKNOWN", "reason": "no canonical TTFT receipt",
                            "attempt": 1, "execution_key": key}],
               "realized_cost": [{"status": "ABSENT", "reason": "no actual charge receipt",
                                  "attempt": 1, "execution_key": key}]}
    if prompt_zero:
        metrics["prompt_tokens"] = [{"status": "OBSERVED", "value": 0, "unit": "tokens",
                                      "attempt": 1, "source_ref": f"attempt:{attempt_hash}",
                                      "source_sha256": attempt_hash, "execution_key": key}]
    return {
        "task_class": {"status": "UNKNOWN", "reason": "no canonical task-class receipt",
                       "execution_key": key},
        "risk_class": {"status": "UNKNOWN", "reason": "no canonical risk-class receipt",
                       "execution_key": key},
        "harness": {"status": "UNKNOWN", "reason": "no canonical harness receipt",
                    "execution_key": key},
        "metrics": metrics,
        "fault_events": [],
        "interventions": [],
    }


def make_acceptance(evidence, source, *, disposition="ACCEPTED"):
    return make_semantic_acceptance(
        acceptance_id="synthetic-acceptance", reviewer_id="synthetic-reviewer",
        evidence_package_hash=evidence["evidence_package_hash"],
        candidate_source_digest=source.snapshot_digest, candidate_head_sha=source.head_sha,
        candidate_tree_sha="tree-synthetic", candidate_diff_digest=source.tracked_diff_digest,
        disposition=disposition,
        observed_at="2026-10-04T00:00:02+00:00", accepted_at="2026-10-04T00:00:03+00:00")


def make_landing(evidence, acceptance):
    landed = {"head_sha": acceptance.candidate_head_sha, "tree_sha": "tree-synthetic",
              "diff_digest": acceptance.candidate_diff_digest,
              "repository": "synthetic-repository", "destination_branch": "synthetic"}
    strategy = LandingStrategy.SQUASH
    equivalence = prove_landed_equivalence(acceptance, landed, strategy)
    provisional = LandingReceipt(
        evidence_package_id=evidence["evidence_package_id"],
        evidence_package_hash=evidence["evidence_package_hash"],
        semantic_acceptance_id=acceptance.acceptance_id,
        semantic_acceptance_hash=acceptance.acceptance_hash,
        accepted_candidate={"source_digest": acceptance.candidate_source_digest,
                            "head_sha": acceptance.candidate_head_sha,
                            "tree_sha": acceptance.candidate_tree_sha,
                            "diff_digest": acceptance.candidate_diff_digest},
        landing_strategy=strategy.value, pre_land_governance=(), landed_result=landed,
        landed_head=landed["head_sha"], landed_tree_sha=landed["tree_sha"],
        equivalence=equivalence, repository=landed["repository"],
        destination_branch=landed["destination_branch"], tracker_result={},
        reconciliation_state="REPOSITORY_LANDED_TRACKER_PENDING", created_at="2026-10-04T00:00:04+00:00")
    core = provisional.core()
    digest = hashlib.sha256(json.dumps(core, sort_keys=True, separators=(",", ":"),
                                      ensure_ascii=False, default=str).encode()).hexdigest()
    return LandingReceipt(**{**provisional.__dict__, "receipt_hash": digest}).to_dict()


def test_positive_record_is_immutable_revalidated_and_delivery_absent(tmp_path, monkeypatch):
    evidence, extensions, source, _, _, _ = make_evidence(tmp_path / "repo", prompt_tokens=0)
    acceptance = make_acceptance(evidence, source)
    landing = make_landing(evidence, acceptance)
    facts = make_facts(evidence, prompt_zero=True)
    import src.evidence_package as evidence_module
    monkeypatch.setattr(evidence_module, "default_artifact_loader",
                        lambda *_: pytest.fail("default file loader must not run"))
    record = build_outcome_record(evidence_package=evidence, companion_facts=facts,
                                  semantic_acceptance=acceptance.to_dict(),
                                  landing_receipt=landing,
                                  candidate_source_snapshot=source.to_dict(),
                                  artifact_extensions=extensions)
    original = record.to_bytes()
    evidence["execution_package"]["objective"] = "mutated after build"
    view = record.to_dict()
    view["raw_evidence_package"]["execution_package"]["objective"] = "detached mutation"
    checked = validate_outcome_record(record)
    assert checked.to_bytes() == original
    result = checked.to_dict()
    assert result["gates"]["verified"] == {"status": "OBSERVED", "value": True}
    assert result["gates"]["semantic_acceptance"]["value"] == "ACCEPTED"
    assert result["gates"]["repository_landed"]["value"] is True
    assert result["gates"]["delivery"]["status"] == "ABSENT"
    assert result["companion_facts"]["metrics"]["prompt_tokens"][0]["value"] == 0


def test_missing_artifact_bytes_refuse_without_file_fallback(tmp_path):
    evidence, _, _, _, _, _ = make_evidence(tmp_path / "repo")
    with pytest.raises(OutcomeError, match="caller must provide bytes"):
        build_outcome_record(evidence_package=evidence)


def test_malformed_fact_status_is_a_bounded_refusal(tmp_path):
    evidence, extensions, _, _, _, _ = make_evidence(tmp_path / "repo")
    facts = make_facts(evidence)
    facts["task_class"]["status"] = []
    with pytest.raises(OutcomeError, match="status must be OBSERVED"):
        build_outcome_record(evidence_package=evidence, companion_facts=facts,
                             artifact_extensions=extensions)


def test_observed_fact_must_match_a_supported_canonical_source(tmp_path):
    evidence, extensions, _, _, _, _ = make_evidence(tmp_path / "repo")
    facts = make_facts(evidence, prompt_zero=True)
    with pytest.raises(OutcomeError, match="contradicts its canonical receipt"):
        build_outcome_record(evidence_package=evidence, companion_facts=facts,
                             artifact_extensions=extensions)

    facts = make_facts(evidence)
    facts["metrics"]["prompt_tokens"] = [{
        "status": "UNKNOWN", "reason": "hide canonical prompt count", "attempt": 1,
        "execution_key": execution_key(evidence)}]
    with pytest.raises(OutcomeError, match="cannot hide a populated canonical"):
        build_outcome_record(evidence_package=evidence, companion_facts=facts,
                             artifact_extensions=extensions)

    facts = make_facts(evidence)
    key = execution_key(evidence)
    attempt_hash = evidence["attempt_receipts"][0]["receipt_hash"]
    facts["metrics"]["realized_cost"] = [{
        "status": "OBSERVED", "value": 99, "currency": "USD", "attempt": 1,
        "source_ref": f"attempt:{attempt_hash}", "source_sha256": attempt_hash,
        "execution_key": key}]
    with pytest.raises(OutcomeError, match="no canonical actual-charge source"):
        build_outcome_record(evidence_package=evidence, companion_facts=facts,
                             artifact_extensions=extensions)

    facts = make_facts(evidence)
    facts["task_class"] = fact("unsupported-class", f"package:{evidence['execution_package']['package_hash']}", key)
    with pytest.raises(OutcomeError, match="no canonical source field"):
        build_outcome_record(evidence_package=evidence, companion_facts=facts,
                             artifact_extensions=extensions)


def test_fault_attribution_cannot_override_canonical_provider_failure(tmp_path):
    evidence, extensions, _, _, _, _ = make_evidence(
        tmp_path / "repo", failure_class="runtime_provider")
    facts = make_facts(evidence)
    key = execution_key(evidence)
    attempt_hash = evidence["attempt_receipts"][0]["receipt_hash"]
    facts["fault_events"] = [{
        "status": "OBSERVED", "value": "task_quality", "attempt": 1,
        "source_ref": f"attempt:{attempt_hash}", "source_sha256": attempt_hash,
        "execution_key": key}]
    with pytest.raises(OutcomeError, match="contradicts its canonical attempt"):
        build_outcome_record(evidence_package=evidence, companion_facts=facts,
                             artifact_extensions=extensions)

    record = build_outcome_record(evidence_package=evidence, companion_facts=make_facts(evidence),
                                  artifact_extensions=extensions)
    faults = record.to_dict()["companion_facts"]["fault_events"]
    assert any(row.get("value") == "runtime_provider" for row in faults)


def test_metric_unit_and_unknown_coverage_are_preserved(tmp_path):
    evidence, extensions, _, _, _, _ = make_evidence(tmp_path / "repo")
    facts = make_facts(evidence)
    key = execution_key(evidence)
    attempt_hash = evidence["attempt_receipts"][0]["receipt_hash"]
    facts["metrics"]["elapsed_s"] = [{
        "status": "OBSERVED", "value": 100, "unit": "milliseconds", "attempt": 1,
        "source_ref": f"attempt:{attempt_hash}", "source_sha256": attempt_hash,
        "execution_key": key}]
    with pytest.raises(OutcomeError, match="unit must be seconds"):
        build_outcome_record(evidence_package=evidence, companion_facts=facts,
                             artifact_extensions=extensions)


def test_inconclusive_verification_remains_unknown(tmp_path):
    evidence, extensions, _, _, _, verification = make_evidence(tmp_path / "repo")
    payload = verification.to_dict()
    payload.pop("receipt_hash")
    payload.pop("outcome")
    payload["stdout_complete"] = False
    payload["stdout_ref"] = ArtifactRef(**payload["stdout_ref"])
    incomplete = make_verification_receipt(**payload)
    from src.evidence_package import compute_evidence_package_hash
    evidence["verification_receipts"] = [incomplete.to_dict()]
    evidence["evidence_package_hash"] = compute_evidence_package_hash(
        {key: value for key, value in evidence.items() if key != "evidence_package_hash"})
    record = build_outcome_record(evidence_package=evidence,
                                  companion_facts=make_facts(evidence),
                                  artifact_extensions=extensions)
    assert record.to_dict()["gates"]["verified"]["status"] == "UNKNOWN"
    from src.outcome_scorecard import aggregate_outcomes
    arm = aggregate_outcomes([record], scoring_version="inconclusive-control")["arms"][0]
    assert arm["gate_coverage"]["verified"]["unknown_n"] == 1
    assert arm["metrics"]["ttft_s"]["unknown_n"] == 1
    assert arm["metrics"]["realized_cost"]["absent_n"] == 1


def test_semantic_acceptance_requires_matching_verified_candidate_snapshot(tmp_path):
    evidence, extensions, source, *_ = make_evidence(tmp_path / "repo")
    acceptance = make_acceptance(evidence, source)
    without_snapshot = build_outcome_record(
        evidence_package=evidence, companion_facts=make_facts(evidence),
        semantic_acceptance=acceptance.to_dict(), artifact_extensions=extensions)
    assert without_snapshot.to_dict()["gates"]["semantic_acceptance"]["status"] == "UNKNOWN"

    changed = make_semantic_acceptance(
        acceptance_id="synthetic-acceptance", reviewer_id="synthetic-reviewer",
        evidence_package_hash=evidence["evidence_package_hash"],
        candidate_source_digest=source.snapshot_digest, candidate_head_sha=source.head_sha,
        candidate_tree_sha="tree-synthetic", candidate_diff_digest="f" * 64,
        observed_at="2026-10-04T00:00:02+00:00", accepted_at="2026-10-04T00:00:03+00:00")
    with pytest.raises(OutcomeError, match="does not match the verified candidate"):
        build_outcome_record(evidence_package=evidence, companion_facts=make_facts(evidence),
                             semantic_acceptance=changed.to_dict(),
                             candidate_source_snapshot=source.to_dict(),
                             artifact_extensions=extensions)


@pytest.mark.parametrize("disposition", ["REJECTED", "REWORK", "BLOCKED"])
def test_late_semantic_disposition_does_not_claim_first_pass_acceptance(tmp_path, disposition):
    evidence, extensions, source, _, first_attempt, first_verification = make_evidence(
        tmp_path / "repo")
    second_attempt_fields = dict(first_attempt.__dict__)
    second_attempt_fields.pop("receipt_hash")
    second_attempt_fields.update(receipt_id="attempt-two", attempt=2, repair_of=1)
    second_attempt = make_attempt_receipt(**second_attempt_fields)
    second_verification_fields = dict(first_verification.__dict__)
    second_verification_fields.pop("receipt_hash")
    second_verification_fields.update(receipt_id="verify-two", attempt=2)
    second_verification = make_verification_receipt(**second_verification_fields)
    evidence["attempt_receipts"].append(second_attempt.to_dict())
    evidence["verification_receipts"].append(second_verification.to_dict())
    from src.evidence_package import compute_evidence_package_hash
    evidence["evidence_package_hash"] = compute_evidence_package_hash(
        {key: value for key, value in evidence.items() if key != "evidence_package_hash"})
    acceptance = make_acceptance(evidence, source, disposition=disposition)
    facts = make_facts(evidence)
    record = build_outcome_record(evidence_package=evidence, companion_facts=facts,
                                  semantic_acceptance=acceptance.to_dict(),
                                  candidate_source_snapshot=source.to_dict(),
                                  artifact_extensions=extensions)
    assert record.to_dict()["gates"]["semantic_acceptance"]["value"] == disposition
    assert record.to_dict()["first_pass"] == {"verified": "TRUE", "accepted": "UNKNOWN"}
    from src.outcome_scorecard import aggregate_outcomes
    view = aggregate_outcomes([record], scoring_version="first-pass-control")
    assert view["arms"][0]["first_pass_accepted"]["unknown_n"] == 1


def test_duplicate_dispatch_identity_refuses_before_aggregate(tmp_path):
    evidence, extensions, _, _, _, _ = make_evidence(tmp_path / "repo")
    original = build_outcome_record(evidence_package=evidence,
                                   companion_facts=make_facts(evidence),
                                   artifact_extensions=extensions)
    from src.outcome_scorecard import aggregate_outcomes
    assert aggregate_outcomes([original, original], scoring_version="duplicate-control")["arms"][0][
        "distinct_execution_n"] == 1
    from src.evidence_package import compute_evidence_package_hash
    duplicated = json.loads(json.dumps(evidence))
    duplicated["dispatch_receipts"] *= 2
    duplicated["evidence_package_hash"] = compute_evidence_package_hash(
        {key: value for key, value in duplicated.items() if key != "evidence_package_hash"})
    with pytest.raises(OutcomeError, match="duplicate dispatch receipt"):
        build_outcome_record(evidence_package=duplicated, artifact_extensions=extensions)


def _build_offer_record(root, *, run_id, tariff_version, harness="command-code",
                        usage_path="subscription-cli"):
    from src.provider_capacity import (
        AuthorizationClass, CapacityState, Entitlement, EvidenceProvenance,
        make_capacity_receipt,
    )
    from src.provider_model_offer import (
        OfferProvenance, make_provider_model_offer_receipt,
    )

    evidence, extensions, _, dispatch, attempt, _ = make_evidence(root, run_id=run_id)
    observed_at = "2026-10-04T00:00:00+00:00"
    provenance = EvidenceProvenance("synthetic", "synthetic-ref", "test-collector",
                                    observed_at, 3600)
    capacity = make_capacity_receipt(
        provider="synthetic-provider", pool_id="synthetic-pool",
        account_identity="synthetic-account", authorization_class=AuthorizationClass.API_KEY,
        entitlement=Entitlement.API, exposed_models=("synthetic-model",),
        observed_at=observed_at, ttl_seconds=3600, collector_id="test-collector",
        evidence_source="synthetic", evidence_reference="synthetic-ref",
        state=CapacityState.AVAILABLE, state_provenance=provenance,
        entitlement_provenance=provenance, zdr_provenance=provenance)
    offer = make_provider_model_offer_receipt(
        provider=capacity.provider, pool_id=capacity.pool_id,
        capacity_receipt_ref=capacity.ref, harness=harness,
        usage_path=usage_path, native_model="synthetic-model",
        tariff_id="synthetic-tariff", tariff_version=tariff_version,
        provenance=OfferProvenance("https://synthetic.example/pricing", "a" * 64,
                                   "synthetic-fixture", observed_at, 3600))
    dispatch_fields = dict(dispatch.__dict__)
    dispatch_fields.pop("receipt_hash")
    dispatch_fields.update(capacity_receipt_refs=(capacity.ref,),
                           offer_receipt_refs=(offer.ref,))
    selected = make_dispatch_receipt(**dispatch_fields)
    attempt_fields = dict(attempt.__dict__)
    attempt_fields.pop("receipt_hash")
    attempt_fields["dispatch_receipt_hash"] = selected.receipt_hash
    selected_attempt = make_attempt_receipt(**attempt_fields)
    evidence["dispatch_receipts"] = [selected.to_dict()]
    evidence["attempt_receipts"] = [selected_attempt.to_dict()]
    from src.evidence_package import compute_evidence_package_hash
    evidence["evidence_package_hash"] = compute_evidence_package_hash(
        {key: value for key, value in evidence.items() if key != "evidence_package_hash"})
    key = execution_key(evidence)
    facts = make_facts(evidence)
    facts["provider"] = fact(capacity.provider, f"capacity:{capacity.receipt_hash}", key)
    facts["pool"] = fact(capacity.pool_id, f"capacity:{capacity.receipt_hash}", key)
    facts["account"] = fact(capacity.account_identity, f"capacity:{capacity.receipt_hash}", key)
    record = build_outcome_record(evidence_package=evidence, companion_facts=facts,
                                  capacity_receipt=capacity.to_dict(),
                                  offer_receipt=offer.to_dict(), artifact_extensions=extensions)
    return record, capacity, offer, evidence, facts, key, extensions


def test_offer_does_not_establish_installed_harness_version_or_config(tmp_path):
    record, capacity, offer, evidence, facts, key, extensions = _build_offer_record(
        tmp_path / "repo", run_id="offer-run", tariff_version="tariff-version-99")
    arm = record.to_dict()["arm"]
    assert arm["harness"]["status"] == "UNKNOWN"
    assert arm["offer_harness"]["identity"] == "command-code"
    assert arm["offer_harness"]["usage_path"] == "subscription-cli"
    assert arm["offer_harness"]["installed_version"]["status"] == "UNKNOWN"
    assert arm["offer_harness"]["config_sha256"]["status"] == "UNKNOWN"

    fabricated = make_facts(evidence)
    fabricated["provider"] = facts["provider"]
    fabricated["pool"] = facts["pool"]
    fabricated["account"] = facts["account"]
    fabricated["harness"] = fact({"identity": "command-code", "version": "invented-version-99",
                                   "config_sha256": "f" * 64, "usage_path": "subscription-cli"},
                                  f"offer:{offer.receipt_hash}", key)
    with pytest.raises(OutcomeError, match="no canonical source field"):
        build_outcome_record(evidence_package=evidence, companion_facts=fabricated,
                             capacity_receipt=capacity.to_dict(), offer_receipt=offer.to_dict(),
                             artifact_extensions=extensions)


def test_tariff_version_is_not_a_material_arm_dimension(tmp_path):
    first, _, first_offer, *_ = _build_offer_record(
        tmp_path / "repo-one", run_id="tariff-run-one", tariff_version="tariff-version-1")
    second, _, second_offer, *_ = _build_offer_record(
        tmp_path / "repo-two", run_id="tariff-run-two", tariff_version="tariff-version-2")
    assert first_offer.receipt_hash != second_offer.receipt_hash
    from src.outcome_scorecard import aggregate_outcomes
    view = aggregate_outcomes([first, second], scoring_version="tariff-grouping-control")
    assert len(view["arms"]) == 1
    assert view["arms"][0]["distinct_execution_n"] == 2
    assert view["source_record_hashes"] == sorted([first.raw_record_hash, second.raw_record_hash])
    assert first.to_dict()["offer_receipt"]["receipt_hash"] == first_offer.receipt_hash
    assert second.to_dict()["offer_receipt"]["receipt_hash"] == second_offer.receipt_hash
    different_harness, *_ = _build_offer_record(
        tmp_path / "repo-three", run_id="tariff-run-three", tariff_version="tariff-version-3",
        harness="other-harness")
    different_path, *_ = _build_offer_record(
        tmp_path / "repo-four", run_id="tariff-run-four", tariff_version="tariff-version-4",
        usage_path="other-usage-path")
    separated = aggregate_outcomes([first, different_harness, different_path],
                                   scoring_version="harness-separation-control")
    assert len(separated["arms"]) == 3
    assert all(row["distinct_execution_n"] == 1 for row in separated["arms"])


def test_dispatch_hash_and_packet_linkage_tampering_refuse(tmp_path):
    evidence, extensions, _, _, _, _ = make_evidence(tmp_path / "repo")
    changed = json.loads(json.dumps(evidence))
    changed["dispatch_receipts"][0]["selected_model"] = "tampered"
    from src.evidence_package import compute_evidence_package_hash
    changed["evidence_package_hash"] = compute_evidence_package_hash(
        {k: v for k, v in changed.items() if k != "evidence_package_hash"})
    with pytest.raises(OutcomeError, match="dispatch receipt hash mismatch"):
        build_outcome_record(evidence_package=changed, artifact_extensions=extensions)


def test_rework_is_not_acceptance_and_repair_history_survives(tmp_path):
    evidence, extensions, source, *_ = make_evidence(tmp_path / "repo")
    acceptance = make_acceptance(evidence, source)
    rejected = dict(acceptance.to_dict(), disposition="REWORK")
    core = {k: v for k, v in rejected.items() if k != "acceptance_hash"}
    rejected["acceptance_hash"] = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()).hexdigest()
    record = build_outcome_record(evidence_package=evidence, companion_facts=make_facts(evidence),
                                  semantic_acceptance=rejected,
                                  candidate_source_snapshot=source.to_dict(),
                                  artifact_extensions=extensions)
    assert record.to_dict()["gates"]["semantic_acceptance"]["value"] == "REWORK"
    assert record.to_dict()["gates"]["delivery"]["status"] == "ABSENT"
