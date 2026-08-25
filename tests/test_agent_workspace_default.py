"""Regression tests for the agent's default scratch workspace.

A live run created ``/app/data/fizzbuzz_work`` — inside the app's own state
directory, alongside app.db / memory.json / settings.json / sessions/ / skills/
— as its scratch space, *after explicitly considering and rejecting /tmp*. It
wasn't being careless: ``_AGENT_WORKDIR`` was DATA_DIR itself, so cwd and HOME
were the state directory and nothing in the prompt said otherwise.
"""


import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.constants import DATA_DIR  # noqa: E402
from src import tool_execution  # noqa: E402
from src import agent_loop  # noqa: E402


# ── Workspace ─────────────────────────────────────────────────────────────

def test_agent_workdir_is_not_the_live_state_directory():
    data_root = os.path.realpath(DATA_DIR)
    workspace = os.path.realpath(tool_execution.agent_workspace_path())
    assert workspace != data_root
    assert workspace.startswith(data_root + os.sep)


def test_workspace_is_created_on_demand(tmp_path, monkeypatch):
    target = tmp_path / "agent-scratch"
    monkeypatch.setattr(
        tool_execution, "agent_workspace_path", lambda: str(target),
    )
    assert not target.exists()
    assert tool_execution.agent_workspace_dir() == str(target)
    assert target.is_dir()


def test_live_state_files_are_write_protected():
    """The data directory stays readable (uploads, documents) but the app's own
    state inside it is off limits to the file tools."""
    data_root = os.path.realpath(DATA_DIR)
    for name in ("app.db", "settings.json", "memory.json", "auth.json",
                 ".app_key", "user_prefs.json"):
        assert tool_execution._is_sensitive_path(os.path.join(data_root, name)), name
    # Backups/journals of the same files too.
    assert tool_execution._is_sensitive_path(
        os.path.join(data_root, "app.db.bak-20260823-single-resident"))
    # And the state directories.
    for d in ("sessions", "skills"):
        assert tool_execution._is_sensitive_path(os.path.join(data_root, d, "x.json")), d


def test_the_agents_own_workspace_is_not_protected():
    workspace = os.path.realpath(tool_execution.agent_workspace_path())
    assert not tool_execution._is_sensitive_path(os.path.join(workspace, "fizzbuzz.py"))
    assert not tool_execution._is_sensitive_path(
        os.path.join(workspace, "fizzbuzz_work", "main.py"))


def test_uploads_stay_readable():
    data_root = os.path.realpath(DATA_DIR)
    assert not tool_execution._is_sensitive_path(
        os.path.join(data_root, "uploads", "2026-08-24", "resume.docx"))


def test_prompt_names_the_workspace_and_warns_off_app_state():
    directive = agent_loop._workspace_directive()
    assert tool_execution.agent_workspace_path() in directive
    lowered = directive.lower()
    assert "live state" in lowered
    assert "never create scratch files" in lowered
