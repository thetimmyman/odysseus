import dataclasses
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
from pathlib import Path

import pytest

from src.offer_economics import comparable_cash, quote_profile, prefer_discounted
from src.promotional_dispatch import PromotionUnavailable
from src.provider_model_offer import PriceEffect, CreditEffect, make_provider_model_offer_receipt
from tests.test_provider_model_offer import offer
from tests.test_promotional_dispatch import configured


WORK = {"input_tokens": 1_000_000, "output_tokens": 100_000,
        "cache_read_tokens": 500_000, "cache_write_tokens": 0}


def component(unit, rate, currency="USD"):
    return offer(price=PriceEffect(currency, unit, rate * 2, rate))


def test_disjoint_token_cache_rates_and_cash_savings():
    rates = [component("million_input_tokens", 2), component("million_output_tokens", 10),
             component("million_cache_read_tokens", .2)]
    assert comparable_cash(rates, WORK) == Decimal("3.1")
    assert comparable_cash(rates, WORK, list_price=True) == Decimal("6.2")


@pytest.mark.parametrize("rates", [
    [component("million_input_tokens", 2)],
    [component("request", 2), component("million_output_tokens", 10)],
    [component("request", 2, "credit")],
    [component("request", 2), component("request", 3)],
])
def test_missing_components_credit_cash_mixture_and_duplicate_units_rejected(rates):
    with pytest.raises(PromotionUnavailable):
        comparable_cash(rates, WORK)


def test_native_credit_face_value_never_changes_cash_quote():
    receipt = offer(price=PriceEffect("USD", "request", 2, 1),
                    credit=CreditEffect("credits", 5000, "promotional allowance"))
    assert comparable_cash([receipt], WORK) == 1


def test_quote_maximum_and_native_effects(configured):
    kwargs, source, path, record, store, capacity = configured
    fields = {f.name: getattr(record, f.name) for f in dataclasses.fields(record)}
    fields["price"] = PriceEffect("USD", "request", 2, .5)
    record = make_provider_model_offer_receipt(**fields)
    path.write_text(json.dumps(record.to_dict()))
    config = {"capacity_store": store.directory, "maximum_predicted_request_usd": "0.75", "profiles": {"p": {
        "provider": record.provider, "pool_id": record.pool_id, "harness": record.harness,
        "credential_sha256": capacity.credential_sha256, "account_identity": capacity.account_identity, "endpoint_id": "e", "transport_provider": "openai",
        "usage_path": record.usage_path, "chat_url": kwargs["chat_url"], "offer_path": str(path), "source_path": str(source)}}}
    quoted = quote_profile(config, **kwargs, workload=WORK)
    assert quoted["predicted_cash_usd"] == "0.5" and quoted["predicted_savings_usd"] == "1.5"
    assert "credit" in quoted["native_effects"][0]
    config["maximum_predicted_request_usd"] = ".49"
    with pytest.raises(PromotionUnavailable, match="maximum"):
        quote_profile(config, **kwargs, workload=WORK)


def test_discount_order_does_not_cross_tiers_or_add_candidates(configured, monkeypatch):
    import os
    kwargs, source, path, record, store, capacity = configured
    base = json.loads(Path(os.environ["ODYSSEUS_FREE_OFFER_CONFIG"]).read_text())
    base.update(maximum_predicted_request_usd="2", prefer_discounted=True,
                quality_tiers={"higher": "high", "expensive": "same", "p": "same"})
    fields = {f.name: getattr(record, f.name) for f in dataclasses.fields(record)}
    fields["price"] = PriceEffect("USD", "request", 2, 1)
    expensive = path.parent / "expensive.json"
    expensive.write_text(json.dumps(make_provider_model_offer_receipt(**fields).to_dict()))
    base["profiles"]["expensive"] = {**base["profiles"]["p"], "offer_path": str(expensive)}
    path_config = path.parent / "discount-config.json"
    path_config.write_text(json.dumps(base))
    monkeypatch.delenv("ODYSSEUS_FREE_OFFER_CONFIG")
    monkeypatch.setenv("ODYSSEUS_OFFER_CONFIG", str(path_config))
    candidates = [{"profile_id": "higher"}, {"profile_id": "expensive"}, {"profile_id": "p"}]
    result = prefer_discounted(candidates, workload=WORK,
        resolve_candidate=lambda c: (kwargs["model"], kwargs["chat_url"], {k: kwargs[k] for k in ("credential_sha256", "endpoint_id", "transport_provider")}))
    assert [c["profile_id"] for c in result] == ["higher", "p", "expensive"]


def test_optional_offer_refs_roundtrip_and_tampering_invalidates_canonical_evidence(monkeypatch):
    from tests.test_dispatch_boundary import _db, _seed, _resolve, _sealed, _invocation
    from tests.test_ps640_provider_capacity import receipt, prov
    from src import dispatch_boundary as boundary
    from src.execution_package import make_dispatch_receipt
    from src.provider_model_offer import OfferProvenance
    from tests.test_dispatch_boundary import NOW
    now = NOW
    stamp = now.isoformat()
    capacity = receipt(provider="ollama", credential_sha256="a"*64, observed_at=stamp, price=None,
        state_provenance=prov(observed=stamp), entitlement_provenance=prov(observed=stamp), zdr_provenance=prov(observed=stamp))
    record = offer(provider="ollama", pool_id=capacity.pool_id, capacity_receipt_ref=capacity.ref,
        native_model="qwen3.8:27b", harness="ollama", price=PriceEffect("USD", "request", 0, 0),
        valid_from=(now-timedelta(hours=1)).isoformat(), valid_until=(now+timedelta(hours=1)).isoformat(),
        provenance=OfferProvenance("https://example.test", "a"*64, "fixture", stamp, 3600))
    quote = {"profile_id": "p-rtx", "provider": "ollama", "pool_id": capacity.pool_id,
        "model": "qwen3.8:27b", "harness": "ollama", "chat_url": "http://10.0.0.10:11434/v1",
        "usage_path": record.usage_path, "observed_at": stamp, "workload": WORK,
        "credential_sha256": "a" * 64, "endpoint_id": "ep-rtx", "transport_provider": "ollama", "account_identity": capacity.account_identity,
        "predicted_cash_usd": "0", "maximum_predicted_request_usd": "0", "offer_refs": [record.ref],
        "offers": [record.to_dict()], "capacity_receipt_ref": capacity.ref}
    monkeypatch.setattr("src.offer_economics.configured_quote", lambda **kwargs: quote)
    db = _db(); task = _seed(db)
    bound = _resolve(db, task, "p-rtx", capacity_receipts=[capacity], run_id="run-offer")
    assert bound.decision.offer_receipt_refs == (record.ref,)
    canonical = make_dispatch_receipt(**bound.decision.to_ps638_receipt_kwargs())
    assert canonical.receipt_hash == bound.decision.receipt_hash
    boundary.verify_invocation(bound, invocation=_invocation(workload=WORK))
    with pytest.raises(boundary.DispatchPinViolation, match="workload"):
        boundary.verify_invocation(bound, invocation=_invocation(workload={**WORK, "input_tokens": 200_000_000}))
    payload = _sealed(bound)
    assert boundary.validate_dispatch_evidence(payload)[0]
    payload["offer_quotes"][0]["predicted_cash_usd"] = "1"
    payload["offer_quotes"][0]["maximum_predicted_request_usd"] = "2"
    payload["seal"]["evidence_hash"] = boundary._sha256_hex(boundary._canonical(boundary.evidence_core(payload)))
    assert not boundary.validate_dispatch_evidence(payload)[0]
    assert "offer_quote_changed" in boundary.validate_dispatch_evidence(payload)[1]
    db.close()


@pytest.mark.parametrize("authorized,budget_allowed,premium,premium_permission,token_overrun", [
    (True, True, False, False, False), (False, True, False, False, False), (True, False, False, False, False),
    (True, True, True, False, False), (False, True, True, True, False), (True, True, True, True, False),
    (True, True, False, False, True),
    (True, True, False, False, "malformed"), (True, True, False, False, "zero"),
    (True, True, False, False, "postprocess"), (True, True, False, False, "unconfigured"),
])
def test_real_scout_discount_uses_fresh_quote_and_existing_paid_budget_authority(
        configured, monkeypatch, tmp_path, authorized, budget_allowed, premium, premium_permission, token_overrun):
    import os
    import sqlalchemy
    from sqlalchemy.orm import sessionmaker
    from core.database import Base, RoutingTask, RoutingModelProfile, RoutingModelRun, ModelEndpoint
    import src.routing_executor as executor
    kwargs, source, path, record, store, capacity = configured
    config = json.loads(Path(os.environ["ODYSSEUS_FREE_OFFER_CONFIG"]).read_text())
    config["maximum_predicted_request_usd"] = ".75"
    config.update(prefer_discounted=True, quality_tiers={"p": "same"})
    fields = {f.name: getattr(record, f.name) for f in dataclasses.fields(record)}
    fields["price"] = PriceEffect("USD", "request", 100, .5)
    if token_overrun:
        fields["price"] = PriceEffect("USD", "million_input_tokens", 1_000_000, 1_000_000)
        output = path.parent / "output-rate.json"
        output.write_text(json.dumps(make_provider_model_offer_receipt(**{
            **fields, "price": PriceEffect("USD", "million_output_tokens", 0, 0)}).to_dict()))
        config["profiles"]["p"]["offer_paths"] = [str(path), str(output)]
        config["maximum_predicted_request_usd"] = 2
    path.write_text(json.dumps(make_provider_model_offer_receipt(**fields).to_dict()))
    if token_overrun == "unconfigured":
        config["profiles"] = {"other-profile": config["profiles"].pop("p")}
    config_path = tmp_path / "cash-config.json"; config_path.write_text(json.dumps(config))
    monkeypatch.delenv("ODYSSEUS_FREE_OFFER_CONFIG")
    monkeypatch.setenv("ODYSSEUS_OFFER_CONFIG", str(config_path))
    import src.offer_economics as economics
    original_quote = economics.quote_profile
    observed_workloads = []
    def trace_quote(*args, **kwargs):
        observed_workloads.append(dict(kwargs["workload"]))
        return original_quote(*args, **kwargs)
    monkeypatch.setattr(economics, "quote_profile", trace_quote)
    engine = sqlalchemy.create_engine("sqlite:///:memory:"); Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(ModelEndpoint(id="e", name="fixture", base_url=kwargs["chat_url"])); db.commit()
    task = RoutingTask(id="t", title="fixture", objective="fixture", task_type="feature_plan",
                       repo_path=str(tmp_path), allow_paid_models=authorized,
                       allow_premium_models=premium_permission, max_cost_usd=1)
    profile = RoutingModelProfile(id="p", model_endpoint_id="e", model=kwargs["model"], roles="[]",
                                  enabled=True, max_output_tokens=100, is_premium=premium)
    db.add_all([task, profile]); db.commit()
    if token_overrun == "postprocess":
        task.task_type = "implementation"; db.commit()
        def broken_processing(*args):
            raise OSError("fixture post-inference artifact failure")
        monkeypatch.setattr(executor, "extract_diff", broken_processing)
    monkeypatch.setattr(executor, "build_context_bundle", lambda task: {})
    monkeypatch.setattr(executor, "_write_run_manifest", lambda *a: None)
    monkeypatch.setattr(executor, "archive_root", lambda: str(tmp_path / "runs"))
    monkeypatch.setattr(executor, "build_prompt", lambda *a: "prompt")
    monkeypatch.setattr(executor, "check_general_budget", lambda *a: {"allowed": True})
    monkeypatch.setattr(executor, "check_premium_budget", lambda *a: {"allowed": True})
    quotes_checked = []
    def check_budget(db, task, spent, estimate):
        quotes_checked.append(estimate)
        return {"allowed": budget_allowed and spent + estimate <= 1, "reason": "budget"}
    monkeypatch.setattr(executor, "check_task_budget", check_budget)
    monkeypatch.setattr(executor, "resolve_endpoint_by_id", lambda *a: (kwargs["chat_url"], kwargs["model"], {}))
    if token_overrun == "unconfigured":
        monkeypatch.setattr(economics, "configured_quote", lambda **kwargs: None)
    calls = []
    def fake_call(*args, **kwargs):
        calls.append(args)
        if token_overrun == "malformed":
            return "response", {"input_tokens": None, "output_tokens": 2}
        if token_overrun == "zero":
            return "response", {"input_tokens": 0, "output_tokens": 0}
        return "response", {"input_tokens": 5, "output_tokens": 2}
    monkeypatch.setattr(executor, "llm_call_with_usage", fake_call)
    candidates = [{"profile_id": "p", "estimated_cost_usd": 100}] * (2 if token_overrun else 1)
    result = executor.execute_candidates(db, task, candidates, len(candidates))
    if token_overrun == "unconfigured":
        assert not calls and result["spend_total_usd"] == 0
        assert quotes_checked == [100, 100]
        db.close(); engine.dispose()
        return
    if isinstance(token_overrun, str):
        assert result["status"] == "failed" and len(calls) == 1
        assert result["spend_total_usd"] == (5 if token_overrun == "postprocess" else 1)
        failed = db.query(RoutingModelRun).one()
        assert failed.cost_usd == result["spend_total_usd"]
        artifact = json.loads(failed.artifacts)
        assert artifact["inference_completed"] is True and artifact["upstream_error"] is False
    elif (premium_permission if premium else authorized) and budget_allowed:
        assert result["status"] == "succeeded" and len(calls) == 1
        assert result["spend_total_usd"] == (5 if token_overrun else .5)
        assert quotes_checked == ([1, 1] if token_overrun else [.5])
        completed = db.query(RoutingModelRun).filter(RoutingModelRun.completed == True).one()
        artifact = json.loads(completed.artifacts)
        assert artifact["cost_basis"] == "offer_tariff_estimate_not_provider_invoice"
        assert artifact["predicted_ceiling_exceeded"] is token_overrun
    else:
        assert result["status"] == "failed" and not calls
    assert observed_workloads and all(w == {"input_tokens": 1, "output_tokens": 100, "cache_read_tokens": 0, "cache_write_tokens": 0} for w in observed_workloads)
    db.close(); engine.dispose()


def test_cash_dispatch_never_uses_config_sample_workload(configured, monkeypatch, tmp_path):
    from src.offer_economics import configured_quote
    import os
    kwargs = configured[0]
    config = json.loads(Path(os.environ["ODYSSEUS_FREE_OFFER_CONFIG"]).read_text())
    config.update(maximum_predicted_request_usd=".05", workload=WORK)
    path = tmp_path / "cash.json"; path.write_text(json.dumps(config))
    monkeypatch.delenv("ODYSSEUS_FREE_OFFER_CONFIG")
    monkeypatch.setenv("ODYSSEUS_OFFER_CONFIG", str(path))
    with pytest.raises(PromotionUnavailable, match="explicit predicted workload"):
        configured_quote(**kwargs)


def test_same_endpoint_with_different_account_credential_is_refused(configured):
    kwargs = configured[0]
    from src.promotional_dispatch import enforce_free_offer
    from src.offer_economics import credential_fingerprint
    with pytest.raises(PromotionUnavailable, match="credential"):
        enforce_free_offer(**{**kwargs, "credential_sha256": credential_fingerprint({"Authorization": "Bearer different-account"})})
