"""Tests for core.log_safety.redact_url (PS-602 / upstream #4750).

A negative/equivalence control for the credential-logging fix: the redactor must
strip EVERY credential-bearing component (userinfo, query, fragment) while
keeping the diagnostic parts (scheme/host/port/path), and must never echo a
secret. This is the vulnerability-specific control -- the old code logged the
raw URL, which is exactly what these assertions forbid.
"""

import pytest

from core.log_safety import redact_url


@pytest.mark.parametrize("raw,expected", [
    ("https://user:pass@api.example.com/v1/models", "https://api.example.com/v1/models"),
    ("https://api.example.com/v1/models?api_key=SECRET123", "https://api.example.com/v1/models"),
    ("https://api.example.com/v1/models#frag", "https://api.example.com/v1/models"),
    ("https://u:p@h.example:8443/v1?token=T#x", "https://h.example:8443/v1"),
    ("http://localhost:11434/v1", "http://localhost:11434/v1"),
    ("https://[2001:db8::1]:8443/v1", "https://[2001:db8::1]:8443/v1"),
    ("", ""),
])
def test_redact_url_strips_credentials_keeps_diagnostics(raw, expected):
    assert redact_url(raw) == expected


def test_redact_url_never_echoes_secrets():
    # The negative control: no secret/credential substring may survive.
    for secret in ("hunter2", "SECRET123", "T", "pass"):
        raw = f"https://user:{secret}@api.example.com/v1?api_key={secret}"
        out = redact_url(raw)
        assert secret not in out, (secret, out)
        assert "user" not in out
