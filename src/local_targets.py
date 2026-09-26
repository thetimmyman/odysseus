"""Measured local-model target registry.

A shared model tag is not a capability record: hosts differ in runtime
version, quantization, memory path, served context and tool support, so each
target is identified and measured separately.

* The declared capability list is not authoritative in either direction (a
  node advertising no ``tools`` may make correct tool calls), so
  ``native_tools`` is set only from a proven call.
* The declared context length is not the served window; ``safe_working_context``
  stays ``None`` until measured.

Rules:

1. ``target_id`` is a stable identity recorded on every pinned run.
2. Capability is measured; declared-but-unproven tools are recorded as
   ``tools_declared_but_unproven`` and never route agent work.
3. Selection fails closed with :class:`LocalTargetUnavailable`, never
   downgrading or falling through to a hosted provider.
4. A target holding a write scope is never selected for a second one.
5. The registry is pure; the live probe sits behind :class:`OllamaInspector`.

Unreachable nodes stay in the fleet snapshot, marked unreachable, so a missing
node is visible.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.endpoint_identity import canonical_endpoint_identity


#: Stable target IDs recorded in RunState/evidence.
TARGET_RTX_4500 = "local-rtx4500"
TARGET_MSR1 = "local-msr1"
TARGET_FRAMEWORK = "local-framework"

#: Ollama binds loopback on current nodes, so they're reached over ssh; a
#: routable endpoint would use ``http``.
TRANSPORT_SSH = "ssh"
TRANSPORT_HTTP = "http"

#: ``unreachable``/``degraded`` differ from ``unhealthy``: couldn't ask vs didn't
#: like the answer. Conflating them hides network faults as model quality.
HEALTH_HEALTHY = "healthy"
HEALTH_DEGRADED = "degraded"
HEALTH_UNREACHABLE = "unreachable"
HEALTH_UNKNOWN = "unknown"

#: Capabilities a packet can require, checked against proven capability;
#: unknown names fail closed.
CAP_NATIVE_TOOLS = "native_tools"
CAP_READONLY_ANALYSIS = "readonly_analysis"
CAP_STREAMING = "streaming"

KNOWN_CAPABILITIES = frozenset({CAP_NATIVE_TOOLS, CAP_READONLY_ANALYSIS, CAP_STREAMING})

#: Local inference is the only class sensitive-domain policy may fall back to;
#: carried on the record rather than re-derived.
PRIVACY_LOCAL_ONLY = "local-only"
#: Network reachability class; current nodes are tailnet/loopback-only.
NETWORK_TAILNET = "tailnet-loopback"

#: Roles a profile may serve; only inference makes a target a worker.
ROLE_INFERENCE = "inference"
ROLE_VERIFIER = "deterministic_verifier"
ROLE_GOVERNANCE = "governance_ci"
ROLE_ARM64_CI = "arm64_ci"

#: Safe per-target concurrency before throughput is contended, from measurement.
DEFAULT_MAX_CONCURRENCY = 1


@dataclass(frozen=True)
class LocalTargetSpec:
    """A dispatchable target's configuration: where it is and over which
    transport. Kept apart from the measured :class:`LocalTargetCapability` so
    probes are re-runnable and measurements never become configuration."""

    target_id: str
    label: str
    ssh_host: str
    endpoint: str
    model: str
    runtime_kind: str = "ollama"
    privacy_class: str = PRIVACY_LOCAL_ONLY
    network_class: str = NETWORK_TAILNET
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    #: Roles this host may serve (registry policy enforced by the routing seam);
    #: a host isn't automatically an inference target.
    roles: Tuple[str, ...] = ()
    #: The qualification behind routability (a profile id or measured receipt);
    #: empty means not routable.
    qualification_ref: str = ""

    @property
    def transport(self) -> str:
        """``http`` only when directly routable; any ``ssh_host`` means tunnelled."""
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
        }


#: The registered fleet. Order is presentation only; selection is by measured
#: fitness (:func:`select_local_target`).
DEFAULT_TARGETS: Tuple[LocalTargetSpec, ...] = (
    LocalTargetSpec(
        target_id=TARGET_RTX_4500,
        label="RTX PRO 4500 Blackwell 32GB (x86_64, i9-12900H)",
        ssh_host="minipc",
        endpoint="http://127.0.0.1:11434",
        model="qwen3.8:27b",
        roles=(ROLE_INFERENCE, ROLE_VERIFIER),
        # Qualified by its own measured receipt.
        qualification_ref="ps632-measured:local-rtx4500",
    ),
    LocalTargetSpec(
        target_id=TARGET_MSR1,
        label="MINISFORUM MS-R1 (aarch64, 12-core, 62GB)",
        ssh_host="msr1",
        endpoint="http://127.0.0.1:11434",
        model="qwen3.8:27b",
        # No inference role: deterministic verification and ARM64 CI only, so it
        # can never be selected for generation.
        roles=(ROLE_VERIFIER, ROLE_GOVERNANCE, ROLE_ARM64_CI),
        qualification_ref="ps637-verifier-role",
    ),
    LocalTargetSpec(
        target_id=TARGET_FRAMEWORK,
        label="Framework Desktop (Strix Halo gfx1151, 128GB unified)",
        ssh_host="framework",
        endpoint="http://127.0.0.1:11434",
        model="qwen3.8:27b",
        # Inference only through an independently qualified profile; until then
        # it has no qualification ref and can't be selected.
        roles=(ROLE_INFERENCE,),
        qualification_ref="",
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


@dataclass
class LocalTargetCapability:
    """What this node can actually do.

    ``native_tools`` is tri-state:

      ``True``   proven by a native tool call that returned ``tool_calls``;
      ``False``  asked and refused/incapable;
      ``None``   unproven.

    Requirement checks treat ``None`` like ``False`` (fail closed), but the record
    keeps the distinction.
    """

    spec: LocalTargetSpec
    runtime_version: str = ""
    model_id: str = ""
    quantization: str = ""
    declared_context: Optional[int] = None
    #: The window actually served (from ``/api/ps``), distinct from
    #: ``declared_context``; sizing from the declared maximum overflows.
    served_context: Optional[int] = None
    #: Empirically safe working context. Set only from measurement; ``None``
    #: means "not yet established" and callers must not invent a number.
    safe_working_context: Optional[int] = None
    declared_capabilities: Tuple[str, ...] = ()
    #: The exact artifact digest, first-class so receipts never describe a tag.
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
    #: Raw observation, so a disputed number can be re-read instead of re-run.
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
        # Read-only analysis needs no tool channel, so tool-less targets stay useful.
        if self.health == HEALTH_HEALTHY:
            caps.append(CAP_READONLY_ANALYSIS)
        return tuple(caps)

    def satisfies(self, required: Iterable[str]) -> bool:
        """True only when every requirement is proven. Unknown names are a caller
        bug and raise."""
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


#: Bump when the receipt shape changes in a way a reader must know about.
CAPABILITY_RECEIPT_SCHEMA_VERSION = 1

#: Provenance classes; routing may require a stronger class than declared.
PROV_MEASURED = "measured"   # this probe observed it on this exact profile
PROV_DETECTED = "detected"   # the runtime reported a fact about its own state now
PROV_DECLARED = "declared"   # the artifact/config advertises it

#: Default validity of a semantic qualification: expensive to prove, so not
#: re-earned per heartbeat, but it expires so drift can't inherit it.
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


def _canonical_bytes(payload: object) -> bytes:
    """Deterministic canonical form (same rule as receipt hashes)."""
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
    """Configured, served, demonstrated, verified and safe context are distinct;
    only the measured safe working context is a capability."""

    configured_context: int = 0
    #: Effective per-request limit when it is a deterministic configuration fact.
    #: ``served_context`` is an observation and is never used for profile ID.
    configured_served_context: int = 0
    served_context: int = 0
    safe_working_context: int = 0
    safe_context_source: str = ""
    engine_demonstrated_context: int = 0
    semantic_verified_context: int = 0
    options: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = {"configured_context": self.configured_context,
               "configured_served_context": self.configured_served_context,
               "served_context": self.served_context,
               "safe_working_context": self.safe_working_context,
               "safe_context_source": self.safe_context_source,
               "options": dict(self.options)}
        if self.engine_demonstrated_context:
            out["engine_demonstrated_context"] = self.engine_demonstrated_context
        if self.semantic_verified_context:
            out["semantic_verified_context"] = self.semantic_verified_context
        return out


@dataclass(frozen=True)
class CapabilityEvidence:
    """Capabilities split by how they became known (declared vs measured), so a
    declaration can never satisfy a required proof."""

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
    """Material host identity a host-sensitive profile depends on. Empty means
    not collected, never "stable"."""

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
    """Typed refusal: no healthy target satisfies the requirement. There is no
    hosted fallback: these targets are the local-only privacy class."""

    def __init__(self, requirement: dict, reasons: Sequence[str]):
        self.requirement = dict(requirement)
        self.reasons = list(reasons)
        joined = "; ".join(self.reasons) if self.reasons else "no candidates registered"
        super().__init__(f"no local target satisfies {self.requirement}: {joined}")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OllamaInspector:
    """Default live inspector for one ollama node:
      * ``/api/version`` — runtime present and build;
      * ``/api/tags``    — model identity, quant and declared caps;
      * ``/api/ps``      — what is resident now;
      * one tool call    — the only evidence that counts for native tools.

    Timing is not measured here (cold loads can take minutes); perf numbers
    arrive from the evaluation harness via ``timings``.
    """

    #: Tool-required packets only go to targets that answered this with ``tool_calls``.
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
        """One API call returning ``{'ok', 'http', 'body', 'err'}``. Failure is a
        value, so a dead node is still reported."""
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
                # Retry without `think` so an older runtime isn't scored tool-less
                # for a version reason.
                proof = self._tool_question(spec, with_think_key=False)
            msg = (proof["body"].get("message") or {}) if proof["ok"] else {}
            raw["tool_proof"] = {
                "ok": proof["ok"],
                "tool_calls": len(msg.get("tool_calls") or []),
                "error": proof["err"],
            }
        return raw

def _apply_timings(rec: LocalTargetCapability, timings: dict) -> LocalTargetCapability:
    """Merge measured timing fields onto a record; missing keys never erase
    earlier measurements."""
    if not timings:
        return rec
    for name in ("ttft_s", "prefill_tok_s", "decode_tok_s", "cold_load_s"):
        value = timings.get(name)
        if value is not None:
            setattr(rec, name, value)
    return rec


def apply_timings(rec: LocalTargetCapability, timings: dict) -> LocalTargetCapability:
    """Public :func:`_apply_timings` for the evaluation harness. Timings must land
    in the records routing reads, or the fitness tie-break falls to target_id
    and can pick a far slower node."""
    return _apply_timings(rec, timings)



def build_capability(
    spec: LocalTargetSpec,
    raw: dict,
    *,
    probed_at: str = "",
) -> LocalTargetCapability:
    """Pure derivation: raw observation -> measured capability record, so numbers
    can be re-derived from stored evidence."""
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
    # Declared-only flags are informational; they never satisfy a requirement.
    rec.thinking = "thinking" in declared
    rec.vision = "vision" in declared

    ps = raw.get("ps") or {}
    loaded = ps.get("models") or []
    rec.queue_depth = len(loaded)
    resident = next((m for m in loaded if m.get("name") in (rec.model_id, spec.model)), None)
    if resident:
        rec.size_vram_bytes = resident.get("size_vram")
        served = resident.get("context_length")
        if isinstance(served, int) and served > 0:
            rec.served_context = served

    proof = raw.get("tool_proof")
    if proof is None:
        # Not asked: unproven, not incapable.
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
    # Safe working context is measured, never inferred from the declared maximum.
    rec.safe_working_context = raw.get("safe_working_context")

    rec.health = HEALTH_HEALTHY
    rec.failure_classes = tuple(dict.fromkeys(failures))
    return rec


def probe_target(
    spec: LocalTargetSpec,
    *,
    inspector: Optional[OllamaInspector] = None,
) -> LocalTargetCapability:
    """Measure one target. An unreachable node is a record, not an exception."""
    inspector = inspector or OllamaInspector()
    raw = inspector.inspect(spec)
    return build_capability(spec, raw)


def probe_fleet(
    specs: Sequence[LocalTargetSpec] = DEFAULT_TARGETS,
    *,
    inspector: Optional[OllamaInspector] = None,
    timings_by_target: Optional[Dict[str, dict]] = None,
) -> Tuple[LocalTargetCapability, ...]:
    """Measure every registered target, including failing ones, so "fleet down"
    never looks like "nothing to do". ``timings_by_target`` attaches measured
    throughput; without it selection falls back to an ID tie-break.
    """
    inspector = inspector or OllamaInspector()
    timings = dict(timings_by_target or {})
    return tuple(
        apply_timings(probe_target(spec, inspector=inspector), timings.get(spec.target_id, {}))
        for spec in specs
    )

def _fitness_key(record: LocalTargetCapability) -> tuple:
    """Deterministic order among equally eligible targets:

    1. lower live queue depth;
    2. higher measured decode throughput (unmeasured scores 0.0);
    3. target_id ascending, so concurrent schedulers agree.
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
    """Choose one healthy target that provably satisfies ``required``, returning
    the full record so the run can name what actually executed.

    Refusals are typed (:class:`LocalTargetUnavailable`) with per-target reasons.
    No hosted or weaker fallback: privacy class, capability and health are hard
    boundaries.
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
            # Two writers must never share a write scope.
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
    """JSON-serializable snapshot of every registered target, readable without
    re-probing; unreachable nodes appear as unreachable."""
    return {
        "generated_at": generated_at or _utc_iso(),
        "fleet_size": len(records),
        "healthy": sum(1 for r in records if r.health == HEALTH_HEALTHY),
        "unreachable": sum(1 for r in records if r.health == HEALTH_UNREACHABLE),
        "tool_capable": sum(1 for r in records if r.native_tools is True),
        "targets": [r.to_dict() for r in records],
    }

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
    """The canonical, hashable, freshness-bound capability record.

    Two-level identity: ``host_id`` is the machine, ``profile_id`` one exact
    execution profile on it (runtime + backend + digest + quant + context).
    Profiles never inherit each other's qualification. Routing consumes this
    instead of host names: per-capability evidence class, observation time and
    TTL, and an identity digest that drift breaks.
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
    #: What qualifies this profile to be routable at all (e.g. a profile id).
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
        """Fields whose change must invalidate prior qualification (runtime,
        artifact, quant, backend, context, host baseline). Timing, health and load
        are re-measured and never carried."""
        return self.execution_profile_material()

    def execution_profile_material(self) -> dict:
        """Stable execution identity, excluding observation timestamps/results."""
        return {
            "host_id": self.host_id,
            "runtime": {k: self.runtime.to_dict()[k] for k in (
                "runtime_kind", "provider", "endpoint_type", "endpoint_url",
                "repository", "version", "commit", "image_digest", "backend",
                "backend_version")},
            "model": {k: self.model.to_dict()[k] for k in (
                "model_id", "alias", "family", "digest", "size_bytes",
                "quantization", "auxiliary_artifacts", "declared_context",
                "declared_capabilities")},
            "context": {k: self.context.to_dict()[k] for k in (
                "configured_context", "configured_served_context", "options")},
            "host": {k: self.host.to_dict()[k] for k in (
                "host_id", "ssh_host", "cpu_arch", "gpu", "kernel",
                "boot_cmdline_digest", "firmware", "mesa", "rocm", "libhsakmt")},
        }

    def qualification_material(self) -> dict:
        """Measured qualification facts, separate from profile identity."""
        return {
            "capabilities": self.capabilities.to_dict(),
            "context": {k: self.context.to_dict().get(k) for k in (
                "engine_demonstrated_context", "semantic_verified_context",
                "safe_working_context", "safe_context_source")},
            "limits": self.limits.to_dict(),
            "qualification_ref": self.qualification_ref,
            "observed_at": self.observed_at,
            "ttl_s": int(self.ttl_s),
        }

    def identity_digest(self) -> str:
        return _digest_of(self.material_identity())

    def qualification_state(self, *, now: Optional[datetime] = None,
                            current_identity_digest: str = "") -> str:
        """``valid``, or the typed reason it is not."""
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
        """Short-lived liveness, on a clock independent of semantic qualification."""
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
        """Only the measured class; declared tool claims never appear."""
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
    # receipt_hash and identity_digest are derived and recomputed on the way in,
    # so a JSON round-trip rebuilds the same receipt.
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
    # profile_id is recomputed, so an edited identity can't keep an old
    # profile's qualification.
    if payload.get("host_id") and payload.get("observed_at"):
        runtime = payload.get("runtime") or RuntimeIdentity()
        model = payload.get("model") or ModelIdentity()
        context = payload.get("context") or ContextProfile()
        payload["profile_id"] = execution_profile_id(
            host_id=str(payload.get("host_id") or ""),
            runtime_kind=runtime.runtime_kind, backend=runtime.backend,
            model_alias=(model.alias or model.model_id),
            quantization=model.quantization,
            model_digest=model.digest,
            runtime_version=runtime.version, runtime_commit=runtime.commit,
            runtime_image_digest=runtime.image_digest, provider=runtime.provider,
            backend_version=runtime.backend_version,
            endpoint_url=runtime.endpoint_url, endpoint_type=runtime.endpoint_type,
            runtime_options=context.options,
            configured_context=context.configured_context,
            configured_served_context=context.configured_served_context)
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
                         model_digest: str, runtime_version: str = "",
                         runtime_commit: str = "", runtime_image_digest: str = "",
                         provider: str = "", backend_version: str = "",
                         endpoint_url: str = "", endpoint_type: str = "",
                         runtime_options: Optional[Mapping[str, Any]] = None,
                         configured_context: int = 0,
                         configured_served_context: int = 0,
                         ) -> str:
    """Identity of execution configuration, excluding qualification observations.
    Empty build identifiers are deterministic UNKNOWN."""
    config = {
        "provider": str(provider or "").strip(),
        "runtime_kind": str(runtime_kind or "").strip(),
        "runtime_version": str(runtime_version or "").strip(),
        "runtime_commit": str(runtime_commit or "").strip(),
        "runtime_image_digest": str(runtime_image_digest or "").strip(),
        "backend": str(backend or "").strip(),
        "backend_version": str(backend_version or "").strip(),
        "endpoint": canonical_endpoint_identity(endpoint_url, endpoint_type)
        if endpoint_url else "",
        "runtime_options": dict(runtime_options or {}),
        "configured_context": int(configured_context or 0),
        "configured_served_context": int(configured_served_context or 0),
    }
    config_digest = _digest_of(config)[:12]
    return ":".join([
        str(host_id).strip() or "unknown-host",
        f"{str(runtime_kind).strip() or 'runtime'}-{str(backend).strip() or 'backend'}",
        str(runtime_version).strip() or "version-unknown",
        str(model_alias).strip() or "model",
        str(quantization).strip() or "quant-unknown",
        f"ctx{int(configured_context or 0)}",
        (str(model_digest).strip() or "digest-unknown")[:12],
        f"cfg{config_digest}",
    ])

def receipt_from_capability(
    record: LocalTargetCapability,
    *,
    configured_context: int = 0,
    configured_served_context: int = 0,
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
    """One measured record -> the canonical receipt. Without a supplied
    ``safe_working_context`` the receipt is unqualified, never inheriting the
    declared window."""
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
        quantization=record.quantization,
        model_digest=digest, runtime_version=record.runtime_version,
        runtime_commit=runtime_commit, runtime_image_digest=runtime_image_digest,
        provider=(spec.runtime_kind or "ollama"), endpoint_url=spec.endpoint,
        endpoint_type=spec.transport, backend_version=str(
            (host_baseline or {}).get("backend_version") or ""),
        runtime_options=record.runtime_options,
        configured_context=configured_context,
        configured_served_context=0)

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
            configured_served_context=int(configured_served_context or 0),
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
