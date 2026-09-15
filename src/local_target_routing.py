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
    KNOWN_CAPABILITIES, NETWORK_TAILNET, PRIVACY_LOCAL_ONLY, ROLE_INFERENCE,
    LocalTargetCapability, receipt_from_capability)

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


def roles_from_capabilities(capabilities: Iterable[str]) -> Tuple[str, ...]:
    """Roles that follow from PS-605-level capabilities.

    A proven tool channel is what makes a node an implementer/repair/debug/review
    target; text generation alone makes it a read-only analyst. Deriving roles from
    capability (rather than from a spec field) is what stops a role being re-added by
    editing configuration without a new measurement.
    """
    caps = set(capabilities)
    roles: List[str] = []
    if dr.CAP_SINGLE_TOOL_CALL in caps:
        roles.extend([dr.ROLE_IMPLEMENTER, dr.ROLE_REPAIR, dr.ROLE_DEBUGGER,
                      dr.ROLE_REVIEWER])
    if dr.CAP_TEXT_GENERATION in caps:
        roles.extend([dr.ROLE_SCOUT, dr.ROLE_SCOUT_ROUTER])
    return tuple(dict.fromkeys(roles))


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
    """Why this record cannot become a routing input, or "" when it can.

    The role/qualification gate is the registry's topology policy and applies to the
    in-memory path exactly as it does to the persisted one: MS-R1 has no inference
    role, and Framework has no qualification reference, so neither becomes a
    candidate just because a probe answered.
    """
    roles = tuple(record.spec.roles or ())
    if ROLE_INFERENCE not in roles:
        return (f"the registry does not give this host the inference role "
                f"(roles={list(roles)})")
    if not str(record.spec.qualification_ref or "").strip():
        return ("unqualified: no independently qualified profile for this host "
                "(research/Phase-0 metadata is not qualification)")
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




# ================================================ persisted receipts (PS-632 store) ===
@dataclass(frozen=True)
class PersistedRoutingInputs:
    """Routing inputs built from PERSISTED receipts, with every refusal recorded."""

    profiles: Tuple[Any, ...] = ()
    receipts: Tuple[Any, ...] = ()
    network_classes: Mapping[str, str] = field(default_factory=dict)
    skipped: Tuple[Mapping[str, str], ...] = ()
    bound_receipts: Mapping[str, str] = field(default_factory=dict)

    def target_ids(self) -> Tuple[str, ...]:
        return tuple(p.target_id for p in self.profiles)

    def receipt_hash_for(self, target_id: str) -> str:
        return str(self.bound_receipts.get(target_id) or "")


def sync_receipts_from_records(store, records: Sequence[LocalTargetCapability], *,
                               profiles_by_host: Mapping[str, Mapping[str, Any]] = None,
                               ttl_s: int = DEFAULT_RECEIPT_TTL_S,
                               health_ttl_s: int = 300) -> List[Mapping[str, Any]]:
    """MEASURE -> PERSIST. The only writer of the capability store (PS-632).

    Routing never calls this: a router that measures is a router that can make a
    capability appear by wanting it. The discovery command measures and stores; the
    router reads what is stored.

    ``profiles_by_host`` carries the profile-level facts a probe cannot observe
    (configured context, the empirically safe context and its source, backend and
    host baseline). A record with no such entry is stored UNQUALIFIED rather than
    given a plausible default.
    """
    by_host = dict(profiles_by_host or {})
    stored: List[Mapping[str, Any]] = []
    for record in records:
        spec = record.spec
        facts = dict(by_host.get(spec.target_id) or {})
        receipt = receipt_from_capability(
            record,
            configured_context=int(facts.get("configured_context") or 0),
            safe_working_context=int(facts.get("safe_working_context") or 0),
            safe_context_source=str(facts.get("safe_context_source") or ""),
            backend=str(facts.get("backend") or ""),
            host_baseline=facts.get("host_baseline") or {},
            runtime_repository=str(facts.get("runtime_repository") or ""),
            runtime_commit=str(facts.get("runtime_commit") or ""),
            runtime_image_digest=str(facts.get("runtime_image_digest") or ""),
            ttl_s=int(facts.get("ttl_s") or ttl_s),
            health_ttl_s=int(facts.get("health_ttl_s") or health_ttl_s),
            roles=spec.roles, qualification_ref=spec.qualification_ref,
            notes=str(facts.get("notes") or ""))
        # Name what this receipt replaces, so the append-only history carries the
        # link itself rather than leaving a reader to compare timestamps.
        existing = store.current_for_host(spec.target_id)
        stored.append(store.append(
            receipt, supersedes=(existing.receipt_hash if existing else "")))
    return stored


def persisted_routing_inputs(store, *, now=None,
                             specs: Sequence[Any] = None) -> PersistedRoutingInputs:
    """PERSISTED receipts -> PS-605 routing inputs. Reads only; selects nothing.

    A host contributes a candidate only when ALL of these hold, and each failure is
    recorded with its own reason so a refusal is auditable:

      * the registry gives the host the inference role and a qualification ref
        (MS-R1 has neither; Framework has no qualification yet);
      * the store has a current receipt for it (no receipt is not "unlimited");
      * the receipt's qualification is valid — not expired, not future-dated, not
        invalidated, not identity-drifted, with a MEASURED safe context and an exact
        artifact digest;
      * its short-lived liveness is live.
    """
    from src.local_targets import (INVALIDATED_UNHEALTHY, ROLE_INFERENCE,
                                   registered_targets)
    from src.target_capability_store import CapabilityStoreError

    moment = now or datetime.datetime.now(datetime.timezone.utc)
    pool = list(specs) if specs is not None else list(registered_targets())
    profiles: List[Any] = []
    receipts: List[Any] = []
    skipped: List[Dict[str, str]] = []
    network_classes: Dict[str, str] = {}
    bound: Dict[str, str] = {}

    for spec in pool:
        host_id = spec.target_id
        if ROLE_INFERENCE not in (spec.roles or ()):
            skipped.append({"target_id": host_id, "reason": (
                "registry does not give this host the inference role "
                f"(roles={list(spec.roles or ())})")})
            continue
        if not str(spec.qualification_ref or "").strip():
            skipped.append({"target_id": host_id, "reason": (
                "unqualified: no independently qualified profile for this host "
                "(research/Phase-0 metadata is not qualification)")})
            continue
        try:
            receipt = store.current_for_host(host_id)
        except CapabilityStoreError as exc:
            skipped.append({"target_id": host_id,
                            "reason": f"capability store unusable: {exc}"})
            continue
        if receipt is None:
            skipped.append({"target_id": host_id, "reason": (
                "no persisted capability receipt for this host: measure it with "
                "odysseus-capability discover")})
            continue

        state = receipt.qualification_state(now=moment)
        if state != "valid":
            skipped.append({"target_id": host_id, "profile_id": receipt.profile_id,
                            "receipt_hash": receipt.receipt_hash,
                            "reason": f"receipt not qualified: {state}"})
            continue
        health = receipt.health_state(now=moment)
        if health != "live":
            skipped.append({"target_id": host_id, "profile_id": receipt.profile_id,
                            "receipt_hash": receipt.receipt_hash,
                            "reason": f"liveness not live: {health}"})
            continue

        mapped: set = set()
        for name in receipt.capabilities.measured:
            mapped.update(CAPABILITY_MAP.get(name, ()))
        # Roles come from the RECEIPT (registry policy recorded at measurement time),
        # so a role cannot be re-added by editing a spec without a new receipt.
        # The registry's roles are POLICY (they gate routability); PS-605's role
        # vocabulary is what the selector understands, so the profile declares the
        # intersection plus the roles its proven capabilities imply.
        policy_roles = [r for r in (receipt.roles or spec.roles or ())
                        if r in dr.ROLE_CAPABILITIES]
        roles = tuple(dict.fromkeys(
            policy_roles + list(roles_from_capabilities(mapped))))
        profiles.append(dr.make_target_profile(
            target_id=host_id, profile_id=receipt.profile_id,
            provider=receipt.runtime.provider or receipt.runtime.runtime_kind,
            host=receipt.host.ssh_host or spec.ssh_host,
            runtime_kind=receipt.runtime.runtime_kind,
            runtime_version=receipt.runtime.version,
            model=receipt.model.model_id, model_digest=receipt.model.digest,
            locality=dr.LOCALITY_LOCAL, endpoint_url=spec.endpoint or "",
            roles=frozenset(roles),
            tools=frozenset({"write_file"}) if CAP_NATIVE_TOOLS in set(
                receipt.capabilities.measured) else frozenset(),
            network_policy=receipt.network_class,
            inference=True, cost_rank=0, budget_class="local"))
        receipts.append(dr.make_capability_receipt(
            receipt_id=receipt.receipt_hash, profile_id=receipt.profile_id,
            target_id=host_id, capabilities=frozenset(mapped),
            exactness=dr.EXACTNESS_EXACT, observed_at=receipt.observed_at,
            ttl_s=int(receipt.ttl_s), healthy=True,
            runtime_version=receipt.runtime.version,
            model_digest=receipt.model.digest,
            host=receipt.host.ssh_host or spec.ssh_host,
            notes=(f"ps632 persisted receipt {receipt.receipt_hash[:16]} "
                   f"(profile {receipt.profile_id})"),
            provenance=dr.PROVENANCE_MEASURED,
            source_receipt_hash=receipt.receipt_hash))
        network_classes[host_id] = receipt.network_class
        bound[host_id] = receipt.receipt_hash

    return PersistedRoutingInputs(
        profiles=tuple(profiles), receipts=tuple(receipts),
        network_classes=network_classes, skipped=tuple(skipped), bound_receipts=bound)


def resolve_persisted_dispatch(inputs: PersistedRoutingInputs, *,
                               packet: Mapping[str, Any], role: str,
                               execution_package_hash: str = "", run_id: str = "",
                               preferred_target_id: str = "",
                               policy: Any = None,
                               now: Optional[datetime.datetime] = None,
                               decision_id: str = "") -> Any:
    """Ask PS-605 to choose among PERSISTED receipts. Selects nothing itself.

    Takes the already-read inputs (so the caller can seal exactly what routing saw)
    and binds them through PS-605's single selection path. A stated preference is
    still only a preference: PS-605 records whether it could honour it, and the
    caller decides whether a different target is a refusal.
    """
    if not inputs.profiles:
        raise FleetRoutingError(
            "no persisted capability receipt is routable: "
            f"{[dict(s) for s in inputs.skipped]}")
    request = request_for_packet(
        packet, role=role, inputs=inputs, execution_package_hash=execution_package_hash,
        run_id=run_id)
    if preferred_target_id:
        request = dataclasses.replace(
            request, preferred_profile_ids=(preferred_target_id,))
    return dbd.resolve_from_estate(
        dbd.TargetEstate(profiles=inputs.profiles, receipts=inputs.receipts,
                         skipped=inputs.skipped),
        request, network_classes=inputs.network_classes, policy=policy, now=now,
        decision_id=decision_id)
