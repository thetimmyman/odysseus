from dataclasses import fields
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path

import pytest

from src.offer_economics import project_offer_quota, quote_profile
from src.provider_capacity import (
    CapacityError, CostClass, PriceObservation, PricingSource, QuotaDimension,
    UNKNOWN as CAPACITY_UNKNOWN,
    make_capacity_receipt,
)
from src.provider_capacity_store import ProviderCapacityStore
from src.provider_model_offer import (
    UNKNOWN as OFFER_UNKNOWN, CreditEffect, OfferProvenance, OfferReceiptError, PriceEffect,
    make_provider_model_offer_receipt,
)
from src.promotional_dispatch import PromotionUnavailable
from tests.test_offer_economics import WORK
from tests.test_provider_model_offer import offer as offer_factory
from tests.test_ps640_provider_capacity import prov as capacity_provenance, receipt as capacity_receipt


@pytest.fixture
def configured(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    capacity = capacity_receipt(
        credential_sha256=hashlib.sha256(b"{}").hexdigest(), observed_at=stamp,
        state_provenance=capacity_provenance(observed=stamp),
        entitlement_provenance=capacity_provenance(observed=stamp),
        zdr_provenance=capacity_provenance(observed=stamp), price=None,
    )
    store = ProviderCapacityStore(str(tmp_path / "capacity"))
    store.append(capacity)
    source = tmp_path / "source"
    source.write_bytes(b"fixture quota terms")
    record = offer_factory(
        provider=capacity.provider, pool_id=capacity.pool_id,
        capacity_receipt_ref=capacity.ref, harness="odysseus-scout",
        native_model="qwen3.8:27b",
        valid_from=(now - timedelta(hours=1)).isoformat(),
        valid_until=(now + timedelta(hours=1)).isoformat(),
        provenance=OfferProvenance(
            "https://example.test/price", hashlib.sha256(source.read_bytes()).hexdigest(),
            "test", stamp, 3600,
        ),
        price=PriceEffect("USD", "request", 1, 0),
    )
    path = tmp_path / "offer.json"
    path.write_text(json.dumps(record.to_dict()))
    profile = {
        "provider": capacity.provider, "pool_id": capacity.pool_id,
        "harness": record.harness, "credential_sha256": capacity.credential_sha256,
        "account_identity": capacity.account_identity, "endpoint_id": "fixture-endpoint",
        "transport_provider": "openai", "usage_path": record.usage_path,
        "chat_url": "https://example.test/v1/chat/completions",
        "offer_path": str(path), "source_path": str(source),
    }
    monkeypatch.setenv("ODYSSEUS_FREE_OFFER_CONFIG", str(tmp_path / "free-config.json"))
    (tmp_path / "free-config.json").write_text(json.dumps({"profiles": {"p": profile}}))
    kwargs = {
        "credential_sha256": capacity.credential_sha256,
        "endpoint_id": "fixture-endpoint", "transport_provider": "openai",
        "profile_id": "p", "model": record.native_model,
        "harness": record.harness, "chat_url": profile["chat_url"],
    }
    return kwargs, source, path, record, store, capacity


def _capacity_with_quotas(capacity, quotas):
    values = {item.name: getattr(capacity, item.name) for item in fields(capacity)}
    values["quotas"] = tuple(quotas)
    return make_capacity_receipt(**values)


def _offer_for_capacity(record, capacity, **changes):
    values = {item.name: getattr(record, item.name) for item in fields(record)}
    values.pop("receipt_hash", None)
    values.update(capacity_receipt_ref=capacity.ref, **changes)
    return make_provider_model_offer_receipt(**values)


def _quote(configured, capacity, record, *, credit=None):
    kwargs, source, path, _, store, _ = configured
    if record.capacity_receipt_ref != capacity.ref or (credit is not None and record.credit != credit):
        record = _offer_for_capacity(
            record, capacity, **({"credit": credit} if credit is not None else {})
        )
    path.write_text(json.dumps(record.to_dict()))
    source_hash_path = str(source)
    original_config = json.loads(Path(os.environ["ODYSSEUS_FREE_OFFER_CONFIG"]).read_text())
    profile = dict(original_config["profiles"]["p"])
    profile["offer_path"] = str(path)
    profile["source_path"] = source_hash_path
    config = {"capacity_store": store.directory, "maximum_predicted_request_usd": "10",
              "profiles": {"p": profile}}
    return quote_profile(config, **kwargs, workload=WORK)


def _quota(capacity, name, unit="requests", remaining=20, *, reset_at=CAPACITY_UNKNOWN,
           provenance=None, limit=100):
    return QuotaDimension(name, unit, limit, remaining, reset_at,
                          provenance or capacity.state_provenance)


def test_quote_with_two_same_unit_dimensions_is_explicitly_ambiguous_and_cash_unchanged(configured):
    kwargs, source, path, record, store, capacity = configured
    baseline = _quote(configured, capacity, record)
    current = datetime.now(timezone.utc)
    quotas = (
        _quota(capacity, "daily", remaining=20, reset_at=(current + timedelta(hours=12)).isoformat()),
        _quota(capacity, "weekly", remaining=80, reset_at=(current + timedelta(days=4)).isoformat()),
    )
    updated = store.append(_capacity_with_quotas(capacity, quotas), supersedes=capacity.receipt_hash)
    credit = CreditEffect("requests", 4, "per_request_debit")
    quoted = _quote(configured, updated, record, credit=credit)
    result = quoted["effective_quota"][0]
    assert result["status"] == "unknown"
    assert result["reason"] == "ambiguous_dimension"
    assert result["conversion_status"] == "identity_same_unit"
    assert [row["requests_from_current_remaining"] for row in result["projections"]] == [None, None]
    assert [row["dimension"] for row in result["projections"]] == ["daily", "weekly"]
    assert quoted["predicted_cash_usd"] == baseline["predicted_cash_usd"]
    assert quoted["native_effects"][0]["credit"] == credit.to_dict()
    assert result["cash_equivalent_usd"] is None
    assert result["evidence_confidence"] == "UNASSESSED"


def test_candidate_order_changes_never_choose_a_different_dimension(configured):
    _, _, _, record, _, capacity = configured
    current = datetime.now(timezone.utc)
    daily = _quota(capacity, "daily", remaining=20,
                   reset_at=(current + timedelta(hours=12)).isoformat())
    weekly = _quota(capacity, "weekly", remaining=80,
                    reset_at=(current + timedelta(days=4)).isoformat())
    credit = CreditEffect("requests", 4, "per_request_debit")
    first_capacity = _capacity_with_quotas(capacity, (daily, weekly))
    second_capacity = _capacity_with_quotas(capacity, (weekly, daily))
    first = project_offer_quota(_offer_for_capacity(record, first_capacity, credit=credit),
                                first_capacity, now=current)
    second = project_offer_quota(_offer_for_capacity(record, second_capacity, credit=credit),
                                 second_capacity, now=current)
    assert (first.status, first.reason, first.conversion_status) == (
        second.status, second.reason, second.conversion_status
    ) == ("unknown", "ambiguous_dimension", "identity_same_unit")
    assert sorted(row.dimension for row in first.projections) == sorted(
        row.dimension for row in second.projections
    ) == ["daily", "weekly"]
    assert all(row.request_count is None for row in first.projections + second.projections)


def test_unique_current_dimension_projects_exact_count_and_keeps_source_refs(configured):
    _, _, _, record, store, capacity = configured
    current = datetime.now(timezone.utc)
    quota = _quota(capacity, "daily", remaining=27,
                   reset_at=(current + timedelta(hours=12)).isoformat())
    updated = store.append(_capacity_with_quotas(capacity, (quota,)), supersedes=capacity.receipt_hash)
    credit = CreditEffect("requests", 3, "per_request_debit")
    result = _quote(configured, updated, record, credit=credit)["effective_quota"][0]
    assert result["status"] == "conditional"
    assert result["conversion_status"] == "identity_same_unit"
    projection = result["projections"][0]
    assert projection["requests_from_current_remaining"] == "9"
    assert projection["offer_ref"] == _offer_for_capacity(record, updated, credit=credit).ref
    assert projection["capacity_receipt_ref"] == updated.ref
    assert projection["dimension"] == "daily" and projection["unit"] == "requests"
    assert projection["remaining"] == 27 and projection["debit_per_request"] == 3
    assert projection["reset_at"] == quota.reset_at
    assert projection["quota_provenance"] == quota.provenance.to_dict()
    assert projection["offer_provenance"] == record.provenance.to_dict()
    assert projection["reset_window_known"] is True
    assert projection["cash_equivalent_usd"] is None


def test_unknown_reset_is_preserved_without_claiming_a_known_window(configured):
    _, _, _, record, _, capacity = configured
    current = datetime.now(timezone.utc)
    cap = _capacity_with_quotas(capacity, (_quota(capacity, "rolling", remaining=10),))
    offer = _offer_for_capacity(record, cap, credit=CreditEffect("requests", 2, "per_request_debit"))
    result = project_offer_quota(offer, cap, now=current)
    projection = result.to_dict()["projections"][0]
    assert result.status == "conditional"
    assert projection["requests_from_current_remaining"] == "5"
    assert projection["reset_at"] == {"status": "unknown"}
    assert projection["reset_window_known"] is False


def test_unknown_debit_remaining_unmatched_unit_and_other_application_are_unknown(configured):
    _, _, _, record, _, capacity = configured
    current = datetime.now(timezone.utc)
    cap = _capacity_with_quotas(capacity, (_quota(capacity, "tokens", "tokens", remaining=10),))
    cases = (
        (_offer_for_capacity(record, cap, credit=CreditEffect("tokens", OFFER_UNKNOWN, "per_request_debit")),
         "debit_unknown"),
        (_offer_for_capacity(record, cap, credit=CreditEffect("requests", 2, "per_request_debit")),
        "unsupported_conversion"),
        (_offer_for_capacity(record, cap, credit=CreditEffect("tokens", 2, "account_allowance")),
         "unsupported_credit_application"),
    )
    for offer, reason in cases:
        result = project_offer_quota(offer, cap, now=current)
        assert result.status == "unknown" and result.reason == reason
        assert result.conversion_status == "UNKNOWN"
        assert result.to_dict()["cash_equivalent_usd"] is None

    unknown_remaining = _capacity_with_quotas(
        capacity, (_quota(capacity, "tokens", "tokens", remaining=CAPACITY_UNKNOWN),)
    )
    unknown_offer = _offer_for_capacity(
        record, unknown_remaining, credit=CreditEffect("tokens", 2, "per_request_debit")
    )
    unknown_result = project_offer_quota(unknown_offer, unknown_remaining, now=current)
    assert unknown_result.reason == "remaining_unknown_or_invalid"
    assert unknown_result.projections[0].request_count is None


def test_zero_debit_and_zero_remaining_are_distinct_and_never_unlimited(configured):
    _, _, _, record, _, capacity = configured
    current = datetime.now(timezone.utc)
    cap = _capacity_with_quotas(
        capacity, (_quota(capacity, "requests", remaining=0, limit=100),)
    )
    zero_debit = _offer_for_capacity(
        record, cap, credit=CreditEffect("requests", 0, "per_request_debit")
    )
    zero_result = project_offer_quota(zero_debit, cap, now=current)
    assert zero_result.reason == "zero_debit_not_unlimited"
    assert zero_result.projections[0].remaining == 0
    assert zero_result.projections[0].request_count is None
    positive_debit = _offer_for_capacity(
        record, cap, credit=CreditEffect("requests", 1, "per_request_debit")
    )
    exhausted = project_offer_quota(positive_debit, cap, now=current)
    assert exhausted.status == "conditional"
    assert exhausted.projections[0].request_count == 0


def test_fractional_and_large_integer_arithmetic_is_exact(configured):
    _, _, _, record, _, capacity = configured
    current = datetime.now(timezone.utc)
    cap = _capacity_with_quotas(
        capacity, (_quota(capacity, "monthly", "credits", remaining=10**40, limit=10**41),)
    )
    offer = _offer_for_capacity(
        record, cap, credit=CreditEffect("credits", 0.25, "per_request_debit")
    )
    result = project_offer_quota(offer, cap, now=current)
    assert result.projections[0].request_count == 4 * 10**40
    assert result.to_dict()["projections"][0]["requests_from_current_remaining"] == str(4 * 10**40)
    assert json.dumps(result.to_dict(), allow_nan=False)


def test_mismatched_exact_capacity_binding_is_refused(configured):
    _, _, _, record, _, capacity = configured
    other_capacity = _capacity_with_quotas(
        capacity, (_quota(capacity, "other", "requests", remaining=1),)
    )
    with pytest.raises(PromotionUnavailable, match="exact capacity"):
        project_offer_quota(record, other_capacity, now=datetime.now(timezone.utc))


def test_stale_capacity_future_offer_and_elapsed_reset_never_emit_a_count(configured):
    _, _, _, record, _, capacity = configured
    current = datetime.now(timezone.utc)
    old_fields = {item.name: getattr(capacity, item.name) for item in fields(capacity)}
    old_fields.update(observed_at=(current - timedelta(minutes=10)).isoformat(), ttl_seconds=1)
    stale_capacity = make_capacity_receipt(**old_fields)
    stale_offer = _offer_for_capacity(
        record, stale_capacity, credit=CreditEffect("requests", 1, "per_request_debit")
    )
    stale = project_offer_quota(stale_offer, stale_capacity, now=current)
    assert stale.reason == "capacity_source_not_current"
    assert not stale.projections

    fresh_cap = _capacity_with_quotas(capacity, (_quota(capacity, "requests", remaining=10),))
    future_provenance = OfferProvenance(
        record.provenance.source_url, record.provenance.content_sha256,
        record.provenance.evidence_class, (current + timedelta(minutes=5)).isoformat(), 3600,
    )
    future_offer = _offer_for_capacity(
        record, fresh_cap, credit=CreditEffect("requests", 1, "per_request_debit"),
        provenance=future_provenance,
    )
    future = project_offer_quota(future_offer, fresh_cap, now=current)
    assert future.reason == "offer_source_or_validity_not_current"
    assert not future.projections

    reset = (current + timedelta(seconds=1)).isoformat()
    reset_cap = _capacity_with_quotas(
        capacity, (_quota(capacity, "requests", remaining=10, reset_at=reset),)
    )
    reset_offer = _offer_for_capacity(
        record, reset_cap, credit=CreditEffect("requests", 1, "per_request_debit")
    )
    elapsed = project_offer_quota(reset_offer, reset_cap, now=current + timedelta(seconds=2))
    assert elapsed.reason == "quota_reset_elapsed"
    assert elapsed.projections[0].request_count is None


def test_observation_timestamp_equal_to_now_is_current_and_eligible(configured):
    _, _, _, record, _, capacity = configured
    current = datetime.now(timezone.utc)
    stamp = current.isoformat()
    provenance = capacity_provenance(observed=stamp, ttl=3600)
    quota = QuotaDimension("current", "credits", 100, 20, CAPACITY_UNKNOWN, provenance)
    values = {item.name: getattr(capacity, item.name) for item in fields(capacity)}
    values.update(
        observed_at=stamp, ttl_seconds=3600, state_provenance=provenance,
        entitlement_provenance=provenance, zdr_provenance=provenance,
        quotas=(quota,),
    )
    exact_capacity = make_capacity_receipt(**values)
    exact_offer_provenance = OfferProvenance(
        record.provenance.source_url, record.provenance.content_sha256,
        record.provenance.evidence_class, stamp, 3600,
    )
    exact_offer = _offer_for_capacity(
        record, exact_capacity, credit=CreditEffect("credits", 2, "per_request_debit"),
        provenance=exact_offer_provenance,
    )
    result = project_offer_quota(exact_offer, exact_capacity, now=current)
    assert result.status == "conditional"
    assert result.projections[0].request_count == 10


@pytest.mark.parametrize(
    "future_fact",
    ["capacity", "state", "entitlement", "zdr", "quota", "rate_limit", "price"],
)
def test_future_capacity_evidence_never_produces_a_numeric_projection(configured, future_fact):
    _, _, _, record, _, capacity = configured
    current = datetime.now(timezone.utc)
    stamp = current.isoformat()
    future_stamp = (current + timedelta(minutes=5)).isoformat()
    current_provenance = capacity_provenance(observed=stamp, ttl=3600)
    future_provenance = capacity_provenance(observed=future_stamp, ttl=3600)
    values = {item.name: getattr(capacity, item.name) for item in fields(capacity)}
    values.update(
        observed_at=future_stamp if future_fact == "capacity" else stamp,
        ttl_seconds=3600,
        state_provenance=future_provenance if future_fact == "state" else current_provenance,
        entitlement_provenance=future_provenance if future_fact == "entitlement" else current_provenance,
        zdr_provenance=future_provenance if future_fact == "zdr" else current_provenance,
    )
    if future_fact == "quota":
        values["quotas"] = (
            QuotaDimension("future", "credits", 100, 20, CAPACITY_UNKNOWN, future_provenance),
        )
    elif future_fact == "rate_limit":
        values["rate_limit"] = QuotaDimension(
            "requests_per_minute", "requests", 100, 80, CAPACITY_UNKNOWN, future_provenance,
        )
    elif future_fact == "price":
        values["price"] = PriceObservation(
            CostClass.METERED, published_list_rate=1, estimated_marginal_cost=CAPACITY_UNKNOWN,
            actual_billed_cost=CAPACITY_UNKNOWN, provenance=future_provenance,
            pricing_source=PricingSource.PROVIDER_API, pricing_version="fixture-v1",
        )
    future_capacity = make_capacity_receipt(**values)
    future_offer = _offer_for_capacity(
        record, future_capacity, credit=CreditEffect("credits", 2, "per_request_debit")
    )
    result = project_offer_quota(future_offer, future_capacity, now=current)
    assert result.status == "unknown"
    assert result.reason == "capacity_source_not_current"
    assert all(item.request_count is None for item in result.projections)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), -1])
def test_invalid_numeric_source_facts_are_refused_at_receipt_boundary(value):
    with pytest.raises(OfferReceiptError):
        CreditEffect("requests", value, "per_request_debit")
    with pytest.raises(CapacityError):
        QuotaDimension("requests", "requests", 100, value, provenance=capacity_provenance())
