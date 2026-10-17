"""Owned publication of synthetic canonical runs; no provider or live adapter."""
from dataclasses import replace
from functools import partial
from types import SimpleNamespace

import pytest

from tests.test_canonical_inspector_admission import native_store as native_store, principal
from tests.test_local_worker_loop import _package
from tests.test_ps638_consumer_chain import _chain
from src.canonical_inspector_admission import PreparedTmosWork
from src.canonical_inspector_service import InspectorService
from src.canonical_pilot_adapter import PilotExecution
from src.evidence_io import retain_evidence_bundle
from src.execution_package import compute_package_hash
from src.routing_inspector import project_retained_routing_inspection


def tmos_package(**changes):
    package = replace(_package(), **{
        "packet_id": "routing-inspector-command", "ticket_key": "TMOS-11", **changes})
    return replace(package, package_hash=compute_package_hash(package.core()))


def retained_work(store, tmp_path, *, begin=True, package_changes=None, **intent_changes):
    owner = principal()
    package = tmos_package(**(package_changes or {}))
    prepared = PreparedTmosWork(package.run_id, package.packet_id, package.package_hash, "c" * 64)
    prepared = replace(prepared, **intent_changes)
    claim = store.admit_tmos_work(owner, "work-one", prepared)
    store.consume(claim, owner)
    if begin:
        store.begin_tmos_work(claim, owner)
    # This fixture runs the normal canonical loop with synthetic transports.
    result, _, _ = _chain(package=package)
    retained_root = tmp_path / "retained"
    retained_root.mkdir(mode=0o700)
    path = retain_evidence_bundle(retained_root, result.evidence_package, artifacts=result.artifacts)
    execution = PilotExecution(path.stem, result.attempts[0].receipt_hash, True)
    service = InspectorService(
        store, retained_root=retained_root,
        projector=partial(project_retained_routing_inspection, scoring_version="source-test-v1"),
        retention_parents=(tmp_path,),
        restored_disclosure_generation="tmos-routing-inspector-v1")
    return SimpleNamespace(owner=owner, package=package, prepared=prepared, claim=claim,
                           result=result, path=path, execution=execution, service=service)


def test_owned_started_tmos_run_publishes_its_exact_retained_attempt(native_store, tmp_path):
    store, _ = native_store
    work = retained_work(store, tmp_path)
    published = work.service.publish_tmos_work(work.owner, work.claim, work.execution, lambda: work.owner)
    view = work.service.inspect(work.owner, published["handle"], lambda: work.owner)
    assert published["status"] == "retained"
    assert view["execution_package_hash"] == work.package.package_hash
    assert view["attempt_receipt_hash"] == work.result.attempts[0].receipt_hash
    assert view["dispatch_receipt_hash"] == work.result.attempts[0].dispatch_receipt_hash
    assert view["semantic_acceptance"] == "UNKNOWN"


def test_unmatched_attempt_is_refused_without_releasing_the_owned_slot(native_store, tmp_path):
    from src.canonical_inspector_admission import InspectorUnavailable

    store, _ = native_store
    work = retained_work(store, tmp_path)
    with pytest.raises(InspectorUnavailable):
        work.service.publish_tmos_work(work.owner, work.claim,
            replace(work.execution, attempt_hash="f" * 64), lambda: work.owner)
    with pytest.raises(InspectorUnavailable):
        store.admit_tmos_work(work.owner, "work-two", replace(work.prepared, run_id="another-run"))


@pytest.mark.parametrize("changed", [
    {"run_id": "another-run"}, {"packet_id": "routing-inspector-control"}, {"package_hash": "f" * 64},
])
def test_publication_refuses_mismatched_mission_packet_or_package(native_store, tmp_path, changed):
    from src.canonical_inspector_admission import InspectorUnavailable

    store, _ = native_store
    work = retained_work(store, tmp_path, **changed)
    with pytest.raises(InspectorUnavailable):
        work.service.publish_tmos_work(work.owner, work.claim, work.execution, lambda: work.owner)


def test_foreign_owner_cannot_publish_or_consume_the_started_result(native_store, tmp_path):
    from src.canonical_inspector_admission import InspectorUnavailable

    store, _ = native_store
    work = retained_work(store, tmp_path)
    foreign = principal("other")
    with pytest.raises(InspectorUnavailable):
        work.service.publish_tmos_work(foreign, work.claim, work.execution, lambda: foreign)
    published = work.service.publish_tmos_work(work.owner, work.claim, work.execution, lambda: work.owner)
    assert work.service.inspect(work.owner, published["handle"], lambda: work.owner)[
        "attempt_receipt_hash"] == work.execution.attempt_hash


@pytest.mark.parametrize("failure", ["not-started", "cleanup-unknown", "corrupt-source", "restart"])
def test_uncertain_source_or_execution_never_publishes_or_releases_slot(native_store, tmp_path, failure):
    from sqlalchemy.orm import sessionmaker
    from src.canonical_inspector_admission import AdmissionStore, InspectorUnavailable

    store, engine = native_store
    work = retained_work(store, tmp_path, begin=failure != "not-started")
    if failure == "cleanup-unknown":
        work.execution = replace(work.execution, cleanup_confirmed=False)
    if failure == "corrupt-source":
        work.path.write_text("corrupt retained bytes")
    if failure == "restart":
        work.service = InspectorService(
            AdmissionStore(sessionmaker(bind=engine, expire_on_commit=False)),
            retained_root=work.path.parent, projector=project_retained_routing_inspection,
            restored_disclosure_generation="tmos-routing-inspector-v1")
    with pytest.raises(InspectorUnavailable):
        work.service.publish_tmos_work(work.owner, work.claim, work.execution, lambda: work.owner)
    with pytest.raises(InspectorUnavailable):
        store.admit_tmos_work(work.owner, "work-two", replace(work.prepared, run_id="another-run"))


def test_real_publication_stays_within_the_admitted_tmos_project(native_store, tmp_path):
    from src.canonical_inspector_admission import InspectorUnavailable

    store, _ = native_store
    work = retained_work(store, tmp_path, package_changes={"ticket_key": "OTHER-1"})
    with pytest.raises(InspectorUnavailable):
        work.service.publish_tmos_work(work.owner, work.claim, work.execution, lambda: work.owner)


def test_revoked_principal_during_publication_cannot_emit_an_owned_handle(native_store, tmp_path):
    from src.canonical_inspector_admission import InspectorUnavailable

    store, _ = native_store
    work = retained_work(store, tmp_path)
    snapshots = iter((work.owner, principal(created=2.0)))
    with pytest.raises(InspectorUnavailable):
        work.service.publish_tmos_work(work.owner, work.claim, work.execution, lambda: next(snapshots))
    # The aborted authorization did not consume a publication or release custody.
    published = work.service.publish_tmos_work(work.owner, work.claim, work.execution, lambda: work.owner)
    assert work.service.inspect(work.owner, published["handle"], lambda: work.owner)[
        "execution_package_hash"] == work.package.package_hash
