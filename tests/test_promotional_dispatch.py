from datetime import datetime, timezone, timedelta
import hashlib
import json

import pytest

from src.promotional_dispatch import enforce_free_offer, PromotionUnavailable
from src.provider_model_offer import OfferProvenance, PriceEffect
from src.provider_capacity_store import ProviderCapacityStore
from tests.test_provider_model_offer import offer
from tests.test_ps640_provider_capacity import receipt, prov


@pytest.fixture
def configured(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    stamp = now.isoformat()
    capacity = receipt(credential_sha256=hashlib.sha256(b"{}").hexdigest(), observed_at=stamp, state_provenance=prov(observed=stamp),
                       entitlement_provenance=prov(observed=stamp), zdr_provenance=prov(observed=stamp), price=None)
    store = ProviderCapacityStore(str(tmp_path / "capacity"))
    store.append(capacity)
    source = tmp_path / "source"
    source.write_bytes(b"fixture free price terms")
    record = offer(provider=capacity.provider, pool_id=capacity.pool_id,
        capacity_receipt_ref=capacity.ref, harness="odysseus-scout", native_model="qwen3.8:27b",
        valid_from=(now - timedelta(hours=1)).isoformat(), valid_until=(now + timedelta(hours=1)).isoformat(),
        provenance=OfferProvenance("https://example.test/price", hashlib.sha256(source.read_bytes()).hexdigest(),
                                  "test", stamp, 3600), price=PriceEffect("USD", "request", 1, 0))
    path = tmp_path / "offer.json"
    path.write_text(json.dumps(record.to_dict()))
    config = {"capacity_store": store.directory, "profiles": {"p": {
        "provider": capacity.provider, "pool_id": capacity.pool_id, "harness": "odysseus-scout",
        "credential_sha256": capacity.credential_sha256, "account_identity": capacity.account_identity, "endpoint_id": "e", "transport_provider": "openai",
        "usage_path": record.usage_path, "chat_url": "https://example.test/v1/chat/completions",
        "offer_path": str(path), "source_path": str(source)}}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    monkeypatch.setenv("ODYSSEUS_FREE_OFFER_CONFIG", str(config_path))
    kwargs = dict(credential_sha256=capacity.credential_sha256, endpoint_id="e", transport_provider="openai", profile_id="p", model="qwen3.8:27b", harness="odysseus-scout",
                  chat_url=config["profiles"]["p"]["chat_url"])
    return kwargs, source, path, record, store, capacity


def test_exact_free_route_accepts_and_returns_evidence(configured):
    kwargs, source, path, record, store, capacity = configured
    assert enforce_free_offer(**kwargs)["offer_ref"] == record.ref


@pytest.mark.parametrize("field,value", [("profile_id", "paid-fallback"), ("model", "other"),
                                        ("chat_url", "https://other.test"), ("harness", "other")])
def test_scope_mismatch_or_paid_fallback_refused(configured, field, value):
    kwargs = configured[0]
    with pytest.raises(PromotionUnavailable):
        enforce_free_offer(**{**kwargs, field: value})


def test_changed_source_refused(configured):
    kwargs, source, *_ = configured
    source.write_text("changed terms")
    with pytest.raises(PromotionUnavailable, match="source"):
        enforce_free_offer(**kwargs)


def test_expiry_is_rechecked_per_call(configured):
    kwargs, source, path, record, *_ = configured
    assert enforce_free_offer(**kwargs)
    from src.provider_model_offer import make_provider_model_offer_receipt
    import dataclasses
    fields = {f.name: getattr(record, f.name) for f in dataclasses.fields(record)}
    fields["valid_until"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    path.write_text(json.dumps(make_provider_model_offer_receipt(**fields).to_dict()))
    with pytest.raises(PromotionUnavailable, match="expired"):
        enforce_free_offer(**kwargs)


def test_unconfigured_default_does_not_change_routing(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_FREE_OFFER_CONFIG", raising=False)
    assert enforce_free_offer(profile_id="p", model="m", chat_url="x", harness="h") is None


def test_free_preference_stays_inside_explicit_equivalent_tiers(configured, monkeypatch):
    from src.promotional_dispatch import prefer_verified_free
    from pathlib import Path
    import os
    kwargs = configured[0]
    path = Path(os.environ["ODYSSEUS_FREE_OFFER_CONFIG"])
    config = json.loads(path.read_text())
    config.update(prefer_verified_free=True, quality_tiers={"paid": "tier1", "p": "tier1", "higher": "tier0"})
    path.write_text(json.dumps(config))
    candidates = [{"profile_id": "higher"}, {"profile_id": "paid"}, {"profile_id": "p"}]
    ordered = prefer_verified_free(candidates, resolve_candidate=lambda c: (kwargs["model"], kwargs["chat_url"], {k: kwargs[k] for k in ("credential_sha256", "endpoint_id", "transport_provider")}))
    assert [c["profile_id"] for c in ordered] == ["higher", "p", "paid"]
    assert candidates[1]["profile_id"] == "paid"
    config["quality_tiers"] = {}
    path.write_text(json.dumps(config))
    assert prefer_verified_free(candidates, resolve_candidate=lambda c: pytest.fail("must not infer tier")) == candidates


def test_construct_from_verified_terms_binds_current_capacity(configured, tmp_path, monkeypatch):
    from src.promotional_dispatch import configure_verified_offer
    kwargs, source, path, record, store, capacity = configured
    terms = {"verified_by": "operator", "credential_sha256": capacity.credential_sha256, "endpoint_id": "e", "transport_provider": "openai", "provider": record.provider, "pool_id": record.pool_id,
             "native_model": record.native_model, "harness": record.harness, "usage_path": record.usage_path,
             "tariff_id": record.tariff_id, "tariff_version": record.tariff_version,
             "source_url": record.provenance.source_url, "source_sha256": record.provenance.content_sha256,
             "observed_at": record.provenance.observed_at, "ttl_seconds": record.provenance.ttl_seconds,
             "valid_from": record.valid_from, "valid_until": record.valid_until,
             "unit": "request", "offered_rate_usd": 0}
    terms_path = tmp_path / "verified.json"
    terms_path.write_text(json.dumps(terms))
    config = configure_verified_offer(terms_path=terms_path, source_path=source,
        capacity_store=store.directory, profile_id="p", chat_url=kwargs["chat_url"], directory=tmp_path / "ready")
    monkeypatch.setenv("ODYSSEUS_FREE_OFFER_CONFIG", config)
    assert enforce_free_offer(**kwargs)["capacity_receipt_ref"] == capacity.ref


def test_explicit_exhausted_quota_refused_even_with_available_state(configured):
    from src.provider_capacity import QuotaDimension, make_capacity_receipt
    import dataclasses
    kwargs, source, path, record, store, capacity = configured
    fields = {f.name: getattr(capacity, f.name) for f in dataclasses.fields(capacity)}
    fields["quotas"] = (QuotaDimension("requests", "requests", limit=10, remaining=0,
                                      provenance=capacity.state_provenance),)
    updated = make_capacity_receipt(**fields)
    store.append(updated, supersedes=capacity.receipt_hash)
    with pytest.raises(PromotionUnavailable, match="quota|capacity"):
        enforce_free_offer(**kwargs)


@pytest.mark.parametrize("blocked", [True, False])
def test_real_executor_gate_and_observed_export(configured, monkeypatch, tmp_path, blocked):
    import sqlalchemy
    from sqlalchemy.orm import sessionmaker
    from core.database import Base, RoutingTask, RoutingModelProfile, RoutingModelRun, ModelEndpoint
    import src.routing_executor as executor
    engine = sqlalchemy.create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(ModelEndpoint(id="e", name="fixture", base_url="https://paid.test")); db.commit()
    if not blocked:
        monkeypatch.delenv("ODYSSEUS_FREE_OFFER_CONFIG")
        usage_config = tmp_path / "usage-config.json"
        usage_config.write_text(json.dumps({"directory": str(tmp_path / "usage"),
            "cohort": "fixture", "profiles": {"paid-fallback": "codex"}}))
        monkeypatch.setenv("ODYSSEUS_USAGE_EXPORT_CONFIG", str(usage_config))
    task = RoutingTask(id="t", title="test", objective="test", task_type="feature_plan", repo_path=str(tmp_path))
    profile = RoutingModelProfile(id="paid-fallback", model_endpoint_id="e", model="paid", roles="[]", enabled=True)
    db.add_all([task, profile]); db.commit()
    monkeypatch.setattr(executor, "build_context_bundle", lambda t: {})
    monkeypatch.setattr(executor, "_write_run_manifest", lambda *a: None)
    monkeypatch.setattr(executor, "archive_root", lambda: str(tmp_path / "runs"))
    monkeypatch.setattr(executor, "build_prompt", lambda *a: "prompt")
    monkeypatch.setattr(executor, "check_general_budget", lambda *a: {"allowed": True})
    monkeypatch.setattr(executor, "check_task_budget", lambda *a: {"allowed": True})
    monkeypatch.setattr(executor, "resolve_endpoint_by_id", lambda *a: ("https://paid.test", "paid", {}))
    calls = []
    def fake_call(*args, **kwargs):
        calls.append(args)
        return "response", {"input_tokens": 3, "output_tokens": 2}
    monkeypatch.setattr(executor, "llm_call_with_usage", fake_call)
    result = executor.execute_candidates(db, task, [{"profile_id": profile.id, "estimated_cost_usd": 0}], 1)
    row = db.query(RoutingModelRun).one()
    if blocked:
        assert result["status"] == "failed" and not calls
        assert json.loads(row.artifacts)["inference_attempted"] is False
    else:
        assert result["status"] == "succeeded" and len(calls) == 1
        assert json.loads(row.artifacts)["inference_attempted"] is True
        recorded = [json.loads(line) for line in (tmp_path / "usage/events.jsonl").read_text().splitlines()]
        assert [e["type"] for e in recorded] == ["task_registered", "turns_recorded"]
    db.close(); engine.dispose()
