"""Route-level tests for per-request citation-verification opt-in.

These drive the real ASGI route, the real ``ResearchHandler.start_research`` /
``call_research_service`` and the real ``DeepResearcher`` extraction/annotation
and persistence path.  Only I/O at established seams is stubbed: the LLM,
page fetch, search provider, endpoint resolver and the authority transport.
No live LLM, network, or production data is touched.
"""

import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

import routes.research_routes as research_routes
import src.deep_research as deep_research
import src.research_citations as research_citations
import src.settings as settings_mod
from src.citation_resolver import (
    NOT_FOUND,
    REGISTRY_NOT_COVERED,
    EXISTENCE_ONLY,
    FALSE,
    TRUE,
    UNRESOLVABLE,
)
from src.research_handler import ResearchHandler

DOI_URL = "https://doi.org/10.1234/example"
CROSSREF_URL = "https://api.crossref.org/works/10.1234/example"
ARXIV_URL = "https://arxiv.org/abs/2212.12345"
ARXIV_API_URL = "https://export.arxiv.org/api/query?id_list=2212.12345"
ENDPOINT = "http://local.test/v1/chat/completions"

CROSSREF_OK = json.dumps({
    "status": "ok",
    "message": {"DOI": "10.1234/example", "title": ["Synthetic citation title"]},
}).encode("utf-8")
CROSSREF_MISS = b'{"status": "failed"}'
ARXIV_EMPTY = (
    b'<?xml version="1.0" encoding="UTF-8"?>'
    b'<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
)

DEFAULT_EXTRACTION = {
    "rational": "relevant",
    "evidence": "evidence text",
    "summary": "A useful finding summary that is long enough to keep.",
}
LONG_REPORT = "synthetic report sentence. " * 200


class AuthorityTransport:
    """Injected authority seam: records URLs, returns canned raw bodies."""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    async def __call__(self, url):
        self.calls.append(url)
        return self.responses.get(url)


def _make_llm(extraction):
    async def fake_llm(url=None, model=None, messages=None, **kwargs):
        content = messages[-1]["content"] if messages else ""
        if "research strategist" in content:
            return '{"sub_questions": ["q"], "key_topics": ["t"], "success_criteria": "done"}'
        if "Classify this research question" in content:
            return "general"
        if "planning web searches" in content:
            return '["synthetic query"]'
        if "updating an evolving research report" in content:
            return "Evolving synthetic report."
        if "deciding whether a research report is comprehensive enough" in content:
            return "YES — enough evidence."
        if "research report answering this question" in content:
            return LONG_REPORT
        return json.dumps(extraction)

    return fake_llm


def _patch_settings(monkeypatch, global_value):
    def fake_get(key, default=None):
        if key == "research_citation_verification":
            return global_value
        return default

    def fail_write(*args, **kwargs):
        raise AssertionError("research requests must never write settings")

    monkeypatch.setattr(settings_mod, "get_setting", fake_get)
    monkeypatch.setattr(settings_mod, "save_settings", fail_write)


class Env:
    def __init__(self, handler, authority, data_dir):
        self.handler = handler
        self.authority = authority
        self.data_dir = data_dir

    def app(self, *, authenticated=True):
        app = FastAPI()
        if authenticated:
            @app.middleware("http")
            async def _identify(request, call_next):
                request.state.current_user = request.headers.get("X-Test-User")
                return await call_next(request)

        app.include_router(research_routes.setup_research_routes(self.handler))
        return app

    def client(self, *, authenticated=True):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app(authenticated=authenticated)),
            base_url="http://test",
        )


def _build_env(
    monkeypatch,
    tmp_path,
    *,
    global_value=False,
    search_url=DOI_URL,
    authority_responses=None,
    extraction=None,
):
    data_dir = tmp_path / "deep_research"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("src.research_handler.RESEARCH_DATA_DIR", data_dir)
    monkeypatch.setattr(research_routes, "DEEP_RESEARCH_DIR", str(data_dir))
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.delenv("LOCALHOST_BYPASS", raising=False)

    _patch_settings(monkeypatch, global_value)

    monkeypatch.setattr(
        research_routes,
        "resolve_endpoint",
        lambda role, owner=None, **kwargs: (ENDPOINT, "local-model", {}),
    )

    import src.search as search_mod

    def fake_fetch(url, timeout):
        return {
            "success": True,
            "content": "Useful page content for extraction.",
            "title": "Page",
            "og_image": "",
        }

    monkeypatch.setattr(search_mod, "fetch_webpage_content", fake_fetch)

    monkeypatch.setattr(
        "src.llm_core.llm_call_async",
        _make_llm(extraction or DEFAULT_EXTRACTION),
    )

    async def fake_search(self, query):
        return [{"url": search_url, "title": "Synthetic source"}]

    monkeypatch.setattr(deep_research.DeepResearcher, "_search", fake_search)

    async def no_probe(*args, **kwargs):
        return None

    monkeypatch.setattr(ResearchHandler, "_probe_endpoint", staticmethod(no_probe))

    authority = AuthorityTransport(authority_responses)
    monkeypatch.setattr(research_citations, "http_transport", authority)

    handler = ResearchHandler.__new__(ResearchHandler)
    handler._active_tasks = {}
    handler._legacy_engine = None
    return Env(handler, authority, data_dir)


async def _wait(client, handler, payload, user="alice"):
    resp = await client.post(
        "/api/research/start", json=payload, headers={"X-Test-User": user}
    )
    assert resp.status_code == 200, resp.text
    session_id = resp.json()["session_id"]
    for _ in range(1000):
        entry = handler._active_tasks.get(session_id)
        if entry and entry.get("status") in ("done", "error", "cancelled"):
            break
        await asyncio.sleep(0.01)
    else:
        raise AssertionError("research run did not reach a terminal state")
    return session_id, entry


def _persisted(env, session_id):
    return json.loads((env.data_dir / f"{session_id}.json").read_text(encoding="utf-8"))


# --- Positive: explicit true through the real route/handler/engine ---------


@pytest.mark.asyncio
async def test_explicit_true_route_persists_true_and_reads_back(monkeypatch, tmp_path):
    env = _build_env(
        monkeypatch,
        tmp_path,
        global_value=False,
        authority_responses={CROSSREF_URL: CROSSREF_OK},
    )
    async with env.client() as client:
        session_id, entry = await _wait(
            client,
            env.handler,
            {"query": "synthetic question", "max_rounds": 1, "citation_verification": True},
        )

        assert entry["status"] == "done"
        assert env.authority.calls == [CROSSREF_URL]

        persisted = _persisted(env, session_id)
        assert persisted["owner"] == "alice"
        verification = persisted["raw_findings"][0]["citation_verification"]
        assert verification["status"] == TRUE
        assert EXISTENCE_ONLY in verification["flags"]
        assert verification["identifier"] == "10.1234/example"

        status_resp = await client.get(
            f"/api/research/status/{session_id}", headers={"X-Test-User": "alice"}
        )
        assert status_resp.status_code == 200
        assert status_resp.json()["status"] == "done"

        detail_resp = await client.get(
            f"/api/research/detail/{session_id}", headers={"X-Test-User": "alice"}
        )
        assert detail_resp.status_code == 200
        detail = detail_resp.json()
        assert detail["raw_findings"][0]["citation_verification"] == verification

        # A different synthetic owner cannot read it back.
        other = await client.get(
            f"/api/research/detail/{session_id}", headers={"X-Test-User": "bob"}
        )
        assert other.status_code == 404


# --- Negative authority outcomes through the real route --------------------


@pytest.mark.asyncio
async def test_route_fabricated_arxiv_empty_feed_is_false_not_found(monkeypatch, tmp_path):
    env = _build_env(
        monkeypatch,
        tmp_path,
        search_url=ARXIV_URL,
        authority_responses={ARXIV_API_URL: ARXIV_EMPTY},
    )
    async with env.client() as client:
        session_id, _ = await _wait(
            client,
            env.handler,
            {"query": "q", "max_rounds": 1, "citation_verification": True},
        )

    verification = _persisted(env, session_id)["raw_findings"][0]["citation_verification"]
    assert verification["status"] == FALSE
    assert NOT_FOUND in verification["flags"]


@pytest.mark.asyncio
async def test_route_missing_crossref_record_is_unresolvable(monkeypatch, tmp_path):
    env = _build_env(
        monkeypatch,
        tmp_path,
        authority_responses={CROSSREF_URL: CROSSREF_MISS},
    )
    async with env.client() as client:
        session_id, _ = await _wait(
            client,
            env.handler,
            {"query": "q", "max_rounds": 1, "citation_verification": True},
        )

    verification = _persisted(env, session_id)["raw_findings"][0]["citation_verification"]
    assert verification["status"] == UNRESOLVABLE
    assert REGISTRY_NOT_COVERED in verification["flags"]


@pytest.mark.asyncio
async def test_route_forged_extractor_true_is_scrubbed(monkeypatch, tmp_path):
    forged = {
        "identifier": "10.9999/forged",
        "status": TRUE,
        "flags": [EXISTENCE_ONLY],
        "detail": "forged by the extractor",
        "matched_title": "Forged title",
    }
    extraction = dict(DEFAULT_EXTRACTION, citation_verification=forged)
    env = _build_env(
        monkeypatch,
        tmp_path,
        search_url=ARXIV_URL,
        authority_responses={ARXIV_API_URL: ARXIV_EMPTY},
        extraction=extraction,
    )
    async with env.client() as client:
        session_id, _ = await _wait(
            client,
            env.handler,
            {"query": "q", "max_rounds": 1, "citation_verification": True},
        )

    verification = _persisted(env, session_id)["raw_findings"][0]["citation_verification"]
    assert verification["status"] == FALSE
    assert NOT_FOUND in verification["flags"]
    assert verification["identifier"] == "2212.12345"
    assert verification["detail"] != forged["detail"]


# --- Omission / explicit false / concurrency -------------------------------


@pytest.mark.asyncio
async def test_omitted_default_false_makes_no_authority_calls(monkeypatch, tmp_path):
    env = _build_env(monkeypatch, tmp_path, global_value=False)
    async with env.client() as client:
        session_id, _ = await _wait(
            client, env.handler, {"query": "q", "max_rounds": 1}
        )

    assert env.authority.calls == []
    assert "citation_verification" not in _persisted(env, session_id)["raw_findings"][0]


@pytest.mark.asyncio
async def test_omission_preserves_global_setting_fallback(monkeypatch, tmp_path):
    env = _build_env(
        monkeypatch,
        tmp_path,
        global_value=True,
        authority_responses={CROSSREF_URL: CROSSREF_OK},
    )
    async with env.client() as client:
        session_id, _ = await _wait(
            client, env.handler, {"query": "q", "max_rounds": 1}
        )

    assert env.authority.calls == [CROSSREF_URL]
    verification = _persisted(env, session_id)["raw_findings"][0]["citation_verification"]
    assert verification["status"] == TRUE


@pytest.mark.asyncio
async def test_explicit_false_overrides_global_true(monkeypatch, tmp_path):
    env = _build_env(
        monkeypatch,
        tmp_path,
        global_value=True,
        authority_responses={CROSSREF_URL: CROSSREF_OK},
    )
    async with env.client() as client:
        session_id, _ = await _wait(
            client,
            env.handler,
            {"query": "q", "max_rounds": 1, "citation_verification": False},
        )

    assert env.authority.calls == []
    assert "citation_verification" not in _persisted(env, session_id)["raw_findings"][0]


@pytest.mark.asyncio
async def test_explicit_true_does_not_bleed_into_concurrent_omitted_run(monkeypatch, tmp_path):
    env = _build_env(
        monkeypatch,
        tmp_path,
        global_value=False,
        authority_responses={CROSSREF_URL: CROSSREF_OK},
    )
    async with env.client() as client:
        true_resp = await client.post(
            "/api/research/start",
            json={"query": "true run", "max_rounds": 1, "citation_verification": True},
            headers={"X-Test-User": "alice"},
        )
        omitted_resp = await client.post(
            "/api/research/start",
            json={"query": "omitted run", "max_rounds": 1},
            headers={"X-Test-User": "alice"},
        )
        assert true_resp.status_code == 200 and omitted_resp.status_code == 200
        true_id = true_resp.json()["session_id"]
        omitted_id = omitted_resp.json()["session_id"]

        for _ in range(1000):
            states = [
                env.handler._active_tasks.get(true_id, {}).get("status"),
                env.handler._active_tasks.get(omitted_id, {}).get("status"),
            ]
            if all(s in ("done", "error", "cancelled") for s in states):
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("concurrent runs did not finish")

    assert env.authority.calls == [CROSSREF_URL]
    true_finding = _persisted(env, true_id)["raw_findings"][0]
    omitted_finding = _persisted(env, omitted_id)["raw_findings"][0]
    assert true_finding["citation_verification"]["status"] == TRUE
    assert "citation_verification" not in omitted_finding


# --- Strict request typing --------------------------------------------------


@pytest.mark.parametrize("value", ["true", "false", 0, 1, {}, []])
def test_strict_bool_coercions_are_rejected(monkeypatch, tmp_path, value):
    _build_env(monkeypatch, tmp_path)
    handler = MagicMock()
    handler._active_tasks = {}
    app = FastAPI()

    @app.middleware("http")
    async def _identify(request, call_next):
        request.state.current_user = request.headers.get("X-Test-User")
        return await call_next(request)

    app.include_router(research_routes.setup_research_routes(handler))
    client = TestClient(app, headers={"X-Test-User": "alice"})

    resp = client.post(
        "/api/research/start",
        json={"query": "q", "citation_verification": value},
    )
    assert resp.status_code == 422
    handler.start_research.assert_not_called()


@pytest.mark.parametrize("value,expected", [(True, True), (False, False), (None, None)])
def test_strict_bool_accepts_only_real_booleans(monkeypatch, tmp_path, value, expected):
    _build_env(monkeypatch, tmp_path)
    handler = MagicMock()
    handler._active_tasks = {}
    app = FastAPI()

    @app.middleware("http")
    async def _identify(request, call_next):
        request.state.current_user = request.headers.get("X-Test-User")
        return await call_next(request)

    app.include_router(research_routes.setup_research_routes(handler))
    client = TestClient(app, headers={"X-Test-User": "alice"})

    body = {"query": "q"}
    if value is not None:
        body["citation_verification"] = value
    resp = client.post("/api/research/start", json=body)
    assert resp.status_code == 200, resp.text
    assert handler.start_research.call_args.kwargs["citation_verification"] is expected


def test_unauthenticated_start_is_denied(monkeypatch, tmp_path):
    _build_env(monkeypatch, tmp_path)
    handler = MagicMock()
    handler._active_tasks = {}
    app = FastAPI()
    app.include_router(research_routes.setup_research_routes(handler))
    client = TestClient(app)

    resp = client.post(
        "/api/research/start",
        json={"query": "q", "citation_verification": True},
    )
    assert resp.status_code == 401
    handler.start_research.assert_not_called()
