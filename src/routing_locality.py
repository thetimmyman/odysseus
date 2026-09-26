"""Endpoint locality, data-sensitivity ceilings and task->role vocabulary.

Lives in the selector seam because the selector must not import the legacy
routing harness; the legacy engine imports from here instead.

Contract:
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

# Data ranked above remoteSensitivityCeiling may only go to loopback/private endpoints.
_SENSITIVITY_RANK = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3, "secret": 4}

_PRIVATE_HOST_RE = re.compile(
    r"^(localhost|127\.\d+\.\d+\.\d+|0\.0\.0\.0|host\.docker\.internal"
    r"|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+"
    r"|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+"
    r"|[^.]+\.local|[^.]+\.internal|[^.]+)$"  # bare hostnames (no dots) = LAN
)


def _endpoint_is_local(url: Optional[str]) -> bool:
    """True for loopback / RFC1918 / bare LAN hosts; a missing URL is not local."""
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


# Public contract: callers use these, not the private helpers.
def endpoint_is_local(url: Optional[str]) -> bool:
    return _endpoint_is_local(url)


def sensitivity_requires_local_only(sensitivity: Optional[str]) -> bool:
    return _SENSITIVITY_RANK.get(str(sensitivity or "internal"), 1) > _remote_ceiling_rank()


def roles_for_task_type(task_type: Optional[str]) -> List[str]:
    return list(ROLE_BY_TASK.get(str(task_type or ""), _DEFAULT_ROLES))
