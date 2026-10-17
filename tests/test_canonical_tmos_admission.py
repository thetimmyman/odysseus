"""Real TMOS work origins through the existing owned admission interface.

All evidence and SQLite files here are isolated synthetic source fixtures.
"""
from dataclasses import replace

import pytest

from tests.test_canonical_inspector_admission import (
    native_store as native_store, prepared as prepared_canary, principal, publication,
)


def test_named_tmos_work_retains_its_actual_owner_and_exact_package(native_store):
    from src.canonical_inspector_admission import PreparedTmosWork

    store, _ = native_store
    owner = principal()
    prepared = PreparedTmosWork(
        run_id="work-one", packet_id="routing-inspector-command",
        package_hash="a" * 64, config_digest="c" * 64)
    claim = store.admit_tmos_work(owner, "real-work-one", prepared)
    assert store.consume(claim, owner) == prepared
    handle = store.publish(claim, owner, replace(
        publication(), disclosure_generation="tmos-routing-inspector-v1"))
    retained = store.resolve(owner, handle, "tmos-routing-inspector-v1")
    assert (retained.run_id, retained.packet_id, retained.package_hash) == (
        "work-one", "routing-inspector-command", "a" * 64)


def test_real_work_cannot_be_published_under_canary_disclosure(native_store):
    from src.canonical_inspector_admission import InspectorUnavailable, PreparedTmosWork

    store, _ = native_store
    owner = principal()
    prepared = PreparedTmosWork("real-one", "routing-inspector-control", "a" * 64, "c" * 64)
    claim = store.admit_tmos_work(owner, "one", prepared)
    store.consume(claim, owner)
    with pytest.raises(InspectorUnavailable):
        store.publish(claim, owner, publication())


def test_correction_cannot_relabel_real_work_as_canary_evidence(native_store):
    from src.canonical_inspector_admission import PreparedTmosWork, UnknownInspection

    store, _ = native_store
    owner = principal()
    prepared = PreparedTmosWork("real-one", "routing-inspector-command", "a" * 64, "c" * 64)
    claim = store.admit_tmos_work(owner, "one", prepared)
    store.consume(claim, owner)
    handle = store.publish(claim, owner, replace(
        publication(), disclosure_generation="tmos-routing-inspector-v1"))
    with pytest.raises(UnknownInspection):
        store.correct(owner, handle, publication())
    assert store.resolve(owner, handle, "tmos-routing-inspector-v1").packet_id == prepared.packet_id


@pytest.mark.parametrize("packet_id", ["synthetic-uppercase", "arbitrary-work", "", None])
def test_real_work_preparation_accepts_only_the_two_named_packets(packet_id):
    from src.canonical_inspector_admission import InspectorUnavailable, PreparedTmosWork

    with pytest.raises(InspectorUnavailable):
        PreparedTmosWork("work-one", packet_id, "a" * 64, "c" * 64)


def test_canary_and_real_admission_interfaces_cannot_exchange_intents(native_store):
    from src.canonical_inspector_admission import InspectorUnavailable, PreparedTmosWork

    store, _ = native_store
    work = PreparedTmosWork("work-one", "routing-inspector-command", "a" * 64, "c" * 64)
    with pytest.raises(InspectorUnavailable):
        store.admit(principal(), "one", work)
    with pytest.raises(InspectorUnavailable):
        store.admit_tmos_work(principal(), "one", prepared_canary())
    claim = store.admit(principal(), "one", prepared_canary())
    assert store.consume(claim, principal()).packet_id == "synthetic-uppercase"


def test_real_origin_refuses_other_owner_epoch_replay_and_restart(native_store):
    from sqlalchemy.orm import sessionmaker
    from src.canonical_inspector_admission import (
        AdmissionStore, InspectorUnavailable, PreparedTmosWork, UnknownInspection,
    )

    store, engine = native_store
    owner, other_epoch = principal(), principal(created=2.0)
    work = PreparedTmosWork("work-one", "routing-inspector-command", "a" * 64, "c" * 64)
    claim = store.admit_tmos_work(owner, "one", work)
    for foreign in (principal("other"), other_epoch):
        with pytest.raises(InspectorUnavailable):
            store.consume(claim, foreign)
    assert store.consume(claim, owner) == work
    handle = store.publish(claim, owner, replace(
        publication(), disclosure_generation="tmos-routing-inspector-v1"))
    reopened = AdmissionStore(sessionmaker(bind=engine, expire_on_commit=False))
    assert reopened.resolve(owner, handle, "tmos-routing-inspector-v1").run_id == "work-one"
    for foreign in (principal("other"), other_epoch):
        with pytest.raises(UnknownInspection):
            reopened.resolve(foreign, handle, "tmos-routing-inspector-v1")
    with pytest.raises(InspectorUnavailable):
        reopened.consume(claim, owner)
    store.release_terminal(claim, cleanup_confirmed=True)
    with pytest.raises(InspectorUnavailable):
        reopened.admit_tmos_work(owner, "one", replace(work, run_id="work-replay"))
