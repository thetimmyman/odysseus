"""Absence of a background-extraction preference must not mean "on".

Both gates used to be ``uprefs.get(key, True)``, so every account that had
never touched the Settings toggle ran the extractor — including every account
created afterwards. That is what made the POS-AI-23 interim containment
partial:

* ``auto_skills`` was set to ``False`` on **one of four** live accounts;
* ``auto_memory`` was set on **none of four** — and it is the subsystem whose
  output contract was persisted as an assistant reply in session ``8670f5ae``.

These tests pin the resolution order — explicit user pref, then the operator's
system-level setting, then off — and prove neither extractor is dispatched for
a user with no preference at all.
"""
import asyncio
import types

import pytest

from routes import chat_helpers
from routes.chat_helpers import (
    BACKGROUND_EXTRACTION_PREFS,
    auto_memory_enabled_for,
    auto_skills_enabled_for,
)
from src.settings import DEFAULT_SETTINGS

RESOLVERS = {
    "auto_memory": auto_memory_enabled_for,
    "auto_skills": auto_skills_enabled_for,
}


# ── the resolution order ─────────────────────────────────────────────────── #

def test_both_gates_are_declared_together():
    """A third background-extraction gate should not be able to appear without
    joining this list — it is what the shipped-default guard iterates."""
    assert set(BACKGROUND_EXTRACTION_PREFS) == {"auto_memory", "auto_skills"}
    assert set(RESOLVERS) == set(BACKGROUND_EXTRACTION_PREFS)


@pytest.mark.parametrize("key", BACKGROUND_EXTRACTION_PREFS)
def test_shipped_system_default_is_off(key):
    """The operator-level policy ships off, so an untouched box is safe."""
    assert DEFAULT_SETTINGS[key] is False


@pytest.mark.parametrize("key", BACKGROUND_EXTRACTION_PREFS)
def test_absent_pref_resolves_off(monkeypatch, key):
    """The regression this file exists for: no key must not mean enabled."""
    monkeypatch.setattr(chat_helpers, "get_setting", lambda k, default=None: default)
    assert RESOLVERS[key]({}) is False
    # ...and it stays off when the user has *other* prefs but not this one,
    # which is the shape every live account had for `auto_memory`.
    assert RESOLVERS[key]({"theme": "dark", "memory_enabled": True}) is False


@pytest.mark.parametrize("key", BACKGROUND_EXTRACTION_PREFS)
def test_explicit_user_pref_wins_over_system_default(monkeypatch, key):
    # System says off; a user who opted in still gets extraction.
    monkeypatch.setattr(chat_helpers, "get_setting", lambda k, default=None: False)
    assert RESOLVERS[key]({key: True}) is True

    # System says on; a user who opted out is still not extracted from. This is
    # the direction that matters for a mitigation: policy must never override a
    # user's explicit "no".
    monkeypatch.setattr(chat_helpers, "get_setting", lambda k, default=None: True)
    assert RESOLVERS[key]({key: False}) is False


@pytest.mark.parametrize("key", BACKGROUND_EXTRACTION_PREFS)
def test_system_setting_governs_users_without_a_pref(monkeypatch, key):
    """One operator switch re-enables everyone who never set the toggle —
    which is what makes this a policy rather than a per-account chore."""
    seen = {}

    def _get_setting(k, default=None):
        seen["key"] = k
        return True

    monkeypatch.setattr(chat_helpers, "get_setting", _get_setting)
    assert RESOLVERS[key]({}) is True
    # Each gate must read *its own* setting, not share one.
    assert seen["key"] == key


@pytest.mark.parametrize("key", BACKGROUND_EXTRACTION_PREFS)
def test_the_gates_do_not_read_each_others_prefs(monkeypatch, key):
    """Setting one must not silently enable or disable the other."""
    other = next(k for k in BACKGROUND_EXTRACTION_PREFS if k != key)
    monkeypatch.setattr(chat_helpers, "get_setting", lambda k, default=None: default)
    assert RESOLVERS[key]({other: True}) is False


@pytest.mark.parametrize("key", BACKGROUND_EXTRACTION_PREFS)
def test_falsy_and_truthy_pref_values_are_coerced(monkeypatch, key):
    """The pref is written by a JSON API, so it can hold non-booleans."""
    monkeypatch.setattr(chat_helpers, "get_setting", lambda k, default=None: default)
    assert RESOLVERS[key]({key: 0}) is False
    assert RESOLVERS[key]({key: None}) is False
    assert RESOLVERS[key]({key: ""}) is False
    assert RESOLVERS[key]({key: 1}) is True


# ── the gates, end to end ────────────────────────────────────────────────── #

class _FakeSession:
    """Minimum surface run_post_response_tasks touches."""

    def __init__(self, history_len):
        self.history = [object()] * history_len
        self.name = "A real title"          # suppresses auto_name_session
        self.endpoint_url = "http://example.invalid/v1"
        self.model = "test-model"
        self.headers = {}


#: Sentinel for _run: resolve the system default for real, through
#: src.settings, instead of stubbing it. Used by the tests that have to fail if
#: *either* the code fallback or DEFAULT_SETTINGS is flipped back on.
REAL = object()


async def _run(monkeypatch, uprefs, *, history_len, agent_rounds, system_default=False):
    """Call run_post_response_tasks and report which extractors were
    dispatched.

    Patches the extractor coroutines themselves rather than the task spawner,
    so these tests do not encode *how* a coroutine is scheduled
    (`asyncio.create_task` on dev, `src.background_tasks.spawn` after the
    POS-AI-23 lane-isolation work lands).

    `history_len` and `agent_rounds` select which gate is even reachable:
    memory extraction needs `len(history) >= 4 and % 4 == 0`, skill extraction
    needs 2+ agent rounds or tool calls. So each test exercises one gate with
    the other structurally unable to fire.
    """
    fired = {"memory": [], "skills": []}

    async def _fake_memory(*args, **kwargs):
        fired["memory"].append("fired")

    async def _fake_skill(*args, **kwargs):
        fired["skills"].append(kwargs.get("owner"))

    import services.memory.memory_extractor as memory_extractor
    import services.memory.skill_extractor as skill_extractor
    monkeypatch.setattr(memory_extractor, "extract_and_store", _fake_memory)
    monkeypatch.setattr(skill_extractor, "maybe_extract_skill", _fake_skill)
    if system_default is not REAL:
        monkeypatch.setattr(
            chat_helpers, "get_setting", lambda k, default=None: system_default
        )
    monkeypatch.setattr(
        "src.task_endpoint.resolve_task_endpoint",
        lambda url, model, headers, owner=None: (url, model, headers),
    )

    chat_helpers.run_post_response_tasks(
        _FakeSession(history_len),
        session_manager=None,
        session_id="s1",
        message="hi",
        full_response="ok",
        last_metrics=None,
        uprefs=uprefs,
        memory_manager=None,
        memory_vector=None,
        webhook_manager=None,          # suppresses the webhook task
        skills_manager=types.SimpleNamespace(),
        owner="somebody",
        agent_rounds=agent_rounds,
        agent_tool_calls=agent_rounds,
    )
    # Let any task that *was* spawned actually start.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return fired


# Kwargs that make exactly one gate reachable.
ONLY_MEMORY = {"history_len": 4, "agent_rounds": 0}
ONLY_SKILLS = {"history_len": 2, "agent_rounds": 5}
REACHABLE = {"auto_memory": ONLY_MEMORY, "auto_skills": ONLY_SKILLS}
LANE = {"auto_memory": "memory", "auto_skills": "skills"}


@pytest.mark.parametrize("key", BACKGROUND_EXTRACTION_PREFS)
async def test_user_with_no_pref_is_not_extracted_from(monkeypatch, key):
    """The headline assertion: a principal that has never set the pref — a
    brand-new account — gets no background extraction."""
    fired = await _run(monkeypatch, uprefs={}, **REACHABLE[key])
    assert fired[LANE[key]] == []


@pytest.mark.parametrize("key", BACKGROUND_EXTRACTION_PREFS)
async def test_absent_pref_is_off_under_the_real_shipped_settings(
    monkeypatch, tmp_path, key
):
    """The same assertion with **nothing stubbed** between the gate and
    `src.settings`, so it fails if either half of the default is flipped back
    on — the code fallback in `_background_extraction_enabled` or the shipped
    `DEFAULT_SETTINGS` entry.

    Points SETTINGS_FILE at a path that does not exist and clears the TTL
    cache, so `load_settings()` returns exactly DEFAULT_SETTINGS and the result
    cannot depend on a settings.json left behind by another test.
    """
    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(settings_mod, "_settings_cache", None)
    fired = await _run(
        monkeypatch, uprefs={}, system_default=REAL, **REACHABLE[key]
    )
    assert fired[LANE[key]] == []


@pytest.mark.parametrize("key", BACKGROUND_EXTRACTION_PREFS)
async def test_positive_control_extractor_does_fire_when_enabled(monkeypatch, key):
    """Proves the tests above can tell the difference — without this, an
    accidentally-inert harness would report a false pass."""
    fired = await _run(monkeypatch, uprefs={key: True}, **REACHABLE[key])
    assert len(fired[LANE[key]]) == 1


@pytest.mark.parametrize("key", BACKGROUND_EXTRACTION_PREFS)
async def test_operator_can_re_enable_for_pref_less_users(monkeypatch, key):
    fired = await _run(
        monkeypatch, uprefs={}, system_default=True, **REACHABLE[key]
    )
    assert len(fired[LANE[key]]) == 1


@pytest.mark.parametrize("key", BACKGROUND_EXTRACTION_PREFS)
async def test_explicit_optout_survives_an_enabled_system_default(monkeypatch, key):
    fired = await _run(
        monkeypatch, uprefs={key: False}, system_default=True, **REACHABLE[key]
    )
    assert fired[LANE[key]] == []
