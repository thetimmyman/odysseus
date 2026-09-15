#!/usr/bin/env python3
"""Registry-driven runtime client factory.

One place decides which client a target id gets, and it decides from the REGISTRY
(``LocalTargetSpec.runtime_kind``), not from a guess about which server is up. That
keeps the runtime dimension in the same file as the identity it belongs to, and it
means a request for a llama-server profile can never be answered by the ollama
client (or the reverse) - the failure mode where a benchmark measures one runtime
and reports another.

An unknown target id stays a hard failure (``KeyError``), which is the fail-closed
behaviour the harness already relies on.
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = ["client_for_target", "resolved_spec"]


def resolved_spec(target_id: str) -> Optional[Any]:
    """The registry spec for a target, or ``None`` when it is not registered."""
    try:
        from src.local_targets import target_by_id
    except Exception:  # pragma: no cover - no registry importable in this context
        return None
    return target_by_id(target_id)


def client_for_target(target_id: str, *, spec: Optional[Any] = None):
    """Build the client the target's declared runtime kind requires."""
    import ollama_client

    spec = spec if spec is not None else resolved_spec(target_id)
    if spec is not None:
        # A client is an INVOCATION PATH. Building one for a host that the registry
        # deliberately left unqualified would create a second way to reach a target
        # routing can never legitimately select, so it is refused here as well as in
        # the selector. (Routing stays the authority; this is belt and braces.)
        roles = tuple(getattr(spec, "roles", ()) or ())
        if "inference" in roles and not str(
                getattr(spec, "qualification_ref", "") or "").strip():
            raise SystemExit(
                f"target {target_id!r} is registered but NOT qualified for inference "
                "(empty qualification_ref): refusing to construct an invocation path "
                "for a non-routable host")
        kind = str(getattr(spec, "runtime_kind", "") or "ollama").strip().lower()
        if kind == "llama-server":
            from llama_server_client import LlamaServerTarget

            return LlamaServerTarget(spec.target_id, spec.ssh_host, spec.model,
                                     spec.endpoint)
        if kind == "ollama":
            return ollama_client.Target(spec.target_id, spec.ssh_host, spec.model)
        raise SystemExit(
            f"target {target_id!r} declares runtime_kind {kind!r}, for which no "
            "client exists: refusing rather than guessing an endpoint")
    # No spec: fall back to the legacy ollama map, which raises KeyError for an
    # unknown id. A target that is not registered is not addressable.
    host, model = ollama_client.TARGETS[target_id]
    return ollama_client.Target(target_id, host, model)
