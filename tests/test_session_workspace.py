"""Session project identity across the real workspace/chat API boundaries."""
import json
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def workspace_api(monkeypatch, tmp_path):
    import core.database as database
    import core.models as models
    import core.session_manager as managers
    import routes.chat_routes as chats
    import routes.history_routes as histories
    import routes.session_routes as sessions
    import src.endpoint_resolver as endpoints
    import src.settings as settings
    from src import agent_runs

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    for module in (database, managers, sessions, chats, histories):
        monkeypatch.setattr(module, "SessionLocal", factory)
    # Earlier source-helper tests can reimport session_routes after Chat/history
    # cached its functions. Use the current real helpers with this fixture's DB,
    # rather than stale module globals or an owner-check substitute.
    monkeypatch.setattr(chats, "_verify_session_owner", sessions._verify_session_owner)
    monkeypatch.setattr(chats, "_resolve_session_workspace", sessions._resolve_session_workspace)
    monkeypatch.setattr(histories, "_verify_session_owner", sessions._verify_session_owner)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setattr(sessions, "router", APIRouter(prefix="/api"))
    monkeypatch.setattr(agent_runs, "_RUNS", {})
    manager = managers.SessionManager()
    monkeypatch.setattr(models, "_session_manager", manager)
    manager.create_session("owned", "Counter", "http://model.invalid/v1", "synthetic", owner="admin")
    manager.create_session("foreign", "Other", "http://model.invalid/v1", "synthetic", owner="otheradmin")

    # Model/context services are outside this regression seam. Real owner/admin
    # gates, ORM persistence, HTTP form parsing and native tool cwd stay active.
    calls = []
    hooks = []

    async def context(sess, request, *args, **kwargs):
        return SimpleNamespace(
            user=request.state.current_user, uprefs={}, messages=[{"role": "user", "content": "Fix counter"}],
            preset=SimpleNamespace(temperature=0, max_tokens=100, reasoning_effort="", character_name=None),
            preprocessed=SimpleNamespace(attachment_meta=[]), auto_opened_docs=[],
            web_sources=[], rag_sources=[], used_memories=[], context_length=4096, was_compacted=False,
        )

    async def agent(*args, workspace=None, **kwargs):
        from src.tool_execution import _direct_fallback
        for hook in hooks:
            await hook()
        result = await _direct_fallback("python", "import os; print(os.getcwd())", workspace=workspace) if workspace else None
        calls.append({"workspace": workspace, "cwd": result["output"].strip() if result else None})
        yield 'data: {"delta":"Counter fixed."}\n\n'
        yield "data: " + json.dumps({"type": "metrics", "data": {
            "model": "synthetic", "workspace": "model-cannot-override-authority",
            "tool_events": [{"tool": "python", "command": "cwd probe", "exit_code": 0}],
        }}) + "\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(chats, "build_chat_context", context)
    monkeypatch.setattr(chats, "stream_agent_loop", agent)
    monkeypatch.setattr(chats, "_clear_orphaned_session_endpoint", lambda *a, **k: False)
    monkeypatch.setattr(chats, "_recover_empty_session_model", lambda *a, **k: False)
    monkeypatch.setattr(chats, "resolve_session_auth", lambda *a, **k: None)
    monkeypatch.setattr(chats, "run_post_response_tasks", lambda *a, **k: None)
    monkeypatch.setattr(chats, "_is_image_generation_session", lambda *a, **k: False)
    monkeypatch.setattr(endpoints, "resolve_chat_fallback_candidates", lambda *a, **k: [])
    monkeypatch.setattr(settings, "get_setting", lambda key, default=None: default)

    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(
        is_admin=lambda user: user in {"admin", "otheradmin"},
        get_privileges=lambda user: {},
    )

    @app.middleware("http")
    async def principal(request, call_next):
        request.state.current_user = request.headers.get("x-test-user", "admin")
        if request.headers.get("x-test-bearer"):
            request.state.api_token = True
            request.state.api_token_owner = "admin"
            request.state.current_user = "api"
        return await call_next(request)

    session_router = sessions.setup_session_routes(manager, {})
    history_router = histories.setup_history_routes(manager)
    # Match app.py: sessions, chat, then the duplicate history router.
    app.include_router(session_router)
    app.include_router(chats.setup_chat_routes(manager, None, None, None, None, None))
    app.include_router(history_router)
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, app=app, manager=manager, calls=calls, hooks=hooks,
            factory=factory, project=tmp_path, managers=managers,
            session_router=session_router, history_router=history_router,
        )
    engine.dispose()


def _chat(api, **fields):
    return api.client.post("/api/chat_stream", data={
        "session": "owned", "message": "Fix the counter", "mode": "agent", "allow_bash": "true", **fields,
    })


def test_workspace_patch_persists_canonical_root_and_all_owned_reads(workspace_api, tmp_path):
    api = workspace_api
    project = tmp_path / "app"
    project.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(project, target_is_directory=True)
    response = api.client.patch("/api/session/owned", data={"project_root": str(link)})
    assert response.status_code == 200
    assert response.json() == {"id": "owned", "project_root": str(project)}
    assert api.managers.SessionManager().get_session("owned").project_root == str(project)
    assert api.client.get("/api/session/owned/workspace").json()["project_root"] == str(project)
    assert api.client.get("/api/history/owned").json()["project_root"] == str(project)
    owned = next(s for s in api.client.get("/api/sessions").json() if s["id"] == "owned")
    assert owned["project_root"] == str(project)


def test_empty_patch_clears_but_omitted_field_does_not(workspace_api):
    api = workspace_api
    api.manager.set_session_project_root("owned", str(api.project))
    assert api.client.patch("/api/session/owned", data={"name": "Renamed"}).status_code == 200
    assert api.manager.get_session("owned").project_root == str(api.project)
    response = api.client.patch("/api/session/owned", data={"project_root": ""})
    assert response.status_code == 200 and response.json()["project_root"] is None
    assert api.managers.SessionManager().get_session("owned").project_root is None


@pytest.mark.parametrize("headers", [{"x-test-user": "regular"}, {"x-test-bearer": "1"}])
def test_workspace_update_requires_admin_browser(workspace_api, headers):
    api = workspace_api
    if headers.get("x-test-user"):
        api.manager.create_session("regular-owned", "Regular", "", "synthetic", owner="regular")
        sid = "regular-owned"
    else:
        sid = "owned"
    response = api.client.patch(f"/api/session/{sid}", data={"project_root": str(api.project)}, headers=headers)
    assert response.status_code == 403
    assert api.manager.get_session(sid).project_root is None


def test_workspace_update_and_detail_do_not_cross_owners(workspace_api):
    api = workspace_api
    assert api.client.patch("/api/session/foreign", data={"project_root": str(api.project)}).status_code == 404
    assert api.client.get("/api/session/foreign/workspace").status_code == 404
    assert api.client.get("/api/history/foreign").status_code == 404
    assert api.client.post("/api/chat_stream", data={
        "session": "foreign", "message": "Fix the counter", "mode": "agent",
        "workspace": str(api.project),
    }).status_code == 404
    assert api.calls == []
    assert api.manager.get_session("foreign").project_root is None


@pytest.mark.parametrize("kind", ["missing", "file", "sensitive", "malformed", "overlong"])
def test_invalid_workspace_refuses_without_losing_prior_root(workspace_api, kind):
    from src.constants import MAX_WORKSPACE_PATH_LENGTH
    api = workspace_api
    api.manager.set_session_project_root("owned", str(api.project))
    candidate = api.project / {"missing": "gone", "file": "plain.txt", "sensitive": ".ssh"}.get(kind, "invalid")
    if kind == "file":
        candidate.write_text("x")
    elif kind == "sensitive":
        candidate.mkdir()
    value = str(candidate)
    if kind == "malformed":
        value += "\0"
    elif kind == "overlong":
        value = "/" + "x" * MAX_WORKSPACE_PATH_LENGTH
    response = api.client.patch("/api/session/owned", data={"project_root": value})
    assert response.status_code == 400
    assert api.manager.get_session("owned").project_root == str(api.project)


def test_active_run_refuses_workspace_change_and_clear(workspace_api, monkeypatch):
    from src import agent_runs
    api = workspace_api
    api.manager.set_session_project_root("owned", str(api.project))
    monkeypatch.setattr(agent_runs, "is_active", lambda sid: sid == "owned")
    for path in ("", str(api.project)):
        assert api.client.patch("/api/session/owned", data={"project_root": path}).status_code == 409
    assert api.manager.get_session("owned").project_root == str(api.project)


def test_native_chat_uses_canonical_root_and_persists_turn_identity(workspace_api):
    api = workspace_api
    api.manager.set_session_project_root("owned", str(api.project))
    response = _chat(api)
    assert response.status_code == 200
    assert api.calls == [{"workspace": str(api.project), "cwd": str(api.project)}]
    history = api.managers.SessionManager().get_session("owned").history
    assert history[-1].metadata["workspace"] == str(api.project)
    assert history[-1].metadata["tool_events"][0]["exit_code"] == 0


def test_legacy_chat_workspace_sets_same_owned_record(workspace_api):
    api = workspace_api
    response = _chat(api, workspace=str(api.project))
    assert response.status_code == 200
    assert api.manager.get_session("owned").project_root == str(api.project)
    assert api.calls[-1]["cwd"] == str(api.project)


def test_explicit_chat_clear_is_persisted_and_not_replaced_by_old_root(workspace_api):
    api = workspace_api
    api.manager.set_session_project_root("owned", str(api.project))
    assert _chat(api, workspace="").status_code == 200
    assert api.calls[-1]["workspace"] is None
    assert api.manager.get_session("owned").project_root is None
    assert api.manager.get_session("owned").history[-1].metadata["workspace"] is None


def test_deleted_persisted_workspace_refuses_before_model_work(workspace_api):
    api = workspace_api
    api.manager.set_session_project_root("owned", str(api.project / "gone"))
    assert _chat(api).status_code == 400
    assert api.calls == []
    assert api.manager.get_session("owned").project_root == str(api.project / "gone")


def test_invalid_legacy_workspace_does_not_fall_back_or_spend_tokens(workspace_api):
    api = workspace_api
    api.manager.set_session_project_root("owned", str(api.project))
    assert _chat(api, workspace=str(api.project / "gone")).status_code == 400
    assert api.calls == []
    assert api.manager.get_session("owned").project_root == str(api.project)


def test_both_history_route_orders_expose_the_same_project(workspace_api):
    api = workspace_api
    api.manager.set_session_project_root("owned", str(api.project))
    assert api.client.get("/api/history/owned").json()["project_root"] == str(api.project)
    # History-first consumers must also return the canonical field, preserving
    # their existing rich history response rather than relying on shadowing.
    alternate = FastAPI()
    alternate.middleware("http")(api.app.user_middleware[0].kwargs["dispatch"])
    alternate.include_router(api.history_router)
    alternate.include_router(api.session_router)
    with TestClient(alternate) as client:
        response = client.get("/api/history/owned")
    assert response.status_code == 200
    assert response.json()["project_root"] == str(api.project)
    assert response.json()["model"] == "synthetic"


def test_actual_active_turn_refuses_change_and_keeps_historical_workspace(workspace_api):
    import httpx
    api = workspace_api
    api.manager.set_session_project_root("owned", str(api.project))
    other = api.project / "other"
    other.mkdir()
    statuses = []

    async def during_turn():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://testserver") as client:
            response = await client.patch("/api/session/owned", data={"project_root": str(other)})
            statuses.append(response.status_code)

    api.hooks.append(during_turn)
    assert _chat(api).status_code == 200
    assert statuses == [409]
    assert api.calls[-1]["cwd"] == str(api.project)
    assert api.client.patch("/api/session/owned", data={"project_root": str(other)}).status_code == 200
    reloaded = api.managers.SessionManager().get_session("owned")
    assert reloaded.project_root == str(other)
    assert reloaded.history[-1].metadata["workspace"] == str(api.project)


def test_legacy_workspace_update_uses_same_admin_and_active_run_gates(workspace_api, monkeypatch):
    from src import agent_runs
    api = workspace_api
    response = api.client.post("/api/chat_stream", data={
        "session": "owned", "message": "Fix", "mode": "agent", "workspace": str(api.project),
    }, headers={"x-test-bearer": "1"})
    assert response.status_code == 403
    monkeypatch.setattr(agent_runs, "is_active", lambda sid: True)
    assert _chat(api, workspace=str(api.project)).status_code == 409
    assert api.calls == []
    assert api.manager.get_session("owned").project_root is None


def test_intentional_single_user_mode_can_set_workspace(workspace_api, monkeypatch):
    api = workspace_api
    monkeypatch.setenv("AUTH_ENABLED", "false")
    response = api.client.patch("/api/session/owned", data={"project_root": str(api.project)}, headers={"x-test-user": ""})
    assert response.status_code == 200
    assert api.managers.SessionManager().get_session("owned").project_root == str(api.project)
