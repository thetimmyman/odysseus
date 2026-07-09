"""routes/config_routes.py — the dedicated Settings config surface (PR-A).

Exposes the versioned Budget editor and a read-only Effective-config view under
``/api/config``. Every route is gated on ``require_admin_cookie`` (cookie admin
only — bearer tokens and the internal-tool loopback are rejected, exactly like
the routing-harness control surface: editing budget caps changes spend
exposure). The actor recorded on a publish/rollback is the current cookie
principal.

Persistence: the budget live file now lives under the data/ volume
(config_store.live_path), seeded from the baked config/routing_budget.json on
first boot — so an in-app save survives a redeploy (the whole point of PR-A).

Mirrors setup_routing_harness_routes structure. Registered in app.py right
after the routing-harness routes.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from core.database import SessionLocal
from src import config_store
from src import routing_budget
from src import routing_policy
from src.auth_helpers import require_admin_cookie
from src.routing_budget import load_budget_config, spend_summary

router = APIRouter(prefix="/api/config", tags=["config"])

_CAP_KEYS = (
    "daily_max_usd",
    "weekly_max_usd",
    "monthly_max_usd",
    "premium_daily_max_usd",
    "premium_weekly_max_usd",
)


class BudgetPublishRequest(BaseModel):
    """The five cap floats. Value-level validity (positivity, premium<=general)
    is enforced by routing_budget.validate_budget → HTTP 400 {detail:[...]}, not
    here — pydantic only guarantees they parse as numbers. The version is
    server-owned (auto-bumped on publish); a client-supplied version is
    deliberately NOT accepted."""
    daily_max_usd: float
    weekly_max_usd: float
    monthly_max_usd: float
    premium_daily_max_usd: float
    premium_weekly_max_usd: float


class BudgetRollbackRequest(BaseModel):
    archive_name: str


def _caps(d: dict) -> dict:
    return {k: d.get(k) for k in _CAP_KEYS}


def _reasons_from_value_error(e: ValueError) -> list:
    """config_store.publish raises ValueError(list-of-reasons); the jail check
    raises ValueError('invalid archive name') (a bare string). Normalize both
    into a list for the {detail:[...]} contract."""
    reasons = e.args[0] if e.args else str(e)
    if isinstance(reasons, list):
        return [str(r) for r in reasons]
    return [str(reasons)]


@router.get("/budget")
def budget_get(request: Request):
    """Current caps + server-owned version + live spend + persistence facts.
    Shape per the PR-A CONTRACT."""
    require_admin_cookie(request)
    cfg = load_budget_config()
    db = SessionLocal()
    try:
        spend = spend_summary(db)
    finally:
        db.close()
    return {
        "caps": _caps(cfg),
        "version": cfg.get("version", "unversioned"),
        "spend": {
            "daily_usd": spend["daily"]["spent"],
            "weekly_usd": spend["weekly"]["spent"],
            "monthly_usd": spend["monthly"]["spent"],
            "premium_daily_usd": spend["premium_daily"]["spent"],
            "premium_weekly_usd": spend["premium_weekly"]["spent"],
        },
        "persisted": True,
        "live_path": config_store.live_path("routing_budget"),
    }


@router.post("/budget/publish")
def budget_publish(body: BudgetPublishRequest, request: Request):
    """Publish the five caps as a new server-versioned budget. Invalid caps
    (non-positive, or a premium sub-cap above its general cap) → 400
    {detail:[reasons]} with the live file left intact (fail-safe)."""
    actor = require_admin_cookie(request)
    d = {k: getattr(body, k) for k in _CAP_KEYS}
    try:
        stored = routing_budget.publish_budget(d, actor=actor or "admin")
    except ValueError as e:
        raise HTTPException(400, detail=_reasons_from_value_error(e))
    return {"ok": True, "version": stored.get("version"), "caps": _caps(stored)}


@router.get("/budget/versions")
def budget_versions(request: Request):
    """Archived budget snapshots newest-first: [{archive_name, version, ts,
    actor}]."""
    require_admin_cookie(request)
    return routing_budget.list_budget_versions()


@router.post("/budget/rollback")
def budget_rollback(body: BudgetRollbackRequest, request: Request):
    """Re-publish an archived budget snapshot (a logged publish). A bad/absent
    archive name → 400 (traversal-jailed) or 404 (no such archive)."""
    actor = require_admin_cookie(request)
    try:
        stored = routing_budget.rollback_budget(body.archive_name, actor=actor or "admin")
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, detail=_reasons_from_value_error(e))
    return {"ok": True, "version": stored.get("version"), "caps": _caps(stored)}


# Notable policy fields surfaced in the effective view, in display order:
# (dotted path, editable_where, danger). `danger` marks a break-glass knob that
# is READ-ONLY in the structured Policy editor (it needs security_admin via the
# raw Routing Harness > Policy tab). Kept in sync with routing_policy's
# DANGER_ZONE_KEYS by value; the frontend renders danger rows read-only.
_POLICY_EFFECTIVE_FIELDS = [
    ("routingPolicyVersion", "Settings > Policy (server-owned)", False),
    ("verificationPolicyVersion", "Settings > Policy (server-owned)", False),
    ("verification.defaultMode", "Settings > Policy", False),
    ("verification.overconfidenceThreshold", "Settings > Policy", False),
    ("coordinator.provider", "Routing Harness > Policy (security_admin)", True),
    ("coordinator.endpointName", "Routing Harness > Policy (security_admin)", True),
    ("coordinator.temperature", "Settings > Policy", False),
    ("coordinator.maxTokens", "Settings > Policy", False),
    ("coordinator.benchmark.defaultReplays", "Settings > Policy", False),
    ("maxUntrustedTokens", "Settings > Policy", False),
    ("rawOutputMaxBytes", "Settings > Policy", False),
    ("remoteSensitivityCeiling", "Routing Harness > Policy (security_admin)", True),
    ("sandbox.image", "Routing Harness > Policy (security_admin)", True),
    ("sandbox.cpus", "Settings > Policy", False),
    ("sandbox.memoryGb", "Settings > Policy", False),
    ("sandbox.pidsLimit", "Settings > Policy", False),
    ("sandbox.wallClockSeconds", "Settings > Policy", False),
    ("sandbox.maxOutputBytes", "Settings > Policy", False),
    ("absis.enabled", "Routing Harness > Policy (security_admin)", True),
]


def _dotted(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


@router.get("/effective")
def effective(request: Request):
    """Read-only 'what is actually in force' view across budget + policy. Each
    item: name, value, source (the file/env it comes from), surface, danger
    (a break-glass knob, read-only in the structured editor), editable (False
    for danger/server-owned), editable_where.

    surface: 'runtime' (a save takes effect immediately) |
             'needs_redeploy' (persisted, but some consumers pick it up only on
             the next run/redeploy) | 'deploy_only' (set at deploy time)."""
    require_admin_cookie(request)
    cfg = load_budget_config()
    dr = config_store.data_root()
    budget_live = config_store.live_path("routing_budget")
    policy = routing_policy.load_policy()
    policy_live = routing_policy.POLICY_PATH

    def item(name, value, source, surface="runtime", danger=False, editable=True, where=""):
        return {"name": name, "value": value, "source": source, "surface": surface,
                "danger": danger, "editable": editable, "editable_where": where}

    items = [
        item("budget.caps", _caps(cfg), budget_live, where="Settings > Budget"),
        item("budget.version", cfg.get("version", "unversioned"), budget_live,
             editable=False, where="Settings > Budget (server-owned, auto-bumped)"),
        item("budget.persisted", True, budget_live, editable=False,
             where="Settings > Budget — data/ volume, survives redeploy"),
    ]
    # Full policy surface.
    for path, where, danger in _POLICY_EFFECTIVE_FIELDS:
        v = _dotted(policy, path)
        version_owned = path.endswith("Version")
        items.append(item("policy." + path, v, policy_live,
                          danger=danger, editable=(not danger and not version_owned), where=where))
    # The allowlist is long — surface its size + the danger pointer, not the raw list.
    allow = _dotted(policy, "sandbox.allowedCommands")
    items.append(item("policy.sandbox.allowedCommands", f"{len(allow) if isinstance(allow, list) else 0} commands",
                      policy_live, danger=True, editable=False,
                      where="Routing Harness > Policy (security_admin)"))
    items.append(item("policy.persisted", True, os.path.join(dr, "routing"), editable=False,
                      where="Policy live file on the data/ volume, survives redeploy"))
    items.append(item("data_root", dr, "ODYSSEUS_DATA_DIR", surface="deploy_only", editable=False,
                      where="deploy env (ODYSSEUS_DATA_DIR)"))
    return {"items": items}


def setup_config_routes(app):
    """Register the config router on the app (mirrors the harness setup call
    site). Returns the router for callers/tests that prefer include_router."""
    app.include_router(router)
    return router
