"""Tests for named research depth modes with deterministic default selection."""

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import routes.research_routes as research_routes
from src.deep_research import (
    DEPTH_MODES,
    DeepResearcher,
    resolve_depth,
    select_default_depth,
)


def test_preset_table_values():
    """Verify DEPTH_MODES table contents match requirements exactly."""
    assert DEPTH_MODES["quick"] == {
        "max_rounds": 2,
        "max_time": 90,
        "max_urls_per_round": 3,
    }
    assert DEPTH_MODES["standard"] == {
        "max_rounds": 4,
        "max_time": 180,
        "max_urls_per_round": 3,
    }
    assert DEPTH_MODES["deep"] == {
        "max_rounds": 8,
        "max_time": 300,
        "max_urls_per_round": 4,
    }
    assert DEPTH_MODES["critical"] == {
        "max_rounds": 8,
        "max_time": 600,
        "max_urls_per_round": 4,
    }


def test_select_default_depth_deterministic():
    """Verify deterministic mapping from category to default depth mode."""
    assert select_default_depth("product") == "standard"
    assert select_default_depth("PRODUCT") == "standard"
    assert select_default_depth("comparison") == "standard"
    assert select_default_depth("howto") == "standard"
    assert select_default_depth("factcheck") == "deep"
    assert select_default_depth("FACTCHECK") == "deep"
    assert select_default_depth(None) == "standard"
    assert select_default_depth("") == "standard"
    assert select_default_depth("unknown") == "standard"


def test_resolve_depth_and_overrides():
    """Verify resolve_depth resolves presets and applies per-request overrides."""
    # Preset without overrides
    q = resolve_depth("quick")
    assert q == {"max_rounds": 2, "max_time": 90, "max_urls_per_round": 3}

    # Override max_time
    q_override = resolve_depth("quick", overrides={"max_time": 120})
    assert q_override == {"max_rounds": 2, "max_time": 120, "max_urls_per_round": 3}

    # Override max_rounds and max_urls_per_round
    s_override = resolve_depth("standard", overrides={"max_rounds": 6, "max_urls_per_round": 5})
    assert s_override == {"max_rounds": 6, "max_time": 180, "max_urls_per_round": 5}

    # None mode defaults to standard legacy limits: 8 / 300 / 3
    none_mode = resolve_depth(None)
    assert none_mode == {"max_rounds": 8, "max_time": 300, "max_urls_per_round": 3}


def test_deep_researcher_defaults_regression():
    """Regression test: constructor defaults match today's values when depth is omitted."""
    r = DeepResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model",
    )
    assert r.max_rounds == 8
    assert r.max_time == 300
    assert r.max_urls_per_round == 3
    assert r.min_rounds == 2
    assert r.depth is None


def test_deep_researcher_presets():
    """DeepResearcher initializes with limits corresponding to the chosen depth mode."""
    r_quick = DeepResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model",
        depth="quick",
    )
    assert r_quick.max_rounds == 2
    assert r_quick.max_time == 90
    assert r_quick.max_urls_per_round == 3
    assert r_quick.min_rounds <= r_quick.max_rounds
    assert r_quick.min_rounds == 2

    r_deep = DeepResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model",
        depth="deep",
    )
    assert r_deep.max_rounds == 8
    assert r_deep.max_time == 300
    assert r_deep.max_urls_per_round == 4
    assert r_deep.min_rounds == 2

    # Overrides win over preset
    r_override = DeepResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model",
        depth="quick",
        max_time=120,
        max_urls_per_round=5,
    )
    assert r_override.max_rounds == 2
    assert r_override.max_time == 120
    assert r_override.max_urls_per_round == 5


@pytest.mark.asyncio
async def test_quick_depth_adds_zero_model_calls():
    """depth='quick' adds ZERO model calls compared with explicit limits:
    preset only changes limits and never injects new LLM steps.
    """
    call_counts = {"depth_quick": 0, "explicit_limits": 0}

    def make_researcher(name, **kwargs):
        r = DeepResearcher(
            llm_endpoint="http://local.test/v1/chat/completions",
            llm_model="local-model",
            **kwargs,
        )

        async def fake_llm(messages, **llm_kwargs):
            call_counts[name] += 1
            prompt_content = messages[0]["content"] if messages else ""
            if "research strategist" in prompt_content:
                return '{"sub_questions": ["q1"], "key_topics": ["t1"], "success_criteria": "complete"}'
            if "planning web searches" in prompt_content:
                return '["query 1"]'
            if "deciding whether a research report is comprehensive enough" in prompt_content:
                return '{"comprehensive_enough": true, "reason": "done"}'
            if "updating an evolving research report" in prompt_content:
                return "Updated report content"
            if "write the final research report" in prompt_content or "synthesize" in prompt_content:
                return "Final report content"
            return "ok"

        r._llm = fake_llm
        r._search = AsyncMock(return_value=[{"url": "https://example.com/1", "title": "Page 1"}])
        r._fetch_and_extract = AsyncMock(return_value={"url": "https://example.com/1", "title": "Page 1", "summary": "Findings"})
        return r

    r1 = make_researcher("depth_quick", depth="quick")
    r2 = make_researcher("explicit_limits", max_rounds=2, max_time=90, max_urls_per_round=3)

    out1 = await r1.research("What is quantum computing?")
    out2 = await r2.research("What is quantum computing?")

    assert call_counts["depth_quick"] > 0
    assert call_counts["depth_quick"] == call_counts["explicit_limits"]


@pytest.mark.asyncio
async def test_limit_max_rounds_honoured():
    """Verify research loop terminates when max_rounds is reached."""
    r = DeepResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model",
        max_rounds=3,
        min_rounds=1,
    )

    rounds_run = []
    query_counter = {"n": 0}  # queries already used are dropped, so each round needs a new one

    async def fake_llm(messages, **llm_kwargs):
        content = messages[0]["content"] if messages else ""
        if "planning web searches" in content:
            query_counter["n"] += 1
            return f'["query {query_counter["n"]}"]'
        if "deciding whether a research report is comprehensive enough" in content:
            # LLM says not comprehensive enough; loop should continue until max_rounds
            return '{"comprehensive_enough": false, "reason": "need more"}'
        return "report text"

    r._llm = fake_llm
    counter = {"n": 0}

    async def fresh_search(query):
        # A new URL every call so no round is an "empty round" that stops the loop early.
        counter["n"] += 1
        return [{"url": f"https://example.com/{counter['n']}", "title": "Page"}]

    async def fake_extract(url, question, title):
        return {"url": url, "title": title, "summary": "Finding"}

    r._search = fresh_search
    r._fetch_and_extract = fake_extract

    def on_progress(evt):
        if evt.get("phase") == "searching" and "round" in evt:
            rounds_run.append(evt["round"])

    r._progress = on_progress

    await r.research("test question")
    assert r.round_count == 3
    assert max(rounds_run) == 3


def test_limit_max_time_honoured():
    """Verify _time_exceeded honours max_time."""
    r = DeepResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model",
        max_time=50,
    )
    r._start_time = time.time() - 40
    assert not r._time_exceeded()

    r._start_time = time.time() - 60
    assert r._time_exceeded()


@pytest.mark.asyncio
async def test_limit_max_urls_per_round_honoured():
    """Verify no more than max_urls_per_round * len(queries) URLs are fetched."""
    r = DeepResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model",
        max_urls_per_round=2,
    )
    r._start_time = time.time()

    async def fake_search(query):
        return [{"url": f"https://example.com/{query}/{i}", "title": f"T{i}"} for i in range(10)]

    extracted_urls = []

    async def fake_extract(url, question, title):
        extracted_urls.append(url)
        return {"url": url, "title": title, "summary": "ok"}

    r._search = fake_search
    r._fetch_and_extract = fake_extract

    queries = ["q1", "q2"]  # 2 queries * max_urls_per_round(2) = 4 max
    findings = await r._search_and_extract(queries, "question")

    assert len(findings) == 4
    assert len(extracted_urls) == 4


@pytest.fixture
def research_client(monkeypatch):
    """Mini app around the real research routes with a recording handler."""
    from unittest.mock import MagicMock

    import src.auth_helpers as auth_helpers

    monkeypatch.setattr(auth_helpers, "require_privilege", lambda request, name: "tester")
    monkeypatch.setattr(
        research_routes, "resolve_endpoint",
        lambda role, owner=None: ("http://local.test/v1/chat/completions", "model", {}),
    )
    handler = MagicMock()
    app = FastAPI()
    app.include_router(research_routes.setup_research_routes(handler))
    return TestClient(app), handler


def test_route_rejects_unknown_depth(research_client):
    client, handler = research_client
    resp = client.post("/api/research/start", json={"query": "q", "depth": "invalid_mode"})
    assert resp.status_code == 422
    handler.start_research.assert_not_called()


def test_route_without_depth_keeps_todays_limits(research_client):
    client, handler = research_client
    resp = client.post("/api/research/start", json={"query": "q"})
    assert resp.status_code == 200
    kwargs = handler.start_research.call_args.kwargs
    assert kwargs["depth"] is None
    assert kwargs["max_rounds"] == 20
    assert kwargs["max_time"] == 300
    assert kwargs["max_urls_per_round"] is None


def test_route_passes_depth_and_overrides(research_client):
    client, handler = research_client
    resp = client.post(
        "/api/research/start",
        json={"query": "q", "depth": "Quick", "max_urls_per_round": 4, "max_time": 120},
    )
    assert resp.status_code == 200
    kwargs = handler.start_research.call_args.kwargs
    assert kwargs["depth"] == "quick"
    assert kwargs["max_urls_per_round"] == 4
    assert kwargs["max_time"] == 120
    assert kwargs["max_rounds"] is None  # the preset supplies it


def test_unknown_depth_mode_is_rejected_not_ignored():
    """An unknown mode must fail loudly at the engine level too."""
    with pytest.raises(ValueError):
        resolve_depth("turbo")
    with pytest.raises(ValueError):
        DeepResearcher(
            llm_endpoint="http://local.test/v1/chat/completions",
            llm_model="local-model",
            depth="turbo",
        )


@pytest.fixture
def captured_researcher(monkeypatch):
    """Run ResearchHandler.call_research_service with DeepResearcher replaced by a recorder."""
    import src.deep_research as deep_research
    from src.research_handler import ResearchHandler

    captured = {}

    class FakeResearcher:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def research(self, query, **kwargs):
            return "report"

        def get_stats(self):
            return {}

    async def no_probe(*args, **kwargs):
        return None

    monkeypatch.setattr(deep_research, "DeepResearcher", FakeResearcher)
    monkeypatch.setattr(ResearchHandler, "_probe_endpoint", staticmethod(no_probe))
    monkeypatch.setattr(
        ResearchHandler, "_format_research_report", lambda self, *a, **k: "formatted"
    )
    handler = ResearchHandler.__new__(ResearchHandler)

    async def run(**kwargs):
        captured.clear()
        await handler.call_research_service(
            "q", "http://local.test/v1/chat/completions", "model", **kwargs
        )
        return dict(captured)

    return run


@pytest.mark.asyncio
async def test_handler_without_depth_uses_legacy_limits(captured_researcher):
    kwargs = await captured_researcher()
    assert kwargs["max_rounds"] == 20
    assert kwargs["max_time"] == 300
    assert kwargs["max_urls_per_round"] == 3
    assert kwargs["depth"] is None


@pytest.mark.asyncio
async def test_handler_auto_depth_is_deterministic_per_category(captured_researcher):
    first = await captured_researcher(depth="auto", category="factcheck")
    second = await captured_researcher(depth="auto", category="factcheck")
    assert first["depth"] == second["depth"] == "deep"
    assert (first["max_rounds"], first["max_time"], first["max_urls_per_round"]) == (8, 300, 4)

    plain = await captured_researcher(depth="auto")
    assert plain["depth"] == "standard"
    assert (plain["max_rounds"], plain["max_time"]) == (4, 180)


@pytest.mark.asyncio
async def test_handler_explicit_limit_overrides_preset(captured_researcher):
    kwargs = await captured_researcher(depth="quick", max_time=120, max_urls_per_round=5)
    assert kwargs["max_rounds"] == 2
    assert kwargs["max_time"] == 120
    assert kwargs["max_urls_per_round"] == 5
