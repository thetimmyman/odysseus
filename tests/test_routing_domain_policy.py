"""Deterministic per-domain routing/privacy policy.

Covers five controls: a sensitive domain refuses hosted execution
(negative); the same domain succeeds against a local target (positive); a dev
domain may use a hosted target (non-sensitive control); fallback never escapes
the domain policy and fails closed (fallback control); and every case leaves an
audit record (audit control). Also: the §9 sensitivity ceiling still binds even
for dev domains, and the policy is operator-overridable via config.
"""
import pytest

from src import routing_domain_policy as rdp


def test_sensitive_domain_refuses_hosted_provider():
    d = rdp.evaluate_route(domain="finance", provider="openrouter", sensitivity="internal")
    assert d.allowed is False
    assert d.rule == "sensitive-domain-local-only"


def test_sensitive_domain_allows_local_provider():
    d = rdp.evaluate_route(domain="finance", provider="ollama", sensitivity="internal")
    assert d.allowed is True
    assert d.rule == "allowed"


def test_dev_domain_allows_hosted_provider():
    d = rdp.evaluate_route(domain="general_swe", provider="openrouter", sensitivity="internal")
    assert d.allowed is True


def test_dev_domain_respects_sensitivity_ceiling():
    # 'restricted' ranks above the default 'confidential' ceiling, so even a dev
    # domain cannot send restricted data to a hosted provider.
    d = rdp.evaluate_route(domain="general_swe", provider="openrouter", sensitivity="restricted")
    assert d.allowed is False
    assert d.rule == "sensitivity-ceiling"


def test_unknown_sensitivity_fails_closed():
    d = rdp.evaluate_route(domain="general_swe", provider="openrouter",
                           sensitivity="internall")
    assert d.allowed is False
    assert d.rule == "sensitivity-unknown"


def test_local_endpoint_url_counts_as_local():
    d = rdp.evaluate_route(domain="finance", provider="unknownhost", endpoint_url="http://127.0.0.1:9000/v1")
    assert d.allowed is True


def test_fallback_fails_closed_for_sensitive_domain():
    with pytest.raises(rdp.PolicyDenied) as err:
        rdp.select_route(
            domain="finance",
            candidates=["openrouter", "cline-pass"],
            sensitivity="internal",
        )
    assert err.value.rule == "no-approved-provider"
    # Fallback must never have escaped the policy: every candidate refused.
    assert len(err.value.denied) == 2


def test_fallback_selects_first_approved_for_dev_domain():
    d = rdp.select_route(domain="general_swe", candidates=["openrouter", "anthropic"])
    assert d.allowed is True
    assert d.provider == "openrouter"


def test_provider_deny_list():
    pol = rdp.DomainPolicy(domain="general_swe", denied_providers=frozenset({"openrouter"}))
    d = rdp.evaluate_route(domain="general_swe", provider="openrouter", policy=pol)
    assert d.allowed is False
    assert d.rule == "provider-denied"


def test_provider_allow_list():
    pol = rdp.DomainPolicy(domain="general_swe", allowed_providers=frozenset({"anthropic"}))
    assert rdp.evaluate_route(domain="general_swe", provider="anthropic", policy=pol).allowed is True
    assert rdp.evaluate_route(domain="general_swe", provider="openrouter", policy=pol).allowed is False


def test_operator_override_via_config(monkeypatch):
    monkeypatch.setattr(
        "src.routing_policy.load_policy",
        lambda: {"domains": {"finance": {"local_only": False, "allowed_providers": ["openrouter"]}}},
    )
    pol = rdp.domain_policy("finance")
    assert pol.local_only is False
    assert pol.allowed_providers == frozenset({"openrouter"})


def test_decision_record_is_complete():
    d = rdp.evaluate_route(domain="finance", provider="ollama")
    rec = d.to_dict()
    for key in ("domain", "provider", "sensitivity", "allowed", "rule", "reason",
                "budget_class", "approval_required", "run_id", "decided_at"):
        assert key in rec
    assert rec["domain"] == "finance"
    assert rec["run_id"]


def test_unknown_domain_fails_closed():
    pol = rdp.domain_policy("not_a_real_domain")
    assert pol.local_only is True
