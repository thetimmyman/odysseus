"""Comparable, bounded request quotes; never authorization or actual spend."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path

from src.provider_model_offer import provider_model_offer_from_dict
from src.provider_capacity_store import ProviderCapacityStore
from src.promotional_dispatch import PromotionUnavailable, _check_quotas

TOKEN_UNITS = {"million_input_tokens": "input_tokens", "million_output_tokens": "output_tokens",
               "million_cache_read_tokens": "cache_read_tokens", "million_cache_write_tokens": "cache_write_tokens"}

def credential_fingerprint(headers):
    # Bind the full resolved header set, including Cookie/vendor-specific auth.
    # Filtering known names would permit an unknown credential header to change
    # accounts at the same endpoint. Benign header changes fail closed too.
    credentials = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    return hashlib.sha256(json.dumps(credentials, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

def quote_digest(quote):
    return hashlib.sha256(json.dumps(quote, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _money(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise PromotionUnavailable("cash quote requires an explicit numeric USD rate")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise PromotionUnavailable("invalid USD rate") from exc
    if not number.is_finite() or number < 0:
        raise PromotionUnavailable("USD rates must be finite and nonnegative")
    return number


def workload_counts(workload):
    if not isinstance(workload, dict) or set(workload) != set(TOKEN_UNITS.values()):
        raise PromotionUnavailable("workload requires uncached input, output, cache-read and cache-write counts")
    if any(type(v) is not int or v < 0 for v in workload.values()):
        raise PromotionUnavailable("predicted token counts must be nonnegative integers")
    return dict(workload)


def comparable_cash(offers, workload, *, list_price=False):
    """A whole-request tariff OR complete component rates, never both.

    input_tokens is uncached input only; cache counts are disjoint. Counts are
    predictions, not measurements. Missing active components are unknown.
    """
    counts = workload_counts(workload)
    rates = {}
    for offer in offers:
        price = offer.price
        if price.currency != "USD" or price.unit in rates:
            raise PromotionUnavailable("cash requires unique comparable USD tariff units")
        rates[price.unit] = _money(price.list_rate if list_price else price.offered_rate)
    if set(rates) == {"request"}:
        return rates["request"]
    if not rates or set(rates) - set(TOKEN_UNITS):
        raise PromotionUnavailable("incomparable or mixed request/token tariff units")
    if not any(counts.values()):
        raise PromotionUnavailable("token tariffs require a nonempty predicted workload")
    total = Decimal(0)
    for unit, count_name in TOKEN_UNITS.items():
        if counts[count_name] and unit not in rates:
            raise PromotionUnavailable("missing tariff for predicted " + count_name)
        total += rates.get(unit, Decimal(0)) * counts[count_name] / Decimal(1_000_000)
    return total


def quote_profile(config, *, profile_id, model, chat_url, harness, workload, provider=None, now=None, credential_sha256=None, endpoint_id=None, transport_provider=None):
    """Read-only quote from preserved source and authoritative current capacity."""
    current = now or datetime.now(timezone.utc)
    counts = workload_counts(workload)
    scope = config["profiles"][profile_id]
    if scope["chat_url"] != chat_url or scope["harness"] != harness:
        raise PromotionUnavailable("quote endpoint or harness does not match")
    if provider is not None and scope["provider"] != provider:
        raise PromotionUnavailable("quote provider does not match")
    capacity = ProviderCapacityStore(config["capacity_store"]).current(scope["pool_id"])
    if (capacity is None or capacity.provider != scope["provider"]
            or (capacity.endpoint_url and capacity.endpoint_url != chat_url)
            or model not in capacity.exposed_models or not capacity.has_usable_capacity_facts(now=current)):
        raise PromotionUnavailable("current capacity is unavailable or stale")
    _check_quotas(capacity)
    if (not credential_sha256 or credential_sha256 != capacity.credential_sha256
            or credential_sha256 != scope.get("credential_sha256")
            or capacity.account_identity != scope.get("account_identity")):
        raise PromotionUnavailable("resolved credential/account does not match authoritative capacity")
    if endpoint_id != scope.get("endpoint_id") or transport_provider != scope.get("transport_provider"):
        raise PromotionUnavailable("resolved endpoint/provider identity differs from offer scope")
    offers = []
    paths = scope.get("offer_paths") or [scope["offer_path"]]
    for path in paths:
        offer = provider_model_offer_from_dict(json.loads(Path(path).read_text()))
        source_path = (scope.get("source_paths") or {}).get(offer.provenance.content_sha256) or scope.get("source_path")
        if not source_path or hashlib.sha256(Path(source_path).read_bytes()).hexdigest() != offer.provenance.content_sha256:
            raise PromotionUnavailable("quote source bytes are missing or changed")
        if not offer.is_eligible(now=current, provider=capacity.provider, pool_id=capacity.pool_id,
                capacity_receipt_ref=capacity.ref, harness=harness, usage_path=scope["usage_path"], native_model=model):
            raise PromotionUnavailable("offer is stale, expired or scope mismatched")
        offers.append(offer)
    cash = comparable_cash(offers, counts)
    ceiling = _money(config["maximum_predicted_request_usd"])
    if cash > ceiling:
        raise PromotionUnavailable("predicted request exceeds explicit maximum_predicted_request_usd")
    try:
        list_cash = comparable_cash(offers, counts, list_price=True)
    except PromotionUnavailable:
        list_cash = None
    quota_effects = []
    for offer in offers:
        credit = offer.credit
        if credit.applies_to != "per_request_debit" or not isinstance(credit.amount, (int, float)):
            continue
        for quota in capacity.quotas:
            if quota.unit == credit.unit and isinstance(quota.remaining, (int, float)):
                debit = Decimal(str(credit.amount))
                quota_effects.append({"offer_ref": offer.ref, "dimension": quota.name,
                    "unit": quota.unit, "remaining": quota.remaining, "debit_per_request": credit.amount,
                    "requests_from_this_dimension": int(Decimal(str(quota.remaining)) // debit) if debit > 0 else None,
                    "cash_equivalent_usd": None})
    return {"profile_id": profile_id, "provider": capacity.provider, "pool_id": capacity.pool_id,
            "harness": harness, "usage_path": scope["usage_path"], "model": model, "chat_url": chat_url,
            "observed_at": current.isoformat(), "workload": counts,
            "credential_sha256": credential_sha256, "endpoint_id": endpoint_id,
            "transport_provider": transport_provider, "account_identity": capacity.account_identity,
            "predicted_cash_usd": str(cash), "maximum_predicted_request_usd": str(ceiling),
            "predicted_list_cash_usd": str(list_cash) if list_cash is not None else None,
            "predicted_savings_usd": str(max(Decimal(0), list_cash - cash)) if list_cash is not None else None,
            "offer_refs": [offer.ref for offer in offers], "offers": [offer.to_dict() for offer in offers],
            "capacity_receipt_ref": capacity.ref, "capacity": capacity.to_dict(),
            "native_effects": [{"offer_ref": o.ref, "credit": o.credit.to_dict(), "allowance": o.allowance.to_dict()}
                               for o in offers],
            "effective_quota": quota_effects,
            "basis": "predicted_cash_only; native credits and allowance are not USD"}


def configured_quote(*, profile_id, model, chat_url, harness, workload=None, provider=None, now=None, **identity):
    free_path = os.environ.get("ODYSSEUS_FREE_OFFER_CONFIG")
    path = os.environ.get("ODYSSEUS_OFFER_CONFIG")
    if free_path and path:
        raise PromotionUnavailable("choose one scoped free-only or bounded-cash configuration")
    if free_path:
        from src.promotional_dispatch import enforce_free_offer
        enforce_free_offer(profile_id=profile_id, model=model, chat_url=chat_url, harness=harness, provider=provider, **identity)
        config = json.loads(Path(free_path).read_text())
        config["maximum_predicted_request_usd"] = 0
    elif path:
        config = json.loads(Path(path).read_text())
        if workload is None:
            raise PromotionUnavailable("bounded-cash dispatch requires explicit predicted workload")
    else:
        return None
    return quote_profile(config, profile_id=profile_id, model=model, chat_url=chat_url,
        harness=harness, provider=provider, now=now, workload=workload or
        {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0}, **identity)


def prefer_discounted(candidates, *, resolve_candidate, workload):
    path = os.environ.get("ODYSSEUS_OFFER_CONFIG")
    if not path:
        return list(candidates)
    config = json.loads(Path(path).read_text())
    if config.get("prefer_discounted") is not True:
        return list(candidates)
    tiers = config.get("quality_tiers", {})
    result = list(candidates)
    start = 0
    while start < len(result):
        tier = tiers.get(result[start].get("profile_id"))
        end = start + 1
        if not isinstance(tier, str) or not tier:
            start = end
            continue
        while end < len(result) and tiers.get(result[end].get("profile_id")) == tier:
            end += 1
        quotes = []
        for index, candidate in enumerate(result[start:end]):
            try:
                resolved = resolve_candidate(candidate)
                model, url = resolved[:2]
                identity = resolved[2] if len(resolved) > 2 else {}
                quote = quote_profile(config, profile_id=candidate["profile_id"], model=model,
                    chat_url=url, harness="odysseus-scout", workload=workload(candidate) if callable(workload) else workload, **identity)
                quotes.append((Decimal(quote["predicted_cash_usd"]), index, candidate))
            except (PromotionUnavailable, OSError, ValueError, KeyError, TypeError):
                # Unknown candidates retain the entire tier's current order:
                # no preference is inferred from incomplete comparative data.
                quotes = []
                break
        if quotes:
            result[start:end] = [entry[2] for entry in sorted(quotes, key=lambda x: x[:2])]
        start = end
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Inspect source-backed inference quote; never calls inference")
    parser.add_argument("--config", required=True)
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--chat-url", required=True)
    parser.add_argument("--harness", required=True)
    parser.add_argument("--workload", required=True, help="JSON file with disjoint predicted token counts")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    scope = config["profiles"][args.profile_id]
    result = quote_profile(config, profile_id=args.profile_id,
        model=args.model, chat_url=args.chat_url, harness=args.harness,
        workload=json.loads(Path(args.workload).read_text()),
        **{k: scope[k] for k in ("credential_sha256", "endpoint_id", "transport_provider")})
    result["identity_basis"] = "configured identity inspected; execution must measure credentials independently"
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
