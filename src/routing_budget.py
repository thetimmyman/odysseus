"""src/routing_budget.py — cost estimation and hard budget-block checks for
the model routing harness. Per architecture review: no approval-gate
machinery for Phase 1/2 (crew_approvals.py's gate is built around pausing a
live streaming session for a human click, which doesn't exist for a CLI
invocation with nothing destructive to gate yet) -- both ceilings below are
hard blocks, bypassable only via an explicit CLI override flag
(`--allow-premium`, wired in routing_executor.py)."""
import json
import logging
import math
import os
from datetime import datetime, timedelta
from typing import Optional

from src import config_store

_log = logging.getLogger(__name__)

# Last successfully-loaded caps for THIS process. Used only to avoid silently
# raising the effective spend ceiling when the live file becomes unreadable
# after we've already read a good one (see load_budget_config).
_last_good_caps: Optional[dict] = None

# The BAKED default (tracked, human-diffable). The LIVE file no longer lives
# here — it's seeded from this into config_store.live_path("routing_budget")
# under the data/ volume so an in-app budget save survives a redeploy
# (previously an edit written back to config/ was silently reverted on the
# next image rebuild). _CONFIG_PATH is kept as the seed source only.
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

# The five caps the versioned editor owns. daily/weekly are the non-overridable
# general ceilings; premium_* are the --allow-premium-bypassable ones;
# monthly_max_usd is ADVISORY (validated as positive but never enforced by a
# check_* function — the UX labels it "not enforced").
_CAP_KEYS = (
    "daily_max_usd",
    "weekly_max_usd",
    "monthly_max_usd",
    "premium_daily_max_usd",
    "premium_weekly_max_usd",
)

# Shared with routing_engine.py's cost-based scoring so the estimate used to
# RANK a candidate matches the actual generation cap used at call time
# (routing_executor.py passes profile.max_output_tokens or this same
# fallback to llm_call_with_usage) -- keeping one constant means a future
# change to the default can't silently make ranking under- or
# over-estimate true worst-case spend.
DEFAULT_MAX_OUTPUT_TOKENS = 4096


def load_budget_config() -> dict:
    """Reads the LIVE budget file from the data/ volume
    (config_store.live_path("routing_budget")), seeded on first read from the
    baked config/routing_budget.json. Re-reads every call (no cache — the
    versioned editor must see a publish immediately). The DEFAULT overlay
    guarantees every cap key is present even if the live file is partial.

    Fail-SAFE direction (spec PR-A review, HIGH): a spend cap must never be
    silently RAISED. DEFAULT_BUDGET_CONFIG is higher than any cap an admin
    tightened below it, so degrading an *unreadable-but-present* live file to
    DEFAULT would silently authorize spend the admin blocked. So:
      - missing file  -> seed + DEFAULT (true first boot; the intended baseline).
      - readable file -> its caps (remembered as last-known-good).
      - present-but-unreadable -> HOLD last-known-good if this process has one;
        only fall back to DEFAULT when we never had a good read, and log loudly.
    The atomic writes in config_store make 'present-but-unreadable' essentially
    external (corruption / an unmounted data volume), not self-inflicted."""
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
    """Fail-safe validator for a published budget. Returns [] when valid, else
    a list of human-readable reasons (config_store.publish turns a non-empty
    list into ValueError, which the route maps to HTTP 400 {detail:[...]}).

    Rules: every cap is a positive number; premium_daily <= daily and
    premium_weekly <= weekly (a premium sub-cap above its general cap can never
    bind); monthly is ADVISORY — validated as positive but not enforced by any
    check_* function (the editor labels it "not enforced")."""
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
        # Reject NaN/Infinity: JSON allows the bare NaN/Infinity literals, and
        # an infinite cap would make `spend >= cap` never fire (a spend cap that
        # never blocks = fail-open). NaN is caught by `not (v > 0)` below, but
        # +inf passes that, so screen non-finite explicitly.
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
    """Auto-bump the server-owned version (never trust a client-supplied one):
    increment the last dotted component of the current version, else fall back
    to a UTC timestamp stamp."""
    try:
        parts = str(current).split(".")
        parts[-1] = str(int(parts[-1]) + 1)
        return ".".join(parts)
    except (ValueError, TypeError):
        return datetime.utcnow().strftime("%Y%m%d-%H%M%S")


def publish_budget(d: dict, actor: str) -> dict:
    """Publish a new budget: take ONLY the five caps from the caller, stamp a
    freshly server-bumped version (client version is ignored), and delegate to
    config_store with validate_budget. Raises ValueError(reasons) on invalid
    caps before any write. Returns the written dict."""
    current_version = load_budget_config().get("version", "1.0")
    new = {k: d.get(k) for k in _CAP_KEYS}
    new["version"] = _bump_version(current_version)
    return config_store.publish(_DOMAIN, new, actor=actor, validate_fn=validate_budget)


def list_budget_versions() -> list:
    """Archived budget snapshots newest-first ([{archive_name, version, ts,
    actor}]), for the editor's version-history list."""
    return config_store.list_versions(_DOMAIN)


def rollback_budget(archive_name: str, actor: str) -> dict:
    """Re-publish an archived budget snapshot (itself a logged publish).
    Traversal-jailed inside the versions dir by config_store.rollback; the
    republish is re-validated so a hand-corrupted archive can't go live."""
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
    """Hard-block check against the plain daily/weekly caps -- applies to
    EVERY candidate regardless of free/paid/premium tier, and is never
    overridable (unlike check_premium_budget below). Returns
    {"allowed": bool, "reason": str|None}."""
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
    """Hard-block check against the premium-specific daily/weekly caps only.
    This is the check `--allow-premium` is meant to bypass -- callers must
    still always call check_general_budget() too, since that one is never
    overridable."""
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
    """Convenience wrapper combining both checks unconditionally (general
    caps always apply; premium caps apply only when `profile.is_premium`).
    routing_executor.py does NOT use this directly -- it calls the two
    checks separately so `--allow-premium` can skip only the premium one."""
    general = check_general_budget(db, config)
    if not general["allowed"]:
        return general
    if profile.is_premium:
        return check_premium_budget(db, config)
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
