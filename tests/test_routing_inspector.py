"""Synthetic read-only controls for the canonical routing inspection projection."""
from __future__ import annotations

import dataclasses
import hashlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import dispatch_routing as routing
from src.attempt_receipt import ArtifactRef, make_attempt_receipt, make_verification_receipt
from src.evidence_contract import INDEPENDENCE_HARNESS_HIDDEN
from src.evidence_package import seal_evidence_package
from src.execution_package import (
    DECIDED_BY_EXPLICIT_PIN, VerificationPlan, build_execution_package,
    make_dispatch_receipt, package_hash_is_valid, seal_verifier_digests,
)
from src.execution_outcomes import build_outcome_record
from src.local_targets import (
    CapabilityEvidence, ContextProfile, ModelIdentity, RuntimeIdentity,
    make_target_capability_receipt,
)
from src.provider_capacity import (
    AuthorizationClass, CapacityState, Entitlement, EvidenceProvenance,
    make_capacity_receipt,
)
from src.routing_inspector import RoutingInspectionError, project_routing_inspection
from src.source_snapshot import take_source_snapshot
from src.worker_context import render_worker_context
from test_execution_outcomes import make_evidence, make_facts

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
PACKET = {
    "packet_id": "synthetic-packet", "objective": "inspect a synthetic route",
    "contract": "read-only projection", "role": "local_implementer",
    "write_scope": ["src/example.py"],
    "interface": [{"name": "value", "required": True,
                    "type_hint": "int", "semantics": "synthetic"}],
    "test_command": "python -m pytest tests/test_example.py -q",
    "acceptance_criteria": ["projection is descriptive"],
    "negative_control": "tampering is refused", "stop_conditions": ["none"],
}


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True)


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "fixture"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src/example.py").write_text("VALUE = 1\n")
    (root / "tests/test_example.py").write_text("def test_value(): assert True\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "synthetic@example.invalid")
    _git(root, "config", "user.name", "Synthetic")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "synthetic baseline")
    source = take_source_snapshot(str(root), base_sha="HEAD",
                                 relevant_paths=["src/example.py", "tests/test_example.py"])
    plan = VerificationPlan(
        verifier_id="tests/test_example.py", command="python -m pytest tests/test_example.py -q",
        verifier_paths=("tests/test_example.py",),
        verifier_digests=seal_verifier_digests(str(root), ["tests/test_example.py"]))
    return build_execution_package(PACKET, source=source, verification=plan,
                                   run_id="synthetic-run", allowed_tools=("read_file",))


def _selected_receipt(package):
    profile = routing.make_target_profile(
        target_id="synthetic-local", profile_id="synthetic-profile", provider="ollama",
        host="synthetic-host", runtime_kind="fixture-runtime", runtime_version="1",
        model="fixture-model", model_digest="d" * 64, backend="fixture-backend",
        endpoint_url="http://127.0.0.1:9999", locality=routing.LOCALITY_LOCAL,
        roles=frozenset({routing.ROLE_IMPLEMENTER}), tools=frozenset({"read_file"}),
        network_policy="private-loopback", budget_class="test", cost_rank=0)
    capability = routing.make_legacy_capability_view(
        receipt_id="synthetic-cap", profile_id=profile.profile_id,
        target_id=profile.target_id, capabilities=frozenset({
            routing.CAP_TEXT_GENERATION, routing.CAP_SINGLE_TOOL_CALL,
            routing.CAP_EXACT_REFERENCE_SEMANTICS}),
        exactness=routing.EXACTNESS_EXACT, observed_at=NOW.isoformat(), ttl_s=3600,
        healthy=True, host=profile.host, runtime_version=profile.runtime_version,
        model_digest=profile.model_digest)
    denied = routing.make_target_profile(
        target_id="synthetic-local", profile_id="synthetic-refused-profile",
        provider="ollama", host="synthetic-host-b", runtime_kind="fixture-runtime",
        runtime_version="1", model="other-model", model_digest="f" * 64,
        backend="fixture-backend", endpoint_url="http://127.0.0.1:9998",
        locality=routing.LOCALITY_LOCAL, roles=frozenset({routing.ROLE_IMPLEMENTER}),
        tools=frozenset({"read_file"}), network_policy="private-loopback",
        budget_class="test", cost_rank=1)
    request = routing.RoutingRequest(
        domain="general_swe", role=routing.ROLE_IMPLEMENTER, run_id=package.run_id,
        packet_id=package.packet_id, execution_package_hash=package.package_hash,
        required_tools=("read_file",), network_policy="private-loopback")
    decision = routing.select_target(request, profiles=[profile, denied], receipts=[capability],
                                     now=NOW, decision_id="synthetic-decision")
    return make_dispatch_receipt(**decision.to_ps638_receipt_kwargs())


def _matching_outcome(package, dispatch, *, decision_receipts=None, attempt_dispatches=None):
    root = package.source.repo_root
    context = render_worker_context(PACKET, max_chars=4000).encode()
    stdout = b"1 passed\n"
    def ref(name, body):
        return ArtifactRef(artifact_id=name, sha256=hashlib.sha256(body).hexdigest(),
                           size=len(body), storage_uri=f"mem://{name}")
    selected_per_attempt = tuple(attempt_dispatches or (dispatch,))
    attempts = []
    verifications = []
    for number, attempt_dispatch in enumerate(selected_per_attempt, start=1):
        output = f"synthetic result {number}".encode()
        attempts.append(make_attempt_receipt(
            receipt_id=f"synthetic-attempt-{number}", run_id=package.run_id,
            packet_id=package.packet_id, attempt=number,
            repair_of=number - 1, execution_package_hash=package.package_hash,
            dispatch_receipt_hash=attempt_dispatch.receipt_hash,
            target_id=attempt_dispatch.selected_target_id, host=attempt_dispatch.selected_host,
            model=attempt_dispatch.selected_model,
            context_projection_hash=hashlib.sha256(context).hexdigest(),
            rendered_context_ref=ref(f"context-{number}", context),
            output_ref=ref(f"output-{number}", output),
            declared_write_set=package.write_scope, actual_write_set=package.write_scope,
            elapsed_s=2.0 + number, prompt_tokens=10, completion_tokens=4, finish_reason="stop"))
    (Path(root) / "src/example.py").write_text("VALUE = 2\n")
    snapshot = take_source_snapshot(root, base_sha="HEAD",
                                    relevant_paths=["src/example.py", "tests/test_example.py"])
    for number in range(1, len(selected_per_attempt) + 1):
        verifications.append(make_verification_receipt(
            receipt_id=f"synthetic-verification-{number}", run_id=package.run_id,
            packet_id=package.packet_id, attempt=number,
            execution_package_hash=package.package_hash,
            verifier_id=package.verification.verifier_id,
            normalized_command=package.verification.command,
            source_snapshot_digest=snapshot.snapshot_digest, exit_code=0,
            verifier_digest=package.verification.digest_of("tests/test_example.py"),
            verifier_paths=("tests/test_example.py",), ended_at=NOW.isoformat(),
            stdout_ref=ref("stdout", stdout), tests_collected=1, tests_executed=1,
            tests_passed=1, tests_failed=0,
            requirement_ids=tuple(req.requirement_id for req in package.evidence_requirements),
            proof_class=INDEPENDENCE_HARNESS_HIDDEN, control_id="synthetic-control",
            control_expected="FAIL", control_observed="FAIL", control_passed=True))
    evidence = seal_evidence_package(
        evidence_package_id="synthetic-evidence", execution_package=package,
        dispatch_receipts=tuple(decision_receipts or (dispatch,)),
        attempt_receipts=tuple(attempts), verification_receipts=tuple(verifications)).to_dict()
    extensions = {"mem://stdout": stdout}
    for number, attempt in enumerate(attempts, start=1):
        extensions[f"mem://context-{number}"] = context
        extensions[f"mem://output-{number}"] = f"synthetic result {number}".encode()
    return build_outcome_record(evidence_package=evidence,
                                companion_facts=make_facts(evidence),
                                artifact_extensions=extensions)


def test_actual_selector_receipt_projects_as_isolated_read_only_view(package):
    dispatch = _selected_receipt(package)
    assert package_hash_is_valid(package.to_dict())
    view = project_routing_inspection(package, dispatch, now=NOW)
    assert view["authoritative"] is False
    assert view["dispatch"]["selected"]["target_id"] == "synthetic-local"
    assert view["dispatch"]["reason"] == dispatch.reason
    assert view["dispatch"]["envelope"]["tools"] == ["read_file"]
    assert view["dispatch"]["candidates_considered"][0]["eligible"] is True
    assert any(row["profile_id"] == "synthetic-refused-profile" and not row["eligible"]
               for row in view["dispatch"]["candidates_considered"])
    assert view["capabilities"]["status"] == "UNKNOWN"
    view["dispatch"]["candidates_considered"][0]["reason"] = "tampered view"
    assert dispatch.candidates_considered[0]["reason"] != "tampered view"
    assert package_hash_is_valid(package.to_dict())


def test_legacy_explicit_pin_has_unknown_alternatives(package):
    dispatch = make_dispatch_receipt(
        receipt_id="synthetic-pin", execution_package_hash=package.package_hash,
        run_id=package.run_id, packet_id=package.packet_id,
        selected_target_id="synthetic-target", selected_host="synthetic-host",
        selected_model="synthetic-model", selected_runtime_kind="fixture",
        selected_runtime_version="1", selected_model_digest="e" * 64,
        selected_backend="fixture", decided_by=DECIDED_BY_EXPLICIT_PIN,
        reason="synthetic operator pin", decided_at=NOW.isoformat())
    view = project_routing_inspection(package, dispatch, now=NOW)
    assert view["dispatch"]["candidates_considered"] == "UNKNOWN"
    assert view["dispatch"]["fallback"] == "UNKNOWN"


def test_tampered_package_dispatch_link_and_selected_marker_are_refused(package):
    dispatch = _selected_receipt(package)
    with pytest.raises(RoutingInspectionError, match="package hash"):
        project_routing_inspection(dataclasses.replace(package, objective="changed"),
                                   dispatch, now=NOW)
    with pytest.raises(RoutingInspectionError, match="dispatch receipt hash"):
        object.__setattr__(dispatch, "reason", "tampered")
        project_routing_inspection(package, dispatch, now=NOW)


def test_naive_now_and_duplicate_candidate_identity_are_refused(package):
    dispatch = _selected_receipt(package)
    with pytest.raises(RoutingInspectionError, match="timezone-aware"):
        project_routing_inspection(package, dispatch, now=datetime(2026, 10, 4))
    rows = list(dispatch.candidates_considered)
    object.__setattr__(dispatch, "candidates_considered", tuple(rows + rows))
    # Hash validation catches direct mutation before the duplicate is interpreted.
    with pytest.raises(RoutingInspectionError, match="dispatch receipt hash"):
        project_routing_inspection(package, dispatch, now=NOW)


def test_referenced_canonical_capability_and_capacity_are_supplemented(package):
    capability = make_target_capability_receipt(
        host_id="synthetic-host", profile_id="ignored-recomputed",
        observed_at=NOW.isoformat(),
        runtime=RuntimeIdentity(runtime_kind="fixture-runtime", provider="fixture",
                                version="1", backend="fixture-backend"),
        model=ModelIdentity(model_id="fixture-model", alias="fixture-model",
                            digest="d" * 64),
        context=ContextProfile(safe_working_context=4096,
                               safe_context_source="synthetic-inspector-fixture"),
        capabilities=CapabilityEvidence(measured=("text_generation",)))
    capacity = make_capacity_receipt(
        provider="fixture", pool_id="synthetic-pool", account_identity="synthetic-account",
        authorization_class=AuthorizationClass.API_KEY, entitlement=Entitlement.API,
        exposed_models=("fixture-model",), observed_at=NOW.isoformat(), ttl_seconds=3600,
        collector_id="synthetic-collector", evidence_source="synthetic-test",
        evidence_reference="synthetic-reference", state=CapacityState.AVAILABLE,
        concurrency_remaining=2)
    dispatch = make_dispatch_receipt(
        receipt_id="synthetic-evidence-pin", execution_package_hash=package.package_hash,
        run_id=package.run_id, packet_id=package.packet_id,
        selected_target_id="synthetic-target", selected_host="synthetic-host",
        selected_model="fixture-model", selected_runtime_kind="fixture-runtime",
        selected_runtime_version="1", selected_model_digest="d" * 64,
        selected_backend="fixture-backend", decided_by=DECIDED_BY_EXPLICIT_PIN,
        reason="synthetic evidence pin", decided_at=NOW.isoformat(),
        capability_receipt_refs=(capability.receipt_hash,),
        capacity_receipt_refs=(capacity.ref,))
    view = project_routing_inspection(
        package, dispatch, capability_receipts=(capability,), capacity_receipts=(capacity,), now=NOW)
    assert view["capabilities"]["status"] == "OBSERVED"
    assert view["capabilities"]["supplements"][0]["qualification_state"] == "valid"
    assert view["capacity"]["status"] == "OBSERVED"
    assert view["capacity"]["billing_authority"] is False
    assert view["capacity"]["supplements"][0]["facts_current"] is True


def test_future_referenced_evidence_stays_unknown(package):
    future = datetime(2026, 10, 4, 13, tzinfo=timezone.utc)
    capability = make_target_capability_receipt(
        host_id="synthetic-host", profile_id="ignored-recomputed",
        observed_at=future.isoformat(),
        runtime=RuntimeIdentity(runtime_kind="fixture-runtime", provider="fixture",
                                version="1", backend="fixture-backend"),
        model=ModelIdentity(model_id="fixture-model", alias="fixture-model",
                            digest="d" * 64),
        context=ContextProfile(safe_working_context=4096,
                               safe_context_source="synthetic-future-inspector-fixture"),
        capabilities=CapabilityEvidence(measured=("text_generation",)))
    future_provenance = EvidenceProvenance(
        source="synthetic-test", reference="future-state", collector_id="synthetic-collector",
        observed_at=future.isoformat(), ttl_seconds=3600)
    current_provenance = EvidenceProvenance(
        source="synthetic-test", reference="current", collector_id="synthetic-collector",
        observed_at=NOW.isoformat(), ttl_seconds=3600)
    capacity = make_capacity_receipt(
        provider="fixture", pool_id="future-pool", account_identity="synthetic-account",
        authorization_class=AuthorizationClass.API_KEY, entitlement=Entitlement.API,
        exposed_models=("fixture-model",), observed_at=NOW.isoformat(), ttl_seconds=3600,
        collector_id="synthetic-collector", evidence_source="synthetic-test",
        evidence_reference="synthetic-reference", state=CapacityState.AVAILABLE,
        state_provenance=future_provenance, entitlement_provenance=current_provenance,
        zdr_provenance=current_provenance, concurrency_remaining=2)
    dispatch = make_dispatch_receipt(
        receipt_id="synthetic-future-pin", execution_package_hash=package.package_hash,
        run_id=package.run_id, packet_id=package.packet_id,
        selected_target_id="synthetic-target", selected_host="synthetic-host",
        selected_model="fixture-model", selected_runtime_kind="fixture-runtime",
        selected_runtime_version="1", selected_model_digest="d" * 64,
        selected_backend="fixture-backend", decided_by=DECIDED_BY_EXPLICIT_PIN,
        reason="synthetic future evidence pin", decided_at=NOW.isoformat(),
        capability_receipt_refs=(capability.receipt_hash,),
        capacity_receipt_refs=(capacity.ref,))
    view = project_routing_inspection(
        package, dispatch, capability_receipts=(capability,), capacity_receipts=(capacity,), now=NOW)
    assert view["capabilities"]["status"] == "UNKNOWN"
    assert view["capabilities"]["supplements"][0]["qualification_state"] == "invalidated_future_observation"
    assert view["capacity"]["status"] == "UNKNOWN"
    assert view["capacity"]["supplements"][0]["facts_current"] is False


def test_forged_link_and_rehashed_duplicate_candidate_rows_are_refused(package):
    dispatch = _selected_receipt(package)
    with pytest.raises(RoutingInspectionError, match="not linked"):
        project_routing_inspection(
            package,
            make_dispatch_receipt(**{**dispatch.core(), "execution_package_hash": "f" * 64}),
            now=NOW)
    fields = dispatch.core()
    fields["candidates_considered"] = list(fields["candidates_considered"]) * 2
    duplicate = make_dispatch_receipt(**fields)
    with pytest.raises(RoutingInspectionError, match="duplicated"):
        project_routing_inspection(package, duplicate, now=NOW)


def test_candidate_identity_types_and_exact_profile_marker(package):
    dispatch = _selected_receipt(package)
    rows = [dict(row) for row in dispatch.candidates_considered]
    alternate = dict(rows[0], profile_id="synthetic-second-profile", selected=False,
                     eligible=False, rule="refused_capability")
    rows.append(alternate)
    paired = make_dispatch_receipt(**{**dispatch.core(), "candidates_considered": rows})
    view = project_routing_inspection(package, paired, now=NOW)
    assert len(view["dispatch"]["candidates_considered"]) == 3
    assert sum(row.get("selected") is True for row in view["dispatch"]["candidates_considered"]) == 1
    assert any(row["profile_id"] == "synthetic-second-profile" and not row["eligible"]
               for row in view["dispatch"]["candidates_considered"])

    malformed = [dict(row) for row in rows]
    malformed[1]["target_id"] = ["bad-id"]
    sealed_bad = make_dispatch_receipt(**{**dispatch.core(), "candidates_considered": malformed})
    with pytest.raises(RoutingInspectionError, match="nonempty strings"):
        project_routing_inspection(package, sealed_bad, now=NOW)

    duplicated = [dict(row) for row in rows]
    duplicated.append(dict(duplicated[0]))
    sealed_duplicate = make_dispatch_receipt(**{**dispatch.core(), "candidates_considered": duplicated})
    with pytest.raises(RoutingInspectionError, match="duplicated"):
        project_routing_inspection(package, sealed_duplicate, now=NOW)

    conflicting_markers = [dict(row) for row in rows]
    conflicting_markers[1]["selected"] = True
    sealed_conflict = make_dispatch_receipt(**{**dispatch.core(), "candidates_considered": conflicting_markers})
    with pytest.raises(RoutingInspectionError, match="markers contradict"):
        project_routing_inspection(package, sealed_conflict, now=NOW)


def test_only_validated_exactly_associated_outcomes_are_aggregated(package, tmp_path):
    dispatch = _selected_receipt(package)
    associated = _matching_outcome(package, dispatch)
    evidence, extensions, _, _, _, _ = make_evidence(tmp_path / "other-run")
    record = build_outcome_record(evidence_package=evidence,
                                  companion_facts=make_facts(evidence),
                                  artifact_extensions=extensions)
    view = project_routing_inspection(package, dispatch, outcomes=(associated, record), now=NOW,
                                      scoring_version="synthetic-v1")
    assert view["outcomes"]["status"] == "OBSERVED"
    assert associated.raw_record_hash in view["outcomes"]["associated_source_hashes"]
    assert record.raw_record_hash in view["outcomes"]["unassociated_source_hashes"]
    with pytest.raises(RoutingInspectionError, match="canonical record"):
        project_routing_inspection(package, dispatch, outcomes=(b"{}",), now=NOW,
                                   scoring_version="synthetic-v1")


def test_unused_and_mixed_dispatch_attempt_histories_stay_unassociated(package):
    dispatch_a = _selected_receipt(package)
    dispatch_b = make_dispatch_receipt(**{
        **dispatch_a.core(), "receipt_id": "synthetic-unused-decision",
        "reason": "same profile, unused decision",
    })
    only_a = _matching_outcome(package, dispatch_a, decision_receipts=(dispatch_a, dispatch_b))
    view_a = project_routing_inspection(
        package, dispatch_a, outcomes=(only_a,), now=NOW, scoring_version="synthetic-v1")
    view_b = project_routing_inspection(
        package, dispatch_b, outcomes=(only_a,), now=NOW, scoring_version="synthetic-v1")
    assert view_a["outcomes"]["associated_source_hashes"] == [only_a.raw_record_hash]
    assert view_b["outcomes"]["status"] == "UNKNOWN"
    assert view_b["outcomes"]["associated_source_hashes"] == []
    assert view_b["outcomes"]["unassociated_source_hashes"] == [only_a.raw_record_hash]

    mixed = _matching_outcome(package, dispatch_a,
                              decision_receipts=(dispatch_a, dispatch_b),
                              attempt_dispatches=(dispatch_a, dispatch_b))
    for dispatch in (dispatch_a, dispatch_b):
        view = project_routing_inspection(
            package, dispatch, outcomes=(mixed,), now=NOW, scoring_version="synthetic-v1")
        assert view["outcomes"]["status"] == "UNKNOWN"
        assert view["outcomes"]["associated_source_hashes"] == []
        assert view["outcomes"]["unassociated_source_hashes"] == [mixed.raw_record_hash]


def test_same_dispatch_repair_chain_and_duplicate_record_keep_denominator(package):
    dispatch = _selected_receipt(package)
    repaired = _matching_outcome(package, dispatch, attempt_dispatches=(dispatch, dispatch))
    view = project_routing_inspection(
        package, dispatch, outcomes=(repaired, repaired), now=NOW,
        scoring_version="synthetic-v1")
    scorecard = view["outcomes"]["scorecard"]
    assert scorecard["confidence_status"] == "INSUFFICIENT"
    assert scorecard["ranking_allowed"] is False
    assert scorecard["arms"][0]["distinct_execution_n"] == 1
    assert scorecard["arms"][0]["attempt_n"] == 2
    assert scorecard["source_record_hashes"] == [repaired.raw_record_hash]
