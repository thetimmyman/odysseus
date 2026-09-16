"""HaloBox Same-GGUF adapter - HERMETIC controls. No live node, no model.

The adapter exists so an already-qualified non-Ollama profile can be represented,
inspected, receipted and dispatched by the SAME code paths that already do that for
ollama. So these controls are about the three contracts it must not break:

  * the REGISTRY stays profile-scoped - no generic Framework inference capability;
  * the INSPECTOR emits the raw observation shape build_capability already consumes,
    with provenance that does not promote a declaration into a measurement;
  * the CLIENT speaks OpenAI on the wire while presenting the SAME surface the worker
    loop already reads, and cannot choose its own endpoint.
"""
import json
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts", "ps635-live"))

import llama_server_client            # noqa: E402
import ollama_client                  # noqa: E402
import runtime_client                 # noqa: E402
from src import local_targets as lt   # noqa: E402
from src.target_capability_store import TargetCapabilityStore   # noqa: E402
from src.local_target_routing import (                          # noqa: E402
    persisted_routing_inputs, sync_receipts_from_records)

HALOBOX = lt.TARGET_FRAMEWORK_HALOBOX
SHARDS = (
    ("Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf",
     "5ce89370720f8bf90890f439361282104c1aa1482d4013bb9a50923e758e71a4"),
    ("Qwen3.8-Flash-Next-UD-IQ4_XS-00002-of-00003.gguf",
     "577a38a2392b40ca2193cea502e1d92f60b8cd370675d308e0ec21885d9daaa7"),
    ("Qwen3.8-Flash-Next-UD-IQ4_XS-00003-of-00003.gguf",
     "d4634e6d84f0ebb0940be15c90d3790bf6464e3dea3a1cddc567dc0e83ad8833"),
)


def halobox_spec(*, endpoint="http://127.0.0.1:8731", ssh_host="framework",
                 runtime_kind=lt.RUNTIME_LLAMA_SERVER, qualification=None):
    return lt.LocalTargetSpec(
        target_id=HALOBOX, label="halobox test", ssh_host=ssh_host,
        endpoint=endpoint, model=lt.HALOBOX_MODEL_ALIAS,
        runtime_kind=runtime_kind,
        roles=(lt.ROLE_INFERENCE,),
        qualification_ref=(lt.HALOBOX_QUALIFICATION_REF if qualification is None
                           else qualification),
        max_concurrency=4,
        artifact_paths=tuple(f"/models/{name}" for name, _ in SHARDS))


def direct_spec(port):
    """A spec with no ssh hop, so the inspector's HTTP transport is exercised."""
    return halobox_spec(endpoint=f"http://127.0.0.1:{port}", ssh_host="")


# ========================================================= registration ===
def test_the_halobox_profile_is_registered_as_its_own_target():
    spec = lt.target_by_id(HALOBOX)
    assert spec is not None
    assert spec.runtime_kind == lt.RUNTIME_LLAMA_SERVER
    assert spec.endpoint == lt.HALOBOX_ENDPOINT_PORT
    assert spec.roles == (lt.ROLE_INFERENCE,)
    assert spec.qualification_ref == lt.HALOBOX_QUALIFICATION_REF
    assert spec.qualification_ref.startswith("ps624-qualified:")
    assert len(spec.artifact_paths) == 3
    assert lt.HALOBOX_PROFILE_ID in spec.qualification_ref


def test_the_generic_framework_target_stays_unqualified():
    """No generic Framework capability: the host alone is still NOT routable."""
    generic = lt.target_by_id(lt.TARGET_FRAMEWORK)
    assert generic is not None
    assert generic.qualification_ref == ""
    profile = lt.target_by_id(HALOBOX)
    assert profile.target_id != generic.target_id


def test_the_halobox_endpoint_is_not_the_framework_ollama_service():
    spec = lt.target_by_id(HALOBOX)
    assert "11434" not in spec.endpoint
    assert spec.endpoint.endswith(":8731")   # sealed launch --port 8731


def test_halobox_does_not_inherit_rtx_identity():
    halobox, rtx = lt.target_by_id(HALOBOX), lt.target_by_id(lt.TARGET_RTX_4500)
    assert halobox.ssh_host != rtx.ssh_host
    assert halobox.endpoint != rtx.endpoint
    assert halobox.model != rtx.model
    assert halobox.qualification_ref != rtx.qualification_ref


def test_an_unknown_target_is_never_resolved_by_guessing():
    assert lt.target_by_id("local-framework-anything") is None


# ============================================================== inspector ===
class _StubHandler(BaseHTTPRequestHandler):
    """A stand-in llama-server: /health, /props, /v1/chat/completions."""

    def log_message(self, *args):  # silence
        return

    def _send(self, payload, *, sse=False):
        body = payload.encode()
        self.send_response(200)
        self.send_header("Content-Type",
                         "text/event-stream" if sse else "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            return self._send(json.dumps({"status": "ok"}))
        if self.path == "/v1/models":
            return self._send(json.dumps({"data": [{
                "id": "/models/" + lt.HALOBOX_MODEL_ALIAS,
                "meta": {"n_ctx": 65536, "n_ctx_train": 262144,
                         "size": 93671559680, "n_params": 176943899520,
                         "ftype": "IQ4_XS - 4.25 bpw"}}]}))
        if self.path == "/props":
            return self._send(json.dumps({
                "build_info": "b6835-29e091e",
                "model_path": "/mnt/framework-data/models/x/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf",
                "total_slots": 4,
                "default_generation_settings": {"n_ctx": 65536},
            }))
        return self._send(json.dumps({"error": "not found"}))

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        if request.get("stream"):
            chunks = [
                {"choices": [{"delta": {"content": "1"}, "index": 0}]},
                {"choices": [{"delta": {"content": "2"}, "index": 0}]},
                {"choices": [{"delta": {"content": "3"}, "index": 0}],
                 "usage": {"prompt_tokens": 11, "completion_tokens": 3}},
            ]
            payload = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
            payload += "data: [DONE]\n\n"
            return self._send(payload, sse=True)
        if request.get("tools"):
            return self._send(json.dumps({
                "choices": [{"message": {"role": "assistant", "content": "",
                    "tool_calls": [{"id": "call_0", "type": "function",
                        "function": {"name": "add_numbers",
                                     "arguments": "{\"a\": 17, \"b\": 25}"}}]},
                    "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 4}}))
        return self._send(json.dumps({
            "choices": [{"message": {"role": "assistant", "content": "OK"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 1}}))


@pytest.fixture()
def stub_server():
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()


def test_inspector_selection_follows_the_declared_runtime_kind():
    assert isinstance(lt.inspector_for(lt.target_by_id(HALOBOX)),
                      lt.LlamaServerInspector)
    assert isinstance(lt.inspector_for(lt.target_by_id(lt.TARGET_RTX_4500)),
                      lt.OllamaInspector)


def test_the_llama_inspector_measures_a_served_window_and_proves_a_tool_call(stub_server):
    spec = direct_spec(stub_server)
    inspector = lt.LlamaServerInspector(probe_artifacts=False)
    raw = inspector.inspect(spec)
    assert raw["reachable"] is True
    assert raw["version"] == "b6835-29e091e"
    assert raw["ps"]["models"][0]["context_length"] == 65536
    assert raw["tool_proof"]["ok"] is True
    assert raw["tool_proof"]["tool_calls"] == 1
    assert raw["streaming"] is True
    assert raw["runtime_options"]["parallel"] == 4
    # The runtime's OWN account of itself is preferred over the artifact filename.
    assert raw["model"]["name"] == lt.HALOBOX_MODEL_ALIAS
    assert raw["model"]["details"]["quantization_level"] == "IQ4_XS"
    assert raw["runtime_options"]["served_model_id"].startswith("/models/")
    assert raw["runtime_options"]["runtime_reported_n_ctx_train"] == 262144
    assert "artifact_identity_unmeasured" in raw["failure_classes"]


def test_the_llama_inspector_reports_an_unreachable_node_without_raising():
    spec = direct_spec(1)          # nothing listens there
    raw = lt.LlamaServerInspector(probe_artifacts=False).inspect(spec)
    assert raw["reachable"] is False
    assert "runtime_unreachable" in raw["failure_classes"]


def test_artifact_identity_hashes_every_shard_and_composes_one_digest(monkeypatch):
    stdout = "".join(f"{digest}  {path}\n" for (_, digest), path in
                     zip(SHARDS, [f"/models/{n}" for n, _ in SHARDS]))
    stdout += "--SIZES--\n" + "".join(
        f"{size} /models/{name}\n" for (name, _), size in
        zip(SHARDS, (10946624, 49835229856, 43836407744)))

    class _Proc:
        returncode = 0
        stderr = ""

        def __init__(self):
            self.stdout = stdout

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Proc())
    identity = lt.LlamaServerInspector().artifact_identity(halobox_spec())
    assert identity["ok"] is True
    assert [s["sha256"] for s in identity["shards"]] == [d for _, d in SHARDS]
    assert identity["total_bytes"] == 10946624 + 49835229856 + 43836407744
    assert len(identity["digest"]) == 64
    # The composite digest is a pure function of the shard hashes and their order.
    again = lt.LlamaServerInspector().artifact_identity(halobox_spec())
    assert again["digest"] == identity["digest"]


# =============================================================== receipt ===
def raw_observation(*, digest=None, tool_calls=1, streaming=True,
                    served=65536, version="b6835-29e091e"):
    """A raw observation in the shape the llama-server inspector emits."""
    return {
        "reachable": True, "version": version,
        "api_kind": "openai-compatible:llama-server",
        "model": {"name": lt.HALOBOX_MODEL_ALIAS, "model": lt.HALOBOX_MODEL_ALIAS,
                  "digest": digest if digest is not None else "c" * 64,
                  "size": 93682584224,
                  "details": {"family": "", "quantization_level": "IQ4_XS",
                              # declared_context = the MODEL's native window
                              # (n_ctx_train, 262144); the per-slot served
                              # window (65536) is the ps entry below.
                              "context_length": 262144},
                  "capabilities": ["completion"]},
        "ps": {"models": [{"name": lt.HALOBOX_MODEL_ALIAS,
                            "context_length": served}]},
        "tool_proof": (None if tool_calls is None
                       else {"ok": True, "tool_calls": tool_calls}),
        "streaming": streaming,
        "auxiliary_artifacts": tuple(f"{n}={d}" for n, d in SHARDS),
        "runtime_options": {"api_kind": "openai-compatible:llama-server",
                            "parallel": 4, "build_info": version},
        "failure_classes": [],
    }


def halobox_record(**kwargs):
    raw = raw_observation(**kwargs)
    return lt.build_capability(halobox_spec(), raw, probed_at="2026-09-15T19:00:00+00:00")


def halobox_receipt(**kwargs):
    return lt.receipt_from_capability(
        halobox_record(**kwargs), configured_context=262144,
        # PS-632 reconciliation 2026-09-16: the routing bound is the
        # ENGINE-DEMONSTRATED window (llama-bench depth ladder, throughput
        # only); the deepest sealed SEMANTIC verification is 19760 tokens.
        safe_working_context=32768,
        engine_demonstrated_context=32768, semantic_verified_context=19760,
        safe_context_source=("PS-624 sealed qualification: engine-demonstrated "
                             "at 32768 (llama-bench depths 0/4K/16K/32K, no "
                             "semantic assertion); semantic context-integrity "
                             "verified to 19760 tokens (long-context marker)"),
        backend="vulkan", runtime_repository="halo-box/llama.cpp",
        runtime_commit=lt.HALOBOX_RUNTIME_COMMIT,
        observed_at="2026-09-15T19:00:00+00:00",
        roles=halobox_spec().roles, qualification_ref=lt.HALOBOX_QUALIFICATION_REF)


def test_a_measured_llama_observation_becomes_a_proven_receipt():
    receipt = halobox_receipt()
    assert receipt.host_id == HALOBOX
    assert receipt.runtime.runtime_kind == "llama-server"
    assert receipt.runtime.provider == "llama-server"
    assert receipt.runtime.backend == "vulkan"
    assert receipt.runtime.commit == lt.HALOBOX_RUNTIME_COMMIT
    assert receipt.model.digest == "c" * 64
    assert receipt.model.quantization == "IQ4_XS"
    assert len(receipt.model.auxiliary_artifacts) == 3
    assert receipt.context.configured_context == 262144
    # Per REQUEST: the sealed launch splits 262144 across --parallel 4 slots.
    assert receipt.context.served_context == 65536
    assert receipt.context.safe_working_context == 32768
    assert receipt.context.engine_demonstrated_context == 32768
    assert receipt.context.semantic_verified_context == 19760
    assert receipt.qualification_ref == lt.HALOBOX_QUALIFICATION_REF
    assert receipt.capabilities.tool_semantics == lt.TOOLS_PROVEN
    assert lt.CAP_NATIVE_TOOLS in receipt.capabilities.measured
    assert lt.CAP_STREAMING in receipt.capabilities.measured
    assert "runtime_present" in receipt.capabilities.detected
    assert lt.ROLE_INFERENCE in receipt.roles
    assert receipt.health == lt.HEALTH_HEALTHY


def test_completion_probe_accepts_qwen_reasoning_only_completion(monkeypatch):
    spec = halobox_spec(ssh_host="")
    inspector = lt.LlamaServerInspector(probe_artifacts=False,
                                        probe_tools=False, probe_streaming=False)
    replies = {
        "/health": {"ok": True, "body": {"status": "ok"}, "err": ""},
        "/props": {"ok": True, "body": {
            "build_info": "b1-29e091e", "model_path": "/m.gguf",
            "default_generation_settings": {"n_ctx": 65536},
            "total_slots": 4}, "err": ""},
        "/v1/models": {"ok": True, "body": {"data": [{"id": "/m.gguf",
            "meta": {"n_ctx": 65536, "n_ctx_train": 262144,
                     "ftype": "IQ4_XS - 4.25 bpw"}}]}, "err": ""},
        "/v1/chat/completions": {"ok": True, "body": {"choices": [{
            "message": {"role": "assistant", "content": "",
                         "reasoning_content": "OK"}}]}, "err": ""},
    }
    monkeypatch.setattr(inspector, "api", lambda _spec, path, body=None: replies[path])
    raw = inspector.inspect(spec)
    assert raw["runtime_options"]["completion_served"] is True


def test_the_profile_id_carries_the_runtime_backend_and_artifact_identity():
    profile = halobox_receipt().profile_id
    assert "llama-server" in profile
    assert "vulkan" in profile
    assert "IQ4_XS" in profile
    assert "ctx32768" in profile


def test_a_declared_only_tool_claim_is_never_counted_as_measured():
    receipt = halobox_receipt(tool_calls=None)
    assert receipt.capabilities.tool_semantics == lt.TOOLS_UNPROVEN
    assert lt.CAP_NATIVE_TOOLS not in receipt.capabilities.measured


@pytest.mark.parametrize("change", [
    {"digest": "d" * 64},
    {"version": "b6900-other"},
])
def test_material_drift_produces_a_different_profile_and_identity(change):
    base = halobox_receipt()
    moved = halobox_receipt(**change)
    assert moved.profile_id != base.profile_id
    assert moved.identity_digest() != base.identity_digest()


def test_a_changed_runtime_commit_breaks_identity_even_when_the_version_matches():
    base = halobox_receipt()
    other = lt.receipt_from_capability(
        halobox_record(), configured_context=262144, safe_working_context=32768,
        safe_context_source="x", backend="vulkan", runtime_repository="halo-box/llama.cpp",
        runtime_commit="0" * 40, observed_at="2026-09-15T19:00:00+00:00",
        roles=halobox_spec().roles, qualification_ref=lt.HALOBOX_QUALIFICATION_REF)
    assert other.profile_id == base.profile_id          # version text is unchanged
    assert other.identity_digest() != base.identity_digest()


def test_a_changed_backend_changes_both_profile_and_identity():
    base = halobox_receipt()
    rocm = lt.receipt_from_capability(
        halobox_record(), configured_context=262144, safe_working_context=32768,
        safe_context_source="x", backend="hip", runtime_repository="halo-box/llama.cpp",
        runtime_commit=lt.HALOBOX_RUNTIME_COMMIT,
        observed_at="2026-09-15T19:00:00+00:00", roles=halobox_spec().roles,
        qualification_ref=lt.HALOBOX_QUALIFICATION_REF)
    assert rocm.profile_id != base.profile_id
    assert rocm.identity_digest() != base.identity_digest()


# =============================================================== routing ===
def store_with(records, facts, tmp_path):
    store = TargetCapabilityStore(str(tmp_path / "target_capabilities"))
    sync_receipts_from_records(store, records, profiles_by_host=facts,
                               ttl_s=7 * 24 * 3600, health_ttl_s=300)
    return store


def store_receipt(receipt, tmp_path):
    """Persist an already-built receipt (the store's own writer, not the probe)."""
    store = TargetCapabilityStore(str(tmp_path / "target_capabilities"))
    store.append(receipt)
    return store


HALOBOX_FACTS = {HALOBOX: {
    "configured_context": 262144, "safe_working_context": 32768,
    "safe_context_source": "PS-624 sealed qualification", "backend": "vulkan",
    "runtime_repository": "halo-box/llama.cpp",
    "runtime_commit": lt.HALOBOX_RUNTIME_COMMIT, "ttl_s": 7 * 24 * 3600}}


#: Liveness is a 300-second fact, so a routing assertion must pin its clock beside
#: the observation instead of relying on how long the test suite took to get here.
OBSERVED = "2026-09-15T19:00:00+00:00"
AFTER_OBSERVED = datetime(2026, 9, 15, 19, 0, 30, tzinfo=timezone.utc)

def test_a_persisted_halobox_receipt_becomes_a_routable_profile(tmp_path):
    store = store_with([halobox_record()], HALOBOX_FACTS, tmp_path)
    inputs = persisted_routing_inputs(store, now=AFTER_OBSERVED)
    mine = [p for p in inputs.profiles if p.target_id == HALOBOX]
    assert len(mine) == 1
    profile = mine[0]
    assert profile.runtime_kind == "llama-server"
    assert profile.endpoint_url == lt.HALOBOX_ENDPOINT_PORT
    assert profile.inference is True
    assert "write_file" in profile.tools
    assert profile.model == lt.HALOBOX_MODEL_ALIAS
    assert inputs.bound_receipts[HALOBOX] == store.current_for_host(HALOBOX).receipt_hash


def test_the_generic_framework_host_is_still_skipped_as_unqualified(tmp_path):
    """A receipt on the HOST cannot create a generic Framework capability."""
    generic_spec = lt.target_by_id(lt.TARGET_FRAMEWORK)
    generic_record = lt.build_capability(
        generic_spec, {**raw_observation(), "model": {
            "name": "qwen3.8:27b", "digest": "e" * 64, "size": 1,
            "details": {"quantization_level": "Q4_K_XL"},
            "capabilities": []}}, probed_at="2026-09-15T19:00:00+00:00")
    store = store_with([generic_record],
                       {lt.TARGET_FRAMEWORK: {"configured_context": 262144,
                                              "safe_working_context": 32768,
                                              "backend": "rocm"}}, tmp_path)
    inputs = persisted_routing_inputs(store)
    assert [p for p in inputs.profiles if p.target_id == lt.TARGET_FRAMEWORK] == []
    reasons = {s["target_id"]: s["reason"] for s in inputs.skipped}
    assert "unqualified" in reasons[lt.TARGET_FRAMEWORK]


def test_a_missing_receipt_is_a_typed_skip_not_a_candidate(tmp_path):
    store = TargetCapabilityStore(str(tmp_path / "empty"))
    inputs = persisted_routing_inputs(store)
    assert inputs.profiles == ()
    reasons = {s["target_id"]: s["reason"] for s in inputs.skipped}
    assert "no persisted capability receipt" in reasons[HALOBOX]


def test_a_stale_halobox_receipt_is_skipped_rather_than_dispatched(tmp_path):
    old = lt.receipt_from_capability(
        halobox_record(), configured_context=262144, safe_working_context=32768,
        safe_context_source="x", backend="vulkan", runtime_repository="halo-box/llama.cpp",
        runtime_commit=lt.HALOBOX_RUNTIME_COMMIT,
        observed_at="2026-09-01T00:00:00+00:00", roles=halobox_spec().roles,
        qualification_ref=lt.HALOBOX_QUALIFICATION_REF, ttl_s=3600, health_ttl_s=300)
    store = store_receipt(old, tmp_path)
    inputs = persisted_routing_inputs(
        store, now=datetime(2026, 9, 15, 23, 0, tzinfo=timezone.utc))
    assert inputs.profiles == ()
    reasons = {s["target_id"]: s["reason"] for s in inputs.skipped}
    assert "not qualified" in reasons[HALOBOX]


def test_a_runtime_serving_the_wrong_per_slot_window_is_refused(tmp_path):
    """The one drift that changes no digest: 'served' is a live fact, not identity.

    The sealed launch fixes -c 262144 across 4 slots, so a request window of
    65536. A server serving 262144 per request (one slot, or a different -c) is a
    different execution profile, and no material-identity digest catches it, so it
    is refused explicitly.
    """
    smaller = lt.receipt_from_capability(
        halobox_record(served=262144), configured_context=262144,
        safe_working_context=32768, safe_context_source="x", backend="vulkan",
        runtime_repository="halo-box/llama.cpp",
        runtime_commit=lt.HALOBOX_RUNTIME_COMMIT, observed_at=OBSERVED,
        roles=halobox_spec().roles, qualification_ref=lt.HALOBOX_QUALIFICATION_REF)
    store = store_receipt(smaller, tmp_path)
    inputs = persisted_routing_inputs(store, now=AFTER_OBSERVED)
    assert [p for p in inputs.profiles if p.target_id == HALOBOX] == []
    reasons = {s["target_id"]: s["reason"] for s in inputs.skipped}
    assert "sealed launch implies" in reasons[HALOBOX]


def test_a_matching_window_is_still_routable(tmp_path):
    """The rule must not become a blanket ban: 262144 served == configured passes."""
    store = store_with([halobox_record()], HALOBOX_FACTS, tmp_path)
    inputs = persisted_routing_inputs(store, now=AFTER_OBSERVED)
    assert len([p for p in inputs.profiles if p.target_id == HALOBOX]) == 1


# ================================================================ client ===
def _stream(chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def stub_client(monkeypatch, *, sse="", served=65536, ssh_host="framework"):
    spec = halobox_spec(ssh_host=ssh_host)
    target = llama_server_client.LlamaServerTarget(
        spec.target_id, spec.ssh_host, spec.model, spec.endpoint)

    def fake_curl(path, payload, timeout, stream=False):
        if path == "/props":
            return True, json.dumps({
                "build_info": "b6835-29e091e", "model_path": "/m/x.gguf",
                "total_slots": 4,
                "default_generation_settings": {"n_ctx": served}}), ""
        return True, sse, ""

    monkeypatch.setattr(target, "_curl", fake_curl)
    return target


def test_openai_streaming_is_normalised_into_the_ollama_shaped_body(monkeypatch):
    client = stub_client(monkeypatch, sse=_stream([
        {"choices": [{"delta": {"content": "1"}}]},
        {"choices": [{"delta": {"content": "2"}}]},
        {"choices": [{"delta": {"content": "3"},
                     "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 11, "completion_tokens": 3}},
    ]))
    res = client.api_streaming_chat([{"role": "user", "content": "count"}], num_ctx=262144)
    assert res.ok is True
    assert res.body["message"]["content"] == "123"
    assert res.body["prompt_eval_count"] == 11
    assert res.body["eval_count"] == 3
    assert res.body["_incremental"] is True
    assert res.body["_chunks"] == 3
    assert res.ttft_s is not None
    assert res.body["_runtime"] == "llama-server"


def test_a_streamed_tool_call_is_accumulated_across_chunks(monkeypatch):
    client = stub_client(monkeypatch, sse=_stream([
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_0",
            "function": {"name": "add_", "arguments": "{\"a\": 1"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0,
            "function": {"name": "numbers", "arguments": "7, \"b\": 25}"}}]}}]},
    ]))
    res = client.api_streaming_chat([{"role": "user", "content": "add"}],
                                    tools=[{"type": "function"}], num_ctx=262144)
    calls = res.body["message"]["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "add_numbers"
    assert calls[0]["function"]["arguments"] == {"a": 17, "b": 25}


def test_a_request_larger_than_the_served_window_is_a_runtime_error(monkeypatch):
    client = stub_client(monkeypatch, sse=_stream([
        {"choices": [{"delta": {"content": "ok"}}]}]))
    res = client.api_streaming_chat([{"role": "user", "content": "x"}], num_ctx=524288)
    assert res.ok is True
    assert "exceeds the served context" in res.runtime_error
    assert res.body["_num_ctx_requested"] == 524288
    assert res.body["_num_ctx_served"] == 65536


def test_a_transport_failure_is_reported_as_a_failure_not_a_verdict(monkeypatch):
    client = stub_client(monkeypatch)

    def broken(path, payload, timeout, stream=False):
        return False, "", "ssh/curl timed out"

    monkeypatch.setattr(client, "_curl", broken)
    res = client.api_streaming_chat([{"role": "user", "content": "x"}])
    assert res.ok is False
    assert "timed out" in res.error


def test_the_client_cannot_choose_its_own_endpoint(monkeypatch):
    spec = halobox_spec()
    target = llama_server_client.LlamaServerTarget(
        spec.target_id, spec.ssh_host, spec.model, spec.endpoint)
    assert target.endpoint == lt.HALOBOX_ENDPOINT_PORT
    assert not hasattr(target, "set_endpoint")
    assert not hasattr(target, "switch_runtime")
    # The endpoint follows the SPEC, so a profile cannot be served by another port.
    other = lt.LocalTargetSpec(target_id=HALOBOX, label="x", ssh_host="framework",
                               endpoint="http://127.0.0.1:9001",
                               model=lt.HALOBOX_MODEL_ALIAS,
                               runtime_kind=lt.RUNTIME_LLAMA_SERVER)
    moved = llama_server_client.LlamaServerTarget(
        other.target_id, other.ssh_host, other.model, other.endpoint)
    assert moved.endpoint == "http://127.0.0.1:9001"
    assert moved.endpoint != target.endpoint


def test_the_client_factory_never_crosses_runtimes():
    halobox = runtime_client.client_for_target(HALOBOX)
    assert isinstance(halobox, llama_server_client.LlamaServerTarget)
    rtx = runtime_client.client_for_target(lt.TARGET_RTX_4500)
    assert isinstance(rtx, ollama_client.Target)
    assert not isinstance(rtx, llama_server_client.LlamaServerTarget)
    # The generic Framework host is REGISTERED but deliberately UNQUALIFIED, so no
    # invocation path may be constructed for it at all.
    with pytest.raises(SystemExit):
        runtime_client.client_for_target(lt.TARGET_FRAMEWORK)
    with pytest.raises(KeyError):
        runtime_client.client_for_target("local-nope")
    weird = lt.LocalTargetSpec(target_id="local-x", label="x", ssh_host="h",
                               endpoint="http://127.0.0.1:9", model="m",
                               runtime_kind="vllm")
    with pytest.raises(SystemExit):
        runtime_client.client_for_target("local-x", spec=weird)


def test_the_ollama_client_entry_point_now_consults_the_registry():
    assert isinstance(ollama_client.target(HALOBOX),
                      llama_server_client.LlamaServerTarget)
    assert isinstance(ollama_client.target(lt.TARGET_RTX_4500), ollama_client.Target)


def test_the_halobox_endpoint_is_loopback_so_a_local_pin_can_hold():
    from src.routing_engine import endpoint_is_local

    assert endpoint_is_local(lt.target_by_id(HALOBOX).endpoint) is True
    assert endpoint_is_local("https://api.openai.com/v1") is False


# ========================================================= endpoint pinning ===
def test_the_dispatch_pin_binds_the_exact_endpoint_not_just_its_locality():
    """A locality-only pin cannot tell two local endpoints apart.

    Found by the live adapter controls: `http://127.0.0.1:11434` (the host's Ollama
    service) and `http://127.0.0.1:9999` were both ACCEPTED as the pinned endpoint,
    because both are "local". Two loopback ports are not the same target, so the pin
    now carries the endpoint and verify_invocation refuses any other one -- before a
    request, with zero model calls.
    """
    from src import dispatch_boundary as dbd
    from src.dispatch_boundary import (DispatchPinViolation, PIN_ENDPOINT_MISMATCH,
                                       PIN_LOCALITY_MISMATCH, PIN_PROFILE_MISMATCH)

    assert PIN_ENDPOINT_MISMATCH == "pin_endpoint_mismatch"
    # The pin payload carries the endpoint, so it is part of the pinned identity.
    import inspect
    source = inspect.getsource(dbd.BoundDispatch.pin_for)
    assert "\"endpoint\"" in source
    verify_source = inspect.getsource(dbd.verify_invocation)
    assert "PIN_ENDPOINT_MISMATCH" in verify_source
    # Locality is the coarser signal and is reported FIRST; the exact-endpoint check
    # follows it, so a hosted URL still fails as a locality violation and only a
    # same-locality-but-different-endpoint fails as an endpoint violation.
    assert verify_source.index("resolved_local") < verify_source.index(
        "PIN_ENDPOINT_MISMATCH")
