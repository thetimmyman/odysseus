"""Deterministic rules + lexicon routing classifier (no LLM).

The null arm of the coordinator benchmark: emits a schema-valid
CoordinatorDecision from lexical signals. Restricted/secret or possibly
sensitive tasks are always forced local. Uncertain inputs get low confidence.
"""

import json
from typing import Any, Dict, List

SCHEMA_VERSION = "0.5"

# Lexicon order matters: first hit wins.

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
    # Raise only on unambiguous danger; sensitivity is a separate field.
    (("sign off", "release readiness", "release_blocking", "v2", "ship",
      "auth bypass", "signing key", "token-signing", "vault unseal", "master key"),
     "release_blocking"),
    (("auth ", "security", "production credential", "admin cookie",
      "debug the vault", "path traversal", "credential rotation"),
     "high"),
]

# keyword -> backend override
BACKEND_ABSIS = ("tacticus", "guild", "boss", "season", "raid", "rpg")
BACKEND_LOCAL_ONLY = ("vault", "unseal", "signing", "secret", "token-signing")


def _text(task: Dict[str, Any]) -> str:
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
    text = _text(task)

    domain = _first_hit(DOMAIN_SIGNALS, text, "general_swe")

    if any(k in text for k in SENSITIVITY_SECRET):
        sensitivity = "secret"
    elif any(k in text for k in SENSITIVITY_RESTRICTED):
        sensitivity = "restricted"
    elif any(k in text for k in SENSITIVITY_CONFIDENTIAL):
        sensitivity = "confidential"
    elif any(k in text for k in SENSITIVITY_PUBLIC):
        sensitivity = "public"
    else:
        # No signal: "internal" is the safe neutral default, not public.
        sensitivity = "internal"

    task_type = _first_hit(TASKTYPE_SIGNALS, text, "implementation")
    ver_mode = _first_hit(VERIFMODE_SIGNALS, text, "analysis_only")

    # Risk keys on the action: only mutating task types can be raised by danger keywords.
    non_mutating = task_type in ("diff_review", "feature_review", "feature_plan",
                                  "analysis_only") or ver_mode in ("analysis_only",)
    if non_mutating:
        risk = "low"
    else:
        risk = _first_hit(RISK_SIGNALS, text, "low")

    if sensitivity in ("secret", "restricted"):
        # Never remote, whatever the keywords suggest.
        backend = "local_framework_coordinator_only"
    elif any(k in text for k in BACKEND_ABSIS):
        backend = "absis_tacticus_job_queue"
    elif any(k in text for k in BACKEND_LOCAL_ONLY):
        backend = "local_framework_coordinator_only"
    else:
        backend = "odysseus_general_swe"

    # Approval keys on risk/task type/verification mode, not sensitivity.
    approval_required = (
        risk == "release_blocking"
        or task_type == "release_readiness"
        or ver_mode == "security_fix"
    )
    approval_level = "security_admin" if (sensitivity in ("secret", "restricted")
                                            and approval_required) else (
        "admin" if approval_required else "none"
    )

    vague = len(text.strip()) < 24 or any(v in text for v in (
        "make it better", "look into", "weird thing", "improve", "later",
    ))
    confidence = 0.35 if vague else 0.9

    # Vague input -> domain/taskType UNKNOWN (not a confident default)
    if vague:
        domain = "unknown"
        task_type = "unknown"

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
    decision = classify(task_payload)
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
        ok_sens = c["dataSensitivity"] == exp.get("dataSensitivity")
        ok_backend = d["routeRecommendation"]["backend"] == exp.get("backend")
        if ok_sens and ok_backend:
            hits += 1
        else:
            misses += 1
            print(f"  MISS {fx['id']}: sens {c['dataSensitivity']} (exp {exp.get('dataSensitivity')}) "
                  f"backend {d['routeRecommendation']['backend']} (exp {exp.get('backend')})")
    print(f"\nsens+backend correct: {hits}/{hits+misses}")
