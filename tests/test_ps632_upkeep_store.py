"""Stored status is pure; measured renewal preserves active authority and history."""
from concurrent.futures import ThreadPoolExecutor
import ast
import dataclasses
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.local_targets import ContextProfile, OllamaInspector, make_target_capability_receipt
from src import target_capability_store as store_module
from src.target_capability_store import TargetCapabilityStore, CapabilityStoreError
from test_ps632_profile_registry import measured, spec


CLI = Path(__file__).resolve().parents[1] / "scripts/odysseus-capability"


def moment(seconds=0):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()


def candidate(previous, *, label="new", **changes):
    fields = {**previous.to_dict(), "observed_at": moment(-1), "health_checked_at": moment(-1),
              "invalidation_reason": "", "supersedes": "",
              "context": dataclasses.replace(previous.context, safe_context_source="measurement-" + label)}
    fields.update(changes)
    return make_target_capability_receipt(**fields)


@pytest.fixture
def installed(tmp_path):
    original = measured(observed_at=moment(-90000), ttl_s=86400, health_checked_at=moment(-3600),
                        context=ContextProfile(configured_context=262144, configured_served_context=262144,
                                               safe_working_context=4096, semantic_verified_context=4096,
                                               safe_context_source="measurement-original",
                                               options={"parser": "synthetic-parser"}))
    store = TargetCapabilityStore(str(tmp_path / "store"))
    store.append(original)
    return store, original


def bytes_in(store):
    return {p.name: p.read_bytes() for p in Path(store.directory).iterdir() if p.is_file()}


def renew(store, original, renewed, **overrides):
    options = {"expected_profile_id": original.profile_id,
               "expected_receipt_hash": original.receipt_hash,
               "current_identity_digest": renewed.identity_digest(),
               "identity_checked_at": moment(-1)}
    options.update(overrides)
    return store.renew_profile(renewed, **options)


def test_status_does_not_create_missing_store_or_probe(tmp_path, monkeypatch):
    store = TargetCapabilityStore(str(tmp_path / "missing"))
    row = measured()
    def forbidden(*args, **kwargs):
        pytest.fail("read-only status attempted a write or provider call")
    monkeypatch.setattr(store, "_writer_lock", forbidden)
    monkeypatch.setattr(store, "_write_json", forbidden)
    monkeypatch.setattr(OllamaInspector, "api", forbidden)
    result = store.status(specs=[spec(row)])
    assert result["ok"] and result["read_only"] and result["provider_calls"] == 0
    assert result["current_material_identity_observed"] is False
    assert result["profiles"][0]["qualification"] == "NO_RECEIPT"
    assert "measure" in result["profiles"][0]["next_action"]
    assert not Path(store.directory).exists()


def test_status_distinguishes_qualification_health_and_material_identity(installed):
    store, original = installed
    before = bytes_in(store)
    result = store.status(specs=[spec(original)])
    row = result["profiles"][0]
    assert row["qualification"] == "qualification_expired"
    assert row["health"] == "liveness_expired"
    assert row["status"] == "REFUSED" and "qualification_expired" in row["refusal_reason"]
    assert row["safe_context_tokens"] == 4096
    assert set(row["roles"]) == {"approximate_analyst", "approximate_implementer"}
    assert row["qualification_expires_at"] and row["health_expires_at"]
    assert row["next_action"] == "requalify the installed profile"
    assert bytes_in(store) == before
    renewed = renew(store, original, candidate(original))
    ready = store.status(specs=[spec(renewed)])["profiles"][0]
    assert ready["qualification"] == "valid" and ready["health"] == "live"
    assert ready["status"] == "READY_FOR_IDENTITY_CHECK"
    assert ready["refusal_reason"] == "current material identity not observed"


def test_status_reports_corrupt_store_without_repairing_it(installed):
    store, original = installed
    path = Path(store.receipts_path)
    path.write_text(path.read_text().replace('"ADOPT_ROLE_SPECIFIC"', '"QUALIFIED"'))
    before = bytes_in(store)
    result = store.status(specs=[spec(original)])
    assert not result["ok"] and result["audit"]["problems"]
    assert result["profiles"][0]["status"] == "REFUSED"
    assert result["profiles"][0]["next_action"] == "repair registry evidence"
    assert bytes_in(store) == before


def test_capability_registry_does_not_import_routing_or_dispatch_layers():
    """Preserve TMOS I8: evidence producers cannot depend on their selector."""
    tree = ast.parse(Path(store_module.__file__).read_text())
    dependencies = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            dependencies.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            dependencies.add(node.module or "")
            dependencies.update((node.module or "") + "." + alias.name for alias in node.names)
    forbidden = {"src.local_target_routing", "src.dispatch_routing", "src.dispatch_boundary"}
    assert not dependencies & forbidden


@pytest.mark.parametrize("changed", ["model", "endpoint", "qualification_ref", "inference_role"])
def test_status_reports_native_binding_refusals_without_selecting(installed, changed):
    store, original = installed
    registered = spec(original)
    field, value = {"model": ("model", "other-model"), "endpoint": ("endpoint", "http://other.invalid"),
                    "qualification_ref": ("qualification_ref", "other-anchor"),
                    "inference_role": ("roles", ("deterministic_verifier",))}[changed]
    registered = dataclasses.replace(registered, **{field: value})
    before = bytes_in(store)
    result = store.status(specs=[registered])
    row = result["profiles"][0]
    if changed == "inference_role":
        assert row["status"] == "NOT_AN_INFERENCE_TARGET"
    else:
        assert row["status"] == "REFUSED" and row["refusal_reason"] == "registered_profile_binding_mismatch"
    assert result["current_material_identity_observed"] is False
    assert all(role.startswith("approximate_") for role in row["roles"])
    assert bytes_in(store) == before


def test_renewal_replaces_expired_measurement_and_preserves_active_and_history(installed):
    store, original = installed
    before = bytes_in(store)
    renewed = renew(store, original, candidate(original))
    after = bytes_in(store)
    assert renewed.qualification_state() == "valid" and renewed.health_state() == "live"
    assert renewed.profile_id == original.profile_id and renewed.qualification_ref == original.qualification_ref
    assert renewed.supersedes == original.receipt_hash
    assert after["active.json"] == before["active.json"]
    assert after["receipts.jsonl"].startswith(before["receipts.jsonl"])
    assert [r.receipt_hash for r in store.entries()] == [original.receipt_hash, renewed.receipt_hash]
    assert store.current_for_host(original.host_id).receipt_hash == renewed.receipt_hash
    assert store.verify()["ok"]


@pytest.mark.parametrize("changed", ["active", "current"])
def test_renewal_refuses_actual_concurrent_authority_changes(installed, changed):
    store, original = installed
    desired = candidate(original)
    if changed == "current":
        store.append(candidate(original, label="other-writer"), supersedes=original.receipt_hash)
    else:
        other = measured(runtime=dataclasses.replace(original.runtime, version="different-profile"))
        store.append(other)
        store.activate_profile(other.host_id, other.profile_id, expected_profile_id=original.profile_id,
                               current_identity_digest=other.identity_digest())
    before = bytes_in(store)
    with pytest.raises(CapabilityStoreError, match="changed before renewal"):
        renew(store, original, desired)
    assert bytes_in(store) == before


def test_competing_renewals_apply_only_one_current_receipt(installed):
    store, original = installed
    first, second = candidate(original, label="one"), candidate(original, label="two")
    before = bytes_in(store)
    def attempt(value):
        try:
            return renew(store, original, value)
        except CapabilityStoreError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attempt, (first, second)))
    assert sum(not isinstance(result, CapabilityStoreError) for result in outcomes) == 1
    assert len(store.entries()) == 2 and store.verify()["ok"]
    assert Path(store.active_path).read_bytes() == before["active.json"]


@pytest.mark.parametrize("changed", ["identity", "profile", "anchor", "expired", "future", "unhealthy",
                                     "old_measurement", "old_source", "larger_context", "stronger_role",
                                     "stronger_capability", "ttl"])
def test_renewal_refuses_changed_or_unmeasured_scope_without_writes(installed, changed):
    store, original = installed
    changes = {}
    if changed in {"identity", "profile"}:
        changes["runtime"] = dataclasses.replace(original.runtime, version="different-profile")
    elif changed == "anchor": changes["qualification_ref"] = "different-anchor"
    elif changed == "expired": changes["observed_at"] = moment(-90000)
    elif changed == "future": changes["observed_at"] = moment(60)
    elif changed == "unhealthy": changes["health"] = "unreachable"
    elif changed == "old_measurement": changes["observed_at"] = original.observed_at
    elif changed == "old_source": changes["context"] = original.context
    elif changed == "larger_context":
        changes["context"] = dataclasses.replace(original.context, safe_context_source="new",
                                                 safe_working_context=8192, semantic_verified_context=8192)
    elif changed == "stronger_role": changes["roles"] = original.roles + ("reviewer",)
    elif changed == "stronger_capability":
        changes["capabilities"] = dataclasses.replace(original.capabilities,
            measured=original.capabilities.measured + ("exact_reference_semantics",))
    elif changed == "ttl": changes["ttl_s"] = original.ttl_s + 1
    desired = candidate(original, **changes)
    options = {"current_identity_digest": "different-material"} if changed == "identity" else {}
    before = bytes_in(store)
    with pytest.raises(CapabilityStoreError): renew(store, original, desired, **options)
    assert bytes_in(store) == before


@pytest.mark.parametrize("checked_at", ["", "invalid", moment(-301), moment(60)])
def test_renewal_requires_current_independent_identity_observation(installed, checked_at):
    store, original = installed
    before = bytes_in(store)
    with pytest.raises(CapabilityStoreError, match="freshly observed"):
        renew(store, original, candidate(original), identity_checked_at=checked_at)
    assert bytes_in(store) == before


def test_identity_expiring_while_waiting_for_lock_cannot_renew(installed, monkeypatch):
    store, original = installed
    desired = candidate(original)
    observed = dt.datetime.now(dt.timezone.utc)
    before = bytes_in(store)
    class Clock:
        calls = 0
        @classmethod
        def now(cls, zone):
            cls.calls += 1
            return observed + dt.timedelta(seconds=301 if cls.calls > 1 else 0)
    monkeypatch.setattr(store_module, "datetime", Clock)
    with pytest.raises(CapabilityStoreError, match="freshly observed"):
        renew(store, original, desired, identity_checked_at=observed.isoformat())
    assert bytes_in(store) == before


def test_cli_status_empty_store_is_read_only_and_renew_retains_anchor(installed, tmp_path):
    store, original = installed
    missing = tmp_path / "missing-status"
    empty = subprocess.run([sys.executable, str(CLI), "--store", str(missing), "status", "--json"],
                           capture_output=True, text=True)
    assert empty.returncode == 0, empty.stderr
    assert json.loads(empty.stdout)["read_only"] and not missing.exists()
    receipt = candidate(original)
    receipt_path = tmp_path / "new-receipt.json"
    receipt_path.write_text(json.dumps(receipt.to_dict()))
    observation_path = tmp_path / "observation.json"
    observation_path.write_text(json.dumps({"profile_id": receipt.profile_id,
        "current_material_identity": receipt.material_identity(), "checked_at": moment(-1)}))
    command = [sys.executable, str(CLI), "--store", store.directory, "renew", str(receipt_path),
               str(observation_path), "--expected-profile", original.profile_id,
               "--expected-receipt", original.receipt_hash]
    before_active = Path(store.active_path).read_bytes()
    applied = subprocess.run(command, capture_output=True, text=True)
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["qualification_ref"] == original.qualification_ref
    assert Path(store.active_path).read_bytes() == before_active
    stale = subprocess.run(command, capture_output=True, text=True)
    assert stale.returncode == 2 and "current receipt changed" in stale.stderr
    assert len(store.entries()) == 2
