"""Opt-in free-only preflight for actual external calls, never a route chooser.

Setting ODYSSEUS_FREE_OFFER_CONFIG makes every invocation require an exact,
fresh zero-price offer plus authoritative current capacity. Unbound profiles
fail closed, so a failed promotion can never fall through to a paid profile.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from src.provider_model_offer import provider_model_offer_from_dict


class PromotionUnavailable(ValueError):
    pass


def _check_quotas(capacity):
    quotas = list(capacity.quotas) + ([capacity.rate_limit] if capacity.rate_limit else [])
    for quota in quotas:
        if not isinstance(quota.remaining, (int, float)) or quota.remaining <= 0:
            raise PromotionUnavailable("current quota remaining is unknown or exhausted")


def prefer_verified_free(candidates, *, resolve_candidate):
    """Stable preference within explicit equivalent tiers, after policy routing.

    Does not add profiles, cross tiers, infer quality from cost-weighted routing
    scores, or authorize any call. Real request preflight must still recheck.
    """
    path = os.environ.get("ODYSSEUS_FREE_OFFER_CONFIG")
    if not path:
        return list(candidates)
    config = json.loads(Path(path).read_text())
    if config.get("prefer_verified_free") is not True:
        return list(candidates)
    tiers = config.get("quality_tiers", {})
    if not isinstance(tiers, dict):
        raise PromotionUnavailable("quality_tiers must be an explicit profile mapping")
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
        ranked = []
        for index, candidate in enumerate(result[start:end]):
            try:
                resolved = resolve_candidate(candidate)
                model, url = resolved[:2]
                identity = resolved[2] if len(resolved) > 2 else {}
                verified = enforce_free_offer(profile_id=candidate["profile_id"], model=model,
                    chat_url=url, harness="odysseus-scout", **identity) is not None
            except (PromotionUnavailable, ValueError, TypeError, KeyError):
                verified = False
            ranked.append((not verified, index, candidate))
        result[start:end] = [row[2] for row in sorted(ranked, key=lambda row: row[:2])]
        start = end
    return result


def configure_verified_offer(*, terms_path, source_path, capacity_store,
                             profile_id, chat_url, directory, maximum_predicted_request_usd=None):
    """Build a scoped receipt/config from operator-verified structured terms.

    Never parse marketing copy into a price. The verified terms document must
    include its preserved source hash and exact scope/time facts explicitly.
    """
    from src.provider_capacity_store import ProviderCapacityStore
    from src.provider_model_offer import (make_provider_model_offer_receipt,
        OfferProvenance, PriceEffect)
    from src.routing_outcomes import _write, _bytes
    terms = json.loads(Path(terms_path).read_text())
    if not isinstance(terms.get("verified_by"), str) or not terms["verified_by"].strip():
        raise PromotionUnavailable("terms require an explicit verifier identity")
    source = Path(source_path).read_bytes()
    if not source or hashlib.sha256(source).hexdigest() != terms["source_sha256"]:
        raise PromotionUnavailable("verified source hash mismatch")
    capacity = ProviderCapacityStore(capacity_store).current(terms["pool_id"])
    if (capacity is None or capacity.provider != terms["provider"]
            or (capacity.endpoint_url and capacity.endpoint_url != chat_url)
            or terms["native_model"] not in capacity.exposed_models
            or not capacity.has_usable_capacity_facts()):
        raise PromotionUnavailable("matching current capacity unavailable")
    _check_quotas(capacity)
    if not capacity.credential_sha256 or terms.get("credential_sha256") != capacity.credential_sha256:
        raise PromotionUnavailable("verified terms require authoritative credential fingerprint")
    from src.offer_economics import _money, comparable_cash
    if maximum_predicted_request_usd is None and (terms["offered_rate_usd"] != 0 or terms["unit"] != "request"):
        raise PromotionUnavailable("free-only activation requires zero USD per complete request")
    receipt = make_provider_model_offer_receipt(provider=terms["provider"],
        pool_id=terms["pool_id"], capacity_receipt_ref=capacity.ref,
        harness=terms["harness"], usage_path=terms["usage_path"], native_model=terms["native_model"],
        tariff_id=terms["tariff_id"], tariff_version=terms["tariff_version"],
        valid_from=terms["valid_from"], valid_until=terms["valid_until"],
        provenance=OfferProvenance(terms["source_url"], terms["source_sha256"],
            "operator_verified_provider_terms", terms["observed_at"], terms["ttl_seconds"]),
        price=PriceEffect("USD", terms["unit"], offered_rate=terms["offered_rate_usd"]))
    if not receipt.is_eligible(provider=capacity.provider, pool_id=capacity.pool_id,
            capacity_receipt_ref=capacity.ref, harness=terms["harness"],
            usage_path=terms["usage_path"], native_model=terms["native_model"]):
        raise PromotionUnavailable("verified terms are not currently effective")
    cash_mode = maximum_predicted_request_usd is not None
    if cash_mode:
        ceiling = _money(maximum_predicted_request_usd)
        quoted = comparable_cash([receipt], terms.get("workload"))
        if quoted > ceiling:
            raise PromotionUnavailable("verified terms exceed explicit request maximum")
    root = Path(directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise PromotionUnavailable("offer output directory must be private (0700)")
    receipt_path = root / (receipt.receipt_hash + ".json")
    retained_source = root / (terms["source_sha256"] + ".source")
    config_path = root / ("offer-config.json" if cash_mode else "free-offer-config.json")
    if config_path.exists():
        raise PromotionUnavailable("configuration already exists; choose a new output directory")
    _write(retained_source, source)
    _write(receipt_path, _bytes(receipt.to_dict()))
    config = {"capacity_store": str(Path(capacity_store).resolve()),
        "profiles": {profile_id: {"provider": capacity.provider, "pool_id": capacity.pool_id,
            "harness": terms["harness"], "usage_path": terms["usage_path"], "chat_url": chat_url,
            "credential_sha256": capacity.credential_sha256, "account_identity": capacity.account_identity,
            "endpoint_id": terms["endpoint_id"], "transport_provider": terms["transport_provider"],
            "offer_path": str(receipt_path), "source_path": str(retained_source)}}}
    if cash_mode:
        config.update(maximum_predicted_request_usd=str(ceiling), workload=terms["workload"])
    _write(config_path, _bytes(config))
    return str(config_path)


def enforce_free_offer(*, profile_id, model, chat_url, harness, provider=None, credential_sha256=None, endpoint_id=None, transport_provider=None):
    path = os.environ.get("ODYSSEUS_FREE_OFFER_CONFIG")
    if not path:
        return None
    try:
        from src.provider_capacity_store import ProviderCapacityStore
        config = json.loads(Path(path).read_text())
        scope = config["profiles"][profile_id]
        # Exact resolved URL, including path; no host-only matching or aliases.
        if scope["chat_url"] != chat_url or scope["harness"] != harness:
            raise ValueError("resolved endpoint/harness differs from promotion binding")
        if provider is not None and scope["provider"] != provider:
            raise ValueError("resolved provider differs from promotion binding")
        offer = provider_model_offer_from_dict(json.loads(Path(scope["offer_path"]).read_text()))
        source = Path(scope["source_path"]).read_bytes()
        if hashlib.sha256(source).hexdigest() != offer.provenance.content_sha256:
            raise ValueError("offer source bytes changed")
        capacity = ProviderCapacityStore(config["capacity_store"]).current(scope["pool_id"])
        if (capacity is None or capacity.provider != scope["provider"]
                or (capacity.endpoint_url and capacity.endpoint_url != chat_url)
                or model not in capacity.exposed_models or not capacity.has_usable_capacity_facts()):
            raise ValueError("current pool capacity is unavailable")
        _check_quotas(capacity)
        if (not credential_sha256 or credential_sha256 != capacity.credential_sha256
                or credential_sha256 != scope.get("credential_sha256")
                or capacity.account_identity != scope.get("account_identity")
                or endpoint_id != scope.get("endpoint_id") or transport_provider != scope.get("transport_provider")):
            raise PromotionUnavailable("resolved credential/account/endpoint/provider differs from offer binding")
        if not offer.is_eligible(provider=scope["provider"], pool_id=scope["pool_id"],
                capacity_receipt_ref="capacity:" + capacity.receipt_hash, harness=harness,
                usage_path=scope["usage_path"], native_model=model):
            raise ValueError("offer expired, stale, or bound to different capacity/path/model")
        if (offer.price.currency != "USD" or offer.price.unit != "request"
                or offer.price.offered_rate != 0):
            raise ValueError("zero USD tariff is not explicitly evidenced")
        return {"offer_ref": offer.ref, "capacity_receipt_ref": offer.capacity_receipt_ref,
                "source_sha256": offer.provenance.content_sha256}
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as exc:
        raise PromotionUnavailable("free offer preflight refused: " + str(exc)) from exc


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Prepare opt-in free-only routing from verified provider terms")
    for name in ("terms-path", "source-path", "capacity-store", "profile-id", "chat-url", "directory"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--maximum-predicted-request-usd", help="explicit opt-in bounded cash mode; requires workload in terms")
    args = parser.parse_args()
    print(configure_verified_offer(**vars(args)))


if __name__ == "__main__":
    main()
