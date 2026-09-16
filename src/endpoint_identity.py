"""Canonical identity for execution endpoints."""
from __future__ import annotations

from urllib.parse import urlsplit


class EndpointIdentityError(ValueError):
    """Raised when an endpoint cannot be represented exactly."""


def canonical_endpoint_identity(url: str, endpoint_type: str = "") -> str:
    """Return a stable scheme/host/port/path/API-shape identity."""
    raw = str(url or "").strip()
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.hostname:
        raise EndpointIdentityError(f"endpoint URL is not absolute: {url!r}")
    try:
        port = parsed.port
    except ValueError as exc:
        raise EndpointIdentityError(f"endpoint URL has invalid port: {url!r}") from exc
    host = parsed.hostname.lower()
    netloc = f"{host}:{port}" if port is not None else host
    path = parsed.path.rstrip("/") or "/"
    return "|".join((str(endpoint_type or "").strip().lower(),
                     parsed.scheme.lower(), netloc, path,
                     parsed.query, parsed.fragment))
