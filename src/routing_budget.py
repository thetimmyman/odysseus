"""Cost estimation and hard budget blocks for the routing harness.

Caps are hard blocks, not approval gates; only the premium caps can be bypassed,
via `--allow-premium`."""
import json
import logging
import math
import os
from datetime import datetime, timedelta
from typing import Optional

from src import config_store

_log = logging.getLogger(__name__)

# Last good caps, held so an unreadable live file never raises the ceiling.
_last_good_caps: Optional[dict] = None

# Baked seed only; the live file sits on the data/ volume so saves survive redeploys.
_DOMAIN = "routing_budget"
_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "routing_budget.json"
)

DEFAULT_BUDGET_CONFIG = {
    "daily_max_usd": 10.0,
    "weekly_max_usd": 50.0,
    "monthly_max_usd": 150.0,
    "premium_daily_max_usd": 5.0,
    "premium_weekly_max_usd": 20.0,
}

# daily/weekly are never overridable; premium_* yield to --allow-premium;
# monthly_max_usd is advisory and never enforced.
_CAP_KEYS = (
    "daily_max_usd",
    "weekly_max_usd",
    "monthly_max_usd",
    "premium_daily_max_usd",
    "premium_weekly_max_usd",
)

# Shared by ranking and execution so estimated and actual worst-case spend agree.
DEFAULT_MAX_OUTPUT_TOKENS = 4096


def load_budget_config() -> dict:
    """Read the live budget file on every call (no cache), seeding it if missing.

    A spend cap must never be silently raised, so a present-but-unreadable file
    holds the last good caps and only falls back to DEFAULT without one."""
    global _last_good_caps
    config_store.seed_if_missing(_DOMAIN, baked_default_path=_CONFIG_PATH,
                                 default_dict=DEFAULT_BUDGET_CONFIG)
    raw = config_store.read_live(_DOMAIN)
    if raw is not None:
        merged = dict(DEFAULT_BUDGET_CONFIG)
        merged.update(raw)
        _last_good_caps = {k: merged[k] for k in _CAP_KEYS if k in merged}
        return merged
    # No parseable live file. Distinguish truly-missing from present-but-corrupt.
    if config_store.live_status(_DOMAIN) == "unreadable" and _last_good_caps:
        _log.warning(
            "routing_budget: live file unreadable; holding last-known-good caps "
            "rather than degrading UP to DEFAULT (would silently raise the cap)")
        merged = dict(DEFAULT_BUDGET_CONFIG)
        merged.update(_last_good_caps)
        return merged
    return dict(DEFAULT_BUDGET_CONFIG)


def validate_budget(d: dict) -> list:
    """Return reasons a budget is invalid ([] when valid).

    Every cap is positive, and premium caps may not exceed their general caps
    (they could never bind)."""
    if not isinstance(d, dict):
        return ["budget config must be a JSON object"]
    reasons = []
    vals = {}
    for k in _CAP_KEYS:
        v = d.get(k)
        # bool is an int subclass — a True/False slipping in as a cap is a bug.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            reasons.append(f"{k} must be a number")
            continue
        # An infinite cap never fires, and +inf passes `v > 0`, so reject it here.
        if not math.isfinite(v):
            reasons.append(f"{k} must be a finite number")
            continue
        if not (v > 0):
            reasons.append(f"{k} must be a positive number")
            continue
        vals[k] = float(v)
    if "premium_daily_max_usd" in vals and "daily_max_usd" in vals:
        if vals["premium_daily_max_usd"] > vals["daily_max_usd"]:
            reasons.append("premium_daily_max_usd must be <= daily_max_usd")
    if "premium_weekly_max_usd" in vals and "weekly_max_usd" in vals:
        if vals["premium_weekly_max_usd"] > vals["weekly_max_usd"]:
            reasons.append("premium_weekly_max_usd must be <= weekly_max_usd")
    return reasons


def _bump_version(current) -> str:
    """Server-owned version bump; a client-supplied version is never trusted."""
    try:
        parts = str(current).split(".")
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    except (ValueError, TypeError):
        return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def publish_budget(d: dict, actor: str) -> dict:
    """Publish only the five caps with a server-bumped version; invalid caps raise
    ValueError before any write."""
    current_version = load_budget_config().get("version", "1.0")
    new = {k: d.get(k) for k in _CAP_KEYS}
    new["version"] = _bump_version(current_version)
    return config_store.publish(_DOMAIN, new, actor=actor, validate_fn=validate_budget)


def list_budget_versions() -> list:
    return config_store.list_versions(_DOMAIN)


def rollback_budget(archive_name: str, actor: str) -> dict:
    """Re-publish an archived snapshot, re-validated so a corrupted archive can't go live."""
    return config_store.rollback(_DOMAIN, archive_name, actor=actor,
                                 validate_fn=validate_budget)


def estimate_cost_usd(profile, input_tokens: int, output_tokens: int) -> float:
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    input_cost = (input_tokens / 1_000_000) * (profile.input_cost_per_mtok or 0.0)
    output_cost = (output_tokens / 1_000_000) * (profile.output_cost_per_mtok or 0.0)
    return input_cost + output_cost


def _period_spend(db, since: datetime, premium_only: bool = False) -> float:
    from core.database import RoutingModelRun, RoutingModelProfile

    q = db.query(RoutingModelRun).join(
        RoutingModelProfile, RoutingModelRun.model_profile_id == RoutingModelProfile.id
    )
    q = q.filter(RoutingModelRun.created_at >= since)
    if premium_only:
        q = q.filter(RoutingModelProfile.is_premium == True)  # noqa: E712
    return sum(r.cost_usd or 0.0 for r in q.all())


def check_general_budget(db, config: Optional[dict] = None) -> dict:
    """Daily/weekly caps for every candidate; never overridable."""
    cfg = config or load_budget_config()
    now = datetime.utcnow()
    day_start = now - timedelta(hours=24)
    week_start = now - timedelta(days=7)

    daily_spend = _period_spend(db, day_start)
    if daily_spend >= cfg["daily_max_usd"]:
        return {"allowed": False, "reason": f"daily spend ${daily_spend:.2f} >= cap ${cfg['daily_max_usd']:.2f}"}

    weekly_spend = _period_spend(db, week_start)
    if weekly_spend >= cfg["weekly_max_usd"]:
        return {"allowed": False, "reason": f"weekly spend ${weekly_spend:.2f} >= cap ${cfg['weekly_max_usd']:.2f}"}

    return {"allowed": True, "reason": None}


def check_premium_budget(db, config: Optional[dict] = None) -> dict:
    """Premium caps only; `--allow-premium` bypasses this, never check_general_budget."""
    cfg = config or load_budget_config()
    now = datetime.utcnow()
    day_start = now - timedelta(hours=24)
    week_start = now - timedelta(days=7)

    premium_daily = _period_spend(db, day_start, premium_only=True)
    if premium_daily >= cfg["premium_daily_max_usd"]:
        return {"allowed": False, "reason": f"premium daily spend ${premium_daily:.2f} >= cap ${cfg['premium_daily_max_usd']:.2f}"}
    premium_weekly = _period_spend(db, week_start, premium_only=True)
    if premium_weekly >= cfg["premium_weekly_max_usd"]:
        return {"allowed": False, "reason": f"premium weekly spend ${premium_weekly:.2f} >= cap ${cfg['premium_weekly_max_usd']:.2f}"}

    return {"allowed": True, "reason": None}


def check_global_budget(db, profile, config: Optional[dict] = None) -> dict:
    """Both checks; the executor calls them separately so it can skip only premium."""
    general = check_general_budget(db, config)
    if not general["allowed"]:
        return general
    if profile.is_premium:
        return check_premium_budget(db, config)
    return {"allowed": True, "reason": None}


def check_task_budget(db, task, spent_so_far: float, next_estimated_cost: float) -> dict:
    """Per-task cap from RoutingTask.max_cost_usd; NULL means no per-task cap."""
    if task.max_cost_usd is None:
        return {"allowed": True, "reason": None}
    from decimal import Decimal
    projected = Decimal(str(spent_so_far)) + Decimal(str(next_estimated_cost))
    if projected > Decimal(str(task.max_cost_usd)):
        return {
            "allowed": False,
            "reason": f"projected spend ${projected:.4f} would exceed task cap ${task.max_cost_usd:.2f}",
        }
    return {"allowed": True, "reason": None}


def spend_summary(db, since: Optional[datetime] = None) -> dict:
    cfg = load_budget_config()
    now = datetime.utcnow()
    day_start = now - timedelta(hours=24)
    week_start = now - timedelta(days=7)
    month_start = now - timedelta(days=30)
    return {
        "daily": {"spent": round(_period_spend(db, day_start), 4), "cap": cfg["daily_max_usd"]},
        "weekly": {"spent": round(_period_spend(db, week_start), 4), "cap": cfg["weekly_max_usd"]},
        "monthly": {"spent": round(_period_spend(db, month_start), 4), "cap": cfg["monthly_max_usd"]},
        "premium_daily": {"spent": round(_period_spend(db, day_start, premium_only=True), 4), "cap": cfg["premium_daily_max_usd"]},
        "premium_weekly": {"spent": round(_period_spend(db, week_start, premium_only=True), 4), "cap": cfg["premium_weekly_max_usd"]},
    }
