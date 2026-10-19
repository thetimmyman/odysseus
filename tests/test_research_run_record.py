"""Checkable deep-research run record (setting research_run_record).

Off: the saved session JSON and findings keep their old shape. On: every page
given to the extractor is recorded with the exact text (sha256 + blob), each
finding's verbatim evidence is tied to its source_id, every LLM call becomes a
RoutingModelRun row, and grounding_view() yields the checker's input shape.
"""
import asyncio
import hashlib
import json
import sys
import types

import httpx
import pytest
import sqlalchemy
from sqlalchemy.orm import sessionmaker

import core.database as cdb
from src import llm_core, research_handler, research_run_record
from src.deep_research import DeepResearcher
from src.research_handler import ResearchHandler
from src.research_run_record import ResearchRunRecorder, grounding_view

PAGE = (
    "Odysseus keeps research runs on disk.\n\n"
    "The extractor reads the page and returns evidence. "
    "Retrieval happens before extraction, and the hash covers the extracted text."
)
EVIDENCE = "Retrieval happens before extraction"
LEGACY_KEYS = {"query", "status", "result", "raw_report", "sources", "raw_findings",
               "stats", "category", "started_at", "completed_at", "owner"}


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture
def fake_web(monkeypatch):
    """Fake page fetch and an OpenAI-shaped LLM endpoint reporting usage."""
    search_mod = types.ModuleType("src.search")
    search_mod.fetch_webpage_content = lambda url, timeout: {
        "success": True, "content": PAGE, "title": "Page title"}
    monkeypatch.setitem(sys.modules, "src.search", search_mod)

    async def immediate_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)

    replies = []

    class _Client:
        async def post(self, *a, **kw):
            body = replies.pop(0)
            if isinstance(body, Exception):
                raise body
            req = httpx.Request("POST", "http://local.test/v1/chat/completions")
            return httpx.Response(200, request=req, json={
                "choices": [{"message": {"content": body}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 30}})

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _Client())
    monkeypatch.setattr(llm_core, "_get_cached_response", lambda key: None)
    monkeypatch.setattr(llm_core, "_set_cached_response", lambda key, value: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **kw: None)
    return replies


@pytest.fixture
def data_dirs(tmp_path, monkeypatch):
    data_dir = tmp_path / "deep_research"
    data_dir.mkdir()
    monkeypatch.setattr(research_handler, "RESEARCH_DATA_DIR", data_dir)
    monkeypatch.setattr(research_run_record, "DEEP_RESEARCH_BLOB_DIR", str(data_dir / "blobs"))
    return data_dir


@pytest.fixture
def routing_db(monkeypatch):
    engine = sqlalchemy.create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool)
    cdb.Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(cdb, "SessionLocal", factory)
    return factory


def _researcher(run_record):
    return DeepResearcher(llm_endpoint="http://local.test/v1/chat/completions",
                          llm_model="local-model", run_record=run_record)


def _extraction(evidence=EVIDENCE, summary="Retrieval precedes extraction."):
    return json.dumps({"rational": "relevant", "evidence": evidence, "summary": summary})


def _save(researcher, data_dir, session_id="rp-test1"):
    handler = ResearchHandler.__new__(ResearchHandler)
    handler._active_tasks = {}
    entry = {"query": "how are runs recorded?", "status": "done", "result": "report",
             "researcher": researcher, "started_at": 1.0, "owner": "alice"}
    handler._save_result(session_id, entry)
    return json.loads((data_dir / f"{session_id}.json").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_flag_off_keeps_old_format(fake_web, data_dirs):
    fake_web.append(_extraction())
    researcher = _researcher(run_record=False)

    finding = await researcher._fetch_and_extract("https://example.test/a", "q", "A")
    researcher.findings = [finding]
    saved = _save(researcher, data_dirs)

    assert researcher.run_recorder is None
    assert "source_id" not in finding
    assert set(saved) == LEGACY_KEYS
    assert saved["raw_findings"] == [{"url": "https://example.test/a", "title": "A",
                                      "summary": "Retrieval precedes extraction."}]
    assert not (data_dirs / "blobs").exists()


@pytest.mark.asyncio
async def test_flag_on_records_text_evidence_and_model_runs(fake_web, data_dirs, routing_db):
    fake_web.append(_extraction())
    researcher = _researcher(run_record=True)

    finding = await researcher._fetch_and_extract("https://example.test/a", "q", "A")
    researcher.findings = [finding]
    saved = _save(researcher, data_dirs)

    assert set(saved) == LEGACY_KEYS | {"run_record"}
    assert saved["raw_findings"][0]["source_id"] == "S1"
    assert saved["raw_findings"][0]["evidence"] == EVIDENCE

    record = saved["run_record"]
    [src] = record["sources"]
    assert src["source_id"] == "S1" and src["url"] == "https://example.test/a"
    assert src["content_sha256"] == _sha(PAGE)
    assert src["retrieved_at"].endswith("+00:00")
    assert (data_dirs / src["blob"]).read_text(encoding="utf-8") == PAGE
    assert record["claims"] == [{"claim_id": "C1", "text": "Retrieval precedes extraction.",
                                 "citations": [{"source_id": "S1", "quote": EVIDENCE}]}]
    [call] = record["llm_calls"]
    assert call["purpose"] == "extract" and call["source_id"] == "S1"
    assert (call["input_tokens"], call["output_tokens"], call["tokens_estimated"]) == (120, 30, False)

    db = routing_db()
    try:
        run = db.get(cdb.RoutingRun, record["routing_run_id"])
        task = db.get(cdb.RoutingTask, run.task_id)
        rows = db.query(cdb.RoutingModelRun).filter(cdb.RoutingModelRun.run_id == run.id).all()
    finally:
        db.close()
    assert task.task_type == "deep_research" and task.owner == "alice"
    assert run.status == "succeeded"
    assert [(r.input_tokens, r.output_tokens, r.completed) for r in rows] == [(120, 30, True)]
    assert json.loads(rows[0].notes)["purpose"] == "extract"


@pytest.mark.asyncio
async def test_grounding_view_carries_exact_text_and_unaltered_quotes(fake_web, data_dirs, routing_db):
    paraphrase = "The page says extraction comes after retrieval."
    fake_web.append(_extraction(evidence=[EVIDENCE, paraphrase]))
    researcher = _researcher(run_record=True)
    researcher.findings = [await researcher._fetch_and_extract("https://example.test/a", "q", "A")]
    saved = _save(researcher, data_dirs)

    view = grounding_view(saved["run_record"], blob_root=data_dirs)

    assert set(view) == {"run_id", "sources", "claims"}
    [src] = view["sources"]
    assert src["text"] == PAGE and _sha(src["text"]) == src["content_sha256"]
    quotes = [c["quote"] for c in view["claims"][0]["citations"]]
    # The paraphrase is recorded as the model returned it; judging it is the
    # grounding checker's job, not the recorder's.
    assert quotes == [EVIDENCE, paraphrase]
    assert EVIDENCE in src["text"] and paraphrase not in src["text"]


@pytest.mark.asyncio
async def test_failed_llm_call_is_recorded_and_finding_dropped(fake_web, data_dirs, routing_db):
    fake_web.append(httpx.ReadTimeout("slow"))
    researcher = _researcher(run_record=True)

    finding = await researcher._fetch_and_extract("https://example.test/a", "q", "A")

    assert finding is None
    record = researcher.run_recorder.build(blob_dir=data_dirs / "blobs")
    assert len(record["sources"]) == 1  # retrieved and sent, even though extraction failed
    assert record["claims"] == []
    [call] = record["llm_calls"]
    assert call["error"] and call["tokens_estimated"] is True


def test_oversized_source_is_hashed_but_not_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(research_run_record, "DEEP_RESEARCH_BLOB_MAX_BYTES", 10)
    recorder = ResearchRunRecorder(run_id="run-1")
    recorder.add_source("https://example.test/big", "Big", "x" * 11)

    [src] = recorder.build(blob_dir=tmp_path / "blobs")["sources"]

    assert src["blob"] is None and src["blob_missing_reason"] == "over_size_cap"
    assert src["content_sha256"] == _sha("x" * 11)
    assert "text" not in grounding_view({"run_id": "run-1", "sources": [src], "claims": []},
                                        blob_root=tmp_path)["sources"][0]


def test_grounding_view_refuses_blob_paths_outside_root(tmp_path):
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    root = tmp_path / "deep_research"
    root.mkdir()
    record = {"run_id": "r", "claims": [], "sources": [
        {"source_id": "S1", "url": "u", "content_sha256": "x", "blob": "../secret.txt"}]}

    assert "text" not in grounding_view(record, blob_root=root)["sources"][0]


@pytest.mark.asyncio
async def test_last_async_usage_is_per_task(fake_web):
    fake_web.extend(["a", "b"])

    async def call():
        await llm_core.llm_call_async("http://local.test/v1/chat/completions", "m",
                                      [{"role": "user", "content": "hi"}])
        return llm_core.last_async_usage()

    usages = await asyncio.gather(call(), call())

    assert usages == [{"input_tokens": 120, "output_tokens": 30}] * 2
    assert llm_core.last_async_usage() is None


def _profile(db, endpoint_id, base_url, model, premium=False):
    db.add(cdb.ModelEndpoint(id=endpoint_id, name=endpoint_id, base_url=base_url))
    db.add(cdb.RoutingModelProfile(id=f"p-{endpoint_id}", model_endpoint_id=endpoint_id, model=model,
                                   roles="[]", input_cost_per_mtok=1_000_000.0,
                                   output_cost_per_mtok=0.0, is_premium=premium))


def _persisted_run(factory, recorder):
    run_id = recorder.persist_model_runs(query="q", owner="alice", status="done")
    db = factory()
    try:
        run = db.get(cdb.RoutingRun, run_id)
        rows = db.query(cdb.RoutingModelRun).filter(cdb.RoutingModelRun.run_id == run_id).all()
        return run.spend_total_usd, run.spend_premium_usd, [r.model_profile_id for r in rows]
    finally:
        db.close()


def _one_call(endpoint, model="m"):
    recorder = ResearchRunRecorder()
    recorder.add_llm_call(purpose="extract", model=model, endpoint=endpoint,
                          started_at="2026-10-19T00:00:00+00:00", latency_ms=5,
                          input_tokens=2, output_tokens=0, tokens_estimated=False)
    return recorder


def test_profile_on_another_endpoint_is_not_charged(routing_db):
    db = routing_db()
    _profile(db, "other", "http://other.test/v1", "m")
    db.commit()
    db.close()

    total, premium, profile_ids = _persisted_run(
        routing_db, _one_call("http://local.test/v1/chat/completions"))

    assert (total, premium, profile_ids) == (0.0, 0.0, [None])


def test_premium_profile_on_the_research_endpoint_counts_as_premium(routing_db):
    db = routing_db()
    _profile(db, "local", "http://local.test/v1", "m", premium=True)
    _profile(db, "other", "http://other.test/v1", "m")
    db.commit()
    db.close()

    total, premium, profile_ids = _persisted_run(
        routing_db, _one_call("http://local.test/v1/chat/completions"))

    assert (total, premium, profile_ids) == (2.0, 2.0, ["p-local"])


@pytest.mark.asyncio
async def test_started_at_is_taken_before_the_call(fake_web, monkeypatch):
    from datetime import datetime, timedelta, timezone

    real_client = llm_core._get_http_client()

    class _SlowClient:
        async def post(self, *a, **kw):
            await asyncio.sleep(1.2)
            return await real_client.post(*a, **kw)

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _SlowClient())
    fake_web.append("ok")
    researcher = _researcher(run_record=True)
    before = datetime.now(timezone.utc).replace(microsecond=0)

    await researcher._llm([{"role": "user", "content": "x"}])

    started = datetime.fromisoformat(researcher.run_recorder.llm_calls[0]["started_at"])
    assert started < before + timedelta(seconds=1)


@pytest.mark.asyncio
async def test_cancelled_llm_call_is_recorded(monkeypatch):
    class _HangingClient:
        async def post(self, *a, **kw):
            await asyncio.sleep(3600)

    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _HangingClient())
    monkeypatch.setattr(llm_core, "_get_cached_response", lambda key: None)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **kw: None)
    researcher = _researcher(run_record=True)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(researcher._llm([{"role": "user", "content": "x"}]), timeout=0.05)

    [call] = researcher.run_recorder.llm_calls
    assert call["error"].startswith("CancelledError")


def test_grounding_cli_reads_the_configured_data_dir(tmp_path):
    import os
    import subprocess
    from pathlib import Path

    research_dir = tmp_path / "data" / "deep_research"
    recorder = ResearchRunRecorder(run_id="run-cli")
    sid = recorder.add_source("https://example.test/a", "A", PAGE)
    recorder.add_finding({"source_id": sid, "summary": "s", "evidence": EVIDENCE})
    record = recorder.build(blob_dir=research_dir / "blobs")
    (research_dir / "rp-cli1.json").write_text(json.dumps({"query": "q", "run_record": record}))
    repo = Path(__file__).resolve().parent.parent

    out = subprocess.run(
        [sys.executable, str(repo / "scripts" / "odysseus-research"), "grounding", "rp-cli1"],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "ODYSSEUS_DATA_DIR": str(tmp_path / "data")})

    assert out.returncode == 0, out.stderr
    view = json.loads(out.stdout)
    assert view["sources"][0]["text"] == PAGE
    assert view["claims"][0]["citations"] == [{"source_id": "S1", "quote": EVIDENCE}]
