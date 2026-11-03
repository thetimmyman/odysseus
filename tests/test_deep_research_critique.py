import asyncio
import json
import pytest

from src.deep_research import DeepResearcher
from src.research_critique import (
    DONE,
    SKIPPED_NO_CLAIMS,
    SKIPPED_SAME_FAMILY,
    GATE_NEEDS_HUMAN,
)
from src.research_handler import ResearchHandler


def _make_researcher(depth=None, critique=None, judge=None, judge_model=None, author_model="gpt-4o", run_record=False):
    r = DeepResearcher(
        llm_endpoint="http://localhost:8000/v1",
        llm_model=author_model,
        depth=depth,
        critique=critique,
        critique_judge=judge,
        critique_judge_model=judge_model,
        run_record=run_record,
    )
    # Stub _llm to avoid network calls during loop
    async def fake_llm(*args, **kwargs):
        return "Fake LLM answer"
    r._llm = fake_llm
    return r


@pytest.mark.asyncio
async def test_critique_disabled_by_default_in_standard_mode():
    judge_called = False

    def dummy_judge(prompt):
        nonlocal judge_called
        judge_called = True
        return json.dumps({"critiques": []})

    r = _make_researcher(depth="standard", judge=dummy_judge, judge_model="claude-3-5-sonnet")
    r.findings = [{"summary": "Fact 1", "quotes": ["quote 1"]}]

    async def dummy_final(q, rep):
        return "Final Report Text"

    r._final_report = dummy_final

    report = await r.research("test question", prior_report="Interim report")
    assert report == "Final Report Text"
    assert not judge_called
    assert r.critique_result is None
    stats = r.get_stats()
    assert "Critique" not in stats


@pytest.mark.asyncio
async def test_critique_enabled_by_default_in_critical_mode():
    judge_called = False

    def dummy_judge(prompt):
        nonlocal judge_called
        judge_called = True
        return json.dumps({
            "critiques": [
                {
                    "claim_id": "C1",
                    "verdict": "SUPPORTED",
                    "severity": None,
                    "reason": "Source supports the claim",
                    "alternative": None,
                }
            ]
        })

    r = _make_researcher(depth="critical", judge=dummy_judge, judge_model="claude-3-5-sonnet", author_model="gpt-4o")
    r.findings = [{"summary": "Fact 1", "quotes": ["quote 1"]}]

    async def dummy_final(q, rep):
        return "Final Report Text"

    r._final_report = dummy_final

    report = await r.research("test question", prior_report="Interim report")
    assert judge_called
    assert r.critique_result is not None
    assert r.critique_result.status == DONE
    assert r.critique_result.critical_claim_ids == ()
    stats = r.get_stats()
    assert stats["Critique"] == DONE
    assert "CriticalFindings" not in stats
    # No critical contradiction, so report is unchanged
    assert report == "Final Report Text"


@pytest.mark.asyncio
async def test_critique_explicit_opt_in_and_opt_out():
    judge_called = False

    def dummy_judge(prompt):
        nonlocal judge_called
        judge_called = True
        return json.dumps({"critiques": []})

    # Explicit opt-in on standard mode
    r_on = _make_researcher(depth="standard", critique=True, judge=dummy_judge, judge_model="claude-3-5-sonnet")
    r_on.findings = [{"summary": "Fact 1", "quotes": ["quote 1"]}]
    r_on._final_report = lambda q, rep: asyncio.sleep(0, result="Report")
    await r_on.research("q", prior_report="rep")
    assert judge_called

    # Explicit opt-out on critical mode
    judge_called = False
    r_off = _make_researcher(depth="critical", critique=False, judge=dummy_judge, judge_model="claude-3-5-sonnet")
    r_off.findings = [{"summary": "Fact 1", "quotes": ["quote 1"]}]
    r_off._final_report = lambda q, rep: asyncio.sleep(0, result="Report")
    await r_off.research("q", prior_report="rep")
    assert not judge_called
    assert r_off.critique_result is None


@pytest.mark.asyncio
async def test_critique_same_family_refusal():
    judge_called = False

    def dummy_judge(prompt):
        nonlocal judge_called
        judge_called = True
        return json.dumps({"critiques": []})

    # Both author and judge are OpenAI (gpt-4o vs gpt-4o-mini)
    r = _make_researcher(depth="critical", judge=dummy_judge, judge_model="gpt-4o-mini", author_model="gpt-4o")
    r.findings = [{"summary": "Fact 1", "quotes": ["quote 1"]}]
    r._final_report = lambda q, rep: asyncio.sleep(0, result="Report")

    await r.research("q", prior_report="rep")
    assert not judge_called
    assert r.critique_result is not None
    assert r.critique_result.status == SKIPPED_SAME_FAMILY
    assert r.get_stats()["Critique"] == SKIPPED_SAME_FAMILY


@pytest.mark.asyncio
async def test_critique_skipped_when_no_claims():
    judge_called = False

    def dummy_judge(prompt):
        nonlocal judge_called
        judge_called = True
        return json.dumps({"critiques": []})

    r = _make_researcher(depth="critical", judge=dummy_judge, judge_model="claude-3-5-sonnet", author_model="gpt-4o")
    r.findings = []
    r._final_report = lambda q, rep: asyncio.sleep(0, result="Report")

    await r.research("q", prior_report="rep")
    assert not judge_called
    assert r.critique_result is not None
    assert r.critique_result.status == SKIPPED_NO_CLAIMS


@pytest.mark.asyncio
async def test_critique_critical_contradiction_appends_section_and_records_stats():
    def dummy_judge(prompt):
        return json.dumps({
            "critiques": [
                {
                    "claim_id": "C1",
                    "verdict": "CONTRADICTED",
                    "severity": "CRITICAL",
                    "reason": "The cited study actually found the opposite result.",
                    "alternative": None,
                },
                {
                    "claim_id": "C2",
                    "verdict": "ALTERNATIVE",
                    "severity": "CRITICAL",
                    "reason": "The effect is better explained by confounding variables.",
                    "alternative": "Age and smoking account for the observed correlation.",
                },
            ]
        })

    r = _make_researcher(
        depth="critical",
        judge=dummy_judge,
        judge_model="claude-3-5-sonnet",
        author_model="gpt-4o",
        run_record=True,
    )
    # Add findings with source so run_recorder keeps them
    src_id = r.run_recorder.add_source("http://ex.test", "Study", "text")
    f1 = {"summary": "Drug X cures condition Y", "quotes": ["Drug X had 90% response rate"], "source_id": src_id}
    f2 = {"summary": "Correlation between A and B", "quotes": ["A was correlated with B"], "source_id": src_id}
    r.run_recorder.add_finding(f1)
    r.run_recorder.add_finding(f2)
    r.findings = [f1, f2]
    r._final_report = lambda q, rep: asyncio.sleep(0, result="# Final Report\n\nDrug X is effective.")

    report = await r.research("q", prior_report="rep")
    assert r.critique_result is not None
    assert r.critique_result.status == DONE
    assert r.critique_result.critical_claim_ids == ("C1", "C2")

    # Verify report contains Adversarial Critique section
    assert "## Adversarial Critique" in report
    assert "[!WARNING]" in report
    assert "The cited study actually found the opposite result." in report
    assert "The effect is better explained by confounding variables." in report
    assert "Age and smoking account for the observed correlation." in report

    # Verify stats
    stats = r.get_stats()
    assert stats["Critique"] == DONE
    assert stats["CriticalFindings"] == 2

    # Verify run record includes serialized critique
    rec = r.run_recorder.build()
    assert "critique" in rec
    assert rec["critique"]["status"] == DONE
    assert rec["critique"]["critical_claim_ids"] == ["C1", "C2"]
    assert rec["critique"]["gate_effect"] == GATE_NEEDS_HUMAN


@pytest.mark.asyncio
async def test_research_handler_wires_critique(monkeypatch):
    handler = ResearchHandler()

    def dummy_judge(prompt):
        return json.dumps({
            "critiques": [
                {
                    "claim_id": "C1",
                    "verdict": "SUPPORTED",
                    "severity": None,
                    "reason": "Matches",
                    "alternative": None,
                }
            ]
        })

    task_entry = {}
    async def fake_probe(*args, **kwargs):
        return True

    handler._probe_endpoint = fake_probe

    # Monkeypatch DeepResearcher._llm so it does not make real network calls
    async def fake_llm(self, *args, **kwargs):
        return "Fake synthesized report"

    monkeypatch.setattr(DeepResearcher, "_llm", fake_llm)

    report = await handler.call_research_service(
        query="test",
        llm_endpoint="http://localhost:8000/v1",
        llm_model="meta-llama/Llama-3-70b",
        depth="critical",
        critique=True,
        critique_judge=dummy_judge,
        critique_judge_model="claude-3-5-sonnet",
        prior_report="Initial report",
        prior_findings=[{"summary": "Evidence A", "quotes": ["quote A"]}],
        _task_entry=task_entry,
    )

    assert report is not None
    assert "critique" in task_entry
    assert task_entry["critique"]["status"] == DONE
    assert task_entry["critique"]["judge_family"] == "anthropic"
    assert task_entry["critique"]["author_family"] == "meta"
