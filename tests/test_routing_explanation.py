"""Deterministic routing explanation and audited operator steering.

Every explanation is a projection of canonical receipts, and the negative
controls are the point of this file:

  * a post-hoc rationale has nowhere to come from — the module imports no model
    client and no network library, and every code it emits is the selector's own;
  * an explanation and the decision it describes cannot disagree, because the
    evaluator calls the selector rather than re-deriving eligibility;
  * an override cannot convert a DENY into an ALLOW: privacy, policy denial,
    credential isolation, missing entitlement and stale evidence all refuse, and
    mint nothing;
  * a hash-sealed receipt that *claims* eligibility is still refused, because
    the gate re-derives the rule instead of trusting the flag;
  * capability evidence that has expired cannot be overridden into freshness.

`test_the_positive_control_explains_a_deterministic_selection` and
`test_an_override_between_eligible_candidates_mints_an_immutable_receipt` are
the controls: without them, an evaluator that refused everything would pass.
"""
from __future__ import annotations

import ast
import dataclasses
import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from src import dispatch_routing as dr
from src.execution_package import _canonical
from src.provider_capacity import (
    AuthorizationClass, CapacityState, Entitlement, make_capacity_receipt,
)
from src.routing_explanation import (
    ANNOTATABLE_REFUSALS, NON_OVERRIDABLE_REFUSALS, OVERRIDE_AUTHORIZES,
    OVERRIDE_SCOPE, REQUIRED_CHANGE, RULE_ELIGIBLE, CandidateExplanation,
    ExplanationError, PolicyViolationException, RoutingExplanation,
    RoutingInspectorEvaluator, apply_operator_override,
    explanation_hash_is_valid, override_receipt_hash_is_valid,
    routing_explanation_from_dict,
)

NOW = datetime.datetime(2026, 9, 15, 12, 0, tzinfo=datetime.timezone.utc)
POLICY = dr.policy_snapshot(policy={"routingPolicyVersion": "1.2",
                                    "remoteSensitivityCeiling": "confidential"})
MODULE_SOURCE = Path("src/routing_explanation.py")

IMPLEMENTER_CAPS = frozenset({
    dr.CAP_TEXT_GENERATION, dr.CAP_SINGLE_TOOL_CALL,
    dr.CAP_EXACT_REFERENCE_SEMANTICS,
})

#: Hosted pool shared by the entitlement tests; one credential, one endpoint.
HOSTED_MODEL = "qwen3.8:27b"
HOSTED_ENDPOINT = "https://openrouter.ai/api/v1"
HOSTED_CREDENTIAL = "a" * 64


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reseal(explanation: RoutingExplanation) -> RoutingExplanation:
    """Re-hash a hand-built record, modelling a tamperer who can rewrite storage."""
    return dataclasses.replace(
        explanation,
        explanation_hash=_sha256_hex(_canonical(explanation.core())))


def profile(**overrides: Any):
    fields: dict[str, Any] = {
        "target_id": "local-a", "profile_id": "prof-a", "provider": "ollama",
        "host": "gpu-host", "runtime_kind": "ollama", "runtime_version": "0.32.11",
        "model": "qwen3.8:27b", "model_digest": "d94d9646", "backend": "cuda",
        "endpoint_url": "http://127.0.0.1:11434/v1",
        "locality": dr.LOCALITY_LOCAL,
        "roles": frozenset({dr.ROLE_IMPLEMENTER}),
        "tools": frozenset({"write_file"}), "network_policy": "private-loopback",
        "budget_class": "dev", "cost_rank": 0,
    }
    fields.update(overrides)
    return dr.make_target_profile(**fields)


def receipt(**overrides: Any):
    fields: dict[str, Any] = {
        "receipt_id": "cap-a", "profile_id": "prof-a", "target_id": "local-a",
        "capabilities": IMPLEMENTER_CAPS, "exactness": dr.EXACTNESS_EXACT,
        "observed_at": "2026-09-15T11:00:00+00:00", "ttl_s": 86400, "healthy": True,
        "host": "gpu-host", "runtime_version": "0.32.11", "model_digest": "d94d9646",
    }
    fields.update(overrides)
    return dr.make_legacy_capability_view(**fields)


def request(**overrides: Any):
    fields: dict[str, Any] = {
        "domain": "general_swe", "role": dr.ROLE_IMPLEMENTER, "run_id": "run-1",
        "packet_id": "P-1", "execution_package_hash": "pkg-hash-1",
        "required_tools": ("write_file",), "network_policy": "private-loopback",
        "budget_class": "dev", "write_scope": ("src/thing.py",),
    }
    fields.update(overrides)
    return dr.RoutingRequest(**fields)


def bare_request(**overrides: Any):
    """A request that constrains only capability, so entitlement is the variable."""
    fields: dict[str, Any] = {"domain": "general_swe", "role": dr.ROLE_IMPLEMENTER,
                              "run_id": "run-1", "packet_id": "P-1"}
    fields.update(overrides)
    return dr.RoutingRequest(**fields)


def hosted_profile(target_id: str, profile_id: str, **overrides: Any):
    fields: dict[str, Any] = {
        "target_id": target_id, "profile_id": profile_id, "provider": "openrouter",
        "host": "api.openrouter.ai", "runtime_kind": "openai-compatible",
        "model": HOSTED_MODEL, "locality": dr.LOCALITY_HOSTED,
        "endpoint_url": HOSTED_ENDPOINT, "credential_sha256": HOSTED_CREDENTIAL,
        "roles": frozenset({dr.ROLE_IMPLEMENTER}), "cost_rank": 1,
    }
    fields.update(overrides)
    return dr.make_target_profile(**fields)


def hosted_receipt(target_id: str, profile_id: str, **overrides: Any):
    fields: dict[str, Any] = {"receipt_id": f"cap-{target_id}",
                              "profile_id": profile_id, "target_id": target_id,
                              "capabilities": IMPLEMENTER_CAPS,
                              "observed_at": "2026-09-15T11:00:00+00:00",
                              "ttl_s": 86400, "healthy": True,
                              "host": "api.openrouter.ai"}
    fields.update(overrides)
    return dr.make_legacy_capability_view(**fields)


def capacity_receipt(**overrides: Any):
    fields: dict[str, Any] = {
        "provider": "openrouter", "pool_id": "openrouter:subscription",
        "account_identity": "subscription:primary",
        "credential_sha256": HOSTED_CREDENTIAL, "endpoint_url": HOSTED_ENDPOINT,
        "authorization_class": AuthorizationClass.AGENT_SDK,
        "entitlement": Entitlement.AGENT_SDK,
        "exposed_models": (HOSTED_MODEL,), "observed_at": NOW.isoformat(),
        "ttl_seconds": 3600, "collector_id": "test-collector",
        "evidence_source": "fixture", "evidence_reference": "capacity-1",
        "state": CapacityState.AVAILABLE, "concurrency_remaining": 4,
    }
    fields.update(overrides)
    return make_capacity_receipt(**fields)


def evaluator(*, capacity=(), now=NOW, policy=POLICY):
    return RoutingInspectorEvaluator(
        capacity_receipts=capacity, policy=policy, now=now)


def two_locals(*, second_ttl: int = 86400,
               second_observed: str = "2026-09-15T11:00:00+00:00"):
    """The canonical selection plus one eligible alternative."""
    alt = profile(target_id="local-b", profile_id="prof-b", cost_rank=1)
    alt_receipt = receipt(receipt_id="cap-b", profile_id="prof-b",
                          target_id="local-b", ttl_s=second_ttl,
                          observed_at=second_observed)
    return [profile(), alt], [receipt(), alt_receipt]


def explain_two_locals() -> RoutingExplanation:
    profiles, receipts = two_locals()
    return evaluator().evaluate(request(), profiles, receipts)


# --------------------------------------------------------------------------
# Positive control: the engine explains a real deterministic selection.
# --------------------------------------------------------------------------

def test_the_positive_control_explains_a_deterministic_selection():
    explanation = explain_two_locals()
    assert explanation.selected_target_id == "local-a"
    assert explanation.eligible_target_ids() == ("local-a", "local-b")
    assert explanation.rationale == (dr.REASON_SELECTED_DETERMINISTIC,)
    assert explanation.refusal_code == ""
    assert len(explanation.dispatch_receipt_hash) == 64
    assert explanation_hash_is_valid(explanation.to_dict())
    # Every alternative is present, not just the winner.
    assert [c.target_id for c in explanation.considered] == ["local-a", "local-b"]
    for candidate in explanation.considered:
        assert candidate.eligible is True
        assert candidate.rule == RULE_ELIGIBLE
        assert candidate.refusal_reasons == ()


def test_identical_evidence_yields_an_identical_explanation():
    """Determinism is what makes the hash evidence rather than decoration."""
    first = explain_two_locals()
    second = explain_two_locals()
    assert first.explanation_hash == second.explanation_hash
    assert _canonical(first.to_dict()) == _canonical(second.to_dict())


def test_candidate_order_does_not_change_the_explanation_hash():
    profiles, receipts = two_locals()
    forward = evaluator().evaluate(request(), profiles, receipts)
    backward = evaluator().evaluate(request(), list(reversed(profiles)),
                                    list(reversed(receipts)))
    assert forward.explanation_hash == backward.explanation_hash


def test_different_evidence_yields_a_different_explanation_hash():
    fresh = explain_two_locals()
    stale = evaluator().evaluate(
        request(), *two_locals(second_observed="2026-09-14T00:00:00+00:00"))
    assert fresh.explanation_hash != stale.explanation_hash
    assert stale.eligible_target_ids() == ("local-a",)


# --------------------------------------------------------------------------
# The explanation is a projection, never generated prose.
# --------------------------------------------------------------------------

def test_the_module_cannot_consult_a_model_or_the_network():
    """The structural guarantee behind "no post-hoc LLM rationale"."""
    tree = ast.parse(MODULE_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"openai", "anthropic", "google", "litellm", "cohere", "mistralai",
                 "ollama", "requests", "httpx", "aiohttp", "urllib", "socket",
                 "http", "subprocess", "asyncio"}
    assert not (imported & forbidden), f"forbidden import(s): {imported & forbidden}"


def test_every_reason_is_the_canonical_selectors_own_words():
    """An explanation can never disagree with the decision it explains."""
    profiles, receipts = two_locals()
    decision = dr.select_target(request(), profiles=profiles, receipts=receipts,
                                policy=POLICY, now=NOW)
    # Pinned to the real decision id, so the explanation cites that receipt.
    explanation = evaluator().evaluate(request(), profiles, receipts,
                                       decision_id=decision.decision_id)
    canonical = {(a.target_id, a.profile_id): a for a in decision.candidates}
    assert explanation.considered
    for candidate in explanation.considered:
        assessment = canonical[(candidate.target_id, candidate.profile_id)]
        assert candidate.rule == assessment.rule
        assert candidate.reason == assessment.reason
        assert candidate.eligible == assessment.eligible
        assert candidate.eligible == (candidate.rule == RULE_ELIGIBLE)
        assert (candidate.rule in dr.KNOWN_REFUSALS
                or candidate.rule == RULE_ELIGIBLE)
    assert explanation.rationale == (decision.reason_code,)
    assert explanation.dispatch_receipt_hash == decision.receipt_hash


def test_the_explanation_carries_the_canonical_receipt_identity():
    profiles, receipts = two_locals()
    explanation = evaluator().evaluate(request(), profiles, receipts)
    by_target = {c.target_id: c for c in explanation.considered}
    assert by_target["local-a"].receipt_hash == receipts[0].receipt_hash
    assert by_target["local-b"].receipt_hash == receipts[1].receipt_hash
    assert by_target["local-a"].receipt_observed_at == receipts[0].observed_at
    assert by_target["local-a"].receipt_ttl_s == receipts[0].ttl_s


# --------------------------------------------------------------------------
# Alternates are explained, with codes and a required change.
# --------------------------------------------------------------------------

def test_the_explanation_says_why_each_alternate_was_filtered_out():
    stale = profile(target_id="local-stale", profile_id="prof-stale")
    stale_receipt = receipt(receipt_id="cap-stale", profile_id="prof-stale",
                            target_id="local-stale", ttl_s=60)
    wrong_role = profile(target_id="local-verifier", profile_id="prof-verifier",
                         roles=frozenset({dr.ROLE_VERIFIER}))
    wrong_role_receipt = receipt(receipt_id="cap-verifier",
                                 profile_id="prof-verifier",
                                 target_id="local-verifier")
    explanation = evaluator().evaluate(
        request(),
        [profile(), stale, wrong_role],
        [receipt(), stale_receipt, wrong_role_receipt])

    assert explanation.eligible_target_ids() == ("local-a",)
    by_target = {c.target_id: c for c in explanation.considered}
    assert by_target["local-stale"].eligible is False
    assert by_target["local-stale"].refusal_reasons == (dr.REFUSED_RECEIPT_STALE,)
    assert by_target["local-stale"].hard_refusals == (dr.REFUSED_RECEIPT_STALE,)
    assert (by_target["local-stale"].required_change_to_qualify
            == REQUIRED_CHANGE[dr.REFUSED_RECEIPT_STALE])
    assert by_target["local-verifier"].refusal_reasons == (dr.REFUSED_ROLE,)
    assert by_target["local-verifier"].hard_refusals == ()
    assert by_target["local-verifier"].required_change_to_qualify
    assert all(c.target_id != "local-stale"
               for c in explanation.eligible_candidates())


def test_a_refusal_is_explained_with_its_code_and_no_selection():
    hosted = hosted_profile("hosted-a", "prof-ha")
    explanation = evaluator().evaluate(
        request(local_only=True), [hosted],
        [hosted_receipt("hosted-a", "prof-ha")])
    assert explanation.selected is None
    assert explanation.refusal_code == dr.REFUSED_PRIVACY_LOCAL_ONLY
    assert explanation.rationale == (dr.REFUSED_PRIVACY_LOCAL_ONLY,)
    assert explanation.eligible_target_ids() == ()
    assert explanation.dispatch_receipt_hash == ""
    assert explanation_hash_is_valid(explanation.to_dict())
    assert explanation.considered[0].refusal_reasons == (
        dr.REFUSED_PRIVACY_LOCAL_ONLY,)


def test_the_sensitive_domain_refusal_names_the_policy_rule():
    hosted = hosted_profile("hosted-a", "prof-ha")
    explanation = evaluator().evaluate(
        request(domain="personal"), [hosted],
        [hosted_receipt("hosted-a", "prof-ha")])
    assert explanation.selected is None
    # The aggregate refusal is the privacy one: nothing local is eligible.
    assert explanation.refusal_code == dr.REFUSED_PRIVACY_LOCAL_ONLY
    # The candidate itself was refused by the domain policy, and says so.
    assert explanation.considered[0].refusal_reasons == (dr.REFUSED_POLICY_DENIED,)
    assert "sensitive-domain-local-only" in explanation.considered[0].reason
    assert explanation.considered[0].hard_refusals == (dr.REFUSED_POLICY_DENIED,)


def test_the_entitlement_matrix_separates_entitled_from_unentitled_targets():
    profiles = [hosted_profile("hosted-a", "prof-ha"),
                hosted_profile("hosted-b", "prof-hb"),
                hosted_profile("hosted-exhausted", "prof-hx",
                               model="deepseek-v4.1"),
                hosted_profile("hosted-none", "prof-hn", model="gemma-3-27b")]
    receipts = [hosted_receipt("hosted-a", "prof-ha"),
                hosted_receipt("hosted-b", "prof-hb"),
                hosted_receipt("hosted-exhausted", "prof-hx"),
                hosted_receipt("hosted-none", "prof-hn")]
    capacity = [
        capacity_receipt(),
        capacity_receipt(pool_id="deepseek:api", evidence_reference="capacity-2",
                         exposed_models=("deepseek-v4.1",),
                         state=CapacityState.EXHAUSTED),
    ]
    explanation = evaluator(capacity=capacity).evaluate(
        bare_request(), profiles, receipts)

    assert explanation.selected_target_id == "hosted-a"
    assert explanation.eligible_target_ids() == ("hosted-a", "hosted-b")
    by_target = {c.target_id: c for c in explanation.considered}
    assert by_target["hosted-exhausted"].refusal_reasons == (
        dr.REFUSED_CAPACITY_UNUSABLE,)
    assert by_target["hosted-none"].refusal_reasons == (
        dr.REFUSED_CAPACITY_MISSING,)
    # The eligible hosted targets carry their entitlement evidence.
    assert by_target["hosted-a"].capacity_eligible is True
    assert by_target["hosted-a"].credential_bound is True
    assert by_target["hosted-a"].capacity_receipt_refs


# --------------------------------------------------------------------------
# Negative controls: an override can never turn a DENY into an ALLOW.
# --------------------------------------------------------------------------

def test_a_sensitive_domain_privacy_violation_cannot_be_overridden():
    sensitive = evaluator().evaluate(
        request(domain="personal"),
        [profile(), hosted_profile("hosted-a", "prof-ha")],
        [receipt(), hosted_receipt("hosted-a", "prof-ha")])
    assert sensitive.selected_target_id == "local-a"

    with pytest.raises(PolicyViolationException) as err:
        apply_operator_override(sensitive, "hosted-a", operator_id="operator-1",
                                justification="the hosted model is better",
                                now=NOW)
    assert dr.REFUSED_POLICY_DENIED in err.value.refusal_reasons
    assert set(err.value.refusal_reasons) <= set(dr.KNOWN_REFUSALS)
    assert err.value.target_id == "hosted-a"


def test_local_only_privacy_cannot_be_overridden_onto_a_hosted_target():
    local_only = evaluator().evaluate(
        request(local_only=True),
        [profile(), hosted_profile("hosted-a", "prof-ha")],
        [receipt(), hosted_receipt("hosted-a", "prof-ha")])
    assert local_only.selected_target_id == "local-a"

    with pytest.raises(PolicyViolationException) as err:
        apply_operator_override(local_only, "hosted-a", operator_id="operator-1",
                                justification="approved out loud", now=NOW)
    assert dr.REFUSED_PRIVACY_LOCAL_ONLY in err.value.refusal_reasons


def test_stale_capability_evidence_cannot_be_overridden_into_freshness():
    """Freshness is a fact about now, not a field an operator can restamp."""
    explanation = explain_two_locals()
    assert explanation.eligible_target_ids() == ("local-a", "local-b")

    with pytest.raises(PolicyViolationException) as err:
        apply_operator_override(
            explanation, "local-b", operator_id="operator-1",
            justification="the alternate looks fine to me",
            now=NOW + datetime.timedelta(days=2))
    assert err.value.refusal_reasons == (dr.REFUSED_RECEIPT_STALE,)
    # The refusal changed nothing: the evidence is still the evidence.
    assert explanation_hash_is_valid(explanation.to_dict())


def test_a_hash_sealed_receipt_that_claims_eligibility_is_still_refused():
    """The flag is an assertion; the gate is the control.

    This models a tampered stored receipt that recomputed its own hash, so hash
    validity alone cannot be the safeguard.
    """
    local_only = evaluator().evaluate(
        request(local_only=True), [profile()], [receipt()])
    lie = CandidateExplanation(
        target_id="hosted-a", profile_id="prof-ha", provider="openrouter",
        locality=dr.LOCALITY_HOSTED, eligible=True, rule=RULE_ELIGIBLE,
        receipt_id="cap-ha", receipt_hash="b" * 64,
        receipt_observed_at=NOW.isoformat(), receipt_ttl_s=86400,
        receipt_healthy=True, credential_bound=True, capacity_eligible=True,
        capacity_rule=RULE_ELIGIBLE)
    forged = _reseal(RoutingExplanation(
        decision_id=local_only.decision_id, request=dict(local_only.request),
        policy=dict(local_only.policy),
        considered=(*local_only.considered, lie),
        selected=dict(local_only.selected or {}), rationale=local_only.rationale,
        observed_at=local_only.observed_at,
        dispatch_receipt_hash=local_only.dispatch_receipt_hash,
        decision_hash=local_only.decision_hash))
    assert explanation_hash_is_valid(forged.to_dict())

    with pytest.raises(PolicyViolationException) as err:
        apply_operator_override(forged, "hosted-a", operator_id="operator-1",
                                justification="the receipt says eligible", now=NOW)
    assert dr.REFUSED_PRIVACY_LOCAL_ONLY in err.value.refusal_reasons


def test_a_hosted_target_without_a_credential_binding_is_refused():
    explanation = explain_two_locals()
    unbound = CandidateExplanation(
        target_id="hosted-a", profile_id="prof-ha", provider="openrouter",
        locality=dr.LOCALITY_HOSTED, eligible=True, rule=RULE_ELIGIBLE,
        receipt_id="cap-ha", receipt_hash="b" * 64,
        receipt_observed_at=NOW.isoformat(), receipt_ttl_s=86400,
        receipt_healthy=True, credential_bound=False, capacity_eligible=True,
        capacity_rule=RULE_ELIGIBLE)
    forged = _reseal(RoutingExplanation(
        decision_id=explanation.decision_id, request=dict(explanation.request),
        policy=dict(explanation.policy),
        considered=(*explanation.considered, unbound),
        selected=dict(explanation.selected or {}), rationale=explanation.rationale,
        observed_at=explanation.observed_at,
        dispatch_receipt_hash=explanation.dispatch_receipt_hash,
        decision_hash=explanation.decision_hash))

    with pytest.raises(PolicyViolationException) as err:
        apply_operator_override(forged, "hosted-a", operator_id="operator-1",
                                justification="credential is fine", now=NOW)
    assert dr.REFUSED_CAPACITY_MISSING in err.value.refusal_reasons


def test_every_known_refusal_is_classified_exactly_once():
    """The taxonomy is a complete, disjoint partition of the selector's codes.

    A refusal code added upstream fails this test until it is classified, so a
    new hard rule can never be left looking overridable by omission.
    """
    assert NON_OVERRIDABLE_REFUSALS | ANNOTATABLE_REFUSALS == set(dr.KNOWN_REFUSALS)
    assert not (NON_OVERRIDABLE_REFUSALS & ANNOTATABLE_REFUSALS)
    assert NON_OVERRIDABLE_REFUSALS <= set(REQUIRED_CHANGE)


def test_every_non_overridable_code_is_actually_non_overridable():
    """The taxonomy is a closed set, and it covers every refusal that matters."""
    for code in (dr.REFUSED_PRIVACY_LOCAL_ONLY, dr.REFUSED_POLICY_DENIED,
                 dr.REFUSED_CAPACITY_MISSING, dr.REFUSED_CAPACITY_UNUSABLE,
                 dr.REFUSED_RECEIPT_STALE, dr.REFUSED_RECEIPT_UNHEALTHY,
                 dr.REFUSED_NETWORK, dr.REFUSED_TOOL):
        assert code in NON_OVERRIDABLE_REFUSALS
        assert code in REQUIRED_CHANGE
    assert NON_OVERRIDABLE_REFUSALS <= set(dr.KNOWN_REFUSALS)

# --------------------------------------------------------------------------
# Malformed and no-op overrides mint nothing.
# --------------------------------------------------------------------------

def test_an_override_must_name_a_different_candidate():
    explanation = explain_two_locals()
    with pytest.raises(ExplanationError, match="already the canonical selection"):
        apply_operator_override(explanation, "local-a", operator_id="operator-1",
                                justification="no change needed", now=NOW)


def test_an_unknown_target_is_refused_rather_than_invented():
    explanation = explain_two_locals()
    with pytest.raises(ExplanationError, match="not among the candidates"):
        apply_operator_override(explanation, "local-does-not-exist",
                                operator_id="operator-1",
                                justification="typo", now=NOW)


def test_an_override_requires_an_operator_and_a_justification():
    explanation = explain_two_locals()
    with pytest.raises(ExplanationError, match="operator"):
        apply_operator_override(explanation, "local-b", operator_id="   ",
                                justification="because", now=NOW)
    with pytest.raises(ExplanationError, match="justification"):
        apply_operator_override(explanation, "local-b", operator_id="operator-1",
                                justification="  ", now=NOW)


def test_an_unsealed_or_tampered_explanation_is_not_evidence():
    explanation = explain_two_locals()
    unsealed = dataclasses.replace(explanation, explanation_hash="")
    with pytest.raises(ExplanationError, match="not sealed"):
        apply_operator_override(unsealed, "local-b", operator_id="operator-1",
                                justification="x", now=NOW)

    payload = explanation.to_dict()
    payload["refusal_code"] = "tampered"
    with pytest.raises(ExplanationError, match="hash does not match"):
        apply_operator_override(payload, "local-b", operator_id="operator-1",
                                justification="x", now=NOW)


# --------------------------------------------------------------------------
# Positive control: the audited override receipt.
# --------------------------------------------------------------------------

def test_an_override_between_eligible_candidates_mints_an_immutable_receipt():
    explanation = explain_two_locals()
    override = apply_operator_override(
        explanation, "local-b", operator_id="operator-1",
        justification="the second box has the dataset cached", now=NOW)

    assert override.original_target_id == "local-a"
    assert override.authorized_target_id == "local-b"
    assert override.authorized_profile_id == "prof-b"
    assert override.operator_id == "operator-1"
    assert override.justification == "the second box has the dataset cached"
    assert override.explanation_hash == explanation.explanation_hash
    assert override.decision_hash == explanation.decision_hash
    assert override.dispatch_receipt_hash == explanation.dispatch_receipt_hash
    assert override.policy_ref == explanation.policy["policy_ref"]
    assert override.original_still_eligible is True
    # The whole eligible set is recorded, so the steering surface is auditable.
    assert override.considered_eligible == ("local-a/prof-a", "local-b/prof-b")
    assert override.authorizes == OVERRIDE_AUTHORIZES
    assert override.scope == OVERRIDE_SCOPE
    assert override.verify() is True
    assert override_receipt_hash_is_valid(override.to_dict())
    assert override.override_id.startswith("override-")
    assert override.evidence_refs

    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(override, "authorized_target_id", "local-c")

    payload = override.to_dict()
    payload["authorized_target_id"] = "local-stale"
    assert override_receipt_hash_is_valid(payload) is False


def test_reissuing_the_same_override_yields_the_same_receipt():
    """A retry cannot fabricate a second authorization for the same intent."""
    explanation = explain_two_locals()
    first = apply_operator_override(explanation, "local-b",
                                    operator_id="operator-1",
                                    justification="cache locality", now=NOW)
    second = apply_operator_override(explanation, "local-b",
                                     operator_id="operator-1",
                                     justification="cache locality", now=NOW)
    assert first.override_id == second.override_id
    assert first.receipt_hash == second.receipt_hash

    other_operator = apply_operator_override(
        explanation, "local-b", operator_id="operator-2",
        justification="cache locality", now=NOW)
    assert other_operator.override_id != first.override_id


def test_the_override_records_that_the_original_is_no_longer_eligible():
    """Steering is recorded honestly: the receipt states what it moved away from."""
    # local-a's evidence is valid for a minute and is inside it at decision time.
    shortly_before = (NOW - datetime.timedelta(seconds=30)).isoformat()
    profiles = [profile(), profile(target_id="local-b", profile_id="prof-b",
                                   cost_rank=1)]
    receipts = [receipt(ttl_s=60, observed_at=shortly_before),
                receipt(receipt_id="cap-b", profile_id="prof-b",
                        target_id="local-b", ttl_s=86400)]
    explanation = evaluator().evaluate(request(), profiles, receipts)
    assert explanation.selected_target_id == "local-a"

    later = NOW + datetime.timedelta(seconds=120)
    override = apply_operator_override(explanation, "local-b",
                                       operator_id="operator-1",
                                       justification="original evidence expired",
                                       now=later)
    assert override.original_still_eligible is False
    assert override.authorized_target_id == "local-b"


def test_the_override_receipt_survives_a_json_round_trip():
    explanation = explain_two_locals()
    override = apply_operator_override(
        explanation, "local-b", operator_id="operator-1",
        justification="a real reason", now=NOW, idempotency_key="intent-1")
    payload = json.loads(json.dumps(override.to_dict()))
    assert override_receipt_hash_is_valid(payload)
    assert payload["idempotency_key"] == "intent-1"
    assert payload["authorizes"] == list(OVERRIDE_AUTHORIZES)
    assert payload["scope"] == OVERRIDE_SCOPE


# --------------------------------------------------------------------------
# Stored explanations round-trip and refuse tampering.
# --------------------------------------------------------------------------

def test_a_stored_explanation_round_trips_and_still_steers():
    explanation = explain_two_locals()
    payload = json.loads(json.dumps(explanation.to_dict()))
    restored = routing_explanation_from_dict(payload)
    assert restored.explanation_hash == explanation.explanation_hash
    assert _canonical(restored.to_dict()) == _canonical(explanation.to_dict())

    override = apply_operator_override(payload, "local-b", operator_id="operator-1",
                                       justification="from storage", now=NOW)
    assert override.explanation_hash == explanation.explanation_hash


def test_a_stored_explanation_with_an_edited_field_is_refused():
    explanation = explain_two_locals()
    payload = json.loads(json.dumps(explanation.to_dict()))
    payload["considered"][1]["capability_receipt_hash"] = "0" * 64
    with pytest.raises(ExplanationError, match="hash does not match"):
        routing_explanation_from_dict(payload)


def test_an_explanation_cannot_claim_both_a_selection_and_a_refusal():
    with pytest.raises(ExplanationError, match="cannot also carry a refusal"):
        RoutingExplanation(
            decision_id="d", request={"domain": "general_swe"},
            policy={"policy_ref": "p"}, considered=(),
            selected={"target_id": "local-a", "profile_id": "prof-a"},
            refusal_code=dr.REFUSED_PRIVACY_LOCAL_ONLY,
            observed_at=NOW.isoformat())


def test_a_candidate_row_cannot_be_eligible_and_refused_at_once():
    with pytest.raises(ExplanationError, match="must carry no refusal"):
        CandidateExplanation(
            target_id="local-a", profile_id="prof-a", provider="ollama",
            locality=dr.LOCALITY_LOCAL, eligible=True, rule=RULE_ELIGIBLE,
            refusal_reasons=(dr.REFUSED_ROLE,))


def test_the_scope_of_an_override_receipt_is_fixed():
    """The receipt may not be re-read as a dispatch, a grant or a policy change."""
    explanation = explain_two_locals()
    override = apply_operator_override(explanation, "local-b",
                                       operator_id="operator-1",
                                       justification="scope check", now=NOW)
    with pytest.raises(ExplanationError, match="nothing else"):
        dataclasses.replace(override, authorizes=("dispatch",))
    with pytest.raises(ExplanationError, match="scope must be"):
        dataclasses.replace(override, scope="full_authority")


def test_a_soft_refusal_is_still_not_overridable():
    """Blocking is unconditional: the hard/soft split is descriptive only.

    ``role_not_supported`` sits on the curable side of the taxonomy, so a
    candidate can carry it with no hard refusal at all. It must still refuse an
    override, because the gate re-derives from the deciding ``rule`` rather than
    from the ``hard_refusals`` label. That keeps the DENY guarantee independent
    of the classification: a mis-classified or newly added code can never become
    an override path.
    """
    wrong_role = profile(target_id="local-verifier", profile_id="prof-verifier",
                         roles=frozenset({dr.ROLE_VERIFIER}))
    explanation = evaluator().evaluate(
        request(),
        [profile(), wrong_role],
        [receipt(), receipt(receipt_id="cap-v", profile_id="prof-verifier",
                            target_id="local-verifier")])
    refused = {c.target_id: c for c in explanation.considered}["local-verifier"]
    assert refused.eligible is False
    assert refused.refusal_reasons == (dr.REFUSED_ROLE,)
    assert dr.REFUSED_ROLE in ANNOTATABLE_REFUSALS
    assert dr.REFUSED_ROLE not in NON_OVERRIDABLE_REFUSALS
    # No hard label on the row at all...
    assert refused.hard_refusals == ()

    # ...and it is still refused, with the rule that blocked it.
    with pytest.raises(PolicyViolationException) as err:
        apply_operator_override(explanation, "local-verifier",
                                operator_id="operator-1",
                                justification="I need this node instead", now=NOW)
    assert err.value.refusal_reasons == (dr.REFUSED_ROLE,)
