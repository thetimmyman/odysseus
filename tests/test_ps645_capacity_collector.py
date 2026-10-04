"""Hosted provider-capacity collector for the ChatGPT Subscription pool.

The collector is an observer, not a router: it mints a capacity receipt from
provider facts, persists it through the append-only store, and feeds the
freshest-wins gate. Its failure modes are negative controls:

  * a 429 from the usage endpoint is a FACT (rate_limited + reset), never a
    transport error and never ``None``;
  * ``None`` is returned only when the pool facts cannot be established
    (unknown auth, empty model list, unreadable usage) — and the store must
    then be LEFT UNTOUCHED so the pre-vacancy receipt stays the authority;
  * the bearer identity appears in no receipt, provenance reference, error
    message, or line of ``receipts.jsonl``;
  * an explicit limit (429 / limit_reached / allowed=false) outranks spend
    control, which outranks a depleted window, which outranks ``available``;
  * the entitlement class is ``third_party_harness`` — never ``agent_sdk``,
    which is what turns the entitlement gate off for this pool by design;
  * the freshest receipt wins per pool regardless of state, and only
    in-TTL receipts reach the gate.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import routes.chatgpt_subscription_routes as csr
from core.database import Base, ProviderAuthSession
from src import capacity_collector as cc
from src import provider_capacity as pc
from src.provider_capacity_store import ProviderCapacityStore


def _mem_db(monkeypatch):
    from sqlalchemy.pool import StaticPool

    engine = create_engine("sqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(cc, "SessionLocal", TestSessionLocal)
    monkeypatch.setattr(csr, "SessionLocal", TestSessionLocal)
    return TestSessionLocal


def _jwt(payload: dict) -> str:
    """A three-part bearer whose payload carries the plan claim. Header and
    signature parts are content-only — the collector only parses the payload."""
    import base64

    def part(raw: dict) -> str:
        blob = json.dumps(raw, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")

    material = dict(payload)
    material.setdefault("sub", "user-1")
    material.setdefault("exp", 4102444800)  # year 2100, always in the future
    return f"eyJhbGciOiJub25lIn0.{part(material)}.sig-not-verified"


BEARER = _jwt({"chatgpt_plan_type": "plus"})

PRIMARY = {"used_percent": 20.0, "reset_at": "2027-01-01T00:00:00+00:00"}
SECONDARY = {"used_percent": 3.0, "reset_at": "2027-06-01T00:00:00+00:00"}


def usage_body(**overrides) -> dict:
    fields = {
        "rate_limit": {
            "primary_window": dict(PRIMARY),
            "secondary_window": dict(SECONDARY),
            "allowed": True,
            "limit_reached": False,
        },
        "credits": {"has_credits": True, "unlimited": False, "balance": 0.0},
        "spend_control": {"reached": False, "individual_limit": 0.0},
    }
    fields.update(overrides)
    return fields


MODELS = ["gpt-5.4", "gpt-5.5"]


def _auth(db, *, owner: str = "sam@example.com") -> str:
    """Create the auth row; the bearer is a module constant, never a literal
    that could land in a log or diff as a secret."""
    row = ProviderAuthSession(
        id="auth1",
        provider="chatgpt_subscription",
        owner=owner,
        base_url="https://chatgpt.com/backend-api/codex",
        access_token=BEARER,
        refresh_token="r" + "f" * 24 + "r",
        auth_mode="chatgpt",
    )
    db.add(row)
    db.commit()
    return "auth1"


def _collect_via(db, monkeypatch, usage) -> pc.ProviderCapacityReceipt | None:
    """Collect with the provider facts injected — no network in the suite."""
    monkeypatch.setattr(
        cc.cgs, "fetch_available_models", lambda token, *a, **k: list(MODELS)
    )
    return cc.collect("auth1", usage=usage)


def _collect_live(db, monkeypatch, fetch_usage) -> pc.ProviderCapacityReceipt | None:
    """Collect with the USAGE FETCH injected (429 / transport mapping)."""
    monkeypatch.setattr(
        cc.cgs, "fetch_available_models", lambda token, *a, **k: list(MODELS)
    )
    monkeypatch.setattr(cc.cgs, "fetch_codex_usage", fetch_usage)
    return cc.collect("auth1")


def test_pool_identity_is_session_scoped():
    assert cc.hosted_pool_id("auth1") == "chatgpt-subscription:session:auth1"
    assert cc.account_identity_for("sam@example.com", "auth1") == "sam@example.com"
    assert cc.account_identity_for(None, "auth1") == "auth:auth1"
    assert cc.account_identity_for("  ", "auth9") == "auth:auth9"


def test_available_receipt_carries_windows_plan_and_no_bearer(monkeypatch):
    SessionLocal = _mem_db(monkeypatch)
    db = SessionLocal()
    try:
        _auth(db)
    finally:
        db.close()
    receipt = _collect_via(db, monkeypatch, usage_body())
    assert receipt is not None
    assert receipt.provider == "chatgpt_subscription"
    assert receipt.pool_id == "chatgpt-subscription:session:auth1"
    assert receipt.account_identity == "sam@example.com"
    assert receipt.authorization_class is pc.AuthorizationClass.OAUTH_CLI
    # A third-party harness, not the Agent SDK, so the runtime gate stays OFF.
    assert receipt.entitlement is pc.Entitlement.THIRD_PARTY_HARNESS
    assert receipt.state is pc.CapacityState.AVAILABLE
    assert tuple(q.name for q in receipt.quotas) == ("primary", "secondary")
    primary, secondary = receipt.quotas
    assert (primary.limit, primary.remaining) == (100.0, 80.0)
    assert (secondary.limit, secondary.remaining) == (100.0, 97.0)
    assert tuple(receipt.exposed_models) == tuple(MODELS)
    # The plan ships in the pricing version; the bearer ships nowhere.
    assert receipt.price is not None
    assert receipt.price.cost_class is pc.CostClass.SUBSCRIPTION_SUNK_COST
    assert receipt.price.pricing_source is pc.PricingSource.SUBSCRIPTION_CONTRACT
    assert receipt.price.pricing_version == "plus"
    payload = json.dumps(receipt.to_dict())
    assert BEARER not in payload
    assert "jwt-bearer" not in payload
    assert "wham/usage" in payload


def test_429_is_a_fact_not_a_failure(monkeypatch):
    SessionLocal = _mem_db(monkeypatch)
    db = SessionLocal()
    try:
        _auth(db)
    finally:
        db.close()

    def _fetch(token: str, **_k):
        exc = cc.cgs.ChatGPTSubscriptionRateLimited("pool is rate limited")
        exc.reset_at = "2027-01-01T00:00:00+00:00"
        raise exc

    receipt = _collect_live(db, monkeypatch, _fetch)
    assert receipt is not None
    assert receipt.state is pc.CapacityState.RATE_LIMITED
    assert receipt.rate_limit is not None
    assert receipt.rate_limit.limit == 100.0
    assert receipt.rate_limit.remaining == 0.0
    assert receipt.rate_limit.reset_at == "2027-01-01T00:00:00+00:00"


def test_limit_reached_in_body_maps_to_rate_limited(monkeypatch):
    SessionLocal = _mem_db(monkeypatch)
    db = SessionLocal()
    try:
        _auth(db)
    finally:
        db.close()
    body = usage_body(
        rate_limit={
            "primary_window": dict(PRIMARY),
            "secondary_window": dict(SECONDARY),
            "limit_reached": True,
        }
    )
    receipt = _collect_via(db, monkeypatch, body)
    assert receipt is not None
    assert receipt.state is pc.CapacityState.RATE_LIMITED
    assert receipt.rate_limit is not None and receipt.rate_limit.remaining == 0.0


def test_spend_control_reached_maps_to_exhausted(monkeypatch):
    SessionLocal = _mem_db(monkeypatch)
    db = SessionLocal()
    try:
        _auth(db)
    finally:
        db.close()
    body = usage_body(spend_control={"reached": True, "individual_limit": 10.0})
    receipt = _collect_via(db, monkeypatch, body)
    assert receipt is not None
    assert receipt.state is pc.CapacityState.EXHAUSTED


def test_depleted_window_maps_to_rate_limited_with_window_reset(monkeypatch):
    SessionLocal = _mem_db(monkeypatch)
    db = SessionLocal()
    try:
        _auth(db)
    finally:
        db.close()
    body = usage_body(
        rate_limit={
            "primary_window": {
                "used_percent": 100.0,
                "reset_at": "2027-03-01T00:00:00+00:00",
            },
            "secondary_window": dict(SECONDARY),
        }
    )
    receipt = _collect_via(db, monkeypatch, body)
    assert receipt is not None
    assert receipt.state is pc.CapacityState.RATE_LIMITED
    assert receipt.rate_limit is not None
    assert receipt.rate_limit.remaining == 0.0
    assert receipt.rate_limit.reset_at == "2027-03-01T00:00:00+00:00"


def test_strict_booleans_only(monkeypatch):
    SessionLocal = _mem_db(monkeypatch)
    db = SessionLocal()
    try:
        _auth(db)
    finally:
        db.close()
    # Provider "1"/"true" never masks as booleans — the fact stays as read.
    body = usage_body(
        rate_limit={
            "primary_window": dict(PRIMARY),
            "secondary_window": dict(SECONDARY),
            "allowed": True,
            "limit_reached": "true",
        }
    )
    receipt = _collect_via(db, monkeypatch, body)
    assert receipt is not None
    assert receipt.state is pc.CapacityState.AVAILABLE
    assert receipt.rate_limit is None


def test_unknown_auth_returns_none(monkeypatch):
    _mem_db(monkeypatch)
    assert cc.collect("no-such-auth") is None


def test_empty_model_list_returns_none(monkeypatch):
    SessionLocal = _mem_db(monkeypatch)
    db = SessionLocal()
    try:
        _auth(db)
    finally:
        db.close()
    monkeypatch.setattr(cc.cgs, "fetch_available_models", lambda token, **k: [])
    assert cc.collect("auth1") is None


def test_usage_transport_failure_is_not_a_pool_fact(monkeypatch):
    SessionLocal = _mem_db(monkeypatch)
    db = SessionLocal()
    try:
        _auth(db)
    finally:
        db.close()

    def _fetch(token: str, **_k):
        raise RuntimeError("offline")

    assert _collect_live(db, monkeypatch, _fetch) is None


def test_sync_appends_and_supersedes_through_the_store(monkeypatch, tmp_path):
    SessionLocal = _mem_db(monkeypatch)
    db = SessionLocal()
    try:
        _auth(db)
    finally:
        db.close()
    monkeypatch.setattr(
        cc.cgs, "fetch_available_models", lambda token, **k: list(MODELS)
    )
    monkeypatch.setattr(cc.cgs, "fetch_codex_usage", lambda token, **k: usage_body())

    store = ProviderCapacityStore(str(tmp_path / "store"))
    first = cc.sync_capacity_store(store, "auth1")
    assert first

    reset_at = (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
    ).isoformat()
    changed = dict(usage_body())
    changed["rate_limit"] = {
        "primary_window": {"used_percent": 55.0, "reset_at": reset_at},
        "secondary_window": dict(SECONDARY),
    }
    monkeypatch.setattr(cc.cgs, "fetch_codex_usage", lambda token, **k: changed)
    second = cc.sync_capacity_store(store, "auth1")
    assert second and second != first

    entries = store.entries()
    assert len(entries) == 2
    assert entries[1].supersedes == first  # the chain is explicit, not implicit
    assert store.verify() is True
    current = store.current("chatgpt-subscription:session:auth1")
    assert current is not None
    assert current.receipt_hash == second

    # The bearer survives on the auth row but never in the persisted store.
    raw = Path(store.receipts_path).read_text(encoding="utf-8")
    assert BEARER not in raw
    assert "sig-not-verified" not in raw


def test_sync_failure_leaves_store_untouched(monkeypatch, tmp_path):
    SessionLocal = _mem_db(monkeypatch)
    db = SessionLocal()
    try:
        _auth(db)
    finally:
        db.close()
    monkeypatch.setattr(
        cc.cgs, "fetch_available_models", lambda token, **k: list(MODELS)
    )

    def _fetch(token: str, **_k):
        raise RuntimeError("offline")

    monkeypatch.setattr(cc.cgs, "fetch_codex_usage", _fetch)
    store = ProviderCapacityStore(str(tmp_path / "store"))
    assert cc.sync_capacity_store(store, "auth1") is None
    assert store.entries() == ()
    assert not Path(store.index_path).exists()


def test_store_env_override(monkeypatch, tmp_path):
    import src.provider_capacity_store as pcs

    monkeypatch.setenv(pcs.STORE_ENV, str(tmp_path / "override"))
    store = cc.store_from_env()
    assert store.directory in (
        str(tmp_path / "override"),
        str(Path(tmp_path / "override").resolve()),
    )
    monkeypatch.delenv(pcs.STORE_ENV, raising=False)
    assert cc.store_from_env().directory


def test_freshest_wins_per_pool_and_gate_sees_only_fresh(monkeypatch, tmp_path):
    SessionLocal = _mem_db(monkeypatch)
    db = SessionLocal()
    try:
        _auth(db)
    finally:
        db.close()
    monkeypatch.setattr(
        cc.cgs, "fetch_available_models", lambda token, **k: list(MODELS)
    )
    monkeypatch.setattr(cc.cgs, "fetch_codex_usage", lambda token, **k: usage_body())
    store = ProviderCapacityStore(str(tmp_path / "store"))
    pool = cc.hosted_pool_id("auth1")

    # A newer rate-limited report supersedes an available one: state does not
    # pick the winner, recency does.
    cc.sync_capacity_store(store, "auth1")
    limited = dict(usage_body())
    limited["rate_limit"] = {
        "primary_window": dict(PRIMARY),
        "secondary_window": dict(SECONDARY),
        "limit_reached": True,
    }
    monkeypatch.setattr(cc.cgs, "fetch_codex_usage", lambda token, **k: limited)
    import time

    time.sleep(1.1)  # a strictly later observed_at
    cc.sync_capacity_store(store, "auth1")

    best = cc.fresh_hosted_capacity_receipts(store)
    assert set(best) == {pool}
    winner = best[pool]
    assert winner.state is pc.CapacityState.RATE_LIMITED
    assert winner.rate_limit is not None and winner.rate_limit.remaining == 0.0

    # Stale (past-TTL) receipt: the gate sees NO facts for the pool.
    stale_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        seconds=cc.DEF_TTL_SECONDS + 1
    )
    assert cc.fresh_hosted_capacity_receipts(store, now=stale_now) == {}


def test_collect_receipts_drive_the_real_hosted_gate(monkeypatch):
    """Dispatch's hosted gate consumes collector-minted
    receipts. A fresh available third-party pool remains refused by entitlement
    policy; stale / missing / rate-limited pools carry their documented reasons."""
    import datetime as dt

    from src import dispatch_routing as dr

    SessionLocal = _mem_db(monkeypatch)
    db = SessionLocal()
    try:
        _auth(db)
    finally:
        db.close()
    monkeypatch.setattr(
        cc.cgs, "fetch_available_models", lambda token, **k: list(MODELS)
    )
    monkeypatch.setattr(cc.cgs, "fetch_codex_usage", lambda token, **k: usage_body())
    profile = dr.ExecutionTargetProfile(
        target_id="tg-1",
        profile_id="prof-1",
        provider="chatgpt_subscription",
        model="gpt-5.5",
        locality=dr.LOCALITY_HOSTED,
    )
    from dataclasses import replace
    receipts = [pc.make_capacity_receipt(**{**cc.collect("auth1").core(), "endpoint_url": "https://chatgpt.com/backend-api/codex/responses"})]
    profile = replace(profile, endpoint_url=receipts[0].endpoint_url, credential_sha256=receipts[0].credential_sha256)
    assert receipts[0] is not None and receipts[0].state is pc.CapacityState.AVAILABLE

    # observed capacity stays reportable, but third-party entitlement is not a policy grant.
    ok, _refs, rule, _why = dr.classify_capacity_for(profile, receipts)
    assert ok is False and rule == dr.REFUSED_CAPACITY_UNUSABLE

    # no receipts at all -> missing (fail closed).
    ok, _refs, rule, _why = dr.classify_capacity_for(profile, ())
    assert ok is False and rule == dr.REFUSED_CAPACITY_MISSING

    # a receipt that has aged past its TTL -> stale, never selected.
    stale_now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        seconds=cc.DEF_TTL_SECONDS + 1
    )
    ok, _refs, rule, _why = dr.classify_capacity_for(profile, receipts, now=stale_now)
    assert ok is False and rule == dr.REFUSED_CAPACITY_STALE

    # a genuinely rate-limited pool stays fresh but unusable.
    limited = dict(usage_body())
    limited["rate_limit"] = {
        "primary_window": dict(PRIMARY),
        "secondary_window": dict(SECONDARY),
        "limit_reached": True,
    }
    rl = _collect_via(db, monkeypatch, limited)
    rl = pc.make_capacity_receipt(**{**rl.core(), "endpoint_url": profile.endpoint_url})
    ok, _refs, rule, _why = dr.classify_capacity_for(profile, [rl])
    assert ok is False and rule == dr.REFUSED_CAPACITY_UNUSABLE

    # a profile for a model the pool never exposed -> missing, not stale.
    other = dr.ExecutionTargetProfile(
        target_id="tg-2",
        profile_id="prof-2",
        provider="chatgpt_subscription",
        model="gpt-9.9",
        locality=dr.LOCALITY_HOSTED,
    )
    ok, _refs, rule, _why = dr.classify_capacity_for(other, receipts)
    assert ok is False and rule == dr.REFUSED_CAPACITY_MISSING


def test_endpoint_capacity_binds_live_query_credentials(monkeypatch, tmp_path):
    import pytest
    from core.database import ModelEndpoint
    from src.offer_economics import credential_fingerprint
    sessions = _mem_db(monkeypatch)
    db = sessions()
    _auth(db)
    db.add(ModelEndpoint(id="bound-endpoint", name="bound", base_url="https://chatgpt.com/backend-api/codex", provider_auth_id="auth1"))
    db.commit(); db.close()
    queried = []
    def usage(token, **kwargs):
        queried.append(token)
        return usage_body()
    monkeypatch.setattr(cc.cgs, "fetch_available_models", lambda token, **k: MODELS)
    monkeypatch.setattr(cc.cgs, "fetch_codex_usage", usage)
    headers = cc.cgs.chatgpt_headers(BEARER)
    monkeypatch.setattr("src.endpoint_resolver.resolve_endpoint_by_id", lambda *a: ("https://chatgpt.com/backend-api/codex/responses", MODELS[0], headers))
    store = ProviderCapacityStore(tmp_path / "capacity")
    receipt = cc.collect_endpoint_capacity(store, "bound-endpoint", MODELS[0])
    assert queried == [BEARER]
    assert receipt.credential_sha256 == credential_fingerprint(headers)
    before = list(store.entries())
    headers["Authorization"] = "Bearer rotated-test-token"
    with pytest.raises(ValueError, match="credentials changed"):
        cc.collect_endpoint_capacity(store, "bound-endpoint", MODELS[0])
    assert list(store.entries()) == before


def test_unknown_usage_and_nonfinite_windows_never_authorize(monkeypatch):
    SessionLocal = _mem_db(monkeypatch)
    with SessionLocal() as db:
        _auth(db)
        for payload in ({}, {"error": {"message": "denied"}}, {"rate_limit": {}},
                        *({"rate_limit": {"primary_window": {"used_percent": n}}}
                          for n in (float("nan"), float("inf"), float("-inf"), 10**1000))):
            receipt = _collect_via(db, monkeypatch, payload)
            assert receipt.state == pc.CapacityState.UNKNOWN
            assert all(q.remaining == pc.UNKNOWN for q in receipt.quotas)
            assert not receipt.has_usable_capacity_facts()


def test_hosted_capacity_missing_index_is_not_recovered_from_history(monkeypatch, tmp_path):
    SessionLocal = _mem_db(monkeypatch)
    with SessionLocal() as db:
        _auth(db)
        receipt = _collect_via(db, monkeypatch, usage_body())
    store = ProviderCapacityStore(str(tmp_path))
    store.append(receipt)
    assert cc.fresh_hosted_capacity_receipts(store)
    Path(store.index_path).unlink()
    assert cc.fresh_hosted_capacity_receipts(store) == {}


def test_caller_cannot_relabel_capacity_account(monkeypatch):
    SessionLocal = _mem_db(monkeypatch)
    with SessionLocal() as db:
        _auth(db, owner="actual@example.test")
    monkeypatch.setattr(cc.cgs, "fetch_available_models", lambda *a, **k: (_ for _ in ()).throw(AssertionError("mismatch must not query")))
    assert cc.collect("auth1", owner="other@example.test", usage=usage_body(), models=MODELS) is None
    receipt = cc.collect("auth1", owner=" Actual@Example.Test ", usage=usage_body(), models=MODELS)
    assert receipt.account_identity == "actual@example.test"
