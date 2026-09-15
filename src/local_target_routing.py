"""src/local_target_routing.py — the PS-632 fleet registry feeding PS-605.

This module is the seam, and only the seam:

    src.local_targets.probe_fleet / fleet_snapshot     (PS-632: MEASUREMENT)
        -> routing_inputs(records)                     (here: TRANSLATION)
        -> dispatch_boundary.resolve_from_estate       (PS-605: SELECTION)
        -> the decision's pin                          (what may run)

Three rules hold it together.

**No second chooser.** Nothing here ranks, prefers or falls back. Every ordering
decision belongs to PS-605's single filter path; this module turns measured records
into profiles and receipts and asks PS-605 what to do. `select_local_target` stays
where it is — a library for callers that have no routing policy to consult — and is
deliberately NOT called by the integrated worker path, because two choosers is how
a router stops being authoritative.

**One vocabulary.** The registry names the requirements a packet may state
(``native_tools``, ``readonly_analysis``, ``streaming``) and the network class
(``tailnet-loopback``); the packet, the profile, the receipt and the request all use
those SAME strings. :data:`CAPABILITY_MAP` is the single place a registry name
becomes a PS-605 capability, and an unmapped requirement is a refusal rather than a
silent pass.

**Measured or absent.** A receipt is built from ``LocalTargetCapability`` —
``proven_capabilities()`` (never the declared list), ``health``, ``last_probe`` and
the model digest — and a record that is unhealthy, unprobed or unmeasured produces
no receipt at all, which makes its target ineligible instead of optimistically
available. Freshness is profile-specific: each receipt carries its own probe time
and TTL, so one node's stale measurement cannot be spent as another's.
"""
from __future__ import annotations

import dataclasses
import datetime
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src import dispatch_boundary as dbd
from src import dispatch_routing as dr
from src.local_targets import (
    CAP_NATIVE_TOOLS, CAP_READONLY_ANALYSIS, CAP_STREAMING, HEALTH_HEALTHY,
    KNOWN_CAPABILITIES, NETWORK_TAILNET, PRIVACY_LOCAL_ONLY,
    LocalTargetCapability)

#: Registry requirement name -> the PS-605 capabilities it proves. Total for the
#: registry's known names; anything else refuses (see ``requirement_capabilities``).
CAPABILITY_MAP: Mapping[str, Tuple[str, ...]] = {
    # A proven native tool call is the same measurement PS-605 calls a single
    # tool call. It also proves the packet can act at all, not just read.
    CAP_NATIVE_TOOLS: (dr.CAP_SINGLE_TOOL_CALL,),
    # Completion-only work: the node can summarize/classify/review prose exactly.
    CAP_READONLY_ANALYSIS: (dr.CAP_TEXT_GENERATION, dr.CAP_EXACT_REFERENCE_SEMANTICS),
    CAP_STREAMING: (dr.CAP_STREAMING,),
}

#: Default TTL for a measured capability receipt, in seconds. A measurement is
#: evidence with an expiry: an hour keeps a routing decision tied to a probe a human
#: could still see in the fleet snapshot, without re-probing per dispatch.
DEFAULT_RECEIPT_TTL_S = 3600


class FleetRoutingError(RuntimeError):
    """A registry record (or packet requirement) that cannot become routing input."""


@dataclass(frozen=True)
class FleetRoutingInputs:
    """What a measured fleet contributes to a routing request."""

    profiles: Tuple[Any, ...] = ()
    receipts: Tuple[Any, ...] = ()
    network_classes: Mapping[str, str] = field(default_factory=dict)
    skipped: Tuple[Mapping[str, str], ...] = ()

    def target_ids(self) -> Tuple[str, ...]:
        return tuple(p.target_id for p in self.profiles)


def model_digest_of(record: LocalTargetCapability) -> str:
    """The exact artifact digest from the raw observation, or "" if unrecorded."""
    model = (record.evidence or {}).get("model") or {}
    return str(model.get("digest") or "")


def _parse_probe_time(value: str) -> Optional[datetime.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def requirement_capabilities(required: Sequence[str]) -> Tuple[str, ...]:
    """Translate packet requirements into PS-605 capabilities, fail-closed.

    An unknown requirement name is refused rather than dropped: a packet that asks
    for something the registry cannot name is a packet this seam cannot route.
    """
    translated: List[str] = []
    for name in required:
        if name not in KNOWN_CAPABILITIES:
            raise FleetRoutingError(
                f"unknown target requirement {name!r}; known: "
                f"{sorted(KNOWN_CAPABILITIES)}")
        because = CAPABILITY_MAP.get(name)
        if not because:
            raise FleetRoutingError(
                f"requirement {name!r} has no PS-605 capability mapping")
        translated.extend(because)
    return tuple(dict.fromkeys(translated))


def roles_for_record(record: LocalTargetCapability) -> Tuple[str, ...]:
    """The roles a measured node may serve, derived from PROVEN capability.

    A node with a proven tool call can implement and repair; any healthy node can do
    read-only analysis — which is exactly why a tool-less node stays useful here
    instead of being written off. The derivation is from proof, never from the
    declared capability list, so a runtime that merely advertises tools cannot be
    handed implementer work.
    """
    proven = set(record.proven_capabilities())
    roles: List[str] = []
    if CAP_NATIVE_TOOLS in proven:
        roles.extend([dr.ROLE_IMPLEMENTER, dr.ROLE_REPAIR, dr.ROLE_DEBUGGER,
                      dr.ROLE_REVIEWER])
    if CAP_READONLY_ANALYSIS in proven:
        roles.extend([dr.ROLE_SCOUT, dr.ROLE_SCOUT_ROUTER])
    return tuple(dict.fromkeys(roles))


def _skip_reason(record: LocalTargetCapability, *, now: datetime.datetime) -> str:
    """Why this record cannot become a routing input, or "" when it can."""
    if record.health != HEALTH_HEALTHY:
        detail = ",".join(record.failure_classes) or "no detail"
        return f"health={record.health} ({detail})"
    probed_at = _parse_probe_time(record.last_probe)
    if probed_at is None:
        return "no probe timestamp: an unmeasured target is not a candidate"
    if not record.proven_capabilities():
        return "no proven capability"
    if probed_at > now:
        return f"probe timestamp {record.last_probe} is in the future"
    return ""


def routing_inputs(records: Sequence[LocalTargetCapability], *,
                   ttl_s: int = DEFAULT_RECEIPT_TTL_S,
                   now: Optional[datetime.datetime] = None) -> FleetRoutingInputs:
    """Measured records -> PS-605 profiles and receipts (NO selection).

    Every record either yields a profile WITH a measured, freshness-bound receipt or
    is skipped with a reason the caller can seal. There is no third outcome: a target
    can never become a candidate without evidence for the capabilities it claims.
    """
    moment = now or datetime.datetime.now(datetime.timezone.utc)
    profiles: List[Any] = []
    receipts: List[Any] = []
    skipped: List[Dict[str, str]] = []
    network_classes: Dict[str, str] = {}

    for record in records:
        target_id = record.target_id
        proven = set(record.proven_capabilities())
        reason = _skip_reason(record, now=moment)
        roles = roles_for_record(record)
        if not reason and not roles:
            reason = "no role follows from the proven capability set"
        if reason:
            skipped.append({"target_id": target_id, "reason": reason})
            continue

        capabilities: set = set()
        for name in proven:
            capabilities.update(CAPABILITY_MAP.get(name, ()))
        digest = model_digest_of(record)
        # The registry's own vocabulary, used verbatim on both the profile and the
        # request so a constraint on the network class cannot silently stop matching.
        network_class = record.spec.network_class or NETWORK_TAILNET
        network_classes[target_id] = network_class
        profiles.append(dr.make_target_profile(
            target_id=target_id, profile_id=target_id,
            provider=f"local:{record.spec.ssh_host or record.spec.endpoint}",
            host=record.spec.ssh_host or record.spec.endpoint,
            runtime_kind=record.spec.runtime_kind or "ollama",
            runtime_version=record.runtime_version,
            model=record.model_id or record.spec.model, model_digest=digest,
            locality=dr.LOCALITY_LOCAL, endpoint_url=record.spec.endpoint or "",
            roles=frozenset(roles),
            tools=frozenset({"write_file"}) if CAP_NATIVE_TOOLS in proven
            else frozenset(),
            network_policy=network_class,
            # A node is an inference target when it can generate text at all, and
            # the registry's read-only capability is proof of exactly that.
            inference=bool({dr.CAP_TEXT_GENERATION} & capabilities),
            cost_rank=0, budget_class="local"))
        receipts.append(dr.make_capability_receipt(
            receipt_id=f"measured:{target_id}:{record.last_probe}",
            profile_id=target_id, target_id=target_id,
            capabilities=frozenset(capabilities), exactness=dr.EXACTNESS_EXACT,
            observed_at=record.last_probe, ttl_s=int(ttl_s), healthy=True,
            runtime_version=record.runtime_version, model_digest=digest,
            host=record.spec.ssh_host or record.spec.endpoint,
            notes=("registry: src.local_targets.LocalTargetCapability "
                   f"(proven={','.join(sorted(proven))})"),
            provenance=dr.PROVENANCE_MEASURED))

    return FleetRoutingInputs(profiles=tuple(profiles), receipts=tuple(receipts),
                              network_classes=network_classes,
                              skipped=tuple(skipped))


def request_for_packet(packet: Mapping[str, Any], *, role: str,
                       inputs: FleetRoutingInputs,
                       packet_metadata: Optional[Mapping[str, Any]] = None,
                       execution_package_hash: str = "", run_id: str = "") -> dr.RoutingRequest:
    """The PS-605 request a worker packet implies — from the PACKET, not an operator.

    ``local_only`` is forced: the registry is local-only by construction and the
    PS-635 worker path has no hosted leg, so a hosted candidate could never win here
    even if one were added to the estate. The domain comes from the packet's own
    metadata when it declares one (``policy_domain``) — that is where a sensitive
    packet states its class — otherwise the work is general software engineering.
    """
    metadata = dict(packet_metadata or packet)
    required = tuple(packet.get("target_requirements") or ())
    declared_writes = tuple(packet.get("write_scope") or ())
    return dr.RoutingRequest(
        domain=str(metadata.get("policy_domain") or "general_swe"), role=role,
        run_id=run_id, packet_id=str(packet.get("packet_id") or ""),
        execution_package_hash=execution_package_hash,
        capabilities=requirement_capabilities(required),
        exactness=dr.EXACTNESS_EXACT,
        sensitivity=str(metadata.get("data_sensitivity") or "internal"),
        local_only=True,
        # A writable packet needs the tool channel; a read-only one does not, and
        # asking anyway would refuse a node the registry proves is useful.
        required_tools=("write_file",) if declared_writes else (),
        # The registry's network class, stated by the packet in the same words.
        network_policy=str(metadata.get("network_policy") or NETWORK_TAILNET),
        max_cost_rank=0)


def resolve_fleet_dispatch(records: Sequence[LocalTargetCapability], *,
                           packet: Mapping[str, Any], role: str,
                           execution_package_hash: str = "", run_id: str = "",
                           preferred_target_id: str = "",
                           policy: Any = None,
                           ttl_s: int = DEFAULT_RECEIPT_TTL_S,
                           now: Optional[datetime.datetime] = None,
                           decision_id: str = "") -> Tuple[Any, FleetRoutingInputs]:
    """Ask PS-605 to choose. Returns (BoundDispatch, the inputs it chose from).

    ``preferred_target_id`` is a STATED PREFERENCE, not a pin: PS-605 records it,
    picks it when it is independently eligible, and otherwise records why it could
    not — with the fallback rule and the reason code in the receipt. A preference
    that cannot be satisfied never silently becomes a weaker standard.
    """
    inputs = routing_inputs(records, ttl_s=ttl_s, now=now)
    if not inputs.profiles:
        raise FleetRoutingError(
            "no measured target is routable: "
            f"{[dict(s) for s in inputs.skipped]}")
    request = request_for_packet(
        packet, role=role, inputs=inputs, execution_package_hash=execution_package_hash,
        run_id=run_id)
    if preferred_target_id:
        request = dataclasses.replace(
            request, preferred_profile_ids=(preferred_target_id,))
    bound = dbd.resolve_from_estate(
        dbd.TargetEstate(profiles=inputs.profiles, receipts=inputs.receipts,
                         skipped=inputs.skipped),
        request, network_classes=inputs.network_classes, policy=policy, now=now,
        decision_id=decision_id)
    return bound, inputs



