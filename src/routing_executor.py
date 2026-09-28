"""Execute routing candidates in scout mode, persisting a RoutingModelRun per attempt.

Not llm_call_with_fallback(): scout mode fans out across the top-K candidates
to compare them, and historical_score() needs every attempt's telemetry,
including failures."""
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from decimal import Decimal
from typing import List

from src import routing_policy
from src.endpoint_resolver import resolve_endpoint_by_id
from src.llm_core import llm_call_with_usage
from src.routing_budget import (
    DEFAULT_MAX_OUTPUT_TOKENS, check_general_budget, check_premium_budget,
    check_task_budget, estimate_cost_usd,
)
from src.routing_context import build_context_bundle, estimate_tokens
from src.routing_engine import ROLE_BY_TASK, _PATCH_SHAPED_TASK_TYPES
from src.routing_patch import extract_diff, validate_patch_shape
from src.routing_prompts import build_prompt, render_context_block, render_universal_wrapper
from src.routing_workdir import data_root
from src.routing_outcomes import export_after_commit


def archive_root() -> str:
    """Per-run artifact archive, resolved per call so ODYSSEUS_DATA_DIR applies."""
    return os.path.join(data_root(), "routing", "runs")

_RATE_LIMIT_RE = re.compile(r"->\s*429\b")


def _classify_llm_error(exc: Exception) -> dict:
    """Parse the status from llm_core's "Upstream {url} -> {status}" message.

    `refused` is absent: a refusal is a normal 200 and is scored manually."""
    msg = str(exc)
    rate_limited = bool(_RATE_LIMIT_RE.search(msg))
    return {"rate_limited": rate_limited, "errored": not rate_limited, "error_message": msg[:2000]}


def _git_head_sha(repo_path: str) -> str:
    """Best-effort HEAD sha; "" on any failure so provenance never blocks a run."""
    try:
        out = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _sha256_hex(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def _write_run_manifest(db, task, run_id: str, run_dir: str, bundle: dict) -> None:
    """Write the run's provenance manifest to disk (survives DB loss) and the DB.

    Per-attempt file paths live in RoutingModelRun.artifacts."""
    from core.database import RunManifestRecord

    constraints = json.loads(task.constraints) if task.constraints else []
    system_prompt = render_universal_wrapper(task.objective, constraints)
    task_prompt = bundle.get("prompt") or task.objective or ""
    context_str = render_context_block(bundle)
    manifest_path = os.path.join(run_dir, "manifest.json")
    manifest = {
        "runId": run_id,
        "taskId": task.id,
        "repo": {
            "repoPath": task.repo_path,
            "baseCommitSha": _git_head_sha(task.repo_path),
            "branch": task.branch_name or "",
        },
        "prompts": {
            "systemPromptHash": _sha256_hex(system_prompt),
            "taskPromptHash": _sha256_hex(task_prompt),
            "contextBundleHash": _sha256_hex(context_str),
        },
        "policy": routing_policy.policy_versions(),
        "verificationMode": task.verification_mode or None,
        "dataSensitivity": task.data_sensitivity or "internal",
        "context": {
            "sources": bundle.get("sources") or [],
            "redactionApplied": bool((bundle.get("metadata") or {}).get("redaction_applied")),
        },
        "artifacts": {
            "runDir": run_dir,
            "manifestPath": manifest_path,
            "promptPath": None,
            "responsePath": None,
        },
        "auditNotes": [
            "promptPath/responsePath recorded per model-run in RoutingModelRun.artifacts",
        ],
    }
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    db.add(RunManifestRecord(
        id=str(uuid.uuid4()), run_id=run_id, manifest=json.dumps(manifest),
    ))
    db.commit()


def _role_for_profile(profile, task) -> str:
    """Pick the prompt role; the task's desired roles win over the profile's own."""
    roles = json.loads(profile.roles) if profile.roles else []
    for preferred in ROLE_BY_TASK.get(task.task_type, []):
        if preferred in roles:
            return preferred
    for preferred in ("implementer", "reviewer", "debugger", "scout", "planner", "escalation"):
        if preferred in roles:
            return preferred
    return "scout"


def _skip(db, run_id, profile_id, model, status, reason, summaries, model_run_id=None):
    """Record a candidate that was never called, so every considered candidate leaves a trace."""
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
    """Fan out to the top `max_attempts` candidates and summarize the RoutingRun.

    `allow_premium_override` bypasses only the premium caps, never the general ones."""
    from core.database import RoutingModelProfile, RoutingRun

    run_id = str(uuid.uuid4())
    run = RoutingRun(id=run_id, task_id=task.id, status="running",
                      spend_total_usd=0.0, spend_premium_usd=0.0)
    db.add(run)
    db.commit()

    spent_so_far = Decimal(0)
    premium_spent = Decimal(0)
    attempted = 0       # real API-call attempts (completed or errored), not skips/blocks
    any_blocked = False
    summaries = []

    try:
        bundle = build_context_bundle(task)
        # Nested under run_id: attempt numbers restart per call, so a re-run
        # would otherwise overwrite files that old rows still point at.
        run_dir = os.path.join(archive_root(), task.id, run_id)
        os.makedirs(run_dir, exist_ok=True)

        # Manifest first, so a crash mid-fan-out keeps provenance; no manifest, no run.
        _write_run_manifest(db, task, run_id, run_dir, bundle)

        from src.promotional_dispatch import prefer_verified_free
        from src.offer_economics import credential_fingerprint
        from src.llm_core import _detect_provider
        prepared = {}
        def prepared_request(candidate):
            profile_id = candidate["profile_id"]
            if profile_id not in prepared:
                item = db.get(RoutingModelProfile, profile_id)
                prompt = build_prompt(_role_for_profile(item, task), task, bundle)
                prepared[profile_id] = (prompt, {"input_tokens": estimate_tokens(prompt),
                    "output_tokens": item.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS,
                    "cache_read_tokens": 0, "cache_write_tokens": 0})
            return prepared[profile_id]
        def resolve_offer_candidate(candidate):
            offer_profile = db.get(RoutingModelProfile, candidate["profile_id"])
            if offer_profile is None or not offer_profile.enabled or not offer_profile.model_endpoint_id:
                raise ValueError("unavailable profile")
            resolved = resolve_endpoint_by_id(offer_profile.model_endpoint_id, offer_profile.model)
            if resolved is None:
                raise ValueError("unavailable endpoint")
            return resolved[1], resolved[0], {"endpoint_id": offer_profile.model_endpoint_id,
                "credential_sha256": credential_fingerprint(resolved[2]), "transport_provider": _detect_provider(resolved[0])}
        candidates = prefer_verified_free(candidates, resolve_candidate=resolve_offer_candidate)
        from src.offer_economics import prefer_discounted
        candidates = prefer_discounted(candidates, resolve_candidate=resolve_offer_candidate,
                                       workload=lambda c: prepared_request(c)[1])
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

            # With scoped offers, evaluate the fresh quote below; the old
            # catalog estimate must not reject a genuinely discounted request.
            offer_policy_active = bool(os.environ.get("ODYSSEUS_OFFER_CONFIG") or os.environ.get("ODYSSEUS_FREE_OFFER_CONFIG"))
            task_check = ({"allowed": True} if offer_policy_active else
                          check_task_budget(db, task, spent_so_far, candidate["estimated_cost_usd"]))
            if not task_check["allowed"]:
                any_blocked = True
                _skip(db, run_id, profile.id, profile.model, "budget_blocked", task_check["reason"], summaries)
                continue

            attempted += 1
            model_run_id = str(uuid.uuid4())
            prompt_path = None
            inference_attempted = False
            inference_completed = False
            reserved_cost = Decimal(0)
            accounted_cost = Decimal(0)
            cash_quote = None
            cost = Decimal(0)
            t0 = time.time()
            try:
                attempt_dir = os.path.join(run_dir, f"{attempted:03d}-{profile.id}")
                os.makedirs(attempt_dir, exist_ok=True)
                prompt_path = os.path.join(attempt_dir, "prompt.md")

                prompt_text, actual_workload = prepared_request(candidate)
                with open(prompt_path, "w") as f:
                    f.write(prompt_text)

                resolved = resolve_endpoint_by_id(profile.model_endpoint_id, profile.model)
                if resolved is None:
                    raise RuntimeError(
                        f"could not resolve endpoint {profile.model_endpoint_id!r} for model "
                        f"{profile.model!r} (disabled, missing, or model not available on that endpoint)"
                    )
                chat_url, model_name, headers = resolved
                identity = {"endpoint_id": profile.model_endpoint_id,
                    "credential_sha256": credential_fingerprint(headers), "transport_provider": _detect_provider(chat_url)}

                from src.promotional_dispatch import enforce_free_offer
                offer_evidence = enforce_free_offer(profile_id=profile.id, model=model_name,
                    chat_url=chat_url, harness="odysseus-scout", **identity)
                from src.offer_economics import configured_quote
                cash_quote = configured_quote(profile_id=profile.id, model=model_name,
                    chat_url=chat_url, harness="odysseus-scout", workload=actual_workload, **identity)
                if cash_quote is not None:
                    paid_permission = task.allow_premium_models if profile.is_premium else task.allow_paid_models
                    if Decimal(cash_quote["predicted_cash_usd"]) > 0 and not paid_permission:
                        raise ValueError("task does not authorize a paid offer")
                    quote_budget = check_task_budget(db, task, spent_so_far, Decimal(cash_quote["predicted_cash_usd"]))
                    if not quote_budget["allowed"]:
                        raise ValueError("offer quote exceeds existing task budget")
                elif offer_policy_active:
                    # A scoped cash offer says nothing about omitted profiles.
                    # Preserve their original task budget before dispatch.
                    catalog_budget = check_task_budget(db, task, spent_so_far, candidate["estimated_cost_usd"])
                    if not catalog_budget["allowed"]:
                        raise ValueError("unconfigured candidate exceeds existing task budget")
                inference_attempted = True
                reserved_cost = Decimal(cash_quote["predicted_cash_usd"]) if cash_quote else Decimal(str(candidate["estimated_cost_usd"]))
                accounted_cost = reserved_cost
                spent_so_far += accounted_cost
                if profile.is_premium:
                    premium_spent += accounted_cost
                response_text, usage = llm_call_with_usage(
                    chat_url, model_name, [{"role": "user", "content": prompt_text}],
                    max_tokens=actual_workload["output_tokens"],
                    headers=headers, timeout=120, bypass_cache=True,
                )
                inference_completed = True
                # Null content is a scoreable empty completion, not an error.
                response_text = response_text or ""
                latency_ms = int((time.time() - t0) * 1000)
                tokens_estimated = usage is None
                input_tokens = usage["input_tokens"] if usage else estimate_tokens(prompt_text)
                output_tokens = usage["output_tokens"] if usage else estimate_tokens(response_text)
                cost = Decimal(str(estimate_cost_usd(profile, input_tokens, output_tokens)))
                if cash_quote is not None:
                    from src.offer_economics import comparable_cash
                    from src.provider_model_offer import provider_model_offer_from_dict
                    cost = comparable_cash(
                        [provider_model_offer_from_dict(o) for o in cash_quote["offers"]],
                        {"input_tokens": input_tokens, "output_tokens": output_tokens,
                         "cache_read_tokens": 0, "cache_write_tokens": 0})
                spent_so_far += cost - accounted_cost
                if profile.is_premium:
                    premium_spent += cost - accounted_cost
                accounted_cost = cost

                response_path = os.path.join(attempt_dir, "response.md")
                with open(response_path, "w") as f:
                    f.write(response_text)

                artifacts = {"response_text_path": response_path, "prompt_path": prompt_path,
                             "inference_attempted": True, "upstream_error": False}
                if offer_evidence is not None:
                    artifacts["promotion"] = offer_evidence
                if cash_quote is not None:
                    artifacts["offer_quote"] = cash_quote
                    artifacts["cost_basis"] = "offer_tariff_estimate_not_provider_invoice"
                    artifacts["predicted_ceiling_exceeded"] = cost > Decimal(cash_quote["maximum_predicted_request_usd"])
                patch_validation = None
                patch_summary = None
                # Patch extraction only; applies to patch-producing task types.
                if task.task_type in _PATCH_SHAPED_TASK_TYPES:
                    diff_text = extract_diff(response_text)
                    patch_validation = validate_patch_shape(diff_text, task.repo_path)
                    if patch_validation["extracted"]:
                        patch_path = os.path.join(attempt_dir, "patch.diff")
                        with open(patch_path, "w") as f:
                            f.write(diff_text)
                        artifacts["patch_path"] = patch_path
                    patch_summary = {
                        "extracted": patch_validation["extracted"],
                        "allowed": patch_validation["allowed"],
                        "file_count": patch_validation["file_count"],
                        "changed_lines": patch_validation["changed_lines"],
                        "reasons": patch_validation["reasons"],
                    }

                from core.database import RoutingModelRun
                db.add(RoutingModelRun(
                    id=model_run_id, run_id=run_id, model_profile_id=profile.id,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                    tokens_estimated=tokens_estimated, cost_usd=float(cost), latency_ms=latency_ms,
                    completed=True, rate_limited=False, errored=False,
                    artifacts=json.dumps(artifacts),
                    patch_validation=json.dumps(patch_validation) if patch_validation is not None else None,
                ))
                summary_entry = {
                    "model_run_id": model_run_id, "profile_id": profile.id, "model": profile.model,
                    "status": "completed", "cost_usd": round(float(cost), 4), "latency_ms": latency_ms,
                    "tokens_estimated": tokens_estimated,
                }
                if patch_summary is not None:
                    summary_entry["patch"] = patch_summary
                if cash_quote is not None:
                    summary_entry["predicted_ceiling_exceeded"] = artifacts["predicted_ceiling_exceeded"]
                summaries.append(summary_entry)
            except Exception as e:
                latency_ms = int((time.time() - t0) * 1000)
                classification = _classify_llm_error(e)
                from core.database import RoutingModelRun
                db.add(RoutingModelRun(
                    id=model_run_id, run_id=run_id, model_profile_id=profile.id,
                    latency_ms=latency_ms, completed=False,
                    cost_usd=float(accounted_cost),
                    rate_limited=classification["rate_limited"], errored=classification["errored"],
                    error_message=classification["error_message"],
                    artifacts=json.dumps({"prompt_path": prompt_path,
                        "inference_attempted": inference_attempted,
                        "inference_completed": inference_completed,
                        "retained_cost_usd": str(accounted_cost),
                        "offer_quote": cash_quote,
                        "cost_basis": "conservative_quote_reserve" if accounted_cost == reserved_cost else "offer_tariff_estimate_not_provider_invoice",
                        "upstream_error": inference_attempted and not inference_completed}),
                ))
                summaries.append({
                    "model_run_id": model_run_id, "profile_id": profile.id, "model": profile.model,
                    "status": "failed", "reason": classification["error_message"][:200],
                })
            db.commit()
            export_after_commit(db, task.id)
            if offer_policy_active and inference_attempted and not any(m.get("model_run_id") == model_run_id and m.get("status") == "completed" for m in summaries):
                # Billing is uncertain after transport or post-processing errors;
                # retain reserve and stop this invocation instead of paid fallback.
                break

    except Exception as e:
        # Always leave the RoutingRun terminal, never stuck at "running".
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
        "spend_total_usd": round(float(spent_so_far), 4), "spend_premium_usd": round(float(premium_spent), 4),
        "model_runs": summaries,
    }
