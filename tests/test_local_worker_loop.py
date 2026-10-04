"""Focused controls for the bounded local worker coordinator."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib

import pytest

from src.local_worker_loop import (
    ACCEPTED_CANDIDATE, BLOCKED, ESCALATE, REFUSED, VerificationExecution,
    WorkerExecution, run_local_worker_loop,
)
from src.repair_packet import (
    RepairContextTooLarge, parse_verification_failure,
    render_repair_context,
)
from src.attempt_receipt import (
    artifact_ref, context_projection_digest, make_attempt_receipt,
    make_verification_receipt,
)
from src.dispatch_boundary import BoundDispatch, InvocationIdentity, TargetEstate
from src.dispatch_routing import (
    CAP_EXACT_REFERENCE_SEMANTICS, CAP_SINGLE_TOOL_CALL, CAP_TEXT_GENERATION,
    EXACTNESS_EXACT, LOCALITY_LOCAL, ROLE_IMPLEMENTER, ROLE_REPAIR,
    make_legacy_capability_view, make_target_profile, policy_snapshot,
    ps638_receipt_hash, select_target,
)
from src.dispatch_routing import RoutingRequest
from src.provider_capacity import (
    AuthorizationClass, CapacityState, Entitlement, make_capacity_receipt,
)
from types import SimpleNamespace
from src.evidence_contract import INDEPENDENCE_HARNESS_HIDDEN
from src.evidence_package import reseal_evidence_payload, validate_evidence_package
from src.execution_package import (
    Budgets, VerificationPlan, build_execution_package, compute_package_hash,
)
from src.source_snapshot import _finalize


NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
POLICY = policy_snapshot(policy={"routingPolicyVersion": "test-1"})


def _package(*, max_attempts=2, resource_facts=None):
    source = _finalize({
        "repo_root": "/synthetic/worker-loop", "head_sha": "base-sha",
        "base_sha": "base-sha", "branch": "main", "repo_identity": "synthetic",
        "worktree": "/synthetic/worker-loop", "detached": False,
        "staged_paths": (), "unstaged_paths": (), "untracked_paths": (),
        "tracked_diff_digest": "", "untracked_digest": "",
        "relevant_digests": (), "truncated_paths": (), "diff_truncated": False,
    })
    packet = {
        "packet_id": "fixture-packet", "objective": "Implement the declared fixture contract",
        "contract": "def transform(value: str) -> str", "role": "local_implementer",
        "write_scope": ["src/thing.py"], "interface": [{"name": "value", "required": True}],
        "test_command": "python -m pytest tests/test_thing.py -q",
        "acceptance_criteria": ["preserve the declared input/output relation"],
        "negative_control": "mutated output must fail the hidden verifier",
    }
    plan = VerificationPlan(
        verifier_id="tests/test_thing.py",
        command="python -m pytest tests/test_thing.py -q",
        verifier_paths=("tests/test_thing.py",),
        verifier_digests=(("tests/test_thing.py", "a" * 64),),
        positive_control="the correct transform passes",
        negative_control="mutated output must fail the hidden verifier",
    )
    package = build_execution_package(
        packet, source=source, verification=plan, run_id="worker-loop-test-run",
        allowed_tools=("write_file",),
        budgets=Budgets(max_attempts=max_attempts, max_repair_chars=5000,
                        output_tokens=512),
    )
    return package


def _profile(profile_id="fixture-local", target_id="fixture-target", **overrides):
    fields = {
        "target_id": target_id, "profile_id": profile_id, "provider": "ollama",
        "host": "synthetic-host", "runtime_kind": "ollama", "runtime_version": "0.1",
        "model": "fixture-model", "model_digest": "model-sha", "backend": "cpu",
        "endpoint_url": "http://127.0.0.1:11434/v1", "locality": LOCALITY_LOCAL,
        "roles": frozenset({ROLE_IMPLEMENTER, ROLE_REPAIR}),
        "tools": frozenset({"write_file"}), "network_policy": "offline",
        "budget_class": "dev", "cost_rank": 0,
    }
    fields.update(overrides)
    return make_target_profile(**fields)


def _receipt(profile):
    return make_legacy_capability_view(
        receipt_id=f"cap-{profile.profile_id}", profile_id=profile.profile_id,
        target_id=profile.target_id,
        capabilities=frozenset({CAP_TEXT_GENERATION, CAP_SINGLE_TOOL_CALL,
                                CAP_EXACT_REFERENCE_SEMANTICS}),
        exactness=EXACTNESS_EXACT, observed_at=NOW.isoformat(), ttl_s=86400,
        healthy=True, host=profile.host, runtime_version=profile.runtime_version,
        model_digest=profile.model_digest,
    )


def _bound(package, *, profile=None, decision_id="decision-1", resources=None,
           policy=None):
    profile = profile or _profile()
    receipt = _receipt(profile)
    package_payload = package.to_dict()
    request = RoutingRequest(
        domain="general_swe", role=ROLE_IMPLEMENTER,
        run_id=package_payload["run_id"], packet_id=package_payload["packet_id"],
        execution_package_hash=package_payload["package_hash"],
        required_tools=("write_file",), network_policy="offline",
        budget_class="dev", write_scope=("src/thing.py",),
    )
    chosen_policy = policy or POLICY
    decision = select_target(
        request, profiles=(profile,), receipts=(receipt,), policy=chosen_policy,
        now=NOW, decision_id=decision_id,
        resources=({profile.target_id: resources} if resources is not None else None),
    )
    bound = BoundDispatch(
        request=request,
        estate=TargetEstate(profiles=(profile,), receipts=(receipt,)),
        decision=decision, policy=chosen_policy,
    )
    if resources and resources.get("capacity_receipt_refs"):
        decision = replace(
            decision,
            capacity_receipt_refs=tuple(resources["capacity_receipt_refs"]))
        decision = replace(
            decision,
            receipt_hash=ps638_receipt_hash(decision.to_ps638_receipt_kwargs()))
        bound = replace(bound, decision=decision)
    return bound


def _hosted_bound(package):
    """Build a current canonical hosted decision with a real synthetic capacity receipt."""
    profile = _profile(
        target_id="hosted-fixture", profile_id="hosted-fixture-profile",
        provider="openrouter", host="hosted-fixture.invalid",
        endpoint_url="https://hosted-fixture.invalid/v1",
        locality="hosted", credential_sha256="d" * 64,
    )
    capability = _receipt(profile)
    capacity = make_capacity_receipt(
        provider=profile.provider, pool_id="fixture-pool",
        account_identity="fixture-account",
        authorization_class=AuthorizationClass.AGENT_SDK,
        entitlement=Entitlement.AGENT_SDK, exposed_models=(profile.model,),
        observed_at=NOW.isoformat(), ttl_seconds=3600,
        collector_id="synthetic-capacity-test", evidence_source="fixture",
        evidence_reference="synthetic-capacity-evidence", state=CapacityState.AVAILABLE,
        concurrency_remaining=4, credential_sha256=profile.credential_sha256,
        endpoint_url=profile.endpoint_url,
    )
    p = package.to_dict()
    request = RoutingRequest(
        domain="general_swe", role=ROLE_IMPLEMENTER, run_id=p["run_id"],
        packet_id=p["packet_id"], execution_package_hash=p["package_hash"],
        required_tools=("write_file",), network_policy=profile.network_policy,
        budget_class="dev", write_scope=("src/thing.py",),
    )
    decision = select_target(
        request, profiles=(profile,), receipts=(capability,), policy=POLICY,
        capacity_receipts=(capacity,), now=NOW, decision_id="hosted-capacity-fixture")
    return BoundDispatch(
        request=request, estate=TargetEstate(profiles=(profile,), receipts=(capability,)),
        decision=decision, policy=POLICY, capacity_receipts=(capacity,))


def _facts(bound, package, **overrides):
    pin = bound.pin_for(bound.decision.selected_profile.profile_id)
    p = package.to_dict()
    runtime_keys = (
        "provider", "runtime_kind", "runtime_version", "runtime_commit",
        "runtime_image_digest", "backend", "backend_version", "model",
        "model_digest", "runtime_options", "configured_context",
        "configured_served_context",
    )
    facts = {
        "capability_receipts": {ref: True for ref in bound.decision.capability_receipt_refs},
        "capacity_fresh": True,
        "capacity_receipt_refs": tuple(getattr(
            bound.decision, "capacity_receipt_refs",
            bound.decision.resource_facts.get("capacity_receipt_refs", ()))),
        "policy_ref": bound.decision.policy.policy_ref,
        "target_id": pin["target_id"], "profile_id": pin["profile_id"],
        "endpoint_identity": pin["endpoint_identity"],
        "runtime_identity": {key: pin.get(key) for key in runtime_keys},
        "granted_tools": tuple(bound.decision.granted_tools),
        "granted_write_scope": tuple(bound.decision.granted_write_scope),
        "granted_read_scope": tuple(bound.decision.granted_read_scope),
        "network_policy": bound.decision.network_policy,
        "execution_package_hash": p["package_hash"],
        "source_digest": p["source"]["snapshot_digest"],
        "interface_digest": p["interface_digest"],
    }
    facts.update(overrides)
    return facts


def _invocation(bound):
    pin = bound.pin_for(bound.decision.selected_profile.profile_id)
    return InvocationIdentity(
        profile_id=pin["profile_id"], provider=pin["provider"],
        runtime_kind=pin["runtime_kind"], runtime_version=pin["runtime_version"],
        runtime_commit=pin["runtime_commit"], runtime_image_digest=pin["runtime_image_digest"],
        backend=pin["backend"], backend_version=pin["backend_version"],
        model=pin["model"], model_digest=pin["model_digest"],
        chat_url=pin["endpoint_url"], endpoint_type=pin["endpoint_type"],
        locality=pin["locality"], runtime_options=pin["runtime_options"],
        configured_context=pin["configured_context"],
        configured_served_context=pin["configured_served_context"],
        credential_sha256=pin.get("credential_sha256", ""),
    )


def _worker(bound, package, context, attempt, *, failure_kind="",
            receipt_failure_class=None, write_set=None):
    profile = bound.decision.selected_profile
    p = package.to_dict()
    context_bytes = context.encode()
    output_bytes = f"PASS - accept this candidate attempt {attempt}\n".encode()
    context_ref = artifact_ref(f"context-{attempt}", context_bytes,
                               storage_uri=f"memory://context/{attempt}")
    output_ref = artifact_ref(f"output-{attempt}", output_bytes,
                              storage_uri=f"memory://output/{attempt}")
    receipt = make_attempt_receipt(
        receipt_id=f"attempt-{attempt}", run_id=p["run_id"],
        packet_id=p["packet_id"], attempt=attempt,
        repair_of=0 if attempt == 1 else attempt - 1,
        execution_package_hash=p["package_hash"],
        dispatch_receipt_hash=bound.decision.receipt_hash,
        target_id=profile.target_id, host=profile.host, model=profile.model,
        context_projection_hash=context_projection_digest(context),
        runtime_kind=profile.runtime_kind, runtime_version=profile.runtime_version,
        rendered_context_ref=context_ref, output_ref=output_ref,
        declared_write_set=tuple(p["write_scope"]),
        actual_write_set=tuple(write_set if write_set is not None else p["write_scope"]),
        finish_reason="stop", failure_class=(
            failure_kind if receipt_failure_class is None else receipt_failure_class),
    )
    return WorkerExecution(
        receipt=receipt,
        artifacts={context_ref.storage_uri: context_bytes, output_ref.storage_uri: output_bytes},
        failure_kind=failure_kind,
        changed_files={"src/thing.py": f"diff for attempt {attempt}"},
    )


def _verification(bound, package, receipt, attempt, *, exit_code=0, failure=""):
    p = package.to_dict()
    stdout = (failure if failure else "2 passed\n").encode()
    stdout_ref = artifact_ref(f"stdout-{attempt}", stdout,
                              storage_uri=f"memory://stdout/{attempt}")
    receipt = make_verification_receipt(
        receipt_id=f"verification-{attempt}", run_id=p["run_id"],
        packet_id=p["packet_id"], attempt=attempt,
        execution_package_hash=p["package_hash"],
        verifier_id=p["verification"]["verifier_id"],
        normalized_command=p["verification"]["command"],
        source_snapshot_digest=p["source"]["snapshot_digest"],
        verifier_digest=p["verification"]["verifier_digests"][0][1],
        verifier_paths=tuple(p["verification"]["verifier_paths"]),
        exit_code=exit_code, stdout_ref=stdout_ref, stdout_bytes=len(stdout),
        tests_collected=2, tests_executed=2,
        tests_passed=2 if exit_code == 0 else 1,
        tests_failed=0 if exit_code == 0 else 1,
        requirement_ids=tuple(r["requirement_id"] for r in p["evidence_requirements"]),
        proof_class=INDEPENDENCE_HARNESS_HIDDEN,
        control_id="negative_control", control_expected="mutated output fails",
        control_observed="negative fixture fails", control_passed=True,
        observed="fixture test result",
    )
    return VerificationExecution(receipt, {stdout_ref.storage_uri: stdout})


def _run(package, bound, verification_codes, *, facts_mutator=None,
         select_fresh=None, worker_failure=None, invocation_mutator=None,
         after_verifier=None, canonical_failure_class=None, changed_files=None,
         max_attempts=None):
    workers, verifiers, contexts, fact_calls, selections = [], [], [], [], []

    def facts(current_bound, attempt):
        fact_calls.append((current_bound.decision.decision_id, attempt))
        values = _facts(current_bound, package)
        if facts_mutator:
            values.update(facts_mutator(current_bound, attempt) or {})
        return values

    def worker(current_bound, context, attempt, _tokens):
        contexts.append(context)
        kind = worker_failure(current_bound, attempt) if worker_failure else ""
        result = _worker(current_bound, package, context, attempt, failure_kind=kind,
                         receipt_failure_class=canonical_failure_class)
        workers.append(result)
        return result

    def verifier(current_bound, attempt_receipt, attempt):
        code, failure = verification_codes[attempt - 1]
        result = _verification(current_bound, package, attempt_receipt, attempt,
                               exit_code=code, failure=failure)
        verifiers.append(result)
        if after_verifier:
            after_verifier(current_bound, attempt, result)
        return result

    selector = None
    if select_fresh:
        def selector(original_request, attempt):
            selections.append((original_request, attempt))
            return select_fresh(original_request, attempt)

    def invocation(current_bound):
        identity = _invocation(current_bound)
        if invocation_mutator:
            return invocation_mutator(current_bound, identity)
        return identity

    result = run_local_worker_loop(
        execution_package=package, standing_dispatch=bound,
        current_facts=facts, invocation=invocation,
        execute_worker=worker, execute_verifier=verifier,
        select_fresh=selector, changed_files=changed_files,
    )
    return result, workers, verifiers, contexts, fact_calls, selections


def test_malformed_package_is_refused_before_any_runtime_callback():
    calls = []
    result = run_local_worker_loop(
        execution_package={"package_hash": "bad"},
        standing_dispatch=SimpleNamespace(),
        current_facts=lambda *_: calls.append("facts"),
        invocation=lambda *_: calls.append("invoke"),
        execute_worker=lambda *_: calls.append("worker"),
        execute_verifier=lambda *_: calls.append("verifier"),
    )
    assert result.status == REFUSED
    assert calls == []


def test_resealed_but_invalid_verifier_contract_is_refused_before_any_callback():
    package = _package()
    payload = package.to_dict()
    payload["verification"]["command"] = ""
    payload.pop("package_hash")
    payload["package_hash"] = compute_package_hash(payload)
    original = _bound(package)
    request = replace(original.request, execution_package_hash=payload["package_hash"])
    fake_bound = SimpleNamespace(request=request, decision=SimpleNamespace(request=request))
    calls = []
    result = run_local_worker_loop(
        execution_package=payload, standing_dispatch=fake_bound,
        current_facts=lambda *_: calls.append("facts"),
        invocation=lambda *_: calls.append("invoke"),
        execute_worker=lambda *_: calls.append("worker"),
        execute_verifier=lambda *_: calls.append("verifier"),
    )
    assert result.status == REFUSED
    assert "verifier contract" in result.refusal_reason
    assert calls == []


def test_pytest_collection_error_is_not_inferred_from_test_import_failure():
    collection = parse_verification_failure(
        "pytest -q", 2, "ERROR collecting tests/test_x.py\nImportError: no module")
    runtime = parse_verification_failure(
        "pytest -q", 1,
        "FAILED tests/test_x.py::test_import - ImportError: application module")
    assert collection.collection_error is True
    assert runtime.collection_error is False


def test_repair_truncation_never_cuts_immutable_contract_fields():
    repair = {
        "attempt": 2, "budget_remaining": 1, "objective": "O" * 90,
        "contract": "C" * 90, "acceptance_criteria": ["A" * 60],
        "write_scope": ["src/x.py"], "interface": ["x | required"],
        "interface_digest": "abc", "verifier": {"verifier_id": "v", "command": "pytest"},
        "failing_command": "pytest", "failing_tests": ["test_x"],
        "failure_reasons": ["bad"], "changed_files": {"src/x.py": "D" * 1000},
        "error_excerpt": "E" * 1000,
    }
    rendered = render_repair_context(repair, max_chars=1100)
    assert len(rendered) <= 1100
    for marker in ("OBJECTIVE:", "CONTRACT (UNCHANGED", "ACCEPTANCE (UNCHANGED",
                   "WRITE_SCOPE:", "INTERFACE (UNCHANGED", "INTERFACE_DIGEST: abc",
                   "VERIFIER CONTRACT (UNCHANGED)", "  command:  pytest"):
        assert marker in rendered
    assert "[CONTEXT TRUNCATED]" in rendered


def test_repair_refuses_budget_smaller_than_immutable_contract():
    repair = {"objective": "task", "contract": "contract" * 100,
              "verifier": {"verifier_id": "v", "command": "pytest"}}
    with pytest.raises(RepairContextTooLarge):
        render_repair_context(repair, max_chars=50)


def test_failure_excerpt_is_bounded_and_fingerprint_input_retains_failure():
    failure = parse_verification_failure("pytest -q", 1,
                                        "FAILED tests/a.py::test_a - Nope\n" + "x" * 100,
                                        max_excerpt=24)
    assert len(failure.excerpt) == 24
    assert failure.failing_tests == ("tests/a.py::test_a",)
    assert failure.reasons == ("Nope",)


def test_failure_excerpt_zero_and_invalid_bounds_never_retain_unbounded_output():
    output = "x" * 100_070
    empty = parse_verification_failure("pytest -q", 1, output, max_excerpt=0)
    assert empty.excerpt == ""
    bounded = parse_verification_failure("pytest -q", 1, output, max_excerpt=37)
    assert bounded.excerpt == "x" * 37
    for invalid in (-1, 100_071, True, 1.5):
        with pytest.raises(ValueError, match="max_excerpt"):
            parse_verification_failure("pytest -q", 1, output, max_excerpt=invalid)


def test_fail_then_repair_pass_reuses_dispatch_and_validates_canonical_red_history():
    package = _package(max_attempts=3)
    bound = _bound(package)
    fail_log = "FAILED tests/test_thing.py::test_contract - AssertionError: wrong output\n"
    result, workers, verifiers, contexts, _facts, selections = _run(
        package, bound, [(1, fail_log), (0, "")])
    assert result.status == ACCEPTED_CANDIDATE
    assert selections == [], "a still-valid standing dispatch decision is reused"
    assert len(workers) == len(verifiers) == len(contexts) == 2
    assert result.attempts[0].attempt == 1 and result.attempts[0].repair_of == 0
    assert result.attempts[1].attempt == 2 and result.attempts[1].repair_of == 1
    assert result.verifications[0].outcome == "FAIL"
    assert result.verifications[1].outcome == "PASS"
    assert contexts[0] != contexts[1]
    assert "FAILING_COMMAND:" in contexts[1]
    assert "wrong output" in contexts[1]
    for immutable in ("Implement the declared fixture contract", "def transform(value: str) -> str",
                      "preserve the declared input/output relation", "value | required"):
        assert immutable in contexts[0]
        assert immutable in contexts[1]
    assert contexts[0] not in contexts[1], "repair receives fresh context, not prior conversation"
    # Re-run the canonical validator independently; a worker/verifier assertion
    # cannot replace its receipt-derived decision.
    validation = validate_evidence_package(result.evidence_package,
                                           artifact_extensions={
                                               **workers[0].artifacts, **workers[1].artifacts,
                                               **verifiers[0].artifacts, **verifiers[1].artifacts,
                                           })
    assert validation.ok, validation.explain()
    attempt_rows = result.evidence_package["attempt_receipts"]
    verification_rows = result.evidence_package["verification_receipts"]
    assert [row["attempt"] for row in attempt_rows] == [1, 2]
    assert [row["attempt"] for row in verification_rows] == [1, 2]
    forged = reseal_evidence_payload(dict(
        result.evidence_package,
        verification_receipts=[dict(verification_rows[1], exit_code=1)],
    ))
    assert not validate_evidence_package(forged, artifact_extensions={
        **workers[0].artifacts, **workers[1].artifacts,
        **verifiers[0].artifacts, **verifiers[1].artifacts,
    }).ok, "canonical validator refuses detached/tampered receipt history"


def test_changed_file_capture_error_returns_block_with_original_red_history():
    package = _package(max_attempts=3)
    bound = _bound(package)
    log = "FAILED tests/test_thing.py::test_contract - AssertionError: first failure\n"

    def unavailable(_bound, _execution):
        raise OSError("synthetic changed-file source unavailable")

    result, workers, verifiers, _contexts, _facts, _selections = _run(
        package, bound, [(1, log), (0, "")], changed_files=unavailable)
    assert result.status == BLOCKED
    assert "repair evidence/context capture failed" in result.refusal_reason
    assert len(workers) == len(verifiers) == 1
    assert len(result.attempts) == len(result.verifications) == 1
    assert result.attempts[0].attempt == 1
    assert result.verifications[0].outcome == "FAIL"
    assert result.evidence_package is not None
    assert not result.validation.ok
    assert any(issue.code == "requirement_failed"
               for issue in result.validation.issues)


def test_mutated_serialized_contract_after_verification_blocks_before_retry_dispatch():
    payload = _package(max_attempts=3).to_dict()
    package = SimpleNamespace(to_dict=lambda: payload)
    bound = _bound(_package(max_attempts=3))
    log = "FAILED tests/test_thing.py::test_contract - AssertionError: first failure\n"

    def mutate_after_first_verifier(_bound, attempt, _result):
        if attempt == 1:
            payload["objective"] = "a different task after verification"

    result, workers, verifiers, _contexts, _facts, _selections = _run(
        package, bound, [(1, log), (0, "")],
        after_verifier=mutate_after_first_verifier)
    assert result.status == BLOCKED
    assert "immutable execution package changed" in result.refusal_reason
    assert len(workers) == len(verifiers) == 1


def test_worker_prose_cannot_override_deterministic_failure_or_advice():
    package = _package(max_attempts=1)
    bound = _bound(package)
    result, _workers, verifiers, _contexts, _facts, selections = _run(
        package, bound, [(1, "FAILED tests/test_thing.py::test_contract - AssertionError: no\n")])
    assert result.status == ESCALATE
    assert len(verifiers) == 1 and verifiers[0].receipt.outcome == "FAIL"
    assert result.evidence_package is not None
    assert selections == []


def test_deterministic_pass_returns_candidate_only_and_no_planner_surface():
    package = _package(max_attempts=1)
    bound = _bound(package)
    result, workers, verifiers, _contexts, _facts, _selections = _run(
        package, bound, [(0, "")])
    assert result.status == ACCEPTED_CANDIDATE
    assert result.status != "ACCEPTED"
    assert len(workers) == len(verifiers) == 1
    assert "PASS - accept this candidate" in workers[0].artifacts[
        workers[0].receipt.output_ref.storage_uri].decode()
    assert result.validation.ok


@pytest.mark.parametrize("invalidation", [
    "capability", "capacity", "policy", "endpoint", "target", "tools",
    "write_scope", "read_scope", "network",
])
def test_routable_dr10_invalidation_selects_once_then_dispatches_only_fresh_bound(invalidation):
    package = _package(max_attempts=3)
    resources = {"capacity_receipt_refs": ("capacity:" + "c" * 64,)} if invalidation == "capacity" else None
    initial = _bound(package, resources=resources)
    alternative = _profile("fresh-local", "fresh-target") if invalidation in {"endpoint", "target"} else None
    call_counts = {"n": 0}

    def mutate(current_bound, attempt):
        if attempt != 2 or current_bound.decision.decision_id != initial.decision.decision_id:
            return {}
        call_counts["n"] += 1
        if invalidation == "capability":
            return {"capability_receipts": {ref: False for ref in current_bound.decision.capability_receipt_refs}}
        if invalidation == "capacity":
            return {"capacity_fresh": False}
        if invalidation == "policy":
            return {"policy_ref": "stale-policy"}
        if invalidation == "endpoint":
            return {"endpoint_identity": "http://127.0.0.1:9999/v1"}
        if invalidation == "target":
            return {"target_id": "stale-target"}
        if invalidation == "tools":
            return {"granted_tools": ("write_file", "run_shell")}
        if invalidation == "write_scope":
            return {"granted_write_scope": ("src/other.py",)}
        if invalidation == "read_scope":
            return {"granted_read_scope": ("secrets.txt",)}
        return {"network_policy": "unrestricted"}

    def choose(original_request, attempt):
        assert original_request == initial.request
        assert attempt == 2
        fresh_policy = (policy_snapshot(policy={"routingPolicyVersion": "test-2"})
                        if invalidation == "policy" else None)
        return _bound(package, profile=alternative or initial.decision.selected_profile,
                      decision_id=f"fresh-{invalidation}", resources=resources,
                      policy=fresh_policy)

    log = "FAILED tests/test_thing.py::test_contract - AssertionError: first failure\n"
    result, workers, _verifiers, _contexts, _facts, selections = _run(
        package, initial, [(1, log), (0, "")], facts_mutator=mutate,
        select_fresh=choose)
    assert result.status == ACCEPTED_CANDIDATE, result.refusal_reason
    assert call_counts["n"] == 1
    assert len(selections) == 1 and selections[0][1] == 2
    assert len(workers) == 2
    assert result.attempts[1].dispatch_receipt_hash == result.dispatches[-1].receipt_hash


@pytest.mark.parametrize("immutable_drift", [
    "execution_package_hash", "source_digest", "interface_digest", "runtime_identity",
])
def test_immutable_dr10_drift_fails_closed_without_selector_or_second_worker(immutable_drift):
    package = _package(max_attempts=3)
    bound = _bound(package)
    log = "FAILED tests/test_thing.py::test_contract - AssertionError: first failure\n"

    def mutate(_bound, attempt):
        if attempt != 2:
            return {}
        facts = _facts(_bound, package)
        if immutable_drift == "runtime_identity":
            runtime = dict(facts["runtime_identity"])
            runtime["model"] = "different-model"
            return {"runtime_identity": runtime}
        return {immutable_drift: "different-immutable-value"}

    result, workers, _verifiers, _contexts, _facts_called, selections = _run(
        package, bound, [(1, log), (0, "")], facts_mutator=mutate,
        select_fresh=lambda *_: pytest.fail("immutable drift must not reselect"))
    assert result.status == BLOCKED
    assert len(workers) == 1
    assert selections == []
    assert result.evidence_package is not None


def test_fresh_eligible_alternate_is_pinned_and_no_eligible_alternate_calls_no_worker():
    package = _package(max_attempts=2)
    initial = _bound(package)
    alternate = _profile("alternate-local", "alternate-target")
    log = "FAILED tests/test_thing.py::test_contract - AssertionError: first failure\n"
    def stale(_bound, attempt):
        return ({"capability_receipts": {ref: False for ref in _bound.decision.capability_receipt_refs}}
                if attempt == 2 and _bound.decision.decision_id == initial.decision.decision_id else {})

    result, workers, _verifiers, _contexts, _facts, selections = _run(
        package, initial, [(1, log), (0, "")], facts_mutator=stale,
        select_fresh=lambda req, n: _bound(package, profile=alternate, decision_id=f"alternate-{n}"))
    assert result.status == ACCEPTED_CANDIDATE
    assert len(selections) == 1 and len(workers) == 2
    assert result.attempts[1].target_id == "alternate-target"
    assert result.attempts[1].dispatch_receipt_hash == result.dispatches[-1].receipt_hash

    result, workers, _verifiers, _contexts, _facts, selections = _run(
        package, initial, [(1, log), (0, "")], facts_mutator=stale,
        select_fresh=lambda _req, _n: None)
    assert result.status == BLOCKED
    assert len(workers) == 1
    assert len(selections) == 1


def test_fresh_dispatch_with_mismatched_invocation_pin_never_calls_worker():
    package = _package(max_attempts=3)
    initial = _bound(package)
    alternate = _profile("fresh-pinned", "fresh-pinned-target")
    log = "FAILED tests/test_thing.py::test_contract - AssertionError: first failure\n"

    def stale(current, attempt):
        if attempt == 2 and current.decision.decision_id == initial.decision.decision_id:
            return {"capability_receipts": {
                ref: False for ref in current.decision.capability_receipt_refs}}
        return {}

    def mispin(current, identity):
        if current.decision.decision_id == "fresh-with-bad-pin":
            return replace(identity, chat_url="http://127.0.0.1:9999/v1")
        return identity

    result, workers, _verifiers, _contexts, _facts, selections = _run(
        package, initial, [(1, log), (0, "")], facts_mutator=stale,
        select_fresh=lambda req, n: _bound(
            package, profile=alternate, decision_id="fresh-with-bad-pin"),
        invocation_mutator=mispin)
    assert result.status == BLOCKED
    assert len(selections) == 1
    assert len(workers) == 1
    assert [attempt.attempt for attempt in result.attempts] == [1]


def test_fresh_selection_cannot_change_immutable_model_or_digest():
    package = _package(max_attempts=3)
    initial = _bound(package)
    changed_model = _profile("fresh-different-model", "fresh-different-target",
                             model="different-model", model_digest="different-digest")
    log = "FAILED tests/test_thing.py::test_contract - AssertionError: first failure\n"

    def stale(current, attempt):
        if attempt == 2 and current.decision.decision_id == initial.decision.decision_id:
            return {"capability_receipts": {
                ref: False for ref in current.decision.capability_receipt_refs}}
        return {}

    result, workers, _verifiers, _contexts, _facts, selections = _run(
        package, initial, [(1, log), (0, "")], facts_mutator=stale,
        select_fresh=lambda req, n: _bound(
            package, profile=changed_model, decision_id=f"changed-model-{n}"))
    assert result.status == BLOCKED
    assert "immutable runtime/model identity" in result.refusal_reason
    assert len(selections) == 1
    assert len(workers) == 1


def test_fresh_decision_must_bind_original_package_request():
    package = _package(max_attempts=3)
    initial = _bound(package)
    log = "FAILED tests/test_thing.py::test_contract - AssertionError: first failure\n"

    def stale(current, attempt):
        if attempt == 2 and current.decision.decision_id == initial.decision.decision_id:
            return {"capability_receipts": {
                ref: False for ref in current.decision.capability_receipt_refs}}
        return {}

    def malformed_selection(req, n):
        fresh = _bound(package, decision_id=f"bad-request-{n}")
        wrong_decision_request = replace(fresh.decision.request, packet_id="other-packet")
        return replace(fresh, decision=replace(fresh.decision, request=wrong_decision_request))

    result, workers, _verifiers, _contexts, _facts, selections = _run(
        package, initial, [(1, log), (0, "")], facts_mutator=stale,
        select_fresh=malformed_selection)
    assert result.status == BLOCKED
    assert len(selections) == 1
    assert len(workers) == 1


def test_capacity_is_revalidated_only_when_decision_relied_on_capacity():
    package = _package(max_attempts=1)
    no_resource = _bound(package)
    facts = _facts(no_resource, package, capacity_fresh=False)
    from src.local_worker_loop import _decision_facts_match
    assert _decision_facts_match(no_resource, package, facts) == (True, "", False)
    relied = _bound(package, resources={"capacity_receipt_refs": ("capacity:" + "a" * 64,)})
    facts = _facts(relied, package, capacity_fresh=False)
    assert _decision_facts_match(relied, package, facts)[0] is False


def test_current_canonical_hosted_capacity_ref_is_checked_without_resource_facts():
    package = _package(max_attempts=3)
    bound = _hosted_bound(package)
    assert bound.decision.capacity_receipt_refs
    assert bound.decision.resource_facts == {}
    log = "FAILED tests/test_thing.py::test_contract - AssertionError: first failure\n"

    initial_stale, initial_workers, initial_verifiers, *_ = _run(
        package, bound, [(0, "")],
        facts_mutator=lambda _current, _attempt: {"capacity_fresh": False})
    assert initial_stale.status == BLOCKED
    assert "capacity facts are stale" in initial_stale.refusal_reason
    assert initial_workers == []
    assert initial_verifiers == []

    def stale_on_retry(_current, attempt):
        return {"capacity_fresh": False} if attempt == 2 else {}

    result, workers, verifiers, _contexts, _facts, _selections = _run(
        package, bound, [(1, log), (0, "")], facts_mutator=stale_on_retry)
    assert result.status == BLOCKED
    assert "capacity facts are stale" in result.refusal_reason
    assert len(workers) == len(verifiers) == 1
    assert result.attempts[0].attempt == 1
    assert result.verifications[0].outcome == "FAIL"


def test_current_canonical_hosted_capacity_fresh_path_is_usable():
    package = _package(max_attempts=1)
    bound = _hosted_bound(package)
    assert bound.decision.capacity_receipt_refs
    assert bound.decision.resource_facts == {}
    result, workers, verifiers, _contexts, _facts, _selections = _run(
        package, bound, [(0, "")])
    assert result.status == ACCEPTED_CANDIDATE
    assert len(workers) == len(verifiers) == 1
    assert result.validation.ok


def test_malformed_decision_capacity_ref_cannot_authorize_worker():
    package = _package(max_attempts=1)
    initial = _bound(package)
    decision = replace(initial.decision, capacity_receipt_refs=("capacity:not-a-digest",))
    decision = replace(decision,
                       receipt_hash=ps638_receipt_hash(decision.to_ps638_receipt_kwargs()))
    malformed = replace(initial, decision=decision)
    result, workers, _verifiers, _contexts, _facts, _selections = _run(
        package, malformed, [(0, "")])
    assert result.status == BLOCKED
    assert "capacity receipt references are malformed" in result.refusal_reason
    assert workers == []


def test_shared_budget_exhaustion_and_repeated_failure_retain_red_receipt_history():
    package = _package(max_attempts=2)
    bound = _bound(package)
    fail_a = "FAILED tests/test_thing.py::test_contract - AssertionError: output 1\n"
    fail_b = "FAILED tests/test_thing.py::test_contract - TypeError: wrong type 2\n"
    result, workers, verifiers, _contexts, _facts, _selections = _run(
        package, bound, [(1, fail_a), (1, fail_b)])
    assert result.status == ESCALATE
    assert len(workers) == len(verifiers) == 2
    assert [a.attempt for a in result.attempts] == [1, 2]
    assert [a.repair_of for a in result.attempts] == [0, 1]
    assert [v.outcome for v in result.verifications] == ["FAIL", "FAIL"]
    assert result.evidence_package is not None

    same = "FAILED tests/test_thing.py::test_contract - AssertionError: same defect 123\n"
    result, workers, verifiers, _contexts, _facts, _selections = _run(
        package, bound, [(1, same), (1, same)])
    assert result.status == ESCALATE
    assert len(workers) == len(verifiers) == 2
    assert "repeated deterministic failure" in result.refusal_reason
    assert len(result.evidence_package["attempt_receipts"]) == 2
    assert len(result.evidence_package["verification_receipts"]) == 2


def test_infrastructure_failure_blocks_without_verifier_but_technical_failure_is_repairable():
    package = _package(max_attempts=2)
    bound = _bound(package)
    result, workers, verifiers, _contexts, _facts, _selections = _run(
        package, bound, [(0, "")], worker_failure=lambda _b, _n: "infra")
    assert result.status == BLOCKED
    assert len(workers) == 1 and verifiers == []
    assert "non-repairable worker failure: infra" in result.refusal_reason

    log = "FAILED tests/test_thing.py::test_contract - AssertionError: fixable\n"
    result, workers, verifiers, _contexts, _facts, _selections = _run(
        package, bound, [(1, log), (0, "")],
        worker_failure=lambda _b, _n: "technical")
    assert result.status == ACCEPTED_CANDIDATE
    assert len(workers) == len(verifiers) == 2

    result, workers, verifiers, _contexts, _facts, _selections = _run(
        package, bound, [(0, "")], worker_failure=lambda _b, _n: "runtime_provider")
    assert result.status == BLOCKED
    assert len(workers) == 1 and verifiers == []
    assert "runtime_provider" in result.refusal_reason


def test_canonical_infrastructure_failure_cannot_be_erased_by_empty_adapter_hint():
    package = _package(max_attempts=2)
    bound = _bound(package)
    result, workers, verifiers, _contexts, _facts, _selections = _run(
        package, bound, [(0, "")], canonical_failure_class="infra")
    assert result.status == BLOCKED
    assert "non-repairable worker failure: infra" in result.refusal_reason
    assert len(workers) == 1
    assert verifiers == []


def test_missing_or_tampered_failure_artifact_blocks_instead_of_building_repair():
    package = _package(max_attempts=2)
    bound = _bound(package)
    workers, verifiers = [], []
    def worker(current_bound, context, attempt, _tokens):
        result = _worker(current_bound, package, context, attempt)
        workers.append(result)
        return result
    def verifier(current_bound, _ar, attempt):
        result = _verification(
            current_bound, package, None, attempt, exit_code=1,
            failure="FAILED tests/test_thing.py::test_contract - AssertionError: bad\n")
        result.artifacts[f"memory://stdout/{attempt}"] = b"tampered output\n"
        verifiers.append(result)
        return result
    result = run_local_worker_loop(
        execution_package=package, standing_dispatch=bound,
        current_facts=lambda b, _n: _facts(b, package), invocation=_invocation,
        execute_worker=worker, execute_verifier=verifier,
    )
    assert result.status == BLOCKED
    assert len(workers) == len(verifiers) == 1
    assert "artifact hash mismatched" in result.refusal_reason


def test_unparseable_deterministic_failure_is_not_sent_as_empty_repair():
    package = _package(max_attempts=2)
    bound = _bound(package)
    result, workers, verifiers, _contexts, _facts, _selections = _run(
        package, bound, [(1, "process exited with status 1\n")])
    assert result.status == BLOCKED
    assert len(workers) == len(verifiers) == 1
    assert "no parsed failing test" in result.refusal_reason
