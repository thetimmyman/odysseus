from copy import deepcopy
from datetime import datetime, timezone

import pytest

from src.provider_model_offer import (
    UNKNOWN, AllowanceEffect, CreditEffect, OfferProvenance, OfferReceiptError,
    PriceEffect, make_provider_model_offer_receipt, provider_model_offer_from_dict,
)


NOW = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)


def offer(**overrides):
    args = {
        "provider": "provider-a", "pool_id": "pool-a",
        "capacity_receipt_ref": "capacity:" + "a" * 64,
        "harness": "command-code", "usage_path": "subscription-cli",
        "native_model": "vendor/model-x", "tariff_id": "standard",
        "tariff_version": "2026-09-01",
        "provenance": OfferProvenance(
            "https://provider.example/pricing", "b" * 64, "provider_pricing_page",
            "2026-09-24T11:00:00Z", 7200),
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_until": "2026-10-01T00:00:00Z",
        "price": PriceEffect("USD", "1M tokens", 2.0, 0.0),
        "credit": CreditEffect("USD", 0.0, "per request"),
        "allowance": AllowanceEffect("tokens", 100000, "weekly"),
    }
    args.update(overrides)
    return make_provider_model_offer_receipt(**args)


def scope(**overrides):
    args = {"now": NOW, "provider": "provider-a", "pool_id": "pool-a",
            "capacity_receipt_ref": "capacity:" + "a" * 64,
            "harness": "command-code", "usage_path": "subscription-cli",
            "native_model": "vendor/model-x"}
    args.update(overrides)
    return args


def test_unknown_is_distinct_from_zero_and_round_trips():
    record = offer(price=PriceEffect("USD", "1M tokens", UNKNOWN, 0.0),
                   credit=CreditEffect("USD", UNKNOWN),
                   allowance=AllowanceEffect("tokens", 0, "weekly"))
    assert record.price.list_rate is UNKNOWN
    assert record.price.offered_rate == 0.0
    assert record.credit.amount is UNKNOWN
    assert record.allowance.amount == 0
    restored = provider_model_offer_from_dict(record.to_dict())
    assert restored == record


def test_discount_price_does_not_change_credit_or_allowance():
    record = offer(price=PriceEffect("USD", "1M tokens", 2.0, 1.25),
                   credit=CreditEffect("USD", 5.0, "account"),
                   allowance=AllowanceEffect("requests", 20, "month"))
    assert record.price.offered_rate == 1.25
    assert record.credit.amount == 5.0
    assert record.allowance.amount == 20


@pytest.mark.parametrize("field,value", [
    ("provider", "provider-b"),
    ("pool_id", "pool-other"),
    ("capacity_receipt_ref", "capacity:" + "c" * 64),
    ("harness", "other-harness"),
    ("usage_path", "api-key"),
    ("native_model", "other/model"),
])
def test_exact_scope_mismatch_is_ineligible(field, value):
    assert not offer().is_eligible(**scope(**{field: value}))


def test_validity_interval_edges_and_unknown_dates():
    record = offer()
    assert record.is_eligible(**scope())
    assert not offer(valid_from="2026-09-24T12:00:01Z").is_eligible(**scope())
    assert not offer(valid_until="2026-09-24T12:00:00Z").is_eligible(**scope())
    assert not offer(valid_until=UNKNOWN).is_eligible(**scope())


def test_observation_freshness_rejects_future_and_expired():
    future = offer(provenance=OfferProvenance(
        "https://provider.example/pricing", "b" * 64, "provider_pricing_page",
        "2026-09-24T13:00:00Z", 7200))
    stale = offer(provenance=OfferProvenance(
        "https://provider.example/pricing", "b" * 64, "provider_pricing_page",
        "2026-09-24T10:00:00Z", 7200))
    assert not future.is_eligible(**scope())
    assert not stale.is_eligible(**scope())


def test_rejects_negative_nonfinite_and_bad_dates():
    with pytest.raises(OfferReceiptError):
        PriceEffect("USD", "1M tokens", -1, 0)
    with pytest.raises(OfferReceiptError):
        PriceEffect("USD", "1M tokens", float("inf"), 0)
    with pytest.raises(OfferReceiptError):
        offer(valid_from="2026-09-01T00:00:00")
    with pytest.raises(OfferReceiptError):
        offer(capacity_receipt_ref="capacity:garbage")
    with pytest.raises(OfferReceiptError):
        offer(schema_version=True)
    with pytest.raises(OfferReceiptError):
        OfferProvenance("https:bogus", "b" * 64, "pricing_page", "2026-09-24T11:00:00Z", 60)
    with pytest.raises(OfferReceiptError):
        offer(provenance={"source_url": "https://provider.example"})


def test_serialized_tampering_is_detected():
    payload = deepcopy(offer().to_dict())
    payload["price"]["offered_rate"] = 0.01
    with pytest.raises(OfferReceiptError, match="hash"):
        provider_model_offer_from_dict(payload)


def test_nested_effects_are_frozen():
    record = offer()
    with pytest.raises((AttributeError, TypeError)):
        record.price.offered_rate = 0.0
    with pytest.raises((AttributeError, TypeError)):
        record.provenance.ttl_seconds = 1
