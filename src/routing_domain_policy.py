"""Deterministic, fail-closed per-domain routing and privacy policy.

Sensitive domains default to local execution and deny hosted targets; a missing
local target raises :class:`PolicyDenied` rather than falling back to a hosted
service. Kept dependency-light so the engine and coordinator can both import it.
Operators override policies via the ``domains`` key of ``routing_policy.json``.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, FrozenSet, List, Optional, Sequence
from urllib.parse import urlparse

# Local providers; anything unlisted is treated as hosted (fail closed).
LOCAL_PROVIDERS: FrozenSet[str] = frozenset({
    "ollama", "local", "vllm", "llama.cpp", "lmstudio", "framework", "absis",
})

#: Domains whose default policy is local-only (real personal content).
SENSITIVE_LOCAL_DOMAINS: FrozenSet[str] = frozenset({
    "personal", "health", "finance", "taxes",
})

DEV_DOMAINS: FrozenSet[str] = frozenset({
    "dev", "general_swe", "tacticus_analytics", "infra", "data_analysis",
    "documentation",
})

# Mirrors routing_engine's Section 9 hard filter ordering.
_SENSITIVITY_RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3, "secret": 4}
KNOWN_SENSITIVITIES = frozenset(_SENSITIVITY_RANK)
_DEFAULT_CEILING = "confidential"

_PRIVATE_HOST_RE = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|host\.docker\.internal"
    r"|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+"
    r"|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|[^.]+)$"
)


class PolicyDenied(Exception):
    """Typed, fail-closed refusal: no approved execution path for this route."""

    def __init__(self, domain: str, rule: str, reason: str, denied: List[str]):
        self.domain = domain
        self.rule = rule
        self.reason = reason
        self.denied = list(denied)
        super().__init__(f"{domain}: {rule} ({reason})")


@dataclass(frozen=True)
class DomainPolicy:

    domain: str
    local_only: bool = False
    allowed_providers: Optional[FrozenSet[str]] = None  # None -> any not denied
    denied_providers: FrozenSet[str] = frozenset()
    fallback_permitted: bool = False
    model_substitution_permitted: bool = False
    approval_required: bool = False
    budget_class: str = "standard"


_DEFAULT_POLICIES: Dict[str, DomainPolicy] = {
    d: DomainPolicy(
        domain=d, local_only=False, fallback_permitted=True,
        model_substitution_permitted=True, approval_required=False,
        budget_class="dev",
    )
    for d in DEV_DOMAINS
}
for _d in SENSITIVE_LOCAL_DOMAINS:
    _DEFAULT_POLICIES[_d] = DomainPolicy(
        domain=_d, local_only=True, fallback_permitted=False,
        model_substitution_permitted=False, approval_required=True,
        budget_class="sensitive",
    )
_DEFAULT_POLICIES["homelab"] = DomainPolicy(
    domain="homelab", local_only=True, fallback_permitted=False,
    model_substitution_permitted=False, approval_required=False,
    budget_class="sensitive",
)
#: Unknown / unclassified domains fail closed (local-only, deny hosted).
_UNKNOWN_POLICY = DomainPolicy(
    domain="unknown", local_only=True, fallback_permitted=False,
    model_substitution_permitted=False, approval_required=True,
    budget_class="sensitive",
)


@dataclass(frozen=True)
class RoutingDecision:
    """Audit record every evaluation leaves, allowed or refused."""

    domain: str
    provider: str
    sensitivity: str
    allowed: bool
    rule: str
    reason: str
    budget_class: str
    approval_required: bool
    run_id: str
    decided_at: str

    def to_dict(self) -> dict:
        return {
            "domain": self.domain,
            "provider": self.provider,
            "sensitivity": self.sensitivity,
            "allowed": self.allowed,
            "rule": self.rule,
            "reason": self.reason,
            "budget_class": self.budget_class,
            "approval_required": self.approval_required,
            "run_id": self.run_id,
            "decided_at": self.decided_at,
        }


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _endpoint_is_local(endpoint_url: Optional[str]) -> bool:
    if not endpoint_url:
        return False
    host = urlparse((endpoint_url or "").strip()).hostname or ""
    return bool(host) and bool(_PRIVATE_HOST_RE.match(host))


def provider_is_local(provider: str) -> bool:
    return (provider or "").strip().lower() in LOCAL_PROVIDERS


def _coerce_policy(domain: str, entry: dict) -> DomainPolicy:
    def _set(v):
        return frozenset(x.strip().lower() for x in (v or []) if str(x).strip())

    return DomainPolicy(
        domain=domain,
        local_only=bool(entry.get("localOnly", entry.get("local_only", False))),
        allowed_providers=_set(entry.get("allowed_providers", entry.get("allowedProviders", [])))
        or None,
        denied_providers=_set(entry.get("denied_providers", entry.get("deniedProviders", []))),
        fallback_permitted=bool(entry.get("fallbackPermitted", entry.get("fallback_permitted", False))),
        model_substitution_permitted=bool(
            entry.get("modelSubstitutionPermitted", entry.get("model_substitution_permitted", False))
        ),
        approval_required=bool(entry.get("approvalRequired", entry.get("approval_required", False))),
        budget_class=str(entry.get("budgetClass", entry.get("budget_class", "standard"))),
    )


def domain_policy(domain: str) -> DomainPolicy:
    """Resolve the policy for ``domain`` (operator override wins over defaults)."""
    dom = (domain or "unknown").strip().lower()
    try:
        from src.routing_policy import load_policy

        configured = (load_policy() or {}).get("domains") or {}
        entry = configured.get(dom)
        if entry is None:
            entry = configured.get((domain or "").strip())
        if isinstance(entry, dict):
            return _coerce_policy(dom, entry)
    except Exception:
        pass
    return _DEFAULT_POLICIES.get(dom, _UNKNOWN_POLICY)


def _ceiling_rank() -> int:
    try:
        from src.routing_policy import load_policy

        ceiling = (load_policy() or {}).get("remoteSensitivityCeiling", _DEFAULT_CEILING)
    except Exception:
        ceiling = _DEFAULT_CEILING
    return _SENSITIVITY_RANK.get(ceiling, _SENSITIVITY_RANK[_DEFAULT_CEILING])


def evaluate_route(
    *,
    domain: str,
    provider: str,
    sensitivity: str = "internal",
    policy: Optional[DomainPolicy] = None,
    endpoint_url: Optional[str] = None,
) -> RoutingDecision:
    """Evaluate one (domain, provider) route. Fail-closed; always leaves a record."""
    pol = policy or domain_policy(domain)
    p = (provider or "").strip().lower()
    local = provider_is_local(p) or _endpoint_is_local(endpoint_url or "")

    def _decide(allowed: bool, rule: str, reason: str) -> RoutingDecision:
        return RoutingDecision(
            domain=pol.domain,
            provider=p,
            sensitivity=sensitivity,
            allowed=allowed,
            rule=rule,
            reason=reason,
            budget_class=pol.budget_class,
            approval_required=pol.approval_required,
            run_id=str(uuid.uuid4()),
            decided_at=_utc_iso(),
        )

    if p in pol.denied_providers:
        return _decide(False, "provider-denied", f"{p} is on the domain deny-list")
    if pol.allowed_providers is not None and p not in pol.allowed_providers:
        return _decide(False, "provider-not-allowed", f"{p} is not on the domain allow-list")

    if sensitivity not in KNOWN_SENSITIVITIES:
        return _decide(False, "sensitivity-unknown",
                       f"unsupported sensitivity classification: {sensitivity!r}")

    if _SENSITIVITY_RANK[sensitivity] > _ceiling_rank() and not local:
        return _decide(False, "sensitivity-ceiling", f"{sensitivity} data requires local execution")

    if pol.local_only and not local:
        return _decide(False, "sensitive-domain-local-only", f"domain {pol.domain} requires local execution")

    return _decide(True, "allowed", "ok")


def select_route(
    *,
    domain: str,
    candidates: Sequence[str],
    sensitivity: str = "internal",
    policy: Optional[DomainPolicy] = None,
) -> RoutingDecision:
    """First policy-approved provider, else :class:`PolicyDenied`; never a downgrade."""
    pol = policy or domain_policy(domain)
    denials: List[str] = []
    for cand in candidates or []:
        provider = (cand if isinstance(cand, str) else "").strip()
        decision = evaluate_route(
            domain=pol.domain, provider=provider, sensitivity=sensitivity, policy=pol,
        )
        if decision.allowed:
            return decision
        denials.append(f"{provider}: {decision.rule}")
    raise PolicyDenied(
        domain=pol.domain,
        rule="no-approved-provider",
        reason=f"none of {list(candidates or [])} permitted by domain policy",
        denied=denials,
    )
