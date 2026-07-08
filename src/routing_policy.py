"""src/routing_policy.py — versioned routing-policy config (spec Section 19).

The live policy is seeded from the baked config/routing_policy.json on first
boot but LIVES on the data/ volume (data_root()/routing/routing_policy.json),
so an in-app Policy edit survives a redeploy instead of being silently reset
to the baked default. Every publish archives the outgoing file to
data_root()/routing/policy_versions/ and appends to a publish log, so a policy
change is never silent and rollback is itself a logged publish — the archive
dir is append-only in normal operation.
"""
import copy
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Optional

from src.routing_budget import load_budget_config

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_root() -> str:
    """The harness data directory: ODYSSEUS_DATA_DIR when set (resolved per
    call so an exported/monkeypatched env is honored without re-import), else
    <repo-root>/data. Duplicated verbatim from routing_workdir.data_root() on
    purpose: this pure-config module must not grow an import edge to the
    worktree/executor stack just to resolve a directory (the same reasoning
    that keeps POLICY_VERSIONS_DIR expressed here rather than imported)."""
    override = os.environ.get("ODYSSEUS_DATA_DIR")
    if override:
        return os.path.realpath(override)
    return os.path.join(_ROOT, "data")


# The shipped default, read-only at runtime: it is the SEED for the live file
# on a fresh deploy, never the live file itself (writing it would not survive a
# redeploy — that was the bug).
BAKED_POLICY_PATH = os.path.join(_ROOT, "config", "routing_policy.json")
# Live policy + its version archive both live under the data/ volume so
# in-app edits persist across redeploys. Computed at import from data_root();
# tests monkeypatch these module globals directly.
POLICY_PATH = os.path.join(data_root(), "routing", "routing_policy.json")
POLICY_VERSIONS_DIR = os.path.join(data_root(), "routing", "policy_versions")

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
        # Phase 8 benchmark knobs (spec Section 20). defaultReplays bounds the
        # per-fixture replay count; thresholds override src.routing_benchmark's
        # HARD_GATE_THRESHOLDS. The benchmark REPORTS whether a candidate model
        # clears these gates; it never flips coordinator.provider to "endpoint"
        # (register the ModelEndpoint, set provider=endpoint + endpointName +
        # model, then publish the policy — a deliberate human decision).
        "benchmark": {
            "defaultReplays": 5,
            "thresholds": {
                "schema_validity": 0.98,
                "policy_gate_compliance": 0.99,
                "domain_classification": 0.95,
                "approval_gate": 0.95,
                "arbitration": 0.85,
                "uncertainty_handling": 0.85,
                "failure_retry": 0.85,
                "consistency": 0.9,
            },
        },
    },
    "maxUntrustedTokens": 256,
    "rawOutputMaxBytes": 262144,
    "remoteSensitivityCeiling": "confidential",
    # Phase 4 mode-aware verification (spec Section 16, routing_verification):
    # defaultMode is the terminal fallback when a task carries no
    # verification_mode and its task_type maps to nothing;
    # equivalenceStdoutComparison documents/enables the v1 byte-wise stdout
    # equivalence check (refactor_equivalence only); overconfidenceThreshold
    # is the metadata-only calibration flag cutoff (an overconfident FAILURE
    # is flagged for WP6's stats — confidence never gates pass/fail).
    "verification": {
        "defaultMode": "regression_guard",
        "equivalenceStdoutComparison": True,
        "overconfidenceThreshold": 0.8,
    },
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
        # Code-only sandbox defaults that MUST survive a partial publish (spec
        # PR-A): mountLabel is the SELinux relabel flag for the worktree bind
        # mount, runAsHostUser drops the container off root when --cap-drop ALL
        # is in effect. These mirror routing_sandbox.DEFAULT_SANDBOX (kept in
        # sync by value, not imported — routing_sandbox imports THIS module, so
        # importing back would be circular). The publish merge over
        # DEFAULT_POLICY guarantees an editor that only sends sandbox.image can
        # never blank runAsHostUser back to container-root.
        "mountLabel": "z",
        "runAsHostUser": True,
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
    # Phase 7 ABSIS integration (spec Section 7): transport config for the
    # host-side absis_tacticus_job_queue dispatcher (src/routing_absis.py +
    # scripts/odysseus-absis). Disabled until llm_inference/oracle_runner
    # workers actually exist — the orchestrator fails unclaimable jobs within
    # seconds, so enabling early only pollutes the queue.
    "absis": {
        "enabled": False,
        "sshTarget": "minipc",
        "kubectlExecPrefix": "sudo kubectl exec -n tacticus deploy/absis-orchestrator --",
        "transportTimeoutSeconds": 30,
        "note": ("no llm_inference/oracle_runner workers deployed as of 2026-07-08; "
                 "enable after workers exist in tacticus-analytics"),
    },
}

_REQUIRED_VERSION_KEYS = ("routingPolicyVersion", "verificationPolicyVersion", "uiConfigVersion")

# --- validation value sets (spec PR-A hardening) -----------------------------
# Hardcoded rather than imported from the enums that own them (VerificationMode
# in routing_coordinator, the sensitivity ladder) to keep this pure-config
# module free of the llm_core/DB import chain those pull in — the same
# dependency-light stance the module docstring already takes. If either enum
# grows a member, this set is the one place to mirror it.
_VERIFICATION_MODES = frozenset({
    "regression_guard", "bug_fix", "feature_addition",
    "refactor_equivalence", "security_fix", "analysis_only",
})
_SENSITIVITY_LEVELS = frozenset({"public", "internal", "confidential", "restricted", "secret"})
_COORDINATOR_PROVIDERS = frozenset({"external", "endpoint"})
# SELinux relabel flag: "z" (shared) / "Z" (private) / "" (disabled). Anything
# else is passed verbatim to `docker run -v ...:LABEL` and is a config error.
_MOUNT_LABELS = frozenset({"z", "Z", ""})

# Sane upper clamps for the sandbox resource knobs. A publish that sets these
# beyond the clamp is rejected (a fat-fingered memoryGb=40000 would let a
# verification container exhaust the host).
_SANDBOX_CLAMPS = {
    "cpus": 32,
    "memoryGb": 128,
    "pidsLimit": 65536,
    "wallClockSeconds": 3600,
    "maxOutputBytes": 104857600,  # 100 MiB
}

# allowedCommands is the sandbox RCE surface. Reject shell metacharacters
# (mirrors routing_sandbox._SHELL_METACHARACTERS) and interpreter prefixes that
# turn "run this exact tool" into "run arbitrary code": a bare interpreter, or
# any entry carrying an inline-code / eval / exec flag.
_ALLOWLIST_METACHARACTERS = (";", "|", "&", "`", "$(", ">", "<", "\n", "\r")
_BARE_INTERPRETERS = frozenset({"python", "python3", "bash", "sh", "zsh", "node"})
_RCE_SUBSTRINGS = (" -c", "-c ", " -e", "-e ", "eval", "exec")

# Danger-zone policy keys (dotted paths). A publish that CHANGES any of these
# vs the current live policy is break-glass-class and gated on security_admin
# in the publish route (routing_harness_routes.policy_publish).
DANGER_ZONE_KEYS = (
    "sandbox.image",
    "sandbox.allowedCommands",
    "remoteSensitivityCeiling",
    "coordinator.provider",
    "coordinator.endpointName",
    "absis.sshTarget",
    "absis.kubectlExecPrefix",
    "absis.enabled",
)

# (path, mtime, parsed) — invalidated whenever the file's mtime moves, so a
# publish (or a hand edit) is picked up without a process restart.
_cache: Optional[tuple] = None


def _deep_merge_defaults(defaults: dict, override: dict) -> dict:
    """Return a deep copy of `defaults` with `override` layered on top: nested
    dicts recurse (so a partial coordinator/sandbox/absis block fills its
    missing keys from the default rather than dropping them), scalars and lists
    are replaced wholesale by the override, and override-only keys are kept.
    This is what preserves the code-only sandbox defaults (runAsHostUser,
    mountLabel) through a partial publish."""
    if not isinstance(override, dict):
        return copy.deepcopy(defaults)
    out = copy.deepcopy(defaults)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge_defaults(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _dotted_get(d: dict, path: str):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def danger_zone_changes(new_policy: dict, current_policy: dict) -> list:
    """The DANGER_ZONE_KEYS whose EFFECTIVE value differs between new and
    current. Each side is merged over DEFAULT_POLICY first so an omitted key
    compares as its default (a partial publish that simply doesn't restate
    sandbox.image is not treated as changing it)."""
    new_m = _deep_merge_defaults(DEFAULT_POLICY, new_policy if isinstance(new_policy, dict) else {})
    cur_m = _deep_merge_defaults(DEFAULT_POLICY, current_policy if isinstance(current_policy, dict) else {})
    return [k for k in DANGER_ZONE_KEYS if _dotted_get(new_m, k) != _dotted_get(cur_m, k)]


def _is_number(v) -> bool:
    # math.isfinite screens NaN/Infinity: NaN would defeat every range/clamp
    # check (NaN<=0, NaN>clamp both False), so a NaN sandbox knob would pass
    # validation and then crash int(NaN) on every sandbox run. JSON permits the
    # bare NaN/Infinity literals, so this is reachable over HTTP.
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _seed_policy_if_missing() -> None:
    """First-boot seed: copy the baked default onto the data-volume live path
    when the live file is absent, so a fresh deploy behaves identically to the
    shipped config and later in-app edits (which write the data-volume file)
    persist across redeploys. Never raises — a failed seed just means
    load_policy degrades to DEFAULT_POLICY, and a publish would create the
    file anyway."""
    try:
        if os.path.exists(POLICY_PATH):
            return
        if not os.path.isfile(BAKED_POLICY_PATH):
            return
        os.makedirs(os.path.dirname(POLICY_PATH), exist_ok=True)
        with open(BAKED_POLICY_PATH, encoding="utf-8") as src_f:
            data = src_f.read()
        with open(POLICY_PATH, "w", encoding="utf-8") as dst_f:
            dst_f.write(data)
    except OSError:
        pass


def load_policy() -> dict:
    """Current policy dict; DEFAULT_POLICY copy when no file exists (or it's
    unreadable — a corrupt policy file must degrade to defaults, not 500
    every wrap call)."""
    global _cache
    _seed_policy_if_missing()
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


def _validate_allowed_commands(entries, reasons: list) -> None:
    if not isinstance(entries, list):
        reasons.append("sandbox.allowedCommands must be a list of command-prefix strings")
        return
    for i, entry in enumerate(entries):
        if not isinstance(entry, str) or not entry.strip():
            reasons.append(f"sandbox.allowedCommands[{i}] must be a non-empty string")
            continue
        if any(meta in entry for meta in _ALLOWLIST_METACHARACTERS):
            reasons.append(
                f"sandbox.allowedCommands[{i}] {entry!r} contains a shell metacharacter "
                "(one of ; | & ` $( > < newline) — the sandbox runs argv without a shell, "
                "so such an entry is either a mistake or an injection attempt")
            continue
        normalized = " ".join(entry.split())
        if normalized in _BARE_INTERPRETERS:
            reasons.append(
                f"sandbox.allowedCommands[{i}] {entry!r} is a bare interpreter — it would "
                "allow running ARBITRARY code; allowlist the specific subcommand instead "
                "(e.g. 'python -m pytest')")
            continue
        if any(sub in normalized for sub in _RCE_SUBSTRINGS):
            reasons.append(
                f"sandbox.allowedCommands[{i}] {entry!r} carries an inline-code / eval / exec "
                "flag (-c/-e/eval/exec) — that turns the allowlist into remote code execution")


def _validate_policy(new_policy: dict) -> dict:
    """Validate and NORMALIZE a candidate policy. The required version keys
    must be present in the RAW input (a partial publish must not inherit them
    from defaults); everything else is merged over DEFAULT_POLICY so code-only
    defaults survive, then the merged result is range/enum/allowlist checked.
    Returns the merged, validated dict that publish_policy will write. Raises
    ValueError (message enumerating every violation) on any failure — a
    rejected publish therefore never writes."""
    if not isinstance(new_policy, dict):
        raise ValueError("policy must be a JSON object")

    reasons = []
    for key in _REQUIRED_VERSION_KEYS:
        if not isinstance(new_policy.get(key), str) or not new_policy.get(key):
            reasons.append(f"policy.{key} is required and must be a non-empty string")
    if reasons:
        # Fail fast on the structural check before merging (merging would fill
        # the missing version keys from defaults and mask the error).
        raise ValueError("; ".join(reasons))

    merged = _deep_merge_defaults(DEFAULT_POLICY, new_policy)

    # --- top-level scalars ---
    ceiling = merged.get("remoteSensitivityCeiling")
    if ceiling not in _SENSITIVITY_LEVELS:
        reasons.append(
            f"remoteSensitivityCeiling {ceiling!r} not in {sorted(_SENSITIVITY_LEVELS)}")
    mut = merged.get("maxUntrustedTokens")
    if not _is_int(mut) or not (0 <= mut <= 8192):
        reasons.append("maxUntrustedTokens must be an integer in [0, 8192]")
    rob = merged.get("rawOutputMaxBytes")
    if not _is_int(rob) or rob <= 0:
        reasons.append("rawOutputMaxBytes must be a positive integer")

    # --- verification ---
    verification = merged.get("verification") or {}
    if isinstance(verification, dict):
        mode = verification.get("defaultMode")
        if mode not in _VERIFICATION_MODES:
            reasons.append(
                f"verification.defaultMode {mode!r} not in {sorted(_VERIFICATION_MODES)}")
    else:
        reasons.append("verification must be a JSON object")

    # --- coordinator ---
    coordinator = merged.get("coordinator") or {}
    if isinstance(coordinator, dict):
        provider = coordinator.get("provider")
        if provider not in _COORDINATOR_PROVIDERS:
            reasons.append(
                f"coordinator.provider {provider!r} not in {sorted(_COORDINATOR_PROVIDERS)}")
        temp = coordinator.get("temperature")
        if not _is_number(temp) or not (0 <= temp <= 2):
            reasons.append("coordinator.temperature must be a number in [0, 2]")
        # benchmark lives under coordinator (DEFAULT_POLICY.coordinator.benchmark)
        benchmark = coordinator.get("benchmark") or {}
        if isinstance(benchmark, dict):
            replays = benchmark.get("defaultReplays")
            if not _is_int(replays) or not (1 <= replays <= 100):
                reasons.append("coordinator.benchmark.defaultReplays must be an integer in [1, 100]")
            thresholds = benchmark.get("thresholds") or {}
            if isinstance(thresholds, dict):
                for tname, tval in thresholds.items():
                    if not _is_number(tval) or not (0 <= tval <= 1):
                        reasons.append(
                            f"coordinator.benchmark.thresholds.{tname} must be a number in [0, 1]")
            else:
                reasons.append("coordinator.benchmark.thresholds must be a JSON object")
        else:
            reasons.append("coordinator.benchmark must be a JSON object")
    else:
        reasons.append("coordinator must be a JSON object")

    # --- sandbox (the RCE / resource-exhaustion surface) ---
    sandbox = merged.get("sandbox") or {}
    if isinstance(sandbox, dict):
        if not isinstance(sandbox.get("image"), str) or not sandbox.get("image"):
            reasons.append("sandbox.image must be a non-empty string")
        for knob, clamp in _SANDBOX_CLAMPS.items():
            val = sandbox.get(knob)
            if not _is_number(val) or val <= 0:
                reasons.append(f"sandbox.{knob} must be a positive number")
            elif val > clamp:
                reasons.append(f"sandbox.{knob} must be <= {clamp}")
        label = sandbox.get("mountLabel")
        if label not in _MOUNT_LABELS:
            reasons.append(
                f"sandbox.mountLabel {label!r} not in {sorted(_MOUNT_LABELS)} "
                '("z" | "Z" | "")')
        _validate_allowed_commands(sandbox.get("allowedCommands"), reasons)
    else:
        reasons.append("sandbox must be a JSON object")

    if reasons:
        raise ValueError("; ".join(reasons))
    return merged


def publish_policy(new_policy: dict, actor: str) -> dict:
    """Validate, archive the outgoing policy, write the new one, log the
    publish. Archive-before-overwrite means a bad publish is always
    recoverable via rollback_policy(). The MERGED, validated policy is what is
    written — a partial publish is filled from DEFAULT_POLICY so code-only
    defaults (runAsHostUser, mountLabel, the full sandbox/coordinator shape)
    are never dropped on disk."""
    global _cache
    new_policy = _validate_policy(new_policy)

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

    # Atomic write (temp + fsync + os.replace): a concurrent load_policy reader
    # (or a crash mid-write during the very redeploy this file persists across)
    # must never see a truncated policy — a torn policy degrades load_policy to
    # DEFAULT_POLICY, which loosens remoteSensitivityCeiling. allow_nan=False so
    # a non-finite value fails here instead of persisting a bare NaN token.
    _tmp_fd, _tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(POLICY_PATH), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(_tmp_fd, "w") as f:
            json.dump(new_policy, f, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(_tmp_path, POLICY_PATH)
    except BaseException:
        try:
            os.unlink(_tmp_path)
        except OSError:
            pass
        raise
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


def read_policy_version(archive_name: str) -> dict:
    """Load an archived policy snapshot as a dict. The archive name is
    realpath+commonpath jailed INSIDE POLICY_VERSIONS_DIR (symlinks included),
    same convention as routing_context.safe_repo_path, so a traversal name can
    never read outside the archive dir. Raises ValueError (bad name) or
    FileNotFoundError (no such archive). Exposed so a caller can AUTHORIZE a
    rollback against the archived content (e.g. re-run danger-zone gating)
    before it is made live — the route must not trust a rollback to be a safe
    change just because the target is an archive."""
    if not archive_name or os.path.basename(archive_name) != archive_name:
        raise ValueError("invalid archive name")
    versions_root = os.path.realpath(POLICY_VERSIONS_DIR)
    candidate = os.path.realpath(os.path.join(versions_root, archive_name))
    if os.path.commonpath([versions_root, candidate]) != versions_root:
        raise ValueError("invalid archive name")
    if not os.path.isfile(candidate):
        raise FileNotFoundError(f"no archived policy named {archive_name!r}")
    with open(candidate) as f:
        return json.load(f)


def rollback_policy(archive_name: str, actor: str) -> dict:
    """Re-publish an archived policy. Goes through publish_policy() so the
    rollback itself archives the (bad) current file and lands in the publish
    log — a rollback is never an invisible state change.

    NOTE: this applies the archived policy with the SAME validation as a normal
    publish, but it does NOT itself re-check danger-zone authorization — that is
    the ROUTE's responsibility (a rollback can re-instate a danger-zone value
    and must be gated exactly like publishing that value directly). Callers that
    are not already authorization-gated must read_policy_version() +
    danger_zone_changes() first."""
    archived = read_policy_version(archive_name)
    return publish_policy(archived, actor)
