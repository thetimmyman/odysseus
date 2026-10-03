"""Capability -> ExecutionTarget contract.

Callers ask for a *capability*; this module resolves it to explicit target ids
and proves availability. Three invariants:

1. A capability's candidate list is its allowlist. There is no cross-capability
   fallthrough, so ``integration_strong`` can never land on Flash.
2. Availability is proven by invoking the target. Picker and catalog text are
   human-facing views and are never the authority.
3. A capability with no live allowlisted target raises
   :class:`CapabilityUnavailable` rather than substituting a weaker one.

``bulk_local`` is not a generic pool: its candidates come from the local
execution-target registry, so every local target keeps its own identity and its
own measured qualification.

Quota and entitlement state is deliberately not modelled here. A probe reports
what the provider said; deciding what to do about an exhausted pool belongs to
the capacity layer.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

CAPABILITY_INTEGRATION_STRONG = "integration_strong"
CAPABILITY_IMPLEMENTATION_FAST = "implementation_fast"
CAPABILITY_BULK_LOCAL = "bulk_local"

#: Unknown capabilities raise instead of routing, so a typo fails closed.
KNOWN_CAPABILITIES = frozenset({
    CAPABILITY_INTEGRATION_STRONG,
    CAPABILITY_IMPLEMENTATION_FAST,
    CAPABILITY_BULK_LOCAL,
})

#: Provider label for targets owned by the local execution-target registry.
LOCAL_PROVIDER = "local"

#: Ordered candidates for the hosted capabilities. A capability's list is its
#: allowlist; a same-model entry on another provider is the only substitute
#: policy allows, never a weaker model. ``bulk_local`` is absent on purpose —
#: it is derived from the local registry, not written down here.
DEFAULT_TARGETS = {
    CAPABILITY_INTEGRATION_STRONG: [
        {"provider": "cline-pass", "model": "deepseek-v4-pro"},
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
    ],
    CAPABILITY_IMPLEMENTATION_FAST: [
        {"provider": "cline-pass", "model": "deepseek-v4.1-flash"},
    ],
}

#: Name of the operator-editable setting holding the per-capability allowlists.
EXECUTION_TARGETS_SETTING = "execution_targets"

#: Longest provider message kept as a probe's reason detail.
_DETAIL_LIMIT = 200


class CapabilityUnavailable(Exception):
    """Typed refusal: no allowlisted target for a capability is live.

    Carries the per-target reasons so a caller can show why a capability could
    not run instead of silently downgrading it.
    """

    def __init__(self, capability: str, reasons: List[str]):
        self.capability = capability
        self.reasons = list(reasons)
        joined = "; ".join(reasons) if reasons else "no candidate could be probed"
        super().__init__(f"{capability}: {joined}")


@dataclass(frozen=True)
class ExecutionTargetSpec:
    """A resolvable execution target: an explicit provider/model id, or a
    local-registry target id.

    ``target_id`` is what dispatch and receipts record, so it stays the same
    string the runtime is invoked with.
    """

    provider: str
    model: str
    capability: str = ""
    local_target_id: str = ""

    @property
    def target_id(self) -> str:
        if self.local_target_id:
            return self.local_target_id
        return f"{self.provider}/{self.model}"

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "target_id": self.target_id,
            "capability": self.capability,
        }


@dataclass
class CapabilityProbe:
    """One target's availability, as observed rather than as displayed."""

    target: ExecutionTargetSpec
    available: bool
    detail: str = ""


def _local_bulk_targets() -> List[ExecutionTargetSpec]:
    """``bulk_local`` candidates, taken from the local execution-target registry.

    A target qualifies only with an inference role and its own measured
    qualification, so a host that has not been qualified is not a candidate.
    """
    from src.local_targets import ROLE_INFERENCE, registered_targets

    candidates: List[ExecutionTargetSpec] = []
    for spec in registered_targets():
        if ROLE_INFERENCE not in spec.roles or not spec.qualification_ref:
            continue
        candidates.append(ExecutionTargetSpec(
            provider=LOCAL_PROVIDER, model=spec.model,
            capability=CAPABILITY_BULK_LOCAL, local_target_id=spec.target_id))
    return candidates


def _configured_targets(capability: str) -> Optional[list]:
    """The operator's allowlist for ``capability``, or None if unset/invalid."""
    try:
        from src.settings import get_setting
        configured = get_setting(EXECUTION_TARGETS_SETTING, {}) or {}
    except Exception:
        return None
    if not isinstance(configured, dict):
        return None
    raw = configured.get(capability)
    return raw if isinstance(raw, list) else None


def catalog_targets(capability: str) -> List[ExecutionTargetSpec]:
    """Ordered allowlisted targets for ``capability``.

    The operator setting wins over :data:`DEFAULT_TARGETS`; ``bulk_local`` falls
    back to the local registry. An unknown capability raises ``ValueError``.
    """
    if capability not in KNOWN_CAPABILITIES:
        raise ValueError(f"unknown capability: {capability!r}")
    raw = _configured_targets(capability)
    if raw is None:
        if capability == CAPABILITY_BULK_LOCAL:
            return _local_bulk_targets()
        raw = DEFAULT_TARGETS[capability]
    specs: List[ExecutionTargetSpec] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        provider = (entry.get("provider") or "").strip()
        model = (entry.get("model") or "").strip()
        if not provider or not model:
            continue
        local_id = (entry.get("local_target_id") or "").strip()
        specs.append(ExecutionTargetSpec(
            provider=provider, model=model, capability=capability,
            local_target_id=local_id))
    return specs


def cline_bin() -> Optional[str]:
    """Resolved Cline CLI, or None when it is not installed.

    The path comes from the operator's environment; nothing host-specific is
    baked in, so a moved install follows PATH or the override.
    """
    binary = os.environ.get("ODYSSEUS_CLINE_BIN") or "cline"
    if os.path.isabs(binary):
        return binary if os.access(binary, os.X_OK) else None
    return shutil.which(binary)


def probe_command(target: ExecutionTargetSpec) -> List[str]:
    """The cheapest invocation that proves ``target`` answers.

    Plan mode with tools off and one trivial prompt, so the probe is
    non-destructive. The model id is the qualified ``modelType/model`` form —
    the bare name is rejected before any request is made.
    """
    cline = cline_bin()
    if not cline:
        raise FileNotFoundError("the Cline CLI is not installed or not on PATH")
    return [
        cline, "--provider", target.provider, "--model", target.target_id,
        "-p", "--thinking", "none", "--json", "Reply exactly: OK",
    ]


def _probe_detail(stdout: str, returncode: int) -> str:
    """Why a probe failed, preferring the runtime's own message over its exit code."""
    detail = ""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        detail = str(event.get("message") or event.get("text") or "").strip()
        if detail:
            break
    if not detail:
        detail = f"exit {returncode}"
    return detail[:_DETAIL_LIMIT]


def _run_cline_probe(target: ExecutionTargetSpec, timeout: float) -> Tuple[bool, str]:
    try:
        cmd = probe_command(target)
    except FileNotFoundError as exc:
        return False, str(exc)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "probe timed out"
    except OSError as exc:
        return False, f"could not invoke cline: {exc}"
    if proc.returncode != 0:
        return False, _probe_detail(proc.stdout or proc.stderr, proc.returncode)
    return True, "ok"


def _run_local_probe(target: ExecutionTargetSpec, timeout: float) -> Tuple[bool, str]:
    """Ask the local registry about one target instead of invoking a CLI."""
    from src.local_targets import HEALTH_HEALTHY, probe_target, target_by_id

    spec = target_by_id(target.target_id)
    if spec is None:
        return False, "not a registered local target"
    record = probe_target(spec)
    if record.health != HEALTH_HEALTHY:
        detail = ",".join(record.failure_classes) or "no detail"
        return False, f"health={record.health} ({detail})"
    return True, "ok"


def _default_probe_runner(
    target: ExecutionTargetSpec,
    timeout: float,
) -> Tuple[bool, str]:
    if target.local_target_id:
        return _run_local_probe(target, timeout)
    return _run_cline_probe(target, timeout)


def probe_target(
    target: ExecutionTargetSpec,
    *,
    runner: Callable[[ExecutionTargetSpec, float], Tuple[bool, str]] = _default_probe_runner,
    timeout: float = 30.0,
) -> CapabilityProbe:
    """Observe one target's availability without trusting a picker."""
    available, detail = runner(target, timeout)
    return CapabilityProbe(target=target, available=bool(available), detail=detail)


def select_target_for_capability(
    capability: str,
    *,
    probe: Optional[Callable[[ExecutionTargetSpec], CapabilityProbe]] = None,
) -> ExecutionTargetSpec:
    """The first live allowlisted target for ``capability``.

    Candidates are exactly that capability's allowlist, in order, so no weaker
    model can absorb the work. When none answers, the typed
    :class:`CapabilityUnavailable` carries every reason.
    """
    candidates = catalog_targets(capability)
    if not candidates:
        raise CapabilityUnavailable(capability, ["no candidates configured"])
    probe = probe or probe_target
    reasons: List[str] = []
    for target in candidates:
        result = probe(target)
        if result.available:
            return target
        reasons.append(f"{target.target_id}: {result.detail or 'unavailable'}")
    raise CapabilityUnavailable(capability, reasons)
