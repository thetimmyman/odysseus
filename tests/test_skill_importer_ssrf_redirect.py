"""Skill importer SSRF hardening (PS-602 / upstream #5261): redirects re-validated per hop.

Previously the importer ran the lenient guard on the *initial* URL only and let
``httpx`` follow redirects itself, so a ``3xx`` to an internal/metadata address
(``127.0.0.1``, ``169.254.169.254``) was still connected to. ``_get_checked``
now follows redirects by hand and re-runs the guard with ``block_private=True``
on every hop. These tests are hermetic: every host is an IP literal (no DNS) and
the HTTP layer is faked (no socket).
"""
import pytest

from services.memory import skill_importer
from services.memory.skill_importer import (
    SkillImportError,
    _check_fetch_url,
    _get_checked,
)

PUBLIC_A = "https://1.1.1.1/skill"
LOOPBACK = "http://127.0.0.1/latest"
METADATA = "http://169.254.169.254/latest/meta-data/"


def _install_fake_client(monkeypatch, *, redirect_from, redirect_to):
    """Replace httpx.Client so ``redirect_from`` 302s to ``redirect_to`` and any
    other URL returns 200. No real socket is opened."""

    class _Resp:
        def __init__(self, url, status, location):
            self.url = url
            self.status_code = status
            self.headers = {"location": location} if location else {}
            self.content = b"ok"
            self.text = ""

        def json(self):
            return {}

    class _Client:
        def __init__(self, *args, **kwargs):
            # Invariant: the importer re-follows redirects by hand and must
            # disable httpx's own redirect following, otherwise this test would
            # silently pass while the guard is bypassed.
            assert kwargs.get("follow_redirects") is False, (
                "skill importer must construct httpx.Client with "
                f"follow_redirects=False; got {kwargs.get('follow_redirects')!r}"
            )

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, headers=None):
            if url == redirect_from:
                return _Resp(url, 302, redirect_to)
            return _Resp(url, 200, None)

    monkeypatch.setattr(skill_importer.httpx, "Client", _Client)


@pytest.mark.parametrize("url", [LOOPBACK, METADATA, "http://10.0.0.5/", "http://[::1]/"])
def test_check_fetch_url_blocks_internal(url):
    with pytest.raises(SkillImportError):
        _check_fetch_url(url)


@pytest.mark.parametrize("url", [PUBLIC_A, "https://8.8.8.8/skill"])
def test_check_fetch_url_allows_public(url):
    _check_fetch_url(url)  # must not raise


def test_redirect_to_loopback_is_refused(monkeypatch):
    _install_fake_client(
        monkeypatch,
        redirect_from=PUBLIC_A,
        redirect_to=LOOPBACK,
    )
    with pytest.raises(SkillImportError):
        _get_checked(PUBLIC_A)


def test_redirect_to_metadata_is_refused(monkeypatch):
    _install_fake_client(
        monkeypatch,
        redirect_from=PUBLIC_A,
        redirect_to=METADATA,
    )
    with pytest.raises(SkillImportError):
        _get_checked(PUBLIC_A)


def test_public_redirect_still_returns_final_response(monkeypatch):
    _install_fake_client(
        monkeypatch,
        redirect_from=PUBLIC_A,
        redirect_to="https://8.8.8.8/skill",
    )
    resp = _get_checked(PUBLIC_A)
    assert resp.status_code == 200
    assert resp.url == "https://8.8.8.8/skill"
