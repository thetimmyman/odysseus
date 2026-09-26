"""Pi runtime configuration: binary, model addressing, session dir and environment.

Odysseus is the control plane and Pi the execution plane, so routing, budget and
policy logic stay in ``src/routing_*``. Premium provider credentials are never
placed in Pi's environment (see :func:`pi_environment`).
"""
from __future__ import annotations

import json
import os
import stat
import shutil
import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple

from src.constants import DATA_DIR

#: ``native`` is Odysseus's own agent loop (default); ``pi`` delegates to Pi.
RUNTIME_NATIVE = "native"
RUNTIME_PI = "pi"

RUNTIME_ENV = "ODYSSEUS_EXECUTION_RUNTIME"

#: Default local target: the models.json provider this module manages.
DEFAULT_PI_PROVIDER = "local-qwen"
DEFAULT_PI_MODEL_ID = "qwen3.8:27b"
DEFAULT_PI_BASE_URL = "http://localhost:11434/v1"

#: Logical aliases -> (provider, model id), so the adapter never hardcodes a provider.
_MODEL_ALIASES: Dict[str, Tuple[str, str]] = {
    "local-qwen3.8-27b": (DEFAULT_PI_PROVIDER, DEFAULT_PI_MODEL_ID),
    "local-qwen": (DEFAULT_PI_PROVIDER, DEFAULT_PI_MODEL_ID),
}

#: The only env keys a Pi run inherits; provider keys and ODYSSEUS_* secrets are
#: dropped so Pi cannot reach premium providers or Odysseus internals.
_ENV_ALLOWLIST = (
    "PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "TZ", "USER",
)


def data_root() -> str:
    """Pi's data root; reads ``ODYSSEUS_DATA_DIR`` at call time so tests can redirect it."""
    override = os.environ.get("ODYSSEUS_DATA_DIR")
    base = os.path.realpath(override) if override else DATA_DIR
    return os.path.join(base, "pi")


def pi_home() -> str:
    """Pi's agent directory (where models.json / sessions / auth.json live)."""
    return os.environ.get("ODYSSEUS_PI_HOME") or os.path.join(os.path.expanduser("~"), ".pi", "agent")


def pi_bin() -> str:
    return os.environ.get("ODYSSEUS_PI_BIN") or "pi"


def pi_launch_dir() -> Optional[str]:
    """Directory of the resolved ``pi`` executable.

    Pi runs on whichever ``node`` is first on PATH; prepending this directory
    pins its sibling ``node`` instead of an incompatible inherited one.
    """
    binary = pi_bin()
    resolved = binary if os.path.isabs(binary) else shutil.which(binary)
    if not resolved:
        return None
    # Do NOT realpath the binary: it is usually a symlink into the package's
    # dist bundle, whose directory holds neither ``pi`` nor ``node``.
    return os.path.dirname(os.path.abspath(resolved))


def session_dir() -> str:
    """Pi session JSONL dir; Odysseus-side by default so it travels with execution records."""
    return os.environ.get("ODYSSEUS_PI_SESSION_DIR") or os.path.join(data_root(), "sessions")


def models_config_path() -> str:
    override = os.environ.get("ODYSSEUS_PI_MODELS_JSON")
    if override:
        return override
    return os.path.join(agent_dir() or pi_home(), "models.json")


def agent_dir() -> Optional[str]:
    """Odysseus-managed Pi agent directory, when configured.

    A dedicated dir hides the operator's premium credentials in
    ``~/.pi/agent/auth.json`` from Pi. Unset uses the user's own Pi install.
    """
    return os.environ.get("ODYSSEUS_PI_AGENT_DIR") or None


def local_base_url() -> str:
    """OpenAI-compatible base URL of the local Qwen runtime Pi should use."""
    return (
        os.environ.get("ODYSSEUS_PI_LOCAL_BASE_URL")
        or os.environ.get("OLLAMA_BASE_URL")
        or DEFAULT_PI_BASE_URL
    )


def resolve_model(requested: str | None) -> Tuple[str, str]:
    """Resolve an alias, ``provider/id`` spec or bare model id to ``(provider, model_id)``."""
    spec = (requested or "").strip()
    if not spec:
        return DEFAULT_PI_PROVIDER, DEFAULT_PI_MODEL_ID
    if spec in _MODEL_ALIASES:
        return _MODEL_ALIASES[spec]
    if "/" in spec:
        provider, model_id = spec.split("/", 1)
        return (provider.strip() or DEFAULT_PI_PROVIDER), (model_id.strip() or DEFAULT_PI_MODEL_ID)
    return DEFAULT_PI_PROVIDER, spec


def model_spec(provider: str, model_id: str) -> str:
    return f"{provider}/{model_id}"


def execution_runtime() -> str:
    """Selected execution runtime: ``pi`` when routing policy delegated to Pi,
    otherwise ``native`` (Odysseus's own agent loop)."""
    value = (os.environ.get(RUNTIME_ENV) or "").strip().lower()
    return RUNTIME_PI if value == RUNTIME_PI else RUNTIME_NATIVE


def pi_environment() -> Dict[str, str]:
    """Least-privilege environment for a Pi execution.

    No provider API keys or ``ODYSSEUS_*`` variables reach the child. With a
    custom Pi home, HOME is pinned so Pi resolves its agent dir there.
    """
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    # Pi's own Node goes first on PATH so an inherited Node cannot shadow it.
    launch_dir = pi_launch_dir()
    if launch_dir:
        parts = [p for p in env["PATH"].split(os.pathsep) if p and p != launch_dir]
        env["PATH"] = os.pathsep.join([launch_dir, *parts])
    agent = agent_dir()
    if agent:
        os.makedirs(agent, exist_ok=True)
        env["PI_CODING_AGENT_DIR"] = agent
    home = os.environ.get("ODYSSEUS_PI_HOME")
    if home:
        # ODYSSEUS_PI_HOME is <home>/.pi/agent; hand Pi the <home> prefix.
        env["HOME"] = os.path.dirname(os.path.dirname(home)) or env.get("HOME", "")
    return env


def build_model_entry(provider: str, model_id: str, base_url: str | None = None) -> dict:
    """models.json provider entry; compat flags because local servers reject the
    developer role and ``reasoning_effort``."""
    return {
        "baseUrl": (base_url or local_base_url()).rstrip("/"),
        "api": "openai-completions",
        "apiKey": "local",  # placeholder: keyless local servers still need a value
        "compat": {
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": False,
        },
        "models": [{"id": model_id}],
    }


def ensure_pi_model_config(provider: str | None = None, model_id: str | None = None) -> str:
    """Idempotently merge the local model into Pi's models.json; never adds premium providers."""
    provider = provider or DEFAULT_PI_PROVIDER
    model_id = model_id or DEFAULT_PI_MODEL_ID
    path = models_config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}

    providers = data.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        data["providers"] = providers

    entry = providers.get(provider)
    entry = entry if isinstance(entry, dict) else {}
    existing_models = entry.get("models") if isinstance(entry.get("models"), list) else []
    if not any(isinstance(m, dict) and m.get("id") == model_id for m in existing_models):
        existing_models.append({"id": model_id})

    if entry:
        # Only fill missing keys: the endpoint may be operator-managed, and
        # rewriting baseUrl would silently repoint a working runtime.
        merged = dict(entry)
        merged.setdefault("baseUrl", local_base_url().rstrip("/"))
        merged.setdefault("api", "openai-completions")
        merged.setdefault("apiKey", "local")
        compat = merged.get("compat")
        compat = dict(compat) if isinstance(compat, dict) else {}
        compat.setdefault("supportsDeveloperRole", False)
        compat.setdefault("supportsReasoningEffort", False)
        merged["compat"] = compat
    else:
        merged = build_model_entry(provider, model_id)
    merged["models"] = existing_models
    providers[provider] = merged

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path


def ensure_pi_provider_auth(provider: str, auth_entry: object, *, directory: str | None = None) -> str:
    """Provision one provider credential into an explicitly isolated Pi dir."""
    target = directory or agent_dir()
    if not target:
        raise ValueError(
            "subscription credential provisioning requires an isolated "
            "ODYSSEUS_PI_AGENT_DIR; refusing to write the operator ~/.pi/agent/auth.json"
        )
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider name must be non-empty")

    # Explicit paths are supported for isolated runtimes and tests, but they
    # must not alias Pi's operator credential directory (including via symlink).
    target_path = os.path.abspath(os.path.expanduser(target))
    user_home = os.path.realpath(os.path.expanduser("~"))
    operator_dir = os.path.realpath(os.path.join(user_home, ".pi", "agent"))
    resolved_target = os.path.realpath(target_path)
    protected = (user_home, os.path.realpath(os.path.join(user_home, ".pi")),
                 operator_dir, os.path.realpath(tempfile.gettempdir()),
                 os.path.realpath("/var/tmp"))
    if any(resolved_target == item or item.startswith(resolved_target.rstrip(os.sep) + os.sep)
           for item in protected):
        raise ValueError("refusing to provision credentials into an operator or shared directory")

    # Do not let any existing symlink in the selected path redirect writes.
    cursor = os.path.sep
    for component in Path(target_path).parts[1:]:
        cursor = os.path.join(cursor, component)
        try:
            info = os.lstat(cursor)
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("isolated Pi directory must not contain symlink components")

    os.makedirs(target_path, mode=0o700, exist_ok=True)
    dir_info = os.stat(target_path, follow_symlinks=False)
    if (not stat.S_ISDIR(dir_info.st_mode) or dir_info.st_uid != os.geteuid()
            or os.path.realpath(target_path) != target_path):
        raise ValueError("isolated Pi directory must be an owned directory")
    os.chmod(target_path, 0o700)
    path = os.path.join(target_path, "auth.json")
    auth = {}
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        fd = None
    if fd is not None:
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or info.st_size > 1024 * 1024):
                raise ValueError("existing Pi auth file must be an owned regular file under 1 MiB")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as fh:
                current = json.load(fh)
            if not isinstance(current, dict):
                raise ValueError("existing Pi auth file must contain an object")
            auth = current
        finally:
            os.close(fd)

    auth[provider.strip()] = auth_entry
    raw = (json.dumps(auth, indent=2, allow_nan=False) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".auth-", suffix=".tmp", dir=target_path)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Refuse a symlinked destination even though replace itself would
        # replace the link rather than follow it; it signals unsafe state.
        try:
            if stat.S_ISLNK(os.lstat(path).st_mode):
                raise ValueError("Pi auth destination must not be a symlink")
        except FileNotFoundError:
            pass
        os.replace(temporary, path)
        dfd = os.open(target_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return path
