"""Independent synthetic alternate association checks, no native/provider work."""
from dataclasses import replace
import pytest
from tests.test_canonical_inspector_admission import native_store as native_store
from tests.test_inspector_alternate import two_target_work, execute_alternate_source
from src.canonical_inspector_admission import PreparedTmosWork, InspectorUnavailable, UnknownInspection
from src.execution_package import compute_package_hash

def test_alternate_result_cannot_apply_original_request_to_another_tmos_ticket(native_store, tmp_path):
    store, _ = native_store
    work = two_target_work(store, tmp_path)
    assert work.package.ticket_key == "TMOS-11"
    work.package = replace(work.package, ticket_key="TMOS-99")
    alternate = execute_alternate_source(store, work)
    with pytest.raises((InspectorUnavailable, UnknownInspection)):
        work.service.publish_tmos_alternate(work.owner, work.published["handle"],
            alternate.preference["receipt_hash"], alternate.claim, alternate.execution, lambda: work.owner)

def test_started_request_cannot_start_again_after_cleanup_without_publication(native_store, tmp_path):
    store, _ = native_store
    work = two_target_work(store, tmp_path)
    alternate = execute_alternate_source(store, work)
    # First canonical attempt exists; known cleanup alone must not unspend its request.
    store.release_terminal(alternate.claim, cleanup_confirmed=True)
    package = replace(work.package, run_id="second-invocation-run")
    package = replace(package, package_hash=compute_package_hash(package.core()))
    claim = store.admit_tmos_work(work.owner, "second-admission", PreparedTmosWork(
        package.run_id, package.packet_id, package.package_hash, "c"*64))
    store.consume(claim, work.owner)
    with pytest.raises((InspectorUnavailable, UnknownInspection)):
        store.begin_tmos_alternate(claim, work.owner, work.published["handle"],
            alternate.preference["receipt_hash"])
