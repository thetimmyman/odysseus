"""PS-623 — capability/role → execution-target contract.

Proves: (1) the catalog maps capability to explicit preferred targets (latest
models), (2) it is operator-controllable via the ``execution_targets`` setting,
(3) selection probes availability and honours the allowlist, (4) a forced-Pro-
unavailable case raises a typed failure and never silently downgrades to Flash,
and (5) the capability is recorded on the pinned execution identity.
"""
import pytest

from src import execution_catalog as ec
from src.agent_execution import build_execution_target


def _probe(states):
    """Build an injectable probe from (target_id -> available) states."""
    def _fn(target):
        return ec.CapabilityProbe(target=target, available=states.get(target.target_id, False))
    return _fn


# --- catalog / settings -----------------------------------------------------

def test_default_catalog_maps_strong_to_pro():
    targets = ec.catalog_targets(ec.CAPABILITY_INTEGRATION_STRONG)
    assert targets[0].target_id == "cline-pass/deepseek-v4-pro"
    # Policy-approved substitute is the SAME model, different provider.
    assert [t.target_id for t in targets] == [
        "cline-pass/deepseek-v4-pro",
        "openrouter/deepseek/deepseek-v4-pro",
    ]


def test_default_catalog_maps_fast_to_latest_flash():
    targets = ec.catalog_targets(ec.CAPABILITY_IMPLEMENTATION_FAST)
    assert [t.target_id for t in targets] == ["cline-pass/deepseek-v4.1-flash"]


def test_unknown_capability_fails_closed():
    with pytest.raises(ValueError):
        ec.catalog_targets("integration_maybe")


def test_catalog_is_operator_settable(monkeypatch):
    from src import settings
    monkeypatch.setattr(
        settings, "get_setting",
        lambda key, default=None: {
            "execution_targets": {
                "implementation_fast": [
                    {"provider": "cline-pass", "model": "deepseek-v4.1-flash-0731"},
                ],
            },
        }.get(key, default),
    )
    targets = ec.catalog_targets(ec.CAPABILITY_IMPLEMENTATION_FAST)
    assert targets[0].target_id == "cline-pass/deepseek-v4.1-flash-0731"


def test_setting_default_is_present_in_default_settings():
    from src.settings import DEFAULT_SETTINGS
    entry = DEFAULT_SETTINGS["execution_targets"]["implementation_fast"][0]
    assert entry == {"provider": "cline-pass", "model": "deepseek-v4.1-flash"}


# --- selection / probe ------------------------------------------------------

def test_select_returns_first_available():
    probe = _probe({"cline-pass/deepseek-v4-pro": True})
    t = ec.select_target_for_capability(ec.CAPABILITY_INTEGRATION_STRONG, probe=probe)
    assert t.target_id == "cline-pass/deepseek-v4-pro"


def test_pro_falls_back_to_same_model_openrouter():
    probe = _probe({
        "cline-pass/deepseek-v4-pro": False,
        "openrouter/deepseek/deepseek-v4-pro": True,
    })
    t = ec.select_target_for_capability(ec.CAPABILITY_INTEGRATION_STRONG, probe=probe)
    assert t.target_id == "openrouter/deepseek/deepseek-v4-pro"


def test_forced_pro_unavailable_raises_typed_not_flash():
    seen = []
    probe = _probe({})  # everything unavailable

    def _recording(t):
        seen.append(t.target_id)
        return probe(t)

    with pytest.raises(ec.CapabilityUnavailable) as err:
        ec.select_target_for_capability(ec.CAPABILITY_INTEGRATION_STRONG, probe=_recording)
    assert err.value.capability == ec.CAPABILITY_INTEGRATION_STRONG
    # The dangerous invariant: Flash must never have been considered for a
    # planning/reconciliation role.
    assert not any("flash" in s for s in seen)
    assert seen == ["cline-pass/deepseek-v4-pro", "openrouter/deepseek/deepseek-v4-pro"]


def test_flash_selected_independently():
    probe = _probe({"cline-pass/deepseek-v4.1-flash": True})
    t = ec.select_target_for_capability(ec.CAPABILITY_IMPLEMENTATION_FAST, probe=probe)
    assert t.target_id == "cline-pass/deepseek-v4.1-flash"


def test_probe_target_uses_runner():
    def runner(target, timeout):
        return (True, "ok")

    result = ec.probe_target(
        ec.ExecutionTargetSpec("cline-pass", "deepseek-v4-pro"),
        runner=runner,
    )
    assert result.available is True
    assert result.detail == "ok"

    result = ec.probe_target(
        ec.ExecutionTargetSpec("cline-pass", "deepseek-v4-pro"),
        runner=lambda t, to: (False, "exit 1"),
    )
    assert result.available is False


# --- identity recording -----------------------------------------------------

def test_identity_records_capability():
    target = build_execution_target(
        endpoint_url="https://api.example/v1",
        model="deepseek-v4-pro",
        capability=ec.CAPABILITY_INTEGRATION_STRONG,
    )
    d = target.to_dict()
    assert d["capability"] == ec.CAPABILITY_INTEGRATION_STRONG
    assert d["model"] == "deepseek-v4-pro"


def test_identity_capability_defaults_empty():
    target = build_execution_target(endpoint_url="https://api.example/v1", model="x")
    assert target.to_dict()["capability"] == ""
