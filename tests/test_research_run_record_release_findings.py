"""Regression tests for the review findings on the research run record release.

Owner-scoped routing-profile pricing, unique blob temp files, cached
completions not charged, orphan blob cleanup after a delete, and the depth
timeout kept by the DeepResearcher failure fallback.
"""
import asyncio
import hashlib
import json
import os
import time
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy
from sqlalchemy.orm import sessionmaker

import core.database as cdb
from src import llm_core, research_run_record
from src.deep_research import DeepResearcher
from src.research_handler import ResearchHandler
from src.research_run_record import ResearchRunRecorder, sweep_orphan_blobs


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def routing_db(monkeypatch):
    engine = sqlalchemy.create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool)
    cdb.Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    return factory


# --- routing-profile lookup is scoped to the research owner -------------------

ENDPOINT = "http://local.test/v1/chat/completions"


def _profile(db, ep_id, owner, price=1_000_000.0):
    db.add(cdb.ModelEndpoint(id=ep_id, name=ep_id, base_url="http://local.test/v1", owner=owner))
    db.add(cdb.RoutingModelProfile(id=f"p-{ep_id}", model_endpoint_id=ep_id, model="m", roles="[]",
                                   input_cost_per_mtok=price, output_cost_per_mtok=0.0))


def _persist(factory, owner):
    recorder = ResearchRunRecorder()
    recorder.add_llm_call(purpose="extract", model="m", endpoint=ENDPOINT,
                          started_at="2026-10-19T00:00:00+00:00", latency_ms=5,
                          input_tokens=2, output_tokens=0, tokens_estimated=False)
    run_id = recorder.persist_model_runs(query="q", owner=owner, status="done")
    db = factory()
    try:
        rows = db.query(cdb.RoutingModelRun).filter(cdb.RoutingModelRun.run_id == run_id).all()
        return db.get(cdb.RoutingRun, run_id).spend_total_usd, [r.model_profile_id for r in rows]
    finally:
        db.close()


def test_another_owners_profile_is_never_attributed(routing_db):
    db = routing_db()
    _profile(db, "bob-ep", owner="bob")
    db.commit()
    db.close()

    assert _persist(routing_db, owner="alice") == (0.0, [None])


def test_another_owners_profile_does_not_make_the_price_ambiguous(routing_db):
    db = routing_db()
    _profile(db, "bob-ep", owner="bob", price=9_000_000.0)
    _profile(db, "shared-ep", owner=None)
    db.commit()
    db.close()

    assert _persist(routing_db, owner="alice") == (2.0, ["p-shared-ep"])


def test_owned_profile_beats_a_shared_one(routing_db):
    db = routing_db()
    _profile(db, "alice-ep", owner="alice", price=3_000_000.0)
    _profile(db, "shared-ep", owner=None)
    db.commit()
    db.close()

    assert _persist(routing_db, owner="alice") == (6.0, ["p-alice-ep"])


def test_two_shared_profiles_stay_ambiguous(routing_db):
    db = routing_db()
    _profile(db, "shared-a", owner=None)
    _profile(db, "shared-b", owner=None)
    db.commit()
    db.close()

    assert _persist(routing_db, owner="alice") == (0.0, [None])


# --- blob writes ---------------------------------------------------------------

def test_concurrent_identical_blob_writes_both_succeed(tmp_path, monkeypatch):
    """The other job finishes writing and renames before this one does."""
    blobs = tmp_path / "blobs"
    text = "same page text"
    real_replace = os.replace
    recorders = [ResearchRunRecorder(run_id=f"run-{i}") for i in range(2)]
    for r in recorders:
        r.add_source("https://example.test/a", "A", text)
    first, second = recorders
    state = {"nested": False}

    def racing_replace(src, dst):
        if not state["nested"]:
            state["nested"] = True
            # the rival writer runs to completion inside our write/rename gap
            rival = second.build(blob_dir=blobs)["sources"][0]
            assert rival["blob"] is not None
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", racing_replace)
    [src] = first.build(blob_dir=blobs)["sources"]

    assert src["blob"] == f"blobs/{_sha(text)}.txt"
    assert (blobs / f"{_sha(text)}.txt").read_text(encoding="utf-8") == text
    assert [p.name for p in blobs.iterdir()] == [f"{_sha(text)}.txt"]  # no stray temp files


def test_failed_blob_write_leaves_no_temp_file(tmp_path, monkeypatch):
    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    recorder = ResearchRunRecorder(run_id="run-1")
    recorder.add_source("https://example.test/a", "A", "text")

    [src] = recorder.build(blob_dir=tmp_path / "blobs")["sources"]

    assert src["blob"] is None and src["blob_missing_reason"] == "write_failed"
    assert list((tmp_path / "blobs").iterdir()) == []


# --- a cached completion is not model spend ------------------------------------

@pytest.fixture
def live_cache(monkeypatch):
    posts = []
    cache = {}

    class _Client:
        async def post(self, *a, **kw):
            posts.append(1)
            req = httpx.Request("POST", ENDPOINT)
            return httpx.Response(200, request=req, json={
                "choices": [{"message": {"content": "an answer"}}]})

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _Client())
    monkeypatch.setattr(llm_core, "_get_cached_response", cache.get)
    monkeypatch.setattr(llm_core, "_set_cached_response", cache.__setitem__)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **kw: None)
    return posts


@pytest.mark.asyncio
async def test_cache_hit_is_recorded_with_zero_tokens_and_zero_spend(live_cache, routing_db):
    db = routing_db()
    _profile(db, "alice-ep", owner="alice")
    db.commit()
    db.close()
    researcher = DeepResearcher(llm_endpoint=ENDPOINT, llm_model="m", run_record=True)
    messages = [{"role": "user", "content": "plan the research please " * 20}]

    await researcher._llm(messages)
    await researcher._llm(messages)  # identical prompt: served from the process cache

    assert len(live_cache) == 1  # one provider request
    first, second = researcher.run_recorder.llm_calls
    assert first["input_tokens"] > 0 and first.get("cached") is None
    assert (second["input_tokens"], second["output_tokens"]) == (0, 0)
    assert second["cached"] is True and second["tokens_estimated"] is False

    run_id = researcher.run_recorder.persist_model_runs(query="q", owner="alice", status="done")
    db = routing_db()
    try:
        rows = db.query(cdb.RoutingModelRun).filter(cdb.RoutingModelRun.run_id == run_id).all()
        cost = {json.loads(r.notes)["cost_basis"]: r.cost_usd for r in rows}
    finally:
        db.close()
    assert cost["cache_hit"] == 0.0 and cost["routing_profile_rate"] > 0.0


# --- deleting research reclaims its blobs --------------------------------------

def _session(root, session_id, *blob_names):
    record = {"sources": [{"source_id": f"S{i}", "blob": f"blobs/{n}"}
                          for i, n in enumerate(blob_names, 1)]}
    (root / f"{session_id}.json").write_text(json.dumps({"run_record": record}), encoding="utf-8")


def _blob(root, name, age=7200):
    (root / "blobs").mkdir(exist_ok=True)
    path = root / "blobs" / name
    path.write_text("text", encoding="utf-8")
    old = time.time() - age
    os.utime(path, (old, old))
    return path


@pytest.fixture
def research_dir(tmp_path, monkeypatch):
    root = tmp_path / "deep_research"
    root.mkdir()
    monkeypatch.setattr(research_run_record, "DEEP_RESEARCH_BLOB_DIR", str(root / "blobs"))
    return root


def test_sweep_keeps_blobs_any_remaining_session_references(research_dir):
    shared, orphan = _blob(research_dir, "a.txt"), _blob(research_dir, "b.txt")
    _session(research_dir, "rp-keep", "a.txt")

    assert sweep_orphan_blobs(research_dir) == 1
    assert shared.exists() and not orphan.exists()


def test_sweep_spares_fresh_blobs_and_removes_stale_temp_files(research_dir):
    fresh = _blob(research_dir, "fresh.txt", age=5)
    stale_tmp = _blob(research_dir, ".abc.1.dead.tmp")

    sweep_orphan_blobs(research_dir)

    assert fresh.exists() and not stale_tmp.exists()


def test_sweep_does_nothing_when_a_session_file_is_unreadable(research_dir):
    orphan = _blob(research_dir, "b.txt")
    (research_dir / "rp-broken.json").write_text("{not json", encoding="utf-8")

    assert sweep_orphan_blobs(research_dir) == 0
    assert orphan.exists()


def test_reusing_a_blob_refreshes_its_age(research_dir):
    path = _blob(research_dir, f"{_sha('page')}.txt")
    recorder = ResearchRunRecorder(run_id="run-1")
    recorder.add_source("https://example.test/a", "A", "page")

    recorder.build(blob_dir=research_dir / "blobs")

    assert time.time() - path.stat().st_mtime < 60
    assert sweep_orphan_blobs(research_dir) == 0


def test_delete_route_reclaims_the_deleted_sessions_blobs(research_dir, monkeypatch):
    from routes.research_routes import setup_research_routes

    monkeypatch.setattr("routes.research_routes.DEEP_RESEARCH_DIR", str(research_dir))
    kept, gone = _blob(research_dir, "kept.txt"), _blob(research_dir, "gone.txt")
    _session(research_dir, "rp-keep", "kept.txt")
    data = {"owner": "alice", "run_record": {"sources": [{"source_id": "S1", "blob": "blobs/gone.txt"}]}}
    (research_dir / "rp-gone.json").write_text(json.dumps(data), encoding="utf-8")
    router = setup_research_routes(SimpleNamespace(_active_tasks={}))
    delete = next(r.endpoint for r in router.routes
                  if r.path == "/api/research/{session_id}" and "DELETE" in r.methods)

    result = asyncio.run(delete("rp-gone", SimpleNamespace(state=SimpleNamespace(current_user="alice"))))

    assert result == {"deleted": True}
    assert kept.exists() and not gone.exists()


async def _manage(content):
    from src.tool_implementations import do_manage_research
    return await do_manage_research(content)


def test_manage_research_delete_reclaims_blobs(research_dir, monkeypatch):
    monkeypatch.setattr("src.tool_implementations.DEEP_RESEARCH_DIR", str(research_dir))
    gone = _blob(research_dir, "gone.txt")
    _session(research_dir, "rp-gone", "gone.txt")

    result = asyncio.run(_manage(json.dumps({"action": "delete", "id": "rp-gone"})))

    assert result["exit_code"] == 0 and not gone.exists()


# --- DeepResearcher failure fallback keeps the depth timeout -------------------

@pytest.mark.parametrize("depth, explicit, expected", [
    ("quick", None, 90), ("critical", None, 600), ("quick", 200, 200), (None, None, 300),
])
def test_failure_fallback_uses_the_resolved_timeout(monkeypatch, depth, explicit, expected):
    calls = []

    class _FailingResearcher:
        def __init__(self, **kw):
            pass

        async def research(self, *a, **kw):
            raise RuntimeError("primary failed")

    legacy = SimpleNamespace(start_research=lambda query, max_time: calls.append(max_time) or "legacy report")
    handler = ResearchHandler.__new__(ResearchHandler)
    handler._active_tasks = {}
    handler._legacy_engine = legacy
    handler._get_legacy_stats = lambda: {}

    async def no_probe(*a, **kw):
        return None

    handler._probe_endpoint = no_probe
    monkeypatch.setattr("src.deep_research.DeepResearcher", _FailingResearcher)

    asyncio.run(handler.call_research_service(
        "q", ENDPOINT, "m", max_time=explicit, depth=depth))

    assert calls == [expected]
