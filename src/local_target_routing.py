"""Translate the measured local fleet registry into dispatch routing inputs.

    src.local_targets.probe_fleet / fleet_snapshot     (measurement)
        -> routing_inputs(records)                     (here: translation)
        -> dispatch_boundary.resolve_from_estate       (selection)
        -> the decision's pin                          (what may run)

* No second chooser: nothing here ranks or falls back; selection belongs to
  dispatch routing. `select_local_target` is not used on the worker path.
* One vocabulary: :data:`CAPABILITY_MAP` is the only place a registry name
  becomes a routing capability; unmapped requirements are refused.
* Measured or absent: receipts come from ``proven_capabilities()``, health,
  probe time and digest; unhealthy/unprobed/unmeasured records get no receipt.
  Each receipt carries its own probe time and TTL.
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
    LocalTargetCapability, TargetCapabilityReceipt, receipt_from_capability)

#: Registry requirement name -> the routing capabilities it proves; anything
#: else refuses (see ``requirement_capabilities``).
CAPABILITY_MAP: Mapping[str, Tuple[str, ...]] = {
    # A proven native tool call is a single tool call, and proves the packet can act.
    CAP_NATIVE_TOOLS: (dr.CAP_SINGLE_TOOL_CALL,),
    # Completion-only work: the node can summarize/classify/review prose exactly.
    CAP_READONLY_ANALYSIS: (dr.CAP_TEXT_GENERATION, dr.CAP_EXACT_REFERENCE_SEMANTICS),
    CAP_STREAMING: (dr.CAP_STREAMING,),
}

#: Measured receipt TTL: an hour ties decisions to a visible probe without
#: re-probing per dispatch.
DEFAULT_RECEIPT_TTL_S = 3600


class FleetRoutingError(RuntimeError):
    """A registry record (or packet requirement) that cannot become routing input."""


def _legacy_view_from_receipt(receipt: TargetCapabilityReceipt, profile: Any,
                              *, now: Optional[datetime.datetime] = None) -> Any:
    """Project canonical capability evidence into the routing (non-authoritative) view."""
    mapped = set()
    for name in receipt.capabilities.measured:
        mapped.update(CAPABILITY_MAP.get(name, (name,)))
    return dr.make_legacy_capability_view(
        receipt_id=(receipt.receipt_hash or
                    f"ps632:{receipt.profile_id}:{receipt.observed_at}"),
        profile_id=receipt.profile_id,
        target_id=profile.target_id,
        capabilities=frozenset(mapped),
        exactness=dr.EXACTNESS_EXACT, observed_at=receipt.observed_at,
        ttl_s=int(receipt.ttl_s),
        healthy=(receipt.qualification_state(now=now) == "valid"
                 and receipt.health_state(now=now) == "live"),
        runtime_version=receipt.runtime.version, model_digest=receipt.model.digest,
        host=receipt.host.ssh_host or receipt.host_id,
        notes="PS-632 canonical receipt projection",
        provenance=dr.PROVENANCE_MEASURED,
        source_receipt_hash=receipt.receipt_hash)


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
    """Translate packet requirements into routing capabilities; unknown names are
    refused, not dropped."""
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
    """Roles implied by capabilities: a tool channel makes an implementer/repair/
    debug/review target, text alone a read-only analyst. Deriving from capability
    means config edits can't add roles without a new measurement."""
    caps = set(capabilities)
    roles: List[str] = []
    if dr.CAP_SINGLE_TOOL_CALL in caps:
        roles.extend([dr.ROLE_IMPLEMENTER, dr.ROLE_REPAIR, dr.ROLE_DEBUGGER,
                      dr.ROLE_REVIEWER])
    if dr.CAP_TEXT_GENERATION in caps:
        roles.extend([dr.ROLE_SCOUT, dr.ROLE_SCOUT_ROUTER])
    return tuple(dict.fromkeys(roles))


def roles_for_record(record: LocalTargetCapability) -> Tuple[str, ...]:
    """Roles derived from proven capability, never the declared list: a proven
    tool call allows implement/repair; any healthy node can do read-only analysis."""
    proven = set(record.proven_capabilities())
    roles: List[str] = []
    if CAP_NATIVE_TOOLS in proven:
        roles.extend([dr.ROLE_IMPLEMENTER, dr.ROLE_REPAIR, dr.ROLE_DEBUGGER,
                      dr.ROLE_REVIEWER])
    if CAP_READONLY_ANALYSIS in proven:
        roles.extend([dr.ROLE_SCOUT, dr.ROLE_SCOUT_ROUTER])
    return tuple(dict.fromkeys(roles))


def _skip_reason(record: LocalTargetCapability, *, now: datetime.datetime) -> str:
    """Why this record can't become a routing input, or "". The registry's
    role/qualification gate applies here too, so a node without an inference
    role or qualification ref never becomes a candidate just because it answered."""
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
    """Measured records -> routing profiles and receipts (no selection). Each
    record yields a profile with a fresh measured receipt or a sealable skip reason."""
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
        # Use the registry's vocabulary verbatim on profile and request so network
        # constraints keep matching.
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
            # Read-only capability proves text generation, i.e. an inference target.
            inference=bool({dr.CAP_TEXT_GENERATION} & capabilities),
            cost_rank=0, budget_class="local"))
        receipts.append(dr.make_legacy_capability_view(
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
    """The routing request a worker packet implies. ``local_only`` is forced (the
    registry and worker path are local-only). The domain comes from the packet's
    ``policy_domain`` if declared, else general software engineering."""
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
        # Only writable packets need the tool channel; requiring it otherwise
        # would refuse useful nodes.
        required_tools=("write_file",) if declared_writes else (),
        network_policy=str(metadata.get("network_policy") or NETWORK_TAILNET),
        max_cost_rank=0)


def resolve_fleet_dispatch(records: Sequence[LocalTargetCapability], *,
                           packet: Mapping[str, Any], role: str,
                           execution_package_hash: str = "", run_id: str = "",
                           preferred_target_id: str = "",
                           policy: Any = None,
                           ttl_s: int = DEFAULT_RECEIPT_TTL_S,
                           capability_store: Any = None,
                           now: Optional[datetime.datetime] = None,
                           decision_id: str = "") -> Tuple[Any, FleetRoutingInputs]:
    """Ask the dispatch router to choose; returns (BoundDispatch, its inputs).
    ``preferred_target_id`` is a preference, not a pin: honoured only when
    independently eligible, otherwise recorded with the reason."""
    if capability_store is None:
        raise FleetRoutingError(
            "canonical PS-632 capability store is required for dispatch")
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
        request, capability_store=capability_store,
        network_classes=inputs.network_classes, policy=policy, now=now,
        decision_id=decision_id)
    return bound, inputs




@dataclass(frozen=True)
class PersistedRoutingInputs:
    """Routing inputs built from PERSISTED receipts, with every refusal recorded."""

    profiles: Tuple[Any, ...] = ()
    receipts: Tuple[Any, ...] = ()
    network_classes: Mapping[str, str] = field(default_factory=dict)
    skipped: Tuple[Mapping[str, str], ...] = ()
    bound_receipts: Mapping[str, str] = field(default_factory=dict)
    capability_store: Any = None

    def target_ids(self) -> Tuple[str, ...]:
        return tuple(p.target_id for p in self.profiles)

    def receipt_hash_for(self, target_id: str) -> str:
        return str(self.bound_receipts.get(target_id) or "")


def sync_receipts_from_records(store, records: Sequence[LocalTargetCapability], *,
                               profiles_by_host: Mapping[str, Mapping[str, Any]] = None,
                               ttl_s: int = DEFAULT_RECEIPT_TTL_S,
                               health_ttl_s: int = 300) -> List[Mapping[str, Any]]:
    """Measure and persist: the only writer of the capability store. Routing never
    calls this, so it can't make a capability appear by wanting it.

    ``profiles_by_host`` supplies profile facts a probe can't observe; records
    without one are stored unqualified rather than defaulted.
    """
    by_host = dict(profiles_by_host or {})
    stored: List[Mapping[str, Any]] = []
    for record in records:
        spec = record.spec
        facts = dict(by_host.get(spec.target_id) or {})
        receipt = receipt_from_capability(
            record,
            configured_context=int(facts.get("configured_context") or 0),
            configured_served_context=int(
                facts.get("configured_served_context") or 0),
            safe_working_context=int(facts.get("safe_working_context") or 0),
            safe_context_source=str(facts.get("safe_context_source") or ""),
            engine_demonstrated_context=int(facts.get("engine_demonstrated_context") or 0),
            semantic_verified_context=int(facts.get("semantic_verified_context") or 0),
            backend=str(facts.get("backend") or ""),
            host_baseline=facts.get("host_baseline") or {},
            runtime_repository=str(facts.get("runtime_repository") or ""),
            runtime_commit=str(facts.get("runtime_commit") or ""),
            runtime_image_digest=str(facts.get("runtime_image_digest") or ""),
            ttl_s=int(facts.get("ttl_s") or ttl_s),
            health_ttl_s=int(facts.get("health_ttl_s") or health_ttl_s),
            roles=spec.roles, qualification_ref=spec.qualification_ref,
            notes=str(facts.get("notes") or ""))
        existing = store.current_for_host(spec.target_id)
        stored.append(store.append(
            receipt, supersedes=(existing.receipt_hash if existing else "")))
    return stored


def persisted_routing_inputs(store, *, now=None,
                             specs: Sequence[Any] = None) -> PersistedRoutingInputs:
    """Persisted receipts -> routing inputs (read-only). A host is a candidate only if:

      * the registry grants it the inference role and a qualification ref;
      * the store has a current receipt (no receipt is not "unlimited");
      * the qualification is valid: not expired, future-dated, invalidated or
        identity-drifted, with a measured safe context and exact digest;
      * its short-lived liveness is live.

    Each failure is recorded with its own reason.
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
        # Roles come from the receipt, so spec edits can't add roles. The profile
        # declares registry roles intersected with the router's vocabulary, plus
        # those its proven capabilities imply.
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
            runtime_commit=receipt.runtime.commit,
            runtime_image_digest=receipt.runtime.image_digest,
            model=receipt.model.model_id, model_digest=receipt.model.digest,
            backend=receipt.runtime.backend,
            backend_version=receipt.runtime.backend_version,
            locality=dr.LOCALITY_LOCAL, endpoint_url=spec.endpoint or "",
            endpoint_type=receipt.runtime.endpoint_type,
            runtime_options=receipt.context.options,
            configured_context=receipt.context.configured_context,
            configured_served_context=receipt.context.configured_served_context,
            roles=frozenset(roles),
            tools=frozenset({"write_file"}) if CAP_NATIVE_TOOLS in set(
                receipt.capabilities.measured) else frozenset(),
            network_policy=receipt.network_class,
            inference=True, cost_rank=0, budget_class="local"))
        receipts.append(dr.make_legacy_capability_view(
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
        network_classes=network_classes, skipped=tuple(skipped), bound_receipts=bound,
        capability_store=store)


def resolve_persisted_dispatch(inputs: PersistedRoutingInputs, *,
                               packet: Mapping[str, Any], role: str,
                               execution_package_hash: str = "", run_id: str = "",
                               preferred_target_id: str = "",
                               policy: Any = None,
                               now: Optional[datetime.datetime] = None,
                               decision_id: str = "") -> Any:
    """Ask the router to choose among persisted receipts. Takes pre-read inputs so
    the caller can seal exactly what routing saw; a preference is only recorded."""
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
        request, capability_store=inputs.capability_store,
        network_classes=inputs.network_classes, policy=policy, now=now,
        decision_id=decision_id)
