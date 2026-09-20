"""src/routing_locality.py — the PS-605 public contract for endpoint locality, data-sensitivity
ceilings and task→role vocabulary, owned by the routing-selector seam.

Moved here from src/routing_engine.py (TMOS M2-RETIRE-legacy-routing-harness, Stage A). The new
selector seam (src/dispatch_boundary.py, src/dispatch_routing.py) must not import the Section-9
legacy harness (constitution forbid `routing-selector -> legacy-routing-harness`, TMOS I8); these
three functions were the last import edge in that direction. The legacy engine now imports them
from here (legacy -> selector is the allowed direction; routing_engine already consumed
src.routing_budget the same way), so `src.routing_engine.endpoint_is_local` etc. keep resolving for
the Section-9 modules and their tests until Stage B retires them.

Contract (unchanged): one shared vocabulary for locality and sensitivity rules —
  endpoint_is_local(url)                    loopback / RFC1918 / bare LAN hostname => local; missing => NOT local
  sensitivity_requires_local_only(level)    level ranks above the policy's remoteSensitivityCeiling
  roles_for_task_type(task_type)            preference-ordered roles for the task vocabulary
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import urlparse

ROLE_BY_TASK: Dict[str, List[str]] = {
    "bug_debug": ["debugger", "scout"],
    "ci_triage": ["debugger", "scout"],
    "feature_plan": ["planner", "reviewer"],
    "feature_review": ["reviewer"],
    "implementation": ["implementer", "debugger"],
    "release_readiness": ["debugger", "scout"],
    "diff_review": ["reviewer"],
}
_DEFAULT_ROLES = ["scout"]

# Sensitivity rank order for the Section 9 hard filter. A task whose
# data_sensitivity ranks ABOVE the policy's remoteSensitivityCeiling may only
# route to endpoints on loopback/private networks.
_SENSITIVITY_RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3, "secret": 4}

_PRIVATE_HOST_RE = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|host\.docker\.internal"
    r"|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+"
    r"|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+"
    r"|[^.]+\.local|[^.]+\.internal|[^.]+)$"  # bare hostnames (no dots) = LAN
)


def _endpoint_is_local(url: Optional[str]) -> bool:
    """True when the endpoint host is loopback / RFC1918 / a bare LAN hostname.
    Anything else (openrouter.ai, api.*, cloud hosts) counts as remote for the
    data-sensitivity hard filter. A missing URL is NOT local — an unverifiable
    destination must never receive restricted data (fail closed)."""
    if not url:
        return False
    host = urlparse(url).hostname or ""
    return bool(host) and bool(_PRIVATE_HOST_RE.match(host))


def _remote_ceiling_rank() -> int:
    try:
        from src.routing_policy import load_policy
        ceiling = load_policy().get("remoteSensitivityCeiling", "confidential")
    except Exception:
        ceiling = "confidential"
    return _SENSITIVITY_RANK.get(ceiling, _SENSITIVITY_RANK["confidential"])


# ------------------------------------------------------- public contract (PS-605) ---
# dispatch_boundary and the registry seam consume these functions rather than
# private helpers, so locality and sensitivity rules have one shared vocabulary.
def endpoint_is_local(url: Optional[str]) -> bool:
    """True when an endpoint URL is loopback/private/LAN."""
    return _endpoint_is_local(url)


def sensitivity_requires_local_only(sensitivity: Optional[str]) -> bool:
    """True when sensitivity exceeds the configured remote ceiling."""
    return _SENSITIVITY_RANK.get(str(sensitivity or "internal"), 1) > _remote_ceiling_rank()


def roles_for_task_type(task_type: Optional[str]) -> List[str]:
    """Preference-ordered roles for the task vocabulary."""
    return list(ROLE_BY_TASK.get(str(task_type or ""), _DEFAULT_ROLES))
