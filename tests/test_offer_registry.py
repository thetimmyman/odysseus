"""Observable controls for versioned, descriptive offer economics."""
from dataclasses import replace
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.offer_terms import (
    EconomicObservation, EconomicSource, LinearTerm, ModelIdentity,
    OfferEconomicTerms, Promotion, RealizedCharge, UsageBand, UsageRule,
)
from src.offer_registry import build_economic_handoff, inspect_offers, validate_economic_handoff
from src.provider_model_offer import (
    OfferProvenance, OfferReceiptError, PriceEffect, make_provider_model_offer_receipt,
    provider_model_offer_from_dict,
)
from src.provider_model_offer_store import get_receipt, put_receipt
from tests.test_provider_model_offer import offer as legacy_offer


NOW = datetime(2026, 10, 13, 12, tzinfo=timezone.utc)
USAGE = {"input_tokens": 1200, "output_tokens": 300, "cache_read_tokens": 400,
         "cache_write_tokens": 0, "requests": 1, "tool_calls": 0}
SCOPE = {"provider": "synthetic-provider", "pool_id": "shared-pool",
         "capacity_receipt_ref": "capacity:" + "a" * 64,
         "harness": "synthetic-harness", "usage_path": "subscription",
         "native_model": "native/model"}


def source(kind="SUPPORTED_PROVIDER_API", *, observed="2026-10-13T11:00:00Z", version="v1"):
    return EconomicSource(kind, "https://provider.example/offers", version,
                          hashlib.sha256(version.encode()).hexdigest(), observed, 7200, "HIGH")


def fixed(amount, unit="USD", *, tier="default"):
    return UsageRule("linear-usage-v1", unit, "REQUEST", tier,
                     (UsageBand(0, None, (LinearTerm("requests", amount),)),))


def usage_rule():
    rates = {"input_tokens": "1.5", "output_tokens": "2", "cache_read_tokens": "0.1",
             "cache_write_tokens": "0", "requests": "0", "tool_calls": "0"}
    return UsageRule("linear-usage-v1", "USD", "USAGE", "default", (
        UsageBand(0, None, tuple(LinearTerm(k, v, 1_000_000) for k, v in rates.items())),))


def fact(kind="marginal_cash", amount="0", *, src=None, status="OBSERVED", rule=None):
    return EconomicObservation(kind, status, src or source(),
                               rule or (fixed(amount) if status == "OBSERVED" else None),
                               None if status == "OBSERVED" else "provider did not expose this fact")


def offer(*observations, **overrides):
    args = {**SCOPE, "tariff_id": "synthetic-plan", "tariff_version": "v1",
            "provenance": OfferProvenance("https://provider.example/offers", "b" * 64,
                                         "provider_offer", "2026-10-13T11:00:00Z", 7200),
            "schema_version": 2, "economic_terms": OfferEconomicTerms(
                observations=tuple(observations))}
    args.update(overrides)
    return make_provider_model_offer_receipt(**args)


def view(*offers, **kwargs):
    return inspect_offers(list(offers), scope=SCOPE, now=NOW, usage=USAGE, **kwargs)


def amount(result):
    return Fraction(int(result["quantity"]["numerator"]), int(result["quantity"]["denominator"]))


def test_v1_bytes_and_hashes_are_unchanged_and_v2_uses_existing_store(tmp_path):
    old = legacy_offer()
    # Verified against the parser at the pinned pre-change dev revision.
    assert old.receipt_hash == "3c5cdebd76c129da17352ef6949a6e625d403eaca052c54932066ac3643d44d9"
    before = json.dumps(old.to_dict(), sort_keys=True)
    assert "economic_terms" not in old.to_dict()
    assert provider_model_offer_from_dict(old.to_dict()) == old
    new = offer(fact())
    directory = tmp_path / "offers"
    directory.mkdir(mode=0o700)
    put_receipt(directory, old)
    put_receipt(directory, new)
    assert get_receipt(directory, old.receipt_hash).to_dict() == json.loads(before)
    assert get_receipt(directory, new.receipt_hash) == new
    assert new.ref != old.ref


def test_zero_cash_is_separate_from_quota_burn_and_list_tariff():
    record = offer(fact(), fact("quota_burn", rule=fixed("5", "native-credit")),
                   fact("published_tariff", "2"), fact("effective_tariff", "1"))
    result = view(record)
    facts = result["dimensions"]
    assert amount(facts["marginal_cash"]) == 0
    assert amount(facts["quota_burn"]) == 5
    assert facts["quota_burn"]["unit"] == "native-credit"
    assert amount(facts["published_tariff"]) == 2
    assert result["ranking_allowed"] is False
    assert result["outcome_economics"]["status"] == "UNKNOWN"


@pytest.mark.parametrize("status", ["UNKNOWN", "ABSENT"])
def test_missing_tariff_never_becomes_free(status):
    result = view(offer(fact(status=status)))["dimensions"]["marginal_cash"]
    assert result["status"] == status
    assert "quantity" not in result


def test_higher_authority_unknown_is_not_overwritten_by_advertised_zero():
    official = offer(fact(status="UNKNOWN"))
    catalog = offer(fact(src=source("THIRD_PARTY_REGISTRY", version="catalog")))
    result = view(official, catalog)
    assert result["dimensions"]["marginal_cash"]["status"] == "UNKNOWN"
    assert len(result["discrepancies"]) == 1
    assert {r["offer_ref"] for r in result["observations"]} == {official.ref, catalog.ref}


def test_equal_authority_time_conflict_is_unknown_and_retained():
    result = view(offer(fact(amount="1")), offer(fact(amount="2")))
    assert result["dimensions"]["marginal_cash"]["status"] == "UNKNOWN"
    assert result["dimensions"]["marginal_cash"]["reason"] == "source_conflict"
    assert len(result["discrepancies"]) == 1


def test_stale_and_future_sources_are_not_refreshed_by_reading():
    for stamp in ["2026-10-13T09:00:00Z", "2026-10-13T13:00:00Z"]:
        result = view(offer(fact(src=source(observed=stamp))))
        assert result["dimensions"]["marginal_cash"]["status"] == "UNKNOWN"
        assert result["observations"][0]["current"] is False


def test_expired_promotion_preserves_history_and_handoff_identity():
    old = offer(fact(), valid_from="2026-10-13T10:00:00Z", valid_until="2026-10-13T12:00:00Z")
    new = offer(fact(amount="3"), tariff_version="v2")
    result = view(old, new)
    assert amount(result["dimensions"]["marginal_cash"]) == 3
    handoff = build_economic_handoff(old, execution_ref="attempt:" + "c" * 64,
        executed_at="2026-10-13T11:30:00Z", scope=SCOPE, usage=USAGE)
    assert handoff["offer_ref"] == old.ref
    assert amount(handoff["dimensions"]["marginal_cash"]) == 0
    assert provider_model_offer_from_dict(old.to_dict()).receipt_hash == old.receipt_hash


def test_shared_subscription_is_one_pool_but_harnesses_and_models_remain_scoped():
    first = offer(fact())
    other = offer(fact(amount="8"), harness="other-harness")
    different_model = offer(fact(amount="9"), native_model="other/model")
    result = view(first, other, different_model)
    assert result["distinct_pool_n"] == 1
    assert amount(result["dimensions"]["marginal_cash"]) == 0
    assert sum(row["scope_matches"] for row in result["offers"]) == 1


def test_alias_mapping_is_source_bound_and_never_a_native_scope_substitute():
    identity = ModelIdentity("OBSERVED", "native/model", "family/model", "friendly", source(), None)
    record = offer(fact(), economic_terms=OfferEconomicTerms(
        observations=(fact(),), model_identity=identity))
    result = view(record)
    assert result["offers"][0]["model_identity"]["canonical_family"] == "family/model"
    result = inspect_offers([record], scope={**SCOPE, "native_model": "friendly"}, now=NOW, usage=USAGE)
    assert result["dimensions"]["marginal_cash"]["status"] == "ABSENT"
    with pytest.raises(ValueError):
        ModelIdentity("OBSERVED", "native/model", "family/model", "friendly", None, None)


def test_token_rule_preserves_cache_categories_and_decimal_precision():
    result = view(offer(fact(rule=usage_rule())))["dimensions"]["marginal_cash"]
    assert amount(result) == Fraction(244, 100000)
    assert result["quantity"]["decimal"] == "0.00244"
    assert result["basis"] == "ESTIMATED_FROM_RULE"
    missing = dict(USAGE)
    del missing["cache_read_tokens"]
    result = inspect_offers([offer(fact(rule=usage_rule()))], scope=SCOPE, now=NOW, usage=missing)
    assert result["dimensions"]["marginal_cash"]["status"] == "UNKNOWN"


def test_context_tiers_have_exact_boundaries_and_service_tier_is_explicit():
    rule = UsageRule("linear-usage-v1", "credit", "REQUEST", "priority", (
        UsageBand(0, 100, (LinearTerm("requests", "1"),)),
        UsageBand(100, None, (LinearTerm("requests", "3"),))))
    record = offer(fact("quota_burn", rule=rule))
    assert view(record)["dimensions"]["quota_burn"]["status"] == "UNKNOWN"
    for context, expected in [(99, 1), (100, 3)]:
        result = view(record, service_tier="priority", context_tokens=context)
        assert amount(result["dimensions"]["quota_burn"]) == expected
    with pytest.raises(ValueError):
        replace(rule, bands=(rule.bands[0], replace(rule.bands[1], minimum_context=99)))


def test_promotion_changes_quota_without_rewriting_published_price():
    promotion = Promotion("bonus-v1", ("quota_burn",), source(), "Twofold quota bonus")
    terms = OfferEconomicTerms(observations=(fact("published_tariff", "4"),
        fact("quota_burn", rule=fixed("0.5", "credit"))), promotions=(promotion,))
    record = offer(economic_terms=terms)
    result = view(record)
    assert amount(result["dimensions"]["published_tariff"]) == 4
    assert amount(result["dimensions"]["quota_burn"]) == Fraction(1, 2)
    assert record.to_dict()["economic_terms"]["promotions"][0]["affects"] == ["quota_burn"]
    with pytest.raises(ValueError):
        Promotion("bonus", ("published_tariff",), source(), "Discounts cannot rewrite list price")


def test_one_time_allowance_bonus_is_not_multiplied_by_request_count():
    bonus = UsageRule("linear-usage-v1", "credit", "FIXED", "default", (
        UsageBand(0, None, (LinearTerm("constant", "100"),)),))
    record = offer(fact("allowance_bonus", rule=bonus))
    result = inspect_offers([record], scope=SCOPE, now=NOW, usage={**USAGE, "requests": 5})
    assert amount(result["dimensions"]["allowance_bonus"]) == 100
    assert result["dimensions"]["allowance_bonus"]["is_actual_capacity_change"] is False
    with pytest.raises(ValueError):
        fact("marginal_cash", rule=bonus)


def test_realized_charge_is_attempt_bound_and_separate_from_quote():
    ref = "attempt:" + "c" * 64
    charge = RealizedCharge(ref, "USD", "0.25", source("PROVIDER_BILLING_RECORD", observed="2026-10-13T11:31:00Z"))
    handoff = build_economic_handoff(offer(fact(amount="0.3")), execution_ref=ref,
        executed_at="2026-10-13T11:30:00Z", scope=SCOPE, usage=USAGE, realized_charge=charge)
    assert handoff["realized_charge"]["amount"] == "0.25"
    assert amount(handoff["dimensions"]["marginal_cash"]) == Fraction(3, 10)
    assert handoff["discrepancies"]
    assert handoff["acceptance"] == {"status": "UNKNOWN", "owner": "execution_outcomes"}
    with pytest.raises(ValueError):
        build_economic_handoff(offer(fact()), execution_ref="attempt:" + "d" * 64,
            executed_at="2026-10-13T11:30:00Z", scope=SCOPE, usage=USAGE, realized_charge=charge)
    with pytest.raises(ValueError):
        replace(charge, source=source("THIRD_PARTY_REGISTRY"))


def test_resealed_malformed_terms_and_legacy_scalar_v2_are_rejected():
    raw = offer(fact()).to_dict()
    raw["economic_terms"]["observations"][0]["rule"]["bands"][0]["terms"][0]["amount"] = True
    core = {k: v for k, v in raw.items() if k != "receipt_hash"}
    raw["receipt_hash"] = hashlib.sha256(json.dumps(core, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    with pytest.raises(ValueError):
        provider_model_offer_from_dict(raw)
    with pytest.raises(OfferReceiptError):
        offer(fact(), price=PriceEffect("USD", "request", 1, 0))


@pytest.mark.parametrize("bad", [True, "NaN", "Infinity", "-1", "1e999", " 1"])
def test_rule_amounts_are_explicit_finite_nonnegative_decimals(bad):
    with pytest.raises(ValueError):
        LinearTerm("requests", bad)


def test_billing_class_requires_capacity_owner_and_conflicts_remain_visible(tmp_path):
    from src.provider_capacity import CostClass, PriceObservation, PricingSource
    from src.provider_capacity_store import ProviderCapacityStore, CapacityStoreError
    from tests.test_ps640_provider_capacity import prov, receipt
    stamp = "2026-10-13T11:59:00Z"
    capacity = receipt(provider=SCOPE["provider"], pool_id=SCOPE["pool_id"],
        observed_at=stamp, exposed_models=(SCOPE["native_model"],),
        price=PriceObservation(CostClass.SUBSCRIPTION_SUNK_COST,
            provenance=prov(observed=stamp), pricing_source=PricingSource.SUBSCRIPTION_CONTRACT,
            pricing_version="synthetic-plan-v1"))
    scope = {**SCOPE, "capacity_receipt_ref": capacity.ref}
    store = ProviderCapacityStore(str(tmp_path / "capacity"))
    store.append(capacity)
    terms = OfferEconomicTerms(observations=(fact(),), billing_class="METERED", billing_source=source())
    record = offer(economic_terms=terms, capacity_receipt_ref=capacity.ref)
    unbound = inspect_offers([record], scope=scope, now=NOW, usage=USAGE)
    assert unbound["offers"][0]["billing_class"] == "UNKNOWN"
    bound = inspect_offers([record], scope=scope, now=NOW, usage=USAGE, capacity_store=store)
    assert bound["capacity_binding"]["billing_class"] == "SUBSCRIPTION_SUNK_COST"
    assert bound["offers"][0]["billing_class"] == "UNKNOWN"
    assert bound["offers"][0]["billing_class_conflict"] is True
    assert bound["discrepancies"][0]["dimension"] == "billing_class"
    inherited = offer(fact(), capacity_receipt_ref=capacity.ref)
    view = inspect_offers([inherited], scope=scope, now=NOW, usage=USAGE, capacity_store=store)
    assert view["offers"][0]["billing_class"] == "SUBSCRIPTION_SUNK_COST"
    Path(store.index_path).unlink()
    with pytest.raises(CapacityStoreError):
        inspect_offers([record], scope=scope, now=NOW, usage=USAGE, capacity_store=store)


def test_provider_precedence_beats_newer_operator_fact_and_input_order_is_stable():
    provider = offer(fact(amount="2"))
    operator = offer(fact(amount="0", src=source("OPERATOR_DECLARED", observed="2026-10-13T11:30:00Z")))
    first = view(provider, operator, provider)
    second = view(operator, provider)
    assert first == second
    assert amount(first["dimensions"]["marginal_cash"]) == 2
    assert len(first["offers"]) == 2


def test_two_explicit_service_tiers_do_not_conflict_with_each_other():
    record = offer(fact(rule=fixed("1")), fact(rule=fixed("3", tier="priority")))
    assert amount(view(record)["dimensions"]["marginal_cash"]) == 1
    priority = view(record, service_tier="priority")
    assert amount(priority["dimensions"]["marginal_cash"]) == 3
    assert priority["discrepancies"] == []


def test_equivalent_decimal_rates_and_units_are_not_source_conflicts():
    one = offer(fact(amount="1"))
    another = offer(fact(rule=UsageRule("linear-usage-v1", "USD", "REQUEST", "default", (
        UsageBand(0, None, (LinearTerm("requests", "1000.000", 1000),)),))))
    assert one.ref != another.ref
    result = view(one, another)
    assert amount(result["dimensions"]["marginal_cash"]) == 1
    assert result["discrepancies"] == []


def test_handoff_consumer_recomputes_rules_instead_of_trusting_resealed_amount():
    record = offer(fact(amount="2"))
    handoff = build_economic_handoff(record, execution_ref="attempt:" + "c" * 64,
        executed_at="2026-10-13T11:30:00Z", scope=SCOPE, usage=USAGE)
    restored = json.loads(json.dumps(handoff))
    assert validate_economic_handoff(restored, record)["handoff_hash"] == handoff["handoff_hash"]
    restored["dimensions"]["marginal_cash"]["quantity"]["numerator"] = "9"
    from src.offer_terms import digest
    restored["handoff_hash"] = digest({k: v for k, v in restored.items() if k != "handoff_hash"})
    with pytest.raises(ValueError):
        validate_economic_handoff(restored, record)


def test_nonterminating_quantities_stay_exact_and_invalid_usage_cannot_quote():
    record = offer(fact(rule=UsageRule("linear-usage-v1", "USD", "REQUEST", "default", (
        UsageBand(0, None, (LinearTerm("requests", "1", 3),)),))))
    result = view(record)["dimensions"]["marginal_cash"]
    assert amount(result) == Fraction(1, 3)
    assert result["quantity"]["decimal"] is None
    for usage in [{**USAGE, "input_tokens": True}, {**USAGE, "reasoning_tokens": 9},
                  {**USAGE, "requests": -1}]:
        with pytest.raises(ValueError):
            inspect_offers([record], scope=SCOPE, now=NOW, usage=usage)


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
def test_unnormalizable_source_dates_are_rejected(stamp):
    with pytest.raises(ValueError):
        source(observed=stamp)


def test_cli_reads_explicit_stored_objects_without_mutation_and_refuses_bad_input(tmp_path):
    directory = tmp_path / "offers"
    directory.mkdir(mode=0o700)
    record = put_receipt(directory, offer(fact()))
    scope_path, usage_path = tmp_path / "scope.json", tmp_path / "usage.json"
    scope_path.write_text(json.dumps(SCOPE))
    usage_path.write_text(json.dumps(USAGE))
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
    command = [sys.executable, "-m", "src.offer_registry", "--store", str(directory),
               "--offer-ref", record.ref, "--scope", str(scope_path), "--usage", str(usage_path),
               "--now", NOW.isoformat()]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert amount(json.loads(result.stdout)["dimensions"]["marginal_cash"]) == 0
    after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in directory.iterdir()}
    assert before == after
    scope_path.write_text('{"provider":"a","provider":"b"}')
    refused = subprocess.run(command, capture_output=True, text=True)
    assert refused.returncode == 2
    assert not refused.stdout
    assert "duplicate inspection input field" in refused.stderr
