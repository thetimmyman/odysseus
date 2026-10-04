"""Hermetic proof: normalization runs only behind canonical selected dispatch."""
import copy
import dataclasses
import hashlib
import socket
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from src.offer_economics import project_offer_quota
from src.provider_capacity import (AuthorizationClass, CapacityState, CostClass,
                                   Entitlement, EvidenceProvenance, PriceObservation,
                                   QuotaDimension, UNKNOWN, make_capacity_receipt)
from src.provider_model_offer import (UNKNOWN as OFFER_UNKNOWN, CreditEffect, OfferProvenance, PriceEffect,
                                      make_provider_model_offer_receipt)
from src.provider_protocol_normalization import (
    NormalizationError, ToolNameConstraints, normalize_request, restore_response,
)
from test_dispatch_boundary import _db, _seed, _resolve, _invocation


def _bound(*, two_eligible=False):
    db = _db()
    task = _seed(db, allow_paid=True, allow_premium=True)
    if two_eligible:
        bound = _resolve(db, task, "p-rtx", "p-openrouter", role="scout")
        assert sum(a.eligible for a in bound.decision.candidates) == 2
        return db, bound, _invocation()
    return db, _resolve(db, task, "p-rtx"), _invocation()


def _payload(name="decode/雪🚀"):
    return {"model": "qwen3.8:27b", "messages": [{"role": "user", "content": "keep bytes"}],
            "tools": [{"type": "function", "function": {
                "name": name, "description": "unchanged", "parameters": {"type": "object"}}}]}


def test_real_selected_decision_roundtrips_alias_without_mutation_or_argument_change(monkeypatch):
    _, bound, invocation = _bound(two_eligible=True)
    original = _payload()
    before = copy.deepcopy(original)
    def refused(*args, **kwargs):
        raise AssertionError("normalizer attempted socket/process launch")

    with monkeypatch.context() as effects:
        effects.setattr(socket, "socket", refused)
        effects.setattr(socket, "create_connection", refused)
        effects.setattr(subprocess, "Popen", refused)
        normalized = normalize_request(bound, invocation, original)
    alias = next(iter(normalized.aliases))
    assert original == before
    assert normalized.payload["tools"][0]["function"]["name"] == alias
    assert normalized.payload["tools"][0]["function"]["parameters"] == original["tools"][0]["function"]["parameters"]
    assert normalized.payload["messages"] == original["messages"]
    assert normalize_request(bound, invocation, original).payload == normalized.payload
    response = {"model": "qwen3.8:27b", "choices": [{"message": {"tool_calls": [
        {"id": "call-id-preserved", "function": {"name": alias, "arguments": '{"x": 1}'}}]}}]}
    original_response = copy.deepcopy(response)
    with monkeypatch.context() as effects:
        effects.setattr(socket, "socket", refused)
        effects.setattr(socket, "create_connection", refused)
        effects.setattr(subprocess, "Popen", refused)
        restored = restore_response(bound, invocation, response, normalized)
    assert response == original_response
    assert restored["choices"][0]["message"]["tool_calls"][0] == {
        "id": "call-id-preserved", "function": {"name": "decode/雪🚀", "arguments": '{"x": 1}'}}


def test_name_alias_is_consistent_across_declaration_history_choice_and_response():
    _, bound, invocation = _bound(two_eligible=True)
    original = _payload("read/雪 🚀")
    original["messages"] = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call-雪-1", "type": "function", "function": {
                "name": "read/雪 🚀", "arguments": '{"raw": "\u2603"}'}}]},
        {"role": "tool", "tool_call_id": "call-雪-1", "content": "unchanged result"},
    ]
    original["tool_choice"] = {"type": "function", "function": {"name": "read/雪 🚀"}}
    before = copy.deepcopy(original)
    normalized = normalize_request(bound, invocation, original)
    alias = next(iter(normalized.aliases))
    assert original == before
    assert normalized.payload["tools"][0]["function"]["name"] == alias
    assert normalized.payload["messages"][0]["tool_calls"][0]["function"]["name"] == alias
    assert normalized.payload["tool_choice"]["function"]["name"] == alias
    assert normalized.payload["messages"][0]["tool_calls"][0]["function"]["arguments"] == '{"raw": "\u2603"}'
    assert normalized.payload["messages"][0]["tool_calls"][0]["id"] == "call-雪-1"
    assert normalized.payload["messages"][1] == original["messages"][1]
    response = {"model": "qwen3.8:27b", "choices": [{"message": {"tool_calls": [
        {"id": "call-雪-1", "function": {"name": alias, "arguments": '{"raw": "\u2603"}'}}]}}]}
    restored = restore_response(bound, invocation, response, normalized)
    assert restored["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "read/雪 🚀"
    assert restored["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"] == '{"raw": "\u2603"}'


@pytest.mark.parametrize("choice", ["none", "auto", "required"])
def test_ordinary_tool_choice_values_remain_unchanged(choice):
    _, bound, invocation = _bound()
    payload = _payload()
    payload["tool_choice"] = choice
    assert normalize_request(bound, invocation, payload).payload["tool_choice"] == choice


@pytest.mark.parametrize("messages,choice", [
    ([{"role": "assistant", "tool_calls": [{"function": {"name": "not-declared"}}]}], None),
    ([{"role": "assistant", "tool_calls": "bad-shape"}], None),
    ([], {"type": "function", "function": {"name": "not-declared"}}),
    ([], {"type": "function", "function": "bad-shape"}),
])
def test_unknown_or_malformed_request_references_refuse_without_mutating_input(messages, choice):
    _, bound, invocation = _bound()
    payload = _payload()
    payload["messages"] = messages
    if choice is not None:
        payload["tool_choice"] = choice
    before = copy.deepcopy(payload)
    with pytest.raises(NormalizationError, match="malformed_tool_reference"):
        normalize_request(bound, invocation, payload)
    assert payload == before


def test_lone_surrogate_refuses_as_typed_error_and_valid_unicode_still_aliases():
    _, bound, invocation = _bound()
    with pytest.raises(NormalizationError, match="malformed_tool_name"):
        normalize_request(bound, invocation, _payload("broken-\ud800"))
    normalized = normalize_request(bound, invocation, _payload("café/🚀"))
    assert normalized.payload["tools"][0]["function"]["name"] in normalized.aliases


def test_lone_surrogate_refuses_even_when_custom_rule_would_accept_it():
    _, bound, invocation = _bound(two_eligible=True)
    name = "bad-\ud800"
    payload = _payload(name)
    payload["messages"] = [{"role": "assistant", "tool_calls": [
        {"id": "surrogate-call", "function": {"name": name, "arguments": "{}"}}]}]
    payload["tool_choice"] = {"type": "function", "function": {"name": name}}
    before = copy.deepcopy(payload)
    with pytest.raises(NormalizationError, match="malformed_tool_name"):
        normalize_request(bound, invocation, payload, constraints=ToolNameConstraints(pattern=".+"))
    assert payload == before


def test_custom_rule_can_keep_valid_unicode_name_unchanged():
    _, bound, invocation = _bound()
    payload = _payload("café")
    normalized = normalize_request(bound, invocation, payload,
                                  constraints=ToolNameConstraints(pattern=".+"))
    assert normalized.payload["tools"][0]["function"]["name"] == "café"
    assert normalized.aliases == {}


@pytest.mark.parametrize("field,value,code", [
    ("model", "other-model", "request_model_mismatch"),
    ("provider", "other-provider", "request_provider_mismatch"),
    ("profile_id", "p-openrouter", "request_profile_mismatch"),
    ("endpoint_url", "https://other.invalid/v1", "request_endpoint_mismatch"),
])
def test_request_identity_substitution_refuses(field, value, code):
    _, bound, invocation = _bound()
    payload = _payload()
    payload[field] = value
    with pytest.raises(NormalizationError, match=code):
        normalize_request(bound, invocation, payload)


def test_alternate_eligible_profile_and_invocation_identity_refuse_without_fallback():
    _, bound, _ = _bound(two_eligible=True)
    alternate = _invocation(profile_id="p-openrouter", provider="openrouter",
        runtime_kind="openrouter", model="deepseek-v4-pro",
        chat_url="https://openrouter.ai/api/v1", locality="hosted")
    with pytest.raises(NormalizationError, match="selected_profile_mismatch"):
        normalize_request(bound, alternate, _payload())
    changed = _invocation(model="substitute")
    with pytest.raises(NormalizationError):
        normalize_request(bound, changed, _payload())


def test_stale_or_tampered_decision_and_missing_decision_refuse():
    _, bound, invocation = _bound()
    altered = dataclasses.replace(bound.decision, reason="tampered")
    with pytest.raises(NormalizationError, match="decision_hash_mismatch"):
        normalize_request(dataclasses.replace(bound, decision=altered), invocation, _payload())
    missing = dataclasses.replace(bound.decision, receipt_hash="")
    with pytest.raises(NormalizationError, match="decision_hash_mismatch"):
        normalize_request(dataclasses.replace(bound, decision=missing), invocation, _payload())


def test_endpoint_provider_and_model_response_substitution_refuse():
    _, bound, invocation = _bound()
    normalized = normalize_request(bound, invocation, _payload())
    for response in (
        {"model": "other-model", "choices": []},
        {"model": "qwen3.8:27b", "provider": "other", "choices": []},
    ):
        with pytest.raises(NormalizationError):
            restore_response(bound, invocation, response, normalized)
    with pytest.raises(NormalizationError):
        normalize_request(bound, dataclasses.replace(invocation, chat_url="http://10.0.0.99/v1"), _payload())


def test_alias_collisions_unknown_aliases_and_case_conflicts_refuse():
    _, bound, invocation = _bound()
    invalid = "needs/punctuation"
    alias = "t_" + hashlib.sha256(invalid.encode()).hexdigest()[:20]
    collision = _payload(invalid)
    collision["tools"].append({"type": "function", "function": {"name": alias.upper(), "parameters": {}}})
    with pytest.raises(NormalizationError, match="tool_name_collision"):
        normalize_request(bound, invocation, collision)
    assert collision["tools"][0]["function"]["name"] == invalid
    # A pattern that excludes the deterministic alias refuses before returning partial output.
    with pytest.raises(NormalizationError):
        normalize_request(bound, invocation, _payload(), constraints=ToolNameConstraints(pattern="[A-Z]+"))
    normalized = normalize_request(bound, invocation, _payload())
    unknown = {"model": "qwen3.8:27b", "choices": [{"message": {"tool_calls": [
        {"function": {"name": "tool_00000000000000000000", "arguments": "{}"}}]}}]}
    with pytest.raises(NormalizationError, match="unknown_tool_alias"):
        restore_response(bound, invocation, unknown, normalized)
    case_variant = {"model": "qwen3.8:27b", "choices": [{"message": {"tool_calls": [
        {"function": {"name": next(iter(normalized.aliases)).upper(), "arguments": "{}"}}]}}]}
    with pytest.raises(NormalizationError, match="unknown_tool_alias"):
        restore_response(bound, invocation, case_variant, normalized)


def test_canonical_quota_unknowns_keep_units_reset_and_billing_unknown():
    now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    stamp = now.isoformat()
    provenance = EvidenceProvenance("synthetic-fixture", "receipt:test", "test", stamp, 3600)
    capacity = make_capacity_receipt(
        provider="provider-a", pool_id="pool-a", account_identity="account-a",
        authorization_class=AuthorizationClass.API_KEY, entitlement=Entitlement.API,
        exposed_models=("vendor/model-x",), observed_at=stamp, ttl_seconds=3600,
        collector_id="synthetic-test", evidence_source="fixture", evidence_reference="capacity:test",
        state=CapacityState.AVAILABLE, state_provenance=provenance,
        entitlement_provenance=provenance, zdr_provenance=provenance,
        quotas=(QuotaDimension("request_count", "requests", UNKNOWN, UNKNOWN,
                               UNKNOWN, provenance),),
        price=PriceObservation(CostClass.METERED, actual_billed_cost=UNKNOWN),
    )
    offer = make_provider_model_offer_receipt(
        provider=capacity.provider, pool_id=capacity.pool_id,
        capacity_receipt_ref=capacity.ref, harness="command-code", usage_path="subscription-cli",
        native_model="vendor/model-x", tariff_id="synthetic", tariff_version="1",
        provenance=OfferProvenance("https://synthetic.invalid/terms", "a" * 64,
                                   "synthetic_fixture", stamp, 3600),
        valid_from=(now - timedelta(minutes=1)).isoformat(),
        valid_until=(now + timedelta(minutes=30)).isoformat(),
        price=PriceEffect("USD", "request", OFFER_UNKNOWN, OFFER_UNKNOWN),
        credit=CreditEffect("requests", 1, "per_request_debit"),
    )
    projected = project_offer_quota(offer, capacity, now=now)
    assert projected.conversion_status == "identity_same_unit"
    assert projected.projections[0].unit == "requests"
    assert projected.projections[0].reset_window_known is False
    assert isinstance(projected.projections[0].remaining, type(UNKNOWN))
    assert capacity.price.actual_billed_cost is UNKNOWN
