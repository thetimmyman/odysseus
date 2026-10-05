"""Writable-run -> retained receipt chain -> independent landing consumption."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta

import pytest

from src.attempt_receipt import artifact_ref, make_attempt_receipt, make_verification_receipt
from src.evidence_io import project_evidence_summary, read_evidence_bundle, retain_evidence_bundle
from src.evidence_package import compute_evidence_package_hash, validate_evidence_package
from src.local_worker_loop import ACCEPTED_CANDIDATE, VerificationExecution, run_local_worker_loop
from src.mechanical_landing import (
    LandingPolicy, LandingStrategy, evaluate_landing_eligibility, land_exact_candidate,
    landing_receipt_hash_is_valid, make_semantic_acceptance,
)
from src.source_snapshot import _finalize
from scripts.verify_execution_chain import run_canary
import json
from tests.test_local_worker_loop import (
    NOW, _bound, _facts, _invocation, _package, _verification, _worker,
)


def _candidate(package, revision):
    core = package.source.core()
    core["unstaged_paths"] = ["src/thing.py"]
    core["tracked_diff_digest"] = str(revision) * 64
    core["relevant_digests"] = [("src/thing.py", str(revision) * 64)]
    return _finalize(core).to_dict()


def _chain(*, retry=False):
    package = _package()
    bound = _bound(package)
    state = {"source": package.source.to_dict()}

    def worker(dispatch, context, number, budget):
        state["source"] = _candidate(package, number)
        result = _worker(dispatch, package, context, number)
        receipt = make_attempt_receipt(**{
            **result.receipt.__dict__, "model_version": dispatch.decision.selected_profile.model_digest,
            "started_at": NOW.isoformat(), "ended_at": (NOW + timedelta(seconds=1)).isoformat(),
            "generation_token": f"generation-{number}"})
        return replace(result, receipt=receipt)

    def verifier(dispatch, attempt, number):
        result = _verification(dispatch, package, attempt, number,
                               exit_code=1 if retry and number == 1 else 0,
                               failure="FAILED tests/test_thing.py::test_output - AssertionError: wrong output\n"
                               if retry and number == 1 else "")
        stderr = artifact_ref(f"stderr-{number}", b"", storage_uri=f"memory://stderr/{number}")
        receipt = make_verification_receipt(**{
            **result.receipt.__dict__, "source_snapshot_digest": state["source"]["snapshot_digest"],
            "stderr_ref": stderr, "started_at": NOW.isoformat(),
            "ended_at": (NOW + timedelta(seconds=1)).isoformat(), "proof_vantage": "harness worktree",
            "control_id": "", "control_passed": None,
            "requirement_ids": tuple(r.requirement_id for r in package.evidence_requirements
                                     if r.requirement_id != "negative_control")})
        controls = []
        if receipt.outcome == "PASS":
            for control, expected in (("positive_control", "PASS"), ("negative_control", "FAIL")):
                controls.append(make_verification_receipt(**{
                    **receipt.__dict__, "receipt_id": f"{control}-{number}", "control_id": control,
                    "control_passed": True, "control_expected": expected, "control_observed": expected,
                    "requirement_ids": ("negative_control",) if control == "negative_control" else ()}))
        return VerificationExecution(receipt, {**result.artifacts, stderr.storage_uri: b""}, tuple(controls))

    result = run_local_worker_loop(
        execution_package=package, standing_dispatch=bound,
        current_facts=lambda d, n: _facts(d, package), invocation=_invocation,
        execute_worker=worker, execute_verifier=verifier,
        current_source=lambda: deepcopy(state["source"]))
    return result, state["source"], bound


def _reseal(payload):
    payload.pop("evidence_package_hash", None)
    payload["evidence_package_hash"] = compute_evidence_package_hash(payload)
    return payload


def _facts_for(bound):
    return {"current_profiles": {bound.decision.selected_profile.profile_id:
                                 bound.decision.selected_profile.to_dict()},
            "current_verifier_digests": {"tests/test_thing.py": "a" * 64},
            "current_policy_ref": bound.policy.policy_ref,
            "now": NOW + timedelta(seconds=2)}


def _acceptance(result, source):
    return make_semantic_acceptance(
        acceptance_id="review-1", reviewer_id="independent-reviewer",
        evidence_package_hash=result.evidence_package["evidence_package_hash"],
        candidate_source_digest=source["snapshot_digest"], candidate_head_sha=source["head_sha"],
        candidate_tree_sha="candidate-tree", candidate_diff_digest=source["tracked_diff_digest"],
        observed_at=NOW.isoformat(), accepted_at=NOW.isoformat())


def test_writable_retry_chain_retains_red_and_lands_exact_output_after_reload(tmp_path):
    result, source, bound = _chain(retry=True)
    assert result.status == ACCEPTED_CANDIDATE, result.validation.explain()
    assert result.evidence_package["schema_version"] == 2
    assert [r.outcome for r in result.verifications] == ["FAIL", "PASS", "PASS", "PASS"]
    path = retain_evidence_bundle(tmp_path, result.evidence_package, artifacts=result.artifacts)
    payload, artifacts = read_evidence_bundle(path)
    assert payload == result.evidence_package
    summary = project_evidence_summary(payload, artifacts=artifacts)
    assert "State: VERIFIED" in summary and "FAIL" in summary
    assert project_evidence_summary(payload, artifacts=artifacts) == summary
    from src.execution_outcomes import build_outcome_record, validate_outcome_record
    outcome = validate_outcome_record(build_outcome_record(
        evidence_package=payload, candidate_source_snapshot=source,
        artifact_extensions=artifacts)).to_dict()
    assert outcome["gates"]["verified"]["value"] is True
    assert [row["verification"] for row in outcome["attempt_outcomes"]] == ["FAIL", "PASS"]
    calls = []

    class Repository:
        def current_source(self):
            return source

        def land(self, strategy):
            calls.append(strategy)
            return {"head_sha": "squashed-sha", "tree_sha": "candidate-tree",
                    "diff_digest": source["tracked_diff_digest"], "repository": "synthetic",
                    "destination_branch": "dev"}

    receipt = land_exact_candidate(
        evidence_package=payload, acceptance=_acceptance(result, source), repository=Repository(),
        tracker=None, current_source=source, governance=(),
        policy=LandingPolicy(allowed_strategies=(LandingStrategy.SQUASH,)),
        strategy=LandingStrategy.SQUASH, artifact_extensions=artifacts,
        acceptance_facts=lambda: _facts_for(bound))
    assert calls == [LandingStrategy.SQUASH]
    assert receipt.equivalence.equivalent and landing_receipt_hash_is_valid(receipt.to_dict())
    assert receipt.deployment_implication == "none"


@pytest.mark.parametrize("mutation,reason", [
    ("sources", "source_identity_missing_or_ambiguous"),
    ("routing", "dispatch_evidence_missing"),
    ("command", "verifier_identity_mismatch"),
    ("attempt_model", "runtime_profile_changed"),
    ("negative", "missing_required_negative_control"),
    ("positive", "missing_required_positive_control"),
    ("vantage", "proof_vantage_mismatch"),
    ("unbound", "verification_receipt_unbound"),
    ("baseline", "preexisting_failure_without_baseline"),
    ("context", "acceptance_context_invalid"),
])
def test_plausibly_resealed_false_packages_fail_named_boundary(mutation, reason):
    result, source, bound = _chain()
    assert result.validation.ok, result.validation.explain()
    payload = deepcopy(result.evidence_package)
    if mutation == "sources":
        payload["acceptance_context"]["source_snapshots"].pop(source["snapshot_digest"])
    elif mutation == "routing":
        payload["acceptance_context"]["dispatch_evidence"] = {}
    elif mutation == "context":
        payload.pop("acceptance_context")
    elif mutation == "attempt_model":
        row = dict(result.attempts[0].__dict__, model_version="wrong-model")
        payload["attempt_receipts"][0] = make_attempt_receipt(**row).to_dict()
    else:
        index = 0
        row = dict(result.verifications[index].__dict__)
        if mutation == "command":
            row["normalized_command"] = "python -c 'print(1)'"
        elif mutation in {"negative", "positive"}:
            index = 2 if mutation == "negative" else 1
            row = dict(result.verifications[index].__dict__, control_passed=False)
        elif mutation == "vantage":
            row["proof_vantage"] = "model/narration"
        elif mutation == "unbound":
            row["run_id"] = "another-run"
        elif mutation == "baseline":
            row.update(claimed_preexisting=True, failure_fingerprint="same-test-failure",
                       baseline_receipt_hash="b" * 64, baseline_source_digest=source["snapshot_digest"],
                       baseline_failure_fingerprint="same-test-failure")
        payload["verification_receipts"][index] = make_verification_receipt(**row).to_dict()
    validation = validate_evidence_package(_reseal(payload), artifact_extensions=result.artifacts)
    assert not validation.ok and validation.has(reason), validation.explain()


@pytest.mark.parametrize("fact,reason", [
    ("current_profiles", "runtime_profile_changed"),
    ("current_verifier_digests", "verifier_identity_mismatch"),
    ("current_policy_ref", "policy_changed"),
])
def test_consumer_revalidates_current_bindings(fact, reason):
    result, source, bound = _chain()
    observations = _facts_for(bound)
    observations[fact] = "changed" if fact == "current_policy_ref" else {}
    validation = validate_evidence_package(result.evidence_package, current_source=source,
                                          artifact_extensions=result.artifacts, **observations)
    assert validation.state == "INVALIDATED" and validation.has(reason)
    assert not evaluate_landing_eligibility(
        result.evidence_package, _acceptance(result, source), current_source=source,
        artifact_extensions=result.artifacts, **observations).eligible


def test_live_evidence_ttl_and_missing_current_observations_fail_closed():
    result, source, bound = _chain()
    payload = deepcopy(result.evidence_package)
    for requirement in payload["execution_package"]["evidence_requirements"]:
        requirement["freshness_rule"] = "ttl_seconds:60"
    # Changing intent cannot retain valid receipt hashes. Freshness must still
    # be evaluated and return STALE in addition to the detached-intent failures.
    validation = validate_evidence_package(_reseal(payload), artifact_extensions=result.artifacts,
                                          now=NOW + timedelta(seconds=61))
    assert validation.state == "STALE" and validation.has("live_evidence_stale")
    assert not evaluate_landing_eligibility(result.evidence_package, _acceptance(result, source),
        current_source=source, artifact_extensions=result.artifacts).eligible


def test_blob_tamper_and_secret_retention_are_refused(tmp_path):
    result, source, bound = _chain()
    path = retain_evidence_bundle(tmp_path, result.evidence_package, artifacts=result.artifacts)
    blob = next((tmp_path / "blobs").iterdir())
    blob.write_bytes(b"corruption")
    with pytest.raises(ValueError, match="hash/size"):
        read_evidence_bundle(path)
    payload = deepcopy(result.evidence_package)
    payload["seals"] = [{"secret": "sk-" + "fixturecredential" * 3}]
    with pytest.raises(ValueError, match="secret-bearing"):
        retain_evidence_bundle(tmp_path, _reseal(payload), artifacts=result.artifacts)


def test_real_command_adapter_preregisters_controls_and_retains_raw_output(tmp_path):
    response = {"response": "def transform(value): return value.upper()", "done_reason": "stop",
                "prompt_eval_count": 15, "eval_count": 12}
    result = run_canary(worktree=tmp_path / "repo", output_directory=tmp_path / "evidence",
                        endpoint="http://127.0.0.1:11434", model="synthetic-model",
                        generate=lambda context: json.dumps(response).encode())
    assert result["validation"]["state"] == "VERIFIED"
    payload, artifacts = read_evidence_bundle(result["bundle"])
    assert json.loads(artifacts[payload["attempt_receipts"][0]["output_ref"]["storage_uri"]]) == response
    assert payload["verification_receipts"][2]["control_observed"] == "FAIL"
    assert payload["acceptance_context"]["source_snapshots"][payload["verification_receipts"][0]["source_snapshot_digest"]]["changed_paths"] == ["src/thing.py"]


def test_final_candidate_cannot_inherit_an_earlier_requirement():
    result, source, bound = _chain(retry=True)
    payload = deepcopy(result.evidence_package)
    rows = []
    for original in result.verifications:
        fields = dict(original.__dict__)
        if original.attempt == 1:
            fields.update(exit_code=0, tests_failed=0, tests_passed=2)
        else:
            fields["requirement_ids"] = tuple(r for r in original.requirement_ids if r != "scope_check")
        rows.append(make_verification_receipt(**fields).to_dict())
    payload["verification_receipts"] = rows
    validation = validate_evidence_package(_reseal(payload), artifact_extensions=result.artifacts)
    assert validation.has("requirement_unresolved"), validation.explain()


def test_measured_unauthorized_candidate_change_is_rejected():
    result, source, bound = _chain()
    payload = deepcopy(result.evidence_package)
    core = dict(source)
    core.pop("snapshot_digest")
    core["unstaged_paths"] = ["src/thing.py", "src/unauthorized.py"]
    core["relevant_digests"] += [["src/unauthorized.py", "f" * 64]]
    changed = _finalize(core).to_dict()
    payload["acceptance_context"]["source_snapshots"][changed["snapshot_digest"]] = changed
    payload["verification_receipts"] = [make_verification_receipt(**{
        **r.__dict__, "source_snapshot_digest": changed["snapshot_digest"]}).to_dict()
        for r in result.verifications]
    validation = validate_evidence_package(_reseal(payload), current_source=changed, artifact_extensions=result.artifacts)
    assert validation.has("write_outside_authorized_scope"), validation.explain()


def test_default_landing_refuses_a_v1_downgrade():
    result, source, bound = _chain()
    payload = deepcopy(result.evidence_package)
    payload.pop("acceptance_context")
    payload["schema_version"] = 1
    _reseal(payload)
    acceptance = make_semantic_acceptance(**{
        **_acceptance(result, source).__dict__, "evidence_package_hash": payload["evidence_package_hash"]})
    result = evaluate_landing_eligibility(payload, acceptance, current_source=source,
                                         artifact_extensions=result.artifacts)
    assert not result.eligible and "EVIDENCE_NOT_VERIFIED" in result.codes


def test_resealed_serialized_receipt_cannot_claim_zero_exit_with_failed_tests():
    result, source, bound = _chain()
    payload = deepcopy(result.evidence_package)
    row = payload["verification_receipts"][0]
    row["tests_failed"] = 1
    body = {k: v for k, v in row.items() if k not in {"receipt_hash", "outcome"}}
    import hashlib
    row["receipt_hash"] = hashlib.sha256(json.dumps(body, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    validation = validate_evidence_package(_reseal(payload), artifact_extensions=result.artifacts)
    assert validation.has("malformed_record") and not validation.ok


def test_canonical_ps632_receipt_survives_v2_dispatch_sealing():
    from tests.test_ps632_capability_receipt import receipt as qualified_receipt
    from src.dispatch_boundary import seal_dispatch_evidence, validate_dispatch_evidence, TargetEstate
    from src.dispatch_routing import select_target
    from src.local_target_routing import _legacy_view_from_receipt
    package = _package()
    canonical = qualified_receipt(observed_at=NOW.isoformat(), probed_at=NOW.isoformat())
    prior = _bound(package)
    profile = replace(prior.decision.selected_profile, profile_id=canonical.profile_id,
                      model=canonical.model.model_id, model_digest=canonical.model.digest,
                      runtime_version=canonical.runtime.version, host=canonical.host.ssh_host or canonical.host_id)
    view = _legacy_view_from_receipt(canonical, profile, now=NOW)
    decision = select_target(prior.request, profiles=(profile,), receipts=(view,), policy=prior.policy, now=NOW)
    bound = replace(prior, decision=decision,
                    estate=TargetEstate(profiles=(profile,), receipts=(view,), canonical_receipts=(canonical,)))
    payload = seal_dispatch_evidence(bound, attempts=(), invocations=(), include_canonical_receipts=True)
    ok, codes = validate_dispatch_evidence(payload)
    assert ok, codes
    assert payload["canonical_capability_receipts"][0]["receipt_hash"] == decision.capability_receipt_refs[0]
    payload["canonical_capability_receipts"] = []
    ok, codes = validate_dispatch_evidence(payload)
    assert not ok and "capability_receipt_changed" in codes


def test_two_eligible_canonical_targets_survive_sealing_and_recorded_replay():
    from tests.test_ps632_capability_receipt import receipt as qualified_receipt, spec
    from src.dispatch_boundary import seal_dispatch_evidence, seal_recorded_dispatch, validate_dispatch_evidence, TargetEstate
    from src.dispatch_routing import select_target
    from src.local_target_routing import _legacy_view_from_receipt
    prior = _bound(_package())
    canonical = tuple(qualified_receipt(spec_=spec(f"synthetic-target-{n}", ssh_host=f"synthetic-{n}"),
        observed_at=NOW.isoformat(), probed_at=NOW.isoformat()) for n in range(2))
    profiles = tuple(replace(prior.decision.selected_profile,
        profile_id=r.profile_id, target_id=f"synthetic-target-{n}",
        model=r.model.model_id, model_digest=r.model.digest,
        runtime_version=r.runtime.version, host=r.host.ssh_host)
        for n, r in enumerate(canonical))
    views = tuple(_legacy_view_from_receipt(r, p, now=NOW) for r, p in zip(canonical, profiles))
    decision = select_target(prior.request, profiles=profiles, receipts=views, policy=prior.policy, now=NOW)
    assert sum(c.eligible for c in decision.candidates) == 2
    bound = replace(prior, decision=decision,
        estate=TargetEstate(profiles=profiles, receipts=views, canonical_receipts=canonical))
    payload = seal_dispatch_evidence(bound, attempts=(), invocations=(), include_canonical_receipts=True)
    assert validate_dispatch_evidence(payload) == (True, ())
    record = {**payload, "dispatch": {"decision": payload["decision"],
        "request": payload["route_request"]}, "dispatch_receipt": payload["decision_receipt"],
        "dispatch_receipt_hash": decision.receipt_hash, "decision_hash": decision.decision_hash}
    replay = seal_recorded_dispatch(record=record)
    assert replay["canonical_capability_receipts"] == payload["canonical_capability_receipts"]
    assert validate_dispatch_evidence(replay) == (True, ())


def test_dispatch_cannot_widen_sealed_read_scope():
    from src.evidence_acceptance import acceptance_issues
    result, source, bound = _chain()
    payload = deepcopy(result.evidence_package)
    payload["dispatch_receipts"][0]["granted_read_scope"] += ["private/unrelated.txt"]
    issues = acceptance_issues(payload)
    assert any(i.code == "dispatch_target_mismatch" and "grants exceed" in i.detail for i in issues)
