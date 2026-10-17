"""Optional registry provenance observes one normal read of synthetic inputs."""
import builtins
from dataclasses import asdict
import json
import os
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from src import local_target_routing as fleet
from src import local_targets as targets


@pytest.fixture
def defaults(monkeypatch):
    specs = tuple(targets.LocalTargetSpec(
        target_id=target_id, label="synthetic", ssh_host="",
        endpoint="http://synthetic.invalid", model="synthetic-model", roles=(),
    ) for target_id in ("synthetic-a", "synthetic-b"))
    monkeypatch.setattr(targets, "DEFAULT_TARGETS", specs)
    monkeypatch.delenv(targets.TARGET_CAPABILITY_PROFILE_REGISTRY_ENV, raising=False)
    return specs


def _write_registry(tmp_path, monkeypatch, payload):
    path = tmp_path / "synthetic-registry.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(targets.TARGET_CAPABILITY_PROFILE_REGISTRY_ENV, str(path))
    return path


def _unknown():
    return {"kind": "unknown", "path": None, "identity": None, "targets": {}}


def _fingerprint(info):
    return {name: getattr(info, "st_" + name) for name in (
        "dev", "ino", "mode", "uid", "gid", "size", "mtime_ns", "ctime_ns",
    )}


def test_builtin_provenance_preserves_exact_legacy_tuple_and_replaces_stale_slot(defaults):
    legacy = targets.registered_targets()
    sink = {"kind": "stale", "secret": "stale-canary"}
    observed = targets.registered_targets(provenance_sink=sink)
    assert observed is legacy is defaults
    assert sink == {
        "kind": "builtin", "path": None, "identity": None,
        "targets": {spec.target_id: {"kind": "builtin", "path": None} for spec in defaults},
    }


def test_explicit_overrides_and_additions_keep_merge_order_and_per_target_origins(
    defaults, tmp_path, monkeypatch,
):
    override = {**asdict(defaults[1]), "label": "private-content-canary"}
    added = {**asdict(defaults[0]), "target_id": "synthetic-added"}
    path = _write_registry(tmp_path, monkeypatch, [added, override])
    legacy = targets.registered_targets()
    sink = {}
    observed = targets.registered_targets(provenance_sink=sink)
    assert observed == legacy
    assert [spec.target_id for spec in observed] == ["synthetic-a", "synthetic-b", "synthetic-added"]
    assert sink == {
        "kind": "explicit_file", "path": str(path), "identity": _fingerprint(path.stat()),
        "targets": {
            "synthetic-a": {"kind": "builtin", "path": None},
            "synthetic-b": {"kind": "explicit_file", "path": str(path)},
            "synthetic-added": {"kind": "explicit_file", "path": str(path)},
        },
    }
    assert "private-content-canary" not in json.dumps(sink)


def test_explicit_empty_registry_records_file_branch_and_builtin_fallbacks(
    defaults, tmp_path, monkeypatch,
):
    path = _write_registry(tmp_path, monkeypatch, [])
    sink = {}
    assert targets.registered_targets(provenance_sink=sink) == defaults
    assert sink["kind"] == "explicit_file" and sink["path"] == str(path)
    assert sink["targets"] == {
        spec.target_id: {"kind": "builtin", "path": None} for spec in defaults
    }


def test_provenance_adds_no_environment_or_content_read(defaults, tmp_path, monkeypatch):
    _write_registry(tmp_path, monkeypatch, [asdict(defaults[0])])
    events = []
    actual_open = builtins.open
    actual_load = json.load
    actual_get = os.environ.get

    def observed_open(*args, **kwargs):
        events.append("open")
        return actual_open(*args, **kwargs)

    def observed_load(*args, **kwargs):
        events.append("json.load")
        return actual_load(*args, **kwargs)

    def observed_get(*args, **kwargs):
        events.append(("env.get", args[0]))
        return actual_get(*args, **kwargs)

    monkeypatch.setattr(targets, "open", observed_open, raising=False)
    monkeypatch.setattr(targets.json, "load", observed_load)
    monkeypatch.setattr(targets.os.environ, "get", observed_get)
    legacy = targets.registered_targets()
    legacy_events = list(events)
    events.clear()
    assert targets.registered_targets(provenance_sink={}) == legacy
    assert events == legacy_events == [
        ("env.get", targets.TARGET_CAPABILITY_PROFILE_REGISTRY_ENV), "open", "json.load",
    ]


@pytest.mark.parametrize("payload", [
    {},
    [{"invalid_field": True}],
    [{"target_id": "synthetic-duplicate", "label": "synthetic", "ssh_host": "",
      "endpoint": "http://synthetic.invalid", "model": "synthetic"}] * 2,
    [{"target_id": targets.TARGET_MSR1, "label": "synthetic", "ssh_host": "",
      "endpoint": "http://synthetic.invalid", "model": "synthetic", "roles": [targets.ROLE_INFERENCE]}],
])
def test_registry_validation_errors_are_unchanged_and_do_not_publish(
    defaults, tmp_path, monkeypatch, payload,
):
    _write_registry(tmp_path, monkeypatch, payload)
    with pytest.raises((ValueError, TypeError)) as legacy:
        targets.registered_targets()
    sink = {"kind": "stale", "targets": {"stale": {"kind": "builtin"}}}
    with pytest.raises(type(legacy.value), match=str(legacy.value).replace("(", r"\(").replace(")", r"\)")) as observed:
        targets.registered_targets(provenance_sink=sink)
    assert str(observed.value) == str(legacy.value)
    assert sink == _unknown()


def test_malformed_json_preserves_error_closes_fd_and_clears_prior_origin(
    defaults, tmp_path, monkeypatch,
):
    path = _write_registry(tmp_path, monkeypatch, [])
    path.write_text("{not-json", encoding="utf-8")
    handles = []
    actual_open = builtins.open

    def observed_open(*args, **kwargs):
        handle = actual_open(*args, **kwargs)
        handles.append(handle)
        return handle

    monkeypatch.setattr(targets, "open", observed_open, raising=False)
    sink = {"kind": "stale"}
    with pytest.raises(json.JSONDecodeError):
        targets.registered_targets(provenance_sink=sink)
    assert len(handles) == 1 and handles[0].closed
    assert sink == _unknown()


def test_missing_file_preserves_error_and_clears_prior_origin(defaults, tmp_path, monkeypatch):
    monkeypatch.setenv(targets.TARGET_CAPABILITY_PROFILE_REGISTRY_ENV, str(tmp_path / "missing.json"))
    sink = {"kind": "stale"}
    with pytest.raises(FileNotFoundError):
        targets.registered_targets(provenance_sink=sink)
    assert sink == _unknown()


@pytest.mark.parametrize("failure", ["fstat_error", "unstable_fd"])
def test_optional_identity_failure_does_not_change_normal_success(
    defaults, tmp_path, monkeypatch, failure,
):
    path = _write_registry(tmp_path, monkeypatch, [asdict(defaults[0])])
    legacy = targets.registered_targets()
    actual_fstat = os.fstat
    calls = []

    def observed_fstat(fd):
        calls.append(fd)
        if failure == "fstat_error":
            raise OSError("synthetic unavailable metadata")
        info = actual_fstat(fd)
        values = {"st_" + name: value for name, value in _fingerprint(info).items()}
        if len(calls) == 2:
            values["st_mtime_ns"] += 1
        return SimpleNamespace(**values)

    monkeypatch.setattr(targets.os, "fstat", observed_fstat)
    sink = {}
    assert targets.registered_targets(provenance_sink=sink) == legacy
    assert sink["kind"] == "explicit_file" and sink["path"] == str(path)
    assert sink["identity"] is None
    assert len(calls) == 2 and calls[0] == calls[1]


class _ForbiddenSink(dict):
    def clear(self):
        raise AssertionError("a dict subclass is not a metadata channel")

    def update(self, *args, **kwargs):
        raise AssertionError("a dict subclass is not a metadata channel")


@pytest.mark.parametrize("sink", [None, _ForbiddenSink(), Mock()])
def test_non_plain_sink_is_ignored_without_extra_metadata_operations(
    defaults, tmp_path, monkeypatch, sink,
):
    _write_registry(tmp_path, monkeypatch, [])
    fstat = Mock(side_effect=AssertionError("legacy read must not acquire metadata"))
    monkeypatch.setattr(targets.os, "fstat", fstat)
    assert targets.registered_targets(provenance_sink=sink) == defaults
    fstat.assert_not_called()
    if isinstance(sink, Mock):
        sink.assert_not_called()


def test_fleet_forwards_exact_plain_sink_and_keeps_legacy_call_shape(defaults, monkeypatch):
    reader = Mock(return_value=defaults)
    monkeypatch.setattr(targets, "registered_targets", reader)
    store = SimpleNamespace(current_for_host=Mock(side_effect=AssertionError("no inference target")))
    legacy = fleet.persisted_routing_inputs(store)
    assert reader.call_args == call()
    sink = {}
    observed = fleet.persisted_routing_inputs(store, provenance_sink=sink)
    assert observed == legacy
    assert reader.call_args == call(provenance_sink=sink)
    reader.reset_mock()
    assert fleet.persisted_routing_inputs(store, provenance_sink=_ForbiddenSink()) == legacy
    assert reader.call_args == call()
    store.current_for_host.assert_not_called()


def test_supplied_specs_skip_registry_and_replace_stale_provenance(defaults, monkeypatch):
    reader = Mock(side_effect=AssertionError("supplied specs must not load registry"))
    monkeypatch.setattr(targets, "registered_targets", reader)
    store = SimpleNamespace(current_for_host=Mock(side_effect=AssertionError("no inference target")))
    legacy = fleet.persisted_routing_inputs(store, specs=defaults)
    sink = {"kind": "stale", "identity": {"stale": True}}
    observed = fleet.persisted_routing_inputs(store, specs=iter(defaults), provenance_sink=sink)
    assert observed == legacy
    assert sink == {"kind": "provided_specs", "path": None, "identity": None, "targets": {}}
    reader.assert_not_called()
    store.current_for_host.assert_not_called()
