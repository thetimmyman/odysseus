"""The session cookie's Secure flag follows the request.

With SECURE_COOKIES unset the flag derives from the request scheme (or
``X-Forwarded-Proto``), so HTTPS installs never hand out a cleartext-sendable
cookie; an explicit true/false still forces it.

Drives the real ``/api/auth/login`` endpoint from ``setup_auth_routes`` with a
capturing response, so the assertion is on the cookie actually set.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from routes.auth_routes import setup_auth_routes, LoginRequest


class _CapturingResponse:
    """Minimal stand-in for starlette's Response; records set_cookie kwargs."""

    def __init__(self):
        self.cookie_kwargs = None

    def set_cookie(self, **kwargs):
        self.cookie_kwargs = kwargs

    def delete_cookie(self, *args, **kwargs):  # pragma: no cover - not used here
        pass


def _login_endpoint(auth_manager):
    router = setup_auth_routes(auth_manager)
    for r in router.routes:
        if (getattr(r, "path", None) == "/api/auth/login"
                and "POST" in getattr(r, "methods", set())):
            return r.endpoint
    raise AssertionError("login route not found on the auth router")


def _login_request(scheme="http", forwarded_proto=None):
    """Stand-in for fastapi.Request carrying the fields login reads: the client
    host (rate limiter), and the URL scheme plus ``X-Forwarded-Proto``."""
    headers = {}
    if forwarded_proto is not None:
        headers["x-forwarded-proto"] = forwarded_proto
    return SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        url=SimpleNamespace(scheme=scheme),
        headers=headers,
        cookies={},
    )


def _login_secure_flag(scheme, forwarded_proto=None):
    """Log in over ``scheme`` and return the cookie's Secure flag."""
    mgr = MagicMock()
    mgr.verify_password.return_value = True
    mgr.totp_enabled.return_value = False
    mgr.create_session_trusted.return_value = "tok-123"
    endpoint = _login_endpoint(mgr)
    response = _CapturingResponse()
    body = LoginRequest(username="carol", password="carol-password")
    request = _login_request(scheme, forwarded_proto)

    result = asyncio.run(endpoint(body=body, request=request, response=response))
    assert result["ok"] is True
    return response.cookie_kwargs["secure"]


def test_https_login_marks_cookie_secure_without_config(monkeypatch):
    monkeypatch.delenv("SECURE_COOKIES", raising=False)

    # A session token handed out over HTTPS must not be allowed to travel
    # back in cleartext just because nobody set an env var.
    assert _login_secure_flag("https") is True


def test_plain_http_login_leaves_cookie_insecure_without_config(monkeypatch):
    monkeypatch.delenv("SECURE_COOKIES", raising=False)

    # Marking it Secure here would make the browser drop the cookie and
    # break login on a plain-HTTP install.
    assert _login_secure_flag("http") is False


def test_empty_env_value_is_treated_as_unset(monkeypatch):
    # docker-compose passes the variable through empty when the host has not
    # defined it; that must derive, not disable.
    monkeypatch.setenv("SECURE_COOKIES", "")
    assert _login_secure_flag("https") is True
    assert _login_secure_flag("http") is False


def test_forwarded_proto_https_marks_cookie_secure(monkeypatch):
    monkeypatch.delenv("SECURE_COOKIES", raising=False)

    # A terminator that is not on an address uvicorn trusts leaves the
    # connection scheme as http, so the header is the only signal there.
    assert _login_secure_flag("http", forwarded_proto="https") is True
    # Chained proxies send a list; the client-facing hop is the first entry.
    assert _login_secure_flag("http", forwarded_proto="https, http") is True


def test_forwarded_proto_http_does_not_downgrade_https_scheme(monkeypatch):
    monkeypatch.delenv("SECURE_COOKIES", raising=False)

    # Either signal saying https is enough -- the same test core/middleware.py
    # applies before sending HSTS.
    assert _login_secure_flag("https", forwarded_proto="http") is True
    assert _login_secure_flag("http", forwarded_proto="http") is False


def test_explicit_true_forces_secure_even_on_plain_http(monkeypatch):
    monkeypatch.setenv("SECURE_COOKIES", "true")
    assert _login_secure_flag("http") is True


def test_explicit_false_stays_off_even_on_https(monkeypatch):
    # The escape hatch for an install answering on both HTTP and HTTPS.
    monkeypatch.setenv("SECURE_COOKIES", "false")
    assert _login_secure_flag("https") is False


def test_env_value_is_case_and_whitespace_insensitive(monkeypatch):
    monkeypatch.setenv("SECURE_COOKIES", "  TRUE  ")
    assert _login_secure_flag("http") is True
    monkeypatch.setenv("SECURE_COOKIES", "False")
    assert _login_secure_flag("https") is False
