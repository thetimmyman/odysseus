"""Immutable tariff data and bounded arithmetic, without routing or collection."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import re
from typing import Any, Mapping
from urllib.parse import urlparse

DIMENSIONS = ("published_tariff", "effective_tariff", "quota_burn", "marginal_cash", "allowance_bonus")
USAGE_DIMENSIONS = frozenset({"input_tokens", "output_tokens", "cache_read_tokens",
                              "cache_write_tokens", "requests", "tool_calls"})
TARIFF_PRECEDENCE = {"SUPPORTED_PROVIDER_API": 0, "PROVIDER_API_DISCOVERED": 0,
                     "PROVIDER_WEB_SESSION_API": 1, "OFFICIAL_PUBLISHED_CATALOG": 2,
                     "THIRD_PARTY_REGISTRY": 3, "OPERATOR_DECLARED": 4, "INFERRED": 5}
SOURCE_CLASSES = frozenset(TARIFF_PRECEDENCE) | {
    "LOCAL_CLIENT_EVIDENCE", "PROVIDER_BILLING_RECORD", "PROVIDER_RESPONSE", "UNAVAILABLE"}


def text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 2000:
        raise ValueError(f"{name} must be nonempty, trimmed and bounded")


def timestamp(value: Any) -> datetime:
    text(value, "timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    try:
        return result.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError("timestamp cannot be normalized to UTC") from exc


def decimal_amount(value: Any) -> Fraction:
    # Strings avoid rounding source prices through a binary float. The bound
    # also prevents hostile exponents or arbitrarily expensive arithmetic.
    if (not isinstance(value, str) or len(value) > 100
            or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,30})?", value) is None):
        raise ValueError("amount must be a bounded nonnegative decimal string")
    return Fraction(Decimal(value))


def count(value: Any, name: str) -> None:
    if type(value) is not int or not 0 <= value <= 10**18:
        raise ValueError(f"{name} must be a bounded nonnegative integer")


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _tuple(value: Any, kind: type, name: str) -> None:
    if not isinstance(value, tuple) or len(value) > 100 or any(not isinstance(v, kind) for v in value):
        raise ValueError(f"{name} must be a bounded immutable tuple of {kind.__name__}")


@dataclass(frozen=True)
class EconomicSource:
    source_class: str
    source_url: str
    source_version: str
    content_sha256: str
    observed_at: str
    ttl_seconds: int
    confidence: str = "UNASSESSED"

    def __post_init__(self) -> None:
        if not isinstance(self.source_class, str) or self.source_class not in SOURCE_CLASSES:
            raise ValueError("unrecognized economic source class")
        text(self.source_url, "source URL")
        try:
            parsed = urlparse(self.source_url)
            accepted = (parsed.scheme in {"https", "http"} and parsed.hostname
                        and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
        except ValueError:
            accepted = False
        if not accepted:
            raise ValueError("source URL must be absolute HTTP(S), without credentials, query or fragment")
        text(self.source_version, "source version")
        if not isinstance(self.content_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", self.content_sha256) is None:
            raise ValueError("source hash must be lowercase SHA-256")
        timestamp(self.observed_at)
        count(self.ttl_seconds, "source TTL")
        if (not self.ttl_seconds or not isinstance(self.confidence, str)
                or self.confidence not in {"HIGH", "MEDIUM", "LOW", "UNASSESSED"}):
            raise ValueError("source requires positive TTL and explicit confidence class")

    def is_current(self, now: datetime) -> bool:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("current time must include a timezone")
        age = now.astimezone(timezone.utc) - timestamp(self.observed_at)
        microseconds = (age.days * 86400 + age.seconds) * 1_000_000 + age.microseconds
        return 0 <= microseconds < self.ttl_seconds * 1_000_000

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ModelIdentity:
    status: str = "UNKNOWN"
    native_model: str | None = None
    canonical_family: str | None = None
    canonical_alias: str | None = None
    source: EconomicSource | None = None
    reason: str | None = "no source-bound mapping supplied"

    def __post_init__(self) -> None:
        if not isinstance(self.status, str) or self.status not in {"OBSERVED", "UNKNOWN", "ABSENT"}:
            raise ValueError("model identity requires an explicit observation status")
        if self.status == "OBSERVED":
            text(self.native_model, "native model")
            text(self.canonical_family, "canonical family")
            if self.canonical_alias is not None:
                text(self.canonical_alias, "canonical alias")
            if (not isinstance(self.source, EconomicSource) or self.reason is not None
                    or self.source.source_class == "UNAVAILABLE"):
                raise ValueError("observed model identity requires provenance, without an unknown reason")
        else:
            if any(v is not None for v in (self.native_model, self.canonical_family, self.canonical_alias)):
                raise ValueError("unobserved model identity cannot carry an invented mapping")
            text(self.reason, "unknown identity reason")
        if self.source is not None and not isinstance(self.source, EconomicSource):
            raise ValueError("model identity source must be typed")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LinearTerm:
    dimension: str
    amount: str
    per_units: int = 1

    def __post_init__(self) -> None:
        if not isinstance(self.dimension, str) or self.dimension not in USAGE_DIMENSIONS | {"constant"}:
            raise ValueError("unknown usage dimension")
        decimal_amount(self.amount)
        count(self.per_units, "rate denominator")
        if not self.per_units:
            raise ValueError("rate denominator must be positive")


@dataclass(frozen=True)
class UsageBand:
    minimum_context: int
    maximum_context: int | None
    terms: tuple[LinearTerm, ...]

    def __post_init__(self) -> None:
        count(self.minimum_context, "minimum context")
        if self.maximum_context is not None:
            count(self.maximum_context, "maximum context")
            if self.maximum_context <= self.minimum_context:
                raise ValueError("context band must be nonempty")
        _tuple(self.terms, LinearTerm, "linear terms")
        if not self.terms or len({t.dimension for t in self.terms}) != len(self.terms):
            raise ValueError("rule terms must be nonempty and dimension-unique")


@dataclass(frozen=True)
class UsageRule:
    rule_version: str
    unit: str
    basis: str
    service_tier: str
    bands: tuple[UsageBand, ...]
    quota_window: str | None = None

    def __post_init__(self) -> None:
        if (self.rule_version != "linear-usage-v1" or not isinstance(self.basis, str)
                or self.basis not in {"REQUEST", "USAGE", "FIXED"}):
            raise ValueError("unsupported rule algorithm or billing basis")
        text(self.unit, "rule unit")
        text(self.service_tier, "exact service tier")
        if self.quota_window is not None:
            text(self.quota_window, "native quota window")
        _tuple(self.bands, UsageBand, "context bands")
        if not self.bands or self.bands[0].minimum_context != 0 or self.bands[-1].maximum_context is not None:
            raise ValueError("context bands must cover zero through unbounded context")
        for previous, current in zip(self.bands, self.bands[1:]):
            if previous.maximum_context != current.minimum_context:
                raise ValueError("context bands must be contiguous and nonoverlapping")
        expected = {"requests"} if self.basis == "REQUEST" else {"constant"} if self.basis == "FIXED" else USAGE_DIMENSIONS
        if self.basis == "FIXED" and len(self.bands) != 1:
            raise ValueError("one-time allowance observations cannot depend on request context")
        if any({term.dimension for term in band.terms} != expected for band in self.bands):
            raise ValueError("every billed category needs an explicit rate, including known zero")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def rule_hash(self) -> str:
        return digest(self.to_dict())

    def evaluate(self, usage: Mapping[str, int] | None, *, service_tier: str,
                 context_tokens: int | None = None) -> Fraction | None:
        if service_tier != self.service_tier:
            return None
        if usage is not None:
            if not isinstance(usage, Mapping):
                return None
            if set(usage) - USAGE_DIMENSIONS:
                raise ValueError("usage has an unknown or overlapping dimension")
            for name, value in usage.items():
                count(value, name)
        if len(self.bands) > 1 and context_tokens is None:
            return None
        if context_tokens is not None:
            count(context_tokens, "context tokens")
        context = context_tokens if context_tokens is not None else 0
        band = next(b for b in self.bands if b.minimum_context <= context
                    and (b.maximum_context is None or context < b.maximum_context))
        if self.basis == "FIXED":
            term = band.terms[0]
            return decimal_amount(term.amount) / term.per_units
        if usage is None:
            return None
        if any(term.dimension not in usage for term in band.terms):
            return None
        return sum((decimal_amount(term.amount) * usage[term.dimension] / term.per_units
                    for term in band.terms), Fraction())


@dataclass(frozen=True)
class EconomicObservation:
    dimension: str
    status: str
    source: EconomicSource
    rule: UsageRule | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if (self.dimension not in DIMENSIONS or not isinstance(self.status, str)
                or self.status not in {"OBSERVED", "UNKNOWN", "ABSENT"}):
            raise ValueError("economic observation requires a named dimension and status")
        if not isinstance(self.source, EconomicSource):
            raise ValueError("economic observation requires typed provenance")
        if self.source.source_class not in TARIFF_PRECEDENCE:
            raise ValueError("this source cannot establish offer terms")
        if self.status == "OBSERVED":
            if not isinstance(self.rule, UsageRule) or self.reason is not None or self.source.source_class == "UNAVAILABLE":
                raise ValueError("observed terms require a supported rule and available source")
            if self.dimension == "marginal_cash" and re.fullmatch(r"[A-Z]{3}", self.rule.unit) is None:
                raise ValueError("marginal cash requires an explicit currency unit")
            if self.rule.basis == "FIXED" and self.dimension != "allowance_bonus":
                raise ValueError("fixed observations are only one-time allowance bonuses")
        else:
            if self.rule is not None:
                raise ValueError("unobserved terms cannot carry a rate")
            text(self.reason, "unobserved terms reason")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Promotion:
    promotion_id: str
    affects: tuple[str, ...]
    source: EconomicSource
    description: str

    def __post_init__(self) -> None:
        text(self.promotion_id, "promotion ID")
        _tuple(self.affects, str, "promotion dimensions")
        if (not self.affects or len(set(self.affects)) != len(self.affects)
                or set(self.affects) - (set(DIMENSIONS) - {"published_tariff"})):
            raise ValueError("promotion must identify its distinct economic effects")
        if not isinstance(self.source, EconomicSource) or self.source.source_class not in TARIFF_PRECEDENCE:
            raise ValueError("promotion requires typed provenance")
        text(self.description, "promotion description")


@dataclass(frozen=True)
class OfferEconomicTerms:
    observations: tuple[EconomicObservation, ...] = ()
    model_identity: ModelIdentity = field(default_factory=ModelIdentity)
    promotions: tuple[Promotion, ...] = ()
    billing_class: str = "UNKNOWN"
    billing_source: EconomicSource | None = None

    def __post_init__(self) -> None:
        _tuple(self.observations, EconomicObservation, "economic observations")
        _tuple(self.promotions, Promotion, "promotions")
        if not isinstance(self.model_identity, ModelIdentity):
            raise ValueError("model identity must be a typed record")
        if (not isinstance(self.billing_class, str)
                or self.billing_class not in {"UNKNOWN", "SUBSCRIPTION_SUNK_COST", "METERED", "LOCAL"}):
            raise ValueError("unsupported billing classification")
        if self.billing_class != "UNKNOWN" and not isinstance(self.billing_source, EconomicSource):
            raise ValueError("billing classification requires its own source")
        if self.billing_source is not None and not isinstance(self.billing_source, EconomicSource):
            raise ValueError("billing source must be typed")
        if self.billing_class != "UNKNOWN" and self.billing_source.source_class == "UNAVAILABLE":
            raise ValueError("unavailable source cannot establish a billing classification")

    def to_dict(self) -> dict[str, Any]:
        # Produce JSON arrays, not mutable references to the frozen tuples.
        return json.loads(json.dumps(asdict(self)))


def terms_from_dict(raw: Mapping[str, Any]) -> OfferEconomicTerms:
    """Strict reconstruction; unknown fields must not disappear during hashing."""
    def checked(value, kind):
        if not isinstance(value, Mapping) or set(value) != set(kind.__dataclass_fields__):
            raise ValueError(f"{kind.__name__} has missing or unknown fields")
        return dict(value)

    def source(value):
        return EconomicSource(**checked(value, EconomicSource)) if value is not None else None

    def rule(value):
        if value is None:
            return None
        data = checked(value, UsageRule)
        if not isinstance(data["bands"], list) or len(data["bands"]) > 100:
            raise ValueError("rule bands must be a bounded JSON array")
        bands = []
        for entry in data["bands"]:
            band = checked(entry, UsageBand)
            if not isinstance(band["terms"], list) or len(band["terms"]) > 100:
                raise ValueError("rule terms must be a bounded JSON array")
            band["terms"] = tuple(LinearTerm(**checked(t, LinearTerm)) for t in band["terms"])
            bands.append(UsageBand(**band))
        data["bands"] = tuple(bands)
        return UsageRule(**data)

    data = checked(raw, OfferEconomicTerms)
    observations = data["observations"]
    promotions = data["promotions"]
    if any(not isinstance(v, list) or len(v) > 100 for v in (observations, promotions)):
        raise ValueError("observations and promotions must be bounded JSON arrays")
    parsed = []
    for entry in observations:
        item = checked(entry, EconomicObservation)
        item["source"], item["rule"] = source(item["source"]), rule(item["rule"])
        parsed.append(EconomicObservation(**item))
    data["observations"] = tuple(parsed)
    identity = checked(data["model_identity"], ModelIdentity)
    identity["source"] = source(identity["source"])
    data["model_identity"] = ModelIdentity(**identity)
    parsed = []
    for entry in promotions:
        item = checked(entry, Promotion)
        if not isinstance(item["affects"], list):
            raise ValueError("promotion dimensions must be a JSON array")
        item["source"], item["affects"] = source(item["source"]), tuple(item["affects"])
        parsed.append(Promotion(**item))
    data["promotions"] = tuple(parsed)
    data["billing_source"] = source(data["billing_source"])
    return OfferEconomicTerms(**data)


@dataclass(frozen=True)
class RealizedCharge:
    execution_ref: str
    currency: str
    amount: str
    source: EconomicSource

    def __post_init__(self) -> None:
        if not isinstance(self.execution_ref, str) or re.fullmatch(r"attempt:[0-9a-f]{64}", self.execution_ref) is None:
            raise ValueError("realized charge requires an exact attempt reference")
        if not isinstance(self.currency, str) or re.fullmatch(r"[A-Z]{3}", self.currency) is None:
            raise ValueError("realized charge requires an explicit currency")
        decimal_amount(self.amount)
        if not isinstance(self.source, EconomicSource) or self.source.source_class not in {
                "PROVIDER_BILLING_RECORD", "PROVIDER_RESPONSE"}:
            raise ValueError("realized charge requires provider billing/response evidence")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
