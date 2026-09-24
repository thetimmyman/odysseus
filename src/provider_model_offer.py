"""PS-679 immutable provider/model offer evidence (contract slice only).

An offer binds one native model on one exact provider pool and usage path to a
time-bounded tariff observation. It is descriptive evidence: this module does
not persist receipts, rank offers, decide legality, debit quotas, or dispatch.
PS-640 remains capacity authority and PS-605 remains the only dispatch
eligibility/selection authority. Outcomes belong to PS-642/PS-638 evidence.

Unknown prices and effects are explicit and never treated as zero or savings.
An offer is usable only when its scope matches the requested pool/capacity
receipt/path/model and every time fact is known, current, and within validity.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import MISSING, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import urlparse

OFFER_SCHEMA_VERSION = 1
OFFER_REF_PREFIX = "offer:"


class OfferReceiptError(ValueError):
    """Malformed or unverifiable provider/model offer evidence."""


@dataclass(frozen=True)
class UnknownValue:
    """A deliberate unknown, distinct from absent and the numeric value zero."""

    status: str = "unknown"

    def __post_init__(self) -> None:
        if self.status != "unknown":
            raise OfferReceiptError("UnknownValue status must be 'unknown'")

    def to_dict(self) -> dict[str, str]:
        return {"status": self.status}


UNKNOWN = UnknownValue()
OfferValue = int | float | str | UnknownValue


def _canonical(value: Any) -> Any:
    if isinstance(value, UnknownValue):
        return value.to_dict()
    if hasattr(value, "to_dict"):
        return _canonical(value.to_dict())
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical(v) for v in value]
    return value


def _digest(value: Any) -> str:
    raw = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise OfferReceiptError(f"{name} must be non-empty and trimmed")


def _timestamp(value: str, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise OfferReceiptError(f"{name} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OfferReceiptError(f"{name} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise OfferReceiptError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _value(value: Any, name: str, *, allow_text: bool = False) -> None:
    if isinstance(value, UnknownValue):
        return
    if isinstance(value, bool):
        raise OfferReceiptError(f"{name} must be a non-negative number or UNKNOWN")
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or value < 0:
            raise OfferReceiptError(f"{name} must be finite and non-negative")
        return
    if allow_text and isinstance(value, str) and value.strip():
        return
    raise OfferReceiptError(f"{name} must be a non-negative number or UNKNOWN")


@dataclass(frozen=True)
class OfferProvenance:
    source_url: str
    content_sha256: str
    evidence_class: str
    observed_at: str
    ttl_seconds: int

    def __post_init__(self) -> None:
        _text(self.source_url, "source_url")
        try:
            parsed_url = urlparse(self.source_url)
            valid_url = (parsed_url.scheme in {"http", "https"} and parsed_url.hostname
                         and not parsed_url.username and not parsed_url.password)
        except ValueError:
            valid_url = False
        if not valid_url:
            raise OfferReceiptError("source_url must be an absolute http(s) URL without userinfo")
        if len(self.content_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.content_sha256):
            raise OfferReceiptError("content_sha256 must be a lowercase SHA-256 hex digest")
        _text(self.evidence_class, "evidence_class")
        _timestamp(self.observed_at, "observed_at")
        if not isinstance(self.ttl_seconds, int) or isinstance(self.ttl_seconds, bool) or self.ttl_seconds <= 0:
            raise OfferReceiptError("ttl_seconds must be a positive integer")

    def to_dict(self) -> dict[str, Any]:
        return {"source_url": self.source_url, "content_sha256": self.content_sha256,
                "evidence_class": self.evidence_class, "observed_at": self.observed_at,
                "ttl_seconds": self.ttl_seconds}


@dataclass(frozen=True)
class PriceEffect:
    """Per-unit tariff terms; a zero offered rate is explicit and source-backed."""

    currency: str
    unit: str
    list_rate: OfferValue = UNKNOWN
    offered_rate: OfferValue = UNKNOWN

    def __post_init__(self) -> None:
        _text(self.currency, "price.currency")
        _text(self.unit, "price.unit")
        _value(self.list_rate, "price.list_rate")
        _value(self.offered_rate, "price.offered_rate")

    def to_dict(self) -> dict[str, Any]:
        return {"currency": self.currency, "unit": self.unit,
                "list_rate": _canonical(self.list_rate),
                "offered_rate": _canonical(self.offered_rate)}


@dataclass(frozen=True)
class CreditEffect:
    """Provider credit/debit terms, kept separate from tariff price."""

    unit: str
    amount: OfferValue = UNKNOWN
    applies_to: str = ""

    def __post_init__(self) -> None:
        _text(self.unit, "credit.unit")
        _value(self.amount, "credit.amount")
        if self.applies_to:
            _text(self.applies_to, "credit.applies_to")

    def to_dict(self) -> dict[str, Any]:
        return {"unit": self.unit, "amount": _canonical(self.amount),
                "applies_to": self.applies_to}


@dataclass(frozen=True)
class AllowanceEffect:
    """Allowance/quota effect, represented in provider-native units."""

    unit: str
    amount: OfferValue = UNKNOWN
    window: str = ""

    def __post_init__(self) -> None:
        _text(self.unit, "allowance.unit")
        _value(self.amount, "allowance.amount")
        if self.window:
            _text(self.window, "allowance.window")

    def to_dict(self) -> dict[str, Any]:
        return {"unit": self.unit, "amount": _canonical(self.amount),
                "window": self.window}


@dataclass(frozen=True)
class ProviderModelOfferReceipt:
    provider: str
    pool_id: str
    capacity_receipt_ref: str
    harness: str
    usage_path: str
    native_model: str
    tariff_id: str
    tariff_version: str
    provenance: OfferProvenance
    valid_from: str | UnknownValue = UNKNOWN
    valid_until: str | UnknownValue = UNKNOWN
    price: PriceEffect = field(default_factory=lambda: PriceEffect("USD", "unknown"))
    credit: CreditEffect = field(default_factory=lambda: CreditEffect("credit"))
    allowance: AllowanceEffect = field(default_factory=lambda: AllowanceEffect("request"))
    schema_version: int = OFFER_SCHEMA_VERSION
    receipt_hash: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.schema_version, int) or isinstance(self.schema_version, bool):
            raise OfferReceiptError("schema_version must be an integer")
        if self.schema_version != OFFER_SCHEMA_VERSION:
            raise OfferReceiptError("unsupported offer receipt schema")
        for name in ("provider", "pool_id", "capacity_receipt_ref", "harness",
                     "usage_path", "native_model", "tariff_id", "tariff_version"):
            _text(getattr(self, name), name)
        prefix, _, capacity_hash = self.capacity_receipt_ref.partition(":")
        if (prefix != "capacity:"[:-1] or len(capacity_hash) != 64
                or any(c not in "0123456789abcdef" for c in capacity_hash)):
            raise OfferReceiptError("capacity_receipt_ref must be capacity:<lowercase SHA-256>")
        if not isinstance(self.provenance, OfferProvenance):
            raise OfferReceiptError("provenance is required")
        if not isinstance(self.price, PriceEffect) or not isinstance(self.credit, CreditEffect) or not isinstance(self.allowance, AllowanceEffect):
            raise OfferReceiptError("price, credit and allowance effects must be typed immutable records")
        for name in ("valid_from", "valid_until"):
            value = getattr(self, name)
            if isinstance(value, UnknownValue):
                continue
            _timestamp(value, name)
        if not isinstance(self.valid_from, UnknownValue) and not isinstance(self.valid_until, UnknownValue):
            if _timestamp(self.valid_until, "valid_until") <= _timestamp(self.valid_from, "valid_from"):
                raise OfferReceiptError("valid_until must be later than valid_from")
        if not self.receipt_hash or self.receipt_hash != _digest(self.core()):
            raise OfferReceiptError("receipt_hash does not cover offer receipt content")

    def core(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "provider": self.provider,
                "pool_id": self.pool_id, "capacity_receipt_ref": self.capacity_receipt_ref,
                "harness": self.harness, "usage_path": self.usage_path,
                "native_model": self.native_model, "tariff_id": self.tariff_id,
                "tariff_version": self.tariff_version,
                "provenance": self.provenance.to_dict(),
                "valid_from": _canonical(self.valid_from),
                "valid_until": _canonical(self.valid_until),
                "price": self.price.to_dict(), "credit": self.credit.to_dict(),
                "allowance": self.allowance.to_dict()}

    def to_dict(self) -> dict[str, Any]:
        return {**self.core(), "receipt_hash": self.receipt_hash}

    @property
    def ref(self) -> str:
        return f"{OFFER_REF_PREFIX}{self.receipt_hash}"

    def is_eligible(self, *, now: Optional[datetime] = None,
                    provider: str, pool_id: str, capacity_receipt_ref: str, harness: str,
                    usage_path: str, native_model: str) -> bool:
        """Freshness and exact-scope predicate only; caller still asks PS-605."""
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise OfferReceiptError("now must include a timezone")
        current = current.astimezone(timezone.utc)
        observed = _timestamp(self.provenance.observed_at, "observed_at")
        if observed > current or (current - observed).total_seconds() >= self.provenance.ttl_seconds:
            return False
        if isinstance(self.valid_from, UnknownValue) or isinstance(self.valid_until, UnknownValue):
            return False
        if not (_timestamp(self.valid_from, "valid_from") <= current < _timestamp(self.valid_until, "valid_until")):
            return False
        return (self.provider == provider and self.pool_id == pool_id
                and self.capacity_receipt_ref == capacity_receipt_ref
                and self.harness == harness and self.usage_path == usage_path
                and self.native_model == native_model)


def make_provider_model_offer_receipt(**kwargs: Any) -> ProviderModelOfferReceipt:
    known = {f.name for f in fields(ProviderModelOfferReceipt)}
    unknown = set(kwargs) - known
    if unknown:
        raise OfferReceiptError(f"unknown offer receipt fields: {sorted(unknown)}")
    kwargs.setdefault("schema_version", OFFER_SCHEMA_VERSION)
    for name, kind in (("provenance", OfferProvenance), ("price", PriceEffect),
                       ("credit", CreditEffect), ("allowance", AllowanceEffect)):
        if name in kwargs and not isinstance(kwargs[name], kind):
            raise OfferReceiptError(f"{name} must be a typed {kind.__name__}")
    # Build the prospective core without admitting an unsealed public record.
    fields_only = dict(kwargs)
    fields_only.pop("receipt_hash", None)
    core_obj = object.__new__(ProviderModelOfferReceipt)
    for item in fields(ProviderModelOfferReceipt):
        if item.name in fields_only:
            value = fields_only[item.name]
        elif item.default_factory is not MISSING:
            value = item.default_factory()
        elif item.default is not MISSING:
            value = item.default
        else:
            raise OfferReceiptError(f"missing required offer receipt field: {item.name}")
        object.__setattr__(core_obj, item.name, value)
    digest = _digest(core_obj.core())
    return ProviderModelOfferReceipt(**{**fields_only, "receipt_hash": digest})


def provider_model_offer_hash_is_valid(payload: Mapping[str, Any]) -> bool:
    if not isinstance(payload, Mapping) or not payload.get("receipt_hash"):
        return False
    core = {k: v for k, v in payload.items() if k != "receipt_hash"}
    try:
        return _digest(core) == payload["receipt_hash"]
    except (TypeError, ValueError):
        return False


def provider_model_offer_from_dict(payload: Mapping[str, Any]) -> ProviderModelOfferReceipt:
    """Parse and validate a serialized receipt, including its content hash."""
    if not provider_model_offer_hash_is_valid(payload):
        raise OfferReceiptError("offer receipt hash is missing or does not match content")

    def value(raw: Any) -> Any:
        if isinstance(raw, Mapping) and raw == {"status": "unknown"}:
            return UNKNOWN
        return raw

    try:
        p = dict(payload)
        prov = OfferProvenance(**p["provenance"])
        price = dict(p["price"])
        credit = dict(p["credit"])
        allowance = dict(p["allowance"])
        return ProviderModelOfferReceipt(
            schema_version=p["schema_version"], provider=p["provider"], pool_id=p["pool_id"],
            capacity_receipt_ref=p["capacity_receipt_ref"], harness=p["harness"],
            usage_path=p["usage_path"], native_model=p["native_model"],
            tariff_id=p["tariff_id"], tariff_version=p["tariff_version"], provenance=prov,
            valid_from=value(p["valid_from"]), valid_until=value(p["valid_until"]),
            price=PriceEffect(price["currency"], price["unit"], value(price["list_rate"]),
                              value(price["offered_rate"])),
            credit=CreditEffect(credit["unit"], value(credit["amount"]), credit["applies_to"]),
            allowance=AllowanceEffect(allowance["unit"], value(allowance["amount"]), allowance["window"]),
            receipt_hash=p["receipt_hash"])
    except (KeyError, TypeError) as exc:
        raise OfferReceiptError(f"malformed serialized offer receipt: {exc}") from exc
