"""Research limits bound actual source reads through the public research loop."""
import json

import pytest

from src.deep_research import DeepResearcher
from src.research_transport import ResearchModelReply


SOURCE_TEXT = "The synthetic instrument reports a measured capacity of seven units."


class SourceBudgetTransport:
    def __init__(self):
        self.query_round = 0
        self.fetched = []

    async def generate(self, *, model, messages, temperature, max_tokens, timeout):
        prompt = "\n".join(message["content"] for message in messages)
        if "research strategist" in prompt:
            text = '{"sub_questions":["instrument capacity"],"key_topics":["measurement"],"success_criteria":"bounded evidence"}'
        elif "planning web searches" in prompt:
            self.query_round += 1
            text = json.dumps([f"round-{self.query_round}-topic-{index}" for index in range(4)])
        elif "deciding whether" in prompt:
            text = "NO — Continue within the configured limits."
        elif "updating an evolving" in prompt or "Write a **long" in prompt:
            text = "The instrument reports seven units, according to the supplied evidence."
        else:
            text = json.dumps({"summary": "Measured instrument capacity", "evidence": SOURCE_TEXT,
                               "quotes": [SOURCE_TEXT]})
        return ResearchModelReply(text, {"input_tokens": 10, "output_tokens": 5})

    async def search(self, query, count):
        return [{"url": f"https://example.test/{query}/{index}", "title": "Synthetic instrument"}
                for index in range(count)]

    async def fetch(self, url, timeout):
        self.fetched.append(url)
        return {"success": True, "content": SOURCE_TEXT, "title": "Synthetic instrument"}


@pytest.mark.asyncio
async def test_round_url_limit_caps_fetches_across_all_queries():
    transport = SourceBudgetTransport()
    researcher = DeepResearcher(
        llm_endpoint="transport:synthetic", llm_model="synthetic-model",
        transport=transport, category="factcheck", max_rounds=1, min_rounds=1,
        max_urls_per_round=3,
    )
    report = await researcher.research("What is the instrument capacity?")
    assert "seven units" in report
    assert len(transport.fetched) == 3
    assert len(researcher.findings) == 3


@pytest.mark.asyncio
async def test_each_round_has_its_own_url_allowance_without_refetching_prior_sources():
    class RepeatedSourceTransport(SourceBudgetTransport):
        async def search(self, query, count):
            shared = [{"url": f"https://example.test/shared/{index}", "title": "Shared source"}
                      for index in range(2)]
            return shared + await super().search(query, count)

    transport = RepeatedSourceTransport()
    researcher = DeepResearcher(
        llm_endpoint="transport:synthetic", llm_model="synthetic-model",
        transport=transport, category="factcheck", depth="quick", min_rounds=2,
    )
    await researcher.research("What is the instrument capacity?")
    assert len(transport.fetched) == 6
    assert len(set(transport.fetched)) == 6


@pytest.mark.asyncio
async def test_explicit_url_override_controls_actual_reads_with_a_larger_depth_preset():
    transport = SourceBudgetTransport()
    researcher = DeepResearcher(
        llm_endpoint="transport:synthetic", llm_model="synthetic-model",
        transport=transport, category="factcheck", depth="deep", max_rounds=1,
        min_rounds=1, max_urls_per_round=2,
    )
    await researcher.research("What is the instrument capacity?")
    assert len(transport.fetched) == 2


@pytest.mark.asyncio
async def test_duplicate_hits_and_failed_fetches_keep_the_attempt_budget_bounded():
    class DuplicateAndFailedTransport(SourceBudgetTransport):
        async def search(self, query, count):
            return [{"url": url, "title": "Synthetic instrument"} for url in (
                "https://example.test/failed", "https://example.test/failed",
                "https://example.test/second", "https://example.test/third",
                "https://example.test/fourth",
            )]

        async def fetch(self, url, timeout):
            page = await super().fetch(url, timeout)
            return {"success": False} if url.endswith("/failed") else page

    transport = DuplicateAndFailedTransport()
    researcher = DeepResearcher(
        llm_endpoint="transport:synthetic", llm_model="synthetic-model",
        transport=transport, category="factcheck", max_rounds=1, min_rounds=1,
        max_urls_per_round=3,
    )
    report = await researcher.research("What is the instrument capacity?")
    assert "seven units" in report
    assert len(transport.fetched) == 3
    assert set(transport.fetched) == {
        "https://example.test/failed", "https://example.test/second", "https://example.test/third",
    }
