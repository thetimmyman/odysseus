"""src/routing_engine.py — model scoring + routing, ported from the source
spec's Section 6 (role map + scoreModelForTask). Scored against
`core.database.RoutingModelProfile` rows and a routing_context.build_context_bundle()
bundle; historical performance comes from routing_scoring.historical_score()
(single source of truth -- don't reimplement that aggregate here)."""
import json
import re
from typing import Dict, List, Optional
from urllib.parse import urlparse

from src.routing_budget import DEFAULT_MAX_OUTPUT_TOKENS, estimate_cost_usd
from src.routing_scoring import historical_score

# Desired roles per RoutingTask.task_type. Keyed on the OdysseusTask-style
# 7-value enum this app's RoutingTask.task_type actually uses (bug_debug/
# ci_triage/feature_plan/feature_review/implementation/release_readiness/
# diff_review) -- the source spec's Section 6.1 role map used a second,
# slightly different TaskType enum (known_bug_reproduction/ci_failure_triage/
# etc.) for the same concept; this is the one reconciliation to the single
# enum actually stored in the DB.
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

# task_types that would produce a patch if patch extraction existed (Phase 3+
# concern) -- used here only to weight implementer-role models higher, not
# to actually apply anything in Phase 1/2.
_PATCH_SHAPED_TASK_TYPES = ("bug_debug", "ci_triage", "implementation")
_REPO_WIDE_TASK_TYPES = ("feature_plan", "release_readiness", "feature_review")


def score_model_for_task(profile, task, bundle: dict, hist_score: Optional[float]) -> dict:
    """Port of the spec's scoreModelForTask. `profile` is a RoutingModelProfile
    row, `task` a RoutingTask row, `bundle` a routing_context bundle dict."""
    desired_roles = ROLE_BY_TASK.get(task.task_type, _DEFAULT_ROLES)
    profile_roles = json.loads(profile.roles) if profile.roles else []
    reasons: List[str] = []
    score = 0.0

    role_matches = [r for r in desired_roles if r in profile_roles]
    if role_matches:
        bonus = len(role_matches) * 25
        score += bonus
        reasons.append(f"role match: {', '.join(role_matches)} (+{bonus})")

    estimated_input_tokens = (bundle.get("metadata") or {}).get("token_estimate", 0)
    if profile.context_window and profile.context_window >= estimated_input_tokens:
        score += 20
        reasons.append("fits context window (+20)")
    else:
        score -= 100
        reasons.append("does NOT fit context window (-100)")

    if task.risk == "low" and profile.is_free:
        score += 15
        reasons.append("low risk + free model (+15)")

    if task.risk in ("high", "release_blocking"):
        if "escalation" in profile_roles:
            score += 30
            reasons.append("escalation role for high/release-blocking risk (+30)")
        if profile.is_free:
            score -= 15
            reasons.append("free model penalized for high/release-blocking risk (-15)")

    # Excludes task types where "implementer" is already a desired_role (e.g.
    # "implementation" itself) -- otherwise the same underlying fact (profile
    # has the implementer role) earns +25 twice: once here and once via the
    # role-match bonus above, for the identical signal.
    requires_patch = task.task_type in _PATCH_SHAPED_TASK_TYPES and "implementer" not in desired_roles
    if requires_patch and "implementer" in profile_roles:
        score += 25
        reasons.append("implementer role for patch-shaped task (+25)")

    requires_repo_wide = task.task_type in _REPO_WIDE_TASK_TYPES or len(bundle.get("files") or []) > 5
    if requires_repo_wide and profile.context_window and profile.context_window >= 500_000:
        score += 15
        reasons.append("500K+ context for repo-wide reasoning (+15)")

    requires_long_context = estimated_input_tokens > 500_000
    if requires_long_context and profile.context_window and profile.context_window >= 1_000_000:
        score += 20
        reasons.append("1M+ context for long-context task (+20)")

    estimated_cost = estimate_cost_usd(profile, estimated_input_tokens, profile.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS)
    if estimated_cost == 0:
        score += 10
        reasons.append("free (+10)")
    elif estimated_cost < 0.1:
        score += 8
        reasons.append(f"cheap (${estimated_cost:.4f}) (+8)")
    elif estimated_cost > 1.0:
        score -= 20
        reasons.append(f"expensive (${estimated_cost:.2f}) (-20)")

    if hist_score is not None:
        bonus = hist_score * 10
        score += bonus
        reasons.append(f"historical score {hist_score:.2f}/5 for this task type (+{bonus:.1f})")

    return {
        "profile_id": profile.id,
        "model": profile.model,
        "roles": profile_roles,
        "score": round(score, 1),
        "estimated_cost_usd": round(estimated_cost, 4),
        "reasons": reasons,
    }


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


def route_task(db, task, bundle: dict) -> dict:
    """Return the ranked candidate chain for `task`, filtered by its
    allow_free/paid/premium flags and the Section 9 data-sensitivity hard
    filter (a hard filter, not a score penalty: dataPolicyFit < 1.0 means the
    candidate never enters ranking at all). `task` is a RoutingTask row."""
    from core.database import ModelEndpoint, RoutingModelProfile

    profiles = db.query(RoutingModelProfile).filter(RoutingModelProfile.enabled == True).all()  # noqa: E712

    sensitivity = getattr(task, "data_sensitivity", None) or "internal"
    needs_local_only = _SENSITIVITY_RANK.get(sensitivity, 1) > _remote_ceiling_rank()
    endpoint_urls = {}
    if needs_local_only:
        ep_ids = [p.model_endpoint_id for p in profiles if p.model_endpoint_id]
        if ep_ids:
            for ep in db.query(ModelEndpoint).filter(ModelEndpoint.id.in_(ep_ids)).all():
                endpoint_urls[ep.id] = ep.base_url

    allowed = []
    remote_excluded = 0
    for p in profiles:
        if p.is_premium:
            if not task.allow_premium_models:
                continue
        elif p.is_free:
            if not task.allow_free_models:
                continue
        else:  # paid, not premium
            if not task.allow_paid_models:
                continue
        if needs_local_only and not _endpoint_is_local(endpoint_urls.get(p.model_endpoint_id)):
            remote_excluded += 1
            continue
        allowed.append(p)

    scored = [
        score_model_for_task(p, task, bundle, historical_score(db, p.id, task.task_type))
        for p in allowed
    ]
    scored.sort(key=lambda s: s["score"], reverse=True)

    return {
        "task_id": task.id,
        "candidates": scored,
        "dataPolicy": {
            "sensitivity": sensitivity,
            "localOnly": needs_local_only,
            "remoteCandidatesExcluded": remote_excluded,
        },
    }
