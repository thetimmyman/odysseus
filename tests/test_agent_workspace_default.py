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


def test_background_bash_launches_in_the_workspace(monkeypatch):
    """`#!bg` bash launches from execute_tool_block, a different function than
    the foreground path, so it needs its own workspace resolution — an earlier
    draft of this change referenced the foreground helper's local variable from
    here, which would have been a NameError the first time anyone backgrounded
    a command. No other test covers this path."""
    import asyncio
    import src.bg_jobs as bg_jobs
    import src.tool_execution as te

    launched = {}

    def _fake_launch(cmd, session_id=None, cwd=None):
        launched.update(cmd=cmd, session_id=session_id, cwd=cwd)
        return {"id": "job-1"}

    class _Block:
        tool_type = "bash"
        content = "#!bg\nsleep 1"

    # Patch the real module's attribute rather than sys.modules: the call site
    # does `from src import bg_jobs`, which resolves the already-imported
    # module object off the package once anything else has imported it.
    monkeypatch.setattr(bg_jobs, "launch", _fake_launch)
    monkeypatch.setattr(te, "is_public_blocked_tool", lambda *a, **kw: False)
    _desc, result = asyncio.run(te.execute_tool_block(_Block(), session_id="sess-1"))
    assert launched, "the #!bg branch never ran — the marker parse changed"
    assert launched["cwd"] == tool_execution.agent_workspace_dir()
    assert os.path.realpath(launched["cwd"]) != os.path.realpath(DATA_DIR)
    assert result["exit_code"] == 0
