"""Auth evaluates the same path Starlette routes.

Exempt prefixes match by path segment, so ``/staticfoo`` is not covered by the
``/static`` exemption, and the ASGI ``root_path`` is stripped before policy
lookup so a deployment mount cannot change which policy applies.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from core.middleware import (
    get_application_route_path,
    path_is_route_or_child,
    with_asgi_root_path,
)

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("root_path", "path", "expected"),
    [
        ("", "/api/models", "/api/models"),
        ("/odysseus", "/odysseus/api/models", "/api/models"),
        ("/odysseus/", "/odysseus//api/models", "/api/models"),
        ("/", "//api/models", "/api/models"),
        ("/odysseus", "/odyssey/api/models", "/odyssey/api/models"),
        ("/app", "/application/api/models", "/application/api/models"),
        ("/odysseus", "/odysseus", ""),
    ],
)
def test_application_route_path_matches_starlette_semantics(root_path, path, expected):
    assert get_application_route_path({
        "root_path": root_path,
        "path": path,
    }) == expected


@pytest.mark.parametrize(
    ("root_path", "expected"),
    [
        ("", "/login"),
        ("/odysseus", "/odysseus/login"),
        ("/odysseus/", "/odysseus/login"),
        ("/", "/login"),
    ],
)
def test_client_redirect_path_includes_asgi_root_path(root_path, expected):
    assert with_asgi_root_path({"root_path": root_path}, "/login") == expected


def test_route_prefix_matching_is_segment_aware():
    assert path_is_route_or_child("/assets", "/assets") is True
    assert path_is_route_or_child("/assets/app.js", "/assets") is True
    assert path_is_route_or_child("/assets-v2/app.js", "/assets") is False


def test_auth_exempt_prefix_is_segment_aware(tmp_path):
    """A string-prefixed sibling route must NOT match the ``/static`` exemption.

    Runs in a subprocess so the heavy app import stays out of this process."""
    env = os.environ.copy()
    env.update({
        "AUTH_ENABLED": "true",
        "CHROMADB_CONNECT_TIMEOUT": "0.01",
        "CHROMADB_HOST": "127.0.0.1",
        "CHROMADB_PORT": "9",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'app.db'}",
        "LOCALHOST_BYPASS": "false",
        "ODYSSEUS_DATA_DIR": str(tmp_path),
        "ODYSSEUS_DISABLE_MCP": "1",
        "OPENAI_API_KEY": "",
        "PYTHONPATH": str(ROOT),
        "PYTHON_DOTENV_DISABLED": "1",
    })
    probe = textwrap.dedent(
        """
        import app as app_module
        f = app_module._is_auth_exempt
        assert f("/static") is True
        assert f("/static/app.js") is True
        assert f("/staticfoo") is False
        assert f("/staticky/secrets") is False
        assert f("/api/models") is False
        print("AUTH_EXEMPT_OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, result.stdout + result.stderr
