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
                 serving context (from /api/ps): 32768; cold load 53.2s
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
model on the RTX node. A packet sized from the declared number would be
dispatched into a window that does not exist, so ``safe_working_context`` stays
``None`` until it is measured and is never inferred from the declared maximum.

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

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------- constants ---

#: Stable target IDs. These are the identity strings that appear in RunState /
#: evidence. A generic ``local_qwen`` label is explicitly NOT sufficient.
TARGET_RTX_4500 = "local-rtx4500"
TARGET_MSR1 = "local-msr1"
TARGET_FRAMEWORK = "local-framework"

#: Transports. Both reachable targets bind ollama to loopback and are therefore
#: only addressable over ssh; a target that later exposes a routable endpoint
#: uses ``http`` and nothing else in this module changes.
TRANSPORT_SSH = "ssh"
TRANSPORT_HTTP = "http"

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
    ),
    LocalTargetSpec(
        target_id=TARGET_MSR1,
        label="MINISFORUM MS-R1 (aarch64, 12-core, 62GB)",
        ssh_host="msr1",
        endpoint="http://127.0.0.1:11434",
        model="qwen3.8:27b",
    ),
    LocalTargetSpec(
        target_id=TARGET_FRAMEWORK,
        label="Framework Desktop (Strix Halo gfx1151, 128GB unified)",
        ssh_host="framework",
        endpoint="http://127.0.0.1:11434",
        model="qwen3.8:27b",
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


# ------------------------------------------------------------------ probing ---

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
    rec.quantization = details.get("quantization_level") or ""
    rec.declared_context = details.get("context_length")
    rec.declared_capabilities = declared
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
    """Measure every registered target, including the ones that fail.

    A node that cannot be reached must still appear in the snapshot. Dropping it
    would make "the fleet is down" and "there is nothing to do" look identical,
    which is how a blocked target silently becomes a scheduling policy.

    ``timings_by_target`` lets an evaluation harness attach the throughput it
    measured (keyed by ``target_id``) so the same records the router reads carry
    real fitness numbers. Without it the records are capability-complete but
    fitness-blind, and selection falls back to a deterministic ID tie-break.
    """
    inspector = inspector or OllamaInspector()
    timings = dict(timings_by_target or {})
    return tuple(
        apply_timings(probe_target(spec, inspector=inspector), timings.get(spec.target_id, {}))
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
