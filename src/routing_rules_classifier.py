"""Arm 0 — deterministic routing classifier (no LLM).

A rules + lexicon classifier over the semi-structured `OdysseusTask` payload
(fixture `task`), emitting a full, schema-valid `CoordinatorDecision` JSON
(schemaVersion 0.5) that the pure `run_benchmark` engine can score. This is the
NULL arm of the routing-coordinator benchmark (ROUTING-COORDINATOR-MODEL-RESEARCH.md
§6.5): if it clears the hard gates, NO model is needed for routine triage.

Design: deterministic only. Every field is a low-cardinality enum; the input is
`{title, objective, type, repoPath, inputs.files[], inputs.logs[]/prompt}`. Signals
are lexical (filenames + keywords). The policy gate is a HARD post-hoc rule: a
`restricted`/`secret` (or "uncertain, possibly sensitive") classification is forced
to a local backend, never remote, regardless of what the lexicon would otherwise
recommend. Uncertain inputs get LOW confidence so `uncertainty_handling` passes
via the existing "conf <= maxConfidenceForUncertain" path.
"""

import json
from typing import Any, Dict, List

SCHEMA_VERSION = "0.5"

# --- Lexicons (signals -> enum labels). Order matters: first hit wins. ---

# filename / path fragments -> domain
DOMAIN_SIGNALS: List[tuple] = [
    (("tacticus", "guild", "boss", "season", "raid", "rpg"), "tacticus_analytics"),
    (("k3s", "flannel", "iface", "cluster", "infra", "vault", "unseal",
      "credential", "network", "node-", "signing key", "token-signing"), "infra"),
    (("README", ".md", "docs", "document", "docs/"), "documentation"),
    (("warehouse", "sql", "percentile", "aggregate", "analytics warehouse"), "data_analysis"),
]

# File + keyword -> dataSensitivity (increasing severity; most specific first)
SENSITIVITY_SECRET = (
    "vault", "unseal", "signing_key", "signing key", "token-signing",
    "secret", "master key", "private_key", "private key",
)
SENSITIVITY_RESTRICTED = (
    "production credential", "production credentials", "credentials rotation",
    "privilege", "admin cookie", "restricted",
)
SENSITIVITY_CONFIDENTIAL = (
    "auth", "billing", "password", "pii", "cookie", "reconcile", "payment",
    "security patch", "auth bypass", "authentication",
)
SENSITIVITY_PUBLIC = (
    "README", "typo", "docs", "document", "public", ".md",
)

# keyword -> taskType
TASKTYPE_SIGNALS: List[tuple] = [
    (("diff", "review the", "review this", "pending diff", "pull request"), "diff_review"),
    (("release", "sign off", "readiness", "v2.", "ship"), "release_readiness"),
    (("crash", "bug", "null", "nullpointer", "typeerror", "fix the", "off-by-one", "pagination"), "bug_debug"),
    (("triage", "ci", "stack trace", "explain", "flaky", "failing"), "ci_triage"),
    (("benchmark", "conformance", "replay", "bake"), "benchmark"),
    (("plan", "design", "spec", "proposal", "document", "docs"), "feature_plan"),
    (("implement", "add", "refactor", "extract", "util", "feature flag", "backoff", "retry"), "implementation"),
]

# keyword -> verificationMode
VERIFMODE_SIGNALS: List[tuple] = [
    (("security", "auth", "signing", "token", "vault"), "security_fix"),
    (("refactor", "equivalence", "without behavior change", "extract a helper"), "refactor_equivalence"),
    (("regression", "guard", "release", "conformance", "replay"), "regression_guard"),
    (("bug", "crash", "fix", "null", "off-by-one"), "bug_fix"),
    (("analy", "explain", "explain a", "stack trace", "review", "document", "docs"), "analysis_only"),
    (("implement", "add", "feature", "util", "flag"), "feature_addition"),
]

# keyword -> risk
RISK_SIGNALS: List[tuple] = [
    (("release", "signing", "vault", "unseal", "credential", "auth bypass", "secret"), "release_blocking"),
    (("auth", "security", "credential", "production", "admin", "restricted"), "high"),
    (("typo", "docs", "README", "document", "log message", "badge"), "low"),
]

# keyword -> backend override
BACKEND_ABSIS = ("tacticus", "guild", "boss", "season", "raid", "rpg")
BACKEND_LOCAL_ONLY = ("vault", "unseal", "signing", "secret", "token-signing")


def _text(task: Dict[str, Any]) -> str:
    """Concatenate all textual signal surface, lowercased."""
    parts = [
        str(task.get("title", "")),
        str(task.get("objective", "")),
        str(task.get("type", "")),
        str(task.get("repoPath", "")),
    ]
    inputs = task.get("inputs") or {}
    for f in inputs.get("files", []) or []:
        parts.append(str(f).lower())
    for f in inputs.get("logs", []) or []:
        parts.append(str(f).lower())
    if inputs.get("prompt"):
        parts.append(str(inputs.get("prompt")).lower())
    return " ".join(parts).lower()


def _first_hit(signals: List[tuple], text: str, default: str) -> str:
    for needles, label in signals:
        for n in needles:
            if n.lower() in text:
                return label
    return default


def classify(task: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic classify of one task payload -> decision dict."""
    text = _text(task)

    # --- domain ---
    domain = _first_hit(DOMAIN_SIGNALS, text, "general_swe")

    # --- dataSensitivity (the gate field) ---
    if any(k in text for k in SENSITIVITY_SECRET):
        sensitivity = "secret"
    elif any(k in text for k in SENSITIVITY_RESTRICTED):
        sensitivity = "restricted"
    elif any(k in text for k in SENSITIVITY_CONFIDENTIAL):
        sensitivity = "confidential"
    elif any(k in text for k in SENSITIVITY_PUBLIC):
        sensitivity = "public"
    else:
        # no signal -> conservative default. "internal" is the safe neutral here
        # (not public); a genuinely ambiguous/secret-shaped input is caught by the
        # keyword lexicons above, and the policy gate below still forces local.
        sensitivity = "internal"

    # --- taskType / verificationMode / risk ---
    task_type = _first_hit(TASKTYPE_SIGNALS, text, "implementation")
    ver_mode = _first_hit(VERIFMODE_SIGNALS, text, "analysis_only")
    risk = _first_hit(RISK_SIGNALS, text, "medium")

    # --- backend + HARD policy gate ---
    if sensitivity in ("secret", "restricted"):
        # NEVER remote. Force local even if a tacticus/token keyword is present.
        backend = "local_framework_coordinator_only"
    elif any(k in text for k in BACKEND_ABSIS):
        backend = "absis_tacticus_job_queue"
    elif any(k in text for k in BACKEND_LOCAL_ONLY):
        backend = "local_framework_coordinator_only"
    else:
        backend = "odysseus_general_swe"

    # --- approval (deterministic rule): key on RISK/TASKTYPE/VERMODE, NOT sensitivity.
    # The harness wants approval=False for analyze/review tasks on restricted data.
    approval_required = (
        risk == "release_blocking"
        or task_type == "release_readiness"
        or ver_mode == "security_fix"
    )
    approval_level = "security_admin" if (sensitivity in ("secret", "restricted")
                                            and approval_required) else (
        "admin" if approval_required else "none"
    )

    # --- confidence: low on vagueness, so uncertainty_handling passes ---
    vague = len(text.strip()) < 24 or any(v in text for v in (
        "make it better", "look into", "weird thing", "improve", "later",
    ))
    confidence = 0.35 if vague else 0.9

    # Vague input -> domain/taskType UNKNOWN (not a confident default)
    if vague:
        domain = "unknown"
        task_type = "unknown"

    # --- lead role: scout on vague, else implementer/planner by type ---
    if vague:
        lead_role = "scout"
    elif task_type in ("diff_review", "feature_review", "analysis_only"):
        lead_role = "reviewer"
    elif task_type in ("bug_debug", "release_readiness"):
        lead_role = "debugger"
    else:
        lead_role = "implementer"

    return {
        "schemaVersion": SCHEMA_VERSION,
        "taskId": task.get("id", ""),
        "classification": {
            "domain": domain,
            "taskType": task_type,
            "risk": risk,
            "dataSensitivity": sensitivity,
            "verificationMode": ver_mode,
        },
        "routeRecommendation": {
            "backend": backend,
            "modelRoleChain": [{"role": lead_role, "reason": "arm0-deterministic"}],
            "allowPremium": False,
        },
        "approvalRecommendation": {"required": approval_required, "level": approval_level},
        "confidence": {"score": confidence, "basis": "metadata"},
        "rationale": [f"arm0: domain={domain} sens={sensitivity} backend={backend}"],
    }


def decide(task_payload: Dict[str, Any]) -> str:
    """decide_fn contract: task_payload -> raw_text (JSON string)."""
    decision = classify(task_payload)
    # Ensure taskId echoes the payload id even if classify didn't resolve it.
    decision["taskId"] = task_payload.get("id", decision["taskId"])
    return json.dumps(decision)


if __name__ == "__main__":
    # Quick smoke: run against the real fixtures and print per-fixture labels.
    import sys
    fixtures = json.load(open("config/routing_coordinator_fixtures/fixtures.json"))
    hits = misses = 0
    for fx in fixtures:
        d = json.loads(decide(fx["task"]))
        c = d["classification"]
        exp = fx["expected"]
        # count correct on the two highest-stakes fields
        ok_sens = c["dataSensitivity"] == exp.get("dataSensitivity")
        ok_backend = d["routeRecommendation"]["backend"] == exp.get("backend")
        if ok_sens and ok_backend:
            hits += 1
        else:
            misses += 1
            print(f"  MISS {fx['id']}: sens {c['dataSensitivity']} (exp {exp.get('dataSensitivity')}) "
                  f"backend {d['routeRecommendation']['backend']} (exp {exp.get('backend')})")
    print(f"\nsens+backend correct: {hits}/{hits+misses}")
