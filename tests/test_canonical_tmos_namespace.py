"""Independent synthetic read-namespace regression; no native/provider work."""
from dataclasses import replace
from functools import partial
import pytest
from tests.test_canonical_inspector_admission import native_store as native_store, principal
from tests.test_local_worker_loop import _package
from tests.test_ps638_consumer_chain import _chain
from src.canonical_inspector_admission import PreparedPilot, PreparedTmosWork, Publication, UnknownInspection
from src.canonical_inspector_service import InspectorService, acknowledge_retention
from src.evidence_io import retain_evidence_bundle
from src.execution_package import compute_package_hash
from src.routing_inspector import project_retained_routing_inspection

@pytest.mark.parametrize("packet_id,ticket_key", [("synthetic-uppercase", "TMOS-11"), ("routing-inspector-command", "OTHER-1")])
@pytest.mark.parametrize("reopened", [False, True])
def test_real_inspection_rechecks_namespace_and_project(native_store, tmp_path, packet_id, ticket_key, reopened):
    store, engine = native_store
    owner = principal()
    package = replace(_package(), packet_id=packet_id, ticket_key=ticket_key)
    package = replace(package, package_hash=compute_package_hash(package.core()))
    result, _, _ = _chain(package=package)
    if packet_id == "synthetic-uppercase":
        prepared = PreparedPilot(package.run_id, package.packet_id, package.package_hash, "c"*64)
        claim = store.admit(owner, "one", prepared)
        store.consume(claim, owner)
    else:
        prepared = PreparedTmosWork(package.run_id, package.packet_id, package.package_hash, "c"*64)
        claim = store.admit_tmos_work(owner, "one", prepared)
        store.consume(claim, owner)
        store.begin_tmos_work(claim, owner)
    root = tmp_path / "retained"
    root.mkdir(mode=0o700)
    path = retain_evidence_bundle(root, result.evidence_package, artifacts=result.artifacts)
    ack = acknowledge_retention(root, path.stem, parent_directories=(tmp_path,))
    attempt = result.attempts[0]
    # Trusted persistence remains generation-generic for the unchanged canary.
    handle = store.publish(claim, owner, Publication(path.stem, ack.evidence_hash,
        attempt.receipt_hash, attempt.dispatch_receipt_hash, "tmos-routing-inspector-v1"))
    if reopened:
        from sqlalchemy.orm import sessionmaker
        from src.canonical_inspector_admission import AdmissionStore
        store = AdmissionStore(sessionmaker(bind=engine, expire_on_commit=False))
    service = InspectorService(store, retained_root=root,
        projector=partial(project_retained_routing_inspection, scoring_version="source-test-v1"),
        restored_disclosure_generation="tmos-routing-inspector-v1")
    with pytest.raises(UnknownInspection):
        service.inspect(owner, handle, lambda: owner)
