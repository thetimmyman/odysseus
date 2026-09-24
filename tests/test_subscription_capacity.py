from datetime import datetime, timedelta, timezone
import pytest
from src import subscription_capacity as sc
from src import provider_capacity as pc


def fixture_payload(provider):
    future = datetime.now(timezone.utc) + timedelta(days=1)
    if provider == "opencode-go":
        return {"usage": {name: {"percent": 20, "resetsAt": future.isoformat(), "status": "ok"} for name in ("rolling", "weekly", "monthly")}}
    return {"windowLimits": {name: {"cap": 100, "used": 20, "resetAt": future.timestamp()*1000} for name in ("fiveHour", "weekly")}, "credits": {"monthlyCredits": 30, "purchasedCredits": 0, "freeCredits": 0}}


@pytest.mark.parametrize("provider,url", [("opencode-go", "https://opencode.ai/zen/go/v1/chat/completions"), ("command-code", "https://api.commandcode.ai/provider/v1/chat/completions")])
def test_bound_subscription_reads(monkeypatch, provider, url):
    payload = fixture_payload(provider)
    seen = []
    def read(target, headers):
        seen.append((target, headers))
        if target.endswith("/subscriptions"):
            return {"data": {"planId": "individual-goat", "status": "active"}}
        return {"data": [{"id": "actual-model"}]} if target.endswith("/models") else payload
    monkeypatch.setattr(sc, "_read", read)
    headers = {"Authorization": "Bearer fixture"}
    receipt = sc.collect_api_capacity(url, "actual-model", headers)
    assert len(seen) == (3 if provider == "command-code" else 2) and all(h is headers for _, h in seen)
    from src.dispatch_routing import ExecutionTargetProfile, classify_capacity_for, LOCALITY_HOSTED
    profile = ExecutionTargetProfile(target_id="t", profile_id="p", provider=provider, model="actual-model", locality=LOCALITY_HOSTED, endpoint_url=url, credential_sha256=receipt.credential_sha256)
    assert classify_capacity_for(profile, [receipt])[0]
    from dataclasses import replace
    assert not classify_capacity_for(replace(profile, endpoint_url="https://other/v1/chat/completions"), [receipt])[0]
    assert not classify_capacity_for(replace(profile, provider="another-provider"), [receipt])[0]
    assert receipt.provider == provider and receipt.state == pc.CapacityState.AVAILABLE
    assert receipt.credential_sha256 == sc.credential_fingerprint(headers)
    assert receipt.price is None and "sha256=" in receipt.evidence_reference
    assert "fixture" not in str(receipt.to_dict())
    if provider == "opencode-go":
        payload["usage"]["monthly"]["percent"] = 100
    else:
        payload["credits"]["monthlyCredits"] = 0
    assert sc.collect_api_capacity(url, "actual-model", headers).state == pc.CapacityState.EXHAUSTED
    with pytest.raises(ValueError, match="absent"):
        sc.collect_api_capacity(url, "invented-model", headers)
    payload.clear()
    with pytest.raises(ValueError, match="missing"):
        sc.collect_api_capacity(url, "actual-model", headers)


def test_no_credentials_sent_to_unknown_or_redirected_hosts(monkeypatch):
    monkeypatch.setattr(sc, "_read", lambda *a: pytest.fail("network must not be called"))
    with pytest.raises(ValueError):
        sc.collect_api_capacity("https://api.commandcode.ai.evil/v1/chat/completions", "m", {"Authorization": "fixture"})
    monkeypatch.undo()
    class Response:
        status_code = 302
    def get(url, **kwargs):
        assert kwargs["allow_redirects"] is False
        return Response()
    monkeypatch.setattr(sc.requests, "get", get)
    with pytest.raises(ValueError, match="HTTP 302"):
        sc._read("https://api.commandcode.ai/provider/v1/models", {"Authorization": "fixture"})


@pytest.mark.parametrize("plan,status", [("individual-go", "active"), ("unrecognized", "active"), ("individual-goat", "cancelled")])
def test_command_code_requires_observed_api_eligible_plan(monkeypatch, plan, status):
    def read(url, headers):
        if url.endswith("/models"):
            return {"data": [{"id": "m"}]}
        if url.endswith("/subscriptions"):
            return {"data": {"planId": plan, "status": status}}
        return fixture_payload("command-code")
    monkeypatch.setattr(sc, "_read", read)
    with pytest.raises(ValueError, match="API-eligible"):
        sc.collect_api_capacity("https://api.commandcode.ai/provider/v1/chat/completions", "m", {"Authorization": "Bearer fixture"})


def test_database_endpoint_capacity_canonical_dispatch_and_invocation(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from sqlalchemy.orm import sessionmaker
    from tests.test_dispatch_boundary import _db, _seed, _candidates
    from core.database import ModelEndpoint, RoutingModelProfile
    from src import capacity_collector as collector, endpoint_resolver as resolver, dispatch_boundary as boundary
    from src import dispatch_routing as routing
    from src.local_targets import TargetCapabilityReceipt, RuntimeIdentity, ModelIdentity, ContextProfile, CapabilityEvidence
    from src.provider_capacity_store import ProviderCapacityStore
    db = _db(); task = _seed(db, sensitivity="public", allow_paid=True)
    endpoint = db.get(ModelEndpoint, "ep-or")
    endpoint.name = "My friendly bargain endpoint"
    endpoint.base_url = "https://api.commandcode.ai/provider/v1"
    model = db.get(RoutingModelProfile, "p-openrouter").model
    db.commit()
    sessions = sessionmaker(bind=db.get_bind())
    monkeypatch.setattr(collector, "SessionLocal", sessions)
    monkeypatch.setattr(resolver, "SessionLocal", sessions)
    monkeypatch.setattr(resolver, "resolve_endpoint_runtime", lambda ep, **kwargs: (ep.base_url, "fixture-key"))
    def read(url, headers):
        assert headers["Authorization"] == "Bearer fixture-key"
        if url.endswith("/models"):
            return {"data": [{"id": model}]}
        if url.endswith("/subscriptions"):
            return {"data": {"planId": "individual-goat", "status": "active"}}
        return fixture_payload("command-code")
    monkeypatch.setattr(sc, "_read", read)
    store = ProviderCapacityStore(tmp_path / "capacity")
    capacity = collector.collect_endpoint_capacity(store, endpoint.id, model)
    actual_url, actual_model, headers = resolver.resolve_endpoint_by_id(endpoint.id, model)
    now = datetime.now(timezone.utc)
    qualified = TargetCapabilityReceipt(host_id="host", profile_id="p-openrouter", observed_at=now.isoformat(),
        runtime=RuntimeIdentity(provider="friendly-label", runtime_kind="openai_compatible", endpoint_url=endpoint.base_url, endpoint_type="openai_compatible"),
        model=ModelIdentity(model_id=model, alias=model, digest="verified-model"),
        context=ContextProfile(safe_working_context=32768),
        capabilities=CapabilityEvidence(measured=(routing.CAP_TEXT_GENERATION, routing.CAP_EXACT_REFERENCE_SEMANTICS)),
        health="healthy", health_checked_at=now.isoformat(), qualification_ref="qualified", locality="hosted")
    capability_store = SimpleNamespace(current=lambda profile_id: qualified)
    estate = boundary.profiles_from_candidates(db, _candidates("p-openrouter"), capability_store=capability_store, now=now)
    assert estate.profiles[0].provider == "command-code"
    assert estate.profiles[0].endpoint_url == actual_url == capacity.endpoint_url
    # Exercise the actual config builder/reader, not a mocked quote function.
    from src.promotional_dispatch import configure_verified_offer
    from src.llm_core import _detect_provider
    workload = {"input_tokens": 10, "output_tokens": 10, "cache_read_tokens": 0, "cache_write_tokens": 0}
    identity = {"credential_sha256": sc.credential_fingerprint(headers), "endpoint_id": endpoint.id, "transport_provider": _detect_provider(actual_url)}
    source = tmp_path / "terms-source"
    source.write_bytes(b"Synthetic test-only verified offer: USD 0.10 per request")
    terms = {"verified_by": "test-only", **identity, "provider": capacity.provider, "pool_id": capacity.pool_id,
        "native_model": actual_model, "harness": "openai_compatible", "usage_path": "api-key",
        "tariff_id": "fixture-tariff", "tariff_version": "1", "source_url": "https://example.test/terms",
        "source_sha256": sc.hashlib.sha256(source.read_bytes()).hexdigest(), "observed_at": now.isoformat(), "ttl_seconds": 300,
        "valid_from": (now-timedelta(seconds=30)).isoformat(), "valid_until": (now+timedelta(minutes=5)).isoformat(),
        "unit": "request", "offered_rate_usd": .1, "workload": workload}
    terms_path = tmp_path / "verified-terms.json"
    terms_path.write_text(sc.json.dumps(terms))
    config_path = configure_verified_offer(terms_path=terms_path, source_path=source, capacity_store=store.directory,
        profile_id="p-openrouter", chat_url=actual_url, directory=tmp_path / "offer", maximum_predicted_request_usd="1")
    monkeypatch.delenv("ODYSSEUS_FREE_OFFER_CONFIG", raising=False)
    monkeypatch.setenv("ODYSSEUS_OFFER_CONFIG", config_path)
    bound = boundary.resolve_dispatch(db, task, _candidates("p-openrouter"), role="scout", capability_store=capability_store, capacity_receipts=[capacity], now=now, workload=workload, offer_identity=identity)
    assert bound.offer_quotes[0]["predicted_cash_usd"] == "0.1"
    assert bound.offer_quotes[0]["chat_url"] == actual_url
    assert bound.decision.offer_quote_digests

    invocation = boundary.InvocationIdentity(credential_sha256=capacity.credential_sha256, profile_id="p-openrouter", provider="command-code", model=actual_model,
        chat_url=actual_url, runtime_kind="openai_compatible", runtime_version="", runtime_commit="", runtime_image_digest="", backend="", backend_version="", model_digest="verified-model", endpoint_type="openai_compatible", locality="hosted", workload=workload, offer_identity=identity)
    boundary.verify_invocation(bound, invocation=invocation)
    from dataclasses import replace
    with pytest.raises(boundary.DispatchPinViolation, match="credentials"):
        boundary.verify_invocation(bound, invocation=replace(invocation, credential_sha256="b" * 64))
    db.close()


def test_unknown_opencode_status_is_not_invented_rate_limit(monkeypatch):
    payload = fixture_payload("opencode-go")
    payload["usage"]["weekly"]["status"] = "maintenance_unknown"
    monkeypatch.setattr(sc, "_read", lambda url, headers: {"data": [{"id": "m"}]} if url.endswith("/models") else payload)
    with pytest.raises(ValueError, match="unknown provider usage window status"):
        sc.collect_api_capacity("https://opencode.ai/zen/go/v1/chat/completions", "m", {"Authorization": "Bearer fixture"})
