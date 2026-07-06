"""src/routing_budget.py — cost estimation and hard budget-block checks for
the model routing harness. Per architecture review: no approval-gate
machinery for Phase 1/2 (crew_approvals.py's gate is built around pausing a
live streaming session for a human click, which doesn't exist for a CLI
invocation with nothing destructive to gate yet) -- both ceilings below are
hard blocks, bypassable only via an explicit CLI override flag
(`--allow-premium`, wired in routing_executor.py)."""
import json
import os
from datetime import datetime, timedelta
from typing import Optional

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


def load_budget_config() -> dict:
    """Reads config/routing_budget.json if present (no versioned/audited UI
    yet -- that's Section 19, out of scope for Phase 1/2), else defaults."""
    if os.path.exists(_CONFIG_PATH):
        try:
            with open(_CONFIG_PATH) as f:
                cfg = json.load(f)
            merged = dict(DEFAULT_BUDGET_CONFIG)
            merged.update(cfg)
            return merged
        except Exception:
            pass
    return dict(DEFAULT_BUDGET_CONFIG)


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


def check_global_budget(db, profile, config: Optional[dict] = None) -> dict:
    """Hard-block check against daily/weekly (+ premium-specific) caps.
    Returns {"allowed": bool, "reason": str|None}."""
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

    if profile.is_premium:
        premium_daily = _period_spend(db, day_start, premium_only=True)
        if premium_daily >= cfg["premium_daily_max_usd"]:
            return {"allowed": False, "reason": f"premium daily spend ${premium_daily:.2f} >= cap ${cfg['premium_daily_max_usd']:.2f}"}
        premium_weekly = _period_spend(db, week_start, premium_only=True)
        if premium_weekly >= cfg["premium_weekly_max_usd"]:
            return {"allowed": False, "reason": f"premium weekly spend ${premium_weekly:.2f} >= cap ${cfg['premium_weekly_max_usd']:.2f}"}

    return {"allowed": True, "reason": None}


def check_task_budget(db, task, spent_so_far: float, next_estimated_cost: float) -> dict:
    """Per-task hard-block check against RoutingTask.max_cost_usd. NULL
    max_cost_usd means no explicit per-task cap (still subject to the
    global/period budget in check_global_budget)."""
    if task.max_cost_usd is None:
        return {"allowed": True, "reason": None}
    projected = spent_so_far + next_estimated_cost
    if projected > task.max_cost_usd:
        return {
            "allowed": False,
            "reason": f"projected spend ${projected:.4f} would exceed task cap ${task.max_cost_usd:.2f}",
        }
    return {"allowed": True, "reason": None}


def spend_summary(db, since: Optional[datetime] = None) -> dict:
    """Used by `odysseus budget status` / `odysseus summarize`."""
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
