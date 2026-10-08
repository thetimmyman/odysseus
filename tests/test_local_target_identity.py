import copy
import hashlib
import json

import pytest

from src import local_target_identity as identity
from src.local_targets import RuntimeIdentity, ModelIdentity, ContextProfile, HostBaseline


def fixture(kind="ollama"):
    runtime = RuntimeIdentity(runtime_kind=kind, provider=kind, endpoint_type="http", endpoint_url="http://localhost:9",
                              repository="catalog", version="1", image_digest="image", backend_version="driver").to_dict()
    model = ModelIdentity(model_id="model", alias="model", family="family", digest="model-digest", size_bytes=10,
                          quantization="quant", declared_capabilities=("tools",)).to_dict()
    context = ContextProfile(configured_context=8192).to_dict()
    host = HostBaseline(host_id="host", cpu_arch="arch", gpu="gpu", kernel="kernel", boot_cmdline_digest="boot",
                        firmware="firmware").to_dict()
    receipt = {"host_id": "host", "runtime": runtime, "model": model, "context": context, "host": host}
    raw = {"kind": kind, "binding": {"endpoint_bound": True}, "endpoint_url": runtime["endpoint_url"],
           "host_baseline": {"cpu_arch": "arch", "kernel": "kernel", "boot_cmdline_digest": "boot",
                             "gpu_driver_firmware": "gpu, driver, firmware"},
           "runtime_image_digest": "image", "model_manifest_sha256": "model-digest",
           "api_version": {"version": "1"}, "api_tags": {"models": [{"name": "model", "digest": "model-digest", "size": 10}]},
           "api_show": {"details": {"family": "family", "quantization_level": "quant"}, "capabilities": ["tools"], "template": "template"},
           "runtime_environment_sha256": "env-digest"}
    request = {"num_ctx": 8192, "num_predict": 512, "temperature": 0, "think": False}
    receipt["context"]["options"] = {**request, "template_sha256": hashlib.sha256(b"template").hexdigest(),
                                          "runtime_environment_sha256": "env-digest"}
    if kind == "halogen-flash":
        raw.update(image_digest="image", checkpoint_sha256="model-digest", checkpoint_size_bytes=10, template_sha256="template",
                   labels={"org.opencontainers.image.revision": "commit"},
                   health={"version": {"api": "1", "engine": "1"}, "model": "model", "context": 8192, "slot_ctx": 4096,
                           "indexer_budget": 256, "drafter_default": "mtp", "prompt_lookup": {"ngram": 3},
                           "prompt_cache": {"enabled": True}, "kv_pool_positions": 16384, "slots": 4})
        raw["host_baseline"].update(gpu=["gpu"], firmware="UNKNOWN", mesa="UNKNOWN", rocm="UNKNOWN", libhsakmt="UNKNOWN")
        receipt["host"].update(firmware="UNKNOWN", mesa="UNKNOWN", rocm="UNKNOWN", libhsakmt="UNKNOWN")
        receipt["runtime"]["commit"] = "commit"
        request = {"temperature": 0, "max_tokens": 512, "enable_thinking": False, "reasoning_effort": "none"}
        receipt["model"].update(declared_context=8192, auxiliary_artifacts=["embedded-MTP:model-digest", "template:template"])
        receipt["context"].update(configured_served_context=4096, options={**request, "template_sha256": "template",
                    "indexer_budget": 256, "drafter": "mtp", "prompt_lookup": {"ngram": 3}, "prompt_cache": {"enabled": True},
                    "kv_pool_positions": 16384, "slots": 4})
    return raw, receipt, request


@pytest.mark.parametrize("kind", ["ollama", "halogen-flash"])
def test_material_shape_matches_native_receipt_and_ignores_qualification(kind):
    raw, receipt, request = fixture(kind)
    material = identity.normalize_identity(raw, receipt, request_options=request)
    assert material == identity._material(receipt)
    assert identity.compare_material(raw, receipt, request_options=request)["equal"]
    changed = copy.deepcopy(receipt)
    changed.update(observed_at="future", qualification_ref="new-proof", ttl_s=1)
    changed["context"].update(safe_working_context=1, safe_context_source="different")
    assert identity._material(changed) == material


@pytest.mark.parametrize("path,value", [(("host_baseline", "kernel"), "changed"),
                                        (("host_baseline", "cpu_arch"), "changed"),
                                        (("runtime_environment_sha256",), "changed"),
                                        (("api_version", "version"), "changed"),
                                        (("api_show", "template"), "changed")])
def test_ollama_material_drift_is_detected(path, value):
    raw, receipt, request = fixture()
    target = raw
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    compared = identity.compare_material(raw, receipt, request_options=request)
    assert not compared["equal"] and compared["changed_paths"]


@pytest.mark.parametrize("key", ["slot_ctx", "context", "indexer_budget", "slots", "kv_pool_positions"])
def test_flash_runtime_option_drift_is_detected(key):
    raw, receipt, request = fixture("halogen-flash")
    raw["health"][key] += 1
    assert not identity.compare_material(raw, receipt, request_options=request)["equal"]


@pytest.mark.parametrize("kind", ["ollama", "halogen-flash"])
def test_new_artifact_cannot_inherit_catalog_or_host_libraries(kind):
    raw, receipt, request = fixture(kind)
    raw["runtime_image_digest" if kind == "ollama" else "image_digest"] = "different"
    with pytest.raises(ValueError, match="cannot be inherited"):
        identity.normalize_identity(raw, receipt, request_options=request)
    raw, receipt, request = fixture(kind)
    receipt["host"]["mesa"] = "known-version"
    with pytest.raises(ValueError, match="independently collected"):
        identity.normalize_identity(raw, receipt, request_options=request)


def test_binding_fails_closed_and_hashes_sensitive_values():
    config = {"health_url": "http://localhost:1234"}
    row = {"Id": "container", "Image": "image", "State": {"Pid": 1, "StartedAt": "start"},
           "Config": {"Env": ["PRIVATE_TOKEN=never-emit"], "Cmd": ["private-command"]},
           "NetworkSettings": {"Ports": {"1234/tcp": [{"HostPort": "1234", "HostIp": "0.0.0.0"}]}}}
    binding = identity._binding(row, config)
    assert binding["endpoint_bound"]
    assert "never-emit" not in str(binding) and "private-command" not in str(binding)
    row["NetworkSettings"]["Ports"]["1234/tcp"][0]["HostIp"] = "192.0.2.1"
    with pytest.raises(ValueError, match="not bound"):
        identity._binding(row, config)


def test_request_and_transport_are_actual_inputs_not_old_receipt_values():
    raw, receipt, request = fixture()
    request["num_ctx"] = 1024
    compared = identity.compare_material(raw, receipt, request_options=request, endpoint_url="http://localhost:8")
    assert set(compared["changed_paths"]) == {"runtime.endpoint_url", "context.configured_context", "context.options.num_ctx"}


def test_full_hash_rejects_concurrent_artifact_change(monkeypatch):
    outputs = iter(["10:1:2:3", "digest  path", "10:1:2:4"])
    monkeypatch.setattr(identity, "_run", lambda *args, **kwargs: next(outputs))
    with pytest.raises(ValueError, match="changed during"):
        identity._artifact({"engine_argv": ["engine"], "container": "container"}, "path")


def test_remote_transport_is_configured_and_private_errors_are_withheld(monkeypatch):
    class Result:
        returncode = 2
        stdout = b""
        stderr = b"PRIVATE_ENDPOINT_AND_TOKEN"
    seen = []
    monkeypatch.setattr(identity.subprocess, "run", lambda argv, **kwargs: (seen.append((argv, kwargs)) or Result()))
    with pytest.raises(ValueError) as error:
        identity.capture({"ssh_argv": ["ssh", "operator-target"], "kind": "ollama"})
    assert "PRIVATE_ENDPOINT" not in str(error.value)
    assert seen[0][0] == ["ssh", "operator-target", "python3 -"]
    assert b"main(['--capture-config-b64'" in seen[0][1]["input"]


def test_wrong_native_address_is_refused_before_provider_reads(monkeypatch):
    monkeypatch.setattr(identity, "_run", lambda *args, **kwargs: json.dumps([{"addr_info": [{"local": "192.0.2.1"}]}]))
    monkeypatch.setattr(identity.socket, "getaddrinfo", lambda *args, **kwargs: [(0, 0, 0, "", ("192.0.2.2", 9))])
    monkeypatch.setattr(identity, "_container", lambda *args: pytest.fail("wrong native endpoint reached provider"))
    with pytest.raises(ValueError, match="native host addresses"):
        identity._capture_local({"kind": "ollama", "endpoint_url": "http://example.invalid:9", "health_url": "http://localhost:9"})


def test_endpoint_can_resolve_to_multiple_owned_native_addresses(monkeypatch):
    monkeypatch.setattr(identity, "_run", lambda *args, **kwargs: json.dumps([{"addr_info": [{"local": "192.0.2.1"}, {"local": "2001:db8::1"}]}]))
    monkeypatch.setattr(identity.socket, "getaddrinfo", lambda *args, **kwargs: [(0, 0, 0, "", ("192.0.2.1", 9)),
                                                                           (0, 0, 0, "", ("2001:db8::1", 9))])
    binding = identity._endpoint_binding({"endpoint_url": "http://example.invalid:9/v1", "health_url": "http://localhost:9/health"})
    assert binding["native_addresses_verified"]


def test_cli_envelope_carries_bound_profile_and_complete_material(tmp_path, monkeypatch, capsys):
    raw, receipt, request = fixture()
    receipt["profile_id"] = "bound-profile"
    raw["observed_at"] = "2026-10-08T00:00:00+00:00"
    receipt_path, config_path = tmp_path / "receipt.json", tmp_path / "private.json"
    receipt_path.write_text(json.dumps(receipt))
    config_path.write_text(json.dumps({"request_options": request, "endpoint_url": raw["endpoint_url"]}))
    monkeypatch.setattr(identity, "capture", lambda config: raw)
    assert identity.main(["--receipt", str(receipt_path), "--config", str(config_path)]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["profile_id"] == "bound-profile"
    assert envelope["current_material_identity"] == identity._material(receipt)
    assert envelope["comparison"]["equal"]


def test_identity_http_uses_direct_transport_and_refuses_redirects(monkeypatch):
    seen = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self):
            return b'{"ok": true}'
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://localhost:9/api/version"
            assert timeout == 20
            return Response()
    monkeypatch.setattr(identity.urllib.request, "build_opener", lambda *handlers: (seen.extend(handlers) or Opener()))
    assert identity._api("http://localhost:9", "/api/version") == {"ok": True}
    assert seen[0].proxies == {}
    with pytest.raises(ValueError, match="redirects"):
        seen[1].redirect_request(None, None, 302, "redirect", {}, "http://elsewhere.invalid")


def test_published_endpoint_requires_each_actual_address_not_only_loopback():
    row={'NetworkSettings':{'Ports':{'9/tcp':[{'HostPort':'9','HostIp':'127.0.0.1'}]}}}
    endpoint={'port':9,'resolved_addresses':['192.0.2.1']}
    with pytest.raises(ValueError,match='not published'):
        identity._published_endpoint(row,endpoint)
    row['NetworkSettings']['Ports']['9/tcp'][0]['HostIp']='0.0.0.0'
    identity._published_endpoint(row,endpoint)
    endpoint['resolved_addresses'].append('2001:db8::1')
    with pytest.raises(ValueError,match='not published'):
        identity._published_endpoint(row,endpoint)
    row['NetworkSettings']['Ports']['9/tcp'].append({'HostPort':'9','HostIp':'::'})
    identity._published_endpoint(row,endpoint)
