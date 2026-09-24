"""PS-640: provider capacity and entitlement facts.

This module is deliberately a facts layer.  It does not select a model,
route, retry, or apply privacy policy.  A receipt describes one independently
consumable pool and is only eligible while the relevant observations are fresh.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Tuple, Union
import re

CAPACITY_SCHEMA_VERSION = 1
CAPACITY_RECEIPT_REF_PREFIX = "capacity:"


class CapacityError(ValueError):
    """Invalid or unverifiable capacity evidence."""


class Entitlement(str, Enum):
    INTERACTIVE_NATIVE = "interactive_native"
    AGENT_SDK = "agent_sdk"
    THIRD_PARTY_HARNESS = "third_party_harness"
    API = "api"
    LOCAL = "local"
    UNKNOWN = "unknown"


class AuthorizationClass(str, Enum):
    """The authenticated interface class observed for a pool."""

    LOCAL_ENDPOINT = "local_endpoint"
    OAUTH_CLI = "oauth_cli"
    API_KEY = "api_key"
    AGENT_SDK = "agent_sdk"
    UNKNOWN = "unknown"


class CapacityState(str, Enum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    COOLDOWN = "cooldown"
    RATE_LIMITED = "rate_limited"
    EXHAUSTED = "exhausted"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class CostClass(str, Enum):
    SUBSCRIPTION_SUNK_COST = "subscription_sunk_cost"
    METERED = "metered"
    LOCAL_ZERO_MARGINAL_DOLLARS = "local_zero_marginal_dollars"


class PricingSource(str, Enum):
    PROVIDER_API = "provider_api"
    PRICING_PAGE_SNAPSHOT = "provider_pricing_page_snapshot"
    SUBSCRIPTION_CONTRACT = "subscription_contract_config"
    OPERATOR_OVERRIDE = "operator_override"
    GENERATED_RATE_TABLE = "generated_rate_table"
    PROVIDER_BILLING_ENDPOINT = "provider_billing_endpoint"
    PROVIDER_INVOICE = "provider_invoice"
    PROVIDER_USAGE_LEDGER = "provider_usage_ledger"
    EXTERNAL_BILLING_EVIDENCE = "external_billing_evidence"


class EvidenceStatus(str, Enum):
    KNOWN = "known"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class UnknownValue:
    """An explicit unknown, distinct from absent and from zero."""

    status: str = EvidenceStatus.UNKNOWN.value

    def __post_init__(self) -> None:
        if self.status != EvidenceStatus.UNKNOWN.value:
            raise CapacityError("UnknownValue status must be 'unknown'")

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status}


UNKNOWN = UnknownValue()
EvidenceValue = Union[int, float, str, bool, UnknownValue]


def _is_unknown(value: Any) -> bool:
    return isinstance(value, UnknownValue)


def _require_enum(value: Any, enum_type: type[Enum], field_name: str) -> None:
    if not isinstance(value, enum_type):
        raise CapacityError(f"{field_name} must be an explicit {enum_type.__name__}")


def _require_number(value: Any, field_name: str, *, integral: bool = False) -> None:
    if _is_unknown(value):
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CapacityError(f"{field_name} must be a finite number or UNKNOWN")
    if integral and isinstance(value, float) and not value.is_integer():
        raise CapacityError(f"{field_name} must be an integer count")


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UnknownValue):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _encode(v) for k, v in value.items()}
    if hasattr(value, "to_dict"):
        return _encode(value.to_dict())
    return value


def _canonical(payload: object) -> bytes:
    return json.dumps(_encode(payload), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _sha256(payload: object) -> str:
    return hashlib.sha256(_canonical(payload)).hexdigest()


def _parse_time(value: str, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise CapacityError(f"{field_name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapacityError(f"{field_name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise CapacityError(f"{field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]*$")


def canonical_identity(value: str, field_name: str) -> str:
    """Return a stable identity or reject an alias-bearing representation."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise CapacityError(f"{field_name} must be non-empty and trimmed")
    if not _IDENTITY_RE.fullmatch(value):
        raise CapacityError(f"{field_name} contains non-canonical characters")
    return value


@dataclass(frozen=True)
class EvidenceProvenance:
    source: str
    reference: str
    collector_id: str
    observed_at: str
    ttl_seconds: int

    def __post_init__(self) -> None:
        if not self.source or not self.reference or not self.collector_id:
            raise CapacityError("evidence provenance requires source, reference, collector")
        observed = _parse_time(self.observed_at, "observed_at")
        if not isinstance(self.ttl_seconds, int) or isinstance(self.ttl_seconds, bool) or self.ttl_seconds <= 0:
            raise CapacityError("ttl_seconds must be a positive integer")
        if observed.timestamp() < 0:
            raise CapacityError("observed_at cannot be before the Unix epoch")

    @property
    def expires_at(self) -> str:
        observed = _parse_time(self.observed_at, "observed_at")
        return datetime.fromtimestamp(observed.timestamp() + self.ttl_seconds, timezone.utc).isoformat()

    def is_fresh(self, now: Optional[datetime] = None) -> bool:
        current = (now or _now()).astimezone(timezone.utc)
        return current < _parse_time(self.expires_at, "expires_at")

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "reference": self.reference,
                "collector_id": self.collector_id, "observed_at": self.observed_at,
                "ttl_seconds": self.ttl_seconds, "expires_at": self.expires_at}


@dataclass(frozen=True)
class QuotaDimension:
    """One optional quota dimension; unknown values remain explicitly unknown."""

    name: str
    unit: str
    limit: EvidenceValue = UNKNOWN
    remaining: EvidenceValue = UNKNOWN
    reset_at: Union[str, UnknownValue] = UNKNOWN
    provenance: Optional[EvidenceProvenance] = None

    def __post_init__(self) -> None:
        if not self.name or not self.unit:
            raise CapacityError("quota name and unit are required")
        for field_name in ("limit", "remaining"):
            value = getattr(self, field_name)
            _require_number(value, f"quota {self.name}.{field_name}",
                            integral=self.unit.lower() in {"requests", "tokens", "concurrency"})
            if not _is_unknown(value) and value < 0:
                raise CapacityError(f"quota {self.name} has negative {field_name}")
        if (not _is_unknown(self.limit) and not _is_unknown(self.remaining) and
                self.remaining > self.limit):
            raise CapacityError(f"quota {self.name}.remaining exceeds limit")
        if self.provenance is None:
            raise CapacityError(f"quota {self.name} requires provenance")
        if not _is_unknown(self.reset_at):
            reset = _parse_time(str(self.reset_at), f"quota {self.name}.reset_at")
            if reset < _parse_time(self.provenance.observed_at, "observed_at"):
                raise CapacityError(f"quota {self.name}.reset_at predates observation")

    def is_fresh(self, now: Optional[datetime] = None) -> bool:
        return self.provenance is not None and self.provenance.is_fresh(now)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "unit": self.unit, "limit": _encode(self.limit),
                "remaining": self.remaining.to_dict() if _is_unknown(self.remaining) else self.remaining,
                "reset_at": self.reset_at.to_dict() if _is_unknown(self.reset_at) else self.reset_at,
                "provenance": _encode(self.provenance)}


@dataclass(frozen=True)
class PriceObservation:
    cost_class: CostClass
    published_list_rate: EvidenceValue = UNKNOWN
    estimated_marginal_cost: EvidenceValue = UNKNOWN
    actual_billed_cost: EvidenceValue = UNKNOWN
    unit: str = "USD"
    provenance: Optional[EvidenceProvenance] = None
    pricing_source: Optional[PricingSource] = None
    pricing_version: str = ""

    def __post_init__(self) -> None:
        _require_enum(self.cost_class, CostClass, "cost_class")
        if self.pricing_source is not None:
            _require_enum(self.pricing_source, PricingSource, "pricing_source")
        for name in ("published_list_rate", "estimated_marginal_cost", "actual_billed_cost"):
            value = getattr(self, name)
            _require_number(value, name)
            if not _is_unknown(value) and value < 0:
                raise CapacityError(f"{name} cannot be negative")
        known_values = any(not _is_unknown(getattr(self, name)) for name in
                           ("published_list_rate", "estimated_marginal_cost", "actual_billed_cost"))
        if known_values and (self.pricing_source is None or not self.pricing_version or self.provenance is None):
            raise CapacityError("known pricing requires source, version and provenance")
        billing_sources = {PricingSource.PROVIDER_BILLING_ENDPOINT, PricingSource.PROVIDER_INVOICE,
                           PricingSource.PROVIDER_USAGE_LEDGER, PricingSource.EXTERNAL_BILLING_EVIDENCE}
        if not _is_unknown(self.actual_billed_cost) and self.pricing_source not in billing_sources:
            raise CapacityError("actual billed cost requires billing-grade evidence")

    def is_fresh(self, now: Optional[datetime] = None) -> bool:
        return self.provenance is not None and self.provenance.is_fresh(now)

    def to_dict(self) -> dict[str, Any]:
        return {"cost_class": self.cost_class.value, "published_list_rate": _encode(self.published_list_rate),
                "estimated_marginal_cost": _encode(self.estimated_marginal_cost),
                "actual_billed_cost": _encode(self.actual_billed_cost), "unit": self.unit,
                "provenance": _encode(self.provenance), "pricing_source": self.pricing_source.value if self.pricing_source else None,
                "pricing_version": self.pricing_version}


@dataclass(frozen=True)
class ProviderCapacityReceipt:
    """Immutable facts for exactly one consumable provider pool."""

    provider: str
    pool_id: str
    account_identity: str
    authorization_class: AuthorizationClass
    entitlement: Entitlement
    exposed_models: Tuple[str, ...]
    observed_at: str
    ttl_seconds: int
    collector_id: str
    evidence_source: str
    evidence_reference: str
    state: CapacityState
    state_provenance: EvidenceProvenance
    entitlement_provenance: EvidenceProvenance
    zdr_provenance: EvidenceProvenance
    quotas: Tuple[QuotaDimension, ...] = ()
    price: Optional[PriceObservation] = None
    concurrency_limit: EvidenceValue = UNKNOWN
    concurrency_remaining: EvidenceValue = UNKNOWN
    rate_limit: Optional[QuotaDimension] = None
    zdr_supported: EvidenceValue = UNKNOWN
    zdr_required_by_pool: EvidenceValue = UNKNOWN
    supersedes: str = ""
    invalidation_reason: str = ""
    schema_version: int = CAPACITY_SCHEMA_VERSION
    receipt_hash: str = ""
    credential_sha256: str = ""
    endpoint_url: str = ""

    def __post_init__(self) -> None:
        if self.schema_version != CAPACITY_SCHEMA_VERSION:
            raise CapacityError("unsupported capacity receipt schema")
        if self.credential_sha256 and (len(self.credential_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.credential_sha256)):
            raise CapacityError("credential_sha256 must be a lowercase SHA-256 digest")
        object.__setattr__(self, "exposed_models", tuple(self.exposed_models))
        object.__setattr__(self, "quotas", tuple(self.quotas))
        for name in ("provider", "pool_id", "account_identity", "authorization_class"):
            value = getattr(self, name)
            if name == "authorization_class":
                _require_enum(value, AuthorizationClass, name)
            else:
                canonical_identity(value, name)
        _require_enum(self.entitlement, Entitlement, "entitlement")
        _require_enum(self.state, CapacityState, "state")
        for name in ("collector_id", "evidence_source", "evidence_reference"):
            if not str(getattr(self, name) or "").strip():
                raise CapacityError(f"{name} must be non-empty")
        if not self.exposed_models or any(not model for model in self.exposed_models):
            raise CapacityError("exposed_models must contain at least one model")
        provenance = EvidenceProvenance(self.evidence_source, self.evidence_reference,
                                        self.collector_id, self.observed_at, self.ttl_seconds)
        for name in ("state_provenance", "entitlement_provenance", "zdr_provenance"):
            if not isinstance(getattr(self, name), EvidenceProvenance):
                raise CapacityError(f"{name} is required for scoped freshness")
        if self.state == CapacityState.COOLDOWN and not any(q.reset_at != UNKNOWN for q in self.quotas):
            raise CapacityError("cooldown requires a known quota reset")
        for value_name in ("concurrency_limit", "concurrency_remaining"):
            value = getattr(self, value_name)
            _require_number(value, value_name, integral=True)
            if not _is_unknown(value) and value < 0:
                raise CapacityError(f"{value_name} cannot be negative")
        if (not _is_unknown(self.concurrency_limit) and not _is_unknown(self.concurrency_remaining)
                and self.concurrency_remaining > self.concurrency_limit):
            raise CapacityError("concurrency_remaining cannot exceed concurrency_limit")
        for name in ("zdr_supported", "zdr_required_by_pool"):
            value = getattr(self, name)
            if not _is_unknown(value) and not isinstance(value, bool):
                raise CapacityError(f"{name} must be boolean or UNKNOWN")
        if not self.receipt_hash:
            raise CapacityError("receipt must be sealed with receipt_hash")
        if self.receipt_hash != _sha256(self.core()):
            raise CapacityError("receipt_hash does not cover receipt content")

    def core(self) -> dict[str, Any]:
        payload = {"schema_version": self.schema_version, "provider": self.provider,
                "pool_id": self.pool_id, "account_identity": self.account_identity,
                "authorization_class": self.authorization_class,
                "entitlement": self.entitlement, "exposed_models": list(self.exposed_models),
                "observed_at": self.observed_at, "ttl_seconds": self.ttl_seconds,
                "collector_id": self.collector_id, "evidence_source": self.evidence_source,
                "evidence_reference": self.evidence_reference, "state": self.state,
                "state_provenance": self.state_provenance,
                "entitlement_provenance": self.entitlement_provenance,
                "zdr_provenance": self.zdr_provenance,
                "quotas": list(self.quotas), "price": self.price,
                "concurrency_limit": self.concurrency_limit,
                "concurrency_remaining": self.concurrency_remaining,
                "rate_limit": self.rate_limit, "zdr_supported": self.zdr_supported,
                "zdr_required_by_pool": self.zdr_required_by_pool,
                "supersedes": self.supersedes,
                "invalidation_reason": self.invalidation_reason}
        if self.credential_sha256:
            payload["credential_sha256"] = self.credential_sha256
        if self.endpoint_url:
            payload["endpoint_url"] = self.endpoint_url
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {**_encode(self.core()), "receipt_hash": self.receipt_hash}

    @property
    def ref(self) -> str:
        return f"{CAPACITY_RECEIPT_REF_PREFIX}{self.receipt_hash}"

    def is_fresh(self, now: Optional[datetime] = None) -> bool:
        return EvidenceProvenance(self.evidence_source, self.evidence_reference,
                                  self.collector_id, self.observed_at, self.ttl_seconds).is_fresh(now)

    def facts_are_fresh(self, now: Optional[datetime] = None) -> bool:
        if not self.is_fresh(now):
            return False
        if not self.state_provenance.is_fresh(now) or not self.entitlement_provenance.is_fresh(now):
            return False
        if not self.zdr_provenance.is_fresh(now):
            return False
        if any(not quota.is_fresh(now) for quota in self.quotas):
            return False
        if self.rate_limit is not None and not self.rate_limit.is_fresh(now):
            return False
        if self.price is not None and any(not _is_unknown(getattr(self.price, name)) for name in
                                          ("published_list_rate", "estimated_marginal_cost", "actual_billed_cost")):
            if not self.price.is_fresh(now):
                return False
        return True

    def is_operationally_available(self, *, now: Optional[datetime] = None) -> bool:
        """Facts-only availability; policy and provider preference remain external."""
        if not self.facts_are_fresh(now):
            return False
        if self.state in (CapacityState.COOLDOWN, CapacityState.RATE_LIMITED,
                          CapacityState.EXHAUSTED, CapacityState.UNAVAILABLE,
                          CapacityState.UNKNOWN):
            return False
        if self.concurrency_remaining == 0:
            return False
        return True

    def has_usable_capacity_facts(self, *, now: Optional[datetime] = None) -> bool:
        """Facts-only usability; PS-605 still decides policy permission."""
        return (self.is_operationally_available(now=now) and
                self.entitlement not in (Entitlement.UNKNOWN, Entitlement.INTERACTIVE_NATIVE))


def make_capacity_receipt(**kwargs: Any) -> ProviderCapacityReceipt:
    known = {f.name for f in fields(ProviderCapacityReceipt)}
    unknown = set(kwargs) - known
    if unknown:
        raise CapacityError(f"unknown receipt fields: {sorted(unknown)}")
    payload = dict(kwargs)
    payload.setdefault("schema_version", CAPACITY_SCHEMA_VERSION)
    payload.setdefault("quotas", ())
    payload.setdefault("exposed_models", ())
    payload.setdefault("price", None)
    payload.setdefault("rate_limit", None)
    payload.setdefault("concurrency_limit", UNKNOWN)
    payload.setdefault("concurrency_remaining", UNKNOWN)
    payload.setdefault("zdr_supported", UNKNOWN)
    payload.setdefault("zdr_required_by_pool", UNKNOWN)
    payload.setdefault("supersedes", "")
    payload.setdefault("invalidation_reason", "")
    base_prov = EvidenceProvenance(payload["evidence_source"], payload["evidence_reference"],
                                   payload["collector_id"], payload["observed_at"], payload["ttl_seconds"])
    payload.setdefault("state_provenance", base_prov)
    payload.setdefault("entitlement_provenance", base_prov)
    payload.setdefault("zdr_provenance", base_prov)
    payload.pop("receipt_hash", None)
    if not payload.get("endpoint_url"):
        payload.pop("endpoint_url", None)
    if not payload.get("credential_sha256"):
        payload.pop("credential_sha256", None)
    return ProviderCapacityReceipt(**payload, receipt_hash=_sha256(payload))


def capacity_receipt_hash_is_valid(payload: Mapping[str, Any]) -> bool:
    supplied = str(payload.get("receipt_hash") or "")
    if not supplied:
        return False
    core = dict(payload)
    core.pop("receipt_hash", None)
    return supplied == _sha256(core)


def _unknown_or_value(value: Any) -> Any:
    if isinstance(value, Mapping) and value.get("status") == "unknown":
        return UNKNOWN
    return value


def capacity_receipt_from_dict(payload: Mapping[str, Any]) -> ProviderCapacityReceipt:
    data = dict(payload)
    if not capacity_receipt_hash_is_valid(data):
        raise CapacityError("capacity receipt hash mismatch")
    data["entitlement"] = Entitlement(data["entitlement"])
    data["state"] = CapacityState(data["state"])
    data["authorization_class"] = AuthorizationClass(data["authorization_class"])
    data["exposed_models"] = tuple(data["exposed_models"])
    for name in ("concurrency_limit", "concurrency_remaining", "zdr_supported", "zdr_required_by_pool"):
        data[name] = _unknown_or_value(data.get(name, {"status": "unknown"}))
    data["quotas"] = tuple(_quota_from_dict(q) for q in data.get("quotas", ()))
    data["rate_limit"] = _quota_from_dict(data["rate_limit"]) if data.get("rate_limit") else None
    for name in ("state_provenance", "entitlement_provenance", "zdr_provenance"):
        data[name] = _provenance_from_dict(data.get(name))
    if data.get("price"):
        data["price"] = _price_from_dict(data["price"])
    return ProviderCapacityReceipt(**data)


def validated_current_receipts(receipts: Iterable[ProviderCapacityReceipt]) -> Tuple[ProviderCapacityReceipt, ...]:
    """Validate a complete in-memory history before exposing current facts."""
    entries = tuple(receipts)
    by_hash = {receipt.receipt_hash: receipt for receipt in entries}
    if len(by_hash) != len(entries):
        raise CapacityError("duplicate receipt hash in history")
    children: dict[str, list[ProviderCapacityReceipt]] = {}
    for receipt in entries:
        if not receipt.supersedes:
            continue
        target = by_hash.get(receipt.supersedes)
        if target is None:
            raise CapacityError("supersession target is missing")
        if target.pool_id != receipt.pool_id:
            raise CapacityError("cross-pool supersession in history")
        if target.receipt_hash == receipt.receipt_hash:
            raise CapacityError("self-supersession in history")
        if target.invalidation_reason and not receipt.invalidation_reason:
            raise CapacityError("invalidated receipt cannot be superseded")
        if _parse_time(target.observed_at, "supersession target observed_at") > _parse_time(receipt.observed_at, "receipt observed_at"):
            raise CapacityError("supersession target must not be newer than receipt")
        children.setdefault(target.receipt_hash, []).append(receipt)
    if any(len(records) > 1 for records in children.values()):
        raise CapacityError("a receipt has multiple supersession successors")
    for receipt in entries:
        seen: set[str] = set()
        cursor = receipt
        while cursor.supersedes:
            if cursor.receipt_hash in seen:
                raise CapacityError("cycle in supersession history")
            seen.add(cursor.receipt_hash)
            cursor = by_hash[cursor.supersedes]
    superseded = set(children)
    current = tuple(receipt for receipt in entries
                    if receipt.receipt_hash not in superseded and not receipt.invalidation_reason)
    pools: dict[str, ProviderCapacityReceipt] = {}
    for receipt in current:
        if receipt.pool_id in pools:
            raise CapacityError(f"conflicting current receipts for {receipt.pool_id}")
        pools[receipt.pool_id] = receipt
    return tuple(pools.values())


def _provenance_from_dict(data: Optional[Mapping[str, Any]]) -> Optional[EvidenceProvenance]:
    if not data:
        return None
    return EvidenceProvenance(data["source"], data["reference"], data["collector_id"],
                              data["observed_at"], data["ttl_seconds"])


def _quota_from_dict(data: Mapping[str, Any]) -> QuotaDimension:
    return QuotaDimension(data["name"], data["unit"], _unknown_or_value(data.get("limit", {"status": "unknown"})),
                          _unknown_or_value(data.get("remaining", {"status": "unknown"})),
                          _unknown_or_value(data.get("reset_at", {"status": "unknown"})),
                          _provenance_from_dict(data.get("provenance")))


def _price_from_dict(data: Mapping[str, Any]) -> PriceObservation:
    return PriceObservation(CostClass(data["cost_class"]), _unknown_or_value(data.get("published_list_rate", {"status": "unknown"})),
                            _unknown_or_value(data.get("estimated_marginal_cost", {"status": "unknown"})),
                            _unknown_or_value(data.get("actual_billed_cost", {"status": "unknown"})), data.get("unit", "USD"),
                            _provenance_from_dict(data.get("provenance")),
                            PricingSource(data["pricing_source"]) if data.get("pricing_source") else None,
                            data.get("pricing_version", ""))


class CapacityRegistry:
    """Read-only capacity facts interface; it never chooses a winner."""

    def __init__(self, receipts: Iterable[ProviderCapacityReceipt] = ()) -> None:
        self._receipts = validated_current_receipts(receipts)

    def capacity_snapshot(self, pool_id: str) -> Optional[ProviderCapacityReceipt]:
        candidates = [r for r in self._receipts if r.pool_id == pool_id]
        return max(candidates, key=lambda r: (r.observed_at, r.receipt_hash)) if candidates else None

    def eligible_capacity_for(self, target: str, *, now: Optional[datetime] = None
                              ) -> Tuple[ProviderCapacityReceipt, ...]:
        """Return all structurally available pools; PS-605 applies policy facts."""
        return tuple(sorted((r for r in self._receipts if target in r.exposed_models and
                             r.has_usable_capacity_facts(now=now)),
                            key=lambda r: (r.provider, r.pool_id, r.receipt_hash)))
