#!/usr/bin/env python3
"""scripts/seed_routing_profiles.py — one-time (idempotent) seed of
RoutingModelProfile rows from the 2026-07-06 benchmark data, not the
routing-harness spec doc's untested placeholders. Safe to re-run: upserts by
id rather than duplicating rows.

    scripts/seed_routing_profiles.py
"""
from __future__ import annotations
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "_lib"))
from cli import quiet_logs
quiet_logs()

import json

from core.database import SessionLocal, ModelEndpoint, RoutingModelProfile


def _find_endpoint_id(db, name: str):
    ep = db.query(ModelEndpoint).filter(ModelEndpoint.name == name).first()
    return ep.id if ep else None


PROFILES = [
    # Local Framework GPU models -- zero marginal cost, private, no
    # third-party data exposure. Roles/notes come from the 2026-07-06
    # 10-test gauntlet (isolated-clone real bug fix graded against a
    # held-back oracle suite + fact-checked design proposal + adversarial
    # review + implementation), NOT the lighter OpenRouter bulk-task
    # benchmark the free-tier profiles below are seeded from -- keep that
    # distinction when updating notes from a future benchmark.
    {
        "id": "qwen3-coder-next",
        "endpoint_name": "Framework llama.cpp (Qwen3-Coder-Next, GPU)",
        "model": "qwen3-coder-next",
        "roles": ["implementer", "debugger"],
        "context_window": 131_072,
        "max_output_tokens": 4096,
        "is_free": True,
        "notes": (
            "Local Framework GPU model, current Odysseus coding default. "
            "2026-07-06 10-test gauntlet vs qwen3.6-35b-a3b-ud-q4km: decisive "
            "win on raw code generation (6/6 vs 1/6 on the synthetic hard "
            "gauntlet), roughly tied on a real production bug fix (test7, "
            "graded against a held-back 26-test oracle suite). Only one local "
            "model fits in GPU memory at a time -- verify `llamacpp-coder` is "
            "the container currently up before routing here."
        ),
    },
    {
        "id": "qwen36-35b-a3b-ud-q4km",
        "endpoint_name": "Framework llama.cpp (Qwen3.6-35B-A3B UD-Q4_K_M, GPU)",
        "model": "qwen3.6-35b-a3b-ud-q4km",
        "roles": ["planner"],
        "context_window": 32_768,
        "max_output_tokens": 4096,
        "is_free": True,
        "notes": (
            "Local Framework GPU model. 2026-07-06 10-test gauntlet vs "
            "qwen3-coder-next: only edged ahead on design-proposal/plan "
            "quality (test8), not on writing correct code -- do NOT give this "
            "an implementer role. Never converged on a written deliverable for "
            "the adversarial-review task (test9) even after nudges, for "
            "either model -- looks like a real behavioral limit on very "
            "open-ended/unbounded tasks, not a routing or prompt fix, so no "
            "reviewer role either until that's re-tested. Only one local model "
            "fits in GPU memory at a time -- currently NOT the one loaded "
            "(check `docker ps llamacpp-qwen36-35b` before routing here)."
        ),
    },
    {
        "id": "nemotron-super-free",
        "endpoint_name": "openrouter-bench",
        "model": "nvidia/nemotron-3-super-120b-a12b:free",
        "roles": ["scout", "planner", "reviewer", "debugger"],
        "context_window": 1_000_000,
        "max_output_tokens": 4096,
        "is_free": True,
        "notes": (
            "Standout free model (2026-07-06 benchmark): 6/6 on the gtt-bench hard "
            "gauntlet, matching top local models. Correctly fixed a real, narrow, "
            "single-target production bug unprompted (test7) -- debugger role is for "
            "that kind of bounded bug, not open-ended implementation, where it "
            "fabricates stub code with hardcoded fake data (test10) and needs explicit "
            "nudges to write output at all for design/review tasks (test8/9)."
        ),
    },
    {
        "id": "hy3-free",
        "endpoint_name": "openrouter-bench",
        "model": "tencent/hy3:free",  # NOT hy3-preview -- that's a different, untested listing
        "roles": ["scout", "reviewer"],
        "context_window": 262_144,
        "max_output_tokens": 4096,
        "is_free": True,
        "notes": (
            "Excellent on the simple bulk-task benchmark (classify/summarize/grep), "
            "weaker on the hard gauntlet (3/6, null-responses on harder problems). "
            "CAVEAT: observed self-identifying as 'Claude, made by Anthropic' in some "
            "responses -- avoid for identity-sensitive tasks."
        ),
    },
    {
        "id": "nemotron-nano-omni-free",
        "endpoint_name": "openrouter-bench",
        "model": "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
        "roles": ["scout", "reviewer"],
        "context_window": 256_000,
        "max_output_tokens": 4096,
        "is_free": True,
        "notes": "Strong second on the hard gauntlet (4/6) and bulk-task benchmark; smaller/faster than nemotron-super.",
    },
    {
        "id": "poolside-xs2-free",
        "endpoint_name": "openrouter-bench",
        "model": "poolside/laguna-xs.2:free",
        "roles": ["scout", "reviewer"],
        "context_window": 262_144,
        "max_output_tokens": 4096,
        "is_free": True,
        "notes": "Excellent on the bulk-task benchmark, weak on hard/long code-gen (1/6 gauntlet, null-responses on harder problems).",
    },
    # Placeholders from the source spec's Section 5 registry -- no API
    # key/endpoint registered for these yet, seeded disabled so
    # routing_engine never routes to them until Tim adds real credentials.
    {
        "id": "glm-5.2",
        "endpoint_name": None,
        "model": "z-ai/glm-5.2",
        "roles": ["debugger", "planner", "implementer", "reviewer"],
        "context_window": 1_000_000,
        "max_output_tokens": 8192,
        "input_cost_per_mtok": 1.40,
        "output_cost_per_mtok": 4.40,
        "enabled": False,
        "notes": "Placeholder from the routing harness spec -- untested, no ModelEndpoint registered yet.",
    },
    {
        "id": "deepseek-v4-flash",
        "endpoint_name": None,
        "model": "deepseek/deepseek-v4-flash",
        "roles": ["scout", "reviewer"],
        "context_window": 1_000_000,
        "max_output_tokens": 8192,
        "input_cost_per_mtok": 0.14,
        "output_cost_per_mtok": 0.28,
        "enabled": False,
        "notes": "Placeholder from the routing harness spec -- untested, no ModelEndpoint registered yet.",
    },
    {
        "id": "codex-5.5",
        "endpoint_name": None,
        "model": "gpt-5.5",
        "roles": ["debugger", "implementer", "escalation"],
        "context_window": 1_000_000,
        "max_output_tokens": 16384,
        "is_premium": True,
        "input_cost_per_mtok": 5.0,
        "output_cost_per_mtok": 30.0,
        "enabled": False,
        "notes": "Placeholder from the routing harness spec -- premium escalation model, no ModelEndpoint registered yet.",
    },
]


def main():
    db = SessionLocal()
    try:
        for spec in PROFILES:
            existing = db.get(RoutingModelProfile, spec["id"])
            endpoint_id = None
            if spec.get("endpoint_name"):
                endpoint_id = _find_endpoint_id(db, spec["endpoint_name"])
                if not endpoint_id:
                    print(f"warning: endpoint {spec['endpoint_name']!r} not found", file=sys.stderr)
                    # Don't silently disable a profile that was already working
                    # against a (presumably still-valid) endpoint just because
                    # this endpoint_name lookup failed this run -- e.g. the
                    # endpoint was renamed and the spec list above is stale.
                    # Leave it untouched and move on rather than clobbering it.
                    if existing and existing.enabled and existing.model_endpoint_id:
                        print(f"  -- leaving existing profile {spec['id']!r} untouched "
                              f"(currently enabled={existing.enabled}, endpoint={existing.model_endpoint_id!r})",
                              file=sys.stderr)
                        continue
                    print(f"  -- seeding {spec['id']!r} disabled", file=sys.stderr)

            # Explicit enabled=False (the paid placeholders) always wins;
            # otherwise a profile is only enabled if its endpoint resolved.
            enabled = False if spec.get("enabled") is False else endpoint_id is not None

            fields = dict(
                model_endpoint_id=endpoint_id,
                model=spec["model"],
                roles=json.dumps(spec["roles"]),
                context_window=spec.get("context_window"),
                max_output_tokens=spec.get("max_output_tokens"),
                input_cost_per_mtok=spec.get("input_cost_per_mtok", 0.0),
                output_cost_per_mtok=spec.get("output_cost_per_mtok", 0.0),
                is_free=spec.get("is_free", False),
                is_premium=spec.get("is_premium", False),
                enabled=enabled,
                notes=spec.get("notes"),
            )
            if existing:
                for k, v in fields.items():
                    setattr(existing, k, v)
                print(f"updated: {spec['id']} (enabled={enabled})")
            else:
                db.add(RoutingModelProfile(id=spec["id"], **fields))
                print(f"created: {spec['id']} (enabled={enabled})")
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
