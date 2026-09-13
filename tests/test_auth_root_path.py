"""PS-602 / upstream #5807: auth evaluates the same path Starlette routes.

Two defects fixed together:
1. ``_is_auth_exempt`` matched exempt prefixes with ``path.startswith(prefix)``,
   so ``/staticfoo`` (or ``/something-static-y``) was treated as auth-exempt just
   because it shares a string prefix with the ``/static`` mount. It now uses
   segment-aware ``path_is_route_or_child``.
2. ``AuthMiddleware`` used ``request.url.path`` (which carries the ASGI
   ``root_path`` prefix when mounted) while Starlette routes on the stripped
   path, so a deployment mount could change which policy applied. It now uses
   ``get_application_route_path(request.scope)``.
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
    """Drive the real ``app._is_auth_exempt`` (in a subprocess so the heavy app
    import never leaks into the main test process): a string-prefixed sibling
    route must NOT match the ``/static`` exemption."""
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
