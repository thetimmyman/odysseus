"""PS-605 — the production dispatch boundary, and its mutation controls.

Two kinds of proof, and both matter:

* the DECISION tests (hermetic, over a real SQLite estate) show what the boundary
  resolves and refuses, with per-candidate reasons;
* the DISPATCHER tests exercise ``routing_executor.execute_candidates`` itself — the
  production entrypoint — and assert that it attempts ONLY the decision's eligible
  candidates, in the decision's order, that a refusal stops the run with ZERO model
  calls, and that an invocation whose resolved model/locality contradicts the pin
  never reaches the network.

The mutation controls are the point of the last section: changing the selected
target, a capability receipt, or the policy revision after sealing must invalidate
the evidence, and a hosted invocation for local-only work must be impossible.
"""
from __future__ import annotations

import datetime
import dataclasses
import json

import pytest
import sqlalchemy
from sqlalchemy.orm import sessionmaker

import core.database as cdb
from src import dispatch_boundary as dbd
from src import dispatch_routing as dr
from src.local_targets import (CapabilityEvidence, ContextProfile, ModelIdentity,
                               RuntimeIdentity, TargetCapabilityReceipt)
from src.provider_capacity import (AuthorizationClass, CapacityState, Entitlement,
                                   make_capacity_receipt)

NOW = datetime.datetime(2026, 9, 15, 12, 0, tzinfo=datetime.timezone.utc)
#: Fixture profiles are "created" a little BEFORE the decision clock, so a declared
#: receipt is genuinely fresh and a negative age stays a failure case rather than
#: an accident of the fixture's date.
# The seed follows the pinned decision clock, not the wall clock.
SEEDED_AT = NOW.replace(tzinfo=None) - datetime.timedelta(minutes=30)


def _db():
    engine = sqlalchemy.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    cdb.Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False)()


def _seed(db, *, sensitivity="internal", allow_paid=False, allow_premium=False):
    """A real estate: a local tool-capable worker, a hosted scout, a deterministic node."""
    db.add_all([
        cdb.ModelEndpoint(id="ep-rtx", name="RTX4500 minipc",
                          base_url="http://192.168.1.130:11434/v1",
                          is_enabled=True, supports_tools=True),
        cdb.ModelEndpoint(id="ep-or", name="OpenRouter",
                          base_url="https://openrouter.ai/api/v1",
                          is_enabled=True, supports_tools=True),
        cdb.ModelEndpoint(id="ep-msr", name="MS-R1 verifier",
                          base_url="http://192.168.1.131:8080/v1",
                          is_enabled=True, supports_tools=None),
    ])
    db.add_all([
        cdb.RoutingModelProfile(
            id="p-rtx", model_endpoint_id="ep-rtx", model="qwen3.8:27b",
            roles=json.dumps(["implementer", "debugger", "scout", "reviewer"]),
            context_window=32768, max_output_tokens=4096, is_free=True,
            enabled=True, created_at=SEEDED_AT,
        ),
        cdb.RoutingModelProfile(
            id="p-openrouter", model_endpoint_id="ep-or", model="deepseek-v4-pro",
            roles=json.dumps(["scout", "reviewer"]), context_window=131072,
            is_free=False, is_premium=False, enabled=True, created_at=SEEDED_AT,
        ),
        cdb.RoutingModelProfile(
            id="p-msr", model_endpoint_id="ep-msr", model="qwen3.8:27b",
            roles=json.dumps(["governance_ci"]), context_window=4096,
            is_free=True, enabled=True, created_at=SEEDED_AT,
        ),
    ])
    task = cdb.RoutingTask(
        id="t-1", title="Implement the thing", objective="do the work",
        task_type="implementation", repo_path="/tmp/repo", risk="low",
        data_sensitivity=sensitivity, allow_free_models=True,
        allow_paid_models=allow_paid, allow_premium_models=allow_premium,
        max_attempts=3)
    db.add(task)
    db.commit()
    return task


def _candidates(*profile_ids):
    return [{"profile_id": pid, "model": "", "roles": [], "score": 1.0,
             "estimated_cost_usd": 0.0, "reasons": []} for pid in profile_ids]


class _FixtureCapabilityStore:
    """Canonical PS-632 fixture store; never uses legacy qualification data."""

    def current(self, profile_id):
        facts = {
            "p-rtx": ("ollama", "qwen3.8:27b", "http://192.168.1.130:11434/v1",
                      {dr.CAP_TEXT_GENERATION, dr.CAP_SINGLE_TOOL_CALL,
                       dr.CAP_EXACT_REFERENCE_SEMANTICS}),
            "p-openrouter": ("openrouter", "deepseek-v4-pro",
                             "https://openrouter.ai/api/v1",
                             {dr.CAP_TEXT_GENERATION, dr.CAP_SINGLE_TOOL_CALL,
                              dr.CAP_EXACT_REFERENCE_SEMANTICS}),
            "p-msr": ("msr1", "qwen3.8:27b", "http://192.168.1.131:8080/v1",
                      {dr.CAP_TEXT_GENERATION}),
        }
        if profile_id not in facts:
            return None
        provider, model, endpoint, caps = facts[profile_id]
        return TargetCapabilityReceipt(
            host_id=profile_id, profile_id=profile_id, observed_at=NOW.isoformat(),
            runtime=RuntimeIdentity(provider=provider, runtime_kind="ollama",
                                    version="0.32.11", endpoint_url=endpoint,
                                    endpoint_type="openai_compatible", backend="cuda"),
            model=ModelIdentity(model_id=model, alias=model, digest="fixture-digest"),
            context=ContextProfile(safe_working_context=32768),
            capabilities=CapabilityEvidence(measured=tuple(sorted(caps))),
            health="healthy", health_checked_at=NOW.isoformat(),
            qualification_ref="fixture-qualified")


def _fixture_store():
    return _FixtureCapabilityStore()


def _resolve(db, task, *profile_ids, **kwargs):
    return dbd.resolve_dispatch(db, task, _candidates(*profile_ids), now=NOW,
                                decision_id="dec-test", capability_store=_fixture_store(), **kwargs)


def _invocation(profile_id="p-rtx", *, model="qwen3.8:27b",
                chat_url="http://192.168.1.130:11434/v1", **overrides):
    values = {
        "profile_id": profile_id, "provider": "ollama",
        "runtime_kind": "ollama", "runtime_version": "0.32.11",
        "runtime_commit": "", "runtime_image_digest": "", "backend": "cuda",
        "backend_version": "", "model": model, "model_digest": "fixture-digest",
        "chat_url": chat_url, "endpoint_type": "openai_compatible",
        "locality": "local",
    }
    values.update(overrides)
    return dbd.InvocationIdentity(**values)


# ============================================================= the decision ===
def test_the_decision_is_resolved_over_the_real_estate():
    db = _db()
    task = _seed(db)
    bound = _resolve(db, task, "p-rtx", "p-openrouter", "p-msr")

    assert bound.decision.selected_profile.profile_id == "p-rtx"
    assert bound.decision.reason_code == dr.REASON_SELECTED_DETERMINISTIC
    rules = {a.profile_id: a.rule for a in bound.decision.candidates}
    assert rules["p-rtx"] == "eligible"
    assert rules["p-msr"] == dr.REFUSED_NOT_INFERENCE_TARGET
    # The role filter runs before the budget one, and the task's role is the first
    # preference the estate supports (implementer): a scout/reviewer-only profile is
    # refused for the ROLE, which is a different answer from "too expensive".
    assert rules["p-openrouter"] == dr.REFUSED_ROLE
    provenance = bound.estate.provenance_summary()
    assert provenance["p-rtx"] == dbd.PROVENANCE_MEASURED
    assert provenance["p-msr"] == dbd.PROVENANCE_MEASURED
    assert bound.policy.policy_ref.startswith("routing_policy@")


def test_database_candidate_without_canonical_ps632_receipt_is_not_routable():
    db = _db()
    task = _seed(db)
    with pytest.raises(dbd.DispatchBoundaryError, match="unqualified"):
        dbd.resolve_dispatch(db, task, _candidates("p-rtx"), now=NOW,
                             decision_id="dec-no-receipt")


def test_only_the_decisions_eligible_candidates_are_offered_to_the_dispatcher():
    db = _db()
    # allow_paid: the hosted profile is cost rank 1, so a free-only task would
    # (correctly) refuse it for BUDGET and this ordering control would be vacuous.
    task = _seed(db, allow_paid=True)
    task.task_type = "diff_review"      # role = reviewer: both rtx and openrouter fit
    db.commit()
    bound = _resolve(db, task, "p-rtx", "p-msr", "p-openrouter")
    order = [c["profile_id"] for c in bound.execution_order(
        _candidates("p-rtx", "p-msr", "p-openrouter"))]
    assert order == ["p-rtx", "p-openrouter"]      # MS-R1 is refused, not offered
    assert bound.pin_for("p-msr") is None
    assert bound.pin_for("p-rtx")["selected"] is True


def test_a_free_only_task_cannot_reach_a_premium_profile():
    """The task's own allow_* flags become a material budget fact."""
    db = _db()
    task = _seed(db, allow_premium=False)
    db.query(cdb.RoutingModelProfile).filter_by(id="p-openrouter").update(
        {"is_free": False, "is_premium": True,
         "roles": json.dumps(["implementer", "debugger"])})
    db.commit()
    bound = _resolve(db, task, "p-openrouter", "p-rtx")
    rules = {a.profile_id: a.rule for a in bound.decision.candidates}
    assert rules["p-openrouter"] == dr.REFUSED_BUDGET
    assert bound.decision.selected_profile.profile_id == "p-rtx"


def test_sensitive_work_refuses_hosted_and_selects_local():
    db = _db()
    task = _seed(db, sensitivity="restricted")
    bound = _resolve(db, task, "p-openrouter", "p-rtx")
    rules = {a.profile_id: a.rule for a in bound.decision.candidates}
    assert rules["p-openrouter"] == dr.REFUSED_POLICY_DENIED
    assert bound.decision.selected_profile.profile_id == "p-rtx"
    assert bound.decision.reason_code == dr.REASON_SELECTED_LOCAL_ONLY
    assert bound.request.local_only is True


def test_sensitive_work_with_no_local_profile_is_a_typed_refusal():
    db = _db()
    task = _seed(db, sensitivity="restricted")
    with pytest.raises(dr.RoutingRefused) as err:
        _resolve(db, task, "p-openrouter")
    assert err.value.code == dr.REFUSED_PRIVACY_LOCAL_ONLY
    assert err.value.assessments[0].eligible is False


def test_a_missing_or_disabled_candidate_is_recorded_not_silently_dropped():
    db = _db()
    task = _seed(db)
    bound = _resolve(db, task, "p-ghost", "p-rtx")
    assert [s["profile_id"] for s in bound.estate.skipped] == ["p-ghost"]
    assert bound.decision.selected_profile.profile_id == "p-rtx"


def test_an_approximate_receipt_cannot_satisfy_an_exact_request():
    db = _db()
    task = _seed(db)
    approximate = dr.make_legacy_capability_view(
        receipt_id="approx:p-rtx", profile_id="p-rtx", target_id="profile:p-rtx",
        capabilities=frozenset({dr.CAP_TEXT_GENERATION, dr.CAP_SINGLE_TOOL_CALL,
                                dr.CAP_EXACT_REFERENCE_SEMANTICS}),
        exactness=dr.EXACTNESS_APPROXIMATE, observed_at=NOW.isoformat(),
        ttl_s=86400, provenance=dr.PROVENANCE_MEASURED)
    with pytest.raises(dbd.DispatchBoundaryError):
        _resolve(db, task, "p-rtx", receipt_overrides={"p-rtx": approximate})


def test_a_stale_receipt_cannot_be_dispatched():
    db = _db()
    task = _seed(db)
    stale = dr.make_legacy_capability_view(
        receipt_id="measured:p-rtx", profile_id="p-rtx", target_id="profile:p-rtx",
        capabilities=frozenset({dr.CAP_TEXT_GENERATION,
                                dr.CAP_EXACT_REFERENCE_SEMANTICS,
                                dr.CAP_SINGLE_TOOL_CALL}),
        exactness=dr.EXACTNESS_EXACT, observed_at="2026-09-01T00:00:00+00:00",
        ttl_s=3600, provenance=dr.PROVENANCE_MEASURED)
    with pytest.raises(dbd.DispatchBoundaryError):
        _resolve(db, task, "p-rtx", receipt_overrides={"p-rtx": stale})


def test_a_measured_receipt_can_require_what_declaration_cannot_evidence():
    """The upgrade path PS-632 unlocks: context integrity must be MEASURED."""
    db = _db()
    task = _seed(db)
    with pytest.raises(dr.RoutingRefused) as err:
        _resolve(db, task, "p-rtx", capabilities=(dr.CAP_CONTEXT_INTEGRITY,))
    assert err.value.code == dr.REFUSED_CAPABILITY_MISSING
    measured = dr.make_legacy_capability_view(
        receipt_id="measured:p-rtx", profile_id="p-rtx", target_id="profile:p-rtx",
        capabilities=frozenset({dr.CAP_TEXT_GENERATION, dr.CAP_SINGLE_TOOL_CALL,
                                dr.CAP_EXACT_REFERENCE_SEMANTICS,
                                dr.CAP_CONTEXT_INTEGRITY}),
        exactness=dr.EXACTNESS_EXACT, observed_at=NOW.isoformat(), ttl_s=86400,
        provenance=dr.PROVENANCE_MEASURED)
    with pytest.raises(dbd.DispatchBoundaryError):
        _resolve(db, task, "p-rtx", capabilities=(dr.CAP_CONTEXT_INTEGRITY,),
                 receipt_overrides={"p-rtx": measured})


# ============================================================== the pin guard ===
def test_the_pin_guard_refuses_a_different_model_or_locality_before_dispatch():
    db = _db()
    task = _seed(db)
    bound = _resolve(db, task, "p-rtx")
    with pytest.raises(dbd.DispatchPinViolation) as err:
        dbd.verify_invocation(bound, invocation=_invocation(model="some-other-model"))
    assert err.value.code == dbd.PIN_MODEL_MISMATCH
    with pytest.raises(dbd.DispatchPinViolation) as err:
        dbd.verify_invocation(bound, invocation=_invocation(
            chat_url="https://openrouter.ai/api/v1", locality="hosted"))
    assert err.value.code == dbd.PIN_LOCALITY_MISMATCH
    with pytest.raises(dbd.DispatchPinViolation) as err:
        dbd.verify_invocation(bound, invocation=_invocation(
            profile_id="p-openrouter", provider="openrouter", runtime_kind="openrouter",
            model="deepseek-v4-pro", chat_url="https://openrouter.ai/api/v1",
            locality="hosted"))
    assert err.value.code == dbd.PIN_PROFILE_MISMATCH
    pin = dbd.verify_invocation(bound, invocation=_invocation())
    assert pin["target_id"] == "profile:p-rtx" and pin["locality"] == "local"


def test_the_pin_guard_refuses_a_different_local_endpoint_before_dispatch():
    db = _db()
    task = _seed(db)
    bound = _resolve(db, task, "p-rtx")
    with pytest.raises(dbd.DispatchPinViolation) as err:
        dbd.verify_invocation(bound, invocation=_invocation(
            chat_url="http://192.168.1.130:8732/v1"))
    assert err.value.code == dbd.PIN_ENDPOINT_MISMATCH


@pytest.mark.parametrize("field", ["provider", "runtime_kind", "runtime_version",
                                    "runtime_commit", "backend", "model_digest",
                                    "configured_context"])
def test_the_pin_guard_refuses_runtime_identity_mutation(field):
    db = _db()
    task = _seed(db)
    bound = _resolve(db, task, "p-rtx")
    values = {"provider": "other", "runtime_kind": "other",
              "runtime_version": "other", "runtime_commit": "other",
              "backend": "other", "model_digest": "other",
              "configured_context": 1234}
    with pytest.raises(dbd.DispatchPinViolation) as err:
        dbd.verify_invocation(
            bound, invocation=dataclasses.replace(_invocation(),
                                                   **{field: values[field]}))
    assert err.value.code == dbd.PIN_RUNTIME_MISMATCH


def test_legacy_estate_cannot_bypass_the_canonical_store():
    with pytest.raises(dbd.DispatchBoundaryError, match="canonical capability store"):
        dbd.resolve_from_estate(
            dbd.TargetEstate(profiles=(dr.make_target_profile(
                target_id="t", profile_id="p", provider="fixture"),), receipts=()),
            dr.RoutingRequest(domain="general_swe", role=dr.ROLE_IMPLEMENTER))


def test_canonical_receipt_projection_remains_dispatchable():
    db = _db()
    task = _seed(db)
    bound = _resolve(db, task, "p-rtx")
    rebuilt = dbd.resolve_from_estate(
        bound.estate, bound.request, capability_store=_fixture_store(),
        policy=bound.policy, now=NOW, decision_id="rebuilt")
    assert rebuilt.decision.selected_profile.profile_id == "p-rtx"


@pytest.mark.parametrize("field,value", [
    ("model", "foreign-model"), ("model_digest", "foreign-digest"),
    ("provider", "foreign-provider"), ("runtime_kind", "foreign-runtime"),
    ("backend", "foreign-backend"),
    ("endpoint_url", "http://192.168.1.130:9999/v1"),
    ("runtime_options", {"foreign": True}),
    ("configured_context", 1234), ("locality", "hosted"),
])
def test_profile_id_cannot_bear_foreign_execution_configuration(field, value):
    db = _db()
    task = _seed(db)
    bound = _resolve(db, task, "p-rtx")
    mutated = dataclasses.replace(
        bound.estate.profiles[0], **{field: value})
    estate = dataclasses.replace(bound.estate, profiles=(mutated,))
    with pytest.raises(dbd.DispatchBoundaryError,
                       match="profile_receipt_identity_mismatch"):
        dbd.resolve_from_estate(
            estate, bound.request, capability_store=_fixture_store(),
            policy=bound.policy, now=NOW, decision_id="mutated")


def test_profile_execution_options_cannot_smuggle_invocation_settings():
    db = _db()
    task = _seed(db)
    bound = _resolve(db, task, "p-rtx")
    mutated = dataclasses.replace(
        bound.estate.profiles[0],
        execution_options={"temperature": 0.7, "top_p": 0.5})
    with pytest.raises(dbd.DispatchBoundaryError, match="invocation_options_unbound"):
        dbd.resolve_from_estate(
            dataclasses.replace(bound.estate, profiles=(mutated,)),
            bound.request, capability_store=_fixture_store(),
            policy=bound.policy, now=NOW, decision_id="invocation-options")


def test_a_local_pin_cannot_resolve_to_a_hosted_url_even_for_the_same_model():
    db = _db()
    task = _seed(db, sensitivity="restricted")
    bound = _resolve(db, task, "p-rtx")
    # Local-only decision, but the endpoint table now points at a REMOTE url: same
    # profile id, same model, different locality -> REFUSED before the call.
    with pytest.raises(dbd.DispatchPinViolation) as err:
        dbd.verify_invocation(bound, invocation=_invocation(
            chat_url="https://openrouter.ai/api/v1", locality="hosted"))
    assert err.value.code == dbd.PIN_LOCALITY_MISMATCH


def test_the_attempt_binding_carries_every_ps638_field_and_the_receipt_hash():
    db = _db()
    task = _seed(db)
    bound = _resolve(db, task, "p-rtx")
    attempt = dbd.attempt_for(bound, attempt=1, profile_id="p-rtx", run_id="run-1")
    payload = attempt.to_dict()
    for field in dbd.PS638_ATTEMPT_BINDING_FIELDS:
        assert field in payload, field
    assert payload["dispatch_receipt_hash"] == bound.decision.receipt_hash
    assert payload["selected"] is True
    assert payload["execution_package_hash"] == bound.request.execution_package_hash


def test_an_attempt_cannot_be_bound_without_the_receipt_hash():
    with pytest.raises(dbd.DispatchBoundaryError):
        dbd.DispatchAttempt(run_id="r", packet_id="p", execution_package_hash="h",
                            attempt=1, dispatch_receipt_hash="", decision_id="d",
                            target_id="t", profile_id="p")


# ====================================================== the sealed evidence ===
def _sealed(bound, *, invocations=(), attempts=(), fixture=None):
    return dbd.seal_dispatch_evidence(
        bound, attempts=attempts, invocations=invocations,
        budget_snapshot={"max_cost_rank": 0, "selected_cost_rank": 0},
        resource_snapshot={"available": True}, fixture=fixture, sealed_at="T")


def _resolved_bound(**kwargs):
    db = _db()
    task = _seed(db, **kwargs)
    bound = _resolve(db, task, "p-rtx", "p-openrouter", "p-msr")
    attempt = dbd.attempt_for(bound, attempt=1, profile_id="p-rtx", run_id="run-1")
    return bound, attempt


def test_a_clean_seal_validates():
    bound, attempt = _resolved_bound()
    payload = _sealed(bound, attempts=(attempt,), invocations=(
        {"target_id": "profile:p-rtx", "locality": "local"},))
    ok, codes = dbd.validate_dispatch_evidence(payload)
    assert ok is True and codes == ()


def test_changing_the_selected_target_invalidates_the_evidence():
    bound, attempt = _resolved_bound()
    payload = _sealed(bound, attempts=(attempt,))
    payload["decision_receipt"]["selected_target_id"] = "profile:p-openrouter"
    ok, codes = dbd.validate_dispatch_evidence(payload)
    assert ok is False
    assert dbd.EVIDENCE_HASH_MISMATCH in codes
    assert dbd.EVIDENCE_PIN_CHANGED in codes


def test_changing_a_capability_receipt_invalidates_the_evidence():
    bound, attempt = _resolved_bound()
    payload = _sealed(bound, attempts=(attempt,))
    payload["capability_receipts"][0]["capabilities"] = ["text_generation"]
    ok, codes = dbd.validate_dispatch_evidence(payload)
    assert ok is False and dbd.EVIDENCE_RECEIPT_CHANGED in codes


def test_changing_the_policy_revision_invalidates_the_evidence():
    bound, attempt = _resolved_bound()
    payload = _sealed(bound, attempts=(attempt,))
    payload["policy"]["version"] = "9.9"
    payload["policy"]["policy_ref"] = "routing_policy@9.9+sha256:deadbeef"
    ok, codes = dbd.validate_dispatch_evidence(payload)
    assert ok is False and dbd.EVIDENCE_POLICY_CHANGED in codes


def test_an_attempt_bound_to_another_receipt_invalidates_the_evidence():
    bound, attempt = _resolved_bound()
    payload = _sealed(bound, attempts=(attempt,))
    payload["attempts"][0]["dispatch_receipt_hash"] = "0" * 64
    ok, codes = dbd.validate_dispatch_evidence(payload)
    assert ok is False and dbd.EVIDENCE_ATTEMPT_UNBOUND in codes


def test_an_attempt_on_a_different_target_invalidates_the_evidence():
    bound, attempt = _resolved_bound()
    payload = _sealed(bound, attempts=(attempt,))
    payload["attempts"][0]["target_id"] = "profile:elsewhere"
    ok, codes = dbd.validate_dispatch_evidence(payload)
    assert ok is False and dbd.EVIDENCE_TARGET_MISMATCH in codes


def test_an_invocation_outside_the_decision_invalidates_the_evidence():
    bound, attempt = _resolved_bound()
    payload = _sealed(bound, attempts=(attempt,), invocations=(
        {"target_id": "profile:somewhere-else", "locality": "local"},))
    ok, codes = dbd.validate_dispatch_evidence(payload)
    assert ok is False and dbd.EVIDENCE_INVOCATION_OUTSIDE_DECISION in codes


def test_a_hosted_invocation_for_local_only_work_invalidates_the_evidence():
    bound, attempt = _resolved_bound(sensitivity="restricted")
    payload = _sealed(bound, attempts=(attempt,), invocations=(
        {"target_id": "profile:p-rtx", "locality": "hosted"},))
    ok, codes = dbd.validate_dispatch_evidence(payload)
    assert ok is False and dbd.EVIDENCE_HOSTED_FOR_LOCAL_ONLY in codes


def test_the_recorder_proves_a_local_only_dispatch_made_no_hosted_call():
    recorder = dbd.InvocationRecorder()
    recorder.record(target_id="profile:p-rtx", locality="local", model="m")
    assert recorder.attempted() == 1 and recorder.hosted() == []
    recorder.assert_no_hosted()
    recorder.record(target_id="profile:p-openrouter", locality="hosted", model="m")
    with pytest.raises(dbd.DispatchBoundaryError):
        recorder.assert_no_hosted()


# =================================================== capacity evidence (T7) ===
# PS-640: capacity receipts must be part of the SEALED evidence, so removing or
# tampering with the capacity fact the decision relied on is detectable — and the
# capacity GATE (T1+T2) still refuses a hosted dispatch that has none.

HOSTED_PROFILE_ID = "clinepass-profile"
HOSTED_MODEL = "cline-3.5-pro"


def _hosted_profile(**overrides):
    fields = {
        "target_id": "profile:clinepass-profile", "profile_id": HOSTED_PROFILE_ID,
        "provider": "clinepass", "host": "api.clinepass.example",
        "runtime_kind": "openai_compatible", "runtime_version": "1.2.3",
        "model": HOSTED_MODEL, "model_digest": "hosted-model-digest",
        "backend": "saas", "endpoint_url": "https://api.clinepass.example/v1",
        "endpoint_type": "openai_compatible", "locality": dr.LOCALITY_HOSTED,
        "roles": frozenset({dr.ROLE_IMPLEMENTER}),
        "tools": frozenset({"write_file"}), "network_policy": "hosted-egress",
        "budget_class": "dev", "cost_rank": 1,
    }
    fields.update(overrides)
    return dr.make_target_profile(**fields)


class _HostedCapabilityStore:
    """A canonical PS-632 receipt whose locality is genuinely HOSTED.

    The default `TargetCapabilityReceipt.locality` is local-only, which would
    silently re-classify this profile as local and bypass the capacity gate —
    exactly what the negative control must not do.
    """

    def __init__(self, profile):
        self._profile = profile

    def current(self, profile_id):
        if profile_id != self._profile.profile_id:
            return None
        p = self._profile
        return TargetCapabilityReceipt(
            host_id="hosted-clinepass", profile_id=p.profile_id,
            observed_at=NOW.isoformat(),
            runtime=RuntimeIdentity(provider=p.provider, runtime_kind=p.runtime_kind,
                                    version=p.runtime_version, endpoint_url=p.endpoint_url,
                                    endpoint_type=p.endpoint_type, backend=p.backend),
            model=ModelIdentity(model_id=p.model, alias=p.model, digest=p.model_digest),
            context=ContextProfile(safe_working_context=32768),
            capabilities=CapabilityEvidence(measured=tuple(sorted({
                dr.CAP_TEXT_GENERATION, dr.CAP_SINGLE_TOOL_CALL,
                dr.CAP_EXACT_REFERENCE_SEMANTICS}))),
            health="healthy", health_checked_at=NOW.isoformat(),
            qualification_ref="fixture-qualified", locality=dr.LOCALITY_HOSTED)


def _capacity_receipt(**overrides):
    fields = {
        "provider": "clinepass", "pool_id": "clinepass:sub",
        "account_identity": "sub:primary",
        "authorization_class": AuthorizationClass.AGENT_SDK,
        "entitlement": Entitlement.AGENT_SDK,
        "exposed_models": (HOSTED_MODEL,), "observed_at": NOW.isoformat(),
        "ttl_seconds": 3600, "collector_id": "t",
        "evidence_source": "fixture", "evidence_reference": "c1",
        "state": CapacityState.AVAILABLE,
    }
    fields.update(overrides)
    return make_capacity_receipt(**fields)


def _hosted_bound(*, capacity_receipts):
    profile = _hosted_profile()
    request = dr.RoutingRequest(
        domain="general_swe", role=dr.ROLE_IMPLEMENTER, run_id="run-hosted",
        packet_id="P-hosted", execution_package_hash="pkg-hosted",
        required_tools=("write_file",), network_policy="hosted-egress",
        budget_class="dev", write_scope=("src/thing.py",), max_cost_rank=1)
    estate = dbd.TargetEstate(profiles=(profile,))
    return dbd.resolve_from_estate(
        estate, request, capability_store=_HostedCapabilityStore(profile),
        now=NOW, decision_id="dec-hosted", capacity_receipts=capacity_receipts)


def test_capacity_receipts_are_sealed_and_validate_clean():
    cap = _capacity_receipt()
    bound = _hosted_bound(capacity_receipts=(cap,))

    # The decision records the capacity receipt it relied on, and the bound
    # dispatch carries the receipts themselves into the evidence layer.
    assert bound.decision.capacity_receipt_refs == (cap.ref,)
    assert bound.capacity_receipts == (cap,)

    payload = _sealed(bound)
    assert [r["receipt_hash"] for r in payload["capacity_receipts"]] == [cap.receipt_hash]
    ok, codes = dbd.validate_dispatch_evidence(payload)
    assert ok is True and codes == ()


def test_removing_capacity_receipts_invalidates_the_evidence():
    bound = _hosted_bound(capacity_receipts=(_capacity_receipt(),))
    payload = _sealed(bound)
    assert payload["capacity_receipts"]
    payload["capacity_receipts"] = []
    ok, codes = dbd.validate_dispatch_evidence(payload)
    assert ok is False
    assert dbd.EVIDENCE_CAPACITY_CHANGED in codes


def test_hosted_dispatch_without_capacity_receipts_still_refuses():
    # The gate from T1+T2 is exercised, not bypassed by the evidence layer.
    with pytest.raises(dr.RoutingRefused) as err:
        _hosted_bound(capacity_receipts=())
    assert err.value.code == dr.REFUSED_CAPACITY_MISSING
