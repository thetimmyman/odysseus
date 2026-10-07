"""User preferences API backed by shared per-user JSON storage."""
from fastapi import APIRouter, Request

from src.auth_helpers import get_current_user
from src.user_preferences import load_for_user, save_for_user


def setup_prefs_routes():
    router = APIRouter(prefix="/api/prefs", tags=["preferences"])

    @router.get("")
    async def get_all_prefs(request: Request):
        user = get_current_user(request)
        return load_for_user(user)

    @router.get("/{key}")
    async def get_pref(request: Request, key: str):
        user = get_current_user(request)
        prefs = load_for_user(user)
        return {"key": key, "value": prefs.get(key)}

    @router.put("/{key}")
    async def set_pref(request: Request, key: str, body: dict):
        user = get_current_user(request)
        prefs = load_for_user(user)
        prefs[key] = body.get("value")
        save_for_user(user, prefs)
        return {"key": key, "value": prefs[key]}

    return router
