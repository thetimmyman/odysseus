"""Verbatim extractor quotes, claim-id citations, and measured grounding.

The extractor prompt asks for an array of exact page spans (``quotes``); the
run record prefers those spans over the older free-text ``evidence``, stamps
each finding with a claim id as it is added, and measures how many quotes
occur verbatim in their source. The final-report prompt lists the supplied
claim ids and forbids inventing others. Record-off output is unchanged.
"""
import asyncio
import json
import sys
import types

import pytest

from src.deep_research import DeepResearcher
from src.research_run_record import (
    grounding_view,
    normalize_quotes,
    quote_grounding_stats,
)

PAGE = (
    "Odysseus keeps research runs on disk.\n\n"
    "The extractor reads the page and returns evidence. "
    "Retrieval happens before extraction, and the hash covers the extracted text."
)
EVIDENCE = "Retrieval happens before extraction"
SUMMARY = "Retrieval precedes extraction."
_MISSING = object()


@pytest.fixture
def fake_page(monkeypatch):
    """A fixed page for every fetch, resolved without a worker thread."""
    search_mod = types.ModuleType("src.search")
    search_mod.fetch_webpage_content = lambda url, timeout: {
        "success": True, "content": PAGE, "title": "Page title"}
    monkeypatch.setitem(sys.modules, "src.search", search_mod)

    async def immediate_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", immediate_to_thread)


def _reply(quotes=_MISSING, evidence=EVIDENCE, summary=SUMMARY):
    payload = {"rational": "relevant", "evidence": evidence, "summary": summary}
    if quotes is not _MISSING:
        payload["quotes"] = quotes
    return json.dumps(payload)


async def _extract(reply, *, run_record=True, url="https://example.test/a", title="A"):
    researcher = DeepResearcher(llm_endpoint="http://local.test/v1/chat/completions",
                                llm_model="local-model", run_record=run_record)

    async def fake_llm(messages, **kwargs):
        return reply

    researcher._llm = fake_llm
    finding = await researcher._fetch_and_extract(url, "q", title)
    researcher.findings.append(finding)
    return researcher, finding


@pytest.mark.asyncio
async def test_verbatim_quotes_are_recorded_and_grounded(fake_page, tmp_path):
    quotes = [EVIDENCE, "the hash covers the extracted text"]
    researcher, finding = await _extract(_reply(quotes))

    record = researcher.run_recorder.build(blob_dir=tmp_path / "blobs")

    [claim] = record["claims"]
    assert [c["quote"] for c in claim["citations"]] == quotes
    assert claim["claim_id"] == finding["claim_id"] == "C1"
    assert record["totals"]["quote_grounding"] == {
        "quotes": 2, "grounded": 2, "not_found": 0, "rate": 1.0}
    # The checker's input shape (texts read back from the stored blobs) agrees.
    view = grounding_view(record, blob_root=tmp_path)
    assert quote_grounding_stats(view, view["sources"])["rate"] == 1.0


@pytest.mark.asyncio
async def test_paraphrased_and_fabricated_quotes_are_not_found(fake_page, tmp_path):
    quotes = ["The page says extraction comes after retrieval.",
              "Odysseus stores research runs in a relational database."]
    researcher, finding = await _extract(_reply(quotes))

    record = researcher.run_recorder.build(blob_dir=tmp_path / "blobs")
    stats = record["totals"]["quote_grounding"]

    assert [c["quote"] for c in record["claims"][0]["citations"]] == quotes
    assert stats["quotes"] == 2
    assert stats["not_found"] == 2
    assert stats["grounded"] == 0
    assert stats["rate"] < 1.0
    assert finding["claim_id"] == "C1"
    assert [c["claim_id"] for c in record["claims"]] == ["C1"]


def test_normalize_quotes_never_raises_on_garbage():
    assert normalize_quotes(None) == []
    assert normalize_quotes(42) == []
    assert normalize_quotes({"quote": "not a container"}) == []
    assert normalize_quotes([1, None, {"x": 1}, "  "]) == []
    assert normalize_quotes("A single span") == ["A single span"]
    assert normalize_quotes([["a"], [["b", ["c"]]]]) == ["a", "b", "c"]


@pytest.mark.asyncio
@pytest.mark.parametrize("quotes, expected", [
    (_MISSING, [EVIDENCE]),
    ("A single verbatim span", ["A single verbatim span"]),
    ([["Nested one"], ["Nested two", ["Nested three"]]],
     ["Nested one", "Nested two", "Nested three"]),
    ({"quote": "not a container"}, [EVIDENCE]),
    ([], []),
], ids=["absent", "string", "nested-list", "garbage", "explicit-empty"])
async def test_recorder_prefers_quotes_and_falls_back_to_evidence(
        fake_page, tmp_path, quotes, expected):
    researcher, _ = await _extract(_reply(quotes))

    record = researcher.run_recorder.build(blob_dir=tmp_path / "blobs")

    assert [c["quote"] for c in record["claims"][0]["citations"]] == expected


def test_whitespace_differences_ground_but_changes_do_not():
    record = {"claims": [{"claim_id": "C1", "citations": [
        # whitespace-only differences still ground
        {"source_id": "S1", "quote": "Retrieval   happens\nbefore extraction"},
        # a changed word does not
        {"source_id": "S1", "quote": "Retrieval happens after extraction"},
        # comparison is case-sensitive
        {"source_id": "S1", "quote": "retrieval happens before extraction"},
    ]}]}

    stats = quote_grounding_stats(record, {"S1": PAGE})

    assert stats == {"quotes": 3, "grounded": 1, "not_found": 2, "rate": 0.3333}


@pytest.mark.asyncio
async def test_formatted_findings_carry_the_ids_build_emits(fake_page, tmp_path):
    researcher, first = await _extract(_reply(["Retrieval happens before extraction"]))

    async def second_llm(messages, **kwargs):
        return _reply(["the hash covers the extracted text"])

    researcher._llm = second_llm
    second = await researcher._fetch_and_extract(
        "https://example.test/b", "q", "B")
    researcher.findings.append(second)

    rendered = researcher._format_findings(researcher.findings)
    claims = researcher.run_recorder.build(blob_dir=tmp_path / "blobs")["claims"]

    assert [c["claim_id"] for c in claims] == ["C1", "C2"]
    assert [first["claim_id"], second["claim_id"]] == ["C1", "C2"]
    for claim in claims:
        assert f"[{claim['claim_id']}]" in rendered


@pytest.mark.asyncio
async def test_record_off_keeps_the_old_finding_shape_and_format(fake_page):
    researcher, finding = await _extract(_reply(["Retrieval happens before extraction"]),
                                         run_record=False)

    assert "claim_id" not in finding
    assert "source_id" not in finding
    assert researcher._format_findings([finding]) == (
        "**Finding 1** — [A](https://example.test/a)\n" + SUMMARY)


@pytest.mark.asyncio
async def test_final_report_prompt_lists_supplied_claim_ids(fake_page, tmp_path):
    researcher, _ = await _extract(_reply(["Retrieval happens before extraction"]))

    async def second_llm(messages, **kwargs):
        return _reply(["the hash covers the extracted text"])

    researcher._llm = second_llm
    researcher.findings.append(await researcher._fetch_and_extract(
        "https://example.test/b", "q", "B"))

    prompts = []

    async def final_llm(messages, **kwargs):
        prompts.append(messages[-1]["content"])
        return "word " * 600  # long enough to skip the expansion retry

    researcher._llm = final_llm
    await researcher._final_report("q", "evolving report")

    [prompt] = prompts
    assert "[C1]" in prompt and "[C2]" in prompt
    assert "Never invent a claim id" in prompt
    assert "https://example.test/a" in prompt and "https://example.test/b" in prompt


@pytest.mark.asyncio
async def test_final_report_prompt_omits_claim_ids_when_record_is_off(fake_page):
    researcher, _ = await _extract(_reply(["Retrieval happens before extraction"]),
                                   run_record=False)
    prompts = []

    async def final_llm(messages, **kwargs):
        prompts.append(messages[-1]["content"])
        return "word " * 600

    researcher._llm = final_llm
    await researcher._final_report("q", "evolving report")

    [prompt] = prompts
    assert "[C1]" not in prompt
    assert "claim id" not in prompt.lower()
