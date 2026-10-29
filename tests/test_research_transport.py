"""Isolated replay adapters exercise the public research loop and run record."""
import json

import pytest

from src.deep_research import DeepResearcher
from src.research_run_record import grounding_view
from src.research_transport import ResearchModelReply


PAGE = "The synthetic instrument has a measured limit of seven units."


class RecordedTransport:
    async def generate(self, *, model, messages, temperature, max_tokens, timeout):
        prompt = "\n".join(m["content"] for m in messages)
        if "research strategist" in prompt:
            text = '{"sub_questions":["instrument limit"],"key_topics":["measurement"],"success_criteria":"source-backed limit"}'
        elif "planning web searches" in prompt:
            text = '["instrument limit"]'
        elif "deciding whether" in prompt:
            text = "YES — The measured limit is sourced."
        elif "updating an evolving" in prompt or "Write a **long" in prompt:
            text = "The measured limit is seven units. [C1] [instrument](https://example.test/instrument)."
        else:
            text = json.dumps({"summary": "Measured instrument limit", "evidence": PAGE, "quotes": [PAGE]})
        return ResearchModelReply(text, {"input_tokens": 10, "output_tokens": 5})

    async def search(self, query, count):
        return [{"url": "https://example.test/instrument", "title": "Synthetic instrument"}]

    async def fetch(self, url, timeout):
        return {"success": True, "content": PAGE, "title": "Synthetic instrument"}


@pytest.mark.asyncio
async def test_replay_uses_the_real_loop_and_keeps_source_text_and_observed_usage(tmp_path):
    researcher = DeepResearcher(
        llm_endpoint="transport:synthetic", llm_model="synthetic-model",
        transport=RecordedTransport(), category="factcheck", max_rounds=1,
        min_rounds=1, run_record=True,
    )
    report = await researcher.research("What is the measured instrument limit?")
    record = researcher.run_recorder.build(blob_dir=tmp_path / "blobs")
    view = grounding_view(record, blob_root=tmp_path)
    assert "seven units" in report
    assert view["sources"][0]["text"] == PAGE
    assert view["claims"][0]["citations"][0]["quote"] == PAGE
    assert record["llm_calls"]
    assert all(c["input_tokens"] == 10 and c["output_tokens"] == 5
               and c["tokens_estimated"] is False for c in record["llm_calls"])


def test_incomplete_transport_is_refused_before_a_research_run():
    with pytest.raises(TypeError, match="generate.*search.*fetch"):
        DeepResearcher(llm_endpoint="transport:synthetic", llm_model="synthetic-model", transport=object())


@pytest.mark.asyncio
async def test_missing_provider_usage_remains_estimated_instead_of_a_zero_cost_cache_hit(tmp_path):
    class UnmeasuredTransport(RecordedTransport):
        async def generate(self, **kwargs):
            reply = await super().generate(**kwargs)
            return ResearchModelReply(reply.text)

    researcher = DeepResearcher(
        llm_endpoint="transport:synthetic", llm_model="synthetic-model",
        transport=UnmeasuredTransport(), category="factcheck", max_rounds=1,
        min_rounds=1, run_record=True,
    )
    await researcher.research("What is the measured instrument limit?")
    calls = researcher.run_recorder.build(blob_dir=tmp_path / "blobs")["llm_calls"]
    assert calls
    assert all(c["tokens_estimated"] is True and c["input_tokens"] > 0 and not c.get("cached") for c in calls)


@pytest.mark.parametrize("usage", [
    {}, {"input_tokens": 1}, {"input_tokens": True, "output_tokens": 1},
    {"input_tokens": -1, "output_tokens": 1}, {"input_tokens": 1, "output_tokens": "2"},
])
def test_incomplete_or_invalid_usage_is_not_accepted_as_observed(usage):
    with pytest.raises(ValueError):
        ResearchModelReply("answer", usage)
