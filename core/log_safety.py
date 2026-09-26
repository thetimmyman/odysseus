"""Keep credentials in admin-configured URLs (userinfo, query) out of logs."""

from urllib.parse import urlparse, urlunparse


def redact_url(url: str) -> str:
    """Return *url* without userinfo, query or fragment; keeps scheme, host, port, path."""
    try:
        parsed = urlparse(url or "")
        host = parsed.hostname or ""
        if ":" in host:  # IPv6 literal -- re-bracket so host:port stays unambiguous
            host = f"[{host}]"
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunparse((parsed.scheme, host, parsed.path, "", "", ""))
    except Exception:
        return "<endpoint>"
