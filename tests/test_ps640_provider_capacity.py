from datetime import datetime, timezone, timedelta
import json
import multiprocessing
import os
import fcntl
from dataclasses import replace

import pytest

from src.provider_capacity import *
from src.provider_capacity import _sha256
from src.provider_capacity_store import CapacityStoreBusyError, CapacityStoreError, ProviderCapacityStore


NOW = "2026-09-15T12:00:00+00:00"
AT_NOW = datetime.fromisoformat(NOW)


def _append_from_child(directory, observed_at):
    ProviderCapacityStore(directory).append(receipt(observed_at=observed_at))


def prov(source="fixture", ref="fixture-1", ttl=3600, observed=NOW):
    return EvidenceProvenance(source, ref, "test-collector", observed, ttl)


def receipt(**overrides):
    data = dict(provider="local", pool_id="local:sim-gpu", account_identity="host:gpu-host",
                authorization_class=AuthorizationClass.LOCAL_ENDPOINT, entitlement=Entitlement.LOCAL,
                exposed_models=("qwen3.8:27b",), observed_at=NOW, ttl_seconds=300,
                collector_id="test-collector", evidence_source="fixture", evidence_reference="local-1",
                state=CapacityState.AVAILABLE,
                state_provenance=prov(), entitlement_provenance=prov(), zdr_provenance=prov(), quotas=(),
                price=PriceObservation(CostClass.LOCAL_ZERO_MARGINAL_DOLLARS,
                                       published_list_rate=0, estimated_marginal_cost=0,
                                       actual_billed_cost=UNKNOWN, pricing_source=PricingSource.OPERATOR_OVERRIDE,
                                       pricing_version="fixture-v1", provenance=prov(ttl=86400)),
                concurrency_limit=4, concurrency_remaining=4, zdr_supported=True)
    data.update(overrides)
    return make_capacity_receipt(**data)


def test_three_shaped_fixtures_are_independent_and_typed():
    local = receipt()
    subscription = receipt(provider="clinepass", pool_id="clinepass:subscription:primary",
                           account_identity="subscription:primary", authorization_class=AuthorizationClass.OAUTH_CLI,
                           entitlement=Entitlement.THIRD_PARTY_HARNESS,
                           quotas=(QuotaDimension("five_hour", "requests", 100, 73, "2026-09-15T16:00:00+00:00", prov()),),
                           price=PriceObservation(CostClass.SUBSCRIPTION_SUNK_COST,
                               actual_billed_cost=UNKNOWN, pricing_source=PricingSource.SUBSCRIPTION_CONTRACT,
                               pricing_version="contract-2026-09", provenance=prov(ttl=86400)))
    metered = receipt(provider="openrouter", pool_id="openrouter:api:primary",
                      account_identity="api-account:primary", authorization_class=AuthorizationClass.API_KEY,
                      entitlement=Entitlement.API,
                      quotas=(QuotaDimension("monthly", "USD", 20, 18.5, UNKNOWN, prov()),
                              QuotaDimension("requests", "requests", UNKNOWN, UNKNOWN, UNKNOWN, prov())),
                      price=PriceObservation(CostClass.METERED, published_list_rate=0.14,
                              estimated_marginal_cost=0.14, actual_billed_cost=UNKNOWN,
                              pricing_source=PricingSource.PRICING_PAGE_SNAPSHOT,
                              pricing_version="snapshot-2026-09-01", provenance=prov(ttl=86400)))
    assert local.has_usable_capacity_facts(now=AT_NOW)
    assert subscription.has_usable_capacity_facts(now=AT_NOW)
    assert metered.has_usable_capacity_facts(now=AT_NOW)
    assert len({local.pool_id, subscription.pool_id, metered.pool_id}) == 3
    assert local.price.actual_billed_cost == UNKNOWN


def test_unknown_entitlement_and_expiry_fail_closed():
    assert not receipt(entitlement=Entitlement.UNKNOWN).has_usable_capacity_facts(now=AT_NOW)
    assert not receipt(entitlement=Entitlement.INTERACTIVE_NATIVE).has_usable_capacity_facts(now=AT_NOW)
    assert not receipt(observed_at="2026-09-15T11:00:00+00:00", ttl_seconds=1).has_usable_capacity_facts(now=AT_NOW)


def test_unknown_quota_is_not_zero_or_unlimited():
    q = QuotaDimension("daily", "tokens", provenance=prov())
    assert q.remaining == UNKNOWN
    assert q.to_dict()["remaining"] == {"status": "unknown"}


def test_invalid_negative_and_reset_evidence_rejected():
    with pytest.raises(CapacityError): QuotaDimension("daily", "requests", remaining=-1)
    with pytest.raises(CapacityError): QuotaDimension("daily", "requests", reset_at="not-a-date")
    with pytest.raises(CapacityError): QuotaDimension("daily", "requests", reset_at="2026-09-15T11:59:00+00:00", provenance=prov())


def test_local_zero_cost_still_fails_when_concurrency_exhausted():
    assert not receipt(concurrency_remaining=0).is_operationally_available(now=AT_NOW)


def test_zdr_unknown_does_not_satisfy_policy_fact_requirement():
    assert receipt(zdr_supported=UNKNOWN).is_operationally_available(now=AT_NOW)
    assert receipt(zdr_supported=False).is_operationally_available(now=AT_NOW)


def test_stale_price_is_not_eligible():
    stale = PriceObservation(CostClass.METERED, published_list_rate=1,
                             pricing_source=PricingSource.PROVIDER_API,
                             pricing_version="old", provenance=prov(observed="2026-09-14T12:00:00+00:00", ttl=60))
    assert not receipt(price=stale).is_operationally_available(now=AT_NOW)


def test_known_price_requires_freshness_provenance_and_version():
    with pytest.raises(CapacityError):
        PriceObservation(CostClass.METERED, published_list_rate=1,
                         pricing_source=PricingSource.PROVIDER_API)


def test_round_trip_hash_integrity_and_append_only_supersession(tmp_path):
    store = ProviderCapacityStore(str(tmp_path))
    first = receipt(); store.append(first)
    second = receipt(observed_at="2026-09-15T12:01:00+00:00")
    store.append(second, supersedes=first.receipt_hash)
    assert len(store.entries()) == 2
    current = store.current(first.pool_id)
    assert current.receipt_hash != second.receipt_hash
    assert current.supersedes == first.receipt_hash
    assert capacity_receipt_from_dict(current.to_dict()).receipt_hash == current.receipt_hash
    assert store.verify()
    payload = first.to_dict(); payload["state"] = "exhausted"
    with open(store.receipts_path, "w", encoding="utf-8") as handle:
        handle.write(__import__("json").dumps(payload) + "\n")
    with pytest.raises(Exception): store.entries()


def test_registry_is_facts_only_and_keeps_separate_same_model_pools():
    a = receipt(pool_id="provider-a:pool")
    b = receipt(provider="provider-b", pool_id="provider-b:pool")
    candidates = CapacityRegistry((b, a)).eligible_capacity_for("qwen3.8:27b", now=AT_NOW)
    assert [r.pool_id for r in candidates] == ["provider-a:pool", "provider-b:pool"]
    assert not hasattr(CapacityRegistry, "choose_best_model")
    assert candidates[0].ref.startswith("capacity:")


def test_identity_aliases_are_rejected_instead_of_hashing_as_new_pools():
    with pytest.raises(CapacityError):
        receipt(pool_id=" local:sim-gpu")
    with pytest.raises(CapacityError):
        receipt(account_identity="host:gpu-host ")
    with pytest.raises(CapacityError):
        receipt(provider="provider name")


@pytest.mark.parametrize("field", ["state_provenance", "entitlement_provenance", "zdr_provenance"])
def test_each_dynamic_receipt_fact_has_independent_freshness(field):
    stale = prov(observed="2026-09-15T11:00:00+00:00", ttl=1)
    assert not receipt(**{field: stale}).is_operationally_available(now=AT_NOW)


def test_stale_quota_fact_cannot_drive_availability():
    stale = QuotaDimension("daily", "requests", 100, 90,
                           "2026-09-16T00:00:00+00:00",
                           prov(observed="2026-09-15T11:00:00+00:00", ttl=1))
    assert not receipt(quotas=(stale,)).is_operationally_available(now=AT_NOW)


@pytest.mark.parametrize("name,unit", [
    ("rolling", "requests"), ("five_hour", "tokens"), ("daily", "credits"),
    ("weekly", "USD"), ("monthly", "requests"), ("concurrency", "requests"),
    ("rate", "requests"),
])
def test_arbitrary_quota_dimensions_remain_optional(name, unit):
    q = QuotaDimension(name, unit, 10, 5, UNKNOWN, prov())
    assert q.remaining == 5


def test_quota_rejects_negative_limit_over_limit_remaining_and_booleans():
    with pytest.raises(CapacityError): QuotaDimension("x", "requests", -1, UNKNOWN, provenance=prov())
    with pytest.raises(CapacityError): QuotaDimension("x", "requests", 1, 2, provenance=prov())
    with pytest.raises(CapacityError): QuotaDimension("x", "requests", True, UNKNOWN, provenance=prov())
    with pytest.raises(CapacityError): QuotaDimension("x", "requests", 1, False, provenance=prov())


def test_known_estimate_and_actual_billing_require_provenance_and_actual_source():
    with pytest.raises(CapacityError): PriceObservation(CostClass.METERED, estimated_marginal_cost=1)
    with pytest.raises(CapacityError):
        PriceObservation(CostClass.METERED, actual_billed_cost=1, pricing_source=PricingSource.OPERATOR_OVERRIDE,
                         pricing_version="v1", provenance=prov())


def test_zdr_is_a_fact_not_a_policy_argument():
    assert receipt(zdr_supported=UNKNOWN).is_operationally_available(now=AT_NOW)
    with pytest.raises(TypeError):
        CapacityRegistry((receipt(),)).eligible_capacity_for("qwen3.8:27b", now=AT_NOW, require_zdr=True)


def test_superseded_history_is_retained_but_registry_uses_only_current(tmp_path):
    store = ProviderCapacityStore(str(tmp_path))
    first = store.append(receipt())
    second = store.append(receipt(observed_at="2026-09-15T12:01:00+00:00"), supersedes=first.receipt_hash)
    assert len(store.history(first.pool_id)) == 2
    assert store.current(first.pool_id).receipt_hash == second.receipt_hash
    assert len(CapacityRegistry(store.history()).eligible_capacity_for("qwen3.8:27b", now=AT_NOW)) == 1


def test_supersession_requires_current_same_pool_existing_target(tmp_path):
    store = ProviderCapacityStore(str(tmp_path))
    first = store.append(receipt())
    other = receipt(pool_id="other:pool")
    with pytest.raises(CapacityStoreError): store.append(other, supersedes=first.receipt_hash)
    with pytest.raises(CapacityStoreError): store.append(receipt(observed_at="2026-09-15T12:01:00+00:00"), supersedes="missing")
    second = store.append(receipt(observed_at="2026-09-15T12:01:00+00:00"), supersedes=first.receipt_hash)
    with pytest.raises(CapacityStoreError): store.append(receipt(observed_at="2026-09-15T12:02:00+00:00"), supersedes=first.receipt_hash)
    with pytest.raises(CapacityStoreError): store.append(second, supersedes=second.receipt_hash)


def test_missing_or_corrupt_current_index_fails_closed(tmp_path):
    store = ProviderCapacityStore(str(tmp_path))
    saved = store.append(receipt())
    os.unlink(store.index_path)
    with pytest.raises(CapacityStoreError): store.current(saved.pool_id)


def test_index_hash_and_pool_mismatch_fail_closed(tmp_path):
    store = ProviderCapacityStore(str(tmp_path))
    saved = store.append(receipt())
    with open(store.index_path, "w", encoding="utf-8") as handle:
        json.dump({"wrong:pool": {"pool_id": "wrong:pool", "receipt_hash": saved.receipt_hash}}, handle)
    with pytest.raises(CapacityStoreError): store.current(saved.pool_id)
    with open(store.index_path, "w", encoding="utf-8") as handle:
        json.dump({saved.pool_id: {"pool_id": saved.pool_id, "receipt_hash": "missing"}}, handle)
    with pytest.raises(CapacityStoreError): store.current(saved.pool_id)


def test_torn_index_and_conflicting_active_history_fail_closed(tmp_path):
    store = ProviderCapacityStore(str(tmp_path))
    first = receipt()
    second = receipt(provider="other", pool_id="other:pool")
    store._append_line(first)
    store._append_line(second)
    with open(store.index_path, "w", encoding="utf-8") as handle:
        handle.write("{\"local:sim-gpu\":")
    with pytest.raises(CapacityStoreError): store.current(first.pool_id)
    with open(store.index_path, "w", encoding="utf-8") as handle:
        json.dump({first.pool_id: {"pool_id": first.pool_id, "receipt_hash": first.receipt_hash},
                   second.pool_id: {"pool_id": second.pool_id, "receipt_hash": second.receipt_hash}}, handle)
    # The unindexed same-pool row is an orphan and cannot replace the indexed
    # authority.
    conflicting = receipt(observed_at="2026-09-15T12:01:00+00:00")
    store._append_line(conflicting)
    assert store.current(first.pool_id).receipt_hash == first.receipt_hash


def test_invalidation_is_append_only_and_not_eligible(tmp_path):
    store = ProviderCapacityStore(str(tmp_path))
    saved = store.append(receipt())
    invalidated = store.invalidate(saved.receipt_hash, "operator revoked", prov(ref="invalidate-1"))
    assert len(store.history(saved.pool_id)) == 2
    assert store.current(saved.pool_id) is None


def test_invalidation_preserves_history_allows_replacement_and_rejects_repeat(tmp_path):
    store = ProviderCapacityStore(str(tmp_path))
    saved = store.append(receipt())
    event = store.invalidate(saved.receipt_hash, "operator revoked", prov(ref="invalidate-1"))
    with pytest.raises(CapacityStoreError):
        store.invalidate(saved.receipt_hash, "repeat", prov(ref="invalidate-2"))
    replacement = store.append(receipt(observed_at="2026-09-15T12:02:00+00:00"))
    assert store.current(saved.pool_id).receipt_hash == replacement.receipt_hash
    assert event in store.history(saved.pool_id)


def test_invalidation_rejects_explicit_cross_pool_target(tmp_path):
    store = ProviderCapacityStore(str(tmp_path))
    saved = store.append(receipt())
    with pytest.raises(CapacityStoreError):
        store.invalidate(saved.receipt_hash, "wrong pool", prov(ref="invalidate-cross"), pool_id="other:pool")


def test_mutation_lock_failure_is_typed_and_no_unlocked_write_occurs(tmp_path):
    store = ProviderCapacityStore(str(tmp_path), lock_timeout_seconds=0)
    os.makedirs(store.directory, exist_ok=True)
    with open(store.lock_path, "a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        with pytest.raises(CapacityStoreBusyError):
            store.append(receipt())
    assert not os.path.exists(store.receipts_path)


def test_concurrent_same_pool_writers_have_one_authoritative_winner(tmp_path):
    context = multiprocessing.get_context("fork")
    processes = [context.Process(target=_append_from_child,
                                 args=(str(tmp_path), f"2026-09-15T12:0{index}:00+00:00"))
                 for index in (1, 2)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=10)
    assert all(not process.is_alive() for process in processes)
    assert sorted(process.exitcode for process in processes) == [0, 1]
    store = ProviderCapacityStore(str(tmp_path))
    assert store.current("local:sim-gpu") is not None
    assert store.verify()


def test_indexed_authority_survives_unindexed_orphan_row(tmp_path):
    store = ProviderCapacityStore(str(tmp_path))
    first = store.append(receipt())
    orphan = receipt(observed_at="2026-09-15T12:03:00+00:00")
    store._append_line(orphan)
    assert store.current(first.pool_id).receipt_hash == first.receipt_hash
    assert len(store.history(first.pool_id)) == 2
    assert len(CapacityRegistry((store.current(first.pool_id),)).eligible_capacity_for("qwen3.8:27b", now=AT_NOW)) == 1


def test_crash_window_uncommitted_successor_fails_closed(tmp_path):
    """A current; B supersedes A but the index was never advanced (interrupted
    commit). Authority is ambiguous and every read surface must refuse.
    """
    store = ProviderCapacityStore(str(tmp_path))
    first = store.append(receipt())
    successor = receipt(observed_at="2026-09-15T12:03:00+00:00")
    core = {**successor.core(), "supersedes": first.receipt_hash}
    store._append_line(replace(successor, supersedes=first.receipt_hash,
                               receipt_hash=_sha256(core)))
    with pytest.raises(CapacityStoreError):
        store.current(first.pool_id)
    with pytest.raises(CapacityStoreError):
        store.authoritative_current_receipts()
    assert store.verify() is False
    # history(bytes) retains both rows; nothing is auto-promoted.
    assert len(store.history(first.pool_id)) == 2


def test_crash_window_registry_exposes_no_eligible_capacity(tmp_path):
    """The same ambiguous state must not yield capacity through CapacityRegistry.

    The registry is fed only the store's canonical authority seam; that seam
    refuses the ambiguous state, so no eligible capacity fact is reachable."""
    store = ProviderCapacityStore(str(tmp_path))
    first = store.append(receipt())
    successor = receipt(observed_at="2026-09-15T12:03:00+00:00")
    core = {**successor.core(), "supersedes": first.receipt_hash}
    store._append_line(replace(successor, supersedes=first.receipt_hash,
                               receipt_hash=_sha256(core)))
    with pytest.raises(CapacityStoreError):
        CapacityRegistry(store.authoritative_current_receipts())


def test_explicit_recovery_commits_orphan_successor(tmp_path):
    """Admin recovery supersedes the durable leaf, committing the interrupted
    history and restoring a single authoritative current. Recovery is explicit:
    nothing auto-promotes the orphan."""
    store = ProviderCapacityStore(str(tmp_path))
    first = store.append(receipt())
    successor = receipt(observed_at="2026-09-15T12:03:00+00:00")
    core = {**successor.core(), "supersedes": first.receipt_hash}
    durable = replace(successor, supersedes=first.receipt_hash,
                      receipt_hash=_sha256(core))
    store._append_line(durable)
    with pytest.raises(CapacityStoreError):
        store.current(first.pool_id)
    # Trying to supersede the stale indexed head is refused: it would leave the
    # durable successor uncommitted and still ambiguous.
    with pytest.raises(CapacityStoreError):
        store.append(receipt(observed_at="2026-09-15T12:20:00+00:00"),
                              supersedes=first.receipt_hash)
    # Explicit recovery: supersede the durable leaf with a fresh receipt.
    recovered = store.append(receipt(observed_at="2026-09-15T12:30:00+00:00"),
                             supersedes=durable.receipt_hash)
    assert store.current(first.pool_id).receipt_hash == recovered.receipt_hash
    assert store.verify()
    assert len(store.history(first.pool_id)) == 3
    eligible = CapacityRegistry(
        store.authoritative_current_receipts()).eligible_capacity_for(
        "qwen3.8:27b", now=AT_NOW)
    assert [r.receipt_hash for r in eligible] == [recovered.receipt_hash]


def test_two_generation_chain_index_rewound_fails_closed(tmp_path):
    """A->B->C durable; index still points at A -> fail closed."""
    store = ProviderCapacityStore(str(tmp_path))
    a = store.append(receipt())
    b = receipt(observed_at="2026-09-15T12:01:00+00:00")
    b = replace(b, supersedes=a.receipt_hash,
                receipt_hash=_sha256({**b.core(), "supersedes": a.receipt_hash}))
    store._append_line(b)
    c = receipt(observed_at="2026-09-15T12:02:00+00:00")
    c = replace(c, supersedes=b.receipt_hash,
                receipt_hash=_sha256({**c.core(), "supersedes": b.receipt_hash}))
    store._append_line(c)
    with pytest.raises(CapacityStoreError):
        store.current(a.pool_id)
    assert store.verify() is False
    with pytest.raises(CapacityStoreError):
        store.append(receipt(observed_at="2026-09-15T12:40:00+00:00"),
                             supersedes=a.receipt_hash)


def test_three_generation_chain_mid_index_fails_closed(tmp_path):
    """A->B->C durable; index commits B but C is an uncommitted successor
    -> fail closed (C claims to supersede a committed chain member)."""
    store = ProviderCapacityStore(str(tmp_path))
    a = store.append(receipt())
    b = store.append(receipt(observed_at="2026-09-15T12:01:00+00:00"),
                     supersedes=a.receipt_hash)
    c = receipt(observed_at="2026-09-15T12:02:00+00:00")
    c = replace(c, supersedes=b.receipt_hash,
                receipt_hash=_sha256({**c.core(), "supersedes": b.receipt_hash}))
    store._append_line(c)
    with pytest.raises(CapacityStoreError):
        store.current(a.pool_id)
    assert store.verify() is False


def test_plain_orphan_does_not_invalidate_indexed_authority(tmp_path):
    """A valid orphan that does NOT supersede the indexed authority must not
    become authority and must not invalidate the index solely by existing."""
    store = ProviderCapacityStore(str(tmp_path))
    first = store.append(receipt())
    orphan = receipt(observed_at="2026-09-15T12:03:00+00:00")
    store._append_line(orphan)
    assert store.current(first.pool_id).receipt_hash == first.receipt_hash
    assert store.verify()
    eligible = CapacityRegistry(
        store.authoritative_current_receipts()).eligible_capacity_for(
        "qwen3.8:27b", now=AT_NOW)
    assert [r.receipt_hash for r in eligible] == [first.receipt_hash]


def test_corrupt_trailing_successor_candidate_is_not_promoted(tmp_path):
    """A corrupt trailing row that would have been a successor must stay a
    corrupt history row: it is neither promoted nor does it create ambiguity,
    and the strict historical reader still reports it."""
    store = ProviderCapacityStore(str(tmp_path))
    first = store.append(receipt())
    with open(store.receipts_path, "a", encoding="utf-8") as handle:
        handle.write("{\"receipt_hash\": \"abc\"\n")
    assert store.current(first.pool_id).receipt_hash == first.receipt_hash
    assert store.verify() is False    # strict integrity reader flags the corruption
    with pytest.raises(CapacityStoreError):
        store.history(first.pool_id)

def test_strict_enum_and_numeric_validation():
    with pytest.raises(CapacityError): receipt(entitlement="bogus")
    with pytest.raises(CapacityError): receipt(state="banana")
    with pytest.raises(CapacityError): receipt(authorization_class="api_key")
    with pytest.raises(CapacityError): PriceObservation("metered")
    with pytest.raises(CapacityError): QuotaDimension("x", "requests", "5", UNKNOWN, provenance=prov())
    with pytest.raises(CapacityError): QuotaDimension("x", "requests", float("nan"), UNKNOWN, provenance=prov())
    with pytest.raises(CapacityError): QuotaDimension("x", "requests", 5.5, UNKNOWN, provenance=prov())
    with pytest.raises(CapacityError): receipt(concurrency_remaining="0")
    with pytest.raises(CapacityError): receipt(concurrency_remaining=float("inf"))
    with pytest.raises(CapacityError): receipt(zdr_supported="yes")


def test_registry_rejects_missing_supersession_target():
    original = receipt()
    core = {**original.core(), "supersedes": "f" * 64}
    malformed = replace(original, supersedes="f" * 64, receipt_hash=_sha256(core))
    with pytest.raises(CapacityError):
        CapacityRegistry((malformed,))


def test_registry_rejects_noncurrent_supersession_successor():
    first = receipt()
    second = receipt(observed_at="2026-09-15T12:01:00+00:00")
    second = replace(second, supersedes=first.receipt_hash,
                     receipt_hash=_sha256({**second.core(), "supersedes": first.receipt_hash}))
    third = receipt(observed_at="2026-09-15T12:02:00+00:00")
    third = replace(third, supersedes=first.receipt_hash,
                    receipt_hash=_sha256({**third.core(), "supersedes": first.receipt_hash}))
    with pytest.raises(CapacityError):
        CapacityRegistry((first, second, third))


def test_actual_billing_requires_billing_grade_source():
    with pytest.raises(CapacityError):
        PriceObservation(CostClass.METERED, actual_billed_cost=1,
                         pricing_source=PricingSource.PROVIDER_API,
                         pricing_version="v1", provenance=prov())
    actual = PriceObservation(CostClass.METERED, actual_billed_cost=1,
                              pricing_source=PricingSource.PROVIDER_USAGE_LEDGER,
                              pricing_version="ledger-v1", provenance=prov())
    assert actual.actual_billed_cost == 1


def test_three_generation_supersession_chain_has_one_current(tmp_path):
    store = ProviderCapacityStore(str(tmp_path))
    a = store.append(receipt())
    b = store.append(receipt(observed_at="2026-09-15T12:01:00+00:00"), supersedes=a.receipt_hash)
    c = store.append(receipt(observed_at="2026-09-15T12:02:00+00:00"), supersedes=b.receipt_hash)
    assert store.current(a.pool_id).receipt_hash == c.receipt_hash
    assert len(CapacityRegistry(store.history()).eligible_capacity_for("qwen3.8:27b", now=AT_NOW)) == 1


def test_capacity_and_capability_are_independent_contract_inputs():
    def dispatch_ready(capability_qualified, capacity):
        return capability_qualified and capacity.has_usable_capacity_facts(now=AT_NOW)
    assert not dispatch_ready(True, receipt(state=CapacityState.EXHAUSTED))
    assert not dispatch_ready(False, receipt())
    assert not dispatch_ready(True, receipt(entitlement=Entitlement.UNKNOWN))


def test_available_without_known_capacity_is_not_usable():
    assert not receipt(concurrency_remaining=UNKNOWN, quotas=()).has_usable_capacity_facts(now=AT_NOW)
