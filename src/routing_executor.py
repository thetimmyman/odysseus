"""src/routing_executor.py — executes routing candidates for a task in scout
mode: resolves each candidate's ModelEndpoint via
endpoint_resolver.resolve_endpoint_by_id (auth/normalization already solved
there, don't rebuild it), calls the model directly via
llm_core.llm_call_with_usage (no agent/tool-calling loop -- passive routing
has no file-editing tools), archives prompt/response to disk, and persists
one RoutingModelRun row per attempt including failures/budget-blocks/skips.

Per architecture review: deliberately does NOT delegate to
llm_call_with_fallback() -- that's stop-at-first-success failover semantics,
but scout mode's job is fan-out-and-compare across the top-K candidates so
Phase 2 scoring has multiple outputs to judge, and llm_call_with_fallback
throws away per-attempt telemetry (including failures) that
historical_score() needs."""
import json
import os
import re
import time
import uuid
from typing import List

from src.endpoint_resolver import resolve_endpoint_by_id
from src.llm_core import llm_call_with_usage
from src.routing_budget import (
    DEFAULT_MAX_OUTPUT_TOKENS, check_general_budget, check_premium_budget,
    check_task_budget, estimate_cost_usd,
)
from src.routing_context import build_context_bundle, estimate_tokens
from src.routing_prompts import build_prompt

ARCHIVE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "routing", "runs"
)

_RATE_LIMIT_RE = re.compile(r"->\s*429\b")


def _classify_llm_error(exc: Exception) -> dict:
    """The only place an upstream HTTP status code survives is inside
    llm_core's HTTPException-formatted message string
    ("Upstream {url} -> {status}: ...") -- it isn't structured anywhere.
    `refused` is deliberately absent here: a refusal is a normal 200 with
    declining text, not detectable from an exception at all -- it stays
    NULL on the RoutingModelRun row until Phase 2 manual scoring sets it."""
    msg = str(exc)
    rate_limited = bool(_RATE_LIMIT_RE.search(msg))
    return {"rate_limited": rate_limited, "errored": not rate_limited, "error_message": msg[:2000]}


def _role_for_profile(profile) -> str:
    """Pick one representative role to render the prompt for, preferring
    whichever of this profile's roles has a dedicated template in
    routing_prompts.py."""
    roles = json.loads(profile.roles) if profile.roles else []
    for preferred in ("implementer", "reviewer", "debugger", "scout", "planner", "escalation"):
        if preferred in roles:
            return preferred
    return "scout"


def _skip(db, run_id, profile_id, model, status, reason, summaries, model_run_id=None):
    """Persist a RoutingModelRun row for a candidate that never actually got
    called (disabled/unresolvable/budget-blocked), so every candidate the
    router considered leaves a trace -- not just the ones that made a real
    API call."""
    from core.database import RoutingModelRun

    model_run_id = model_run_id or str(uuid.uuid4())
    db.add(RoutingModelRun(
        id=model_run_id, run_id=run_id, model_profile_id=profile_id,
        completed=False, errored=False, rate_limited=False,
        error_message=f"{status}: {reason}",
    ))
    db.commit()
    summaries.append({
        "model_run_id": model_run_id, "profile_id": profile_id, "model": model,
        "status": status, "reason": reason,
    })


def execute_candidates(db, task, candidates: List[dict], max_attempts: int,
                        allow_premium_override: bool = False) -> dict:
    """Fan out to the top `max_attempts` candidates from route_task()'s
    ranked list, persisting a RoutingModelRun per attempt (including
    failures/budget-blocks/skips). Returns a dict summarizing the RoutingRun
    (also persisted to the DB).

    `allow_premium_override` bypasses ONLY the premium-specific budget caps
    (check_premium_budget) -- the plain daily/weekly caps (check_general_budget)
    always apply, to every candidate, regardless of this flag."""
    from core.database import RoutingModelProfile, RoutingRun

    run_id = str(uuid.uuid4())
    run = RoutingRun(id=run_id, task_id=task.id, status="running",
                      spend_total_usd=0.0, spend_premium_usd=0.0)
    db.add(run)
    db.commit()

    spent_so_far = 0.0
    premium_spent = 0.0
    attempted = 0       # real API-call attempts (completed or errored), not skips/blocks
    any_blocked = False
    summaries = []

    try:
        bundle = build_context_bundle(task)
        run_dir = os.path.join(ARCHIVE_ROOT, task.id)
        os.makedirs(run_dir, exist_ok=True)

        for candidate in candidates:
            if attempted >= max_attempts:
                break

            profile = db.get(RoutingModelProfile, candidate["profile_id"])
            if not profile or not profile.enabled or not profile.model_endpoint_id:
                any_blocked = True
                _skip(db, run_id, candidate.get("profile_id"), candidate.get("model"),
                      "skipped", "profile disabled, missing, or has no endpoint configured", summaries)
                continue

            general_check = check_general_budget(db)
            if not general_check["allowed"]:
                any_blocked = True
                _skip(db, run_id, profile.id, profile.model, "budget_blocked", general_check["reason"], summaries)
                continue

            if profile.is_premium and not allow_premium_override:
                premium_check = check_premium_budget(db)
                if not premium_check["allowed"]:
                    any_blocked = True
                    _skip(db, run_id, profile.id, profile.model, "budget_blocked", premium_check["reason"], summaries)
                    continue

            task_check = check_task_budget(db, task, spent_so_far, candidate["estimated_cost_usd"])
            if not task_check["allowed"]:
                any_blocked = True
                _skip(db, run_id, profile.id, profile.model, "budget_blocked", task_check["reason"], summaries)
                continue

            attempted += 1
            model_run_id = str(uuid.uuid4())
            prompt_path = None
            t0 = time.time()
            try:
                attempt_dir = os.path.join(run_dir, f"{attempted:03d}-{profile.id}")
                os.makedirs(attempt_dir, exist_ok=True)
                prompt_path = os.path.join(attempt_dir, "prompt.md")

                role = _role_for_profile(profile)
                prompt_text = build_prompt(role, task, bundle)
                with open(prompt_path, "w") as f:
                    f.write(prompt_text)

                resolved = resolve_endpoint_by_id(profile.model_endpoint_id, profile.model)
                if resolved is None:
                    raise RuntimeError(
                        f"could not resolve endpoint {profile.model_endpoint_id!r} for model "
                        f"{profile.model!r} (disabled, missing, or model not available on that endpoint)"
                    )
                chat_url, model_name, headers = resolved

                response_text, usage = llm_call_with_usage(
                    chat_url, model_name, [{"role": "user", "content": prompt_text}],
                    max_tokens=profile.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS,
                    headers=headers, timeout=120, bypass_cache=True,
                )
                # Some providers/models return a null `content` field (a genuine
                # empty completion, not an HTTP error) -- record it as a real,
                # scoreable "completed but empty" outcome rather than crashing on
                # the file write below. A model that does this often should score
                # poorly over time via historical_score(), not silently vanish.
                response_text = response_text or ""
                latency_ms = int((time.time() - t0) * 1000)
                tokens_estimated = usage is None
                input_tokens = usage["input_tokens"] if usage else estimate_tokens(prompt_text)
                output_tokens = usage["output_tokens"] if usage else estimate_tokens(response_text)
                cost = estimate_cost_usd(profile, input_tokens, output_tokens)

                response_path = os.path.join(attempt_dir, "response.md")
                with open(response_path, "w") as f:
                    f.write(response_text)

                from core.database import RoutingModelRun
                db.add(RoutingModelRun(
                    id=model_run_id, run_id=run_id, model_profile_id=profile.id,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    tokens_estimated=tokens_estimated, cost_usd=cost, latency_ms=latency_ms,
                    completed=True, rate_limited=False, errored=False,
                    artifacts=json.dumps({"response_text_path": response_path, "prompt_path": prompt_path}),
                ))
                spent_so_far += cost
                if profile.is_premium:
                    premium_spent += cost
                summaries.append({
                    "model_run_id": model_run_id, "profile_id": profile.id, "model": profile.model,
                    "status": "completed", "cost_usd": round(cost, 4), "latency_ms": latency_ms,
                    "tokens_estimated": tokens_estimated,
                })
            except Exception as e:
                latency_ms = int((time.time() - t0) * 1000)
                classification = _classify_llm_error(e)
                from core.database import RoutingModelRun
                db.add(RoutingModelRun(
                    id=model_run_id, run_id=run_id, model_profile_id=profile.id,
                    latency_ms=latency_ms, completed=False,
                    rate_limited=classification["rate_limited"], errored=classification["errored"],
                    error_message=classification["error_message"],
                    artifacts=json.dumps({"prompt_path": prompt_path}) if prompt_path else None,
                ))
                summaries.append({
                    "model_run_id": model_run_id, "profile_id": profile.id, "model": profile.model,
                    "status": "failed", "reason": classification["error_message"][:200],
                })
            db.commit()

    except Exception as e:
        # Anything unexpected outside the per-candidate try/except (e.g.
        # build_context_bundle itself raising on a malformed task) must still
        # leave the RoutingRun in a terminal state -- otherwise it's stuck at
        # status="running" forever with no way to detect or reconcile it.
        run.status = "failed"
        run.spend_total_usd = spent_so_far
        run.spend_premium_usd = premium_spent
        run.summary = f"crashed after {attempted} candidate(s): {e}"
        db.commit()
        raise

    if any(m.get("status") == "completed" for m in summaries):
        run.status = "succeeded"
    elif attempted > 0:
        run.status = "failed"
    elif any_blocked:
        run.status = "budget_blocked"
    else:
        run.status = "failed"
    run.spend_total_usd = spent_so_far
    run.spend_premium_usd = premium_spent
    run.summary = f"{attempted} candidate(s) attempted"
    db.commit()
    db.refresh(run)

    return {
        "run_id": run_id, "task_id": task.id, "status": run.status,
        "spend_total_usd": round(spent_so_far, 4), "spend_premium_usd": round(premium_spent, 4),
        "model_runs": summaries,
    }
