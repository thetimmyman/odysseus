"""Admin-only routes for running and previewing a repo's dev server.

Every endpoint requires an admin cookie (no bearer, no loopback). The process
manager is confined to REPOS_ROOT with fixed npm command templates.
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
    """Fail-closed CSRF guard for env writes: require same-origin/same-site
    Sec-Fetch-Site and a matching Origin when present; reject if both are absent."""
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
    """Return a refusal reason unless the write is over HTTPS, from loopback, or
    DEV_PREVIEW_ALLOW_INSECURE_ENV_WRITE is set: secrets would cross in cleartext."""
    if os.environ.get("DEV_PREVIEW_ALLOW_INSECURE_ENV_WRITE", "").lower() == "true":
        return None
    # Trust only request.url.scheme (uvicorn sets it from X-Forwarded-Proto for
    # trusted proxies only); reading the header ourselves would be spoofable.
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
        # Write-only: the value is never logged or echoed.
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
        # Fetches the mapped secret server-side and writes it to .env.local.
        # Write-only: the value is never returned.
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
        # Non-secret config, so no transport gate.
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
        # Stores only a validated locator, never a value; no transport gate.
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
