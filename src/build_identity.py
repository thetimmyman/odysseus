"""Commit-aware build identity — injected, never inferred.

Odysseus's source tree on disk is a MUTABLE git checkout; reading git at
runtime could report a SHA/branch that never matched the code actually running
(a dirty worktree, a cherry-pick, or a checkout moved after the image built).
Build identity is therefore INJECTED at build/deploy time as environment
variables (see the Dockerfile build args and deploy-odysseus.sh) and never
inferred from the runtime checkout.

Local development (no injected values) reports ``status="dev"`` and empty
fields — explicitly un-pinned, never a fabricated commit.
"""

import os
from typing import Dict

GIT_SHA_ENV = "ODYSSEUS_BUILD_GIT_SHA"
BRANCH_ENV = "ODYSSEUS_BUILD_BRANCH"
BUILT_AT_ENV = "ODYSSEUS_BUILD_TIME"

STATUS_PINNED = "pinned"
STATUS_DEV = "dev"


def build_identity() -> Dict[str, str]:
    """Return ``{status, git_sha, branch, built_at}`` from injected env values."""
    git_sha = os.getenv(GIT_SHA_ENV, "").strip()
    branch = os.getenv(BRANCH_ENV, "").strip()
    built_at = os.getenv(BUILT_AT_ENV, "").strip()
    status = STATUS_PINNED if git_sha else STATUS_DEV
    return {
        "status": status,
        "git_sha": git_sha,
        "branch": branch,
        "built_at": built_at,
    }


def version_payload() -> Dict[str, str]:
    """The ``/api/version`` payload: version + build identity fields."""
    from src.constants import APP_VERSION

    payload: Dict[str, str] = {"version": APP_VERSION}
    payload.update(build_identity())
    return payload
