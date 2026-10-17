"""Optional policy provenance follows the data actually returned to callers."""
import builtins
import copy
import json
import os
from types import SimpleNamespace
import uuid

import pytest

from src import routing_domain_policy as domain
from src import routing_policy as policy


CANARY = "synthetic-policy-secret-never-in-provenance"
IDENTITY_KEYS = {"dev", "ino", "mode", "uid", "gid", "size", "mtime_ns", "ctime_ns"}


@pytest.fixture
def policy_files(tmp_path, monkeypatch):
    selected = tmp_path / "selected-policy.json"
    baked = tmp_path / "baked-policy.json"
    monkeypatch.setattr(policy, "POLICY_PATH", str(selected))
    monkeypatch.setattr(policy, "BAKED_POLICY_PATH", str(baked))
    monkeypatch.setattr(policy, "_cache", None)
    monkeypatch.setattr(domain.uuid, "uuid4", lambda: uuid.UUID(int=1))
    monkeypatch.setattr(domain, "_utc_iso", lambda: "2026-01-01T00:00:00+00:00")
    return SimpleNamespace(selected=selected, baked=baked, alternate=tmp_path / "alternate-policy.json")


def _write(path, value):
    path.write_text(json.dumps(value))


def _identity(path):
    info = path.stat()
    return {name: getattr(info, "st_" + name) for name in IDENTITY_KEYS}


def _track_open(monkeypatch):
    opened = []
    original = builtins.open

    def observed(path, *args, **kwargs):
        opened.append((os.fspath(path), args[0] if args else kwargs.get("mode", "r")))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", observed)
    return opened


def test_file_origin_uses_the_consumed_open_file_and_keeps_payload_separate(policy_files, caplog):
    content = {"routingPolicyVersion": "synthetic", "private": CANARY}
    _write(policy_files.selected, content)
    sink = {"old": CANARY}
    result = policy.load_policy(provenance_sink=sink)
    assert result == content
    assert sink == {
        "delivery": "file", "candidate_path": str(policy_files.selected),
        "origin": {"kind": "file", "path": str(policy_files.selected), "identity": _identity(policy_files.selected)},
    }
    assert set(result) == set(content)
    assert CANARY not in json.dumps(sink) + caplog.text
    sink["origin"]["path"] = "synthetic-caller-mutation"
    result["private"] = "synthetic-caller-mutation"
    cached = {}
    assert policy.load_policy(provenance_sink=cached) == content
    assert cached["delivery"] == "cache"
    assert cached["origin"]["path"] == str(policy_files.selected)


def test_opt_in_reads_metadata_only_on_the_already_open_fd_and_closes_it(policy_files, monkeypatch):
    _write(policy_files.selected, {"routingPolicyVersion": "synthetic"})
    opened = _track_open(monkeypatch)
    original = os.fstat
    fds = []

    def observed(fd):
        fds.append(fd)
        return original(fd)

    monkeypatch.setattr(os, "fstat", observed)
    sink = {}
    policy.load_policy(provenance_sink=sink)
    assert opened == [(str(policy_files.selected), "r")]
    assert len(fds) == 2 and fds[0] == fds[1]
    with pytest.raises(OSError):
        original(fds[0])
    policy.load_policy(provenance_sink={})
    assert len(opened) == 1 and len(fds) == 2


@pytest.mark.parametrize("failure", ["unavailable", "changed", "invalid_fields"])
def test_metadata_failure_never_turns_successful_policy_into_defaults(policy_files, monkeypatch, failure):
    content = {"routingPolicyVersion": "synthetic-unchanged"}
    _write(policy_files.selected, content)
    original = os.fstat
    calls = []

    def unreliable(fd):
        calls.append(fd)
        if failure == "unavailable":
            raise OSError(CANARY)
        value = original(fd)
        fields = {"st_" + name: getattr(value, "st_" + name) for name in IDENTITY_KEYS}
        if failure == "invalid_fields":
            fields["st_ctime_ns"] = [CANARY]
        if len(calls) > 1:
            fields["st_ctime_ns"] += 1
        return SimpleNamespace(**fields)

    monkeypatch.setattr(os, "fstat", unreliable)
    sink = {}
    assert policy.load_policy(provenance_sink=sink) == content
    assert sink["origin"]["kind"] == "file"
    assert sink["origin"]["identity"] is None
    assert CANARY not in json.dumps(sink)


def test_legacy_three_field_cache_has_unknown_origin_without_a_reread(policy_files, monkeypatch):
    content = {"routingPolicyVersion": "synthetic-cached", "private": CANARY}
    _write(policy_files.selected, {"routingPolicyVersion": "different-on-disk"})
    monkeypatch.setattr(policy, "_cache", (str(policy_files.selected), policy_files.selected.stat().st_mtime, content))
    opened = _track_open(monkeypatch)
    sink = {}
    assert policy.load_policy(provenance_sink=sink) == content
    assert opened == []
    assert sink == {
        "delivery": "cache", "candidate_path": str(policy_files.selected),
        "origin": {"kind": "unknown", "path": None, "identity": None},
    }


def test_path_change_and_builtin_fallback_do_not_relabel_an_earlier_cache(policy_files, monkeypatch):
    content = {"routingPolicyVersion": "synthetic-A"}
    _write(policy_files.selected, content)
    first = {}
    assert policy.load_policy(provenance_sink=first) == content
    monkeypatch.setattr(policy, "POLICY_PATH", str(policy_files.alternate))
    second = {}
    assert policy.load_policy(provenance_sink=second) == policy.DEFAULT_POLICY
    assert second["candidate_path"] == str(policy_files.alternate)
    assert second["origin"]["kind"] == "builtin"
    assert second["origin"]["path"] is None
    monkeypatch.setattr(policy, "POLICY_PATH", str(policy_files.selected))
    third = {}
    assert policy.load_policy(provenance_sink=third) == content
    assert third["delivery"] == "cache"
    assert third["origin"] == first["origin"]


def test_cache_hit_keeps_one_payload_and_origin_generation_during_another_load(policy_files, monkeypatch):
    first_payload = {"routingPolicyVersion": "synthetic-generation-A"}
    second_payload = {"routingPolicyVersion": "synthetic-generation-B"}
    _write(policy_files.selected, first_payload)
    _write(policy_files.alternate, second_payload)
    first_origin = {}
    assert policy.load_policy(provenance_sink=first_origin) == first_payload
    first_cache = policy._cache
    monkeypatch.setattr(policy, "POLICY_PATH", str(policy_files.alternate))
    assert policy.load_policy(provenance_sink={}) == second_payload
    second_cache = policy._cache
    monkeypatch.setattr(policy, "POLICY_PATH", str(policy_files.selected))
    monkeypatch.setattr(policy, "_cache", first_cache)
    emit = policy._policy_provenance

    def another_normal_load_finishes(sink, delivery, candidate_path, origin):
        emit(sink, delivery, candidate_path, origin)
        # A concurrent normal reader can publish its new cache after this
        # request's metadata has been captured but before its payload returns.
        if delivery == "cache":
            policy._cache = second_cache

    monkeypatch.setattr(policy, "_policy_provenance", another_normal_load_finishes)
    opened = _track_open(monkeypatch)
    sink = {}
    assert policy.load_policy(provenance_sink=sink) == first_payload
    assert sink["delivery"] == "cache"
    assert sink["origin"] == first_origin["origin"]
    assert policy._cache is second_cache
    assert opened == []


@pytest.mark.parametrize("raw", [None, "invalid-json", "[]"])
def test_missing_or_invalid_policy_reports_the_builtin_it_actually_returns(policy_files, raw):
    if raw is not None:
        policy_files.selected.write_text(raw)
    sink = {}
    result = policy.load_policy(provenance_sink=sink)
    assert result == policy.DEFAULT_POLICY
    assert result is not policy.DEFAULT_POLICY
    assert sink == {
        "delivery": "builtin", "candidate_path": str(policy_files.selected),
        "origin": {"kind": "builtin", "path": None, "identity": None,
                   "symbol": "src.routing_policy.DEFAULT_POLICY"},
    }


def test_baked_seed_keeps_the_existing_open_order_and_reports_live_file(policy_files, monkeypatch):
    content = {"routingPolicyVersion": "synthetic-baked"}
    _write(policy_files.baked, content)
    opened = _track_open(monkeypatch)
    sink = {}
    assert policy.load_policy(provenance_sink=sink) == content
    assert opened == [(str(policy_files.baked), "r"), (str(policy_files.selected), "w"),
                      (str(policy_files.selected), "r")]
    assert sink["origin"]["path"] == str(policy_files.selected)
    assert sink["origin"]["identity"] == _identity(policy_files.selected)


class UnsupportedSink(dict):
    calls = 0

    def clear(self):
        self.calls += 1
        raise AssertionError("unsupported sink must not be called")

    def update(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("unsupported sink must not be called")


@pytest.mark.parametrize("sink", [None, [], UnsupportedSink(), lambda value: pytest.fail("callback invoked")])
def test_only_an_exact_plain_dict_can_opt_into_provenance(policy_files, monkeypatch, sink):
    content = {"routingPolicyVersion": "synthetic-legacy"}
    _write(policy_files.selected, content)
    monkeypatch.setattr(os, "fstat", lambda fd: pytest.fail("unsupported sink requested metadata"))
    assert policy.load_policy(provenance_sink=sink) == content
    if isinstance(sink, UnsupportedSink):
        assert sink.calls == 0


@pytest.mark.parametrize("configured", [True, False])
def test_domain_reports_configured_entry_or_its_actual_builtin(policy_files, configured):
    content = {"routingPolicyVersion": "synthetic", "private": CANARY}
    if configured:
        content["domains"] = {"finance": {"local_only": False, "allowed_providers": ["synthetic"]}}
    _write(policy_files.selected, content)
    sink = {}
    result = domain.domain_policy("finance", provenance_sink=sink)
    assert result.local_only is (not configured)
    assert sink["branch"] == ("configured_entry" if configured else "builtin_default")
    assert sink["load"]["origin"]["path"] == str(policy_files.selected)
    if not configured:
        assert sink["symbol"] == "src.routing_domain_policy._DEFAULT_POLICIES"
    assert CANARY not in json.dumps(sink)


@pytest.mark.parametrize("ceiling,configured", [("secret", True), (None, False), ("invalid", False)])
def test_ceiling_reports_configured_value_or_rank_default(policy_files, ceiling, configured):
    content = {"routingPolicyVersion": "synthetic"}
    if ceiling is not None:
        content["remoteSensitivityCeiling"] = ceiling
    _write(policy_files.selected, content)
    sink = {}
    result = domain.evaluate_route(domain="general_swe", provider="openrouter", sensitivity="restricted",
                                   provenance_sink=sink)
    assert result.allowed is configured
    assert sink["ceiling"]["branch"] == ("configured_entry" if configured else "builtin_default")
    assert sink["ceiling"]["load"]["delivery"] == "cache"


@pytest.mark.parametrize("provided,provider,sensitivity,loads,ceiling", [
    (None, "ollama", "internal", 2, True),
    (None, "ollama", "invalid", 1, False),
    (domain.DomainPolicy(domain="finance", denied_providers=frozenset({"ollama"})), "ollama", "internal", 0, False),
    (domain.DomainPolicy(domain="finance", local_only=True), "ollama", "internal", 1, True),
])
def test_evaluation_preserves_values_and_exact_normal_load_count(policy_files, monkeypatch, provided,
                                                               provider, sensitivity, loads, ceiling):
    _write(policy_files.selected, {"routingPolicyVersion": "synthetic", "private": CANARY})
    original = policy.load_policy
    calls = []

    def tracked(**kwargs):
        calls.append(bool(kwargs))
        return original(**kwargs)

    monkeypatch.setattr(policy, "load_policy", tracked)
    args = {"domain": "finance", "provider": provider, "sensitivity": sensitivity, "policy": provided}
    legacy = domain.evaluate_route(**args)
    assert calls == [False] * loads
    calls.clear()
    monkeypatch.setattr(policy, "_cache", None)
    sink = {}
    observed = domain.evaluate_route(**args, provenance_sink=sink)
    assert observed.to_dict() == legacy.to_dict()
    assert calls == [True] * loads
    assert (sink["ceiling"].get("branch") != "not_reached") is ceiling
    if provided is not None:
        assert sink["domain"] == {"branch": "caller_supplied"}
    assert CANARY not in json.dumps(sink)


def test_unsupported_sink_does_not_change_legacy_no_keyword_loader_calls(policy_files, monkeypatch):
    _write(policy_files.selected, {"routingPolicyVersion": "synthetic"})
    calls = []

    def legacy_loader():
        calls.append("load")
        return {}

    monkeypatch.setattr(policy, "load_policy", legacy_loader)
    sink = UnsupportedSink()
    assert domain.domain_policy("finance", provenance_sink=sink).local_only is True
    assert domain.evaluate_route(domain="finance", provider="ollama", provenance_sink=sink).allowed is True
    assert calls == ["load", "load", "load"]
    assert sink.calls == 0


def test_builtin_policy_and_legacy_payload_never_gain_provenance_fields(policy_files):
    baseline = copy.deepcopy(policy.DEFAULT_POLICY)
    sink = {}
    first = policy.load_policy(provenance_sink=sink)
    assert first == baseline
    assert policy.DEFAULT_POLICY == baseline
    assert "origin" not in first and "provenance" not in first


@pytest.mark.parametrize("observed", [False, True])
def test_optional_metadata_does_not_access_an_extra_handle_name_property(policy_files, monkeypatch, observed):
    content = {"routingPolicyVersion": "synthetic-handle"}
    _write(policy_files.selected, content)
    original = builtins.open

    class Handle:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, *args):
            return self.handle.read(*args)

        def fileno(self):
            return self.handle.fileno()

        @property
        def name(self):
            raise OSError(CANARY)

    monkeypatch.setattr(builtins, "open", lambda *args, **kwargs: Handle(original(*args, **kwargs)))
    kwargs = {"provenance_sink": {}} if observed else {}
    assert policy.load_policy(**kwargs) == content


def test_supplied_policy_truth_value_is_evaluated_once_with_or_without_metadata(policy_files, monkeypatch):
    _write(policy_files.selected, {"routingPolicyVersion": "synthetic"})

    class Supplied:
        domain = "finance"
        denied_providers = frozenset({"ollama"})
        allowed_providers = None
        budget_class = "synthetic"
        approval_required = False
        calls = 0

        def __bool__(self):
            self.calls += 1
            return True

    for kwargs in ({}, {"provenance_sink": {}}):
        supplied = Supplied()
        decision = domain.evaluate_route(domain="finance", provider="ollama", policy=supplied, **kwargs)
        assert decision.rule == "provider-denied"
        assert supplied.calls == 1


def test_reused_evaluation_sink_cannot_keep_success_after_an_unchanged_ceiling_error(policy_files, monkeypatch):
    _write(policy_files.selected, {"routingPolicyVersion": "synthetic-valid"})
    sink = {}
    assert domain.evaluate_route(domain="finance", provider="ollama", provenance_sink=sink).allowed is True
    previous = copy.deepcopy(sink)
    _write(policy_files.selected, {"routingPolicyVersion": "synthetic-invalid", "remoteSensitivityCeiling": []})
    monkeypatch.setattr(policy, "_cache", None)
    with pytest.raises(TypeError) as legacy:
        domain.evaluate_route(domain="finance", provider="ollama")
    with pytest.raises(type(legacy.value)) as observed:
        domain.evaluate_route(domain="finance", provider="ollama", provenance_sink=sink)
    assert str(observed.value) == str(legacy.value)
    assert sink != previous
    assert sink == {"domain": {"branch": "unknown"}, "ceiling": {"branch": "not_reached"}}


@pytest.mark.parametrize("entrypoint", ["load", "domain", "evaluation"])
def test_reused_sink_is_reset_before_existing_early_parse_errors(policy_files, monkeypatch, entrypoint):
    _write(policy_files.selected, {"routingPolicyVersion": "synthetic-valid"})
    if entrypoint == "load":
        monkeypatch.setattr(policy, "POLICY_PATH", object())
        reader, args = policy.load_policy, {}
    elif entrypoint == "domain":
        reader, args = domain.domain_policy, {"domain": 1}
    else:
        reader, args = domain.evaluate_route, {"domain": "finance", "provider": 1}
    with pytest.raises((TypeError, AttributeError)) as legacy:
        reader(**args)
    sink = {"previous_success": CANARY}
    with pytest.raises(type(legacy.value)) as observed:
        reader(**args, provenance_sink=sink)
    assert str(observed.value) == str(legacy.value)
    assert CANARY not in json.dumps(sink)
    if entrypoint == "load":
        assert sink == {"delivery": "unknown", "candidate_path": None,
                        "origin": {"kind": "unknown", "path": None, "identity": None}}
    elif entrypoint == "domain":
        assert sink == {"branch": "unknown", "load": {}}
    else:
        assert sink == {"domain": {"branch": "unknown"}, "ceiling": {"branch": "not_reached"}}
