"""Shared auth helpers used by all route files."""

import os
from typing import Optional
from fastapi import Request, HTTPException


def get_current_user(request: Request) -> Optional[str]:
    """Get current username from request state (set by auth middleware)."""
    return getattr(request.state, 'current_user', None)


def effective_user(request: Request):
    """The real human behind the request, for ownership/attribution.

    Cookie sessions resolve to the logged-in username. Bearer ``ody_`` callers
    come through as the sandboxed pseudo-user "api" so they can't wander into
    cookie/user routes by default, but their token was minted by, and belongs
    to, a real owner stamped on ``request.state.api_token_owner``. Routes that
    should attribute a token's actions to that owner (sessions, chat history)
    call this instead of :func:`get_current_user`, so a paired client sees and
    creates the SAME data as the owner's desktop UI rather than a separate
    "api"-owned silo.

    For cookie sessions this is identical to :func:`get_current_user`, so
    swapping a route over is a no-op for browser users. A bearer token with no
    owner falls back to :func:`get_current_user` (the "api" pseudo-user), so it
    never escalates.
    """
    if getattr(request.state, "api_token", False):
        owner = getattr(request.state, "api_token_owner", None)
        if owner:
            return owner
    return get_current_user(request)


def _auth_disabled() -> bool:
    """True when the operator has explicitly turned off auth via .env.
    Mirrors the AUTH_ENABLED parse in app.py / core/middleware.py so the
    three call sites agree on what "off" means."""
    return os.getenv("AUTH_ENABLED", "true").lower() == "false"


def require_user(request: Request) -> str:
    """FastAPI dependency: reject unauthenticated callers when the upstream
    auth middleware was bypassed unexpectedly (e.g. SSRF from a sibling
    service). Returns the resolved username, or "" in single-user / anonymous
    modes where no username is available.

    The three "" cases are:
      1. AUTH_ENABLED=false — the operator explicitly turned auth off.
         The full /login flow is skipped (issue #622), so route-level
         require_user must let the request through too instead of 401-ing
         and forcing the browser to /login.
      2. Unconfigured first-run + loopback caller — pre-setup access from
         localhost so the operator can hit the SPA before creating the
         first admin.
      3. LOCALHOST_BYPASS=true + loopback caller — documented dev bypass.

    Use this on routes that touch user data so middleware misconfig can't
    open them up.
    """
    u = get_current_user(request)
    if u:
        return u
    # Operator-disabled auth: honor it at the route layer too. Without this,
    # routes that depend on require_user 401, the front-end fetch wrapper
    # redirects to /login, and the user sees a login page despite
    # AUTH_ENABLED=false (issue #622). Docker / reverse-proxy deployments
    # hit this because requests arrive from a non-loopback client.host, so
    # the loopback fall-through below never fires.
    if _auth_disabled():
        return ""
    auth_mgr = getattr(request.app.state, "auth_manager", None)
    client = getattr(request, "client", None)
    host = (client.host if client else "") or ""
    is_loopback = host in ("127.0.0.1", "::1", "localhost")
    # LOCALHOST_BYPASS=true is the dev-only "I'm on loopback, skip auth"
    # switch. Mirror the middleware so routes don't 401 the same caller
    # the middleware just let through.
    if is_loopback and os.getenv("LOCALHOST_BYPASS", "false").lower() == "true":
        return ""
    if auth_mgr is not None and getattr(auth_mgr, "is_configured", False):
        raise HTTPException(401, "Not authenticated")
    # Unconfigured / first-run mode: only allow loopback callers.
    if is_loopback:
        return ""
    raise HTTPException(401, "Not authenticated")


def require_privilege(request: Request, key: str) -> str:
    """Reject callers whose `auth.json` privilege flag for `key` is False.
    Returns the username so the route handler can keep using it.

    Admins always have every privilege via `auth_manager.get_privileges`
    (which returns ADMIN_PRIVILEGES wholesale), so this is a no-op for
    them. In unauthenticated single-user mode (`require_user` returns ""),
    privileges aren't enforced.
    """
    user = require_user(request)
    if not user:
        return user
    auth_mgr = getattr(request.app.state, "auth_manager", None)
    if auth_mgr is None:
        return user
    try:
        privs = auth_mgr.get_privileges(user) or {}
    except Exception:
        return user
    # True = permitted; missing key defaults to permitted (unknown privileges
    # fail open — the UI gates display-side).
    if not privs.get(key, True):
        raise HTTPException(403, f"Your account is not allowed to {key.replace('_', ' ')}.")
    return user


def require_admin_cookie(request: Request) -> str:
    """Cookie-only admin gate for the Argo crew approval routes (the Oracle's
    seal). Deliberately does NOT delegate to ``core.middleware.require_admin``,
    which honours the internal-tool loopback token and ``current_user ==
    "internal-tool"`` — an ``app_api``/loopback path could otherwise ride that
    into ``/approve`` and approve a destructive ``bash``/email side effect.

    Rejects, in order:
      * any bearer-token caller (``request.state.api_token`` truthy),
      * the ``api`` / ``internal-tool`` pseudo-users,
      * a present internal-tool loopback header,
    THEN requires a real cookie user with ``auth_manager.is_admin(user)``.

    Raises ``HTTPException(403)`` on any failure; returns the admin username on
    success. Desktop-admin-only for the MVP (Decision A) — phone/remote
    approval via a ``crew_approve`` scope is a deliberate Tier-2 follow-up.
    """
    # 1. Reject bearer tokens outright (companion/mobile tokens default to
    #    scopes="chat" and no route checks scopes — see hardening fix #4).
    if getattr(request.state, "api_token", False):
        raise HTTPException(403, "Admin cookie session required")

    # 2. Reject the sandboxed / loopback pseudo-users.
    user = get_current_user(request)
    if user in ("api", "internal-tool"):
        raise HTTPException(403, "Admin cookie session required")

    # 3. Reject any internal-tool loopback marker on the request, even if the
    #    middleware did not stamp current_user (belt-and-suspenders).
    try:
        from core.middleware import INTERNAL_TOOL_HEADER
        if request.headers.get(INTERNAL_TOOL_HEADER):
            raise HTTPException(403, "Admin cookie session required")
    except HTTPException:
        raise
    except Exception:
        pass

    # 4. Require a real, admin cookie user.
    auth_manager = getattr(request.app.state, "auth_manager", None)
    if auth_manager is None:
        raise HTTPException(403, "Admin only")
    if not user or not auth_manager.is_admin(user):
        raise HTTPException(403, "Admin only")
    return user


def owner_filter(query, model_cls, user: str, *, include_shared: bool = True):
    """Filter `query` so only rows owned by `user` (and optionally null-owner
    'shared' rows) come through. No-op when `user` is empty (single-user
    mode). Returns the modified query."""
    if not user:
        return query
    if include_shared:
        return query.filter((model_cls.owner == user) | (model_cls.owner == None))  # noqa: E711
    return query.filter(model_cls.owner == user)
