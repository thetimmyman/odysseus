"""src/pi_config.py — Pi Coding runtime configuration for the Odysseus control plane.

Architectural boundary this module exists to serve:

    Odysseus = control plane  (routing, policy, budgets, lifecycle, audit)
    Pi       = execution plane (inner coding loop, tools, context)

So this module owns ONLY the runtime-side knobs Odysseus hands to Pi: which Pi
binary to launch, which execution model/provider routing policy requested, where
Pi persists its sessions, and the constrained environment Pi runs under. It
deliberately contains no routing, budget or policy logic — those stay in
``src/routing_*`` and ``config/routing_policy.json``.

Local-first: the default execution target is the same local Qwen runtime
Odysseus already talks to (Ollama's OpenAI-compatible API), configured for Pi
through its ``models.json``. Premium provider credentials are never placed in
Pi's environment — see :func:`pi_environment`.
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Dict, Optional, Tuple

from src.constants import DATA_DIR

#: Runtime identifiers. ``native`` is Odysseus's own agent loop (unchanged, the
#: default compatibility path); ``pi`` delegates the inner loop to Pi.
RUNTIME_NATIVE = "native"
RUNTIME_PI = "pi"

#: Environment variable that selects the execution runtime for delegated work.
RUNTIME_ENV = "ODYSSEUS_EXECUTION_RUNTIME"

#: Default local execution target. Provider ``local-qwen`` is the models.json
#: provider this module manages; the model id is Ollama's tag for the local
#: Qwen runtime already in use by Odysseus.
DEFAULT_PI_PROVIDER = "local-qwen"
DEFAULT_PI_MODEL_ID = "qwen3.8:27b"
DEFAULT_PI_BASE_URL = "http://localhost:11434/v1"

#: Logical aliases policy may use when requesting a model, mapped to
#: (provider, model id). Keeps Pi's model selection externally controlled: a
#: request for ``local-qwen3.8-27b`` never hardcodes a provider into the adapter.
_MODEL_ALIASES: Dict[str, Tuple[str, str]] = {
    "local-qwen3.8-27b": (DEFAULT_PI_PROVIDER, DEFAULT_PI_MODEL_ID),
    "local-qwen": (DEFAULT_PI_PROVIDER, DEFAULT_PI_MODEL_ID),
}

#: Environment keys a Pi execution is allowed to inherit. Everything else —
#: provider API keys, ODYSSEUS_* secrets, cloud credentials — is dropped so a
#: Pi run cannot reach premium providers or Odysseus internals on its own.
_ENV_ALLOWLIST = (
    "PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "TZ", "USER",
)


def data_root() -> str:
    """Pi's data root under the Odysseus data dir.

    Reads ``ODYSSEUS_DATA_DIR`` at CALL time (same convention as
    ``routing_workdir.data_root``) so a host CLI or a test can redirect it
    without re-importing the module.
    """
    override = os.environ.get("ODYSSEUS_DATA_DIR")
    base = os.path.realpath(override) if override else DATA_DIR
    return os.path.join(base, "pi")


def pi_home() -> str:
    """Pi's agent directory (where models.json / sessions / auth.json live)."""
    return os.environ.get("ODYSSEUS_PI_HOME") or os.path.join(os.path.expanduser("~"), ".pi", "agent")


def pi_bin() -> str:
    """The Pi executable to launch (override for tests / alternate installs)."""
    return os.environ.get("ODYSSEUS_PI_BIN") or "pi"


def pi_launch_dir() -> Optional[str]:
    """Directory of the resolved ``pi`` executable.

    Pi is a Node CLI (``#!/usr/bin/env node``), so its runtime is whichever
    ``node`` is first on PATH. The sibling ``node`` in this directory is the one
    Pi will actually use, so prepending it to a Pi execution's PATH makes the
    Node version deterministic instead of inherited from the caller (which on
    this host can be an incompatible mise-managed Node 20).
    """
    binary = pi_bin()
    resolved = binary if os.path.isabs(binary) else shutil.which(binary)
    if not resolved:
        return None
    # Do NOT realpath the binary: it is usually a symlink into the package's
    # dist bundle, whose directory holds neither ``pi`` nor ``node``.
    return os.path.dirname(os.path.abspath(resolved))


def session_dir() -> str:
    """Directory Pi persists session JSONL files into.

    Odysseus-side by default so execution identity files and Pi session files
    travel together; overridable for a shared Pi install.
    """
    return os.environ.get("ODYSSEUS_PI_SESSION_DIR") or os.path.join(data_root(), "sessions")


def models_config_path() -> str:
    """Path of the Pi models.json this module manages."""
    override = os.environ.get("ODYSSEUS_PI_MODELS_JSON")
    if override:
        return override
    return os.path.join(agent_dir() or pi_home(), "models.json")


def agent_dir() -> Optional[str]:
    """Odysseus-managed Pi agent directory, when configured.

    Pi resolves its config directory from ``PI_CODING_AGENT_DIR`` (default
    ``~/.pi/agent``). Pointing a delegated execution at a dedicated directory
    means Pi sees only the local execution provider's ``models.json`` and none
    of the operator's premium credentials in ``~/.pi/agent/auth.json``. Unset
    (the default) keeps the existing behaviour of using the user's own Pi
    install.
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
    """Resolve a requested model spec to ``(provider, model_id)`` for Pi.

    Accepts a logical alias (``local-qwen3.8-27b``), a Pi-style ``provider/id``
    spec, or a bare model id (default provider assumed). Routing policy decides
    *what* to request; this only translates it into Pi's addressing scheme.
    """
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
    """Pi CLI ``--model`` argument for a resolved provider/model pair."""
    return f"{provider}/{model_id}"


def execution_runtime() -> str:
    """Selected execution runtime: ``pi`` when routing policy delegated to Pi,
    otherwise ``native`` (Odysseus's own agent loop)."""
    value = (os.environ.get(RUNTIME_ENV) or "").strip().lower()
    return RUNTIME_PI if value == RUNTIME_PI else RUNTIME_NATIVE


def pi_environment() -> Dict[str, str]:
    """Minimal, least-privilege environment for a Pi execution.

    Only inert process keys are inherited. No provider API keys and no
    ``ODYSSEUS_*`` variables reach the child, so Pi cannot independently select
    premium models, read Odysseus secrets, or change budgets/policy.

    The one exception is a custom Pi home: HOME is pinned so Pi resolves its
    agent dir (models.json / auth.json / sessions) where Odysseus configured it.
    """
    env = {k: os.environ[k] for k in _ENV_ALLOWLIST if k in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    # Deterministic runtime: launch Pi with its own Node first on PATH so a
    # different Node earlier in the inherited PATH cannot shadow it.
    launch_dir = pi_launch_dir()
    if launch_dir:
        # Put it FIRST, whether or not it was already present later in PATH.
        parts = [p for p in env["PATH"].split(os.pathsep) if p and p != launch_dir]
        env["PATH"] = os.pathsep.join([launch_dir, *parts])
    agent = agent_dir()
    if agent:
        # Pi must resolve its config dir where Odysseus manages it (local
        # provider only, no premium credentials from the user's ~/.pi/agent).
        os.makedirs(agent, exist_ok=True)
        env["PI_CODING_AGENT_DIR"] = agent
    home = os.environ.get("ODYSSEUS_PI_HOME")
    if home:
        # ODYSSEUS_PI_HOME is <home>/.pi/agent; hand Pi the <home> prefix.
        env["HOME"] = os.path.dirname(os.path.dirname(home)) or env.get("HOME", "")
    return env


def build_model_entry(provider: str, model_id: str, base_url: str | None = None) -> dict:
    """The models.json provider entry for one local model.

    Local OpenAI-compatible servers (Ollama / vLLM / LM Studio / llama.cpp) take
    a system message rather than the OpenAI developer role and do not accept
    ``reasoning_effort``, hence the compat flags.
    """
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
    """Idempotently ensure Pi's models.json lists the local execution model.

    Merges into any existing file without disturbing unrelated providers, but
    never adds premium providers. Returns the path written.
    """
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
        # Preserve an existing provider entry. The endpoint may be managed
        # outside Odysseus (a deployment/operator choice); rewriting its
        # baseUrl would silently repoint a working local runtime at the wrong
        # host. Only fill in keys that are missing.
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
