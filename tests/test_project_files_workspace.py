"""Owned project explorer routes reuse the native workspace path policy."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


@pytest.fixture
def project_api(monkeypatch, tmp_path):
    import core.database as database
    import core.models as models
    import core.session_manager as managers
    import routes.session_routes as sessions
    from routes.project_files_routes import setup_project_files_routes

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    for module in (database, managers, sessions):
        monkeypatch.setattr(module, "SessionLocal", factory)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    manager = managers.SessionManager()
    monkeypatch.setattr(models, "_session_manager", manager)
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "project-sibling"
    outside.mkdir()
    (root / "summary.mjs").write_text("export const done = 2;\n")
    (root / "src").mkdir()
    (root / "src/note.txt").write_text("nested\n")
    (outside / "private.txt").write_text("outside unchanged\n")
    for sid, owner in (("owned", "admin"), ("foreign", "other")):
        manager.create_session(sid, sid, "", "synthetic", owner=owner)
        manager.set_session_project_root(sid, str(root))

    app = FastAPI()
    app.state.auth_manager = SimpleNamespace(get_privileges=lambda user: {"can_use_bash": user != "limited"})

    @app.middleware("http")
    async def principal(request, call_next):
        request.state.current_user = request.headers.get("x-test-user", "admin")
        if request.headers.get("x-test-bearer"):
            request.state.api_token = True
            request.state.api_token_owner = "admin"
            request.state.current_user = "api"
        return await call_next(request)

    app.include_router(setup_project_files_routes())
    with TestClient(app, raise_server_exceptions=False) as client:
        yield SimpleNamespace(client=client, root=root, outside=outside, manager=manager)
    engine.dispose()


def _read(api, path, sid="owned", headers=None):
    return api.client.get("/api/project-files/read", params={"session_id": sid, "path": str(path)}, headers=headers)


def _write(api, path, content="updated\n", sid="owned", headers=None):
    return api.client.post("/api/project-files/write", json={
        "session_id": sid, "path": str(path), "content": content,
    }, headers=headers)


def test_tree_lists_owned_root_and_relative_subdirectory(project_api):
    api = project_api
    response = api.client.get("/api/project-files/tree", params={"session_id": "owned"})
    assert response.status_code == 200
    assert response.json()["root"] == str(api.root)
    assert [entry["name"] for entry in response.json()["entries"]] == ["src", "summary.mjs"]
    nested = api.client.get("/api/project-files/tree", params={"session_id": "owned", "path": "src"})
    assert nested.status_code == 200
    assert nested.json()["dir"] == str(api.root / "src")
    assert nested.json()["parent"] == str(api.root)
    assert nested.json()["entries"][0]["path"] == str(api.root / "src/note.txt")


@pytest.mark.parametrize("absolute", [False, True])
def test_read_uses_the_saved_session_root(project_api, absolute):
    api = project_api
    path = api.root / "summary.mjs" if absolute else "summary.mjs"
    response = _read(api, path)
    assert response.status_code == 200
    assert response.json()["path"] == str(api.root / "summary.mjs")
    assert response.json()["content"] == "export const done = 2;\n"


def test_write_and_read_share_owned_root_without_temporary_leftovers(project_api):
    api = project_api
    response = _write(api, "src/new.txt", "one\ntwo\n")
    assert response.status_code == 200
    assert response.json()["path"] == str(api.root / "src/new.txt")
    assert (api.root / "src/new.txt").read_text() == "one\ntwo\n"
    assert _read(api, "src/new.txt").json()["content"] == "one\ntwo\n"
    assert list(api.root.rglob("*.odytmp")) == []


def test_tree_read_and_write_refuse_another_owner(project_api):
    api = project_api
    assert api.client.get("/api/project-files/tree", params={"session_id": "foreign"}).status_code == 404
    assert _read(api, "summary.mjs", sid="foreign").status_code == 404
    assert _write(api, "summary.mjs", sid="foreign").status_code == 404
    assert (api.root / "summary.mjs").read_text() == "export const done = 2;\n"


@pytest.mark.parametrize("headers", [{"x-test-user": "limited"}, {"x-test-bearer": "1"}])
def test_explorer_keeps_existing_privilege_and_bearer_refusal(project_api, headers):
    api = project_api
    assert api.client.get("/api/project-files/tree", params={"session_id": "owned"}, headers=headers).status_code == 403
    assert _read(api, "summary.mjs", headers=headers).status_code == 403
    assert _write(api, "summary.mjs", headers=headers).status_code == 403
    assert (api.root / "summary.mjs").read_text() == "export const done = 2;\n"


@pytest.mark.parametrize("absolute", [False, True])
def test_outside_paths_refuse_read_tree_and_write(project_api, absolute):
    api = project_api
    file = api.outside / "private.txt" if absolute else "../project-sibling/private.txt"
    directory = api.outside if absolute else "../project-sibling"
    assert _read(api, file).status_code == 403
    assert _write(api, file).status_code == 403
    assert api.client.get("/api/project-files/tree", params={"session_id": "owned", "path": str(directory)}).status_code == 403
    assert (api.outside / "private.txt").read_text() == "outside unchanged\n"


def test_symlink_escape_is_omitted_and_refused_for_all_operations(project_api):
    api = project_api
    (api.root / "escape").symlink_to(api.outside, target_is_directory=True)
    tree = api.client.get("/api/project-files/tree", params={"session_id": "owned"})
    assert tree.status_code == 200
    assert "escape" not in {entry["name"] for entry in tree.json()["entries"]}
    assert _read(api, "escape/private.txt").status_code == 403
    assert _write(api, "escape/private.txt").status_code == 403
    assert api.client.get("/api/project-files/tree", params={"session_id": "owned", "path": "escape"}).status_code == 403
    assert (api.outside / "private.txt").read_text() == "outside unchanged\n"


@pytest.mark.parametrize("path", [".env", ".ssh/key.txt"])
def test_sensitive_files_and_aliases_stay_hidden_and_refused(project_api, path):
    api = project_api
    sensitive = api.root / path
    sensitive.parent.mkdir(exist_ok=True)
    sensitive.write_text("synthetic protected value\n")
    (api.root / "alias.txt").symlink_to(sensitive)
    tree = api.client.get("/api/project-files/tree", params={"session_id": "owned"})
    assert tree.status_code == 200
    names = {entry["name"] for entry in tree.json()["entries"]}
    assert "alias.txt" not in names and ".env" not in names and ".ssh" not in names
    for candidate in (path, "alias.txt"):
        assert _read(api, candidate).status_code == 403
        assert _write(api, candidate).status_code == 403
    assert sensitive.read_text() == "synthetic protected value\n"


def test_missing_project_root_refuses_instead_of_using_default_paths(project_api):
    api = project_api
    api.manager.set_session_project_root("owned", None)
    assert api.client.get("/api/project-files/tree", params={"session_id": "owned"}).status_code == 404
    assert _read(api, "summary.mjs").status_code == 404
    assert _write(api, "summary.mjs").status_code == 404
