"""Read-only projections of explicit historical offers; no routing authority."""
from __future__ import annotations

from datetime import datetime, timezone
from fractions import Fraction
import json
import re
from typing import Any, Mapping

from src.offer_terms import (
    DIMENSIONS, TARIFF_PRECEDENCE, RealizedCharge, decimal_amount, digest, text, timestamp,
)
from src.provider_model_offer import ProviderModelOfferReceipt, provider_model_offer_from_dict

SCOPE_FIELDS = frozenset({"provider", "pool_id", "capacity_receipt_ref", "harness", "usage_path", "native_model"})


def _receipt(value: ProviderModelOfferReceipt | Mapping[str, Any]) -> ProviderModelOfferReceipt:
    return provider_model_offer_from_dict(value.to_dict() if isinstance(value, ProviderModelOfferReceipt) else value)


def _quantity(value: Fraction) -> dict[str, str | None]:
    # Return an exact rational even for nonterminating decimal prices.
    denominator, twos, fives = value.denominator, 0, 0
    while denominator % 2 == 0:
        twos += 1
        denominator //= 2
    while denominator % 5 == 0:
        fives += 1
        denominator //= 5
    decimal = None
    if denominator == 1:
        places = max(twos, fives)
        scaled = value.numerator * 2 ** (places - twos) * 5 ** (places - fives)
        digits = str(scaled).zfill(places + 1)
        decimal = (digits[:-places] + "." + digits[-places:]).rstrip("0").rstrip(".") if places else digits
    return {"numerator": str(value.numerator), "denominator": str(value.denominator), "decimal": decimal}


def _semantic(observation) -> str:
    rule = observation.rule.to_dict() if observation.rule else None
    if rule is not None:
        # Source bytes and rule hashes stay intact. Compare exact economic
        # coefficients so equivalent decimal spellings/order are not conflicts.
        for band in rule["bands"]:
            coefficients = []
            for term in band["terms"]:
                value = decimal_amount(term["amount"]) / term["per_units"]
                coefficients.append({"dimension": term["dimension"],
                                     "numerator": str(value.numerator), "denominator": str(value.denominator)})
            band["terms"] = sorted(coefficients, key=lambda t: t["dimension"])
    return digest({"status": observation.status,
                   "rule": rule})


def inspect_offers(receipts, *, scope: Mapping[str, str], now: datetime | None = None,
                   usage: Mapping[str, int] | None = None, service_tier: str = "default",
                   context_tokens: int | None = None, capacity_store=None) -> dict[str, Any]:
    """Project exact-scope, fresh source facts without creating a current pointer.

    Caller-selected receipts and caller-supplied usage are not independently
    verified provider facts. Source hashes prove integrity, not source truth.
    """
    if not isinstance(scope, Mapping) or set(scope) != SCOPE_FIELDS:
        raise ValueError("registry scope requires exactly provider/pool/capacity/harness/path/native model")
    for key, value in scope.items():
        text(value, key)
    current = now if now is not None else datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("registry time must include a timezone")
    current = current.astimezone(timezone.utc)
    text(service_tier, "service tier")
    if not isinstance(receipts, (list, tuple)) or len(receipts) > 1000:
        raise ValueError("registry requires at most 1000 explicitly selected offers")
    capacity_class = "UNKNOWN"
    capacity_binding = {"status": "UNKNOWN", "reason": "capacity owner store not supplied"}
    if capacity_store is not None:
        from src.provider_capacity_store import ProviderCapacityStore
        if not isinstance(capacity_store, ProviderCapacityStore):
            raise ValueError("capacity classification requires the existing owner store")
        capacity = capacity_store.current(scope["pool_id"])
        if (capacity is not None and capacity.ref == scope["capacity_receipt_ref"]
                and capacity.provider == scope["provider"] and capacity.pool_id == scope["pool_id"]
                and timestamp(capacity.observed_at) <= current and capacity.price is not None
                and capacity.price.provenance is not None
                and timestamp(capacity.price.provenance.observed_at) <= current
                and capacity.price.is_fresh(now=current)):
            classes = {"subscription_sunk_cost": "SUBSCRIPTION_SUNK_COST", "metered": "METERED",
                       "local_zero_marginal_dollars": "LOCAL"}
            capacity_class = classes[capacity.price.cost_class.value]
            capacity_binding = {"status": "OBSERVED", "capacity_receipt_ref": capacity.ref,
                                "billing_class": capacity_class,
                                "source": capacity.price.provenance.to_dict()}
        else:
            capacity_binding = {"status": "UNKNOWN", "reason": "current capacity/price binding unavailable"}
    unique = {}
    for raw in receipts:
        record = _receipt(raw)
        unique[record.ref] = record
    offers, observations, candidates = [], [], []
    pools = set()
    for ref, record in sorted(unique.items()):
        pools.add((record.provider, record.pool_id))
        matches = all(getattr(record, key) == value for key, value in scope.items())
        eligible = matches and record.is_eligible(now=current, **scope)
        terms = record.economic_terms
        identity = terms.model_identity.to_dict() if terms else {"status": "ABSENT"}
        identity_current = bool(eligible and terms and terms.model_identity.source
                                and terms.model_identity.source.is_current(current))
        if identity.get("status") == "OBSERVED" and not identity_current:
            identity = {"status": "UNKNOWN", "reason": "mapping_source_not_current"}
        declared_class = (terms.billing_class if terms and eligible and terms.billing_source
                          and terms.billing_source.is_current(current) else "UNKNOWN")
        class_conflict = matches and declared_class != "UNKNOWN" and capacity_class != "UNKNOWN" and declared_class != capacity_class
        offers.append({"offer_ref": ref, "scope_matches": matches, "current": eligible,
                       "schema_version": record.schema_version,
                       "model_identity": identity,
                       "model_identity_observation": terms.model_identity.to_dict() if terms else None,
                       "billing_class": capacity_class if eligible and not class_conflict else "UNKNOWN",
                       "billing_class_observation": declared_class,
                       "billing_class_conflict": class_conflict,
                       "billing_class_authority": "provider_capacity owner store",
                       "promotions": [{**p.source.to_dict(), "promotion_id": p.promotion_id,
                                       "affects": list(p.affects), "description": p.description,
                                       "current": bool(eligible and p.source.is_current(current))}
                                      for p in terms.promotions] if terms else []})
        if terms is None:
            continue
        for observation in terms.observations:
            fresh = bool(eligible and observation.source.is_current(current))
            applies = observation.rule is None or observation.rule.service_tier == service_tier
            row = {"offer_ref": ref, **observation.to_dict(), "scope_matches": matches, "current": fresh,
                   "service_tier_matches": applies}
            observations.append(row)
            if matches:
                candidates.append((record, observation, fresh))
    dimensions = {}
    discrepancies = [{"dimension": "billing_class", "reason": "offer_disagrees_with_capacity_owner",
                      "offer_refs": [r["offer_ref"]]} for r in offers if r["billing_class_conflict"]]
    for dimension in DIMENSIONS:
        all_rows = [(r, o, fresh) for r, o, fresh in candidates if o.dimension == dimension]
        fresh_rows = [(r, o) for r, o, fresh in all_rows if fresh
                      and (o.rule is None or o.rule.service_tier == service_tier)]
        if not fresh_rows:
            dimensions[dimension] = {"status": "UNKNOWN" if all_rows else "ABSENT",
                                     "reason": "no_current_source" if all_rows else "no_observation"}
            continue
        best_rank = min(TARIFF_PRECEDENCE.get(o.source.source_class, 6) for _, o in fresh_rows)
        authoritative = [(r, o) for r, o in fresh_rows
                         if TARIFF_PRECEDENCE.get(o.source.source_class, 6) == best_rank]
        newest = max(timestamp(o.source.observed_at) for _, o in authoritative)
        selected = [(r, o) for r, o in authoritative if timestamp(o.source.observed_at) == newest]
        if len({_semantic(o) for _, o in fresh_rows}) > 1:
            discrepancies.append({"dimension": dimension, "reason": "retained_source_disagreement",
                                  "offer_refs": sorted({r.ref for r, _ in fresh_rows}),
                                  "source_hashes": sorted({o.source.content_sha256 for _, o in fresh_rows})})
        if len({_semantic(o) for _, o in selected}) != 1:
            dimensions[dimension] = {"status": "UNKNOWN", "reason": "source_conflict",
                                     "offer_refs": sorted({r.ref for r, _ in selected})}
            continue
        _, observation = selected[0]
        result = {"status": observation.status,
                  "observation_status": observation.status,
                  "offer_refs": sorted({r.ref for r, _ in selected}),
                  "source": observation.source.to_dict()}
        if observation.status != "OBSERVED":
            result["reason"] = observation.reason
        else:
            rule = observation.rule
            result.update({"rule_hash": rule.rule_hash, "rule": rule.to_dict(), "unit": rule.unit,
                           "basis": "ESTIMATED_FROM_RULE", "is_actual_charge": False,
                           "is_actual_capacity_change": False})
            quantity = rule.evaluate(usage, service_tier=service_tier, context_tokens=context_tokens)
            if quantity is None:
                result.update({"status": "UNKNOWN", "reason": "usage_or_service_tier_unknown"})
            else:
                result["quantity"] = _quantity(quantity)
        dimensions[dimension] = result
    return {"schema": "provider-offer-view-v1", "observed_at": current.isoformat(),
            "scope": dict(scope), "service_tier": service_tier, "context_tokens": context_tokens,
            "offers": offers, "observations": observations, "dimensions": dimensions,
            "discrepancies": discrepancies, "distinct_pool_n": len(pools),
            "capacity_binding": capacity_binding,
            "ranking_allowed": False, "capacity_reservation": False,
            "outcome_economics": {"status": "UNKNOWN", "owner": "execution_outcomes",
                                  "sample_n": None, "uncertainty": None,
                                  "reason": "no matched source-bound outcome cohort supplied"}}


def build_economic_handoff(receipt, *, execution_ref: str, executed_at: str,
                           scope: Mapping[str, str], usage: Mapping[str, int],
                           realized_charge: RealizedCharge | None = None,
                           service_tier: str = "default", context_tokens: int | None = None) -> dict[str, Any]:
    """Freeze descriptive economics for an outcome-owner-verified attempt join.

    This does not attest execution, usage, billing truth, acceptance or landing.
    It binds supplied claims so the outcome owner can verify them separately.
    """
    if not isinstance(execution_ref, str) or re.fullmatch(r"attempt:[0-9a-f]{64}", execution_ref) is None:
        raise ValueError("handoff requires an exact attempt reference")
    record = _receipt(receipt)
    if not isinstance(scope, Mapping) or set(scope) != SCOPE_FIELDS or any(
            getattr(record, key) != value for key, value in scope.items()):
        raise ValueError("handoff scope must match the exact historical offer")
    executed = timestamp(executed_at)
    if realized_charge is not None:
        if not isinstance(realized_charge, RealizedCharge) or realized_charge.execution_ref != execution_ref:
            raise ValueError("realized charge does not bind this exact attempt")
        if timestamp(realized_charge.source.observed_at) < executed:
            raise ValueError("realized charge cannot precede its execution")
    view = inspect_offers([record], scope=scope, now=executed, usage=usage,
                          service_tier=service_tier, context_tokens=context_tokens)
    result = {"schema": "offer-outcome-handoff-v1", "execution_ref": execution_ref,
              "offer_ref": record.ref, "executed_at": executed.isoformat(), "scope": dict(scope),
              "usage": dict(usage), "usage_status": "CALLER_SUPPLIED_REQUIRES_ATTEMPT_JOIN",
              "service_tier": service_tier, "context_tokens": context_tokens,
              "dimensions": view["dimensions"], "discrepancies": view["discrepancies"],
              "realized_charge": realized_charge.to_dict() if realized_charge else {"status": "UNKNOWN"},
              "acceptance": {"status": "UNKNOWN", "owner": "execution_outcomes"},
              "ranking_allowed": False, "outcome_economics": view["outcome_economics"]}
    estimate = result["dimensions"]["marginal_cash"]
    if realized_charge and estimate.get("status") == "OBSERVED" and estimate.get("unit") == realized_charge.currency:
        quantity = estimate["quantity"]
        actual = Fraction(realized_charge.amount)
        predicted = Fraction(int(quantity["numerator"]), int(quantity["denominator"]))
        if actual != predicted:
            result["discrepancies"].append({"dimension": "realized_charge", "reason": "reported_charge_differs_from_estimate"})
    result["handoff_hash"] = digest(result)
    return result


def validate_economic_handoff(payload: Mapping[str, Any], receipt) -> dict[str, Any]:
    """Recompute the descriptive projection, not the truth of supplied claims."""
    if not isinstance(payload, Mapping):
        raise ValueError("economic handoff must be a mapping")
    try:
        charge = payload["realized_charge"]
        if charge == {"status": "UNKNOWN"}:
            charge = None
        else:
            from src.offer_terms import EconomicSource
            if not isinstance(charge, Mapping) or set(charge) != set(RealizedCharge.__dataclass_fields__):
                raise ValueError("realized charge has missing or unknown fields")
            source = charge["source"]
            if not isinstance(source, Mapping) or set(source) != set(EconomicSource.__dataclass_fields__):
                raise ValueError("realized charge source has missing or unknown fields")
            charge = RealizedCharge(**{**charge, "source": EconomicSource(**source)})
        expected = build_economic_handoff(receipt, execution_ref=payload["execution_ref"],
            executed_at=payload["executed_at"], scope=payload["scope"], usage=payload["usage"],
            realized_charge=charge, service_tier=payload["service_tier"], context_tokens=payload["context_tokens"])
        if set(payload) != set(expected) or digest(payload) != digest(expected):
            raise ValueError("economic handoff differs from its bound offer/rules/input claims")
        return expected
    except (KeyError, TypeError) as exc:
        raise ValueError("malformed economic handoff") from exc


def main() -> None:
    """Explicit local receipt inspection; no directory discovery or mutation."""
    import argparse
    from pathlib import Path
    from src.provider_model_offer_store import get_receipt

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True)
    parser.add_argument("--offer-ref", action="append", required=True)
    parser.add_argument("--scope", required=True)
    parser.add_argument("--usage")
    parser.add_argument("--now")
    parser.add_argument("--service-tier", default="default")
    parser.add_argument("--context-tokens", type=int)
    parser.add_argument("--capacity-store")
    args = parser.parse_args()

    def load(path):
        with Path(path).open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("inspection input exceeds one MiB")
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate inspection input field")
                result[key] = value
            return result
        return json.loads(raw, object_pairs_hook=unique)

    try:
        receipts = []
        for ref in args.offer_ref:
            if re.fullmatch(r"offer:[0-9a-f]{64}", ref) is None:
                raise ValueError("offer reference must be offer:<lowercase SHA-256>")
            receipts.append(get_receipt(args.store, ref.split(":", 1)[1]))
        capacity_store = None
        if args.capacity_store:
            from src.provider_capacity_store import ProviderCapacityStore
            capacity_store = ProviderCapacityStore(args.capacity_store)
        result = inspect_offers(receipts, scope=load(args.scope), usage=load(args.usage) if args.usage else None,
                                now=timestamp(args.now) if args.now else None,
                                service_tier=args.service_tier, context_tokens=args.context_tokens,
                                capacity_store=capacity_store)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(2, f"offer inspection refused: {exc}\n")


if __name__ == "__main__":
    main()
