"""Preference HTTP handlers retain middleware-resolved user scoping."""
import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from routes.prefs_routes import setup_prefs_routes
from src import user_preferences as prefs


@pytest.mark.parametrize("owner", ["alice", None])
def test_prefs_api_round_trip_preserves_other_users(owner, tmp_path, monkeypatch):
    prefs_file = tmp_path / "user_prefs.json"
    prefs_file.write_text(json.dumps({"_users": {
        "alice": {"theme": "light"},
        "bob": {"theme": "paper"},
    }}), encoding="utf-8")
    monkeypatch.setattr(prefs, "PREFS_FILE", str(prefs_file))
    app = FastAPI()

    @app.middleware("http")
    async def resolved_user(request: Request, call_next):
        request.state.current_user = owner
        return await call_next(request)

    app.include_router(setup_prefs_routes())
    with TestClient(app) as client:
        response = client.get("/api/prefs")
        assert response.status_code == 200
        assert response.json() == {"theme": "light"}
        response = client.get("/api/prefs/missing")
        assert response.status_code == 200
        assert response.json() == {"key": "missing", "value": None}
        response = client.put("/api/prefs/theme", json={"value": "dark", "owner": "bob"})
        assert response.status_code == 200
        assert response.json() == {"key": "theme", "value": "dark"}
        assert client.get("/api/prefs/theme").json() == {"key": "theme", "value": "dark"}

    assert prefs.load_all() == {"_users": {
        "alice": {"theme": "dark"},
        "bob": {"theme": "paper"},
    }}
