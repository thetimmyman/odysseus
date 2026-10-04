"""Regression tests for the progressive tool-disclosure notice.

The notice must not claim unloaded tools are available that turn, nor elide
them in a way that implies a hidden reserve; models misread that as lacking
tools they actually have.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import agent_loop  # noqa: E402

def test_disclosure_notice_does_not_claim_unloaded_tools_are_available():
    prompt = agent_loop._assemble_prompt({"bash", "edit_file"})
    assert "Other tools available when needed" not in prompt
    assert "NOT loaded this turn" in prompt


def test_disclosure_notice_is_not_truncated():
    """Every unloaded tool is named; a '… (N more)' elision reads as a hidden reserve."""
    included = {"bash", "edit_file"}
    prompt = agent_loop._assemble_prompt(included)
    not_shown = set(agent_loop.TOOL_SECTIONS.keys()) - included
    assert "more)" not in prompt.split("NOT loaded this turn:")[-1]
    for name in not_shown:
        assert name in prompt, name


def test_disclosure_notice_states_documented_tools_are_callable_now():
    prompt = agent_loop._assemble_prompt({"bash", "edit_file"})
    assert "RIGHT NOW" in prompt
    assert "no way to request more tools mid-turn" in prompt


def test_no_notice_when_every_tool_is_loaded():
    prompt = agent_loop._assemble_prompt(set(agent_loop.TOOL_SECTIONS.keys()))
    assert "NOT loaded this turn" not in prompt
