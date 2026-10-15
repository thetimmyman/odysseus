"""Public retained-input inspection, using synthetic canonical worker receipts."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import json

import pytest

from src.evidence_package import compute_evidence_package_hash, seal_evidence_package
from src.attempt_receipt import make_attempt_receipt
from src.dispatch_boundary import TargetEstate, seal_dispatch_evidence
from src import dispatch_routing as routing
from src.execution_outcomes import build_outcome_record
from src.execution_package import make_dispatch_receipt
from src.local_target_routing import _legacy_view_from_receipt
from src.local_targets import (
    CapabilityEvidence, ContextProfile, HostBaseline, ModelIdentity, RuntimeIdentity,
    make_target_capability_receipt,
)
from src.provider_capacity import (
    AuthorizationClass, CapacityState, Entitlement, make_capacity_receipt,
)
from src.routing_inspector import (
    RoutingInspectionError, project_retained_routing_inspection,
    project_routing_inspection,
)
from tests.test_local_worker_loop import NOW, _bound, _package
from tests.test_ps638_consumer_chain import _chain


def _reseal(payload):
    body = {key: value for key, value in payload.items()
            if key != "evidence_package_hash"}
    payload["evidence_package_hash"] = compute_evidence_package_hash(body)
    return payload


def _rehash(row, *, exclude=()):
    body = {key: value for key, value in row.items()
            if key not in {"receipt_hash", *exclude}}
    row["receipt_hash"] = hashlib.sha256(json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


@pytest.fixture
def retained():
    result, _, bound = _chain(retry=True)
    # A normal JSON serialization contains normalized strings, not WorkPacket fields.
    payload = json.loads(json.dumps(result.evidence_package))
    return result, payload, dict(result.artifacts), bound


def _inspect(payload, artifacts, *, attempt=None, **kwargs):
    return project_retained_routing_inspection(
        payload, artifacts=artifacts,
        attempt_receipt_hash=attempt or payload["attempt_receipts"][0]["receipt_hash"],
        now=NOW + timedelta(seconds=2), **kwargs)


def test_serialized_normalized_package_matches_typed_projection(retained, monkeypatch):
    result, payload, artifacts, _ = retained
    assert all(isinstance(line, str) for line in payload["execution_package"]["interface"])
    import src.evidence_package as module
    monkeypatch.setattr(module, "default_artifact_loader",
                        lambda *_: pytest.fail("URI loader must never be invoked"))
    expected = project_routing_inspection(
        _package(), result.dispatches[0], now=NOW + timedelta(seconds=2))
    view = _inspect(payload, artifacts)
    metadata = view.pop("retained_evidence")
    assert view == expected
    assert metadata["attempt_receipt_hash"] == payload["attempt_receipts"][0]["receipt_hash"]
    assert metadata["evidence_package_hash"] == payload["evidence_package_hash"]
    assert metadata["attempt_verifications"][0]["outcome"] == "FAIL"
    assert metadata["validation"]["state"] == "VERIFIED"
    assert view["authoritative"] is False


@pytest.mark.parametrize("verified", [False, True])
def test_failed_and_unverified_attempts_do_not_become_storage_corruption(retained, verified):
    result, _, artifacts, _ = retained
    evidence = seal_evidence_package(
        evidence_package_id="synthetic-unverified", execution_package=_package(),
        dispatch_receipts=result.dispatches,
        attempt_receipts=result.attempts[:1],
        verification_receipts=result.verifications[:1] if verified else (),
        acceptance_context=result.evidence_package["acceptance_context"],
        sealed_at=NOW.isoformat()).to_dict()
    view = _inspect(evidence, artifacts)
    assert view["retained_evidence"]["validation"]["ok"] is False
    assert view["retained_evidence"]["validation"]["state"] == "INCOMPLETE"
    outcomes = view["retained_evidence"]["attempt_verifications"]
    assert [row["outcome"] for row in outcomes] == (["FAIL"] if verified else [])
    assert view["capabilities"]["status"] == "UNKNOWN"


@pytest.mark.parametrize("key", ["attempt_receipts", "dispatch_receipts"])
def test_duplicate_full_hash_matches_refuse_even_after_outer_reseal(retained, key):
    _, payload, artifacts, _ = retained
    payload[key].append(deepcopy(payload[key][0]))
    with pytest.raises(RoutingInspectionError):
        _inspect(_reseal(payload), artifacts)


@pytest.mark.parametrize("field,value", [
    ("run_id", "other-run"), ("packet_id", "other-packet"),
    ("execution_package_hash", "f" * 64), ("dispatch_receipt_hash", "f" * 64),
    ("target_id", "other-target"), ("host", "other-host"), ("model", "other-model"),
])
def test_rehashed_attempt_cannot_cross_its_exact_joins(retained, field, value):
    _, payload, artifacts, _ = retained
    payload["attempt_receipts"][0][field] = value
    _rehash(payload["attempt_receipts"][0])
    with pytest.raises(RoutingInspectionError):
        _inspect(_reseal(payload), artifacts)


@pytest.mark.parametrize("attempt", ["", "abc", "f" * 64, "A" * 64, "../" + "f" * 64])
def test_only_exact_full_hash_selection_is_supported(retained, attempt):
    _, payload, artifacts, _ = retained
    with pytest.raises(RoutingInspectionError):
        project_retained_routing_inspection(
            payload, artifacts=artifacts, attempt_receipt_hash=attempt, now=NOW)


def test_corrupted_historical_seal_refuses_after_outer_reseal(retained):
    _, payload, artifacts, _ = retained
    seal = next(iter(payload["acceptance_context"]["dispatch_evidence"].values()))
    seal["decision"]["selected_profile"]["model"] = "different-historical-model"
    with pytest.raises(RoutingInspectionError):
        _inspect(_reseal(payload), artifacts)


@pytest.mark.parametrize("mutation", ["missing", "empty_uri", "wrong_bytes", "baseline_missing"])
def test_artifact_preflight_refuses_before_any_uri_loader(retained, monkeypatch, mutation):
    _, payload, artifacts, _ = retained
    import src.evidence_package as module
    monkeypatch.setattr(module, "default_artifact_loader",
                        lambda *_: pytest.fail("URI loader must never be invoked"))
    row = payload["attempt_receipts"][0]
    ref = row["rendered_context_ref"]
    if mutation == "missing":
        artifacts.pop(ref["storage_uri"])
    elif mutation == "wrong_bytes":
        artifacts[ref["storage_uri"]] = b"different retained bytes"
    elif mutation == "empty_uri":
        artifacts[""] = artifacts.pop(ref["storage_uri"])
        ref["storage_uri"] = ""
        _rehash(row)
        _reseal(payload)
    else:
        baseline = deepcopy(payload["verification_receipts"][0])
        baseline["stdout_ref"]["storage_uri"] = "file:///synthetic/absent-baseline"
        _rehash(baseline, exclude=("outcome",))
        payload["acceptance_context"]["baseline_receipts"] = [baseline]
        _reseal(payload)
    with pytest.raises(RoutingInspectionError):
        _inspect(payload, artifacts)


def test_projection_and_payload_are_mutually_isolated(retained):
    _, payload, artifacts, _ = retained
    before = deepcopy(payload)
    view = _inspect(payload, artifacts)
    view["dispatch"]["candidates_considered"][0]["reason"] = "changed view"
    view["retained_evidence"]["attempt_verifications"].clear()
    assert payload == before
    assert _inspect(payload, artifacts)["retained_evidence"]["attempt_verifications"]


def test_outcome_scoring_version_and_aware_clock_remain_required(retained):
    _, payload, artifacts, _ = retained
    with pytest.raises(RoutingInspectionError, match="scoring_version"):
        _inspect(payload, artifacts, outcomes=(b"{}",))
    with pytest.raises(RoutingInspectionError, match="timezone-aware"):
        project_retained_routing_inspection(
            payload, artifacts=artifacts,
            attempt_receipt_hash=payload["attempt_receipts"][0]["receipt_hash"],
            now=NOW.replace(tzinfo=None))


def _supplemented(*, alias=False):
    result, _, _ = _chain()
    package = _package()
    prior = _bound(package)
    canonical = make_target_capability_receipt(
        host_id="synthetic-node-id" if alias else "synthetic-host",
        host=HostBaseline(ssh_host="synthetic-host"), observed_at=NOW.isoformat(),
        health="healthy", health_checked_at=NOW.isoformat(),
        runtime=RuntimeIdentity(runtime_kind="ollama", version="0.1", backend="cpu"),
        model=ModelIdentity(model_id="fixture-model", alias="fixture-model", digest="model-sha"),
        context=ContextProfile(safe_working_context=4096,
                               safe_context_source="synthetic retained fixture"),
        capabilities=CapabilityEvidence(measured=(
            routing.CAP_TEXT_GENERATION, routing.CAP_SINGLE_TOOL_CALL,
            routing.CAP_EXACT_REFERENCE_SEMANTICS)))
    profile = replace(prior.decision.selected_profile, profile_id=canonical.profile_id)
    refused = replace(profile, profile_id="synthetic-refused-profile",
                      model="different-model", roles=frozenset({routing.ROLE_SCOUT}))
    legacy = _legacy_view_from_receipt(canonical, profile, now=NOW)
    capacity = make_capacity_receipt(
        provider="synthetic-provider", pool_id="synthetic-pool",
        account_identity="synthetic-account", authorization_class=AuthorizationClass.API_KEY,
        entitlement=Entitlement.API, exposed_models=("fixture-model",),
        observed_at=NOW.isoformat(), ttl_seconds=3600, collector_id="synthetic-collector",
        evidence_source="synthetic-test", evidence_reference="synthetic-reference",
        state=CapacityState.AVAILABLE, concurrency_remaining=2)
    decision = routing.select_target(
        prior.request, profiles=(profile, refused), receipts=(legacy,),
        policy=prior.policy, now=NOW, decision_id="synthetic-retained-supplements")
    decision = replace(decision, capacity_receipt_refs=(capacity.ref,))
    decision = replace(decision, receipt_hash=routing.ps638_receipt_hash(
        decision.to_ps638_receipt_kwargs()))
    bound = replace(prior, decision=decision, capacity_receipts=(capacity,),
                    estate=TargetEstate(profiles=(profile, refused), receipts=(legacy,),
                                        canonical_receipts=(canonical,)))
    dispatch = make_dispatch_receipt(**decision.to_ps638_receipt_kwargs())
    attempt = make_attempt_receipt(**{
        **result.attempts[0].__dict__, "dispatch_receipt_hash": dispatch.receipt_hash})
    context = deepcopy(result.evidence_package["acceptance_context"])
    context["dispatch_evidence"] = {dispatch.receipt_hash: seal_dispatch_evidence(
        bound, attempts=(), invocations=(), include_canonical_receipts=True,
        sealed_at=NOW.isoformat())}
    payload = seal_evidence_package(
        evidence_package_id="synthetic-retained-supplements", execution_package=package,
        dispatch_receipts=(dispatch,), attempt_receipts=(attempt,),
        verification_receipts=result.verifications, acceptance_context=context,
        sealed_at=NOW.isoformat()).to_dict()
    return payload, dict(result.artifacts), canonical, capacity


def test_only_historical_supplements_and_all_same_target_pairs_are_preserved():
    payload, artifacts, canonical, capacity = _supplemented()
    view = _inspect(payload, artifacts)
    pairs = view["dispatch"]["candidates_considered"]
    assert len(pairs) == 2
    assert {row["target_id"] for row in pairs} == {"fixture-target"}
    assert {row["profile_id"] for row in pairs} == {
        canonical.profile_id, "synthetic-refused-profile"}
    assert any(not row["eligible"] and row["rule"] for row in pairs)
    assert view["capabilities"]["status"] == "OBSERVED"
    assert view["capabilities"]["supplements"][0]["receipt_hash"] == canonical.receipt_hash
    assert view["capacity"]["status"] == "OBSERVED"
    assert view["capacity"]["supplements"][0]["receipt_hash"] == capacity.receipt_hash
    assert view["capacity"]["billing_authority"] is False
    # Inspection uses the original observations even when they have since expired.
    expired = project_retained_routing_inspection(
        payload, artifacts=artifacts,
        attempt_receipt_hash=payload["attempt_receipts"][0]["receipt_hash"],
        now=NOW + timedelta(days=400))
    assert expired["capabilities"]["status"] == "UNKNOWN"
    assert expired["capacity"]["status"] == "UNKNOWN"
    assert expired["capabilities"]["supplements"][0]["observed_at"] == canonical.observed_at


def test_distinct_node_id_ssh_alias_keeps_existing_typed_false_refusal_visible():
    payload, artifacts, _, _ = _supplemented(alias=True)
    with pytest.raises(RoutingInspectionError, match="identity conflicts"):
        _inspect(payload, artifacts)


@pytest.mark.parametrize("mutation", ["inner_decision", "duplicate_capability", "duplicate_capacity"])
def test_resealed_historical_inconsistency_and_duplicate_supplements_refuse(mutation):
    payload, artifacts, _, _ = _supplemented()
    history = next(iter(payload["acceptance_context"]["dispatch_evidence"].values()))
    if mutation == "inner_decision":
        history["decision"]["selected_profile"]["model"] = "other-model"
    else:
        field = "canonical_capability_receipts" if mutation == "duplicate_capability" else "capacity_receipts"
        history[field].append(deepcopy(history[field][0]))
    core = {key: value for key, value in history.items() if key != "seal"}
    history["seal"]["evidence_hash"] = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    with pytest.raises(RoutingInspectionError):
        _inspect(_reseal(payload), artifacts)


def test_optional_missing_legacy_supplements_remain_unknown(retained):
    _, payload, artifacts, _ = retained
    view = _inspect(payload, artifacts)
    assert view["capabilities"]["status"] == "UNKNOWN"
    assert view["capabilities"]["references"]
    assert view["capabilities"]["supplements"] == []
    assert view["capacity"]["status"] == "UNKNOWN"
    assert view["capacity"]["supplements"] == []


def test_mixed_dispatch_outcome_remains_unassociated_for_each_exact_attempt(retained):
    result, payload, artifacts, prior = retained
    decision = replace(prior.decision, decision_id="synthetic-second-dispatch")
    decision = replace(decision, receipt_hash=routing.ps638_receipt_hash(
        decision.to_ps638_receipt_kwargs()))
    bound = replace(prior, decision=decision)
    dispatch = make_dispatch_receipt(**decision.to_ps638_receipt_kwargs())
    second = make_attempt_receipt(**{
        **result.attempts[1].__dict__, "dispatch_receipt_hash": dispatch.receipt_hash})
    context = deepcopy(payload["acceptance_context"])
    context["dispatch_evidence"][dispatch.receipt_hash] = seal_dispatch_evidence(
        bound, attempts=(), invocations=(), include_canonical_receipts=True,
        sealed_at=NOW.isoformat())
    payload = seal_evidence_package(
        evidence_package_id="synthetic-mixed-dispatch", execution_package=_package(),
        dispatch_receipts=(*result.dispatches, dispatch),
        attempt_receipts=(result.attempts[0], second),
        verification_receipts=result.verifications, acceptance_context=context,
        sealed_at=NOW.isoformat()).to_dict()
    outcome = build_outcome_record(evidence_package=payload, artifact_extensions=artifacts)
    assert len({row["dispatch_receipt_hash"] for row in payload["attempt_receipts"]}) == 2
    for attempt in payload["attempt_receipts"]:
        view = _inspect(payload, artifacts, attempt=attempt["receipt_hash"],
                        outcomes=(outcome,), scoring_version="synthetic-retained-v1")
        assert view["outcomes"]["status"] == "UNKNOWN"
        assert view["outcomes"]["associated_source_hashes"] == []
        assert view["outcomes"]["unassociated_source_hashes"] == [outcome.raw_record_hash]


def test_public_refusal_does_not_echo_malformed_private_input(retained):
    _, payload, artifacts, _ = retained
    payload["acceptance_context"]["dispatch_evidence"] = "private-input-canary"
    with pytest.raises(RoutingInspectionError) as error:
        _inspect(_reseal(payload), artifacts)
    assert "private-input-canary" not in str(error.value)


def test_duplicate_historical_legacy_receipts_are_not_silently_collapsed():
    payload, artifacts, _, _ = _supplemented()
    history = next(iter(payload["acceptance_context"]["dispatch_evidence"].values()))
    history["capability_receipts"].append(deepcopy(history["capability_receipts"][0]))
    core = {key: value for key, value in history.items() if key != "seal"}
    history["seal"]["evidence_hash"] = hashlib.sha256(json.dumps(
        core, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    with pytest.raises(RoutingInspectionError):
        _inspect(_reseal(payload), artifacts)


def test_unused_retained_baseline_receipt_still_requires_original_integrity(retained):
    _, payload, artifacts, _ = retained
    baseline = deepcopy(payload["verification_receipts"][0])
    baseline["receipt_hash"] = "f" * 64
    payload["acceptance_context"]["baseline_receipts"] = [baseline]
    with pytest.raises(RoutingInspectionError):
        _inspect(_reseal(payload), artifacts)


def test_original_v1_evidence_keeps_absent_historical_supplements_unknown():
    result, _, _ = _chain()
    payload = seal_evidence_package(
        evidence_package_id="synthetic-v1-evidence", execution_package=_package(),
        dispatch_receipts=result.dispatches, attempt_receipts=result.attempts,
        verification_receipts=result.verifications, sealed_at=NOW.isoformat()).to_dict()
    assert payload["schema_version"] == 1 and "acceptance_context" not in payload
    view = _inspect(payload, dict(result.artifacts))
    assert view["capabilities"]["status"] == "UNKNOWN"
    assert view["capacity"]["status"] == "UNKNOWN"
