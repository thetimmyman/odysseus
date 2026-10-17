"""Alternate requests remain owned intent until canonical dispatch executes."""
import pytest
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from tests.test_canonical_inspector_admission import native_store as native_store
from tests.test_canonical_tmos_publication import retained_work


def routing_inputs(tmp_path, *, hosted_b=False):
    from src import dispatch_routing as routing
    from src.dispatch_boundary import TargetEstate
    from src.local_targets import (
        CapabilityEvidence, ContextProfile, ModelIdentity, RuntimeIdentity,
        PRIVACY_LOCAL_ONLY, make_target_capability_receipt,
    )
    from src.target_capability_store import TargetCapabilityStore

    now = datetime.now(timezone.utc)
    store = TargetCapabilityStore(str(tmp_path / "capabilities"))
    profiles = []
    for name, port in (("synthetic-a", 9998), ("synthetic-b", 9999)):
        hosted = hosted_b and name == "synthetic-b"
        endpoint = "https://alternate.example.test/v1" if hosted else f"http://127.0.0.1:{port}"
        provider = "openrouter" if hosted else "ollama"
        receipt = make_target_capability_receipt(
            host_id=name, profile_id="recomputed", observed_at=now.isoformat(),
            runtime=RuntimeIdentity(runtime_kind="ollama", provider=provider, version="fixture-v1",
                                    backend="fixture-backend", endpoint_url=endpoint, endpoint_type="ollama"),
            model=ModelIdentity(model_id="fixture-model", alias="fixture-model", digest="d" * 64),
            context=ContextProfile(configured_context=4096, configured_served_context=4096,
                safe_working_context=4096, safe_context_source="synthetic-only"),
            capabilities=CapabilityEvidence(measured=(routing.CAP_TEXT_GENERATION,
                routing.CAP_SINGLE_TOOL_CALL, routing.CAP_EXACT_REFERENCE_SEMANTICS)),
            locality=routing.LOCALITY_HOSTED if hosted else PRIVACY_LOCAL_ONLY,
            health="healthy", health_checked_at=now.isoformat(), qualification_ref="synthetic-only")
        store.append(receipt)
        profiles.append(routing.make_target_profile(
            target_id=name, profile_id=receipt.profile_id, provider=provider, host=name,
            runtime_kind="ollama", runtime_version="fixture-v1", backend="fixture-backend",
            model="fixture-model", model_digest="d" * 64, endpoint_url=endpoint, endpoint_type="ollama",
            locality=routing.LOCALITY_HOSTED if hosted else routing.LOCALITY_LOCAL,
            credential_sha256="c" * 64 if hosted else "",
            roles=frozenset({routing.ROLE_IMPLEMENTER}),
            tools=frozenset({"write_file"}), configured_context=4096, configured_served_context=4096,
            network_policy="offline", budget_class="dev"))
    return TargetEstate(profiles=tuple(profiles)), store, now, profiles


def alternate_context(store, tmp_path, *, hosted_b=False):
    from src import dispatch_routing as routing
    from src.execution_package import compute_package_hash

    work = retained_work(store, tmp_path)
    published = work.service.publish_tmos_work(work.owner, work.claim, work.execution, lambda: work.owner)
    view = work.service.inspect(work.owner, published["handle"], lambda: work.owner)
    estate, capabilities, now, profiles = routing_inputs(tmp_path, hosted_b=hosted_b)
    preference = store.request_alternate(work.owner, published["handle"], "tmos-routing-inspector-v1",
        view["dispatch_receipt_hash"], profiles[1].profile_id, "operator preference", "one")
    package = replace(work.package, run_id="next-owned-run")
    package = replace(package, package_hash=compute_package_hash(package.core()))
    request = routing.RoutingRequest(
        domain="general_swe", role=routing.ROLE_IMPLEMENTER,
        run_id=package.run_id, packet_id=package.packet_id, execution_package_hash=package.package_hash,
        required_tools=("write_file",), network_policy="offline", write_scope=package.write_scope,
        minimum_context_tokens=4096)
    return SimpleNamespace(work=work, view=view, estate=estate, capabilities=capabilities,
        now=now, profiles=profiles, preference=preference, request=request,
        policy=routing.policy_snapshot(policy={"routingPolicyVersion": "synthetic-preference-v1"}))


def test_alternate_request_is_durable_owned_intent_without_invented_dispatch(native_store, tmp_path):
    from sqlalchemy.orm import sessionmaker
    from src.canonical_inspector_admission import AdmissionStore

    store, engine = native_store
    work = retained_work(store, tmp_path)
    published = work.service.publish_tmos_work(work.owner, work.claim, work.execution, lambda: work.owner)
    view = work.service.inspect(work.owner, published["handle"], lambda: work.owner)
    request = store.request_alternate(work.owner, published["handle"], "tmos-routing-inspector-v1",
        view["dispatch_receipt_hash"], "another-profile", "independent eligible target", "request-one")
    assert request["action"] == "prefer_eligible_target"
    assert request["requested_profile_id"] == "another-profile"
    assert request["status"] == "REQUESTED"
    assert request["original_dispatch_receipt_hash"] == view["dispatch_receipt_hash"]
    assert request["attempt_receipt_hash"] == view["attempt_receipt_hash"]
    assert request["resulting_dispatch_receipt_hash"] is None
    reopened = AdmissionStore(sessionmaker(bind=engine, expire_on_commit=False))
    assert reopened.interventions(work.owner, published["handle"], "tmos-routing-inspector-v1") == [request]


def test_preference_replay_cannot_change_target_reason_or_action(native_store, tmp_path):
    from src.canonical_inspector_admission import UnknownInspection

    store, _ = native_store
    work = retained_work(store, tmp_path)
    published = work.service.publish_tmos_work(work.owner, work.claim, work.execution, lambda: work.owner)
    view = work.service.inspect(work.owner, published["handle"], lambda: work.owner)
    arguments = (work.owner, published["handle"], "tmos-routing-inspector-v1", view["dispatch_receipt_hash"])
    receipt = store.request_alternate(*arguments, "another-profile", "reason", "one")
    assert store.request_alternate(*arguments, "another-profile", "reason", "one") == receipt
    for profile, reason in (("different-profile", "reason"), ("another-profile", "changed reason")):
        with pytest.raises(UnknownInspection):
            store.request_alternate(*arguments, profile, reason, "one")
    with pytest.raises(UnknownInspection):
        store.request_escalation(*arguments, "reason", "one")


def test_foreign_and_mismatched_alternate_requests_do_not_touch_canonical_decision(native_store, tmp_path):
    from tests.test_canonical_inspector_admission import principal
    from src.canonical_inspector_admission import UnknownInspection

    store, _ = native_store
    work = retained_work(store, tmp_path)
    published = work.service.publish_tmos_work(work.owner, work.claim, work.execution, lambda: work.owner)
    view = work.service.inspect(work.owner, published["handle"], lambda: work.owner)
    for owner, generation, dispatch_hash in (
            (principal("other"), "tmos-routing-inspector-v1", view["dispatch_receipt_hash"]),
            (work.owner, "tmos-routing-inspector-v1", "f" * 64),
            (work.owner, "public-uppercase-v1", view["dispatch_receipt_hash"])):
        with pytest.raises(UnknownInspection):
            store.request_alternate(owner, published["handle"], generation, dispatch_hash,
                                    "another-profile", "reason", "refused")
    assert store.interventions(work.owner, published["handle"], "tmos-routing-inspector-v1") == []
    store.request_alternate(work.owner, published["handle"], "tmos-routing-inspector-v1",
                            view["dispatch_receipt_hash"], "another-profile", "reason", "one")
    after = work.service.inspect(work.owner, published["handle"], lambda: work.owner)
    assert after["routing"] == view["routing"]
    assert after["dispatch_receipt_hash"] == view["dispatch_receipt_hash"]


def test_eligible_preference_uses_canonical_selection_and_seals_its_actor_request(native_store, tmp_path):
    from src.execution_package import make_dispatch_receipt
    from src.inspector_alternate import select_requested_alternate

    store, _ = native_store
    context = alternate_context(store, tmp_path)
    decision = select_requested_alternate(context.request, context.preference, actor=context.work.owner,
        estate=context.estate, capability_store=context.capabilities, now=context.now, policy=context.policy)
    assert decision.decision.selected_profile.profile_id == context.profiles[1].profile_id
    assert all(candidate.eligible for candidate in decision.decision.candidates)
    receipt = make_dispatch_receipt(**decision.decision.to_ps638_receipt_kwargs())
    assert receipt.receipt_hash == decision.decision.receipt_hash
    assert receipt.decided_by == "ps605_policy"
    assert receipt.authority["operator_request_receipt_hash"] == context.preference["receipt_hash"]
    assert receipt.authority["acting_principal"] == context.work.owner.auth_key
    assert receipt.authority["original_dispatch_receipt_hash"] == context.view["dispatch_receipt_hash"]


def hosted_capacity(context, *, entitlement=None, observed_at=None):
    from src.provider_capacity import (
        AuthorizationClass, CapacityState, Entitlement, make_capacity_receipt,
    )

    profile = context.profiles[1]
    return make_capacity_receipt(
        provider=profile.provider, pool_id="synthetic-api-pool", account_identity="synthetic-account",
        credential_sha256=profile.credential_sha256, endpoint_url=profile.endpoint_url,
        authorization_class=AuthorizationClass.API_KEY,
        entitlement=entitlement or Entitlement.API, exposed_models=(profile.model,),
        observed_at=observed_at or context.now.isoformat(), ttl_seconds=3600,
        collector_id="synthetic", evidence_source="fixture", evidence_reference="fixture",
        state=CapacityState.AVAILABLE, concurrency_remaining=1)


def test_hosted_alternate_requires_fresh_usable_api_entitlement(native_store, tmp_path):
    from datetime import timedelta
    from src import dispatch_routing as routing
    from src.inspector_alternate import select_requested_alternate
    from src.provider_capacity import Entitlement

    store, _ = native_store
    context = alternate_context(store, tmp_path, hosted_b=True)
    arguments = dict(actor=context.work.owner, estate=context.estate,
        capability_store=context.capabilities, now=context.now, policy=context.policy)
    fresh = hosted_capacity(context)
    selected = select_requested_alternate(context.request, context.preference,
        capacity_receipts=(fresh,), **arguments)
    assert selected.decision.selected_profile.profile_id == context.profiles[1].profile_id
    for receipt, rule in (
            (hosted_capacity(context, entitlement=Entitlement.INTERACTIVE_NATIVE), routing.REFUSED_CAPACITY_UNUSABLE),
            (hosted_capacity(context, observed_at=(context.now - timedelta(seconds=3601)).isoformat()),
             routing.REFUSED_CAPACITY_STALE)):
        with pytest.raises(routing.RoutingRefused) as refused:
            select_requested_alternate(context.request, context.preference,
                capacity_receipts=(receipt,), **arguments)
        assert refused.value.code == rule
        assert receipt.observed_at != context.now.isoformat() or rule == routing.REFUSED_CAPACITY_UNUSABLE


@pytest.mark.parametrize("field", ["original_dispatch_receipt_hash", "attempt_receipt_hash"])
def test_malformed_resealed_preference_is_refused_before_reading_facts(native_store, tmp_path, field):
    import hashlib
    import json
    from src.canonical_inspector_admission import UnknownInspection
    from src.inspector_alternate import select_requested_alternate

    store, _ = native_store
    context = alternate_context(store, tmp_path)
    record = dict(context.preference)
    del record[field]
    del record["receipt_hash"]
    record["receipt_hash"] = hashlib.sha256(json.dumps(record, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    with pytest.raises(UnknownInspection):
        select_requested_alternate(context.request, record, actor=context.work.owner,
            estate=None, capability_store=None)


@pytest.mark.parametrize("refusal", ["permission", "capability", "liveness", "privacy", "capacity"])
def test_preference_cannot_execute_a_fallback_when_requested_target_is_refused(native_store, tmp_path, refusal):
    from datetime import timedelta
    from src import dispatch_routing as routing
    from src.inspector_alternate import select_requested_alternate
    from src.local_targets import CapabilityEvidence, make_target_capability_receipt

    store, _ = native_store
    context = alternate_context(store, tmp_path, hosted_b=refusal in ("privacy", "capacity"))
    profiles = context.profiles
    if refusal == "permission":
        profiles[1] = replace(profiles[1], tools=frozenset())
    if refusal in ("capability", "liveness"):
        previous = context.capabilities.current(profiles[1].profile_id)
        changes = ({"capabilities": CapabilityEvidence(measured=(routing.CAP_TEXT_GENERATION,
                       routing.CAP_EXACT_REFERENCE_SEMANTICS))}
                   if refusal == "capability" else
                   {"health_checked_at": (context.now - timedelta(seconds=301)).isoformat()})
        changed = make_target_capability_receipt(**{**previous.to_dict(), **changes})
        context.capabilities.append(changed, supersedes=previous.receipt_hash)
    request = replace(context.request, local_only=True) if refusal == "privacy" else context.request
    with pytest.raises(routing.RoutingRefused) as refused:
        select_requested_alternate(request, context.preference, actor=context.work.owner,
            estate=replace(context.estate, profiles=tuple(profiles)), capability_store=context.capabilities,
            now=context.now, policy=context.policy)
    assert refused.value.code == {
        "permission": routing.REFUSED_TOOL, "capability": routing.REFUSED_CAPABILITY_MISSING,
        "privacy": routing.REFUSED_PRIVACY_LOCAL_ONLY, "capacity": routing.REFUSED_CAPACITY_MISSING,
        "liveness": routing.REFUSED_RECEIPT_UNHEALTHY,
    }[refusal]
    if refusal == "liveness":
        still_stale = context.capabilities.current(profiles[1].profile_id)
        assert still_stale.health_state(now=context.now) == "liveness_expired"
        assert still_stale.observed_at == previous.observed_at


def two_target_work(store, tmp_path):
    from functools import partial
    from src.canonical_inspector_admission import PreparedTmosWork
    from src.canonical_inspector_service import InspectorService
    from src.canonical_pilot_adapter import PilotExecution
    from src.dispatch_boundary import resolve_from_estate
    from src.dispatch_routing import RoutingRequest, ROLE_IMPLEMENTER, policy_snapshot
    from src.evidence_io import retain_evidence_bundle
    from src.routing_inspector import project_retained_routing_inspection
    from tests.test_canonical_inspector_admission import principal
    from tests.test_canonical_tmos_publication import tmos_package
    from tests.test_ps638_consumer_chain import _chain

    package = tmos_package()
    estate, capabilities, now, profiles = routing_inputs(tmp_path)
    request = RoutingRequest(domain="general_swe", role=ROLE_IMPLEMENTER,
        run_id=package.run_id, packet_id=package.packet_id, execution_package_hash=package.package_hash,
        required_tools=package.allowed_tools, network_policy=package.network_policy,
        write_scope=package.write_scope, preferred_profile_ids=(profiles[0].profile_id,))
    policy = policy_snapshot(policy={"routingPolicyVersion": "synthetic-preference-v1"})
    bound = resolve_from_estate(estate, request, capability_store=capabilities, now=now, policy=policy)
    result, _, _ = _chain(package=package, bound=bound)
    assert len(result.attempts) == 1, result.refusal_reason
    owner = principal()
    prepared = PreparedTmosWork(package.run_id, package.packet_id, package.package_hash, "c" * 64)
    claim = store.admit_tmos_work(owner, "original", prepared)
    store.consume(claim, owner)
    store.begin_tmos_work(claim, owner)
    retained = tmp_path / "retained"
    retained.mkdir(mode=0o700)
    path = retain_evidence_bundle(retained, result.evidence_package, artifacts=result.artifacts)
    service = InspectorService(store, retained_root=retained,
        projector=partial(project_retained_routing_inspection, scoring_version="source-test-v1"),
        retention_parents=(tmp_path,), restored_disclosure_generation="tmos-routing-inspector-v1")
    published = service.publish_tmos_work(owner, claim,
        PilotExecution(path.stem, result.attempts[0].receipt_hash, True), lambda: owner)
    return SimpleNamespace(owner=owner, service=service, published=published, package=package,
        estate=estate, capabilities=capabilities, now=now, profiles=profiles, policy=policy)


def test_owned_alternate_endpoint_only_accepts_a_recorded_eligible_other_target(native_store, tmp_path, monkeypatch):
    from tests.test_canonical_inspector_routes import client_for

    store, _ = native_store
    work = two_target_work(store, tmp_path)
    view = work.service.inspect(work.owner, work.published["handle"], lambda: work.owner)
    _, _, client = client_for(tmp_path, monkeypatch, work.service)
    path = "/api/harness/inspector/attempts/" + work.published["handle"] + "/alternate"
    body = {"dispatch_receipt_hash": view["dispatch_receipt_hash"], "profile_id": work.profiles[1].profile_id,
        "reason": "independent eligible target", "idempotency_key": "one"}
    assert client.post(path, json=body).status_code == 403
    headers = {"x-test-user": "admin"}
    assert client.post(path, json=body, headers={"x-test-user": "other"}).status_code == 404
    assert client.post(path, json={**body, "owner": "admin"}, headers=headers).status_code == 404
    assert client.post(path, json={**body, "profile_id": work.profiles[0].profile_id}, headers=headers).status_code == 404
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 200
    assert response.json()["status"] == "REQUESTED"
    assert response.json()["receipt"]["requested_profile_id"] == work.profiles[1].profile_id
    assert response.json()["receipt"]["resulting_dispatch_receipt_hash"] is None
    assert work.service.inspect(work.owner, work.published["handle"], lambda: work.owner)[
        "dispatch_receipt_hash"] == view["dispatch_receipt_hash"]


def execute_alternate_source(store, work, *, wrong_profile=False):
    """Normal canonical loop, synthetic worker/verifier transports only."""
    from src.canonical_inspector_admission import PreparedTmosWork
    from src.canonical_pilot_adapter import PilotExecution
    from src.dispatch_routing import RoutingRequest, ROLE_IMPLEMENTER
    from src.evidence_io import retain_evidence_bundle
    from src.execution_package import compute_package_hash
    from src.inspector_alternate import select_requested_alternate
    from tests.test_ps638_consumer_chain import _chain

    view = work.service.inspect(work.owner, work.published["handle"], lambda: work.owner)
    preference = work.service.request_alternate(work.owner, work.published["handle"],
        view["dispatch_receipt_hash"], work.profiles[1].profile_id, "another eligible target", "one",
        lambda: work.owner)["receipt"]
    package = replace(work.package, run_id="owned-alternate-run")
    package = replace(package, package_hash=compute_package_hash(package.core()))
    request = RoutingRequest(domain="general_swe", role=ROLE_IMPLEMENTER,
        run_id=package.run_id, packet_id=package.packet_id, execution_package_hash=package.package_hash,
        required_tools=package.allowed_tools, network_policy=package.network_policy, write_scope=package.write_scope)
    bound = select_requested_alternate(request, preference, actor=work.owner,
        estate=work.estate, capability_store=work.capabilities, now=work.now, policy=work.policy)
    if wrong_profile:
        from src.dispatch_boundary import resolve_from_estate
        from src.dispatch_routing import ps638_receipt_hash
        wrong = resolve_from_estate(work.estate, replace(request, preferred_profile_ids=(work.profiles[0].profile_id,)),
            capability_store=work.capabilities, now=work.now, policy=work.policy)
        decision = replace(wrong.decision, authority=bound.decision.authority)
        decision = replace(decision, receipt_hash=ps638_receipt_hash(decision.to_ps638_receipt_kwargs()))
        bound = replace(wrong, decision=decision)
    prepared = PreparedTmosWork(package.run_id, package.packet_id, package.package_hash, "c" * 64)
    claim = store.admit_tmos_work(work.owner, "alternate-run", prepared)
    store.consume(claim, work.owner)
    store.begin_tmos_alternate(claim, work.owner, work.published["handle"], preference["receipt_hash"])
    result, _, _ = _chain(package=package, bound=bound)
    assert len(result.attempts) == 1, result.refusal_reason
    assert result.attempts[0].dispatch_receipt_hash == bound.decision.receipt_hash
    path = retain_evidence_bundle(work.service._root, result.evidence_package, artifacts=result.artifacts)
    return SimpleNamespace(claim=claim, preference=preference, result=result, bound=bound,
        execution=PilotExecution(path.stem, result.attempts[0].receipt_hash, True))


def test_alternate_result_joins_owned_request_new_decision_and_actual_retained_attempt(native_store, tmp_path):
    from sqlalchemy.orm import sessionmaker
    from src.canonical_inspector_admission import AdmissionStore

    store, engine = native_store
    work = two_target_work(store, tmp_path)
    alternate = execute_alternate_source(store, work)
    published = work.service.publish_tmos_alternate(work.owner, work.published["handle"],
        alternate.preference["receipt_hash"], alternate.claim, alternate.execution, lambda: work.owner)
    assert published["status"] == "retained"
    assert published["handle"] != work.published["handle"]
    new = work.service.inspect(work.owner, published["handle"], lambda: work.owner)
    assert new["dispatch_receipt_hash"] == alternate.bound.decision.receipt_hash
    assert new["attempt_receipt_hash"] == alternate.result.attempts[0].receipt_hash
    assert new["routing"]["decision"]["operator_action"] == {
        "action": "prefer_eligible_target", "acting_principal": work.owner.auth_key,
        "operator_request_receipt_hash": alternate.preference["receipt_hash"],
        "original_dispatch_receipt_hash": alternate.preference["original_dispatch_receipt_hash"],
        "original_attempt_receipt_hash": alternate.preference["attempt_receipt_hash"],
    }
    original = work.service.inspect(work.owner, work.published["handle"], lambda: work.owner)
    requests = [row for row in original["interventions"] if row["status"] == "REQUESTED"]
    applied = [row for row in original["interventions"] if row["status"] == "APPLIED"]
    assert requests == [alternate.preference]
    assert len(applied) == 1
    assert applied[0]["operator_request_receipt_hash"] == alternate.preference["receipt_hash"]
    assert applied[0]["original_dispatch_receipt_hash"] == original["dispatch_receipt_hash"]
    assert applied[0]["resulting_dispatch_receipt_hash"] == new["dispatch_receipt_hash"]
    assert applied[0]["resulting_attempt_receipt_hash"] == new["attempt_receipt_hash"]
    assert applied[0]["resulting_handle"] == published["handle"]
    reopened = AdmissionStore(sessionmaker(bind=engine, expire_on_commit=False))
    assert reopened.interventions(work.owner, work.published["handle"], "tmos-routing-inspector-v1") == original["interventions"]


@pytest.mark.parametrize("failure", ["attempt", "cleanup", "wrong-profile", "request", "foreign", "restart", "revoked", "ordinary-publication"])
def test_uncertain_or_mismatched_alternate_never_records_application_or_releases_custody(native_store, tmp_path, failure):
    from sqlalchemy.orm import sessionmaker
    from src.canonical_inspector_admission import AdmissionStore, InspectorUnavailable, UnknownInspection, PreparedTmosWork
    from tests.test_canonical_inspector_admission import principal

    store, engine = native_store
    work = two_target_work(store, tmp_path)
    alternate = execute_alternate_source(store, work, wrong_profile=failure == "wrong-profile")
    result = alternate.execution
    owner = principal("other") if failure == "foreign" else work.owner
    request_hash = "f" * 64 if failure == "request" else alternate.preference["receipt_hash"]
    if failure == "attempt":
        result = replace(result, attempt_hash="f" * 64)
    if failure == "cleanup":
        result = replace(result, cleanup_confirmed=False)
    if failure == "restart":
        work.service._store = AdmissionStore(sessionmaker(bind=engine, expire_on_commit=False))
    if failure == "revoked":
        store.revoke(work.owner, work.published["handle"])
    with pytest.raises((InspectorUnavailable, UnknownInspection)):
        if failure == "ordinary-publication":
            work.service.publish_tmos_work(owner, alternate.claim, result, lambda: owner)
        else:
            work.service.publish_tmos_alternate(owner, work.published["handle"], request_hash,
                alternate.claim, result, lambda: owner)
    if failure != "revoked":
        assert store.interventions(work.owner, work.published["handle"], "tmos-routing-inspector-v1") == [alternate.preference]
    with pytest.raises(InspectorUnavailable):
        store.admit_tmos_work(work.owner, "another", PreparedTmosWork(
            "another-run", work.package.packet_id, "a" * 64, "c" * 64))


def test_result_write_failure_rolls_back_the_new_locator_and_keeps_the_started_claim(native_store, tmp_path):
    from sqlalchemy import event
    from src.canonical_inspector_admission import InspectorUnavailable

    store, engine = native_store
    work = two_target_work(store, tmp_path)
    alternate = execute_alternate_source(store, work)

    def fail_result(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO canonical_inspector_intervention"):
            raise OSError("synthetic write failure")

    event.listen(engine, "before_cursor_execute", fail_result)
    with pytest.raises(InspectorUnavailable):
        work.service.publish_tmos_alternate(work.owner, work.published["handle"],
            alternate.preference["receipt_hash"], alternate.claim, alternate.execution, lambda: work.owner)
    event.remove(engine, "before_cursor_execute", fail_result)
    assert store.interventions(work.owner, work.published["handle"], "tmos-routing-inspector-v1") == [alternate.preference]
    # Same completed source result, no provider re-execution. An orphaned
    # committed locator would make this exact one-use publication refuse.
    published = work.service.publish_tmos_alternate(work.owner, work.published["handle"],
        alternate.preference["receipt_hash"], alternate.claim, alternate.execution, lambda: work.owner)
    assert work.service.inspect(work.owner, published["handle"], lambda: work.owner)[
        "attempt_receipt_hash"] == alternate.execution.attempt_hash


def test_applied_request_cannot_start_a_second_invocation(native_store, tmp_path):
    from src.canonical_inspector_admission import InspectorUnavailable, PreparedTmosWork

    store, _ = native_store
    work = two_target_work(store, tmp_path)
    alternate = execute_alternate_source(store, work)
    with pytest.raises(InspectorUnavailable):
        store.begin_tmos_alternate(alternate.claim, work.owner, work.published["handle"], alternate.preference["receipt_hash"])
    work.service.publish_tmos_alternate(work.owner, work.published["handle"],
        alternate.preference["receipt_hash"], alternate.claim, alternate.execution, lambda: work.owner)
    claim = store.admit_tmos_work(work.owner, "second-run", PreparedTmosWork(
        "second-invocation", work.package.packet_id, "a" * 64, "c" * 64))
    store.consume(claim, work.owner)
    with pytest.raises(InspectorUnavailable):
        store.begin_tmos_alternate(claim, work.owner, work.published["handle"], alternate.preference["receipt_hash"])


def test_default_alternate_endpoint_remains_unavailable_and_real_cookie_gated(tmp_path, monkeypatch):
    from tests.test_canonical_inspector_routes import client_for

    monkeypatch.setenv("CANONICAL_INSPECTOR_ENABLED", "true")
    _, _, client = client_for(tmp_path, monkeypatch)
    path = "/api/harness/inspector/attempts/unknown/alternate"
    assert client.post(path, json={}).status_code == 403
    assert client.post(path, json={}, headers={"x-test-user": "admin", "x-test-bearer": "yes"}).status_code == 403
    assert client.post(path, json={}, headers={"x-test-user": "admin"}).status_code == 503
