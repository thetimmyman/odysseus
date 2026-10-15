"""Read-only projection of a canonical dispatch decision and its evidence."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Any, Iterable, Mapping

from src.execution_package import (
    DispatchDecisionReceipt, ExecutionPackage, _canonical,
    package_hash_is_valid,
)
from src.local_targets import (
    TargetCapabilityReceipt, make_target_capability_receipt,
    target_capability_receipt_hash_is_valid,
)
from src.provider_capacity import (
    ProviderCapacityReceipt,
    capacity_receipt_from_dict, capacity_receipt_hash_is_valid,
)
from src.execution_outcomes import ExecutionOutcomeRecord, validate_outcome_record
from src.outcome_scorecard import aggregate_outcomes
from src.attempt_receipt import (
    attempt_receipt_hash_is_valid, recompute_outcome,
    verification_receipt_hash_is_valid,
)
from src.dispatch_boundary import validate_dispatch_evidence
from src.evidence_package import (
    evidence_package_hash_is_valid, validate_evidence_package,
)
from src.source_snapshot import snapshot_digest_is_valid
from src.work_packet import interface_digest_from_normalized


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


def _identity_matches(row: Mapping[str, Any], dispatch: Mapping[str, Any]) -> bool:
    fields = {
        "target_id": dispatch["selected_target_id"],
        "host": dispatch["selected_host"],
        "model": dispatch["selected_model"],
        "runtime_kind": dispatch["selected_runtime_kind"],
        "runtime_version": dispatch["selected_runtime_version"],
        "model_digest": dispatch["selected_model_digest"],
        "backend": dispatch["selected_backend"],
    }
    aliases = {"host": "host", "model": "model", "target_id": "target_id"}
    for key, expected in fields.items():
        actual = row.get(aliases.get(key, key))
        if expected and actual not in (None, "", expected):
            return False
    return True


def _capability_view(receipt: TargetCapabilityReceipt, *, now: datetime,
                     refs: set[str], dispatch: Mapping[str, Any],
                     selected_profile_id: str = "") -> dict[str, Any]:
    payload = receipt.to_dict()
    if not target_capability_receipt_hash_is_valid(payload):
        raise RoutingInspectionError("capability receipt hash is invalid")
    if receipt.receipt_hash not in refs:
        raise RoutingInspectionError("unreferenced capability supplement supplied")
    if (receipt.host_id != dispatch["selected_host"] or
            (selected_profile_id and receipt.profile_id != selected_profile_id) or
            (receipt.model.model_id and receipt.model.model_id != dispatch["selected_model"]) or
            (dispatch["selected_runtime_kind"] and receipt.runtime.runtime_kind != dispatch["selected_runtime_kind"]) or
            (dispatch["selected_runtime_version"] and receipt.runtime.version != dispatch["selected_runtime_version"]) or
            (dispatch["selected_model_digest"] and receipt.model.digest != dispatch["selected_model_digest"]) or
            (dispatch["selected_backend"] and receipt.runtime.backend != dispatch["selected_backend"])):
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


def _outcome_is_bound_to_dispatch(evidence: Mapping[str, Any], *,
                                  dispatch: Mapping[str, Any],
                                  package_hash: str) -> bool:
    """Associate whole records only when every attempt used this exact receipt."""
    package = evidence.get("execution_package")
    receipt_rows = evidence.get("dispatch_receipts")
    attempt_rows = evidence.get("attempt_receipts")
    if (not isinstance(package, Mapping) or package.get("package_hash") != package_hash or
            not isinstance(receipt_rows, list) or not isinstance(attempt_rows, list) or
            not attempt_rows):
        return False
    cited_hashes = {row.get("receipt_hash") for row in receipt_rows
                    if isinstance(row, Mapping)}
    if dispatch["receipt_hash"] not in cited_hashes:
        return False
    for attempt in attempt_rows:
        if not isinstance(attempt, Mapping):
            return False
        if (attempt.get("dispatch_receipt_hash") != dispatch["receipt_hash"] or
                attempt.get("target_id") != dispatch["selected_target_id"] or
                attempt.get("host") != dispatch["selected_host"] or
                attempt.get("model") != dispatch["selected_model"]):
            return False
        for field, selected in (("runtime_kind", dispatch["selected_runtime_kind"]),
                                ("runtime_version", dispatch["selected_runtime_version"])):
            if attempt.get(field) and selected and attempt[field] != selected:
                return False
    return True


def _project_routing_mapping(
    package: Mapping[str, Any],
    dispatch: Mapping[str, Any],
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
    try:
        if not package_hash_is_valid(package):
            raise RoutingInspectionError("execution package hash is invalid")
        core = {key: value for key, value in dispatch.items() if key != "receipt_hash"}
        expected_hash = hashlib.sha256(_canonical(core)).hexdigest()
        if dispatch["receipt_hash"] != expected_hash:
            raise RoutingInspectionError("dispatch receipt hash is invalid")
        if (dispatch["execution_package_hash"] != package["package_hash"] or
                dispatch["run_id"] != package["run_id"] or dispatch["packet_id"] != package["packet_id"]):
            raise RoutingInspectionError("dispatch receipt is not linked to this package")
        rows = list(dispatch["candidates_considered"])
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
            if identity[0] == dispatch["selected_target_id"]:
                target_rows.append(row)
        if len(selected_markers) > 1 or (selected_markers and selected_markers[0][0] != dispatch["selected_target_id"]):
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

    cap_refs = set(dispatch["capability_receipt_refs"])
    capacity_refs = set(dispatch.get("capacity_receipt_refs", ()))
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
            if _outcome_is_bound_to_dispatch(
                    evidence, dispatch=dispatch, package_hash=package["package_hash"]):
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
        "execution_package": {"package_hash": package["package_hash"],
                               "run_id": package["run_id"], "packet_id": package["packet_id"],
                               "objective": package["objective"],
                               "write_scope": list(package["write_scope"]),
                               "allowed_tools": list(package["allowed_tools"])},
        "dispatch": {
            "receipt_hash": dispatch["receipt_hash"],
            "reason": dispatch["reason"],
            "decided_by": dispatch["decided_by"],
            "policy_ref": dispatch["policy_ref"] or "UNKNOWN",
            "decided_at": dispatch["decided_at"] or "UNKNOWN",
            "selected": {
                "target_id": dispatch["selected_target_id"],
                "host": dispatch["selected_host"],
                "model": dispatch["selected_model"],
                "runtime_kind": dispatch["selected_runtime_kind"] or "UNKNOWN",
                "runtime_version": dispatch["selected_runtime_version"] or "UNKNOWN",
                "model_digest": dispatch["selected_model_digest"] or "UNKNOWN",
                "backend": dispatch["selected_backend"] or "UNKNOWN",
            },
            "envelope": {"tools": list(dispatch["granted_tools"]),
                         "read_scope": list(dispatch["granted_read_scope"]),
                         "write_scope": list(dispatch["granted_write_scope"]),
                         "network_policy": dispatch["network_policy"] or "UNKNOWN"},
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
    _require_time(now)
    if not isinstance(package, ExecutionPackage) or not isinstance(dispatch, DispatchDecisionReceipt):
        raise RoutingInspectionError("typed package and dispatch receipt are required")
    try:
        package_payload = package.to_dict()
        dispatch_payload = {**_dispatch_core(dispatch), "receipt_hash": dispatch.receipt_hash}
    except RoutingInspectionError:
        raise
    except Exception as exc:
        raise RoutingInspectionError("canonical routing inputs are malformed") from exc
    return _project_routing_mapping(
        package_payload, dispatch_payload, capability_receipts=capability_receipts,
        capacity_receipts=capacity_receipts, outcomes=outcomes, now=now,
        scoring_version=scoring_version)


def _full_hash(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


def _retained_artifacts(payload: Mapping[str, Any], artifacts: Mapping[str, bytes]) -> dict[str, bytes]:
    """Preflight every canonical locator before the validator can use its loader."""
    if not isinstance(artifacts, Mapping) or any(
            not isinstance(uri, str) or not uri or not isinstance(data, bytes)
            for uri, data in artifacts.items()):
        raise RoutingInspectionError("artifacts require explicit nonempty locators and bytes")
    supplied = dict(artifacts)
    refs = []
    for receipt in payload["attempt_receipts"]:
        refs.extend(receipt.get(key) for key in ("rendered_context_ref", "output_ref")
                    if receipt.get(key) is not None)
        extra = receipt.get("artifact_refs", [])
        if not isinstance(extra, list):
            raise RoutingInspectionError("retained artifact references are malformed")
        refs.extend(extra)
    context = payload.get("acceptance_context") or {}
    baselines = context.get("baseline_receipts", [])
    if not isinstance(baselines, list) or any(not isinstance(row, Mapping) for row in baselines):
        raise RoutingInspectionError("retained baseline receipts are malformed")
    baseline_hashes = set()
    for receipt in baselines:
        digest = receipt.get("receipt_hash")
        if (not _full_hash(digest) or digest in baseline_hashes
                or type(receipt.get("schema_version")) is not int
                or receipt["schema_version"] != 1
                or not verification_receipt_hash_is_valid(receipt)):
            raise RoutingInspectionError("retained baseline receipt integrity is invalid")
        baseline_hashes.add(digest)
    for receipt in [*payload["verification_receipts"], *baselines]:
        refs.extend(receipt.get(key) for key in ("stdout_ref", "stderr_ref")
                    if receipt.get(key) is not None)
    identities = {}
    for ref in refs:
        if not isinstance(ref, Mapping):
            raise RoutingInspectionError("retained artifact reference is malformed")
        uri, digest, size = ref.get("storage_uri"), ref.get("sha256"), ref.get("size")
        if (not isinstance(uri, str) or not uri or not _full_hash(digest)
                or type(size) is not int or size < 0):
            raise RoutingInspectionError("retained artifact identity is malformed")
        if uri in identities and identities[uri] != (digest, size):
            raise RoutingInspectionError("retained artifact locator is conflicting")
        identities[uri] = (digest, size)
        data = supplied.get(uri)
        if data is None or len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise RoutingInspectionError("caller must provide matching bytes for every retained artifact")
    return supplied


def _retained_receipts(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Check original receipt hashes and exact joins, without rebuilding records."""
    package = payload["execution_package"]
    dispatches = {}
    attempts = {}
    numbers = set()
    for field in ("dispatch_receipts", "attempt_receipts", "verification_receipts"):
        rows = payload.get(field)
        if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
            raise RoutingInspectionError("retained receipt collections are malformed")
        seen = set()
        for row in rows:
            digest = row.get("receipt_hash")
            if not _full_hash(digest) or digest in seen or type(row.get("schema_version")) is not int or row["schema_version"] != 1:
                raise RoutingInspectionError("retained receipt identity is malformed or duplicated")
            seen.add(digest)
            if field == "dispatch_receipts":
                for name, prefix in (("capability_receipt_refs", ""),
                                     ("capacity_receipt_refs", "capacity:")):
                    refs = row.get(name, [])
                    if (not isinstance(refs, list)
                            or any(not isinstance(ref, str) or not ref.startswith(prefix)
                                   or not _full_hash(ref[len(prefix):]) for ref in refs)
                            or len(set(refs)) != len(refs)):
                        raise RoutingInspectionError("dispatch supplement references are malformed or duplicated")
                body = {key: value for key, value in row.items() if key != "receipt_hash"}
                valid = hashlib.sha256(_canonical(body)).hexdigest() == digest
                dispatches[digest] = row
            elif field == "attempt_receipts":
                valid = attempt_receipt_hash_is_valid(row)
                number = row.get("attempt")
                if type(number) is not int or number <= 0 or number in numbers:
                    raise RoutingInspectionError("retained attempt ordinal is malformed or duplicated")
                numbers.add(number)
                attempts[digest] = row
            else:
                valid = verification_receipt_hash_is_valid(row)
            if not valid:
                raise RoutingInspectionError("retained receipt hash is invalid")
            if any(row.get(key) != package.get(target) for key, target in (
                    ("execution_package_hash", "package_hash"), ("run_id", "run_id"),
                    ("packet_id", "packet_id"))):
                raise RoutingInspectionError("retained receipt is detached from its package")
    if sorted(numbers) != list(range(1, len(numbers) + 1)):
        raise RoutingInspectionError("retained attempt history is incomplete")
    for attempt in attempts.values():
        dispatch = dispatches.get(attempt.get("dispatch_receipt_hash"))
        if dispatch is None or any(attempt.get(key) != dispatch.get(selected) for key, selected in (
                ("target_id", "selected_target_id"), ("host", "selected_host"),
                ("model", "selected_model"))):
            raise RoutingInspectionError("retained attempt is detached from its dispatch")
        for key, selected in (("runtime_kind", "selected_runtime_kind"),
                              ("runtime_version", "selected_runtime_version"),
                              ("model_version", "selected_model_digest")):
            if attempt.get(key) and dispatch.get(selected) and attempt[key] != dispatch[selected]:
                raise RoutingInspectionError("retained attempt runtime conflicts with its dispatch")
    if any(type(row.get("attempt")) is not int or row["attempt"] not in numbers
           for row in payload["verification_receipts"]):
        raise RoutingInspectionError("retained verification has no matching attempt")
    return dispatches


def _historical_supplements(payload: Mapping[str, Any], dispatches: Mapping[str, Any],
                            selected_hash: str) -> tuple[tuple, tuple]:
    """Only original embedded dispatch evidence can supplement this projection."""
    context = payload.get("acceptance_context")
    if context is None and payload["schema_version"] == 1:
        return (), ()
    if not isinstance(context, Mapping) or not isinstance(context.get("dispatch_evidence"), Mapping):
        raise RoutingInspectionError("historical dispatch evidence is missing")
    histories = context["dispatch_evidence"]
    if set(histories) != set(dispatches):
        raise RoutingInspectionError("historical dispatch evidence does not match retained dispatches")
    selected_caps, selected_capacity = (), ()
    package = payload["execution_package"]
    for digest, history in histories.items():
        if not isinstance(history, Mapping):
            raise RoutingInspectionError("historical dispatch evidence is malformed")
        legacy = history.get("capability_receipts", [])
        if not isinstance(legacy, list) or any(not isinstance(row, Mapping) for row in legacy):
            raise RoutingInspectionError("historical capability views are malformed")
        hashes = [row.get("receipt_hash") for row in legacy]
        profiles = [row.get("profile_id") for row in legacy]
        if (any(not _full_hash(value) for value in hashes)
                or any(not isinstance(value, str) or not value for value in profiles)
                or len(set(hashes)) != len(hashes) or len(set(profiles)) != len(profiles)):
            raise RoutingInspectionError("historical capability views are ambiguous")
        valid, _ = validate_dispatch_evidence(history)
        dispatch = dispatches[digest]
        body = {key: value for key, value in dispatch.items() if key != "receipt_hash"}
        decision = history["decision"]
        decision_body = {key: value for key, value in decision.items()
                         if key not in ("receipt_hash", "decision_hash")}
        decision_hash = hashlib.sha256(_canonical(decision_body)).hexdigest()
        if (not valid or history["seal"]["dispatch_receipt_hash"] != digest
                or history["decision_receipt"] != body
                or history["seal"]["decision_hash"] != decision_hash
                or decision.get("decision_hash") != decision_hash
                or decision.get("request") != history.get("route_request")
                or decision.get("policy") != history.get("policy")
                or not _identity_matches(decision["selected_profile"], dispatch)):
            raise RoutingInspectionError("historical dispatch evidence is invalid or detached")
        for source in (history["route_request"], history["execution_package"]):
            if (source.get("execution_package_hash") != package["package_hash"]
                    or source.get("packet_id") != package["packet_id"]):
                raise RoutingInspectionError("historical dispatch evidence cites another package")
        if history["route_request"].get("run_id") != package["run_id"]:
            raise RoutingInspectionError("historical dispatch evidence cites another run")
        caps, capacity = [], []
        for name in ("canonical_capability_receipts", "capacity_receipts"):
            rows = history.get(name, [])
            if not isinstance(rows, list):
                raise RoutingInspectionError("historical supplements are malformed")
            seen = set()
            for row in rows:
                if not isinstance(row, Mapping) or row.get("receipt_hash") in seen:
                    raise RoutingInspectionError("historical supplement identity is malformed or duplicated")
                seen.add(row.get("receipt_hash"))
                if name == "canonical_capability_receipts":
                    if not target_capability_receipt_hash_is_valid(row):
                        raise RoutingInspectionError("historical capability receipt hash is invalid")
                    receipt = make_target_capability_receipt(**row)
                    if receipt.to_dict() != row:
                        raise RoutingInspectionError("historical capability receipt is not canonical")
                    if receipt.receipt_hash in dispatch["capability_receipt_refs"]:
                        caps.append(receipt)
                else:
                    receipt = capacity_receipt_from_dict(row)
                    if receipt.to_dict() != row:
                        raise RoutingInspectionError("historical capacity receipt is not canonical")
                    if receipt.ref in dispatch.get("capacity_receipt_refs", []):
                        capacity.append(receipt)
        if digest == selected_hash:
            selected_caps, selected_capacity = tuple(caps), tuple(capacity)
    return selected_caps, selected_capacity


def project_retained_routing_inspection(
    payload: Mapping[str, Any],
    *,
    artifacts: Mapping[str, bytes],
    attempt_receipt_hash: str,
    now: datetime,
    outcomes: Iterable[ExecutionOutcomeRecord] = (),
    scoring_version: str = "",
) -> dict[str, Any]:
    """Inspect one exact attempt from explicit retained canonical inputs, without I/O.

    The caller owns authentication, owner association and bounded storage reads.
    This descriptive library result is not a UI disclosure allowlist. No current
    source, profile, grant or capacity lookup is performed. Validation metadata
    reports the existing validator over retained inputs only, not live acceptance.
    Missing legacy supplements stay UNKNOWN; failed/unverified attempts remain
    inspectable when their original hashes and joins are intact.
    """
    moment = _require_time(now)
    if not _full_hash(attempt_receipt_hash):
        raise RoutingInspectionError("attempt selection requires an exact full receipt hash")
    try:
        if not isinstance(payload, Mapping):
            raise RoutingInspectionError("retained evidence must be a mapping")
        # Do not accept custom object serializers or coerce normalized interfaces.
        data = json.loads(json.dumps(dict(payload), allow_nan=False, ensure_ascii=False))
        if type(data.get("schema_version")) is not int or data["schema_version"] not in (1, 2):
            raise RoutingInspectionError("retained evidence schema is unsupported")
        package = data["execution_package"]
        if (not isinstance(package, Mapping) or not evidence_package_hash_is_valid(data)
                or not package_hash_is_valid(package)):
            raise RoutingInspectionError("retained evidence or execution package hash is invalid")
        if any(not isinstance(package.get(key), str) or not package[key].strip()
               for key in ("package_hash", "run_id", "packet_id")):
            raise RoutingInspectionError("retained package identity is malformed")
        interface = package.get("interface")
        if (not isinstance(interface, list) or any(not isinstance(line, str) for line in interface)
                or (package.get("write_scope") and not interface)
                or (interface and package.get("interface_digest") != interface_digest_from_normalized(interface))
                or not snapshot_digest_is_valid(package["source"])):
            raise RoutingInspectionError("retained package interface or source integrity is invalid")
        dispatches = _retained_receipts(data)
        attempts = [row for row in data["attempt_receipts"]
                    if row["receipt_hash"] == attempt_receipt_hash]
        if len(attempts) != 1:
            raise RoutingInspectionError("exact retained attempt is absent or ambiguous")
        attempt = attempts[0]
        supplied = _retained_artifacts(data, artifacts)
        caps, capacity = _historical_supplements(data, dispatches, attempt["dispatch_receipt_hash"])
        validation = validate_evidence_package(data, artifact_extensions=supplied, now=moment)
        integrity_codes = {
            "malformed_record", "schema_version_invalid", "unsupported_schema_version",
            "evidence_package_hash_mismatch", "execution_package_hash_mismatch",
            "receipt_hash_mismatch", "source_snapshot_digest_invalid",
            "source_identity_missing_or_ambiguous",
            "artifact_hash_mismatch", "artifact_unavailable", "secret_shaped_fixture_leak",
            "interface_changed_after_sealing", "requirement_state_tampered",
        }
        if integrity_codes.intersection(validation.codes):
            raise RoutingInspectionError("retained evidence integrity validation failed")
        view = _project_routing_mapping(
            package, dispatches[attempt["dispatch_receipt_hash"]],
            capability_receipts=caps, capacity_receipts=capacity, outcomes=outcomes,
            now=moment, scoring_version=scoring_version)
        view["retained_evidence"] = {
            "evidence_package_hash": data["evidence_package_hash"],
            "attempt_receipt_hash": attempt_receipt_hash,
            "attempt": attempt["attempt"],
            "attempt_verifications": [
                {"receipt_hash": row["receipt_hash"], "outcome": recompute_outcome(row)}
                for row in data["verification_receipts"] if row["attempt"] == attempt["attempt"]],
            "validation": {"ok": validation.ok, "state": validation.state,
                           "codes": list(validation.codes)},
        }
        return view
    except RoutingInspectionError:
        raise
    except Exception as exc:
        raise RoutingInspectionError("retained routing inputs are malformed") from exc
