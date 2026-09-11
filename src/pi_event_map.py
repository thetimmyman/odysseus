"""src/pi_event_map.py — map Pi runtime events into Odysseus execution events.

Pi's RPC mode (verified against the installed ``@earendil-works/pi-coding-agent``
0.74.2 protocol) streams JSON events on stdout:

    agent_start, agent_end, turn_start, turn_end, message_start, message_update,
    message_end, tool_execution_start, tool_execution_update, tool_execution_end,
    queue_update, compaction_start, compaction_end, auto_retry_start,
    auto_retry_end, extension_error

This module is a pure translation layer — no I/O, no policy. It exists so the
operator can see *what the Pi agent is doing* in Odysseus terms without Odysseus
reaching into Pi's inner loop (Pi owns reasoning rounds, tool lifecycle and
compaction; see the architectural boundary).

Mapping is intentionally not one-to-one: an ``agent_end`` may carry several
messages, and a ``message_end`` may carry both prose and tool calls.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# --- Odysseus-side event vocabulary ----------------------------------------
EXECUTION_STARTED = "execution_started"
MODEL_TURN = "model_turn"
MODEL_TURN_END = "model_turn_end"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"
FILE_MODIFICATION = "file_modification"
SHELL_COMMAND = "shell_command"
TEST_EXECUTION = "test_execution"
WARNING = "warning"
CONTEXT_EVENT = "context_event"
FAILURE = "failure"
COMPLETION = "completion"
MESSAGE_DELTA = "message_delta"

#: Destructive/notification-free classification of Pi's built-in tools.
_WRITE_TOOLS = frozenset({"write", "edit"})
_READ_TOOLS = frozenset({"read", "grep", "find", "ls"})

#: Commands whose execution means "the agent ran the test suite / a check".
_TEST_CMD_RE = re.compile(
    r"(^|[\s;&|])(pytest|python3?\s+-m\s+pytest|py\.test|npm\s+(run\s+)?test|"
    r"yarn\s+test|pnpm\s+test|jest|vitest|go\s+test|cargo\s+test|make\s+test|"
    r"ruff\s+check|eslint|tsc\s+--noEmit|node\s+--check|py_compile)",
    re.IGNORECASE,
)


def is_test_command(command: str) -> bool:
    """True when a shell command looks like a test/verification run."""
    return bool(_TEST_CMD_RE.search(command or ""))


def _tool_args(event: Dict[str, Any]) -> Dict[str, Any]:
    args = event.get("args")
    return args if isinstance(args, dict) else {}


def _tool_path(event: Dict[str, Any]) -> Optional[str]:
    args = _tool_args(event)
    for key in ("path", "file_path", "filePath", "file"):
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def classify_tool_event(raw: Dict[str, Any]) -> str:
    """Odysseus event type for a Pi tool event.

    ``bash`` is split into shell/test execution; ``write``/``edit`` are file
    modifications; read-only tools stay generic tool calls.
    """
    name = (raw.get("toolName") or "").strip().lower()
    if name in ("bash", "shell"):
        command = _tool_args(raw).get("command") or ""
        return TEST_EXECUTION if is_test_command(command) else SHELL_COMMAND
    if name in _WRITE_TOOLS:
        return FILE_MODIFICATION
    return TOOL_CALL


def _tool_summary(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Compact, log-safe view of a tool event (no secrets, bounded size)."""
    out: Dict[str, Any] = {"tool": raw.get("toolName")}
    args = _tool_args(raw)
    command = args.get("command")
    if isinstance(command, str):
        out["command"] = command[:1000]
    path = _tool_path(raw)
    if path:
        out["path"] = path
    return out


def _message_text(message: Dict[str, Any]) -> Dict[str, str]:
    """Split a Pi message's content into answer text and reasoning text.

    Mirrors ``src/stream_events.py``'s contract: reasoning never masquerades as
    the agent's answer.
    """
    text_parts: List[str] = []
    thinking_parts: List[str] = []
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif kind == "thinking" and isinstance(block.get("thinking"), str):
                thinking_parts.append(block["thinking"])
    elif isinstance(content, str):
        text_parts.append(content)
    return {"text": "".join(text_parts), "thinking": "".join(thinking_parts)}
def map_pi_event(event: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Translate one raw Pi event into zero or more Odysseus execution events."""
    if not isinstance(event, dict):
        return []
    kind = event.get("type")
    ts = event.get("timestamp")

    if kind == "agent_start":
        return [{"type": EXECUTION_STARTED, "ts": ts}]

    if kind == "turn_start":
        return [{"type": MODEL_TURN, "ts": ts}]

    if kind == "turn_end":
        out = [{"type": MODEL_TURN_END, "ts": ts}]
        message = event.get("message")
        if isinstance(message, dict):
            parts = _message_text(message)
            if parts["text"]:
                out.append({"type": COMPLETION, "phase": "turn", "text": parts["text"][:20000], "ts": ts})
        return out

    if kind == "message_end":
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return []
        parts = _message_text(message)
        if parts["text"]:
            return [{"type": COMPLETION, "phase": "message", "text": parts["text"][:20000], "ts": ts}]
        return []

    if kind == "message_update":
        delta = event.get("assistantMessageEvent")
        if isinstance(delta, dict) and delta.get("type") == "text_delta":
            text = delta.get("delta")
            if isinstance(text, str) and text:
                return [{"type": MESSAGE_DELTA, "text": text[:2000], "ts": ts}]
        return []

    if kind == "tool_execution_start":
        return [{"type": classify_tool_event(event), "phase": "start", "ts": ts, **_tool_summary(event)}]

    if kind == "tool_execution_end":
        summary = _tool_summary(event)
        if event.get("isError"):
            return [{"type": FAILURE, "failure": "tool_failure", "phase": "tool", "ts": ts, **summary}]
        return [{"type": TOOL_RESULT, "phase": "end", "ts": ts, **summary}]

    if kind == "tool_execution_update":
        return []

    if kind in ("compaction_start", "compaction_end"):
        return [{"type": CONTEXT_EVENT, "phase": kind, "ts": ts}]

    if kind in ("auto_retry_start", "auto_retry_end"):
        return [{"type": WARNING, "reason": kind, "ts": ts}]

    if kind == "queue_update":
        return [{"type": WARNING, "reason": "queue_update", "ts": ts}]

    if kind == "extension_error":
        return [{"type": WARNING, "reason": "extension_error", "ts": ts}]

    if kind == "agent_end":
        messages = event.get("messages")
        return [{
            "type": COMPLETION,
            "phase": "run",
            "messages": len(messages) if isinstance(messages, list) else None,
            "ts": ts,
        }]

    return []


def files_from_event(raw: Dict[str, Any]) -> List[str]:
    """Paths a Pi file-modification tool event touched (empty otherwise)."""
    if not isinstance(raw, dict):
        return []
    if (raw.get("toolName") or "").lower() not in _WRITE_TOOLS:
        return []
    path = _tool_path(raw)
    return [path] if path else []


def command_from_event(raw: Dict[str, Any]) -> Optional[str]:
    """The shell command a Pi bash tool event ran, if any."""
    if not isinstance(raw, dict) or (raw.get("toolName") or "").lower() != "bash":
        return None
    command = _tool_args(raw).get("command")
    return command if isinstance(command, str) else None

