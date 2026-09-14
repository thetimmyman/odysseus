"""src/execution_catalog.py — capability/role → execution-target contract (PS-623).

Odysseus routes by *capability* with an explicit preferred target, never by
inferring model quality from UI picker/catalog text. The mapping is:

  integration_strong  -> DeepSeek V4 Pro    (planning / reconciliation / security)
  implementation_fast -> DeepSeek V4.1 Flash (bounded act-mode after a Pro plan)
  bulk_local           -> local Qwen pool   (parallel subagents for bulk work)

Targets are **operator-controllable** via the ``execution_targets`` setting
(edited in the settings UI); this module only reads that setting and applies the
routing rule. The #1 rule it enforces: a planning/reconciliation request must
NEVER silently downgrade to Flash. The only policy-approved substitute for Pro
is the SAME model on a different provider, mirroring
``agent_execution.prefer_same_model_order``.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple


class CapabilityUnavailable(Exception):
    """Typed failure: no approved target for a required capability is live.

    Raised instead of a silent downgrade. Callers surface this as a provider/
    model failure — never substitute a weaker model for a stronger role.
    """

    def __init__(self, capability: str, reasons: List[str]):
        self.capability = capability
        self.reasons = list(reasons)
        joined = "; ".join(reasons) if reasons else "no candidate could be probed"
        super().__init__(f"{capability}: {joined}")


CAPABILITY_INTEGRATION_STRONG = "integration_strong"
CAPABILITY_IMPLEMENTATION_FAST = "implementation_fast"
CAPABILITY_BULK_LOCAL = "bulk_local"

#: All capabilities the catalog understands. Unknown capabilities are rejected
#: rather than guessed (fail closed: a typo must not route to a wrong target).
KNOWN_CAPABILITIES = frozenset({
    CAPABILITY_INTEGRATION_STRONG,
    CAPABILITY_IMPLEMENTATION_FAST,
    CAPABILITY_BULK_LOCAL,
})

#: Default map used when the ``execution_targets`` setting is absent/incomplete.
#: Deliberately latest models: V4 Pro and V4.1 Flash. The setting (UI-editable)
#: wins over this per capability.
DEFAULT_TARGETS = {
    CAPABILITY_INTEGRATION_STRONG: [
        {"provider": "cline-pass", "model": "deepseek-v4-pro"},
        # Policy-approved substitute: SAME model, different provider. Never a
        # weaker model.
        {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
    ],
    CAPABILITY_IMPLEMENTATION_FAST: [
        {"provider": "cline-pass", "model": "deepseek-v4.1-flash"},
    ],
    CAPABILITY_BULK_LOCAL: [
        {"provider": "ollama", "model": "qwen3.8:27b"},
    ],
}

#: cline binary fallback when not on the process PATH.
DEFAULT_CLINE_BIN = "/home/tdefreest/.nvm/versions/node/v22.23.2/bin/cline"


@dataclass(frozen=True)
class ExecutionTargetSpec:
    """A resolvable execution target: provider + model, optionally its role."""

    provider: str
    model: str
    capability: str = ""

    @property
    def target_id(self) -> str:
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
    """Result of a non-destructive availability probe for one target."""

    target: ExecutionTargetSpec
    available: bool
    detail: str = ""


def catalog_targets(capability: str) -> List[ExecutionTargetSpec]:
    """Return the ordered candidate targets for ``capability``.

    Reads the operator-controlled ``execution_targets`` setting, falling back to
    :data:`DEFAULT_TARGETS`. Unknown capabilities raise :class:`ValueError`
    (fail closed — route nothing rather than guess).
    """
    if capability not in KNOWN_CAPABILITIES:
        raise ValueError(f"unknown capability: {capability!r}")
    raw = None
    try:
        from src.settings import get_setting
        configured = get_setting("execution_targets", {}) or {}
        if isinstance(configured, dict):
            raw = configured.get(capability)
    except Exception:
        raw = None
    if raw is None:
        raw = DEFAULT_TARGETS[capability]
    specs: List[ExecutionTargetSpec] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        provider = (entry.get("provider") or "").strip()
        model = (entry.get("model") or "").strip()
        if not provider or not model:
            continue
        specs.append(ExecutionTargetSpec(provider=provider, model=model,
                                         capability=capability))
    return specs


def _default_probe_runner(
    target: ExecutionTargetSpec,
    timeout: float,
) -> Tuple[bool, str]:
    """Cheapest non-destructive check: a plan-mode trivial completion.

    ``cline -p`` is plan mode (no tools), ``--thinking none`` avoids spending
    reasoning budget, and one trivial prompt establishes that the provider+model
    resolve and answer. Exit code is the availability authority — NOT the UI
    picker.
    """
    cline = shutil.which("cline") or DEFAULT_CLINE_BIN
    cmd = [
        cline, "--provider", target.provider, "--model", target.model,
        "-p", "--thinking", "none", "--json", "Reply exactly: OK",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "probe timed out"
    except OSError as exc:
        return False, f"could not invoke cline: {exc}"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return False, (detail[:200] or f"exit {proc.returncode}")
    return True, "ok"


def probe_target(
    target: ExecutionTargetSpec,
    *,
    runner: Callable[[ExecutionTargetSpec, float], Tuple[bool, str]] = _default_probe_runner,
    timeout: float = 30.0,
) -> CapabilityProbe:
    """Probe one target's availability (non-destructive)."""
    available, detail = runner(target, timeout)
    return CapabilityProbe(target=target, available=bool(available), detail=detail)


def select_target_for_capability(
    capability: str,
    *,
    probe: Optional[Callable[[ExecutionTargetSpec], CapabilityProbe]] = None,
) -> ExecutionTargetSpec:
    """Select a live target for ``capability``, or raise :class:`CapabilityUnavailable`.

    Candidates are exactly the capability's allowlisted targets in order; there
    is no cross-capability substitution, so integration_strong can never fall
    through to Flash. Returns the first candidate whose probe reports available.
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

