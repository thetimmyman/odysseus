"""Read-only projection of a canonical dispatch decision and its evidence."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
from typing import Any, Iterable, Mapping

from src.execution_package import (
    DispatchDecisionReceipt, ExecutionPackage, _canonical,
    package_hash_is_valid,
)
from src.local_targets import (
    TargetCapabilityReceipt, target_capability_receipt_hash_is_valid,
)
from src.provider_capacity import (
    ProviderCapacityReceipt,
    capacity_receipt_hash_is_valid,
)
from src.execution_outcomes import ExecutionOutcomeRecord, validate_outcome_record
from src.outcome_scorecard import aggregate_outcomes


class RoutingInspectionError(ValueError):
    """Canonical routing input is malformed or internally inconsistent."""


def _iso_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _require_time(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise RoutingInspectionError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _dispatch_core(receipt: DispatchDecisionReceipt) -> dict[str, Any]:
    try:
        return receipt.core()
    except Exception as exc:
        raise RoutingInspectionError("dispatch receipt core is malformed") from exc


def _plain(value: Any) -> Any:
    """Make an isolated JSON-compatible copy without accepting arbitrary objects."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise RoutingInspectionError("mapping keys must be strings")
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if is_dataclass(value):
        return _plain(asdict(value))
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return _plain(value.to_dict())
    raise RoutingInspectionError("input contains a non-serializable value")


def _identity_matches(row: Mapping[str, Any], dispatch: DispatchDecisionReceipt) -> bool:
    fields = {
        "target_id": dispatch.selected_target_id,
        "host": dispatch.selected_host,
        "model": dispatch.selected_model,
        "runtime_kind": dispatch.selected_runtime_kind,
        "runtime_version": dispatch.selected_runtime_version,
        "model_digest": dispatch.selected_model_digest,
        "backend": dispatch.selected_backend,
    }
    aliases = {"host": "host", "model": "model", "target_id": "target_id"}
    for key, expected in fields.items():
        actual = row.get(aliases.get(key, key))
        if expected and actual not in (None, "", expected):
            return False
    return True


def _capability_view(receipt: TargetCapabilityReceipt, *, now: datetime,
                     refs: set[str], dispatch: DispatchDecisionReceipt,
                     selected_profile_id: str = "") -> dict[str, Any]:
    payload = receipt.to_dict()
    if not target_capability_receipt_hash_is_valid(payload):
        raise RoutingInspectionError("capability receipt hash is invalid")
    if receipt.receipt_hash not in refs:
        raise RoutingInspectionError("unreferenced capability supplement supplied")
    if (receipt.host_id != dispatch.selected_host or
            (selected_profile_id and receipt.profile_id != selected_profile_id) or
            (receipt.model.model_id and receipt.model.model_id != dispatch.selected_model) or
            (dispatch.selected_runtime_kind and receipt.runtime.runtime_kind != dispatch.selected_runtime_kind) or
            (dispatch.selected_runtime_version and receipt.runtime.version != dispatch.selected_runtime_version) or
            (dispatch.selected_model_digest and receipt.model.digest != dispatch.selected_model_digest) or
            (dispatch.selected_backend and receipt.runtime.backend != dispatch.selected_backend)):
        raise RoutingInspectionError("capability receipt identity conflicts with selected target")
    observed = _iso_time(receipt.observed_at)
    state = receipt.qualification_state(now=now)
    if observed is not None and observed > now:
        state = "invalidated_future_observation"
    return {
        "status": "OBSERVED" if state == "valid" else "UNKNOWN",
        "receipt_hash": receipt.receipt_hash,
        "host_id": receipt.host_id,
        "profile_id": receipt.profile_id,
        "identity_digest": receipt.identity_digest(),
        "observed_at": receipt.observed_at,
        "qualification_state": state,
        "locality": receipt.locality,
        "privacy_class": receipt.privacy_class,
        "network_class": receipt.network_class,
        "runtime": receipt.runtime.to_dict(),
        "model": receipt.model.to_dict(),
        "capabilities": receipt.capabilities.to_dict(),
        "limits": receipt.limits.to_dict(),
        "source": {"qualification_ref": receipt.qualification_ref,
                   "invalidation_reason": receipt.invalidation_reason},
    }


def _capacity_view(receipt: ProviderCapacityReceipt, *, now: datetime,
                   refs: set[str]) -> dict[str, Any]:
    payload = receipt.to_dict()
    if not capacity_receipt_hash_is_valid(payload):
        raise RoutingInspectionError("capacity receipt hash is invalid")
    if receipt.ref not in refs:
        raise RoutingInspectionError("unreferenced capacity supplement supplied")
    try:
        fresh = receipt.facts_are_fresh(now)
    except Exception as exc:
        raise RoutingInspectionError("capacity receipt facts are malformed") from exc
    provenances = [receipt.state_provenance, receipt.entitlement_provenance,
                   receipt.zdr_provenance]
    provenances.extend(quota.provenance for quota in receipt.quotas)
    if receipt.rate_limit is not None:
        provenances.append(receipt.rate_limit.provenance)
    if receipt.price is not None and receipt.price.provenance is not None:
        provenances.append(receipt.price.provenance)
    observations = [_iso_time(provenance.observed_at) if provenance is not None else None
                    for provenance in provenances]
    observed = _iso_time(receipt.observed_at)
    if (observed is None or observed > now or any(
            timestamp is None or timestamp > now for timestamp in observations)):
        fresh = False
    return {
        "status": "OBSERVED" if fresh else "UNKNOWN",
        "receipt_hash": receipt.receipt_hash,
        "provider": receipt.provider,
        "pool_id": receipt.pool_id,
        "account_identity": receipt.account_identity,
        "entitlement": receipt.entitlement.value,
        "state": receipt.state.value,
        "observed_at": receipt.observed_at,
        "ttl_seconds": receipt.ttl_seconds,
        "facts_current": bool(fresh),
        "quotas": _plain([q.to_dict() for q in receipt.quotas]),
        "rate_limit": _plain(receipt.rate_limit.to_dict()) if receipt.rate_limit else None,
        "price": _plain(receipt.price.to_dict()) if receipt.price else None,
        "concurrency_remaining": _plain(receipt.concurrency_remaining),
        "provenance": {
            "evidence_source": receipt.evidence_source,
            "evidence_reference": receipt.evidence_reference,
            "collector_id": receipt.collector_id,
            "state": _plain(receipt.state_provenance),
            "entitlement": _plain(receipt.entitlement_provenance),
            "zdr": _plain(receipt.zdr_provenance),
        },
    }


def project_routing_inspection(
    package: ExecutionPackage,
    dispatch: DispatchDecisionReceipt,
    *,
    capability_receipts: Iterable[TargetCapabilityReceipt] = (),
    capacity_receipts: Iterable[ProviderCapacityReceipt] = (),
    outcomes: Iterable[ExecutionOutcomeRecord] = (),
    now: datetime,
    scoring_version: str = "",
) -> dict[str, Any]:
    """Return an isolated, descriptive view; never selects or authorizes a target."""
    moment = _require_time(now)
    try:
        capability_receipts = tuple(capability_receipts)
        capacity_receipts = tuple(capacity_receipts)
        outcomes = tuple(outcomes)
    except TypeError as exc:
        raise RoutingInspectionError("evidence supplements must be iterable") from exc
    if outcomes and (not isinstance(scoring_version, str) or not scoring_version.strip()):
        raise RoutingInspectionError("scoring_version is required when outcomes are supplied")
    if not isinstance(package, ExecutionPackage) or not isinstance(dispatch, DispatchDecisionReceipt):
        raise RoutingInspectionError("typed package and dispatch receipt are required")
    try:
        package_payload = package.to_dict()
        if not package_hash_is_valid(package_payload):
            raise RoutingInspectionError("execution package hash is invalid")
        core = _dispatch_core(dispatch)
        expected_hash = hashlib.sha256(_canonical(core)).hexdigest()
        if dispatch.receipt_hash != expected_hash:
            raise RoutingInspectionError("dispatch receipt hash is invalid")
        if (dispatch.execution_package_hash != package.package_hash or
                dispatch.run_id != package.run_id or dispatch.packet_id != package.packet_id):
            raise RoutingInspectionError("dispatch receipt is not linked to this package")
        rows = list(dispatch.candidates_considered)
        if any(not isinstance(row, Mapping) for row in rows):
            raise RoutingInspectionError("candidate entries must be mappings")
        seen: set[tuple[str, str]] = set()
        selected_markers = []
        target_rows = []
        for row in rows:
            target_id = row.get("target_id")
            profile_id = row.get("profile_id")
            if (not isinstance(target_id, str) or not target_id.strip() or
                    not isinstance(profile_id, str) or not profile_id.strip()):
                raise RoutingInspectionError("candidate identity must contain nonempty strings")
            for field_name in ("eligible", "selected"):
                if field_name in row and not isinstance(row[field_name], bool):
                    raise RoutingInspectionError(f"candidate {field_name} must be boolean")
            identity = (target_id, profile_id)
            if identity in seen:
                raise RoutingInspectionError("candidate identity is missing or duplicated")
            seen.add(identity)
            if row.get("selected") is True:
                selected_markers.append(identity)
            if identity[0] == dispatch.selected_target_id:
                target_rows.append(row)
        if len(selected_markers) > 1 or (selected_markers and selected_markers[0][0] != dispatch.selected_target_id):
            raise RoutingInspectionError("selected candidate markers contradict receipt")
        if not rows:
            selected_rows = []
        elif selected_markers:
            marked_pair = selected_markers[0]
            selected_rows = [row for row in target_rows
                             if (row["target_id"], row["profile_id"]) == marked_pair]
            if len(selected_rows) != 1:
                raise RoutingInspectionError("selected candidate pair is ambiguous")
        else:
            if len(target_rows) != 1:
                raise RoutingInspectionError("selected target is ambiguous without an exact profile marker")
            selected_rows = target_rows
        if selected_rows and not _identity_matches(selected_rows[0], dispatch):
            raise RoutingInspectionError("selected candidate identity conflicts with receipt")
    except RoutingInspectionError:
        raise
    except Exception as exc:
        raise RoutingInspectionError("canonical routing inputs are malformed") from exc

    cap_refs = set(dispatch.capability_receipt_refs)
    capacity_refs = set(dispatch.capacity_receipt_refs)
    selected_profile_id = str(selected_rows[0].get("profile_id") or "") if selected_rows else ""
    caps = {}
    for item in capability_receipts:
        if not isinstance(item, TargetCapabilityReceipt):
            raise RoutingInspectionError("capability supplements must be canonical typed receipts")
        if item.receipt_hash in caps:
            raise RoutingInspectionError("duplicate capability receipt")
        caps[item.receipt_hash] = _capability_view(
            item, now=moment, refs=cap_refs, dispatch=dispatch,
            selected_profile_id=selected_profile_id)
    capacity = {}
    for item in capacity_receipts:
        if not isinstance(item, ProviderCapacityReceipt):
            raise RoutingInspectionError("capacity supplements must be canonical typed receipts")
        if item.receipt_hash in capacity:
            raise RoutingInspectionError("duplicate capacity receipt")
        capacity[item.receipt_hash] = _capacity_view(item, now=moment, refs=capacity_refs)

    selected_caps = [caps[ref] for ref in sorted(cap_refs) if ref in caps]
    selected_capacity = [capacity[ref.split(":", 1)[1]] for ref in sorted(capacity_refs)
                         if ref.startswith("capacity:") and ref.split(":", 1)[1] in capacity]
    cap_status = ("OBSERVED" if any(row["status"] == "OBSERVED" for row in selected_caps)
                  else "UNKNOWN")
    capacity_status = ("OBSERVED" if any(row["status"] == "OBSERVED" for row in selected_capacity)
                       else "UNKNOWN")
    normalized_outcomes = []
    unassociated = []
    for record in outcomes:
        try:
            valid = validate_outcome_record(record)
            data = valid.to_dict()
            evidence = data.get("raw_evidence_package") or {}
            linked = evidence.get("dispatch_receipts", [])
            dispatch_hashes = {row.get("receipt_hash") for row in linked if isinstance(row, Mapping)}
            pkg = evidence.get("execution_package") or {}
            if (dispatch.receipt_hash in dispatch_hashes and
                    pkg.get("package_hash") == package.package_hash):
                normalized_outcomes.append(valid)
            else:
                unassociated.append(valid.raw_record_hash)
        except Exception as exc:
            raise RoutingInspectionError("outcome is not a valid canonical record") from exc
    scorecard = (aggregate_outcomes(normalized_outcomes, scoring_version=scoring_version)
                 if normalized_outcomes else None)
    view = {
        "schema": "routing-inspection-projection-v1",
        "authoritative": False,
        "execution_package": {"package_hash": package.package_hash,
                               "run_id": package.run_id, "packet_id": package.packet_id,
                               "objective": package.objective,
                               "write_scope": list(package.write_scope),
                               "allowed_tools": list(package.allowed_tools)},
        "dispatch": {
            "receipt_hash": dispatch.receipt_hash,
            "reason": dispatch.reason,
            "decided_by": dispatch.decided_by,
            "policy_ref": dispatch.policy_ref or "UNKNOWN",
            "decided_at": dispatch.decided_at or "UNKNOWN",
            "selected": {
                "target_id": dispatch.selected_target_id,
                "host": dispatch.selected_host,
                "model": dispatch.selected_model,
                "runtime_kind": dispatch.selected_runtime_kind or "UNKNOWN",
                "runtime_version": dispatch.selected_runtime_version or "UNKNOWN",
                "model_digest": dispatch.selected_model_digest or "UNKNOWN",
                "backend": dispatch.selected_backend or "UNKNOWN",
            },
            "envelope": {"tools": list(dispatch.granted_tools),
                         "read_scope": list(dispatch.granted_read_scope),
                         "write_scope": list(dispatch.granted_write_scope),
                         "network_policy": dispatch.network_policy or "UNKNOWN"},
            "candidates_considered": _plain(rows) if rows else "UNKNOWN",
            "fallback": _plain(selected_rows[0].get("fallback_used")) if selected_rows else "UNKNOWN",
        },
        "capabilities": {"status": cap_status, "references": sorted(cap_refs),
                         "supplements": selected_caps},
        "capacity": {"status": capacity_status, "references": sorted(capacity_refs),
                     "supplements": selected_capacity,
                     "billing_authority": False},
        "outcomes": {"status": "OBSERVED" if scorecard else "UNKNOWN",
                     "associated_source_hashes": scorecard["source_record_hashes"] if scorecard else [],
                     "unassociated_source_hashes": sorted(unassociated),
                     "scorecard": scorecard},
        "inspection_time": moment.isoformat(),
    }
    return deepcopy(_plain(view))
