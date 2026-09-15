#!/usr/bin/env python3
"""llama.cpp / HaloBox runtime client, behind the SAME surface as the ollama one.

Why a second client and not a flag on the first: ollama and llama-server differ in
endpoint paths and in wire shape, and folding both into one class would put two
protocols behind one set of branches. What must NOT differ is the interface the
worker loop sees - ``api_streaming_chat`` returning the same body the loop already
reads - so the model's behaviour is not accidentally changed by its runtime.

The OpenAI surface is normalised INTO the ollama-shaped body:

    choices[0].delta.content            -> message.content
    choices[0].delta.tool_calls[...]    -> message.tool_calls[...]
    usage.prompt_tokens / completion_tokens -> prompt_eval_count / eval_count

This client reports facts. It cannot choose an endpoint: the endpoint arrives from
the registry spec and there is no setter, so an adapter can never reroute itself to
Ollama, Halogen, RTX or anything else.
"""
from __future__ import annotations

import json
import subprocess
import time
from typing import List, Optional

from ollama_client import CallResult


class LlamaServerTarget:
    """One llama-server target, addressed exactly as the registry addresses it."""

    def __init__(self, target_id: str, ssh_host: str, model: str, endpoint: str):
        self.target_id = target_id
        self.ssh_host = ssh_host
        self.model = model
        #: Immutable: set from the registry spec at construction, never mutated.
        self.endpoint = endpoint.rstrip("/")

    # ------------------------------------------------------------ transport ---
    def _curl(self, path: str, payload: Optional[dict], timeout: int,
              stream: bool = False) -> tuple:
        url = f"{self.endpoint}{path}"
        if not self.ssh_host:
            # Directly reachable endpoint (no ssh hop). Same call, same contract;
            # this is what makes the adapter testable without a remote host.
            import urllib.request

            req = urllib.request.Request(
                url, data=json.dumps(payload).encode() if payload is not None else None,
                headers={"Content-Type": "application/json"} if payload is not None else {})
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return True, resp.read().decode(), ""
            except Exception as exc:  # noqa: BLE001
                return False, "", str(exc)[:200]
        flag = "-N " if stream else ""
        if payload is None:
            remote = f"curl -sS --max-time {timeout} '{url}'"
        else:
            remote = (f"curl -sS {flag}--max-time {timeout} '{url}' "
                      f"-H 'Content-Type: application/json' -d @-")
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 self.ssh_host, remote],
                input=json.dumps(payload) if payload is not None else None,
                capture_output=True, text=True, timeout=timeout + 30,
            )
        except subprocess.TimeoutExpired:
            return False, "", f"ssh/curl timed out after {timeout}s"
        except OSError as exc:
            return False, "", str(exc)[:200]
        if proc.returncode != 0:
            return False, proc.stdout, (proc.stderr or "").strip()[:200]
        return True, proc.stdout, ""

    # ------------------------------------------------------------- non-stream --
    def api(self, path: str, payload: Optional[dict] = None,
            timeout: int = 900) -> CallResult:
        started = time.monotonic()
        ok, out, err = self._curl(path, payload, timeout)
        elapsed = round(time.monotonic() - started, 3)
        if not ok:
            return CallResult(False, elapsed, error=err or out[:200])
        try:
            parsed = json.loads(out or "{}")
        except json.JSONDecodeError:
            return CallResult(False, elapsed, error=f"non-JSON: {out[:150]}")
        runtime_err = ""
        error = parsed.get("error")
        if isinstance(error, dict):
            runtime_err = str(error.get("message") or error)[:300]
        elif isinstance(error, str):
            runtime_err = error[:300]
        return CallResult(True, elapsed, parsed, runtime_error=runtime_err)

    def props(self) -> dict:
        r = self.api("/props", timeout=20)
        return r.body if r.ok else {}

    def served_context(self) -> Optional[int]:
        body = self.props()
        settings = body.get("default_generation_settings") or {}
        for candidate in (settings.get("n_ctx"), body.get("n_ctx")):
            if isinstance(candidate, int) and candidate > 0:
                return candidate
        return None

    def ps(self) -> dict:
        """Ollama-shaped residency view, derived from what the server reports."""
        body = self.props()
        ctx = self.served_context()
        if not ctx:
            return {}
        return {"models": [{"name": self.model, "model": self.model,
                            "context_length": ctx}]}

    # --------------------------------------------------------------- stream ---
    def api_streaming_chat(self, messages: List[dict], *, num_ctx: int = 0,
                           tools: Optional[List[dict]] = None, timeout: int = 900,
                           num_predict: int = 128, think: bool = False) -> CallResult:
        """OpenAI SSE in, ollama-shaped body out, with TTFT measured.

        ``num_ctx`` is a REQUEST-scoped option on ollama and a SERVER-scoped one
        here, so it is reported rather than silently ignored: if the caller asks for
        a window larger than the server is serving, that is a sizing fact and it is
        surfaced as a runtime error instead of being papered over.
        """
        served = self.served_context()
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0,
            "max_tokens": num_predict,
        }
        if tools:
            payload["tools"] = tools

        started = time.monotonic()
        ttft: Optional[float] = None
        chunks = 0
        content_chunks = 0
        acc_content: List[str] = []
        acc_tool_calls: dict = {}
        usage: dict = {}
        finish = ""
        runtime_err = ""
        err = ""
        budget = timeout
        ok, out, err = self._curl("/v1/chat/completions", payload, budget, stream=True)
        if num_ctx and served and int(num_ctx) > int(served):
            runtime_err = (f"requested context {num_ctx} exceeds the served "
                           f"context {served} on this runtime")
        if not ok:
            return CallResult(False, round(time.monotonic() - started, 3),
                              error=err or out[:200], ttft_s=ttft)
        for line in (out or "").splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]" or not data:
                continue
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            chunks += 1
            if isinstance(obj.get("error"), dict):
                runtime_err = str(obj["error"].get("message") or obj["error"])[:300]
            if obj.get("usage"):
                usage = dict(obj["usage"])
            for choice in (obj.get("choices") or []):
                delta = choice.get("delta") or {}
                piece = delta.get("content") or ""
                if piece:
                    acc_content.append(piece)
                    content_chunks += 1
                    if ttft is None:
                        ttft = round(time.monotonic() - started, 3)
                for call in (delta.get("tool_calls") or []):
                    index = int(call.get("index") or 0)
                    slot = acc_tool_calls.setdefault(index, {
                        "id": call.get("id") or f"call_{index}",
                        "type": "function", "function": {"name": "", "arguments": ""}})
                    if call.get("id"):
                        slot["id"] = call["id"]
                    fn = call.get("function") or {}
                    if fn.get("name"):
                        slot["function"]["name"] = (slot["function"]["name"]
                                                    + str(fn["name"]))
                    if fn.get("arguments"):
                        slot["function"]["arguments"] += str(fn["arguments"])
                    if ttft is None:
                        ttft = round(time.monotonic() - started, 3)
                if choice.get("finish_reason"):
                    finish = str(choice["finish_reason"])
        elapsed = round(time.monotonic() - started, 3)

        tool_calls: List[dict] = []
        for index in sorted(acc_tool_calls):
            slot = acc_tool_calls[index]
            raw_args = slot["function"].get("arguments") or ""
            try:
                slot["function"]["arguments"] = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                slot["function"]["arguments"] = raw_args
            tool_calls.append(slot)
        message: dict = {"role": "assistant", "content": "".join(acc_content)}
        if tool_calls:
            message["tool_calls"] = tool_calls
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        body = {
            "model": self.model,
            "message": message,
            "response": message["content"],
            "done": True,
            "done_reason": finish or "stop",
            "finish_reason": finish or "stop",
            "prompt_eval_count": prompt_tokens,
            "eval_count": completion_tokens,
            "_chunks": chunks,
            "_content_chunks": content_chunks,
            "_incremental": content_chunks > 1,
            "_runtime": "llama-server",
            "_endpoint": self.endpoint,
            "_num_ctx_requested": int(num_ctx or 0),
            "_num_ctx_served": int(served or 0),
            "_usage": dict(usage),
        }
        return CallResult(True, elapsed, body, ttft_s=ttft,
                          runtime_error=runtime_err)

    # ---------------------------------------------------------------- helpers --
    def runtime_state(self) -> dict:
        """Cheap pre-request state: host load, GTT, served window. Not routing."""
        started = time.monotonic()
        remote = (
            "printf 'LOADAVG '; cat /proc/loadavg; "
            "printf 'GTT '; "
            "(cat /sys/class/drm/card*/device/mem_info_gtt_used 2>/dev/null || "
            "rocm-smi --showmeminfo gtt 2>/dev/null | tr '\\n' ' ' || true); "
            "printf '\\nPROPS '; "
            f"curl -sS --max-time 8 '{self.endpoint}/props'"
        )
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 self.ssh_host, remote],
                capture_output=True, text=True, timeout=25,
            )
            return {"ok": proc.returncode == 0,
                    "elapsed_s": round(time.monotonic() - started, 3),
                    "returncode": proc.returncode,
                    "raw": (proc.stdout or "")[:4000],
                    "error": (proc.stderr or "").strip()[:300]}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "elapsed_s": round(time.monotonic() - started, 3),
                    "returncode": None, "raw": "", "error": str(exc)[:300]}
