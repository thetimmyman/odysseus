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


@router.get("/effective")
def effective(request: Request):
    """Read-only 'what is actually in force' view. PR-A returns a small, honest
    set: the budget caps/version/persistence facts plus the policy live-file
    persistence fact and the deploy-only data-volume location. Extended into a
    full effective-config in PR-C.

    surface: 'runtime' (a save takes effect immediately) |
             'needs_redeploy' (persisted, but some consumers pick it up only on
             the next run/redeploy) | 'deploy_only' (set at deploy time)."""
    require_admin_cookie(request)
    cfg = load_budget_config()
    dr = config_store.data_root()
    budget_live = config_store.live_path("routing_budget")
    items = [
        {
            "name": "budget.caps",
            "value": _caps(cfg),
            "source": budget_live,
            "surface": "runtime",
            "editable_where": "Settings > Budget",
        },
        {
            "name": "budget.version",
            "value": cfg.get("version", "unversioned"),
            "source": budget_live,
            "surface": "runtime",
            "editable_where": "Settings > Budget (server-owned, auto-bumped)",
        },
        {
            "name": "budget.persisted",
            "value": True,
            "source": budget_live,
            "surface": "runtime",
            "editable_where": "Settings > Budget — live file on the data/ volume, survives redeploy",
        },
        {
            "name": "policy.persisted",
            "value": True,
            "source": os.path.join(dr, "routing"),
            "surface": "runtime",
            "editable_where": "Routing Harness > Policy — live file relocated to the data/ volume, survives redeploy",
        },
        {
            "name": "data_root",
            "value": dr,
            "source": "ODYSSEUS_DATA_DIR",
            "surface": "deploy_only",
            "editable_where": "deploy env (ODYSSEUS_DATA_DIR)",
        },
    ]
    return {"items": items}


def setup_config_routes(app):
    """Register the config router on the app (mirrors the harness setup call
    site). Returns the router for callers/tests that prefer include_router."""
    app.include_router(router)
    return router
