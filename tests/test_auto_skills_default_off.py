"""Absence of the `auto_skills` preference must not mean "on".

The gate used to be ``bool(uprefs.get("auto_skills", True))``, so every account
that had never touched the Settings toggle ran background skill extraction —
including accounts created after the fact. That made the POS-AI-23 interim
mitigation (setting ``auto_skills=False``) cover only the accounts someone had
remembered to set it on: on the live family box, one of four.

These tests pin the new resolution order — explicit user pref, then the
operator's system-level setting, then off — and prove the extractor is not
dispatched for a user with no preference at all.
"""
import asyncio
import types

from routes import chat_helpers
from routes.chat_helpers import auto_skills_enabled_for
from src.settings import DEFAULT_SETTINGS


# ── the resolution order ─────────────────────────────────────────────────── #

def test_shipped_system_default_is_off():
    """The operator-level policy ships off, so an untouched box is safe."""
    assert DEFAULT_SETTINGS["auto_skills"] is False


def test_absent_pref_resolves_off(monkeypatch):
    """The regression this file exists for: no key must not mean enabled."""
    monkeypatch.setattr(chat_helpers, "get_setting", lambda key, default=None: default)
    assert auto_skills_enabled_for({}) is False
    # ...and it stays off when the user has *other* prefs but not this one,
    # which is exactly the shape of the live `lacey` account.
    assert auto_skills_enabled_for({"auto_memory": True, "theme": "dark"}) is False


def test_explicit_user_pref_wins_over_system_default(monkeypatch):
    # System says off; a user who opted in still gets extraction.
    monkeypatch.setattr(chat_helpers, "get_setting", lambda key, default=None: False)
    assert auto_skills_enabled_for({"auto_skills": True}) is True

    # System says on; a user who opted out is still not extracted from. This is
    # the direction that matters for a mitigation: policy must never override a
    # user's explicit "no".
    monkeypatch.setattr(chat_helpers, "get_setting", lambda key, default=None: True)
    assert auto_skills_enabled_for({"auto_skills": False}) is False


def test_system_setting_governs_users_without_a_pref(monkeypatch):
    """One operator switch re-enables everyone who never set the toggle —
    which is what makes this a policy rather than a per-account chore."""
    seen = {}

    def _get_setting(key, default=None):
        seen["key"] = key
        return True

    monkeypatch.setattr(chat_helpers, "get_setting", _get_setting)
    assert auto_skills_enabled_for({}) is True
    assert seen["key"] == "auto_skills"


def test_falsy_and_truthy_pref_values_are_coerced(monkeypatch):
    """The pref is written by a JSON API, so it can hold non-booleans."""
    monkeypatch.setattr(chat_helpers, "get_setting", lambda key, default=None: default)
    assert auto_skills_enabled_for({"auto_skills": 0}) is False
    assert auto_skills_enabled_for({"auto_skills": None}) is False
    assert auto_skills_enabled_for({"auto_skills": ""}) is False
    assert auto_skills_enabled_for({"auto_skills": 1}) is True


# ── the gate, end to end ─────────────────────────────────────────────────── #

class _FakeSession:
    """Minimum surface run_post_response_tasks touches."""

    def __init__(self):
        # 2 messages: below the memory extractor's `>= 4 and % 4 == 0` gate, so
        # skill extraction is the only background task this call can spawn.
        self.history = [object(), object()]
        self.name = "A real title"          # suppresses auto_name_session
        self.endpoint_url = "http://example.invalid/v1"
        self.model = "test-model"
        self.headers = {}


#: Sentinel for _run_gate: resolve the system default for real, through
#: src.settings, instead of stubbing it. Used by the test that has to fail if
#: *either* the code fallback or DEFAULT_SETTINGS is flipped back on.
REAL = object()


async def _run_gate(monkeypatch, uprefs, system_default=False):
    """Call run_post_response_tasks and report whether the extractor was
    dispatched. Deliberately patches `maybe_extract_skill` itself rather than
    the task-spawning helper, so this test does not encode *how* the coroutine
    is scheduled (`asyncio.create_task` today, `src.background_tasks.spawn`
    after the POS-AI-23 lane-isolation work lands)."""
    calls = []

    async def _fake_extractor(*args, **kwargs):
        calls.append(kwargs.get("owner"))

    import services.memory.skill_extractor as skill_extractor
    monkeypatch.setattr(skill_extractor, "maybe_extract_skill", _fake_extractor)
    if system_default is not REAL:
        monkeypatch.setattr(
            chat_helpers, "get_setting", lambda key, default=None: system_default
        )
    monkeypatch.setattr(
        "src.task_endpoint.resolve_task_endpoint",
        lambda url, model, headers, owner=None: (url, model, headers),
    )

    chat_helpers.run_post_response_tasks(
        _FakeSession(),
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
        agent_rounds=5,                # well past the >= 2 activity threshold
        agent_tool_calls=5,
    )
    # Let any task that *was* spawned actually start.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return calls


async def test_user_with_no_auto_skills_key_is_not_extracted_from(monkeypatch):
    """The headline assertion: a principal that has never set the pref — a
    brand-new account — does not get background skill extraction."""
    assert await _run_gate(monkeypatch, uprefs={}) == []


async def test_absent_pref_is_off_under_the_real_shipped_settings(monkeypatch, tmp_path):
    """The same assertion with **nothing stubbed** between the gate and
    `src.settings`, so it fails if either half of the default is flipped back
    on — the code fallback in `auto_skills_enabled_for` or the shipped
    `DEFAULT_SETTINGS["auto_skills"]`.

    Points SETTINGS_FILE at a path that does not exist and clears the TTL
    cache, so `load_settings()` returns exactly DEFAULT_SETTINGS and the result
    cannot depend on a settings.json left behind by another test.
    """
    import src.settings as settings_mod
    monkeypatch.setattr(settings_mod, "SETTINGS_FILE", str(tmp_path / "absent.json"))
    monkeypatch.setattr(settings_mod, "_settings_cache", None)
    assert await _run_gate(monkeypatch, uprefs={}, system_default=REAL) == []


async def test_positive_control_extractor_does_fire_when_enabled(monkeypatch):
    """Proves the test above can tell the difference — without this, an
    accidentally-inert harness would report a false pass."""
    assert await _run_gate(monkeypatch, uprefs={"auto_skills": True}) == ["somebody"]


async def test_operator_can_re_enable_for_pref_less_users(monkeypatch):
    assert await _run_gate(monkeypatch, uprefs={}, system_default=True) == ["somebody"]


async def test_explicit_optout_survives_an_enabled_system_default(monkeypatch):
    assert await _run_gate(
        monkeypatch, uprefs={"auto_skills": False}, system_default=True
    ) == []
