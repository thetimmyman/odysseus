"""Dev Preview routes — admin-only control surface for running a repo's dev
server inside the container and previewing it.

Every endpoint requires an ADMIN COOKIE session (require_admin_cookie — rejects
bearer/api/internal-tool; does NOT honor the loopback). The process manager
(src/dev_preview.py) is path-confined to REPOS_ROOT, uses fixed command
templates (no arbitrary shell), npm-only, single-server, killable, capped logs.
"""

import logging
import os
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from src.auth_helpers import require_admin_cookie
from src import dev_preview

logger = logging.getLogger(__name__)


class _AppBody(BaseModel):
    app_id: str


class _StartBody(BaseModel):
    app_id: str
    script: Optional[str] = "dev"
    port: Optional[int] = None


class _EnvSetBody(BaseModel):
    key: str
    value: str


def _same_origin_or_reject(request: Request) -> bool:
    """Fail-closed CSRF guard for state-changing env writes (on top of the
    session cookie's SameSite=Lax). ACCEPT iff Sec-Fetch-Site is same-origin/
    same-site when present AND Origin host-matches the request Host when present;
    REJECT when BOTH are absent on an unsafe method (a real SPA always sends
    Origin on PUT/DELETE, so this only blocks header-stripped curl-shaped calls)."""
    sfs = request.headers.get("sec-fetch-site")
    origin = request.headers.get("origin")
    if sfs is not None and sfs not in ("same-origin", "same-site"):
        return False
    if origin is not None:
        try:
            if urlparse(origin).netloc != request.headers.get("host", ""):
                return False
        except Exception:
            return False
    if sfs is None and origin is None:
        return False
    return True


def _env_write_transport_reason(request: Request) -> Optional[str]:
    """None if a value-write is allowed over this transport, else a refusal
    reason. The PUT body (the secret) and the session cookie both cross the wire
    in cleartext on plaintext HTTP, so refuse plaintext NON-loopback writes by
    default — unless HTTPS, a loopback client (e.g. SSH tunnel), or an explicit
    operator opt-in (DEV_PREVIEW_ALLOW_INSECURE_ENV_WRITE=true)."""
    if os.environ.get("DEV_PREVIEW_ALLOW_INSECURE_ENV_WRITE", "").lower() == "true":
        return None
    # request.url.scheme is the source of truth. uvicorn only folds
    # X-Forwarded-Proto into it when started with --forwarded-allow-ips for a
    # TRUSTED proxy, so an untrusted client cannot spoof HTTPS. We deliberately do
    # NOT read the X-Forwarded-Proto header ourselves — trusting a client-supplied
    # forwarding header would be a spoofable bypass of this gate.
    if request.url.scheme == "https":
        return None
    client = (request.client.host if request.client else "") or ""
    if client in ("127.0.0.1", "::1", "localhost") or client.startswith("127."):
        return None
    return ("Refusing to write a secret over a plaintext non-loopback connection. "
            "Use HTTPS, an SSH tunnel (loopback), or set "
            "DEV_PREVIEW_ALLOW_INSECURE_ENV_WRITE=true on the server.")


def setup_dev_preview_routes() -> APIRouter:
    router = APIRouter(prefix="/api/dev-preview", tags=["dev-preview"])

    @router.get("/apps")
    def apps(request: Request):
        require_admin_cookie(request)
        return {"apps": dev_preview.list_apps(),
                "preview_port": dev_preview.PREVIEW_PORT,
                "proxy_port": dev_preview.PROXY_PORT}

    @router.post("/install")
    def install(request: Request, body: _AppBody):
        require_admin_cookie(request)
        try:
            return dev_preview.install(body.app_id)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.post("/start")
    def start(request: Request, body: _StartBody):
        require_admin_cookie(request)
        try:
            return dev_preview.start(body.app_id, body.script or "dev", body.port)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.post("/stop")
    def stop(request: Request, body: Optional[_AppBody] = None):
        require_admin_cookie(request)
        return dev_preview.stop(body.app_id if body else None)

    @router.get("/status")
    def status(request: Request):
        require_admin_cookie(request)
        return dev_preview.status()

    @router.get("/logs")
    def logs(request: Request, app_id: str, kind: str = "run"):
        require_admin_cookie(request)
        if kind not in ("run", "install"):
            raise HTTPException(400, "kind must be 'run' or 'install'")
        return dev_preview.get_logs(app_id, kind)

    @router.get("/app/{app_id}")
    def app_detail(request: Request, app_id: str):
        require_admin_cookie(request)
        try:
            return dev_preview.app_detail(app_id)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.put("/app/{app_id}/env")
    def env_set(request: Request, app_id: str, body: _EnvSetBody):
        # WRITE-ONLY: the value is in the request body and is NEVER logged or
        # echoed. Admin cookie + fail-closed CSRF guard + transport gate.
        require_admin_cookie(request)
        if not _same_origin_or_reject(request):
            raise HTTPException(403, "cross-site request refused")
        reason = _env_write_transport_reason(request)
        if reason:
            raise HTTPException(400, reason)
        try:
            return dev_preview.env_set(app_id, body.key, body.value)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.delete("/app/{app_id}/env/{key}")
    def env_clear(request: Request, app_id: str, key: str):
        require_admin_cookie(request)
        if not _same_origin_or_reject(request):
            raise HTTPException(403, "cross-site request refused")
        reason = _env_write_transport_reason(request)
        if reason:
            raise HTTPException(400, reason)
        try:
            return dev_preview.env_clear(app_id, key)
        except ValueError as e:
            raise HTTPException(400, str(e))

    class _SourceBody(BaseModel):
        key: str

    @router.post("/app/{app_id}/env/source")
    def env_source(request: Request, app_id: str, body: _SourceBody):
        # Fetches the mapped value from k3s/Vaultwarden (server-side, via ssh
        # minipc) and writes it to .env.local. WRITE-ONLY: the value is never
        # returned. Same admin + CSRF + transport + gitignore gates as a set.
        require_admin_cookie(request)
        if not _same_origin_or_reject(request):
            raise HTTPException(403, "cross-site request refused")
        reason = _env_write_transport_reason(request)
        if reason:
            raise HTTPException(400, reason)
        try:
            return dev_preview.env_source_from_vault(app_id, body.key)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.get("/config")
    def get_config(request: Request):
        require_admin_cookie(request)
        return dev_preview.config()

    class _ConfigBody(BaseModel):
        updates: dict

    @router.put("/config")
    def put_config(request: Request, body: _ConfigBody):
        # Writes runtime-safe config (enabled/app_allowlist/package_manager) to
        # settings.json. No transport gate — these are config, not secret values.
        require_admin_cookie(request)
        if not _same_origin_or_reject(request):
            raise HTTPException(403, "cross-site request refused")
        try:
            return dev_preview.set_config(body.updates)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.get("/app/{app_id}/vault-map")
    def vault_map_get(request: Request, app_id: str):
        require_admin_cookie(request)
        try:
            return dev_preview.vault_map_get(app_id)
        except ValueError as e:
            raise HTTPException(400, str(e))

    class _VaultMapBody(BaseModel):
        key: str
        mapping: dict

    @router.put("/app/{app_id}/vault-map")
    def vault_map_set(request: Request, app_id: str, body: _VaultMapBody):
        # Stores ONLY a validated locator (k3s ns/secret/key or vw item_id/field),
        # never a value. Admin + CSRF; no transport gate (locators aren't secret).
        require_admin_cookie(request)
        if not _same_origin_or_reject(request):
            raise HTTPException(403, "cross-site request refused")
        try:
            return dev_preview.vault_map_set(app_id, body.key, body.mapping)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.delete("/app/{app_id}/vault-map/{key}")
    def vault_map_delete(request: Request, app_id: str, key: str):
        require_admin_cookie(request)
        if not _same_origin_or_reject(request):
            raise HTTPException(403, "cross-site request refused")
        try:
            return dev_preview.vault_map_delete(app_id, key)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @router.get("/security-status")
    def security(request: Request):
        require_admin_cookie(request)
        return dev_preview.security_status()

    return router
