"""Per-request reasoning-effort override: validation, and that every user-facing
chat path actually forwards it.

The forwarding assertions are source-level on purpose. The original
reasoning_effort work threaded the field through the preset layer and shipped
with no way to reach it from the UI at all, and the chat-mode call site silently
dropped it. Both were invisible to behavioural tests because the field is
optional everywhere — omitting it looks exactly like "user didn't ask".
"""

import re
from pathlib import Path

import pytest

from src.request_models import ChatRequest, VALID_REASONING_EFFORTS

_REPO = Path(__file__).resolve().parent.parent
_CHAT_ROUTES = (_REPO / "routes" / "chat_routes.py").read_text()
_INDEX_HTML = (_REPO / "static" / "index.html").read_text()
_CHAT_JS = (_REPO / "static" / "js" / "chat.js").read_text()


def _req(**kw):
    return ChatRequest(message="hi", session="s1", **kw)


@pytest.mark.parametrize("level", sorted(VALID_REASONING_EFFORTS))
def test_valid_levels_are_kept(level):
    assert _req(reasoning_effort=level).reasoning_effort == level


@pytest.mark.parametrize("bad", ["", "LOW", "turbo", "ultra", "none", "0"])
def test_unknown_levels_degrade_to_model_default(bad):
    # Dropped, not rejected: a bad hint must not fail the whole chat request.
    assert _req(reasoning_effort=bad).reasoning_effort is None


def test_absent_field_stays_none():
    assert _req().reasoning_effort is None


def test_ui_exposes_every_valid_level():
    """The control must offer each level the backend accepts, plus a default."""
    select = re.search(
        r'<select id="reasoning-effort-select".*?</select>',
        _INDEX_HTML,
        re.S,
    )
    assert select, "reasoning-effort selector missing from the composer"
    offered = set(re.findall(r'<option value="([^"]*)"', select.group(0)))
    assert "" in offered, "no 'model default' option — user can't opt back out"
    assert VALID_REASONING_EFFORTS <= offered, (
        f"UI is missing levels the API accepts: {VALID_REASONING_EFFORTS - offered}"
    )


def test_frontend_sends_the_field():
    assert "fd.append('reasoning_effort'" in _CHAT_JS


def test_both_routes_apply_the_override():
    """Form (stream) and JSON (non-stream) entrypoints both override the preset."""
    assert _CHAT_ROUTES.count("ctx.preset.reasoning_effort = ") == 2


def test_every_llm_call_site_forwards_reasoning_effort():
    """Guard the regression this test file was written for.

    `temperature=ctx.preset.temperature` marks a user-facing generation call.
    Each one must pass reasoning_effort too, or that mode silently ignores the
    user's choice — which is what chat mode did.
    """
    temp_sites = _CHAT_ROUTES.count("temperature=ctx.preset.temperature")
    effort_sites = _CHAT_ROUTES.count("reasoning_effort=ctx.preset.reasoning_effort")
    assert effort_sites == temp_sites, (
        f"{temp_sites} generation call sites but only {effort_sites} forward "
        "reasoning_effort — one of them drops the user's setting"
    )
