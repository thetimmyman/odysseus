"""Tests for src.research_evaluation: skeleton, fill, overlap, validator, render.

No model call and no network: the incumbent search is injected everywhere it
is used, and a search that fails must say so instead of looking filled.
"""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.pinned_source import NOT_SEARCHED, TRUE, UNPINNED, PinResult
from src.research_evaluation import (
    DRAFT_FOOTER,
    FILLED,
    RULING_SECTIONS,
    SKIPPED,
    UNKNOWN,
    RulingSection,
    fill_section,
    find_incumbent_overlap,
    new_ruling_skeleton,
    render_ruling_markdown,
    validate_ruling,
)

CANDIDATE = "candidate-x"
QUESTION = "Should TMOS adopt candidate-x?"


def _skeleton():
    return new_ruling_skeleton(CANDIDATE, QUESTION)


def _with_section(draft, name, section):
    """A NEW draft with one section replaced (a draft's sections are read-only)."""
    sections = dict(draft.sections)
    sections[name] = section
    return replace(draft, sections=sections)


def _filled(text="evidence-backed text", refs=("tools/tmos",)):
    return RulingSection(FILLED, text, tuple(refs))


def _clean_draft(verdict="adopt"):
    """A draft that satisfies every rule; one test proves it validates clean."""
    draft = _skeleton().with_verdict(verdict)
    for name in RULING_SECTIONS:
        draft = _with_section(draft, name, _filled(f"{name} text", (f"{name}.ref",)))
    return draft


def _section_bodies(markdown):
    """{section name: [non-blank body lines]} for a rendered ruling."""
    bodies = {}
    for chunk in markdown.split("## ")[1:]:
        head, _, body = chunk.partition("\n")
        lines = [line for line in body.splitlines() if line.strip()]
        if lines and lines[-1] == DRAFT_FOOTER:
            lines.pop()
        bodies[head] = lines
    return bodies


# --- Skeleton ----------------------------------------------------------------
def test_skeleton_has_the_eight_house_sections_in_order():
    assert RULING_SECTIONS == (
        "ruling",
        "pinned_source_identity",
        "licence",
        "incumbent_overlap",
        "integration_cost",
        "results_and_controls",
        "authority_reconsideration_removal",
        "reproduction",
    )
    draft = _skeleton()
    assert tuple(draft.sections) == RULING_SECTIONS
    assert draft.verdict is None
    for name in RULING_SECTIONS:
        section = draft.sections[name]
        assert section.status == UNKNOWN
        assert section.text == ""
        assert section.evidence_refs == ()


def test_skeleton_stores_candidate_and_question_verbatim():
    draft = new_ruling_skeleton("  Candidate-X  v1.2 ", "Should we adopt it?  ")
    assert draft.candidate == "  Candidate-X  v1.2 "
    assert draft.question == "Should we adopt it?  "


def test_draft_sections_cannot_be_edited_in_place():
    draft = _skeleton()
    with pytest.raises(TypeError):
        draft.sections["licence"] = _filled()


def test_draft_section_accessor_rejects_unknown_names():
    with pytest.raises(ValueError):
        _skeleton().section("licence_terms")


# --- fill_section ------------------------------------------------------------
def test_fill_section_with_evidence_is_filled():
    draft = fill_section(
        _skeleton(), "licence", "MIT, per the pinned source.", ["tools/tmos/LICENSE"]
    )
    section = draft.sections["licence"]
    assert section.status == FILLED
    assert section.text == "MIT, per the pinned source."
    assert section.evidence_refs == ("tools/tmos/LICENSE",)


def test_fill_section_without_evidence_stays_unknown_and_drops_the_text():
    draft = fill_section(_skeleton(), "licence", "MIT, probably.", [])
    section = draft.sections["licence"]
    assert section.status == UNKNOWN
    assert section.text == ""
    assert section.evidence_refs == ()
    # blank refs are not evidence either
    blank = fill_section(_skeleton(), "licence", "MIT.", ["", "   "])
    assert blank.sections["licence"].status == UNKNOWN


def test_fill_section_never_mutates_the_input_draft():
    draft = _skeleton()
    before = {name: draft.sections[name] for name in RULING_SECTIONS}
    fill_section(draft, "licence", "MIT.", ["tools/tmos/LICENSE"])
    assert {name: draft.sections[name] for name in RULING_SECTIONS} == before
    assert draft.sections["licence"].status == UNKNOWN


@pytest.mark.parametrize("name", ["", "Licence", "incumbent", "unknown_section"])
def test_fill_section_rejects_an_unknown_section_name(name):
    with pytest.raises(ValueError):
        fill_section(_skeleton(), name, "text", ["ref"])


@pytest.mark.parametrize("text", ["", "   ", "\n\t "])
def test_fill_section_rejects_blank_text(text):
    with pytest.raises(ValueError):
        fill_section(_skeleton(), "licence", text, ["ref"])


# --- Incumbent overlap -------------------------------------------------------
INCUMBENT = {"name": "tmos-gate", "owner": "platform", "source": "tools/tmos/gate.py"}


def test_overlap_without_a_search_is_skipped():
    result = find_incumbent_overlap(CANDIDATE)
    assert result.status == SKIPPED
    assert result.matches == ()
    assert "no overlap search" in result.section_text


def test_overlap_lists_a_found_incumbent():
    calls = []

    def search(candidate):
        calls.append(candidate)
        return [INCUMBENT]

    result = find_incumbent_overlap(CANDIDATE, search=search)
    assert calls == [CANDIDATE]
    assert result.status == FILLED
    assert result.matches == (INCUMBENT,)
    assert "tmos-gate" in result.section_text
    assert "platform" in result.section_text
    assert "tools/tmos/gate.py" in result.section_text
    assert result.section_refs == ("tools/tmos/gate.py",)


def test_overlap_result_fills_the_incumbent_section():
    result = find_incumbent_overlap(CANDIDATE, search=lambda candidate: [INCUMBENT])
    draft = fill_section(
        _skeleton(), "incumbent_overlap", result.section_text, result.section_refs
    )
    assert draft.sections["incumbent_overlap"].status == FILLED
    assert "tmos-gate" in draft.sections["incumbent_overlap"].text


def test_overlap_search_failure_is_unknown_and_names_the_exception():
    def search(candidate):
        raise RuntimeError("connection to private-host failed")

    result = find_incumbent_overlap(CANDIDATE, search=search)
    assert result.status == UNKNOWN
    assert result.matches == ()
    assert "RuntimeError" in result.section_text
    # a failure is never an all-clear, and the message may carry private text
    assert "private-host" not in result.section_text


def test_overlap_search_without_a_match_is_unknown_not_filled():
    result = find_incumbent_overlap(CANDIDATE, search=lambda candidate: [])
    assert result.status == UNKNOWN
    assert "no match" in result.section_text


def test_overlap_search_with_a_non_list_result_is_unknown():
    result = find_incumbent_overlap(CANDIDATE, search=lambda candidate: INCUMBENT)
    assert result.status == UNKNOWN
    assert "dict" in result.section_text


# --- Validator ---------------------------------------------------------------
def test_clean_draft_returns_no_problems():
    # Positive control for every negative case below: the same validator, on a
    # draft of the same shape with nothing broken, returns an empty list.
    assert validate_ruling(_clean_draft("adopt")) == []
    assert validate_ruling(_clean_draft("reference")) == []
    assert validate_ruling(_skeleton()) == []


def test_unknown_verdict_is_reported():
    draft = _clean_draft("adopt").with_verdict("buy")
    problems = validate_ruling(draft)
    assert len(problems) == 1
    assert "buy" in problems[0]
    # the same draft with a house verdict is clean
    assert validate_ruling(draft.with_verdict("adopt")) == []


@pytest.mark.parametrize("verdict", ["adopt", "adapt", "build"])
def test_proposal_while_incumbent_overlap_is_skipped_is_reported(verdict):
    # Negative control: verdict adopt + incumbent_overlap still SKIPPED.
    draft = _with_section(_clean_draft(verdict), "incumbent_overlap", RulingSection(SKIPPED))
    problems = validate_ruling(draft)
    assert len(problems) == 1
    assert "incumbent_overlap" in problems[0]
    assert SKIPPED in problems[0]


@pytest.mark.parametrize("verdict", ["adopt", "adapt", "build"])
def test_proposal_while_incumbent_overlap_is_unknown_is_reported(verdict):
    draft = _with_section(_clean_draft(verdict), "incumbent_overlap", RulingSection())
    problems = validate_ruling(draft)
    assert len(problems) == 1
    assert "incumbent_overlap" in problems[0]
    assert UNKNOWN in problems[0]


def test_reference_needs_no_overlap_search():
    draft = _with_section(_clean_draft("reference"), "incumbent_overlap", RulingSection(SKIPPED))
    assert validate_ruling(draft) == []


@pytest.mark.parametrize("verdict", ["adopt", "adapt"])
def test_adoption_without_a_pinned_identity_is_reported(verdict):
    draft = _with_section(_clean_draft(verdict), "pinned_source_identity", RulingSection())
    problems = validate_ruling(draft)
    assert len(problems) == 1
    assert "pinned_source_identity" in problems[0]


def test_build_without_a_pinned_identity_is_allowed():
    draft = _with_section(_clean_draft("build"), "pinned_source_identity", RulingSection())
    assert validate_ruling(draft) == []


def test_filled_section_without_evidence_refs_is_reported():
    draft = _with_section(_skeleton(), "licence", RulingSection(FILLED, "MIT.", ()))
    assert validate_ruling(draft) == ["section 'licence' is FILLED without evidence refs"]
    # one ref on the same section and the problem is gone
    assert validate_ruling(_with_section(_skeleton(), "licence", _filled())) == []


def test_unpinned_pin_result_under_a_filled_identity_is_reported():
    pin = PinResult(TRUE, [UNPINNED], "branch ref can drift")
    problems = validate_ruling(_clean_draft("adopt"), pin_results=[pin])
    assert len(problems) == 1
    assert "pinned_source_identity" in problems[0]
    assert UNPINNED in problems[0]
    assert TRUE in problems[0]
    # control: the same unpinned result, but the identity section is not filled
    quiet = _with_section(_clean_draft("reference"), "pinned_source_identity", RulingSection())
    assert validate_ruling(quiet, pin_results=[pin]) == []


def test_a_pinned_result_without_the_flag_is_not_reported():
    pin = PinResult(TRUE, [], "commit sha")
    assert validate_ruling(_clean_draft("adopt"), pin_results=[pin]) == []


def test_pin_result_naming_its_section_is_checked_against_that_section():
    pin = SimpleNamespace(status=NOT_SEARCHED, flags=[UNPINNED], section="reproduction")
    problems = validate_ruling(_clean_draft("reference"), pin_results=[pin])
    assert len(problems) == 1
    assert "reproduction" in problems[0]
    # the same pin with that section not filled is silent
    quiet = _with_section(_clean_draft("reference"), "reproduction", RulingSection(SKIPPED))
    assert validate_ruling(quiet, pin_results=[pin]) == []


def test_each_broken_rule_gets_its_own_message():
    draft = _clean_draft("adopt")
    draft = _with_section(draft, "incumbent_overlap", RulingSection(SKIPPED))
    draft = _with_section(draft, "pinned_source_identity", RulingSection())
    problems = validate_ruling(draft)
    assert len(problems) == 2
    assert any("incumbent_overlap" in problem for problem in problems)
    assert any("pinned_source_identity" in problem for problem in problems)


# --- Renderer ----------------------------------------------------------------
def test_render_passes_unfilled_sections_as_status_words_only():
    markdown = render_ruling_markdown(_skeleton())
    bodies = _section_bodies(markdown)
    assert list(bodies) == list(RULING_SECTIONS)
    for name in RULING_SECTIONS:
        assert bodies[name] == [UNKNOWN]
    assert DRAFT_FOOTER not in bodies


def test_render_prints_skipped_as_skipped():
    draft = _with_section(_skeleton(), "incumbent_overlap", RulingSection(SKIPPED))
    assert _section_bodies(render_ruling_markdown(draft))["incumbent_overlap"] == [SKIPPED]


def test_render_starts_with_the_bold_ruling_line_and_ends_with_the_footer():
    markdown = render_ruling_markdown(_clean_draft("adopt"))
    assert markdown.splitlines()[0] == "**Ruling: adopt**"
    assert markdown.splitlines()[-1] == DRAFT_FOOTER


def test_render_of_an_unset_verdict_is_unknown():
    assert render_ruling_markdown(_skeleton()).splitlines()[0] == f"**Ruling: {UNKNOWN}**"


def test_render_shows_a_filled_sections_text_and_evidence():
    body = _section_bodies(render_ruling_markdown(_clean_draft("defer")))["licence"]
    assert body[0] == "licence text"
    assert body[1] == "Evidence: licence.ref"


def test_render_is_deterministic():
    first = render_ruling_markdown(_clean_draft("adopt"))
    second = render_ruling_markdown(_clean_draft("adopt"))
    assert first == second
    assert first.encode("utf-8") == second.encode("utf-8")
    assert render_ruling_markdown(_skeleton()) == render_ruling_markdown(_skeleton())
