#!/usr/bin/env python3
"""Sprint 7 harness: a thin ollama client that speaks to a REMOTE loopback node.

Why this exists instead of reusing the shipped inspector: the shipped
`OllamaInspector` deliberately probes cheaply (version/tags/ps + one tool
question). Slices A and B need things it refuses to do — a real tool-call LOOP,
streaming TTFT, per-request `num_ctx`, and forced fixed-length generation —
while producing the same identity fields so evidence lines up.

Transport: both reachable targets bind ollama to 127.0.0.1, so every call is
`ssh <host> curl http://127.0.0.1:11434/...`. Nothing here writes to a remote
host: `num_ctx` is a per-REQUEST option, which is how a window gets calibrated
without editing a unit file or restarting anyone's daemon.
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class CallResult:
    ok: bool
    elapsed_s: float
    body: dict = field(default_factory=dict)
    error: str = ""
    ttft_s: Optional[float] = None
    #: An error the RUNTIME reported (e.g. context exhausted). Kept separate
    #: from `error`, which means the TRANSPORT failed. Conflating the two turns a
    #: sizing fact into a phantom outage, and an outage into a phantom model
    #: verdict — the exact misattribution this stack keeps having to undo.
    runtime_error: str = ""


class Target:
    """One local target, addressed exactly as the registry addresses it."""

    def __init__(self, target_id: str, ssh_host: str, model: str):
        self.target_id = target_id
        self.ssh_host = ssh_host
        self.model = model

    # ------------------------------------------------------------ transport ---
    def _curl(self, path: str, payload: Optional[dict], timeout: int) -> tuple[bool, str, str]:
        url = f"http://127.0.0.1:11434{path}"
        if payload is None:
            remote = f"curl -sS --max-time {timeout} '{url}'"
        else:
            remote = (
                f"curl -sS -N --max-time {timeout} '{url}' "
                f"-H 'Content-Type: application/json' -d @-"
            )
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
    def api(self, path: str, payload: Optional[dict] = None, timeout: int = 900) -> CallResult:
        started = time.monotonic()
        ok, out, err = self._curl(path, payload, timeout)
        elapsed = round(time.monotonic() - started, 3)
        if not ok:
            return CallResult(False, elapsed, error=err or out[:200])
        try:
            parsed = json.loads(out or "{}")
        except json.JSONDecodeError:
            return CallResult(False, elapsed, error=f"non-JSON: {out[:150]}")
        runtime_err = parsed.get("error") if isinstance(parsed.get("error"), str) else ""
        return CallResult(True, elapsed, parsed, runtime_error=runtime_err or "")


    # --------------------------------------------------------------- stream ---
    def api_streaming_chat(self, messages: List[dict], *, num_ctx: int = 0,
                           tools: Optional[List[dict]] = None, timeout: int = 900,
                           num_predict: int = 128, think: bool = False) -> CallResult:
        """Chat with streaming, so TTFT is a measurement and not an inference.

        TTFT is time-to-first-content-or-tool-token as observed by the client.
        The ssh hop adds a small constant; it is the same constant for every
        window tested, so comparisons across window sizes stay valid.
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "think": think,
            "keep_alive": "10m",
            "options": {"temperature": 0, "num_predict": num_predict},
        }
        if tools:
            payload["tools"] = tools
        if num_ctx:
            payload["options"]["num_ctx"] = num_ctx

        url = "http://127.0.0.1:11434/api/chat"
        remote = (
            f"curl -sS -N --max-time {timeout} '{url}' "
            f"-H 'Content-Type: application/json' -d @-"
        )
        started = time.monotonic()
        ttft: Optional[float] = None
        final: dict = {}
        chunks = 0
        err = ""
        runtime_err = ""
        # Streaming tool calls are NOT always repeated in the final chunk.
        # Measured 2026-09-14: ollama 0.33.3 (msr1) emits the tool call in the
        # FIRST chunk (`done:false`) and a final `done:true` chunk whose message
        # is `{"role":"assistant","content":""}` with no tool_calls at all.
        # Reading only the final message therefore DROPS a perfectly valid tool
        # call and reports "the worker did not call the tool" — a false negative
        # about the model caused entirely by the reader. So: accumulate.
        acc_content: List[str] = []
        acc_tool_calls: List[dict] = []
        content_chunks = 0
        try:
            proc = subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 self.ssh_host, remote],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, bufsize=1,
            )
            proc.stdin.write(json.dumps(payload))
            proc.stdin.close()
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                chunks += 1
                if isinstance(obj.get("error"), str) and obj["error"]:
                    runtime_err = obj["error"]
                msg = obj.get("message") or {}
                piece = msg.get("content") or ""
                if piece:
                    acc_content.append(piece)
                    content_chunks += 1
                if msg.get("tool_calls"):
                    acc_tool_calls.extend(msg["tool_calls"])
                if ttft is None and (piece.strip() or msg.get("tool_calls")):
                    ttft = round(time.monotonic() - started, 3)
                final = obj
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
            stderr = (proc.stderr.read() or "").strip()[:200]
            if proc.returncode not in (0, None):
                err = stderr or f"exit {proc.returncode}"
        except OSError as exc:
            err = str(exc)[:200]
        elapsed = round(time.monotonic() - started, 3)
        if err and not final:
            return CallResult(False, elapsed, error=err, ttft_s=ttft)
        # Rebuild the message the way a streaming client is SUPPOSED to: the
        # union of every chunk, not the contents of the last one.
        merged = dict(final)
        message = dict(merged.get("message") or {})
        message["content"] = "".join(acc_content)
        if acc_tool_calls:
            message["tool_calls"] = acc_tool_calls
        merged["message"] = message
        if acc_content:
            merged["response"] = "".join(acc_content)
        out = CallResult(True, elapsed, merged, ttft_s=ttft,
                         runtime_error=runtime_err)
        out.body["_chunks"] = chunks
        out.body["_content_chunks"] = content_chunks
        # A stream that delivered everything in ONE chunk is not incremental.
        # On msr1 that is the normal shape, which means TTFT measured there is
        # the WHOLE turn, not a first-token latency. Recording the flag keeps the
        # two numbers from being compared as if they meant the same thing.
        out.body["_incremental"] = content_chunks > 1
        return out

    # ---------------------------------------------------------------- helpers --
    def ps(self) -> dict:
        r = self.api("/api/ps", timeout=20)
        return r.body if r.ok else {}

    def runtime_state(self) -> dict:
        """Capture one cheap pre-request state snapshot for benchmark forensics.

        This is deliberately separate from the model request and is not used for
        routing, retries, or acceptance.  A single SSH command records Ollama's
        loaded-model state, host load, and best-effort NVIDIA utilization.  The
        probe duration is returned so it can be excluded from API timing.
        """
        started = time.monotonic()
        remote = (
            "printf 'LOADAVG '; cat /proc/loadavg; "
            "printf 'OLLAMA_PS '; curl -sS --max-time 10 "
            "'http://127.0.0.1:11434/api/ps'; "
            "printf '\\nGPU '; "
            "(nvidia-smi --query-gpu=name,utilization.gpu,memory.used,memory.total "
            "--format=csv,noheader,nounits 2>/dev/null || true)"
        )
        try:
            proc = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                 self.ssh_host, remote],
                capture_output=True, text=True, timeout=25,
            )
            output = proc.stdout or ""
            return {
                "ok": proc.returncode == 0,
                "elapsed_s": round(time.monotonic() - started, 3),
                "returncode": proc.returncode,
                "raw": output[:4000],
                "error": (proc.stderr or "").strip()[:300],
            }
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "ok": False,
                "elapsed_s": round(time.monotonic() - started, 3),
                "returncode": None,
                "raw": "",
                "error": str(exc)[:300],
            }

    def served_context(self) -> Optional[int]:
        for m in (self.ps().get("models") or []):
            if m.get("name") == self.model:
                return m.get("context_length")
        return None

    def unload(self) -> None:
        self.api("/api/generate", {"model": self.model, "prompt": "x",
                                   "stream": False, "keep_alive": "0s",
                                   "options": {"num_predict": 1}}, timeout=180)


TARGETS = {
    "local-rtx4500": ("minipc", "qwen3.8:27b"),
    "local-msr1": ("msr1", "qwen3.8:27b"),
}


def target(target_id: str) -> Target:
    host, model = TARGETS[target_id]
    return Target(target_id, host, model)
