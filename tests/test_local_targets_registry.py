"""Registry loading and derivation from observed servers (TMOS-224).

Covers:
1. DEFAULT_TARGETS keeps only a minimal unqualified fallback.
2. Registry generation from TMOS observed graph and from broker /state, with
   one canonical endpoint spelling per server, Framework on :8731 serving
   halogen-qwen3.8-flash-next, and no msr1 generation target.
3. Deployed default registry file loading (LOCAL_TARGETS_REGISTRY_FILE) and
   PS632_PROFILE_REGISTRY override precedence.
4. Negative control: registry entries whose endpoints fail health are never
   returned as selectable (both live probe and persisted capability store).
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from src import constants
from src import local_target_routing as ltr
from src import local_targets as lt
from src.target_capability_store import TargetCapabilityStore


def _synthetic_observed_graph() -> dict:
    return {
        "schema_ref": "tmos/graph-ir/v1",
        "generated_at": "2026-11-15T15:32:09Z",
        "nodes": [
            {
                "node_id": "host.framework",
                "kind": "Host",
                "name": "framework",
                "description": "observed host: hostname=framework arch=x86_64 threads=32 ram_gb=125.1",
                "truth_class": "DECLARED",
            },
            {
                "node_id": "host.minipc",
                "kind": "Host",
                "name": "minipc",
                "description": "observed host: hostname=tacticusanalytics arch=x86_64 threads=20 ram_gb=62.5",
                "truth_class": "DECLARED",
            },
            {
                "node_id": "host.msr1",
                "kind": "Host",
                "name": "msr1",
                "description": "observed host: hostname=ms-r1-builder arch=aarch64 threads=12 ram_gb=62.3",
                "truth_class": "OBSERVED",
            },
            {
                "node_id": "component.halogen.flash-server",
                "kind": "Component",
                "name": "Halogen Flash",
                "lifecycle": "active",
                "truth_class": "DECLARED",
                "exposes_api": ["http:POST /v1/chat/completions", "http:GET /health"],
            },
            {
                "node_id": "component.unit.framework.halogen-flash.service",
                "kind": "Component",
                "name": "halogen-flash.service",
                "description": "systemd system unit: active/running",
                "truth_class": "OBSERVED",
            },
            {
                "node_id": "component.unit.minipc.ollama.service",
                "kind": "Component",
                "name": "ollama.service",
                "description": "systemd system unit: active/running",
                "truth_class": "OBSERVED",
            },
            {
                "node_id": "component.unit.msr1.tmos-agent-worker.service",
                "kind": "Component",
                "name": "tmos-agent-worker.service",
                "description": "systemd system unit: active/running",
                "truth_class": "OBSERVED",
            },
            {
                "node_id": "component.process.msr1.paseo-daemon",
                "kind": "Component",
                "name": "paseo-daemon",
                "description": "daemon process observed",
                "truth_class": "OBSERVED",
            },
        ],
        "edges": [
            {
                "relation": "VERIFIED_BY",
                "source": "host.framework",
                "target": "tool.fabric-observe.probe",
                "truth_class": "OBSERVED",
            },
            {
                "relation": "VERIFIED_BY",
                "source": "host.minipc",
                "target": "tool.fabric-observe.probe",
                "truth_class": "OBSERVED",
            },
            {
                "relation": "VERIFIED_BY",
                "source": "host.msr1",
                "target": "tool.fabric-observe.probe",
                "truth_class": "OBSERVED",
            },
            {
                "relation": "RUNS_ON",
                "source": "component.halogen.flash-server",
                "target": "host.framework",
                "truth_class": "DECLARED",
            },
            {
                "relation": "RUNS_ON",
                "source": "component.unit.framework.halogen-flash.service",
                "target": "host.framework",
                "truth_class": "OBSERVED",
            },
            {
                "relation": "RUNS_ON",
                "source": "component.unit.minipc.ollama.service",
                "target": "host.minipc",
                "truth_class": "OBSERVED",
            },
            {
                "relation": "RUNS_ON",
                "source": "component.unit.msr1.tmos-agent-worker.service",
                "target": "host.msr1",
                "truth_class": "OBSERVED",
            },
            {
                "relation": "RUNS_ON",
                "source": "component.process.msr1.paseo-daemon",
                "target": "host.msr1",
                "truth_class": "OBSERVED",
            },
        ],
    }


def _synthetic_server_observations() -> list[dict]:
    """Includes Framework :8731, stale msr1 candidate, and three RTX spellings."""
    return [
        {
            "host": "framework",
            "target_id": "local-framework",
            "label": "Framework Desktop (Halogen Flash)",
            "endpoint": "HTTP://Framework.Example.Invalid:8731/v1/",
            "model": "halogen-qwen3.8-flash-next",
            "runtime_kind": "halogen-flash",
            "service_unit": "halogen-flash.service",
        },
        # Stale candidate for msr1: observed graph has NO inference unit on msr1,
        # so this must never become a generation target.
        {
            "host": "msr1",
            "target_id": "local-msr1",
            "label": "MS-R1",
            "endpoint": "http://127.0.0.1:11434",
            "model": "qwen3.8:27b",
            "runtime_kind": "ollama",
            "service_unit": "ollama.service",
        },
        # Three different endpoint spellings across the estate for minipc RTX:
        {
            "host": "minipc",
            "target_id": "local-rtx4500",
            "label": "Mini PC RTX 4500 Ada",
            "endpoint": "http://127.0.0.1:11434",
            "model": "qwen3.8:27b",
            "runtime_kind": "ollama",
            "service_unit": "ollama.service",
        },
        {
            "host": "minipc",
            "target_id": "local-rtx4500",
            "label": "Mini PC RTX 4500 Ada",
            "endpoint": "HTTP://TacticusAnalytics-1.Example.Invalid:11434/",
            "model": "qwen3.8:27b",
            "runtime_kind": "ollama",
            "service_unit": "ollama.service",
        },
        {
            "host": "minipc",
            "target_id": "local-rtx4500",
            "label": "Mini PC RTX 4500 Ada",
            "endpoint": "http://198.51.100.42:11434/v1/",
            "model": "qwen3.8:27b",
            "runtime_kind": "ollama",
            "service_unit": "ollama.service",
        },
    ]


def test_default_targets_keeps_only_minimal_unqualified_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv(lt.TARGET_CAPABILITY_PROFILE_REGISTRY_ENV, raising=False)
    monkeypatch.setattr(lt, "LOCAL_TARGETS_REGISTRY_FILE", str(tmp_path / "absent.json"))

    assert hasattr(constants, "LOCAL_TARGETS_REGISTRY_FILE")
    assert len(lt.DEFAULT_TARGETS) == 1
    fallback = lt.DEFAULT_TARGETS[0]
    assert fallback.qualification_ref == ""
    assert "msr1" not in fallback.target_id
    assert "framework" not in fallback.target_id
    assert fallback.ssh_host == ""

    sink = {}
    loaded = lt.registered_targets(provenance_sink=sink)
    assert loaded == lt.DEFAULT_TARGETS
    assert sink["kind"] == "builtin"

    store = TargetCapabilityStore(str(tmp_path / "store"))
    inputs = ltr.persisted_routing_inputs(store)
    assert inputs.target_ids() == ()
    assert all("unqualified" in item["reason"] for item in inputs.skipped)


def test_generated_graph_registry_loads_framework_on_8731_and_excludes_msr1(
    tmp_path, monkeypatch,
):
    graph = _synthetic_observed_graph()
    observations = _synthetic_server_observations()
    aliases = {
        "198.51.100.42": "minipc.example.invalid",
        "tacticusanalytics-1.example.invalid": "minipc.example.invalid",
    }

    derived_forward = lt.derive_local_target_registry(
        observed_graph=graph,
        server_endpoints=observations,
        host_aliases=aliases,
    )
    derived_reverse = lt.derive_local_target_registry(
        observed_graph=graph,
        server_endpoints=list(reversed(observations)),
        host_aliases=aliases,
    )
    assert derived_forward == derived_reverse

    registry_file = tmp_path / "local_targets.json"
    lt.write_local_target_registry(derived_forward, registry_file)

    monkeypatch.delenv(lt.TARGET_CAPABILITY_PROFILE_REGISTRY_ENV, raising=False)
    monkeypatch.setattr(lt, "LOCAL_TARGETS_REGISTRY_FILE", str(registry_file))

    sink = {}
    loaded = lt.registered_targets(provenance_sink=sink)
    assert sink["kind"] == "file"
    assert sink["path"] == str(registry_file)

    by_id = {spec.target_id: spec for spec in loaded}
    assert "local-framework" in by_id
    fw = by_id["local-framework"]
    assert fw.ssh_host == ""
    assert fw.transport == lt.TRANSPORT_HTTP
    assert fw.endpoint == "http://framework.example.invalid:8731/v1"
    assert ":8731" in fw.endpoint
    assert fw.model == "halogen-qwen3.8-flash-next"
    assert fw.runtime_kind == "halogen-flash"
    assert lt.ROLE_INFERENCE in fw.roles
    assert fw.qualification_ref == ""

    # No msr1 generation target (nor any msr1 entry at all).
    assert not [
        spec for spec in loaded
        if "msr1" in spec.target_id or spec.ssh_host == "msr1"
    ]

    # One canonical endpoint spelling per server: all three minipc RTX spellings
    # collapsed to one canonical routable URL.
    rtx = by_id["local-rtx4500"]
    assert rtx.ssh_host == ""
    assert rtx.transport == lt.TRANSPORT_HTTP
    assert rtx.endpoint == "http://minipc.example.invalid:11434"
    assert len(loaded) == 2
    assert len({spec.endpoint for spec in loaded}) == 2


def test_generated_broker_state_registry_and_cli_script(tmp_path, monkeypatch):
    broker_state = {
        "gpus": {
            "gpus": [
                {"gpu": "Radeon 8060S", "host": "Framework (Halogen)", "pool": "halogen"},
                {"gpu": "RTX 4500 Ada", "host": "minipc eGPU", "pool": "rtx4500"},
                {
                    "gpu": "RTX 2080 Ti",
                    "host": "TMOS workstation",
                    "pool": None,
                    "excluded": "desktop interactive",
                },
            ]
        },
        "health": {
            "halogen": {"ok": True, "slots": 3},
            "rtx4500": {"ok": True, "slots": 1},
        },
    }
    servers_payload = {
        "host_aliases": {
            "tacticusanalytics-1.example.invalid": "minipc.example.invalid",
            "198.51.100.42": "minipc.example.invalid",
        },
        "servers": [
            {
                "pool": "halogen",
                "host": "framework",
                "target_id": "local-framework",
                "label": "Framework Halogen",
                "endpoint": "http://framework.example.invalid:8731/v1/",
                "model": "halogen-qwen3.8-flash-next",
                "runtime_kind": "halogen-flash",
            },
            {
                "pool": "rtx4500",
                "host": "minipc",
                "target_id": "local-rtx4500",
                "label": "Mini PC RTX",
                "endpoints": [
                    "http://127.0.0.1:11434",
                    "http://TacticusAnalytics-1.Example.Invalid:11434",
                    "http://198.51.100.42:11434/",
                ],
                "model": "qwen3.8:27b",
                "runtime_kind": "ollama",
            },
            {
                "pool": "retired-msr1",
                "host": "msr1",
                "target_id": "local-msr1",
                "endpoint": "http://msr1.example.invalid:11434",
                "model": "qwen3.8:27b",
                "runtime_kind": "ollama",
            },
        ],
    }

    state_file = tmp_path / "broker-state.json"
    servers_file = tmp_path / "servers.json"
    out_file = tmp_path / "generated-registry.json"
    state_file.write_text(json.dumps(broker_state), encoding="utf-8")
    servers_file.write_text(json.dumps(servers_payload), encoding="utf-8")

    script = Path(__file__).resolve().parents[1] / "scripts" / "odysseus-local-targets"
    proc = subprocess.run(
        [
            sys.executable,
            str(script),
            "--broker-state",
            str(state_file),
            "--servers",
            str(servers_file),
            "--output",
            str(out_file),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    summary = json.loads(proc.stdout)
    assert summary["target_ids"] == ["local-framework", "local-rtx4500"]

    monkeypatch.delenv(lt.TARGET_CAPABILITY_PROFILE_REGISTRY_ENV, raising=False)
    monkeypatch.setattr(lt, "LOCAL_TARGETS_REGISTRY_FILE", str(out_file))
    loaded = lt.registered_targets()
    assert [s.target_id for s in loaded] == ["local-framework", "local-rtx4500"]
    assert loaded[0].endpoint == "http://framework.example.invalid:8731/v1"
    assert loaded[0].model == "halogen-qwen3.8-flash-next"
    assert loaded[1].endpoint == "http://minipc.example.invalid:11434"


def test_negative_control_unhealthy_registry_entry_is_never_selectable(
    tmp_path, monkeypatch,
):
    """NEGATIVE CONTROL: a registry entry whose endpoint fails health must never
    be returned as selectable."""
    registry_file = tmp_path / "local_targets.json"
    specs = [
        lt.LocalTargetSpec(
            target_id="local-framework",
            label="Framework Halogen",
            ssh_host="framework",
            endpoint="http://framework.example.invalid:8731/v1",
            model="halogen-qwen3.8-flash-next",
            runtime_kind="halogen-flash",
            roles=(lt.ROLE_INFERENCE,),
        ),
        lt.LocalTargetSpec(
            target_id="local-rtx4500",
            label="Mini PC RTX",
            ssh_host="minipc",
            endpoint="http://minipc.example.invalid:11434",
            model="qwen3.8:27b",
            runtime_kind="ollama",
            roles=(lt.ROLE_INFERENCE,),
        ),
    ]
    lt.write_local_target_registry(specs, registry_file)
    monkeypatch.delenv(lt.TARGET_CAPABILITY_PROFILE_REGISTRY_ENV, raising=False)
    monkeypatch.setattr(lt, "LOCAL_TARGETS_REGISTRY_FILE", str(registry_file))

    class AllUnhealthyInspector(lt.OllamaInspector):
        def __init__(self):
            super().__init__(probe_tools=False)

        def api(self, spec, path, body=None):
            if spec.target_id == "local-framework" and path == "/health":
                # Front-end responds HTTP 200, but engine.responds is False -> unhealthy!
                return {
                    "ok": True,
                    "http": "200",
                    "body": {"status": "ok", "engine": {"responds": False}},
                    "err": "",
                }
            return {"ok": False, "http": "", "body": {}, "err": "connection refused"}

    unhealthy_records = lt.probe_fleet(inspector=AllUnhealthyInspector())
    assert [r.target_id for r in unhealthy_records] == ["local-framework", "local-rtx4500"]
    assert all(r.health != lt.HEALTH_HEALTHY for r in unhealthy_records)
    assert "health_check_failed" in unhealthy_records[0].failure_classes
    assert "runtime_unreachable" in unhealthy_records[1].failure_classes

    with pytest.raises(lt.LocalTargetUnavailable) as exc_info:
        lt.select_local_target(inspector=AllUnhealthyInspector())
    assert "local-framework" in str(exc_info.value)
    assert "local-rtx4500" in str(exc_info.value)

    # Even when another target IS healthy, an unhealthy Framework entry is never
    # returned when pinned, and is skipped in unpinned selection.
    class FrameworkDeadMinipcLiveInspector(lt.OllamaInspector):
        def __init__(self):
            super().__init__(probe_tools=False)

        def api(self, spec, path, body=None):
            if spec.target_id == "local-framework":
                return {
                    "ok": True,
                    "http": "200",
                    "body": {"status": "error", "error": "backend down"},
                    "err": "",
                }
            if path == "/api/version":
                return {"ok": True, "http": "200", "body": {"version": "0.33.3"}, "err": ""}
            if path == "/api/tags":
                return {
                    "ok": True,
                    "http": "200",
                    "body": {"models": [{"name": spec.model, "size": 1024, "details": {}}]},
                    "err": "",
                }
            if path == "/api/ps":
                return {"ok": True, "http": "200", "body": {"models": []}, "err": ""}
            raise AssertionError(f"unexpected path: {path}")

    with pytest.raises(lt.LocalTargetUnavailable):
        lt.select_local_target(
            pin="local-framework",
            inspector=FrameworkDeadMinipcLiveInspector(),
        )
    chosen = lt.select_local_target(inspector=FrameworkDeadMinipcLiveInspector())
    assert chosen.target_id == "local-rtx4500"

    # Positive control: when Framework /health and /v1/models succeed and minipc
    # fails health, Framework (:8731, halogen-qwen3.8-flash-next) is selected.
    class FrameworkLiveMinipcDeadInspector(lt.OllamaInspector):
        def __init__(self):
            super().__init__(probe_tools=False)

        def api(self, spec, path, body=None):
            if spec.target_id == "local-rtx4500":
                return {"ok": False, "http": "", "body": {}, "err": "connection refused"}
            if path == "/health":
                return {
                    "ok": True,
                    "http": "200",
                    "body": {
                        "status": "ok",
                        "engine": {"responds": True},
                        "version": {"api": "0.4.2"},
                    },
                    "err": "",
                }
            if path == "/v1/models":
                return {
                    "ok": True,
                    "http": "200",
                    "body": {
                        "data": [
                            {
                                "id": "halogen-qwen3.8-flash-next",
                                "context_length": 262144,
                            }
                        ]
                    },
                    "err": "",
                }
            raise AssertionError(f"unexpected path: {path}")

    chosen_fw = lt.select_local_target(inspector=FrameworkLiveMinipcDeadInspector())
    assert chosen_fw.target_id == "local-framework"
    assert chosen_fw.model_id == "halogen-qwen3.8-flash-next"
    assert chosen_fw.health == lt.HEALTH_HEALTHY


def test_negative_control_unhealthy_persisted_receipt_is_never_selectable(
    tmp_path, monkeypatch,
):
    """NEGATIVE CONTROL (persisted capability store): a qualified registry entry
    whose health observation fails is skipped and never returned as selectable."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    spec = lt.LocalTargetSpec(
        target_id="local-framework",
        label="Framework Halogen",
        ssh_host="",
        endpoint="http://framework.example.invalid:8731/v1",
        model="halogen-qwen3.8-flash-next",
        runtime_kind="halogen-flash",
        roles=(lt.ROLE_INFERENCE,),
        qualification_ref="qualified-framework-synthetic",
    )
    registry_file = tmp_path / "local_targets.json"
    lt.write_local_target_registry([spec], registry_file)
    monkeypatch.delenv(lt.TARGET_CAPABILITY_PROFILE_REGISTRY_ENV, raising=False)
    monkeypatch.setattr(lt, "LOCAL_TARGETS_REGISTRY_FILE", str(registry_file))

    store = TargetCapabilityStore(str(tmp_path / "store"))
    receipt = lt.make_target_capability_receipt(
        host_id=spec.target_id,
        profile_id="derived",
        observed_at=now,
        ttl_s=3600,
        health_ttl_s=300,
        health=lt.HEALTH_HEALTHY,
        health_checked_at=now,
        runtime=lt.RuntimeIdentity(
            runtime_kind="halogen-flash",
            provider="halogen-flash",
            version="0.4.2",
            image_digest="a" * 64,
            endpoint_type="http",
            endpoint_url=spec.endpoint,
            backend="hip",
        ),
        model=lt.ModelIdentity(
            model_id=spec.model,
            digest="b" * 64,
            quantization="Q4_K_M",
        ),
        context=lt.ContextProfile(
            configured_context=262144,
            configured_served_context=262144,
            safe_working_context=32768,
            safe_context_source="engine-demonstrated; semantic-verified",
            engine_demonstrated_context=32768,
            semantic_verified_context=32768,
        ),
        capabilities=lt.CapabilityEvidence(
            measured=("text_generation", "single_tool_call", "native_tools", "context_integrity")
        ),
        host=lt.HostBaseline(host_id=spec.target_id, kernel="synthetic"),
        roles=(lt.ROLE_INFERENCE,),
        qualification_ref=spec.qualification_ref,
        schema_version=2,
        qualification_disposition="ADOPT_ROLE_SPECIFIC",
    )
    store.append(receipt)
    assert ltr.persisted_routing_inputs(store).target_ids() == ("local-framework",)

    # Once health fails, the persisted entry must not be returned as selectable.
    store.refresh_observation(
        receipt.profile_id,
        current_identity_digest=receipt.identity_digest(),
        health="unhealthy",
        checked_at=now,
    )
    degraded = ltr.persisted_routing_inputs(store)
    assert degraded.target_ids() == ()
    assert any(
        item["target_id"] == "local-framework" and "liveness not live" in item["reason"]
        for item in degraded.skipped
    )


@pytest.mark.parametrize('body', [{}, [], {'error': 'failed'}, {'status': ''}])
def test_unknown_health_is_not_selectable(body):
    spec = lt.LocalTargetSpec(target_id='local-test', label='test', ssh_host='',
                              endpoint='http://example.invalid:8731', model='m',
                              runtime_kind='halogen-flash', qualification_ref='qualified')

    class Inspector(lt.OllamaInspector):
        def api(self, spec, path, body=None):
            return {'ok': True, 'body': health_body if path == '/health' else {'data': [{'id': 'm'}]}, 'err': ''}

    health_body = body
    with pytest.raises(lt.LocalTargetUnavailable):
        lt.select_local_target(specs=[spec], inspector=Inspector(probe_tools=False))


def test_conflicting_observations_refused_in_either_order():
    state = {'gpus': {'gpus': [{'pool': 'p'}]}}
    rows = [dict(host='node', pool='p', endpoint='http://a.invalid:11434', model='old'),
            dict(host='node', pool='p', endpoint='http://z.invalid:11434', model='new')]
    for order in (rows, rows[::-1]):
        with pytest.raises(ValueError):
            lt.derive_local_target_registry(broker_state=state, server_endpoints=order)


@pytest.mark.parametrize('inventory', [{'servers': 'bad'}, {'servers': [1]},
                                      {'servers': [], 'host_aliases': []},
                                      {'servers': [], 'host_aliases': {'x': 1}}])
def test_cli_refuses_invalid_inventory_without_replacing_output(tmp_path, inventory):
    state = tmp_path / 'state.json'
    state.write_text('{"gpus":{"gpus":[]}}')
    servers = tmp_path / 'servers.json'
    servers.write_text(json.dumps(inventory))
    output = tmp_path / 'output.json'
    output.write_bytes(b'previous registry\n')
    result = subprocess.run([sys.executable, 'scripts/odysseus-local-targets', '--broker-state', str(state),
                             '--servers', str(servers), '--output', str(output)], capture_output=True)
    assert result.returncode == 2
    assert output.read_bytes() == b'previous registry\n'


def test_distinct_instances_on_one_host_remain_separate():
    state = {'gpus': {'gpus': [{'pool': 'p'}]}}
    rows = [dict(host='node', pool='p', target_id='first', endpoint='http://node:11434', model='a'),
            dict(host='node', pool='p', target_id='second', endpoint='http://node:8731', model='b', runtime_kind='halogen')]
    forward = lt.derive_local_target_registry(broker_state=state, server_endpoints=rows)
    reverse = lt.derive_local_target_registry(broker_state=state, server_endpoints=rows[::-1])
    assert forward == reverse
    assert [(s.target_id, s.endpoint, s.model) for s in forward] == [
        ('first', 'http://node:11434', 'a'), ('second', 'http://node:8731', 'b')]


@pytest.mark.parametrize('change', [
    {'endpoint': 'http://other.invalid:11434'},
    {'runtime_kind': 'halogen'},
    {'qualification_ref': 'other'},
    {'target_id': 'other'},
])
def test_conflicting_identity_or_endpoint_refused_in_either_order(change):
    state = {'gpus': {'gpus': [{'pool': 'p'}]}}
    row = dict(host='node', pool='p', target_id='first', endpoint='http://node:11434', model='a')
    rows = [row, {**row, **change}]
    for order in (rows, rows[::-1]):
        with pytest.raises(ValueError):
            lt.derive_local_target_registry(broker_state=state, server_endpoints=order)
