"""Authenticated in-Odysseus preview proxy.

Serves the LOOPBACK-ONLY dev server (127.0.0.1:DEV_PORT, never published) at the
ROOT of a SEPARATE admin-cookie-gated origin (PROXY_PORT), so the Dev Preview
iframe can embed it. Mirrors how the app is already proxied in Codespaces
(origin-root, no basePath). Security:

  * ADMIN COOKIE ONLY — every request (HTTP + WS) must carry a valid
    `odysseus_session` admin cookie. Bearer/Authorization, the internal-tool
    loopback header, and missing/non-admin cookies are all rejected. No bypass.
  * FIXED UPSTREAM — always forwards to 127.0.0.1:DEV_PORT (the single active
    manager dev server). Never an arbitrary URL. 503 when nothing is running.
  * FRAME-UNBLOCK ONLY — strips `X-Frame-Options` and the CSP `frame-ancestors`
    directive (and nothing else) so Odysseus can iframe it; the rest of the
    app's CSP is preserved.
  * HTTP + WebSocket/HMR, redirects, Set-Cookie (multi), request bodies,
    /_next assets, app /api routes — all proxied faithfully.
"""

import asyncio
import logging
from urllib.parse import urlparse

import httpx
import websockets
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route, WebSocketRoute

logger = logging.getLogger("dev_preview_proxy")

SESSION_COOKIE = "odysseus_session"
# Hop-by-hop headers (RFC 7230) never forwarded.
_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
        "te", "trailers", "transfer-encoding", "upgrade", "host"}

_auth_manager = None
_client: httpx.AsyncClient = None
_dev_port = 3000


def _internal_tool_header():
    try:
        from core.middleware import INTERNAL_TOOL_HEADER
        return (INTERNAL_TOOL_HEADER or "").lower()
    except Exception:
        return "x-internal-tool"


def _admin_ok(headers, cookies) -> bool:
    """Cookie-only admin gate — rejects bearer + internal-tool, requires a valid
    admin `odysseus_session`. `headers` is a Starlette Headers (case-insensitive)."""
    if headers.get("authorization"):
        return False
    ith = _internal_tool_header()
    if ith and headers.get(ith):
        return False
    tok = cookies.get(SESSION_COOKIE)
    if not tok or _auth_manager is None:
        return False
    try:
        user = _auth_manager.get_username_for_token(tok)
        return bool(user) and _auth_manager.is_admin(user)
    except Exception:
        return False


def _active() -> bool:
    try:
        from src import dev_preview
        return dev_preview.status().get("running") is not None
    except Exception:
        return False


def _same_site(headers, host_header) -> bool:
    """CSRF guard for a 0.0.0.0-exposed origin: reject CROSS-SITE requests.
    Prefers Fetch-Metadata (Sec-Fetch-Site); falls back to an Origin/Host match.
    Absent both (curl, same-origin GET that omits Origin) => allow."""
    sfs = (headers.get("sec-fetch-site") or "").lower()
    if sfs:
        return sfs in ("same-origin", "same-site", "none")
    origin = headers.get("origin")
    if origin:
        oh = (urlparse(origin).netloc or "").lower()
        return bool(oh) and oh == (host_header or "").lower()
    return True


def _strip_frame_ancestors(csp: str) -> str:
    keep = [seg.strip() for seg in csp.split(";")
            if seg.strip() and not seg.strip().lower().startswith("frame-ancestors")]
    return "; ".join(keep)


def _filter_response_headers(resp: httpx.Response):
    """Faithful pass-through MINUS hop-by-hop + the frame blockers; preserves
    multiple Set-Cookie, content-encoding, content-length, Location, etc."""
    out = []
    for k, v in resp.headers.multi_items():
        lk = k.lower()
        if lk in _HOP:
            continue
        if lk == "x-frame-options":
            continue
        if lk == "content-security-policy":
            v = _strip_frame_ancestors(v)
            if not v:
                continue
        out.append((k.encode("latin-1"), v.encode("latin-1")))
    return out


async def _http(request):
    if not _admin_ok(request.headers, request.cookies):
        return PlainTextResponse("Admin cookie session required.", status_code=401)
    if request.method in ("POST", "PUT", "DELETE", "PATCH", "CONNECT", "TRACE") and \
            not _same_site(request.headers, request.headers.get("host")):
        return PlainTextResponse("Cross-site request rejected.", status_code=403)
    if not _active():
        return PlainTextResponse("No preview app is running. Start one in Dev Preview.",
                                 status_code=503)
    url = httpx.URL(scheme="http", host="127.0.0.1", port=_dev_port,
                    path=request.url.path, query=request.url.query.encode("ascii") if request.url.query else b"")
    fwd_headers = [(k, v) for k, v in request.headers.items() if k.lower() not in _HOP]
    fwd_headers.append(("host", f"127.0.0.1:{_dev_port}"))
    body = await request.body()
    try:
        req = _client.build_request(request.method, url, headers=fwd_headers, content=body)
        upstream = await _client.send(req, stream=True)
    except Exception as e:
        logger.warning("proxy upstream error %s %s: %r", request.method, request.url.path, e)
        return PlainTextResponse("Preview upstream unavailable.", status_code=502)
    resp = StreamingResponse(
        upstream.aiter_raw(),
        status_code=upstream.status_code,
        background=BackgroundTask(upstream.aclose),
    )
    # Replace Starlette's computed headers with the faithful, frame-unblocked
    # upstream set (preserves multi Set-Cookie, content-encoding, Location, …).
    resp.raw_headers = _filter_response_headers(upstream)
    return resp


async def _ws(websocket):
    if not _admin_ok(websocket.headers, websocket.cookies):
        await websocket.close(code=1008)
        return
    if not _same_site(websocket.headers, websocket.headers.get("host")):
        await websocket.close(code=1008)
        return
    if not _active():
        await websocket.close(code=1011)
        return
    subprotocols = list(websocket.scope.get("subprotocols") or [])
    q = websocket.url.query
    up_url = f"ws://127.0.0.1:{_dev_port}{websocket.url.path}" + (f"?{q}" if q else "")
    try:
        upstream = await websockets.connect(
            up_url, subprotocols=subprotocols or None, open_timeout=10,
            max_size=None, ping_interval=None)
    except Exception as e:
        logger.warning("ws upstream connect failed %s: %r", websocket.url.path, e)
        await websocket.close(code=1011)
        return
    # Echo the UPSTREAM-negotiated subprotocol (not blindly the client's first).
    await websocket.accept(subprotocol=getattr(upstream, "subprotocol", None))

    async def c2u():
        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("text") is not None:
                    await upstream.send(msg["text"])
                elif msg.get("bytes") is not None:
                    await upstream.send(msg["bytes"])
        except Exception:
            pass

    async def u2c():
        try:
            async for m in upstream:
                if isinstance(m, (bytes, bytearray)):
                    await websocket.send_bytes(m)
                else:
                    await websocket.send_text(m)
        except Exception:
            pass

    try:
        await asyncio.gather(c2u(), u2c())
    finally:
        try:
            await upstream.close()
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass


def make_app(auth_manager, dev_port: int):
    global _auth_manager, _client, _dev_port
    _auth_manager = auth_manager
    _dev_port = dev_port
    _client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=None),
                                follow_redirects=False)
    return Starlette(routes=[
        WebSocketRoute("/{path:path}", _ws),
        Route("/{path:path}", _http,
              methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]),
    ])


async def serve(auth_manager, dev_port: int, host: str = "0.0.0.0", port: int = 7100):
    """Run the gated proxy server (a second uvicorn) alongside Odysseus."""
    import uvicorn
    app = make_app(auth_manager, dev_port)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning",
                            ws="websockets", access_log=False)
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None   # don't fight the main server
    logger.info("dev-preview proxy listening on %s:%d -> 127.0.0.1:%d", host, port, dev_port)
    await server.serve()
