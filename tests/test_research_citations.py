"""Tests for runtime citation verification in deep-research findings.

Every authority answer is supplied by an injected transport stub: no live
DOI / arXiv lookup, no HTTP client, and no LLM call is made here.
"""

import asyncio
import json
import sys
import types

import pytest

from src.citation_resolver import (
    AUTHORITY_UNREACHABLE,
    EXISTENCE_ONLY,
    FALSE,
    NOT_FOUND,
    REGISTRY_NOT_COVERED,
    TRUE,
    UNRESOLVABLE,
)
from src.deep_research import DeepResearcher
from src.research_citations import (
    MAX_AUTHORITY_BYTES,
    MAX_LOOKUPS_PER_RUN,
    CitationVerificationSession,
    citation_target,
    http_transport,
)

DOI_URL = "https://doi.org/10.1234/example"
# The documented live Crossref shape: message.DOI carries the registered
# identity of the work, and a successful answer must match the requested DOI.
CROSSREF_OK = {
    "status": "ok",
    "message": {
        "DOI": "10.1234/example",
        "title": ["Synthetic citation title"],
        "author": [{"family": "Rivera"}, {"family": "Chen"}],
    },
}
CROSSREF_MISS = b'{"status": "failed"}'
ARXIV_URL = "https://arxiv.org/abs/2212.12345"
ARXIV_API_URL = "https://export.arxiv.org/api/query?id_list=2212.12345"


def _arxiv_feed(entry_id, title="Synthetic citation title",
                authors=("Ana Rivera", "Bo Chen"),
                namespace="http://www.w3.org/2005/Atom"):
    author_xml = "".join(f"<author><name>{name}</name></author>" for name in authors)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="{namespace}">
  <entry>
    <id>{entry_id}</id>
    <title>{title}</title>
    {author_xml}
  </entry>
</feed>
""".encode("utf-8")


ARXIV_ATOM = _arxiv_feed("http://arxiv.org/abs/2212.12345v1")
ARXIV_EMPTY = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"></feed>
"""
ARXIV_HTML = b"<html><body>Unavailable</body></html>"
ARXIV_ERROR = _arxiv_feed(
    "http://arxiv.org/api/errors#incorrect_id_format_for_0704.0001",
    title="Error",
)
ARXIV_WRONG_ID = _arxiv_feed("http://arxiv.org/abs/2201.99999v1")
ARXIV_UNNAMESPACED = b"""<feed>
  <entry>
    <id>http://arxiv.org/abs/2212.12345</id>
    <title>Synthetic citation title</title>
  </entry>
</feed>
"""
ARXIV_SUFFIX_NAMESPACE = _arxiv_feed(
    "http://arxiv.org/abs/2212.12345v1",
    namespace="http://www.w3.org/2005/Atom/",
)


class RecordingTransport:
    """Async transport stub: records URLs and returns canned raw bodies."""

    def __init__(self, responses=None, raises=None):
        self.responses = responses or {}
        self.raises = raises
        self.calls = []

    async def __call__(self, url):
        self.calls.append(url)
        if self.raises is not None:
            raise self.raises
        return self.responses.get(url)


def _crossref_ok_bytes(doi="10.1234/example"):
    payload = json.loads(json.dumps(CROSSREF_OK))
    payload["message"]["DOI"] = doi
    return json.dumps(payload).encode("utf-8")


def _finding(url="https://example.test/page", **extra):
    finding = {
        "url": url,
        "title": "Decorative source title -- Example Blog",
        "summary": "A useful finding summary that is long enough to keep.",
    }
    finding.update(extra)
    return finding


async def _annotate(url, responses=None, raises=None, **kwargs):
    transport = RecordingTransport(responses, raises)
    session = CitationVerificationSession(transport=transport, **kwargs)
    finding = _finding(url)
    record = await session.annotate(finding)
    return finding, record, transport, session


# --- URL recognition: exact host, path-only identifiers -------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://doi.org/10.1234/ABC", ("doi", "10.1234/abc")),
        ("https://dx.doi.org/10.1234/x", ("doi", "10.1234/x")),
        ("https://arxiv.org/abs/2212.12345", ("arxiv", "2212.12345")),
        ("https://arxiv.org/pdf/2212.12345v2.pdf", ("arxiv", "2212.12345v2")),
        ("https://www.arxiv.org/abs/hep-th/9901001", ("arxiv", "hep-th/9901001")),
    ],
)
def test_citation_target_accepts_authoritative_urls(url, expected):
    assert citation_target(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://doi.org.evil.example/10.1234/x",
        "https://evil.example/doi.org/10.1234/x",
        "https://evil.example/arxiv.org/abs/2212.12345",
        "https://doi.org@evil.example/10.1234/x",
        "https://doi.org:443/10.1234/x",
        "http://doi.org/10.1234/x",
        "https://user:pass@doi.org/10.1234/x",
        "https://doi.org/not-a-doi",
        "https://doi.org/articles/10.1234/x",
        "https://arxiv.org/abs/not-an-id",
        "https://arxiv.org/abs/2212.12345/extra",
        "https://arxiv.org/format/2212.12345",
        "https://example.com/10.1234/x",
        "https://example.com/arxiv.org/abs/2212.12345",
        "javascript:alert(1)",
        "not a url",
        "",
        None,
        123,
    ],
)
def test_citation_target_rejects_lookalikes_and_ordinary_urls(url):
    assert citation_target(url) is None


async def test_lookalike_and_malformed_urls_make_zero_authority_calls():
    transport = RecordingTransport()
    session = CitationVerificationSession(transport=transport)
    for url in (
        "https://doi.org.evil.example/10.1234/x",
        "https://doi.org:443/10.1234/x",
        "https://example.com/10.1234/x",
        "https://doi.org/not-a-doi",
        "https://arxiv.org/abs/not-an-id",
        "https://arxiv.org/abs/2212.12345/extra",
    ):
        finding = _finding(url)
        assert await session.annotate(finding) is None
        assert "citation_verification" not in finding
    assert transport.calls == []
    assert session.lookups == 0


# --- Status discipline stays the resolver's --------------------------------


@pytest.mark.asyncio
async def test_valid_doi_opt_in_is_true_and_serializable():
    finding, record, transport, _ = await _annotate(
        DOI_URL,
        {"https://api.crossref.org/works/10.1234/example": _crossref_ok_bytes()},
    )

    assert transport.calls == ["https://api.crossref.org/works/10.1234/example"]
    assert record == finding["citation_verification"]
    assert record["status"] == TRUE
    assert EXISTENCE_ONLY in record["flags"]
    assert record["identifier"] == "10.1234/example"
    assert record["matched_title"] == "Synthetic citation title"
    assert set(record) == {"identifier", "status", "flags", "detail", "matched_title"}
    # Retained by the current finding-persistence projection, JSON-safe.
    from src.research_handler import ResearchHandler

    persisted = ResearchHandler._extract_raw_findings([finding])
    assert persisted[0]["citation_verification"] == record
    json.dumps(persisted)


@pytest.mark.asyncio
async def test_arxiv_miss_is_false_not_found():
    _, record, transport, _ = await _annotate(
        ARXIV_URL, {"https://export.arxiv.org/api/query?id_list=2212.12345": ARXIV_EMPTY}
    )
    assert len(transport.calls) == 1
    assert record["status"] == FALSE
    assert NOT_FOUND in record["flags"]


@pytest.mark.asyncio
async def test_crossref_missing_record_is_unresolvable_registry_not_covered():
    _, record, transport, _ = await _annotate(
        DOI_URL, {"https://api.crossref.org/works/10.1234/example": CROSSREF_MISS}
    )
    assert len(transport.calls) == 1
    assert record["status"] == UNRESOLVABLE
    assert REGISTRY_NOT_COVERED in record["flags"]


@pytest.mark.asyncio
async def test_arxiv_hit_is_true_existence_only_without_title_matching():
    _, record, _, _ = await _annotate(
        ARXIV_URL,
        {"https://export.arxiv.org/api/query?id_list=2212.12345": ARXIV_ATOM},
    )
    # The finding's decorative title is never compared to the authority title.
    assert record["status"] == TRUE
    assert EXISTENCE_ONLY in record["flags"]
    assert record["matched_title"] == "Synthetic citation title"


# --- Failures are unavailable, never TRUE or fabricated FALSE --------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        None,
        b"not json at all",
        b'["not", "an", "object"]',
        b"x" * (MAX_AUTHORITY_BYTES + 1),
    ],
)
async def test_unusable_crossref_answers_are_unavailable(response):
    _, record, _, _ = await _annotate(
        DOI_URL, {"https://api.crossref.org/works/10.1234/example": response}
    )
    assert record["status"] == UNRESOLVABLE
    assert AUTHORITY_UNREACHABLE in record["flags"]


@pytest.mark.asyncio
async def test_invalid_atom_is_unavailable_not_false_miss():
    _, record, _, _ = await _annotate(
        ARXIV_URL,
        {"https://export.arxiv.org/api/query?id_list=2212.12345": b"<feed><entry>"},
    )
    assert record["status"] == UNRESOLVABLE
    assert AUTHORITY_UNREACHABLE in record["flags"]


@pytest.mark.asyncio
async def test_transport_exception_is_unavailable():
    _, record, _, _ = await _annotate(DOI_URL, raises=TimeoutError("timed out"))
    assert record["status"] == UNRESOLVABLE
    assert AUTHORITY_UNREACHABLE in record["flags"]


async def test_default_transport_refuses_non_authority_hosts_offline():
    # No client is constructed for a URL outside the exact allowlist.
    assert await http_transport("https://evil.example/10.1234/x") is None
    assert await http_transport("http://api.crossref.org/works/10.1234/x") is None


# --- Lookup budget and duplicates ------------------------------------------


@pytest.mark.asyncio
async def test_per_run_cap_bounds_actual_lookups():
    transport = RecordingTransport()
    session = CitationVerificationSession(transport=transport)
    records = []
    for i in range(MAX_LOOKUPS_PER_RUN + 1):
        url = f"https://doi.org/10.1234/item{i}"
        transport.responses[f"https://api.crossref.org/works/10.1234/item{i}"] = (
            _crossref_ok_bytes(f"10.1234/item{i}")
        )
        finding = _finding(url)
        records.append(await session.annotate(finding))

    assert len(transport.calls) == MAX_LOOKUPS_PER_RUN
    assert session.lookups == MAX_LOOKUPS_PER_RUN
    assert all(r["status"] == TRUE for r in records[:MAX_LOOKUPS_PER_RUN])
    assert records[MAX_LOOKUPS_PER_RUN]["status"] == UNRESOLVABLE
    assert AUTHORITY_UNREACHABLE in records[MAX_LOOKUPS_PER_RUN]["flags"]
    assert "cap" in records[MAX_LOOKUPS_PER_RUN]["detail"]


@pytest.mark.asyncio
async def test_duplicate_identifier_reuses_the_completed_result():
    transport = RecordingTransport(
        {"https://api.crossref.org/works/10.1234/example": _crossref_ok_bytes()}
    )
    session = CitationVerificationSession(transport=transport)
    first = _finding(DOI_URL)
    second = _finding(DOI_URL)
    r1 = await session.annotate(first)
    r2 = await session.annotate(second)

    assert len(transport.calls) == 1
    assert session.lookups == 1
    assert r1 == r2
    # Copies, not a shared mutable flags list.
    r1["flags"].append("MUTATED")
    assert "MUTATED" not in second["citation_verification"]["flags"]


# --- Cancellation propagates ------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_propagates_through_the_lookup():
    started = asyncio.Event()

    async def blocked(url):
        started.set()
        await asyncio.Event().wait()

    session = CitationVerificationSession(transport=blocked)
    task = asyncio.create_task(session.annotate(_finding(DOI_URL)))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- DeepResearcher extraction/finding path --------------------------------


def _patch_extraction(monkeypatch, extraction=None):
    search_mod = types.ModuleType("src.search")

    def fake_fetch_webpage_content(url, timeout):
        return {
            "success": True,
            "content": "Useful page content for extraction.",
            "title": "Page",
            "og_image": "",
        }

    search_mod.fetch_webpage_content = fake_fetch_webpage_content
    monkeypatch.setitem(sys.modules, "src.search", search_mod)

    async def immediate_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)
    return extraction or {
        "rational": "relevant",
        "evidence": "evidence",
        "summary": "A useful finding summary that is long enough to keep.",
    }


@pytest.mark.asyncio
async def test_disabled_default_does_zero_lookups_and_preserves_finding(monkeypatch):
    payload = _patch_extraction(monkeypatch)
    transport = RecordingTransport()
    researcher = DeepResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model",
        citation_transport=transport,
    )

    async def fake_llm(messages, **kwargs):
        return json.dumps(payload)

    researcher._llm = fake_llm
    finding = await researcher._fetch_and_extract(DOI_URL, "question", "Decorative title")

    assert transport.calls == []
    assert "citation_verification" not in finding
    assert finding["summary"] == payload["summary"]
    assert finding["url"] == DOI_URL
    assert researcher._citation_session is None


@pytest.mark.asyncio
async def test_deep_researcher_opted_in_records_true_on_the_finding(monkeypatch):
    payload = _patch_extraction(monkeypatch)
    transport = RecordingTransport(
        {"https://api.crossref.org/works/10.1234/example": _crossref_ok_bytes()}
    )
    researcher = DeepResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model",
        citation_verification=True,
        citation_transport=transport,
    )

    async def fake_llm(messages, **kwargs):
        return json.dumps(payload)

    researcher._llm = fake_llm
    finding = await researcher._fetch_and_extract(
        DOI_URL, "question", "Decorative title -- Example Blog"
    )

    assert transport.calls == ["https://api.crossref.org/works/10.1234/example"]
    record = finding["citation_verification"]
    assert record["status"] == TRUE
    assert EXISTENCE_ONLY in record["flags"]
    assert record["identifier"] == "10.1234/example"
    assert record["matched_title"] == "Synthetic citation title"
    json.dumps(finding)


@pytest.mark.asyncio
async def test_nonacademic_finding_url_makes_no_call_even_when_opted_in(monkeypatch):
    payload = _patch_extraction(monkeypatch)
    transport = RecordingTransport()
    researcher = DeepResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model",
        citation_verification=True,
        citation_transport=transport,
    )

    async def fake_llm(messages, **kwargs):
        return json.dumps(payload)

    researcher._llm = fake_llm
    finding = await researcher._fetch_and_extract(
        "https://example.com/article", "question", "Ordinary page"
    )

    assert transport.calls == []
    assert "citation_verification" not in finding


# --- Handler opt-in and persistence projection -----------------------------


@pytest.mark.asyncio
async def test_handler_reads_setting_and_passes_explicit_opt_in(monkeypatch):
    import src.settings as settings_mod
    from src.research_handler import ResearchHandler

    captured = {}

    class FakeResearcher:
        findings = []

        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def research(self, *args, **kwargs):
            return "synthetic report"

        def get_stats(self):
            return {}

    def fake_get_setting(key, default=None):
        if key == "research_citation_verification":
            return True
        return default

    monkeypatch.setattr(settings_mod, "get_setting", fake_get_setting)
    monkeypatch.setattr("src.deep_research.DeepResearcher", FakeResearcher)

    handler = object.__new__(ResearchHandler)

    async def fake_probe(*args, **kwargs):
        return None

    handler._probe_endpoint = fake_probe
    await handler.call_research_service(
        "question",
        "http://local.test/v1/chat/completions",
        "local-model",
    )
    assert captured["citation_verification"] is True


@pytest.mark.asyncio
async def test_handler_defaults_opt_in_to_false(monkeypatch):
    import src.settings as settings_mod
    from src.research_handler import ResearchHandler

    captured = {}

    class FakeResearcher:
        findings = []

        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def research(self, *args, **kwargs):
            return "synthetic report"

        def get_stats(self):
            return {}

    monkeypatch.setattr(settings_mod, "get_setting", lambda key, default=None: default)
    monkeypatch.setattr("src.deep_research.DeepResearcher", FakeResearcher)

    handler = object.__new__(ResearchHandler)

    async def fake_probe(*args, **kwargs):
        return None

    handler._probe_endpoint = fake_probe
    await handler.call_research_service(
        "question",
        "http://local.test/v1/chat/completions",
        "local-model",
    )
    assert captured["citation_verification"] is False


# --- arXiv identity: feed shape, error entries, entry-id match -------------


@pytest.mark.asyncio
async def test_arxiv_html_is_unavailable_not_an_authoritative_miss():
    _, record, _, _ = await _annotate(ARXIV_URL, {ARXIV_API_URL: ARXIV_HTML})
    assert record["status"] == UNRESOLVABLE
    assert AUTHORITY_UNREACHABLE in record["flags"]
    assert NOT_FOUND not in record["flags"]


@pytest.mark.asyncio
async def test_arxiv_error_entry_is_unavailable_never_true():
    _, record, _, _ = await _annotate(ARXIV_URL, {ARXIV_API_URL: ARXIV_ERROR})
    assert record["status"] == UNRESOLVABLE
    assert AUTHORITY_UNREACHABLE in record["flags"]


@pytest.mark.asyncio
async def test_arxiv_wrong_entry_id_is_unavailable_not_evidence():
    _, record, _, _ = await _annotate(ARXIV_URL, {ARXIV_API_URL: ARXIV_WRONG_ID})
    assert record["status"] == UNRESOLVABLE
    assert AUTHORITY_UNREACHABLE in record["flags"]


@pytest.mark.asyncio
async def test_arxiv_unnamespaced_feed_is_not_atom_proof():
    _, record, _, _ = await _annotate(ARXIV_URL, {ARXIV_API_URL: ARXIV_UNNAMESPACED})
    assert record["status"] == UNRESOLVABLE
    assert AUTHORITY_UNREACHABLE in record["flags"]


@pytest.mark.asyncio
async def test_arxiv_suffix_namespace_feed_is_not_atom_proof():
    _, record, _, _ = await _annotate(
        ARXIV_URL, {ARXIV_API_URL: ARXIV_SUFFIX_NAMESPACE}
    )
    assert record["status"] == UNRESOLVABLE
    assert AUTHORITY_UNREACHABLE in record["flags"]


@pytest.mark.asyncio
async def test_arxiv_requested_version_must_match_exactly():
    url = "https://arxiv.org/abs/2212.12345v2"
    api = "https://export.arxiv.org/api/query?id_list=2212.12345v2"
    mismatched = _arxiv_feed("http://arxiv.org/abs/2212.12345v1")
    _, record, _, _ = await _annotate(url, {api: mismatched})
    assert record["status"] == UNRESOLVABLE

    matched = _arxiv_feed("http://arxiv.org/abs/2212.12345v2")
    _, record, _, _ = await _annotate(url, {api: matched})
    assert record["status"] == TRUE


@pytest.mark.asyncio
async def test_arxiv_unversioned_request_accepts_canonical_versioned_entry():
    _, record, _, _ = await _annotate(ARXIV_URL, {ARXIV_API_URL: ARXIV_ATOM})
    assert record["status"] == TRUE
    assert EXISTENCE_ONLY in record["flags"]
    assert record["identifier"] == "2212.12345"


@pytest.mark.asyncio
async def test_arxiv_empty_valid_feed_remains_false_not_found():
    _, record, _, _ = await _annotate(ARXIV_URL, {ARXIV_API_URL: ARXIV_EMPTY})
    assert record["status"] == FALSE
    assert NOT_FOUND in record["flags"]


# --- Crossref identity: message.DOI must match the requested DOI -----------


def _crossref_message(**overrides):
    message = {
        "DOI": "10.1234/example",
        "title": ["Synthetic citation title"],
        "author": [{"family": "Rivera"}, {"family": "Chen"}],
    }
    message.update(overrides)
    return json.dumps({"status": "ok", "message": message}).encode("utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        _crossref_message(DOI=None),
        _crossref_message(DOI="10.1234/a-different-work"),
        _crossref_message(DOI=12345),
        _crossref_message(DOI="not a doi"),
        json.dumps({"status": "ok", "message": {"title": ["No identity"]}}).encode("utf-8"),
    ],
)
async def test_crossref_missing_or_conflicting_identity_is_unavailable(raw):
    url = "https://api.crossref.org/works/10.1234/example"
    _, record, _, _ = await _annotate(DOI_URL, {url: raw})
    assert record["status"] == UNRESOLVABLE
    assert EXISTENCE_ONLY not in record["flags"]


@pytest.mark.asyncio
async def test_crossref_matching_identity_is_true():
    url = "https://api.crossref.org/works/10.1234/example"
    _, record, _, _ = await _annotate(DOI_URL, {url: _crossref_message()})
    assert record["status"] == TRUE
    assert EXISTENCE_ONLY in record["flags"]


# --- Lookup bound is not externally expandable ------------------------------


def test_max_lookups_defaults_to_the_public_bound_and_cannot_be_raised():
    default = CitationVerificationSession(transport=RecordingTransport())
    assert MAX_LOOKUPS_PER_RUN == 10
    assert default.max_lookups == MAX_LOOKUPS_PER_RUN
    raised = CitationVerificationSession(
        transport=RecordingTransport(), max_lookups=10_000
    )
    assert raised.max_lookups == MAX_LOOKUPS_PER_RUN


@pytest.mark.asyncio
async def test_oversized_custom_max_lookups_still_bounds_actual_requests():
    transport = RecordingTransport()
    session = CitationVerificationSession(transport=transport, max_lookups=10_000)
    for i in range(MAX_LOOKUPS_PER_RUN + 1):
        transport.responses[f"https://api.crossref.org/works/10.1234/cap{i}"] = (
            _crossref_ok_bytes(f"10.1234/cap{i}")
        )
        await session.annotate(_finding(f"https://doi.org/10.1234/cap{i}"))
    assert len(transport.calls) == MAX_LOOKUPS_PER_RUN
    assert session.lookups == MAX_LOOKUPS_PER_RUN


# --- Parser edge cases fail unknown without losing the finding -------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xfe<feed",
        b'<?xml version="1.0" encoding="UTF-16"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>',
        b"<feed\x00/>",
        b"\x00\x01\x02",
    ],
)
async def test_unparseable_arxiv_payload_fails_unknown_without_raising(raw):
    finding, record, _, _ = await _annotate(ARXIV_URL, {ARXIV_API_URL: raw})
    assert record["status"] == UNRESOLVABLE
    assert AUTHORITY_UNREACHABLE in record["flags"]
    assert finding["citation_verification"] == record


@pytest.mark.asyncio
async def test_unparseable_crossref_unicode_fails_unknown_without_raising():
    url = "https://api.crossref.org/works/10.1234/example"
    _, record, _, _ = await _annotate(DOI_URL, {url: b"\xff\xfe\x00{"})
    assert record["status"] == UNRESOLVABLE
    assert AUTHORITY_UNREACHABLE in record["flags"]


@pytest.mark.asyncio
async def test_malformed_authority_payload_does_not_discard_the_finding(monkeypatch):
    payload = _patch_extraction(monkeypatch)
    transport = RecordingTransport({ARXIV_API_URL: b"\xff\xfe<feed"})
    researcher = DeepResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model",
        citation_verification=True,
        citation_transport=transport,
    )

    async def fake_llm(messages, **kwargs):
        return json.dumps(payload)

    researcher._llm = fake_llm
    finding = await researcher._fetch_and_extract(
        ARXIV_URL, "question", "Decorative title"
    )

    assert finding is not None
    assert finding["summary"] == payload["summary"]
    assert finding["citation_verification"]["status"] == UNRESOLVABLE


# --- Extractor cannot fabricate verification -------------------------------


FORGED_VERIFICATION = {
    "identifier": "10.9999/forged",
    "status": TRUE,
    "flags": [EXISTENCE_ONLY],
    "detail": "forged by the extractor",
    "matched_title": "Forged title",
}


async def _run_extraction(monkeypatch, url, *, enabled, transport, forged=True,
                          title="Decorative title"):
    payload = _patch_extraction(monkeypatch)
    if forged:
        payload["citation_verification"] = dict(FORGED_VERIFICATION)
    researcher = DeepResearcher(
        llm_endpoint="http://local.test/v1/chat/completions",
        llm_model="local-model",
        citation_verification=enabled,
        citation_transport=transport,
    )

    async def fake_llm(messages, **kwargs):
        return json.dumps(payload)

    researcher._llm = fake_llm
    return await researcher._fetch_and_extract(url, "question", title)


def _persisted(finding):
    from src.research_handler import ResearchHandler

    return ResearchHandler._extract_raw_findings([finding])


@pytest.mark.asyncio
async def test_disabled_run_discards_extractor_supplied_verification(monkeypatch):
    transport = RecordingTransport()
    finding = await _run_extraction(
        monkeypatch, DOI_URL, enabled=False, transport=transport
    )

    assert transport.calls == []
    assert "citation_verification" not in finding
    assert "citation_verification" not in _persisted(finding)[0]


@pytest.mark.asyncio
async def test_enabled_ordinary_url_discards_extractor_supplied_verification(monkeypatch):
    transport = RecordingTransport()
    finding = await _run_extraction(
        monkeypatch,
        "https://example.com/article",
        enabled=True,
        transport=transport,
    )

    assert transport.calls == []
    assert "citation_verification" not in finding
    assert "citation_verification" not in _persisted(finding)[0]


@pytest.mark.asyncio
async def test_enabled_academic_url_overwrites_forged_value_with_authority(monkeypatch):
    transport = RecordingTransport(
        {"https://api.crossref.org/works/10.1234/example": _crossref_ok_bytes()}
    )
    finding = await _run_extraction(
        monkeypatch, DOI_URL, enabled=True, transport=transport
    )

    record = finding["citation_verification"]
    assert record["status"] == TRUE
    assert record["identifier"] == "10.1234/example"
    assert record["matched_title"] == "Synthetic citation title"
    assert record != FORGED_VERIFICATION
    assert _persisted(finding)[0]["citation_verification"] == record


@pytest.mark.asyncio
async def test_enabled_academic_url_forged_true_cannot_survive_authoritative_false(monkeypatch):
    transport = RecordingTransport({ARXIV_API_URL: ARXIV_EMPTY})
    finding = await _run_extraction(
        monkeypatch, ARXIV_URL, enabled=True, transport=transport
    )

    record = finding["citation_verification"]
    assert record["status"] == FALSE
    assert NOT_FOUND in record["flags"]
    assert _persisted(finding)[0]["citation_verification"] == record
