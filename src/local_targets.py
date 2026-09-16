"""src/local_targets.py — measured local-Qwen target registry (PS-632).

The defect this module exists to prevent:

    the control plane represented local capacity as ONE thing.

Until now every local route in this stack was spelled ``ollama`` + ``qwen3.8:27b``
(or worse, ``local_qwen``), a label that says nothing about WHICH host, WHICH
runtime version, WHICH quantization, HOW MUCH context actually fits, or whether
the target can call a tool at all. Those are not cosmetic differences. Measured
2026-09-14 (PS-632 probe; both nodes reachable via ssh-to-loopback):

  local-rtx4500  RTX PRO 4500 Blackwell 32GB  ollama 0.32.11  qwen3.8:27b 17.56GB
                 x86_64 i9-12900H, 31.1GB RAM, driver 595.84
                 declared caps ["completion","vision"]        <- NO tools declared
                 proven native tool call: YES (add_numbers a=17 b=25, correct)
                 historical serving context: 32768; current qualified profile: 131072
  local-msr1     MINISFORUM MS-R1   aarch64 12-core 62.3GB   ollama 0.33.3
                 qwen3.8:27b 16.52GB Q4_K_M (parent qwen3.8:27b-q4_K_M)
                 declared caps ["completion","tools","thinking","vision"]
                 proven native tool call: YES (same call, correct)
                 served entirely from system RAM (size_vram 0); cold load 29.0s

Two lessons, both of which changed this module's design:

1. **The declared capability list is NOT authoritative, in EITHER direction.**
   The RTX node advertises no ``tools`` capability and nonetheless emitted a
   correct native tool call; a probe that trusted ``capabilities`` would have
   written off the fastest node in the fleet. So ``native_tools`` is set only
   from a proven call, and the declared list is recorded as a hint, never as
   authority.
2. **The same model tag is not the same capability record.** The healthy-but-
   unprobed assumption that these nodes are interchangeable is what let a single
   ``local_qwen`` label stand in for two machines whose runtime versions,
   quantization, memory path and cold-load latency all differ.

A third measured fact that routing must respect: the *declared* context length
(262144) is not the *serving* window. ``/api/ps`` reported 32768 for the loaded
model on the RTX node. The distinct 2026-09-15 qualification measured 131072
with context-integrity fixtures. The persisted router requires exactly 131072
for the RTX worker, so a lower receipt is refused rather than used as a silent
fallback.

Design rules, all load-bearing:

1. **Every target has a stable identity.** ``target_id`` is explicit and is
   recorded on the pinned execution identity of any run dispatched to it, so
   evidence names the actual host/model/runtime used.
2. **Capability is measured, never assumed, and declaration is not proof.**
   ``native_tools`` is True only from a PROVEN native tool call. A runtime that
   *declares* ``tools`` but fails the probe is recorded with
   ``tools_declared_but_unproven`` — declared-only never routes agent work.
3. **Selection fails closed.** A requirement that no healthy target satisfies
   raises :class:`LocalTargetUnavailable`; it never downgrades to a target
   missing a required capability, and never falls through to a hosted provider.
4. **Two writers never share a worktree.** ``write_scope`` is part of a
   dispatch requirement and a target already holding a write scope is not
   selected for a second one (PS-632 collision control).
5. **The registry is pure and transport-injectable**, so the routing rules are
   deterministic and testable without a live node, while the live probe lives
   behind one small interface (:class:`OllamaInspector`).

Node availability is *not* an assumption either: ``local-framework``
(Framework/Strix Halo) is registered but marked unreachable when a probe cannot
reach it, and selection then routes only to a policy-allowed healthy target or
refuses. It is never silently dropped from the fleet snapshot — a missing node
must be visible, because an invisible node looks like a fleet with nothing to do.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

# --------------------------------------------------------------- constants ---

#: Stable target IDs. These are the identity strings that appear in RunState /
#: evidence. A generic ``local_qwen`` label is explicitly NOT sufficient.
TARGET_RTX_4500 = "local-rtx4500"
TARGET_MSR1 = "local-msr1"
TARGET_FRAMEWORK = "local-framework"

#: The Framework HaloBox Same-GGUF execution profile (PS-624 disposition
#: QUALIFIED_EXPERIMENTAL). This is a PROFILE-scoped target, deliberately separate
#: from TARGET_FRAMEWORK: the Framework HOST keeps no generic inference capability,
#: and a request for HaloBox either resolves to this exact profile or refuses.
TARGET_FRAMEWORK_HALOBOX = "local-framework-halobox"

#: The exact PS-624 profile identity, and the qualification reference that makes the
#: target routable. Both are one string on purpose: the qualification IS the profile.
HALOBOX_PROFILE_ID = (
    "framework/halobox-same-gguf/vulkan/qwen38-flash-next-ud-iq4_xs"
    "@halo-box-29e091e")
HALOBOX_QUALIFICATION_REF = f"ps624-qualified:{HALOBOX_PROFILE_ID}"

#: The sealed PS-624 HaloBox runtime build. A different commit is a different profile.
HALOBOX_RUNTIME_COMMIT = "29e091ea5b228ac1735cde369e68e6767a53e510"

#: HaloBox's own endpoint on the Framework host, taken from the SEALED PS-624 launch
#: command (halobox-control/launch.sh: --host 127.0.0.1 --port 8731). NOT 11434:
#: that is the host's Ollama service and must never stand in for this profile.
HALOBOX_ENDPOINT_PORT = "http://127.0.0.1:8731"

#: The sealed launch also fixes the slot count: -c 262144 --parallel 4 is what
#: splits the configured window into per-request windows (262144 / 4 = 65536).
HALOBOX_PARALLEL_SLOTS = 4

#: The alias Odysseus addresses on this profile. The sealed launch passes no
#: --alias, so llama-server reports the shard PATH as the model id; the addressed
#: alias is that path's basename, and the full served id is kept in the receipt's
#: runtime options as measured evidence.
HALOBOX_MODEL_ALIAS = "Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf"

#: The sealed split-GGUF shards. Identity is per-shard SHA-256 (measured at probe).
HALOBOX_ARTIFACT_PATHS: Tuple[str, ...] = (
    "/mnt/framework-data/models/halogen-flash-same-gguf/UD-IQ4_XS"
    "/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf",
    "/mnt/framework-data/models/halogen-flash-same-gguf/UD-IQ4_XS"
    "/Qwen3.8-Flash-Next-UD-IQ4_XS-00002-of-00003.gguf",
    "/mnt/framework-data/models/halogen-flash-same-gguf/UD-IQ4_XS"
    "/Qwen3.8-Flash-Next-UD-IQ4_XS-00003-of-00003.gguf",
)

#: Transports. Both reachable targets bind ollama to loopback and are therefore
#: only addressable over ssh; a target that later exposes a routable endpoint
#: uses ``http`` and nothing else in this module changes.
TRANSPORT_SSH = "ssh"
TRANSPORT_HTTP = "http"

#: Runtime kinds. ``runtime_kind`` selects the INSPECTOR (measurement) and the
#: CLIENT (invocation), and it is part of the receipt's material identity, so a
#: runtime change is a profile change rather than a silent substitution.
RUNTIME_OLLAMA = "ollama"
RUNTIME_LLAMA_SERVER = "llama-server"

#: Measured health states. ``unreachable`` and ``degraded`` are DIFFERENT from
#: ``unhealthy`` on purpose: the first means we could not ask, the second means
#: we asked and did not like the answer. Conflating them hides a network fault
#: behind a model-quality judgment.
HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_UNREACHABLE = "unreachable"
HEALTH_UNKNOWN = "unknown"

#: Capabilities a packet can REQUIRE of a local target. Requirements are checked
#: against PROVEN capability; unknown requirement names fail closed.
CAP_NATIVE_TOOLS = "native_tools"
CAP_READONLY_ANALYSIS = "readonly_analysis"
CAP_STREAMING = "streaming"

KNOWN_CAPABILITIES = frozenset({CAP_NATIVE_TOOLS, CAP_READONLY_ANALYSIS, CAP_STREAMING})

#: Privacy class for every target in this registry. Local-inference targets are
#: the ONLY class sensitive-domain policy may fall back to, so the class is
#: carried on the record rather than re-derived at each call site.
PRIVACY_LOCAL_ONLY = "local-only"
#: Network reachability class. Both reachable nodes are tailnet/loopback-only.
NETWORK_TAILNET = "tailnet-loopback"

#: Roles a profile may serve. Inference is the ONE that makes a target a worker;
#: everything else is a different kind of compute on the same fleet.
ROLE_INFERENCE = "inference"
ROLE_VERIFIER = "deterministic_verifier"
ROLE_GOVERNANCE = "governance_ci"
ROLE_ARM64_CI = "arm64_ci"

#: Concurrency the operator may safely run per target before throughput is
#: contended. Bounded by measurement, not by hope: the RTX target shares an
#: i9-12900H host with other workloads and the MS-R1 decodes on 12 ARM cores.
DEFAULT_MAX_CONCURRENCY = 1


# ------------------------------------------------------------------- specs ---

@dataclass(frozen=True)
class LocalTargetSpec:
    """A dispatchable local target: WHERE it is, over WHICH transport.

    This half of the record is configuration (stable, operator-visible); the
    measured half is :class:`LocalTargetCapability`. Keeping them apart is what
    makes the probe re-runnable: identity does not change because a node
    rebooted, and a measurement does not silently become configuration.
    """

    target_id: str
    label: str
    ssh_host: str
    endpoint: str
    model: str
    runtime_kind: str = "ollama"
    privacy_class: str = PRIVACY_LOCAL_ONLY
    network_class: str = NETWORK_TAILNET
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    #: Roles this host may serve. A host is not automatically an inference target:
    #: MS-R1's Qwen role is retired (PS-637) and Framework is inference ONLY through
    #: an independently qualified profile, so the role list is registry policy and
    #: the routing seam enforces it.
    roles: Tuple[str, ...] = ()
    #: The qualification behind this host's routability: a PS-624 profile id, a
    #: PS-632 measured receipt, or "" when nothing qualifies it. Empty means NOT
    #: ROUTABLE, and research/Phase-0 metadata never fills this in.
    qualification_ref: str = ""
    #: Artifact files whose CONTENT is part of this profile's identity (a split GGUF
    #: is three files, and a receipt that names only an alias names nothing). The
    #: paths are configuration; their hashes are MEASURED at probe time.
    artifact_paths: Tuple[str, ...] = ()
    #: True when the qualified launch FIXES the served window, so a runtime serving
    #: a different window is a DIFFERENT profile even though "served" is a live fact
    #: rather than material identity. Set on the HaloBox profile, whose sealed launch
    #: is -c 262144; a 131072-server must not answer a 262144-qualified request.
    requires_exact_served_context: bool = False

    @property
    def transport(self) -> str:
        """``http`` only when the endpoint is routable from the control plane.

        An empty ``ssh_host`` means the node is addressable directly; anything
        else must be tunnelled, and pretending otherwise produces a target that
        probes "unreachable" for the wrong reason.
        """
        return TRANSPORT_HTTP if not self.ssh_host else TRANSPORT_SSH

    def to_dict(self) -> dict:
        return {
            "target_id": self.target_id,
            "label": self.label,
            "ssh_host": self.ssh_host,
            "transport": self.transport,
            "endpoint": self.endpoint,
            "model": self.model,
            "runtime_kind": self.runtime_kind,
            "privacy_class": self.privacy_class,
            "network_class": self.network_class,
            "max_concurrency": self.max_concurrency,
            "roles": list(self.roles),
            "qualification_ref": self.qualification_ref,
            "artifact_paths": list(self.artifact_paths),
            "requires_exact_served_context": self.requires_exact_served_context,
        }


#: The registered fleet. Order is NOT dispatch priority — selection is by
#: measured fitness (see :func:`select_local_target`); this order is only the
#: stable presentation order of a fleet snapshot. All three entries share the
#: qwen3.8:27b tag and are still three different machines.
DEFAULT_TARGETS: Tuple[LocalTargetSpec, ...] = (
    LocalTargetSpec(
        target_id=TARGET_RTX_4500,
        label="RTX PRO 4500 Blackwell 32GB (x86_64, i9-12900H)",
        ssh_host="minipc",
        endpoint="http://127.0.0.1:11434",
        model="qwen3.8:27b",
        roles=(ROLE_INFERENCE, ROLE_VERIFIER),
        # Qualified by its own MEASURED receipt (PS-632); see the receipt store.
        qualification_ref="ps632-measured:local-rtx4500",
    ),
    LocalTargetSpec(
        target_id=TARGET_MSR1,
        label="MINISFORUM MS-R1 (aarch64, 12-core, 62GB)",
        ssh_host="msr1",
        endpoint="http://127.0.0.1:11434",
        model="qwen3.8:27b",
        # PS-637: MS-R1's Qwen/inference role is RETIRED. It is registered for
        # deterministic verification and ARM64 governance CI only, and it carries
        # no inference role, so no routing request can select it for generation.
        roles=(ROLE_VERIFIER, ROLE_GOVERNANCE, ROLE_ARM64_CI),
        qualification_ref="ps637-verifier-role",
    ),
    LocalTargetSpec(
        target_id=TARGET_FRAMEWORK,
        label="Framework Desktop (Strix Halo gfx1151, 128GB unified)",
        ssh_host="framework",
        endpoint="http://127.0.0.1:11434",
        model="qwen3.8:27b",
        # Inference ONLY through a profile PS-624 has independently qualified.
        # Until then this host has no qualification reference, so it cannot be
        # selected: research/Phase-0 metadata is not qualification, and there is
        # deliberately no generic "framework" capability.
        roles=(ROLE_INFERENCE,),
        qualification_ref="",
    ),
    LocalTargetSpec(
        target_id=TARGET_FRAMEWORK_HALOBOX,
        label=("Framework Strix Halo / HaloBox same-GGUF Vulkan "
               "(Qwen3.8 Flash-Next UD-IQ4_XS)"),
        ssh_host="framework",
        # HaloBox's OWN endpoint. The Framework Ollama service at 11434 is a
        # different runtime and can never satisfy this profile.
        endpoint=HALOBOX_ENDPOINT_PORT,
        model=HALOBOX_MODEL_ALIAS,
        runtime_kind=RUNTIME_LLAMA_SERVER,
        # Only what the PS-624 qualification actually established: inference.
        roles=(ROLE_INFERENCE,),
        qualification_ref=HALOBOX_QUALIFICATION_REF,
        # Sealed launch: --parallel 4, which is also what splits 262144 into a
        # 65536 per-request slot window.
        max_concurrency=HALOBOX_PARALLEL_SLOTS,
        artifact_paths=HALOBOX_ARTIFACT_PATHS,
        # The sealed launch fixes the served window at 262144; serving anything else
        # is a different execution profile, not a smaller one.
        requires_exact_served_context=True,
    ),
)


def registered_targets() -> Tuple[LocalTargetSpec, ...]:
    """The fleet's identity half. Never returns one collapsed ``local_qwen``."""
    return DEFAULT_TARGETS


def target_by_id(target_id: str) -> Optional[LocalTargetSpec]:
    """Lookup by stable ID. Unknown ID returns ``None`` — callers must refuse."""
    for spec in DEFAULT_TARGETS:
        if spec.target_id == target_id:
            return spec
    return None


# ------------------------------------------------------- measured capability ---

@dataclass
class LocalTargetCapability:
    """The measured half of a target record: what this node can actually do.

    ``native_tools`` is deliberately ``Optional[bool]``. Three states matter and
    two of them are not "no":

      ``True``   proven by a native tool call that returned ``tool_calls``;
      ``False``  asked and refused/incapable — a real negative;
      ``None``   UNPROVEN. Never treat "we did not check" as "it cannot".

    A requirement check treats ``None`` exactly like ``False`` (fail closed) but
    the distinction is preserved on the record so an unprobed node is not
    mistaken for a measured one.
    """

    spec: LocalTargetSpec
    runtime_version: str = ""
    model_id: str = ""
    quantization: str = ""
    declared_context: Optional[int] = None
    #: The context window the runtime is ACTUALLY serving, read from the
    #: resident-model entry in ``/api/ps``. Distinct from ``declared_context``
    #: (the model's maximum): measured 2026-09-14, the RTX node declares 262144
    #: and serves 32768. Sizing a packet from the declared number puts it in a
    #: window that does not exist.
    served_context: Optional[int] = None
    #: Empirically safe working context. Set only from measurement; ``None``
    #: means "not yet established" and callers must not invent a number.
    safe_working_context: Optional[int] = None
    declared_capabilities: Tuple[str, ...] = ()
    #: The exact artifact digest, first-class. Reaching into ``evidence`` for it
    #: (as the routing seam had to) is how a receipt ends up describing a tag.
    model_digest: str = ""
    model_family: str = ""
    size_bytes: int = 0
    #: Draft/MTP/sidecar artifacts whose identity changes semantics.
    auxiliary_artifacts: Tuple[str, ...] = ()
    #: Runtime/parser settings that can change agent semantics, as observed.
    runtime_options: Dict[str, Any] = field(default_factory=dict)
    native_tools: Optional[bool] = None
    thinking: Optional[bool] = None
    vision: Optional[bool] = None
    streaming: Optional[bool] = None
    health: str = HEALTH_UNKNOWN
    last_probe: str = ""
    queue_depth: int = 0
    ttft_s: Optional[float] = None
    prefill_tok_s: Optional[float] = None
    decode_tok_s: Optional[float] = None
    cold_load_s: Optional[float] = None
    size_vram_bytes: Optional[int] = None
    failure_classes: Tuple[str, ...] = ()
    #: The raw observation the record was derived from, kept so a disputed
    #: number can be re-read instead of re-run.
    evidence: dict = field(default_factory=dict)

    @property
    def target_id(self) -> str:
        return self.spec.target_id

    def proven_capabilities(self) -> Tuple[str, ...]:
        """Capabilities backed by evidence. Declaration alone is not enough."""
        caps: List[str] = []
        if self.native_tools is True:
            caps.append(CAP_NATIVE_TOOLS)
        if self.streaming is True:
            caps.append(CAP_STREAMING)
        # Read-only analysis needs no tool channel at all — a completion-only
        # runtime can summarize, classify and review prose. That is exactly why
        # a tool-less target stays USEFUL here instead of being written off.
        if self.health == HEALTH_HEALTHY:
            caps.append(CAP_READONLY_ANALYSIS)
        return tuple(caps)

    def satisfies(self, required: Iterable[str]) -> bool:
        """True only when every requirement is in :meth:`proven_capabilities`.

        Unknown requirement names are a caller bug, not a policy event, so they
        raise rather than quietly matching nothing.
        """
        supplied = set(self.proven_capabilities())
        for cap in required:
            if cap not in KNOWN_CAPABILITIES:
                raise ValueError(f"unknown local-target capability: {cap!r}")
            if cap not in supplied:
                return False
        return True

    def to_dict(self) -> dict:
        return {
            **self.spec.to_dict(),
            "runtime_version": self.runtime_version,
            "model_id": self.model_id,
            "quantization": self.quantization,
            "declared_context": self.declared_context,
            "served_context": self.served_context,
            "safe_working_context": self.safe_working_context,
            "declared_capabilities": list(self.declared_capabilities),
            "model_digest": self.model_digest,
            "model_family": self.model_family,
            "size_bytes": self.size_bytes,
            "auxiliary_artifacts": list(self.auxiliary_artifacts),
            "runtime_options": dict(self.runtime_options),
            "native_tools": self.native_tools,
            "thinking": self.thinking,
            "vision": self.vision,
            "streaming": self.streaming,
            "proven_capabilities": list(self.proven_capabilities()),
            "health": self.health,
            "last_probe": self.last_probe,
            "queue_depth": self.queue_depth,
            "ttft_s": self.ttft_s,
            "prefill_tok_s": self.prefill_tok_s,
            "decode_tok_s": self.decode_tok_s,
            "cold_load_s": self.cold_load_s,
            "size_vram_bytes": self.size_vram_bytes,
            "failure_classes": list(self.failure_classes),
        }


# ============================================================ capability receipt ===
#: Bump when the receipt shape changes in a way a reader must know about.
CAPABILITY_RECEIPT_SCHEMA_VERSION = 1

#: Provenance classes. A receipt records HOW each capability became known, and
#: routing may require a stronger class than "the runtime said so".
PROV_MEASURED = "measured"   # this probe observed it on this exact profile
PROV_DETECTED = "detected"   # the runtime reported a fact about its own state now
PROV_DECLARED = "declared"   # the artifact/config advertises it

#: How many seconds a SEMANTIC qualification stays valid by default. A semantic
#: qualification is expensive to prove (recall ladders, tool proofs), so it is not
#: re-earned on every heartbeat; it expires so a drifted profile cannot inherit it.
DEFAULT_QUALIFICATION_TTL_S = 7 * 24 * 3600
#: Short-lived liveness. Cheap to re-check and cheap to expire.
DEFAULT_HEALTH_TTL_S = 300

#: Invalidation reasons, recorded rather than inferred.
INVALIDATED_EXPIRED = "qualification_expired"
INVALIDATED_FUTURE = "observed_at_in_the_future"
INVALIDATED_IDENTITY_DRIFT = "material_identity_changed"
INVALIDATED_UNHEALTHY = "health_not_healthy"
INVALIDATED_SUPERSEDED = "superseded_by_newer_receipt"
INVALIDATED_SAFE_CONTEXT_UNMEASURED = "safe_working_context_unmeasured"
#: The pool/configured context was recorded as if it were a measured per-request
#: bound (PS-632 reconciliation 2026-09-16: the HaloBox ctx262144 siblings).
INVALIDATED_SAFE_CONTEXT_POOL_MISLABELED = "measured_safe_context_is_the_shared_pool_not_a_served_window"


def _canonical_bytes(payload: object) -> bytes:
    """Deterministic canonical form (the same rule PS-638 uses for its hashes)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def _digest_of(payload: object) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class RuntimeIdentity:
    """The runtime that would execute the work — not the host, and not the model."""

    runtime_kind: str = "ollama"
    provider: str = "ollama"
    endpoint_type: str = TRANSPORT_SSH
    endpoint_url: str = ""
    repository: str = ""
    version: str = ""
    commit: str = ""
    image_digest: str = ""
    backend: str = ""
    backend_version: str = ""

    def to_dict(self) -> dict:
        return {"runtime_kind": self.runtime_kind, "provider": self.provider,
                "endpoint_type": self.endpoint_type, "endpoint_url": self.endpoint_url,
                "repository": self.repository, "version": self.version,
                "commit": self.commit, "image_digest": self.image_digest,
                "backend": self.backend, "backend_version": self.backend_version}


@dataclass(frozen=True)
class ModelIdentity:
    """The exact artifact. A tag is not an artifact; the digest is."""

    model_id: str = ""
    alias: str = ""
    family: str = ""
    digest: str = ""
    size_bytes: int = 0
    quantization: str = ""
    #: Draft/MTP/sidecar/speculation artifacts whose identity changes semantics.
    auxiliary_artifacts: Tuple[str, ...] = ()
    declared_context: int = 0
    declared_capabilities: Tuple[str, ...] = ()

    def is_exact(self) -> bool:
        """A profile is exactly identified only when the artifact digest is known."""
        return bool(self.digest)

    def to_dict(self) -> dict:
        return {"model_id": self.model_id, "alias": self.alias, "family": self.family,
                "digest": self.digest, "size_bytes": self.size_bytes,
                "quantization": self.quantization,
                "auxiliary_artifacts": list(self.auxiliary_artifacts),
                "declared_context": self.declared_context,
                "declared_capabilities": list(self.declared_capabilities)}


@dataclass(frozen=True)
class ContextProfile:
    """Configured vs served vs demonstrated vs verified context — distinct numbers.

    The declared 262144 a model advertises is not a context anyone has run; the
    served window is what the runtime actually hands out; the safe working context
    is the largest one a measurement passed. Only that one routes. Two finer
    distinctions keep an engine demonstration from being read as a semantic
    verification: a throughput benchmark may pass at a depth no semantic probe
    ever ran, and the deepest VERIFIED context is the smaller of the two when
    that is so. PS-632 reconciliation 2026-09-16 (HaloBox): measured-safe 32768
    is ENGINE-DEMONSTRATED (llama-bench depth ladder, throughput only); the
    deepest sealed semantic/context-integrity evidence is 19760 tokens. Neither
    number may erase the other.
    """

    configured_context: int = 0
    served_context: int = 0
    safe_working_context: int = 0
    safe_context_source: str = ""
    #: Deepest context the execution engine demonstrably processed (e.g. a
    #: llama-bench depth ladder with rc=0). Throughput evidence, NOT a semantic
    #: integrity claim.
    engine_demonstrated_context: int = 0
    #: Deepest context a semantic/context-integrity probe (recall, exact-marker,
    #: multi-round) actually verified. May be smaller than the engine bound.
    semantic_verified_context: int = 0
    options: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"configured_context": self.configured_context,
                "served_context": self.served_context,
                "safe_working_context": self.safe_working_context,
                "safe_context_source": self.safe_context_source,
                "engine_demonstrated_context": self.engine_demonstrated_context,
                "semantic_verified_context": self.semantic_verified_context,
                "options": dict(self.options)}


@dataclass(frozen=True)
class CapabilityEvidence:
    """What this profile can do, split by HOW it became known.

    The split is the point: a runtime reporting ``tools`` in its capability list is
    DECLARED, and a validated 32768 context window on a specific artifact is
    MEASURED, and routing may require the stronger class. Collapsing them would let
    a declaration satisfy a proof.
    """

    measured: Tuple[str, ...] = ()
    detected: Tuple[str, ...] = ()
    declared: Tuple[str, ...] = ()
    #: Tool semantics as actually observed, never as advertised.
    tool_semantics: str = "unproven"
    tool_calls_observed: int = 0
    streaming_observed: Optional[bool] = None
    cancellation_observed: Optional[bool] = None
    error_behavior: str = ""

    def to_dict(self) -> dict:
        return {"measured": list(self.measured), "detected": list(self.detected),
                "declared": list(self.declared),
                "tool_semantics": self.tool_semantics,
                "tool_calls_observed": self.tool_calls_observed,
                "streaming_observed": self.streaming_observed,
                "cancellation_observed": self.cancellation_observed,
                "error_behavior": self.error_behavior}


@dataclass(frozen=True)
class CapabilityLimits:
    """Measured load/resource envelope. ``None`` is NOT COLLECTED, never unlimited."""

    max_concurrency: Optional[int] = None
    queue_depth: int = 0
    vram_resident_bytes: Optional[int] = None
    ttft_s: Optional[float] = None
    prefill_tok_s: Optional[float] = None
    decode_tok_s: Optional[float] = None
    cold_load_s: Optional[float] = None

    def to_dict(self) -> dict:
        return {"max_concurrency": self.max_concurrency,
                "queue_depth": self.queue_depth,
                "vram_resident_bytes": self.vram_resident_bytes,
                "ttft_s": self.ttft_s, "prefill_tok_s": self.prefill_tok_s,
                "decode_tok_s": self.decode_tok_s, "cold_load_s": self.cold_load_s}


@dataclass(frozen=True)
class HostBaseline:
    """The material host identity a host-sensitive profile depends on.

    Empty means NOT COLLECTED — never "stable". A ROCm/profile qualification that
    cannot name its kernel/firmware/ROCm build is not reproducible, so the fields
    exist even where the current ollama profile does not need them.
    """

    host_id: str = ""
    label: str = ""
    ssh_host: str = ""
    cpu_arch: str = ""
    gpu: str = ""
    kernel: str = ""
    boot_cmdline_digest: str = ""
    firmware: str = ""
    mesa: str = ""
    rocm: str = ""
    libhsakmt: str = ""

    def to_dict(self) -> dict:
        return {"host_id": self.host_id, "label": self.label,
                "ssh_host": self.ssh_host, "cpu_arch": self.cpu_arch, "gpu": self.gpu,
                "kernel": self.kernel,
                "boot_cmdline_digest": self.boot_cmdline_digest,
                "firmware": self.firmware, "mesa": self.mesa, "rocm": self.rocm,
                "libhsakmt": self.libhsakmt}


class LocalTargetUnavailable(Exception):
    """Typed refusal: no healthy target satisfies the dispatch requirement.

    Raised instead of a downgrade. There is deliberately NO hosted-provider
    fallback here: these targets are the local-only privacy class, and borrowing
    capacity from a hosted provider would move content out of the domain whose
    policy put it here in the first place (PS-605).
    """

    def __init__(self, requirement: dict, reasons: Sequence[str]):
        self.requirement = dict(requirement)
        self.reasons = list(reasons)
        joined = "; ".join(self.reasons) if self.reasons else "no candidates registered"
        super().__init__(f"no local target satisfies {self.requirement}: {joined}")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OllamaInspector:
    """Default live inspector: read one ollama node over its transport.

    It answers four cheap questions and nothing more:
      * ``/api/version`` — is a runtime there at all, and which build;
      * ``/api/tags``    — the model's real identity, quant and declared caps;
      * ``/api/ps``      — what is resident now, i.e. current load;
      * one tool call    — the ONLY evidence that counts for native tools.

    Timing is NOT measured here. A cold 27B load on the ARM node costs minutes,
    and a registry probe that silently spends them gets switched off within a
    day — which would leave the fleet with no measurements at all. Perf numbers
    arrive from the evaluation harness via the observation's ``timings`` key.

    Node inventory is never a cached guess: the whole point of the probe is that
    "what is installed" and "what is loaded" are observed, not configured.
    """

    #: A tool-required packet is only ever offered a target that answered this
    #: prompt with a real ``tool_calls`` array.
    TOOL_PROMPT = (
        "Call the add_numbers tool with a=17 and b=25. "
        "Do not answer in prose. Use the tool."
    )
    TOOL_SCHEMA = {
        "type": "function",
        "function": {
            "name": "add_numbers",
            "description": "Add two integers and return the sum.",
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                "required": ["a", "b"],
            },
        },
    }

    def __init__(self, *, timeout: int = 25, probe_tools: bool = True):
        self.timeout = timeout
        self.probe_tools = probe_tools

    def api(self, spec: LocalTargetSpec, path: str, body: Optional[dict] = None) -> dict:
        """One API call, returning ``{'ok', 'http', 'body', 'err'}``.

        Failure is a value, never an exception: a probe that raises on a dead
        node cannot report the fleet, and an unreported dead node is
        indistinguishable from a node that was never registered.
        """
        url = f"{spec.endpoint.rstrip('/')}{path}"
        if spec.transport == TRANSPORT_HTTP:
            import urllib.request

            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"} if data else {},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return {"ok": True, "http": str(resp.status), "err": "",
                            "body": json.loads(resp.read().decode() or "{}")}
            except Exception as exc:  # noqa: BLE001 — a probe never propagates
                return {"ok": False, "http": "", "body": {}, "err": str(exc)[:200]}

        if body is None:
            remote = f"curl -sS --max-time {self.timeout} '{url}'"
        else:
            remote = (
                f"curl -sS --max-time {self.timeout} '{url}' "
                f"-H 'Content-Type: application/json' -d @-"
            )
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes",
                 "-o", f"ConnectTimeout={min(self.timeout, 8)}",
                 spec.ssh_host, remote],
                input=json.dumps(body) if body is not None else None,
                capture_output=True, text=True, timeout=self.timeout + 10,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"ok": False, "http": "", "body": {}, "err": str(exc)[:200]}
        if proc.returncode != 0:
            return {"ok": False, "http": "", "body": {},
                    "err": (proc.stderr or proc.stdout or "").strip()[:200]}
        try:
            parsed = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return {"ok": False, "http": "", "body": {},
                    "err": f"non-JSON reply: {(proc.stdout or '')[:120]}"}
        return {"ok": True, "http": "200", "body": parsed, "err": ""}

    def _tool_question(self, spec: LocalTargetSpec, *, with_think_key: bool) -> dict:
        body = {
            "model": spec.model,
            "messages": [{"role": "user", "content": self.TOOL_PROMPT}],
            "tools": [self.TOOL_SCHEMA],
            "stream": False,
            "options": {"temperature": 0},
        }
        if with_think_key:
            body["think"] = False
        return self.api(spec, "/api/chat", body)

    def inspect(self, spec: LocalTargetSpec) -> dict:
        """The raw observation :func:`build_capability` turns into a record."""
        raw: dict = {"reachable": False, "failure_classes": []}
        ver = self.api(spec, "/api/version")
        if not ver["ok"]:
            raw["failure_classes"].append("runtime_unreachable")
            raw["error"] = ver["err"]
            return raw
        raw["reachable"] = True
        raw["version"] = ver["body"].get("version") or ""

        tags = self.api(spec, "/api/tags")
        if not tags["ok"]:
            raw["failure_classes"].append("inventory_failed")
            raw["error"] = tags["err"]
            return raw
        entries = tags["body"].get("models") or []
        entry = next((m for m in entries if m.get("name") == spec.model), None)
        if entry is None:
            entry = next((m for m in entries if m.get("model") == spec.model), None)
        if entry is None:
            raw["failure_classes"].append("model_absent")
            raw["available_models"] = [m.get("name") for m in entries]
            return raw
        raw["model"] = entry

        ps = self.api(spec, "/api/ps")
        raw["ps"] = ps["body"] if ps["ok"] else {}

        if self.probe_tools:
            proof = self._tool_question(spec, with_think_key=True)
            if not proof["ok"]:
                # A runtime predating the `think` key must not be scored as
                # tool-less for a VERSION reason; ask the same question again
                # without it so a runtime difference cannot masquerade as a
                # capability difference.
                proof = self._tool_question(spec, with_think_key=False)
            msg = (proof["body"].get("message") or {}) if proof["ok"] else {}
            raw["tool_proof"] = {
                "ok": proof["ok"],
                "tool_calls": len(msg.get("tool_calls") or []),
                "error": proof["err"],
            }
        return raw

class LlamaServerInspector:
    """Live inspector for an OpenAI-compatible llama.cpp server (HaloBox).

    Same RAW-OBSERVATION contract as :class:`OllamaInspector`, so
    :func:`build_capability` and the whole receipt path are SHARED rather than
    duplicated. That is the point of the seam: the runtime dimension changes which
    inspector answers, not what a capability record is.

    Provenance discipline, because this profile's qualification came from somewhere
    else:

      * MEASURED - endpoint health, served context, a real tool call, a real stream,
        and the SHA-256 of every declared artifact read off the target's disk;
      * DETECTED - runtime present, model resident (the server answers ``/props``
        and served a completion);
      * SEALED/QUALIFICATION - the runtime commit, the qualification reference and
        the empirically safe context are supplied by the CALLER as qualification
        inputs. They are never produced here and never relabelled as measurements.

    Artifact hashing is the one expensive step (87 GiB of split GGUF), so it is
    explicit and skippable: ``probe_artifacts=False`` yields a record with no model
    digest, which the receipt path then treats as NOT QUALIFIED. A cheap probe must
    never look like a measured artifact.
    """

    #: Identical prompt and schema to the ollama inspector, deliberately: the tool
    #: proof has to mean the same thing on both runtimes to be comparable at all.
    TOOL_PROMPT = OllamaInspector.TOOL_PROMPT
    TOOL_SCHEMA = OllamaInspector.TOOL_SCHEMA

    def __init__(self, *, timeout: int = 25, probe_tools: bool = True,
                 probe_streaming: bool = True, probe_artifacts: bool = True,
                 artifact_timeout: int = 900, known_identity: Optional[dict] = None):
        self.timeout = timeout
        self.probe_tools = probe_tools
        self.probe_streaming = probe_streaming
        self.probe_artifacts = probe_artifacts
        self.artifact_timeout = artifact_timeout
        #: A previously MEASURED artifact identity ({"shards": [{"path", "sha256",
        #: "size_bytes"}], "digest": ...}), so a liveness HEARTBEAT does not re-hash
        #: 87 GiB of split GGUF every 300 seconds. Reuse is conditional on every
        #: declared path still having its recorded SIZE; any change falls through to
        #: a full re-hash, so a heartbeat can refresh liveness but can never carry
        #: old qualification onto a different artifact.
        self.known_identity = known_identity or {}

    # ------------------------------------------------------------ transport ---
    def api(self, spec: LocalTargetSpec, path: str, body: Optional[dict] = None) -> dict:
        """One API call, returning ``{'ok','http','body','err'}`` (same as ollama)."""
        url = f"{spec.endpoint.rstrip('/')}{path}"
        if spec.transport == TRANSPORT_HTTP:
            import urllib.request

            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(
                url, data=data,
                headers={"Content-Type": "application/json"} if data else {},
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return {"ok": True, "http": str(resp.status), "err": "",
                            "body": json.loads(resp.read().decode() or "{}")}
            except Exception as exc:  # noqa: BLE001 - a probe never propagates
                return {"ok": False, "http": "", "body": {}, "err": str(exc)[:200]}
        remote = self._curl_command(url, body)
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes",
                 "-o", f"ConnectTimeout={min(self.timeout, 8)}",
                 spec.ssh_host, remote],
                input=json.dumps(body) if body is not None else None,
                capture_output=True, text=True, timeout=self.timeout + 10,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"ok": False, "http": "", "body": {}, "err": str(exc)[:200]}
        if proc.returncode != 0:
            return {"ok": False, "http": "", "body": {},
                    "err": (proc.stderr or proc.stdout or "").strip()[:200]}
        try:
            return {"ok": True, "http": "200",
                    "body": json.loads(proc.stdout or "{}"), "err": ""}
        except json.JSONDecodeError:
            return {"ok": False, "http": "", "body": {},
                    "err": f"non-JSON reply: {(proc.stdout or '')[:120]}"}

    @staticmethod
    def _curl_command(url: str, body: Optional[dict] = None) -> str:
        if body is None:
            return f"curl -sS --max-time 25 '{url}'"
        return (f"curl -sS --max-time 25 '{url}' "
                f"-H 'Content-Type: application/json' -d @-")

    def raw_text(self, spec: LocalTargetSpec, path: str, body: Optional[dict],
                 timeout: int = 120) -> tuple:
        """A streaming reply is SSE text, not JSON: read it as text.

        Honours the spec's transport exactly like :meth:`api`, so the same
        inspector serves a tunnelled loopback endpoint and a directly reachable
        one without a second code path.
        """
        url = f"{spec.endpoint.rstrip('/')}{path}"
        if spec.transport == TRANSPORT_HTTP:
            import urllib.request

            req = urllib.request.Request(
                url, data=json.dumps(body).encode() if body is not None else None,
                headers={"Content-Type": "application/json"} if body is not None else {})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return True, resp.read().decode(), ""
            except Exception as exc:  # noqa: BLE001
                return False, "", str(exc)[:200]
        remote = (f"curl -sS -N --max-time {timeout} '{url}' "
                  f"-H 'Content-Type: application/json' -d @-")
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 spec.ssh_host, remote],
                input=json.dumps(body) if body is not None else None,
                capture_output=True, text=True, timeout=timeout + 15,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return False, "", str(exc)[:200]
        if proc.returncode != 0:
            return False, proc.stdout or "", (proc.stderr or "").strip()[:200]
        return True, proc.stdout or "", ""

    # -------------------------------------------------------- artifact identity -
    def _reuse_identity(self, spec: LocalTargetSpec, paths: tuple):
        """Reuse a measured identity IF every declared artifact still has its size.

        Sizes are cheap to read and a changed GGUF necessarily changes size; when a
        size differs this returns ``None`` so the caller does the full hash. What it
        must NEVER do is hand back a digest it cannot tie to the bytes on disk.
        """
        known = list(self.known_identity.get("shards") or [])
        if len(known) != len(paths):
            return None
        by_path = {str(s.get("path") or ""): s for s in known}
        for path in paths:
            if path not in by_path or not int(by_path[path].get("size_bytes") or 0):
                return None
        quoted = " ".join(f"'{p}'" for p in paths)
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 spec.ssh_host, f"stat -c '%s %n' {quoted}"],
                capture_output=True, text=True, timeout=self.timeout + 15,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if proc.returncode != 0:
            return None
        sizes = {}
        for line in (proc.stdout or "").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                try:
                    sizes[parts[1]] = int(parts[0])
                except ValueError:
                    return None
        shards = []
        for path in paths:
            entry = dict(by_path[path])
            if sizes.get(path) != int(entry.get("size_bytes") or 0):
                return None
            shards.append(entry)
        return {"ok": True, "err": "", "shards": shards,
                "digest": str(self.known_identity.get("digest") or ""),
                "total_bytes": sum(int(s.get("size_bytes") or 0) for s in shards),
                "source": "reused_after_size_check"}

    def artifact_identity(self, spec: LocalTargetSpec) -> dict:
        """SHA-256 every declared artifact ON THE TARGET, then compose one digest.

        A split GGUF is three files. A receipt that names only an alias names
        nothing, so the shard hashes are measured where they live and the composite
        digest becomes the model identity that drift detection compares.
        """
        paths = tuple(str(p) for p in (spec.artifact_paths or ()) if str(p).strip())
        if paths and self.known_identity.get("shards"):
            reused = self._reuse_identity(spec, paths)
            if reused is not None:
                return reused
        if not paths:
            return {"ok": False, "err": "no artifact paths declared on the spec",
                    "shards": [], "digest": "", "total_bytes": 0}
        quoted = " ".join(f"'{p}'" for p in paths)
        remote = f"sha256sum {quoted}; echo '--SIZES--'; stat -c '%s %n' {quoted}"
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 spec.ssh_host, remote],
                capture_output=True, text=True, timeout=self.artifact_timeout + 60,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return {"ok": False, "err": str(exc)[:200], "shards": [],
                    "digest": "", "total_bytes": 0}
        if proc.returncode != 0:
            return {"ok": False, "err": (proc.stderr or "").strip()[:200],
                    "shards": [], "digest": "", "total_bytes": 0}
        sizes: Dict[str, int] = {}
        shards: List[dict] = []
        hashes_section = True
        for line in (proc.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            if line == "--SIZES--":
                hashes_section = False
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            if hashes_section:
                shards.append({"path": parts[1].lstrip("*"), "sha256": parts[0]})
            else:
                try:
                    sizes[parts[1]] = int(parts[0])
                except ValueError:
                    continue
        if len(shards) != len(paths):
            return {"ok": False,
                    "err": f"hashed {len(shards)} of {len(paths)} declared artifacts",
                    "shards": shards, "digest": "", "total_bytes": 0}
        for shard in shards:
            shard["size_bytes"] = sizes.get(shard["path"], 0)
            name = shard["path"].rsplit("/", 1)[-1]
            shard["artifact"] = name
        digest = _digest_of([{"artifact": s["artifact"], "sha256": s["sha256"]}
                             for s in shards])
        return {"ok": True, "err": "", "shards": shards, "digest": digest,
                "total_bytes": sum(int(s["size_bytes"] or 0) for s in shards),
                "source": "measured_by_hash"}

    # ------------------------------------------------------------- the probes --
    def _tool_question(self, spec: LocalTargetSpec) -> dict:
        return self.api(spec, "/v1/chat/completions", {
            "model": spec.model,
            "messages": [{"role": "user", "content": self.TOOL_PROMPT}],
            "tools": [self.TOOL_SCHEMA],
            "stream": False,
            "temperature": 0,
            "max_tokens": 256,
        })

    def _completion(self, spec: LocalTargetSpec) -> dict:
        return self.api(spec, "/v1/chat/completions", {
            "model": spec.model,
            "messages": [{"role": "user", "content": "Reply with the single word OK."}],
            "stream": False,
            "temperature": 0,
            "max_tokens": 8,
        })

    def _streaming_probe(self, spec: LocalTargetSpec) -> dict:
        ok, text, err = self.raw_text(spec, "/v1/chat/completions", {
            "model": spec.model,
            "messages": [{"role": "user", "content": "Count from 1 to 5."}],
            "stream": True,
            "max_tokens": 32,
        })
        data_lines = [line for line in (text or "").splitlines()
                      if line.strip().startswith("data:")]
        chunks = [line for line in data_lines if "[DONE]" not in line]
        return {"ok": bool(ok and chunks), "err": err if not chunks else "",
                "data_lines": len(data_lines), "chunks": len(chunks),
                "incremental": len(chunks) > 1}

    def inspect(self, spec: LocalTargetSpec) -> dict:
        """The raw observation :func:`build_capability` turns into a record."""
        raw: dict = {"reachable": False, "failure_classes": []}
        health = self.api(spec, "/health")
        props = self.api(spec, "/props")
        if not health["ok"] and not props["ok"]:
            raw["failure_classes"].append("runtime_unreachable")
            raw["error"] = health["err"] or props["err"]
            return raw
        raw["reachable"] = True
        raw["api_kind"] = "openai-compatible:llama-server"
        raw["health"] = dict(health["body"]) if health["ok"] else {}

        settings = dict((props["body"].get("default_generation_settings") or {}))
        build_info = str(props["body"].get("build_info") or "")
        raw["version"] = build_info
        # /v1/models is where a llama-server states what it is actually serving:
        # the model id, the per-slot context, the parameter count and the runtime's
        # OWN quantisation report. Preferred over parsing the artifact filename,
        # which is a label while this is the runtime's account of itself.
        listing = self.api(spec, "/v1/models")
        served_row: dict = {}
        if listing["ok"]:
            rows = listing["body"].get("data") or listing["body"].get("models") or []
            if rows:
                served_row = dict(rows[0])
        meta = dict(served_row.get("meta") or {})
        served_id = str(served_row.get("id") or served_row.get("name") or "")
        n_ctx = 0
        for candidate in (meta.get("n_ctx"), settings.get("n_ctx"),
                          props["body"].get("n_ctx")):
            if isinstance(candidate, int) and candidate > 0:
                n_ctx = candidate
                break
        model_path = str(props["body"].get("model_path") or served_id)
        slots = props["body"].get("total_slots")

        artifact = (self.artifact_identity(spec) if self.probe_artifacts
                    else {"ok": False, "err": "artifact probe disabled", "shards": [],
                          "digest": "", "total_bytes": 0})
        raw["artifacts"] = artifact
        if not artifact["ok"]:
            # A profile whose artifact identity was not measured has NO identity
            # digest, so the receipt it produces cannot be qualified. Say so in the
            # failure classes instead of quietly emitting an alias-only record.
            raw["failure_classes"].append("artifact_identity_unmeasured")
            raw["artifact_error"] = artifact.get("err", "")
        raw["auxiliary_artifacts"] = tuple(
            f"{s['artifact']}={s['sha256']}" for s in artifact["shards"])

        # Quantisation is read off the artifact FILENAMES (a measured local fact),
        # not copied from prose: the split file names carry UD-IQ4_XS.
        ftype = str(meta.get("ftype") or "")
        names = " ".join(s["artifact"] for s in artifact["shards"]) or model_path
        quantisation = ""
        if ftype:
            # e.g. "IQ4_XS - 4.25 bpw" -> IQ4_XS (the runtime's own report)
            quantisation = ftype.split("-")[0].strip().replace(" ", "_")
        if not quantisation:
            for token in names.replace("-", "_").replace(".", "_").split("_"):
                if token.upper().startswith("IQ") or token.upper().startswith("Q") and any(
                        c.isdigit() for c in token):
                    quantisation = token.upper()
        served_alias = (served_id.rsplit("/", 1)[-1] if served_id.startswith("/")
                        else served_id)
        raw["model"] = {
            "name": served_alias or spec.model,
            "model": served_alias or spec.model,
            "digest": artifact["digest"],
            "size": int(artifact["total_bytes"] or 0),
            "details": {"family": "", "quantization_level": quantisation,
                        # declared_context contractually means the MODEL's own
                        # maximum window. /v1/models meta n_ctx is the PER-SLOT
                        # served window (262144 pool / 4 slots = 65536); the
                        # native window is n_ctx_train. Recording the slot window
                        # here made "declared" mean two different things
                        # (PS-632 reconciliation 2026-09-16).
                        "context_length": (
                            int(meta["n_ctx_train"])
                            if int(meta.get("n_ctx_train") or 0) > 0
                            else (n_ctx or None)),
                        "model_path": model_path},
            "capabilities": ["completion"],
        }
        raw["runtime_options"] = {
            "api_kind": raw["api_kind"], "build_info": build_info,
            "artifact_identity_source": artifact.get("source", "unmeasured"),
            "artifact_shard_sizes": {s["artifact"]: int(s["size_bytes"] or 0)
                                     for s in artifact["shards"]},
            "model_path": model_path, "parallel": slots,
            "served_context": n_ctx or None,
            "served_model_id": served_id or None,
            "runtime_reported_size_bytes": int(meta.get("size") or 0),
            "runtime_reported_params": int(meta.get("n_params") or 0),
            "runtime_reported_ftype": ftype or None,
            "runtime_reported_n_ctx_train": int(meta.get("n_ctx_train") or 0),
            "residency_evidence": (
                "props/model_path present and the server answered a completion"
                if model_path else ""),
        }

        completion = self._completion(spec)
        if not completion["ok"]:
            raw["failure_classes"].append("completion_probe_failed")
            raw["error"] = completion["err"]
        else:
            choices = completion["body"].get("choices") or []
            message = (choices[0].get("message") or {}) if choices else {}
            # Qwen3.8 may spend a short probe's entire budget in the
            # OpenAI-compatible ``reasoning_content`` field. That is still a
            # real assistant completion (and usage is present); treating it as
            # unavailable would make the inspector lie about a live runtime.
            served = bool(message.get("content") or message.get("reasoning_content")
                          or message.get("tool_calls"))
            raw["runtime_options"]["completion_served"] = served
            if not served:
                raw["failure_classes"].append("completion_empty")

        if n_ctx:
            raw["ps"] = {"models": [{"name": spec.model, "model": spec.model,
                                     "context_length": n_ctx}]}
        else:
            raw["ps"] = {}
            raw["failure_classes"].append("served_context_unmeasured")

        if self.probe_tools:
            proof = self._tool_question(spec)
            calls = []
            if proof["ok"]:
                for choice in (proof["body"].get("choices") or []):
                    message = choice.get("message") or {}
                    calls.extend(message.get("tool_calls") or [])
            raw["tool_proof"] = {"ok": proof["ok"], "tool_calls": len(calls),
                                 "error": proof["err"]}
            raw["runtime_options"]["tool_calls_observed"] = len(calls)

        if self.probe_streaming:
            stream = self._streaming_probe(spec)
            raw["streaming"] = bool(stream["ok"])
            raw["runtime_options"]["streaming_chunks"] = stream["chunks"]
            if not stream["ok"]:
                raw["failure_classes"].append("streaming_unproven")
        return raw


def inspector_for(spec: LocalTargetSpec):
    """The inspector a spec's ``runtime_kind`` requires.

    ONE probe path, two runtimes: the registry decides which adapter answers, and
    nothing downstream (capability record, receipt, store, routing) needs to know
    which one it was.
    """
    kind = str(getattr(spec, "runtime_kind", "") or RUNTIME_OLLAMA).strip().lower()
    if kind == RUNTIME_LLAMA_SERVER:
        return LlamaServerInspector()
    return OllamaInspector()

def _apply_timings(rec: LocalTargetCapability, timings: dict) -> LocalTargetCapability:
    """Merge measured timing fields onto a record. Only MEASURED keys are set.

    A missing key never overwrites an existing value with ``None``: "we did not
    measure TTFT this pass" must not erase a TTFT we did measure earlier.
    """
    if not timings:
        return rec
    for name in ("ttft_s", "prefill_tok_s", "decode_tok_s", "cold_load_s"):
        value = timings.get(name)
        if value is not None:
            setattr(rec, name, value)
    return rec


def apply_timings(rec: LocalTargetCapability, timings: dict) -> LocalTargetCapability:
    """Public form of :func:`_apply_timings`, for an evidence harness.

    The registry probe deliberately does NOT time anything (a cold 27B load on
    the ARM node costs minutes and a probe that expensive gets switched off).
    So the measured numbers necessarily arrive from an evaluation harness, and
    they must land in the SAME record that routing reads — otherwise selection
    ranks on fields nobody ever filled in. Measured 2026-09-14: with no timings
    the fitness tie-break falls through to ``target_id`` and picks the 2.83
    tok/s ARM node over the 37.1 tok/s GPU node, which is exactly the failure
    this function exists to prevent.
    """
    return _apply_timings(rec, timings)



def build_capability(
    spec: LocalTargetSpec,
    raw: dict,
    *,
    probed_at: str = "",
) -> LocalTargetCapability:
    """Pure derivation: raw observation -> measured capability record.

    Pure and total on purpose. Every field is a function of the observation, so
    the same observation always produces the same record and a disputed number
    can be re-derived from stored evidence without touching a live node.
    """
    rec = LocalTargetCapability(
        spec=spec,
        last_probe=probed_at or _utc_iso(),
        health=HEALTH_UNKNOWN,
        evidence=raw,
    )
    failures: List[str] = list(raw.get("failure_classes") or [])

    if not raw.get("reachable"):
        rec.health = HEALTH_UNREACHABLE
        rec.failure_classes = tuple(dict.fromkeys(failures or ["runtime_unreachable"]))
        return rec

    rec.runtime_version = raw.get("version") or ""
    model = raw.get("model") or {}
    if not model:
        rec.health = HEALTH_DEGRADED
        rec.failure_classes = tuple(dict.fromkeys(failures or ["model_absent"]))
        return rec

    details = model.get("details") or {}
    declared = tuple(model.get("capabilities") or ())
    rec.model_id = model.get("name") or model.get("model") or spec.model
    rec.model_digest = str(model.get("digest") or "")
    rec.model_family = str(details.get("family") or "")
    rec.size_bytes = int(model.get("size") or 0)
    rec.quantization = details.get("quantization_level") or ""
    rec.declared_context = details.get("context_length")
    rec.declared_capabilities = declared
    rec.runtime_options = dict(raw.get("runtime_options") or {})
    rec.auxiliary_artifacts = tuple(raw.get("auxiliary_artifacts") or ())
    # Declared-only flags: informational. They never satisfy a requirement on
    # their own (see proven_capabilities).
    rec.thinking = "thinking" in declared
    rec.vision = "vision" in declared

    ps = raw.get("ps") or {}
    loaded = ps.get("models") or []
    rec.queue_depth = len(loaded)
    resident = next((m for m in loaded if m.get("name") in (rec.model_id, spec.model)), None)
    if resident:
        rec.size_vram_bytes = resident.get("size_vram")
        # The window actually being served, not the one the model advertises.
        served = resident.get("context_length")
        if isinstance(served, int) and served > 0:
            rec.served_context = served

    # A streamed probe is MEASURED or absent, never assumed: the ollama inspector
    # does not emit this key, so its records still carry ``None`` (unproven) rather
    # than inheriting a flag they did not earn.
    streaming_flag = raw.get("streaming")
    if streaming_flag is not None:
        rec.streaming = bool(streaming_flag)

    proof = raw.get("tool_proof")
    if proof is None:
        # Not asked (or not askable). UNPROVEN, not incapable — the distinction
        # is what stops "we did not check" from being read as a measurement.
        rec.native_tools = None
        failures.append("tools_unproven")
    elif proof.get("ok"):
        rec.native_tools = (proof.get("tool_calls") or 0) > 0
        if not rec.native_tools:
            failures.append(
                "tools_declared_but_unproven" if "tools" in declared
                else "no_native_tools_declared"
            )
    else:
        rec.native_tools = None
        failures.append("tool_probe_failed")

    timings = raw.get("timings") or {}
    _apply_timings(rec, timings)
    # A safe working context is a MEASUREMENT. It is never inferred from the
    # declared maximum, because the declared number is precisely the one that
    # does not survive contact with a real prompt.
    rec.safe_working_context = raw.get("safe_working_context")

    rec.health = HEALTH_HEALTHY
    rec.failure_classes = tuple(dict.fromkeys(failures))
    return rec


def probe_target(
    spec: LocalTargetSpec,
    *,
    inspector: Optional[object] = None,
) -> LocalTargetCapability:
    """Measure one target. An unreachable node is a record, not an exception."""
    inspector = inspector or inspector_for(spec)
    raw = inspector.inspect(spec)
    return build_capability(spec, raw)


def probe_fleet(
    specs: Sequence[LocalTargetSpec] = DEFAULT_TARGETS,
    *,
    inspector: Optional[OllamaInspector] = None,
    timings_by_target: Optional[Dict[str, dict]] = None,
) -> Tuple[LocalTargetCapability, ...]:
    """Measure every registered target, including the ones that fail.

    A node that cannot be reached must still appear in the snapshot. Dropping it
    would make "the fleet is down" and "there is nothing to do" look identical,
    which is how a blocked target silently becomes a scheduling policy.

    ``timings_by_target`` lets an evaluation harness attach the throughput it
    measured (keyed by ``target_id``) so the same records the router reads carry
    real fitness numbers. Without it the records are capability-complete but
    fitness-blind, and selection falls back to a deterministic ID tie-break.
    """
    timings = dict(timings_by_target or {})
    return tuple(
        apply_timings(
            probe_target(spec, inspector=inspector or inspector_for(spec)),
            timings.get(spec.target_id, {}))
        for spec in specs
    )

def _fitness_key(record: LocalTargetCapability) -> tuple:
    """Deterministic dispatch order among equally-eligible targets.

    1. lower live queue depth (prefer idle capacity, not a fixed host order);
    2. higher measured decode throughput — an unmeasured node scores 0.0 and so
       never outranks a measured one on a claim nobody has tested;
    3. target_id ascending, so a tie is stable across runs and two concurrent
       schedulers make the SAME choice instead of racing.
    """
    return (record.queue_depth, -(record.decode_tok_s or 0.0), record.target_id)


def select_local_target(
    required: Iterable[str] = (),
    *,
    records: Optional[Sequence[LocalTargetCapability]] = None,
    specs: Sequence[LocalTargetSpec] = DEFAULT_TARGETS,
    inspector: Optional[OllamaInspector] = None,
    exclude: Iterable[str] = (),
    pin: str = "",
    write_scope: str = "",
    held_write_scopes: Optional[Dict[str, str]] = None,
) -> LocalTargetCapability:
    """Choose one healthy target that PROVABLY satisfies ``required``.

    Returns the full capability record — identity plus measurement — because the
    caller must be able to record which host/model/runtime actually ran the
    packet (a generic ``local_qwen`` label is not sufficient evidence).

    Refusals are typed (:class:`LocalTargetUnavailable`) and carry a per-target
    reason, so a refusal can be told apart from a bug. Nothing here falls back to
    a hosted provider or to a weaker local target: an unmet requirement is a
    refusal, never a downgrade — privacy class, capability and health are all
    hard boundaries.
    """
    for cap in required:
        if cap not in KNOWN_CAPABILITIES:
            raise ValueError(f"unknown local-target capability: {cap!r}")
    held = dict(held_write_scopes or {})
    pool = (
        tuple(records)
        if records is not None
        else probe_fleet(specs, inspector=inspector)
    )
    if pin:
        if target_by_id(pin) is None:
            raise LocalTargetUnavailable({"pin": pin}, [f"unknown target_id {pin!r}"])
        pool = tuple(r for r in pool if r.target_id == pin)

    excluded = set(exclude)
    eligible: List[LocalTargetCapability] = []
    reasons: List[str] = []
    for rec in pool:
        if rec.target_id in excluded:
            reasons.append(f"{rec.target_id}: excluded by caller")
            continue
        if rec.health != HEALTH_HEALTHY:
            detail = ",".join(rec.failure_classes) or "no detail"
            reasons.append(f"{rec.target_id}: health={rec.health} ({detail})")
            continue
        if not rec.satisfies(required):
            missing = [c for c in required if c not in rec.proven_capabilities()]
            reasons.append(f"{rec.target_id}: missing proven capability {'/'.join(missing)}")
            continue
        if write_scope and held.get(rec.target_id) == write_scope:
            # Two writers must never share a write scope. An idle node is not
            # permission to collide: the second writer would edit the first
            # writer's worktree and produce a merge nobody owns.
            reasons.append(f"{rec.target_id}: already holds write scope {write_scope!r}")
            continue
        eligible.append(rec)

    if not eligible:
        requirement = {"required": list(required), "write_scope": write_scope, "pin": pin}
        raise LocalTargetUnavailable(requirement, reasons)
    return sorted(eligible, key=_fitness_key)[0]


def fleet_snapshot(
    records: Sequence[LocalTargetCapability],
    *,
    generated_at: str = "",
) -> dict:
    """JSON-serializable registry snapshot naming every registered target.

    This is the artifact PS-632 asks for: readable by a human or a router
    without re-running the probe, and recording an unreachable node as an
    unreachable NODE rather than as absent capacity.
    """
    return {
        "generated_at": generated_at or _utc_iso(),
        "fleet_size": len(records),
        "healthy": sum(1 for r in records if r.health == HEALTH_HEALTHY),
        "unreachable": sum(1 for r in records if r.health == HEALTH_UNREACHABLE),
        "tool_capable": sum(1 for r in records if r.native_tools is True),
        "targets": [r.to_dict() for r in records],
    }

# ===================================================== canonical capability receipt ===
def _parse_utc(value: str) -> Optional[datetime]:
    """Parse an ISO timestamp, or None when it cannot be trusted as a time."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class TargetCapabilityReceipt:
    """The canonical, hashable, freshness-bound capability record (PS-632).

    Identity is deliberately TWO-level: ``host_id`` is the stable machine
    (``local-rtx4500``), and ``profile_id`` is one exact execution profile on it
    (runtime + backend + artifact digest + quantisation + context). Two profiles on
    one host — the Strix Halo Vulkan and HIP builds, say — are DIFFERENT profiles,
    and neither inherits the other's qualification.

    A receipt is what routing consumes INSTEAD OF a host name or a config
    declaration: it carries the evidence class of every capability, its own
    observation time and TTL, and a material-identity digest that a changed
    runtime/model/context breaks, so old qualification cannot survive drift.
    """

    host_id: str
    profile_id: str
    observed_at: str
    runtime: RuntimeIdentity = field(default_factory=RuntimeIdentity)
    model: ModelIdentity = field(default_factory=ModelIdentity)
    context: ContextProfile = field(default_factory=ContextProfile)
    host: HostBaseline = field(default_factory=HostBaseline)
    capabilities: CapabilityEvidence = field(default_factory=CapabilityEvidence)
    limits: CapabilityLimits = field(default_factory=CapabilityLimits)
    locality: str = PRIVACY_LOCAL_ONLY
    privacy_class: str = PRIVACY_LOCAL_ONLY
    network_class: str = NETWORK_TAILNET
    health: str = HEALTH_UNKNOWN
    health_checked_at: str = ""
    ttl_s: int = DEFAULT_QUALIFICATION_TTL_S
    health_ttl_s: int = DEFAULT_HEALTH_TTL_S
    #: Roles this profile may serve (registry policy, not a measurement).
    roles: Tuple[str, ...] = ()
    #: What qualifies this profile to be routable at all, e.g. a PS-624 profile id.
    qualification_ref: str = ""
    invalidation_reason: str = ""
    supersedes: str = ""
    notes: str = ""
    schema_version: int = CAPABILITY_RECEIPT_SCHEMA_VERSION
    receipt_hash: str = field(default="")

    def __post_init__(self) -> None:
        for name in ("host_id", "profile_id", "observed_at"):
            if not str(getattr(self, name) or "").strip():
                raise LocalTargetUnavailable(
                    {"receipt": name}, [f"capability receipt {name} must be non-empty"])
        if int(self.ttl_s) <= 0:
            raise LocalTargetUnavailable(
                {"receipt": "ttl_s"}, ["a routable receipt needs a positive TTL"])

    def material_identity(self) -> dict:
        """Fields a change to which MUST invalidate prior qualification.

        Runtime, artifact, quantisation, backend, the configured/safe context and the
        host baseline all change what a qualified result MEANS. Timing, health and
        load do not: they are re-measured every heartbeat and never carried.
        """
        return {
            "host_id": self.host_id,
            "runtime": {k: self.runtime.to_dict()[k] for k in (
                "runtime_kind", "repository", "version", "commit", "image_digest",
                "backend", "backend_version")},
            "model": {k: self.model.to_dict()[k] for k in (
                "model_id", "digest", "quantization", "auxiliary_artifacts")},
            "context": {k: self.context.to_dict()[k] for k in (
                "configured_context", "safe_working_context")},
            "host": {k: self.host.to_dict()[k] for k in (
                "kernel", "boot_cmdline_digest", "firmware", "mesa", "rocm",
                "libhsakmt")},
        }

    def identity_digest(self) -> str:
        return _digest_of(self.material_identity())

    def qualification_state(self, *, now: Optional[datetime] = None,
                            current_identity_digest: str = "") -> str:
        """``valid``, or the typed reason it is not — a refusal that explains itself."""
        moment = now or datetime.now(timezone.utc)
        observed = _parse_utc(self.observed_at)
        if observed is None:
            return INVALIDATED_FUTURE
        if observed > moment:
            return INVALIDATED_FUTURE
        if self.invalidation_reason:
            return self.invalidation_reason
        if (moment - observed).total_seconds() > float(self.ttl_s):
            return INVALIDATED_EXPIRED
        if not self.model.is_exact():
            return INVALIDATED_IDENTITY_DRIFT
        if int(self.context.safe_working_context) <= 0:
            return INVALIDATED_SAFE_CONTEXT_UNMEASURED
        if current_identity_digest and current_identity_digest != self.identity_digest():
            return INVALIDATED_IDENTITY_DRIFT
        return "valid"

    def qualification_ok(self, *, now: Optional[datetime] = None,
                         current_identity_digest: str = "") -> bool:
        return self.qualification_state(
            now=now, current_identity_digest=current_identity_digest) == "valid"

    def health_state(self, *, now: Optional[datetime] = None) -> str:
        """Short-lived liveness, separate from the longer semantic qualification.

        A heartbeat refresh must not re-earn a semantic qualification, and it must
        not extend one either: the two clocks are independent on purpose.
        """
        moment = now or datetime.now(timezone.utc)
        checked = _parse_utc(self.health_checked_at or self.observed_at)
        if self.health != HEALTH_HEALTHY:
            return INVALIDATED_UNHEALTHY
        if checked is None:
            return "liveness_unknown"
        if checked > moment:
            return INVALIDATED_FUTURE
        if (moment - checked).total_seconds() > float(self.health_ttl_s):
            return "liveness_expired"
        return "live"

    def measured_capabilities(self) -> Tuple[str, ...]:
        """Only the MEASURED class: a declared tool claim is never in here."""
        return tuple(self.capabilities.measured)

    def to_dict(self) -> dict:
        payload = self.core()
        payload["receipt_hash"] = self.receipt_hash
        return payload

    def core(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "host_id": self.host_id,
            "profile_id": self.profile_id,
            "identity_digest": self.identity_digest(),
            "runtime": self.runtime.to_dict(),
            "model": self.model.to_dict(),
            "context": self.context.to_dict(),
            "host": self.host.to_dict(),
            "capabilities": self.capabilities.to_dict(),
            "limits": self.limits.to_dict(),
            "locality": self.locality,
            "privacy_class": self.privacy_class,
            "network_class": self.network_class,
            "roles": list(self.roles),
            "qualification_ref": self.qualification_ref,
            "observed_at": self.observed_at,
            "ttl_s": int(self.ttl_s),
            "health": self.health,
            "health_checked_at": self.health_checked_at,
            "health_ttl_s": int(self.health_ttl_s),
            "invalidation_reason": self.invalidation_reason,
            "supersedes": self.supersedes,
            "notes": self.notes,
        }

#: Tool semantics, as observed.
TOOLS_PROVEN = "native_call_proven"
TOOLS_DECLARED_ONLY = "declared_but_unproven"
TOOLS_REFUSED = "refused"
TOOLS_UNPROVEN = "unproven"

_RECEIPT_SUBRECORDS = {"runtime": RuntimeIdentity, "model": ModelIdentity,
                       "context": ContextProfile, "host": HostBaseline,
                       "capabilities": CapabilityEvidence, "limits": CapabilityLimits}


def make_target_capability_receipt(**kwargs: Any) -> TargetCapabilityReceipt:
    """Validate, seal and freeze a receipt (fail closed on unknown fields)."""
    from dataclasses import fields as _fields

    known = {f.name for f in _fields(TargetCapabilityReceipt)}
    # receipt_hash and identity_digest are DERIVED: they appear in to_dict()/core()
    # for audit and are recomputed on the way in, so a JSON round-trip rebuilds the
    # same receipt instead of tripping the unknown-field gate.
    derived = {"receipt_hash", "identity_digest"}
    unknown = set(kwargs) - known - derived
    if unknown:
        raise LocalTargetUnavailable(
            {"receipt": "unknown_fields"},
            [f"unknown capability receipt field(s): {sorted(unknown)}"])
    payload = dict(kwargs)
    payload.pop("receipt_hash", None)
    payload.pop("identity_digest", None)
    for name, kind in _RECEIPT_SUBRECORDS.items():
        value = payload.get(name)
        if value is None:
            continue
        if isinstance(value, Mapping):
            payload[name] = kind(**value)
    if payload.get("roles") is not None:
        payload["roles"] = tuple(payload["roles"])
    # profile_id is DERIVED, not carried: it is recomputed from the receipt's own
    # runtime/model/context so an edited identity cannot keep an old profile id and
    # quietly inherit that profile's qualification.
    if payload.get("host_id") and payload.get("observed_at"):
        runtime = payload.get("runtime") or RuntimeIdentity()
        model = payload.get("model") or ModelIdentity()
        context = payload.get("context") or ContextProfile()
        payload["profile_id"] = execution_profile_id(
            host_id=str(payload.get("host_id") or ""),
            runtime_kind=runtime.runtime_kind, backend=runtime.backend,
            model_alias=(model.alias or model.model_id),
            quantization=model.quantization,
            safe_working_context=int(context.safe_working_context or 0),
            model_digest=model.digest,
            runtime_version=runtime.version)
    provisional = TargetCapabilityReceipt(**payload)
    return TargetCapabilityReceipt(
        **{**payload, "receipt_hash": _digest_of(provisional.core())})


def target_capability_receipt_hash_is_valid(payload: Mapping[str, Any]) -> bool:
    """True when a serialized receipt's hash covers exactly its own content."""
    body = {k: v for k, v in dict(payload).items() if k != "receipt_hash"}
    try:
        return str(payload.get("receipt_hash") or "") == _digest_of(body)
    except (TypeError, ValueError):
        return False


def execution_profile_id(*, host_id: str, runtime_kind: str, backend: str,
                         model_alias: str, quantization: str,
                         safe_working_context: int, model_digest: str,
                         runtime_version: str = "") -> str:
    """One exact profile's identity: a change to any input is a DIFFERENT profile.

    The digest and the context are in the id on purpose. Re-tagging a different
    artifact under the same alias, or widening the context, produces a new profile
    id — which is how a previous qualification stops applying instead of being
    quietly inherited.
    """
    return ":".join([
        str(host_id).strip() or "unknown-host",
        f"{str(runtime_kind).strip() or 'runtime'}-{str(backend).strip() or 'backend'}",
        str(runtime_version).strip() or "version-unknown",
        str(model_alias).strip() or "model",
        str(quantization).strip() or "quant-unknown",
        f"ctx{int(safe_working_context or 0)}",
        (str(model_digest).strip() or "digest-unknown")[:12],
    ])

def receipt_from_capability(
    record: LocalTargetCapability,
    *,
    configured_context: int = 0,
    safe_working_context: int = 0,
    safe_context_source: str = "",
    engine_demonstrated_context: int = 0,
    semantic_verified_context: int = 0,
    backend: str = "",
    host_baseline: Optional[Mapping[str, Any]] = None,
    runtime_repository: str = "",
    runtime_commit: str = "",
    runtime_image_digest: str = "",
    ttl_s: int = DEFAULT_QUALIFICATION_TTL_S,
    health_ttl_s: int = DEFAULT_HEALTH_TTL_S,
    observed_at: str = "",
    roles: Sequence[str] = (),
    qualification_ref: str = "",
    limits: Optional[CapabilityLimits] = None,
    notes: str = "",
) -> TargetCapabilityReceipt:
    """One measured record -> the canonical receipt routing consumes.

    ``safe_working_context`` is a MEASUREMENT, not a field copy: if the caller does
    not supply one (or supplies 0), the receipt is built unqualified rather than
    inheriting the model's declared window. That is the difference between "the
    artifact advertises 262144" and "we have run 32768 on this exact profile".
    """
    spec = record.spec
    digest = str(record.model_digest or "")
    declared = tuple(record.declared_capabilities or ())
    tool_semantics = TOOLS_UNPROVEN
    if record.native_tools is True:
        tool_semantics = TOOLS_PROVEN
    elif record.native_tools is False:
        tool_semantics = TOOLS_REFUSED
    elif "tools" in declared:
        tool_semantics = TOOLS_DECLARED_ONLY

    detected: List[str] = []
    if record.runtime_version:
        detected.append("runtime_present")
    if record.size_vram_bytes:
        detected.append("model_resident")
    measured = list(record.proven_capabilities())
    safe = int(safe_working_context or 0)

    profile_id = execution_profile_id(
        host_id=spec.target_id, runtime_kind=(spec.runtime_kind or "ollama"),
        backend=backend, model_alias=(record.model_id or spec.model),
        quantization=record.quantization, safe_working_context=safe,
        model_digest=digest, runtime_version=record.runtime_version)

    return make_target_capability_receipt(
        host_id=spec.target_id, profile_id=profile_id,
        observed_at=observed_at or record.last_probe,
        runtime=RuntimeIdentity(
            runtime_kind=(spec.runtime_kind or "ollama"),
            provider=(spec.runtime_kind or "ollama"), endpoint_type=spec.transport,
            endpoint_url=spec.endpoint, repository=runtime_repository,
            version=record.runtime_version, commit=runtime_commit,
            image_digest=runtime_image_digest,
            backend=backend, backend_version=str(
                (host_baseline or {}).get("backend_version") or "")),
        model=ModelIdentity(
            model_id=record.model_id or spec.model, alias=spec.model,
            family=record.model_family, digest=digest,
            size_bytes=int(record.size_bytes or 0),
            quantization=record.quantization,
            auxiliary_artifacts=tuple(record.auxiliary_artifacts or ()),
            declared_context=int(record.declared_context or 0),
            declared_capabilities=declared),
        context=ContextProfile(
            configured_context=int(configured_context or 0),
            served_context=int(record.served_context or 0),
            safe_working_context=safe, safe_context_source=safe_context_source,
            engine_demonstrated_context=int(engine_demonstrated_context or 0),
            semantic_verified_context=int(semantic_verified_context or 0),
            options=dict(record.runtime_options or {})),
        host=HostBaseline(
            host_id=spec.target_id, label=spec.label, ssh_host=spec.ssh_host,
            gpu=str((host_baseline or {}).get("gpu") or ""),
            cpu_arch=str((host_baseline or {}).get("cpu_arch") or ""),
            kernel=str((host_baseline or {}).get("kernel") or ""),
            boot_cmdline_digest=str(
                (host_baseline or {}).get("boot_cmdline_digest") or ""),
            firmware=str((host_baseline or {}).get("firmware") or ""),
            mesa=str((host_baseline or {}).get("mesa") or ""),
            rocm=str((host_baseline or {}).get("rocm") or ""),
            libhsakmt=str((host_baseline or {}).get("libhsakmt") or "")),
        capabilities=CapabilityEvidence(
            measured=tuple(measured), detected=tuple(detected), declared=declared,
            tool_semantics=tool_semantics,
            tool_calls_observed=1 if record.native_tools is True else 0,
            streaming_observed=record.streaming),
        limits=limits or CapabilityLimits(
            max_concurrency=spec.max_concurrency, queue_depth=int(record.queue_depth),
            vram_resident_bytes=record.size_vram_bytes, ttft_s=record.ttft_s,
            prefill_tok_s=record.prefill_tok_s, decode_tok_s=record.decode_tok_s,
            cold_load_s=record.cold_load_s),
        locality=PRIVACY_LOCAL_ONLY, privacy_class=spec.privacy_class,
        network_class=spec.network_class, health=record.health,
        health_checked_at=record.last_probe, ttl_s=int(ttl_s),
        health_ttl_s=int(health_ttl_s), roles=tuple(roles),
        qualification_ref=qualification_ref, notes=notes)
