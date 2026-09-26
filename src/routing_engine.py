"""Model scoring and routing over RoutingModelProfile rows.

Historical performance comes only from routing_scoring.historical_score()."""
import json
from typing import Dict, List, Optional

from src.routing_budget import DEFAULT_MAX_OUTPUT_TOKENS, estimate_cost_usd
from src.routing_scoring import historical_score

# Task->role vocabulary, sensitivity ranks and endpoint locality live in
# src/routing_locality.py; re-exported so existing callers keep importing them here.
from src.routing_locality import (  # noqa: F401  (re-exports)
    ROLE_BY_TASK, _DEFAULT_ROLES, _SENSITIVITY_RANK, _PRIVATE_HOST_RE, _endpoint_is_local,
    _remote_ceiling_rank, endpoint_is_local, roles_for_task_type, sensitivity_requires_local_only,
)

# Patch-producing task types; used only to weight implementer-role models higher.
_PATCH_SHAPED_TASK_TYPES = ("bug_debug", "ci_triage", "implementation")
_REPO_WIDE_TASK_TYPES = ("feature_plan", "release_readiness", "feature_review")


def score_model_for_task(profile, task, bundle: dict, hist_score: Optional[float]) -> dict:
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

    # Skip when "implementer" is already a desired role, or the same fact earns +25 twice.
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


def route_task(db, task, bundle: dict) -> dict:
    """Ranked candidates for `task`, filtered by tier flags and data sensitivity.

    Sensitivity is a hard filter, not a penalty. The only historical signal is
    task performance; lesson-gen scores never change the order."""
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
