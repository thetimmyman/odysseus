"""Bind an owned operator preference through the normal canonical selector.

This interface does not authenticate a browser, load a profile, start a worker
or refresh evidence. The execution owner must obtain the exact request from
owned canonical state and supply current inputs through its qualified normal
reader. A sealed request is audit data, never an authorization grant.
"""
from dataclasses import replace
import hashlib
import json
import re

from src.canonical_inspector_admission import Principal, UnknownInspection
from src.constants import CANONICAL_TMOS_PACKET_IDS
from src.dispatch_boundary import resolve_from_estate
from src.dispatch_routing import KNOWN_REFUSALS, RoutingRefused, RoutingRequest, ps638_receipt_hash


def _preference(record, actor, request):
    try:
        if type(record) is not dict or not isinstance(actor, Principal) or not isinstance(request, RoutingRequest):
            raise ValueError("unsupported input")
        copied = json.loads(json.dumps(record, allow_nan=False))
        expected = copied.pop("receipt_hash")
        actual = hashlib.sha256(json.dumps(copied, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        if (expected != actual or copied.get("schema") != "operator-intervention-request-v1"
                or copied.get("action") != "prefer_eligible_target" or copied.get("status") != "REQUESTED"
                or copied.get("actor") != actor.auth_key or copied.get("account_epoch") != actor.account_epoch
                or copied.get("packet_id") not in CANONICAL_TMOS_PACKET_IDS
                or copied.get("packet_id") != request.packet_id
                or copied.get("resulting_dispatch_receipt_hash") is not None
                or type(copied.get("requested_profile_id")) is not str
                or not copied["requested_profile_id"].strip()
                or len(copied["requested_profile_id"].encode()) > 1024
                or any(type(copied.get(field)) is not str
                       or re.fullmatch(r"[0-9a-f]{64}", copied[field]) is None
                       for field in ("original_dispatch_receipt_hash", "attempt_receipt_hash",
                                     "execution_package_hash"))):
            raise ValueError("mismatched preference")
        copied["receipt_hash"] = expected
        return copied
    except (ValueError, TypeError, KeyError):
        raise UnknownInspection("inspection unavailable") from None


def select_requested_alternate(request, operator_request, *, actor, estate, capability_store,
                               capacity_receipts=(), policy=None, resources=None,
                               network_classes=None, now=None, decision_id=""):
    """Select only the requested eligible profile and seal its audit association.

    Freshness, privacy, entitlement, permissions and capability remain entirely
    with resolve_from_estate/select_target. Their complete assessments survive.
    A different fallback winner is a refused preference, not an alternate run.
    The returned bound decision still requires normal invocation revalidation
    and a real worker attempt before any completion may be recorded.
    """
    preference = _preference(operator_request, actor, request)
    profile_id = preference["requested_profile_id"]
    requested = replace(request, preferred_profile_ids=(profile_id,))
    bound = resolve_from_estate(
        estate, requested, capability_store=capability_store, capacity_receipts=capacity_receipts,
        policy=policy, resources=resources, network_classes=network_classes, now=now, decision_id=decision_id)
    if bound.decision.selected_profile.profile_id != profile_id:
        assessment = next((item for item in bound.decision.candidates if item.profile_id == profile_id), None)
        if assessment is not None and assessment.rule in KNOWN_REFUSALS:
            raise RoutingRefused(assessment.rule, assessment.reason, assessments=bound.decision.candidates,
                                 run_id=request.run_id, packet_id=request.packet_id)
        raise UnknownInspection("inspection unavailable")
    authority = {
        "authority_schema_version": 1, "acting_principal": actor.auth_key,
        "action": "prefer_eligible_target", "operator_request_receipt_hash": preference["receipt_hash"],
        "original_dispatch_receipt_hash": preference["original_dispatch_receipt_hash"],
        "original_attempt_receipt_hash": preference["attempt_receipt_hash"],
    }
    decision = replace(bound.decision, authority=authority)
    decision = replace(decision, receipt_hash=ps638_receipt_hash(decision.to_ps638_receipt_kwargs()))
    return replace(bound, decision=decision)
