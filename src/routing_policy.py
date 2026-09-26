"""Versioned routing-policy config.

The live policy is seeded from config/routing_policy.json but lives on the
data/ volume so edits survive redeploys. Every publish archives the outgoing
file and is logged; rollback is itself a logged publish.
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
    """ODYSSEUS_DATA_DIR (read per call) or <repo-root>/data.

    Duplicated from routing_workdir.data_root() to avoid importing the executor stack."""
    override = os.environ.get("ODYSSEUS_DATA_DIR")
    if override:
        return os.path.realpath(override)
    return os.path.join(_ROOT, "data")


# Seed only, never written: writes here would not survive a redeploy.
BAKED_POLICY_PATH = os.path.join(_ROOT, "config", "routing_policy.json")
# Computed at import; tests monkeypatch these globals directly.
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
        # Benchmark knobs; thresholds override HARD_GATE_THRESHOLDS. Switching
        # provider to "endpoint" stays a deliberate human publish.
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
    # defaultMode applies when neither task nor task_type yields a mode;
    # overconfidenceThreshold only flags stats, never gates pass/fail.
    "verification": {
        "defaultMode": "regression_guard",
        "equivalenceStdoutComparison": True,
        "overconfidenceThreshold": 0.8,
    },
    # A command is allowed iff it equals an allowedCommands entry or starts with
    # entry + " ", and has no shell metacharacters.
    "sandbox": {
        "image": "python:3.12-slim",
        "cpus": 2,
        "memoryGb": 4,
        "pidsLimit": 256,
        "wallClockSeconds": 600,
        "maxOutputBytes": 1048576,
        # Must survive a partial publish so runAsHostUser can't revert to root.
        # Mirrors routing_sandbox.DEFAULT_SANDBOX by value (importing is circular).
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
    # ABSIS dispatcher transport; disabled by default because jobs with no
    # matching worker fail within seconds and pollute the queue.
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

# Hardcoded to avoid the llm_core/DB import chain; mirror any new enum member here.
_VERIFICATION_MODES = frozenset({
    "regression_guard", "bug_fix", "feature_addition",
    "refactor_equivalence", "security_fix", "analysis_only",
})
_SENSITIVITY_LEVELS = frozenset({"public", "internal", "confidential", "restricted", "secret"})
_COORDINATOR_PROVIDERS = frozenset({"external", "endpoint"})
# SELinux relabel flag: "z" (shared) / "Z" (private) / "" (disabled). Anything
# else is passed verbatim to `docker run -v ...:LABEL` and is a config error.
_MOUNT_LABELS = frozenset({"z", "Z", ""})

# Publishes above these clamps are rejected so a typo can't exhaust the host.
_SANDBOX_CLAMPS = {
    "cpus": 32,
    "memoryGb": 128,
    "pidsLimit": 65536,
    "wallClockSeconds": 3600,
    "maxOutputBytes": 104857600,  # 100 MiB
}

# allowedCommands is the sandbox RCE surface: reject shell metacharacters, bare
# interpreters and inline-code/eval flags.
_ALLOWLIST_METACHARACTERS = (";", "|", "&", "`", "$(", ">", "<", "\n", "\r")
_BARE_INTERPRETERS = frozenset({"python", "python3", "bash", "sh", "zsh", "node"})
_RCE_SUBSTRINGS = (" -c", "-c ", " -e", "-e ", "eval", "exec")

# Changing any of these requires security_admin at publish.
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

# (path, mtime, parsed); an mtime change picks up edits without a restart.
_cache: Optional[tuple] = None


def _deep_merge_defaults(defaults: dict, override: dict) -> dict:
    """Deep-merge `override` over `defaults`: dicts recurse, scalars and lists replace.

    This preserves code-only defaults through a partial publish."""
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
    """DANGER_ZONE_KEYS whose effective value changes; omitted keys compare as defaults."""
    new_m = _deep_merge_defaults(DEFAULT_POLICY, new_policy if isinstance(new_policy, dict) else {})
    cur_m = _deep_merge_defaults(DEFAULT_POLICY, current_policy if isinstance(current_policy, dict) else {})
    return [k for k in DANGER_ZONE_KEYS if _dotted_get(new_m, k) != _dotted_get(cur_m, k)]


def _is_number(v) -> bool:
    # NaN passes every range check, and JSON over HTTP can carry it.
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _seed_policy_if_missing() -> None:
    """Copy the baked default to the live path if absent; never raises."""
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
    """Current policy; DEFAULT_POLICY when missing or unreadable, never a 500."""
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
    """Version stamps recorded on every audit row and RunManifest."""
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
    """Validate and return the policy merged over DEFAULT_POLICY.

    Version keys must be in the raw input, not inherited. Raises ValueError
    listing every violation."""
    if not isinstance(new_policy, dict):
        raise ValueError("policy must be a JSON object")

    reasons = []
    for key in _REQUIRED_VERSION_KEYS:
        if not isinstance(new_policy.get(key), str) or not new_policy.get(key):
            reasons.append(f"policy.{key} is required and must be a non-empty string")
    if reasons:
        # Check before merging, which would fill missing version keys.
        raise ValueError("; ".join(reasons))

    merged = _deep_merge_defaults(DEFAULT_POLICY, new_policy)

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

    verification = merged.get("verification") or {}
    if isinstance(verification, dict):
        mode = verification.get("defaultMode")
        if mode not in _VERIFICATION_MODES:
            reasons.append(
                f"verification.defaultMode {mode!r} not in {sorted(_VERIFICATION_MODES)}")
    else:
        reasons.append("verification must be a JSON object")

    coordinator = merged.get("coordinator") or {}
    if isinstance(coordinator, dict):
        provider = coordinator.get("provider")
        if provider not in _COORDINATOR_PROVIDERS:
            reasons.append(
                f"coordinator.provider {provider!r} not in {sorted(_COORDINATOR_PROVIDERS)}")
        temp = coordinator.get("temperature")
        if not _is_number(temp) or not (0 <= temp <= 2):
            reasons.append("coordinator.temperature must be a number in [0, 2]")
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
    """Validate, archive the outgoing policy, write the merged one and log it.

    Archiving first keeps a bad publish recoverable via rollback_policy()."""
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
            # Sanitized so a hostile version can't inject path separators.
            safe_version = re.sub(r"[^A-Za-z0-9._-]", "_", str(current_version))[:40]
            archive_path = os.path.join(POLICY_VERSIONS_DIR, f"{ts}-{safe_version}.json")
            with open(archive_path, "w") as f:
                f.write(current_raw)
        except OSError:
            # An unreadable current file must not block publishing a good one.
            pass

    # Atomic write: a torn file would fall back to DEFAULT_POLICY and loosen
    # remoteSensitivityCeiling. allow_nan=False rejects non-finite values.
    _tmp_fd, _tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(POLICY_PATH), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(_tmp_fd, "w") as f:
            json.dump(new_policy, f, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        # 0644 so a root-written policy stays readable by the non-root app user.
        os.chmod(_tmp_path, 0o644)
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
    """Load an archived policy, jailed inside POLICY_VERSIONS_DIR.

    Lets a caller authorize a rollback against its content before it goes live."""
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
    """Re-publish an archived policy through publish_policy(), so it is logged.

    Danger-zone authorization is not checked here; callers must gate it first."""
    archived = read_policy_version(archive_name)
    return publish_policy(archived, actor)
