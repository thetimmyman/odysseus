"""Regression tests for research terminal-state persistence (defects B & C).

Defect C: the bare-error branch (no partial report) set status="error" in
memory but never called _save_result(), so the failure was invisible after
a restart and never appeared in history.

Defect B: a run interrupted by a restart left no trace at all — the entry
lived only in _active_tasks, so after a restart get_status() returned 404
and the run silently vanished.

Fix: start_research() writes a lightweight "running" marker file
immediately; every terminal branch (done / error / timeout-without-
partial) persists via _save_result(); and handler construction sweeps any
"running" marker left by a previous process to a terminal error state.
No fake-resume: the LLM task context is gone after a restart.
"""
import asyncio
import json

from src import research_handler
from src.research_handler import ResearchHandler


def _bare_handler(tmp_path, monkeypatch):
    """Handler without __init__'s engine wiring; data dir pinned to tmp."""
    monkeypatch.setattr(research_handler, "RESEARCH_DATA_DIR", tmp_path)
    handler = ResearchHandler.__new__(ResearchHandler)
    handler._active_tasks = {}
    handler._legacy_engine = None
    return handler



def test_bare_error_is_persisted(tmp_path, monkeypatch):
    """Defect C: exception with no partial report must still hit disk."""
    handler = _bare_handler(tmp_path, monkeypatch)

    async def boom(self, *a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(ResearchHandler, "call_research_service", boom)

    async def go():
        handler.start_research("rp-err1", "why is the sky blue", "http://x", "m", hard_timeout=5)
        await handler._active_tasks["rp-err1"]["task"]

    asyncio.run(go())

    data = json.loads((tmp_path / "rp-err1.json").read_text(encoding="utf-8"))
    assert data["status"] == "error"
    assert "kaboom" in data["result"]


def test_timeout_without_partial_is_persisted(tmp_path, monkeypatch):
    """Hard timeout with no evolving_report must still hit disk as error."""
    handler = _bare_handler(tmp_path, monkeypatch)

    async def slow(self, *a, **k):
        await asyncio.sleep(5)
        return "never reached"

    monkeypatch.setattr(ResearchHandler, "call_research_service", slow)

    async def go():
        handler.start_research("rp-to1", "slow question", "http://x", "m", hard_timeout=1)
        await handler._active_tasks["rp-to1"]["task"]

    asyncio.run(go())

    data = json.loads((tmp_path / "rp-to1.json").read_text(encoding="utf-8"))
    assert data["status"] == "error"
    assert "timed out" in data["result"]


def test_running_marker_written_at_start(tmp_path, monkeypatch):
    """Defect B: the marker exists the moment start_research returns."""
    handler = _bare_handler(tmp_path, monkeypatch)
    release = asyncio.Event()

    async def blocked(self, *a, **k):
        await release.wait()
        return "finished at last"

    monkeypatch.setattr(ResearchHandler, "call_research_service", blocked)

    async def go():
        handler.start_research("rp-run1", "pending question", "http://x", "m", hard_timeout=30)
        marker = json.loads((tmp_path / "rp-run1.json").read_text(encoding="utf-8"))
        assert marker["status"] == "running"
        assert marker["query"] == "pending question"
        assert marker["completed_at"] is None
        release.set()
        await handler._active_tasks["rp-run1"]["task"]

    asyncio.run(go())

    final = json.loads((tmp_path / "rp-run1.json").read_text(encoding="utf-8"))
    assert final["status"] == "done"
    assert final["result"] == "finished at last"


def test_sweep_marks_orphaned_running_marker_as_error(tmp_path, monkeypatch):
    """A 'running' file from a dead process is swept to terminal error."""
    handler = _bare_handler(tmp_path, monkeypatch)
    (tmp_path / "rp-orphan.json").write_text(
        json.dumps({"query": "lost run", "status": "running", "owner": "u1", "started_at": 1.0}),
        encoding="utf-8",
    )

    swept = handler._sweep_interrupted_runs()

    assert swept == 1
    data = json.loads((tmp_path / "rp-orphan.json").read_text(encoding="utf-8"))
    assert data["status"] == "error"
    assert "restart" in data["result"]
    assert data["completed_at"] is not None


def test_sweep_leaves_terminal_files_untouched(tmp_path, monkeypatch):
    handler = _bare_handler(tmp_path, monkeypatch)
    done = {"query": "ok", "status": "done", "result": "fine", "owner": "u1"}
    (tmp_path / "rp-done.json").write_text(json.dumps(done), encoding="utf-8")

    swept = handler._sweep_interrupted_runs()

    assert swept == 0
    assert json.loads((tmp_path / "rp-done.json").read_text(encoding="utf-8")) == done


def test_sweep_survives_corrupt_files(tmp_path, monkeypatch):
    handler = _bare_handler(tmp_path, monkeypatch)
    (tmp_path / "rp-corrupt.json").write_text("{not json", encoding="utf-8")

    assert handler._sweep_interrupted_runs() == 0


def test_restart_get_status_reports_swept_error(tmp_path, monkeypatch):
    """After sweep, get_status() reports the orphan as error, not 404."""
    handler = _bare_handler(tmp_path, monkeypatch)
    (tmp_path / "rp-orphan2.json").write_text(
        json.dumps({"query": "lost", "status": "running", "owner": "u1", "started_at": 1.0}),
        encoding="utf-8",
    )
    handler._sweep_interrupted_runs()

    status = handler.get_status("rp-orphan2")

    assert status is not None
    assert status["status"] == "error"
