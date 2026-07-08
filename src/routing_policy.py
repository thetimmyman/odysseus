"""src/routing_policy.py — versioned routing-policy config (spec Section 19).

The live policy is config/routing_policy.json (tracked, human-diffable, same
path-resolution approach as routing_budget.py's config). Every publish
archives the outgoing file to data/routing/policy_versions/ and appends to a
publish log, so a policy change is never silent and rollback is itself a
logged publish — the archive dir is append-only in normal operation.
"""
import copy
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional

from src.routing_budget import load_budget_config

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICY_PATH = os.path.join(_ROOT, "config", "routing_policy.json")
# Sibling of routing_executor's archive root (data/routing/runs). Resolved
# with the same repo-root expression rather than imported: routing_executor
# imports this module for RunManifest policy stamping, so importing back from
# it would be circular (and drags llm_core/DB into a pure-config module).
POLICY_VERSIONS_DIR = os.path.join(_ROOT, "data", "routing", "policy_versions")

DEFAULT_POLICY = {
    "routingPolicyVersion": "1.0",
    "verificationPolicyVersion": "1.0",
    "uiConfigVersion": "1.0",
    "coordinator": {
        "provider": "external",
        "endpointName": None,
        "model": None,
        "temperature": 0.1,
        "maxTokens": 2048,
    },
    "maxUntrustedTokens": 256,
    "rawOutputMaxBytes": 262144,
    "remoteSensitivityCeiling": "confidential",
    # Phase 3 Safe Execution (spec Section 15): resource limits + command
    # allowlist for routing_sandbox.run_in_sandbox. allowedCommands entries
    # are normalized prefixes -- a command is allowed iff it equals an entry
    # or starts with entry + " " (and carries no shell metacharacters).
    "sandbox": {
        "image": "python:3.12-slim",
        "cpus": 2,
        "memoryGb": 4,
        "pidsLimit": 256,
        "wallClockSeconds": 600,
        "maxOutputBytes": 1048576,
        "allowedCommands": [
            "pytest",
            "python -m pytest",
            "python -m py_compile",
            "npm test",
            "node --check",
            "ruff check",
            "eslint",
            "tsc --noEmit",
            "make test",
        ],
    },
}

_REQUIRED_VERSION_KEYS = ("routingPolicyVersion", "verificationPolicyVersion", "uiConfigVersion")

# (path, mtime, parsed) — invalidated whenever the file's mtime moves, so a
# publish (or a hand edit) is picked up without a process restart.
_cache: Optional[tuple] = None


def load_policy() -> dict:
    """Current policy dict; DEFAULT_POLICY copy when no file exists (or it's
    unreadable — a corrupt policy file must degrade to defaults, not 500
    every wrap call)."""
    global _cache
    try:
        mtime = os.path.getmtime(POLICY_PATH)
    except OSError:
        return copy.deepcopy(DEFAULT_POLICY)
    if _cache and _cache[0] == POLICY_PATH and _cache[1] == mtime:
        return copy.deepcopy(_cache[2])
    try:
        with open(POLICY_PATH) as f:
            policy = json.load(f)
        if not isinstance(policy, dict):
            raise ValueError("policy file is not a JSON object")
    except Exception:
        return copy.deepcopy(DEFAULT_POLICY)
    _cache = (POLICY_PATH, mtime, policy)
    return copy.deepcopy(policy)


def policy_versions() -> dict:
    """The four version stamps recorded on every audit row / RunManifest.
    budgetPolicyVersion comes from config/routing_budget.json's "version"
    (that file is owned by routing_budget.py, not versioned through
    publish_policy — "unversioned" flags a pre-versioning config)."""
    p = load_policy()
    try:
        budget_version = load_budget_config().get("version", "unversioned")
    except Exception:
        budget_version = "unversioned"
    return {
        "routingPolicyVersion": p.get("routingPolicyVersion", "unversioned"),
        "verificationPolicyVersion": p.get("verificationPolicyVersion", "unversioned"),
        "uiConfigVersion": p.get("uiConfigVersion", "unversioned"),
        "budgetPolicyVersion": budget_version,
    }


def _validate_policy(new_policy: dict) -> None:
    if not isinstance(new_policy, dict):
        raise ValueError("policy must be a JSON object")
    for key in _REQUIRED_VERSION_KEYS:
        if not isinstance(new_policy.get(key), str) or not new_policy.get(key):
            raise ValueError(f"policy.{key} is required and must be a non-empty string")


def publish_policy(new_policy: dict, actor: str) -> dict:
    """Validate, archive the outgoing policy, write the new one, log the
    publish. Archive-before-overwrite means a bad publish is always
    recoverable via rollback_policy()."""
    global _cache
    _validate_policy(new_policy)

    os.makedirs(POLICY_VERSIONS_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(POLICY_PATH), exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    if os.path.exists(POLICY_PATH):
        try:
            with open(POLICY_PATH) as f:
                current_raw = f.read()
            current_version = "unknown"
            try:
                current_version = json.loads(current_raw).get("routingPolicyVersion", "unknown")
            except Exception:
                pass
            # Version goes in the filename so `ls` alone answers "what was
            # live before this publish"; sanitized so a hostile version
            # string can't smuggle path separators into the archive name.
            safe_version = re.sub(r"[^A-Za-z0-9._-]", "_", str(current_version))[:40]
            archive_path = os.path.join(POLICY_VERSIONS_DIR, f"{ts}-{safe_version}.json")
            with open(archive_path, "w") as f:
                f.write(current_raw)
        except OSError:
            # An unreadable current file must not block publishing a good one.
            pass

    with open(POLICY_PATH, "w") as f:
        json.dump(new_policy, f, indent=2)
        f.write("\n")
    _cache = None

    log_path = os.path.join(POLICY_VERSIONS_DIR, "publish_log.jsonl")
    with open(log_path, "a") as f:
        f.write(json.dumps({
            "ts": datetime.now(timezone.utc).isoformat(),
            "actor": actor or "unknown",
            "routingPolicyVersion": new_policy.get("routingPolicyVersion"),
        }) + "\n")

    return load_policy()


def list_policy_versions() -> list:
    """Archived policy snapshots, newest first."""
    if not os.path.isdir(POLICY_VERSIONS_DIR):
        return []
    out = []
    for name in os.listdir(POLICY_VERSIONS_DIR):
        if not name.endswith(".json"):
            continue
        path = os.path.join(POLICY_VERSIONS_DIR, name)
        try:
            st = os.stat(path)
        except OSError:
            continue
        # Filename is <UTCts>-<routingPolicyVersion>.json (see publish_policy).
        stem = name[:-len(".json")]
        ts_part, _, version_part = stem.partition("-")
        out.append({
            "archive": name,
            "archived_ts": ts_part,
            "routingPolicyVersion": version_part or "unknown",
            "size_bytes": st.st_size,
            "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        })
    out.sort(key=lambda e: e["archive"], reverse=True)
    return out


def rollback_policy(archive_name: str, actor: str) -> dict:
    """Re-publish an archived policy. Goes through publish_policy() so the
    rollback itself archives the (bad) current file and lands in the publish
    log — a rollback is never an invisible state change."""
    if not archive_name or os.path.basename(archive_name) != archive_name:
        raise ValueError("invalid archive name")
    # realpath+commonpath jail, same convention as routing_context.safe_repo_path:
    # the name must resolve inside the archive dir, symlinks included.
    versions_root = os.path.realpath(POLICY_VERSIONS_DIR)
    candidate = os.path.realpath(os.path.join(versions_root, archive_name))
    if os.path.commonpath([versions_root, candidate]) != versions_root:
        raise ValueError("invalid archive name")
    if not os.path.isfile(candidate):
        raise FileNotFoundError(f"no archived policy named {archive_name!r}")
    with open(candidate) as f:
        archived = json.load(f)
    return publish_policy(archived, actor)
