"""Integration tests for the Settings config REST surface (/api/config).

Self-contained like tests/test_routing_harness_routes.py: a bare FastAPI app
with ONLY the config router + an in-memory SQLite DB + a stub auth_manager and
header-driven middleware, so require_admin_cookie runs its REAL logic. Each
test's live budget file + archives land under a monkeypatched ODYSSEUS_DATA_DIR
tmp dir (seeded from the repo's baked config/routing_budget.json), so nothing
touches the repo ./data.

Exercises the PR-A CONTRACT:
  - GET /budget shape (caps/version/spend/persisted/live_path)
  - POST /budget/publish auto-bumps version + persists; 400 {detail:[...]} on
    bad caps with the live file untouched (fail-safe)
  - GET /budget/versions + POST /budget/rollback roundtrip
  - GET /effective honest item set
  - admin-cookie gating (401/403 unauth) on every route
"""
import os
import sys

import pytest
import sqlalchemy
from sqlalchemy.orm import sessionmaker
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _auth_disabled_env(monkeypatch):
    # Functional tests run with auth "disabled"; require_admin_cookie still
    # demands a real admin cookie user regardless (see test_routing_harness).
    monkeypatch.setenv("AUTH_ENABLED", "false")


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path))
    return tmp_path


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.database as cdb  # noqa: E402
import routes.config_routes as cr  # noqa: E402

ADMIN = {"x-test-user": "admin"}
NON_ADMIN = {"x-test-user": "bob"}
BEARER = {"x-test-user": "admin", "x-test-bearer": "1"}

GOOD_CAPS = {
    "daily_max_usd": 20.0, "weekly_max_usd": 80.0, "monthly_max_usd": 200.0,
    "premium_daily_max_usd": 8.0, "premium_weekly_max_usd": 30.0,
}


class _StubAuthManager:
    is_configured = True

    def is_admin(self, user):
        return user == "admin"

    def get_privileges(self, user):
        return {"security_admin": user == "sec"}


def _make_app_and_client():
    engine = sqlalchemy.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    cdb.Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autoflush=False)
    cr.SessionLocal = TestSession

    app = FastAPI()
    app.state.auth_manager = _StubAuthManager()
    cr.setup_config_routes(app)

    @app.middleware("http")
    async def _stamp(request, call_next):
        user = request.headers.get("x-test-user")
        if user:
            request.state.current_user = user
        if request.headers.get("x-test-bearer"):
            request.state.api_token = True
        return await call_next(request)

    return app, TestClient(app)


# ---------- GET /budget ----------
def test_budget_get_shape():
    _app, client = _make_app_and_client()
    r = client.get("/api/config/budget", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    # Seeded from the baked config/routing_budget.json (version 1.0, caps 10/50/...).
    assert body["caps"]["daily_max_usd"] == 10.0
    assert body["version"] == "1.0"
    assert body["persisted"] is True
    assert body["live_path"].endswith("routing/routing_budget.json")
    for k in ("daily_usd", "weekly_usd", "monthly_usd",
              "premium_daily_usd", "premium_weekly_usd"):
        assert k in body["spend"]


# ---------- publish ----------
def test_publish_bumps_version_and_persists():
    _app, client = _make_app_and_client()
    r = client.post("/api/config/budget/publish", headers=ADMIN, json=GOOD_CAPS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["version"] == "1.1"  # server auto-bumped from seeded 1.0
    assert body["caps"]["daily_max_usd"] == 20.0

    # Persisted: a fresh GET reflects the new caps + version.
    g = client.get("/api/config/budget", headers=ADMIN).json()
    assert g["caps"]["daily_max_usd"] == 20.0
    assert g["version"] == "1.1"


def test_publish_ignores_client_version():
    _app, client = _make_app_and_client()
    payload = dict(GOOD_CAPS, version="99.99")  # extra field ignored by the model
    r = client.post("/api/config/budget/publish", headers=ADMIN, json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["version"] == "1.1"  # not 99.99


def test_publish_400_on_nonpositive_cap():
    _app, client = _make_app_and_client()
    bad = dict(GOOD_CAPS, daily_max_usd=0)
    r = client.post("/api/config/budget/publish", headers=ADMIN, json=bad)
    assert r.status_code == 400, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, list)
    assert any("daily_max_usd" in d for d in detail)
    # Fail-safe: the live file still reads the seeded default (unchanged).
    assert client.get("/api/config/budget", headers=ADMIN).json()["version"] == "1.0"


def test_publish_400_on_premium_above_general():
    _app, client = _make_app_and_client()
    bad = dict(GOOD_CAPS, premium_daily_max_usd=999.0)
    r = client.post("/api/config/budget/publish", headers=ADMIN, json=bad)
    assert r.status_code == 400, r.text
    assert any("premium_daily" in d for d in r.json()["detail"])


# ---------- versions + rollback ----------
def test_versions_and_rollback_roundtrip():
    _app, client = _make_app_and_client()
    # publish A (1.1), then B (1.2)
    caps_a = dict(GOOD_CAPS, daily_max_usd=20.0)
    caps_b = dict(GOOD_CAPS, daily_max_usd=30.0)
    client.post("/api/config/budget/publish", headers=ADMIN, json=caps_a)
    client.post("/api/config/budget/publish", headers=ADMIN, json=caps_b)

    versions = client.get("/api/config/budget/versions", headers=ADMIN).json()
    # publish A archived the seeded 1.0; publish B archived 1.1 -> 2 archives.
    assert len(versions) == 2
    for v in versions:
        assert set(v.keys()) == {"archive_name", "version", "ts", "actor"}
    assert versions[0]["version"] == "1.1"  # newest archive first
    assert versions[1]["version"] == "1.0"

    # Roll back to the 1.1 snapshot (caps A).
    rb = client.post("/api/config/budget/rollback", headers=ADMIN,
                     json={"archive_name": versions[0]["archive_name"]})
    assert rb.status_code == 200, rb.text
    assert rb.json()["ok"] is True
    assert rb.json()["caps"]["daily_max_usd"] == 20.0

    g = client.get("/api/config/budget", headers=ADMIN).json()
    assert g["caps"]["daily_max_usd"] == 20.0
    # The rollback itself archived the 1.2 file -> 3 archives now.
    assert len(client.get("/api/config/budget/versions", headers=ADMIN).json()) == 3


def test_rollback_bad_name_400():
    _app, client = _make_app_and_client()
    r = client.post("/api/config/budget/rollback", headers=ADMIN,
                    json={"archive_name": "../routing_budget.json"})
    assert r.status_code == 400, r.text
    assert isinstance(r.json()["detail"], list)


# ---------- effective ----------
def test_effective_returns_honest_item_set():
    _app, client = _make_app_and_client()
    r = client.get("/api/config/effective", headers=ADMIN)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    names = {i["name"] for i in items}
    assert {"budget.caps", "budget.persisted", "policy.persisted", "data_root"} <= names
    surfaces = {i["surface"] for i in items}
    assert surfaces <= {"runtime", "needs_redeploy", "deploy_only"}
    for i in items:
        assert set(i.keys()) >= {"name", "value", "source", "surface", "editable_where"}


# ---------- auth gating (AUTH_ENABLED=true, real gate code paths) ----------
class TestAuthGating:
    def setup_method(self):
        os.environ["AUTH_ENABLED"] = "true"

    def teardown_method(self):
        os.environ["AUTH_ENABLED"] = "false"

    GATED = [
        ("get", "/api/config/budget", None),
        ("post", "/api/config/budget/publish", GOOD_CAPS),
        ("get", "/api/config/budget/versions", None),
        ("post", "/api/config/budget/rollback", {"archive_name": "x.json"}),
        ("get", "/api/config/effective", None),
    ]

    def _call(self, client, method, path, body, headers):
        kwargs = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        return getattr(client, method)(path, **kwargs)

    def test_unauthenticated_rejected(self):
        _app, client = _make_app_and_client()
        for method, path, body in self.GATED:
            r = self._call(client, method, path, body, headers={})
            assert r.status_code == 403, f"{path}: {r.status_code}"

    def test_bearer_token_rejected(self):
        _app, client = _make_app_and_client()
        for method, path, body in self.GATED:
            r = self._call(client, method, path, body, headers=BEARER)
            assert r.status_code == 403, f"{path}: {r.status_code}"

    def test_non_admin_cookie_rejected(self):
        _app, client = _make_app_and_client()
        for method, path, body in self.GATED:
            r = self._call(client, method, path, body, headers=NON_ADMIN)
            assert r.status_code == 403, f"{path}: {r.status_code}"

    def test_admin_cookie_allowed(self):
        _app, client = _make_app_and_client()
        assert client.get("/api/config/budget", headers=ADMIN).status_code == 200
        assert client.get("/api/config/effective", headers=ADMIN).status_code == 200
