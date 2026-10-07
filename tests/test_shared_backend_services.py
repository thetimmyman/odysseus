"""HTTP and background consumers share state without loading their adapters."""
import asyncio
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.contacts_routes import setup_contacts_routes
from routes.note_routes import setup_note_routes
from routes.skills_routes import setup_skills_routes
from src import contacts, reminders, skill_audit
from src.builtin_actions import TaskNoop, action_audit_skills
from src.tool_implementations import do_manage_contact


def test_agent_contact_write_is_visible_to_http_and_shared_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(contacts, "LOCAL_CONTACTS_FILE", tmp_path / "contacts.json")
    monkeypatch.setattr(contacts, "_get_carddav_config", lambda: {"url": ""})
    monkeypatch.setattr(contacts, "_contact_cache", {"contacts": [], "fetched_at": None})
    result = asyncio.run(do_manage_contact('{"action":"add","name":"Ada","email":"ada@example.test"}', owner="admin"))
    assert result["exit_code"] == 0
    app = FastAPI()
    app.include_router(setup_contacts_routes())
    from core.middleware import require_admin
    app.dependency_overrides[require_admin] = lambda: "admin"
    with TestClient(app) as client:
        rows = client.get("/api/contacts/list").json()["contacts"]
        assert len(rows) == 1
        assert rows[0]["emails"] == ["ada@example.test"]
        response = client.put('/api/contacts/' + rows[0]['uid'], json={"name":"Ada Updated", "emails":["ada@example.test"]})
        assert response.json()["success"] is True
    assert contacts._fetch_contacts()[0]["name"] == "Ada Updated"


def test_reminder_scheduler_binding_and_dedupe_are_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(reminders, "DATA_DIR", tmp_path)
    monkeypatch.setattr(reminders, "_scheduler_ref", None)
    scheduler = SimpleNamespace(add_notification=Mock())
    setup_note_routes(scheduler)
    async def dispatch(owner):
        return await reminders.dispatch_reminder("Due", "Body", "same-note", owner=owner,
            settings_override={"reminder_channel":"browser", "reminder_llm_synthesis":False})
    assert asyncio.run(dispatch("alice"))["browser_sent"]
    assert asyncio.run(dispatch("bob"))["browser_sent"]
    assert asyncio.run(dispatch("alice"))["skipped"]
    assert [c.kwargs["owner"] for c in scheduler.add_notification.call_args_list] == ["alice", "bob"]
    assert (tmp_path / "note_pings_alice.json").is_file()
    assert (tmp_path / "note_pings_bob.json").is_file()


def test_http_and_scheduled_audit_share_jobs_without_cross_owner_cancellation(monkeypatch):
    alice = {"status":"running", "total":1, "done":0, "current":"a", "results":[], "log":[]}
    bob = {"status":"running", "total":1, "done":0, "current":"b", "results":[], "log":[]}
    monkeypatch.setattr(skill_audit, "_skill_audit_jobs", {("alice",):alice, ("bob",):bob})
    with pytest.raises(TaskNoop, match="already running"):
        asyncio.run(action_audit_skills("alice"))
    app = FastAPI()
    @app.middleware("http")
    async def identity(request, call_next):
        request.state.current_user = "alice"
        return await call_next(request)
    app.include_router(setup_skills_routes(Mock()))
    with TestClient(app) as client:
        assert client.get("/api/skills/audit-all/status").json()["current"] == "a"
        assert client.post("/api/skills/audit-all/cancel").json()["status"] == "cancelled"
    assert alice["cancel"] is True
    assert bob["status"] == "running"
    assert "cancel" not in bob


def test_audit_worker_loads_and_updates_only_the_requested_owner(monkeypatch):
    job = {"status":"running", "done":0, "results":[], "log":[]}
    monkeypatch.setattr(skill_audit, "_skill_audit_jobs", {("alice",):job})
    manager = Mock()
    manager.load.return_value = [{"name":"a", "owner":"alice"}]
    calls = []
    async def audit(sm, skill, url, model, headers, teacher, owner, log):
        calls.append((sm, skill["name"], owner))
        return {"skill":skill["name"], "result":"pass"}
    monkeypatch.setattr(skill_audit, "_audit_one_skill", audit)
    asyncio.run(skill_audit._run_audit_all_job(("alice",), manager, ["a", "absent"], "url", "model", {}, None, "alice"))
    assert calls == [(manager, "a", "alice")]
    assert all(c.kwargs == {"owner":"alice"} for c in manager.load.call_args_list)
    assert job["status"] == "done" and job["done"] == 1


def test_shared_backends_do_not_import_their_http_adapters():
    root = Path(__file__).resolve().parents[1]
    blocked = {"routes.skills_routes", "routes.note_routes", "routes.contacts_routes"}
    for rel in ["src/skill_audit.py", "src/reminders.py", "src/contacts.py", "src/builtin_actions.py", "src/tool_implementations.py"]:
        for node in ast.walk(ast.parse((root / rel).read_text())):
            if isinstance(node, ast.ImportFrom):
                modules = {node.module} | {f"{node.module}.{a.name}" for a in node.names}
                assert not modules & blocked, rel
            elif isinstance(node, ast.Import):
                assert not {a.name for a in node.names} & blocked, rel
