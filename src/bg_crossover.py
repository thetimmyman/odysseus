"""Tripwire for background-utility completions landing in a user's chat.

Background prompts (memory extraction, skill extraction, the completion
verifier) share the chat's model and endpoint, and each has a rigid output
contract no human reply satisfies. This detector lets the chat finaliser catch
one that leaked through; the isolation in ``src/llm_lane.py`` and
``src/background_tasks.py`` is the actual fix.

Every rule matches a whole response against an exact contract, never a
substring of a longer answer.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Background contracts are short; a long answer is a real reply even if it
# contains one of these shapes.
MAX_INSPECT_CHARS = 4000

# The skill extractor's documented decline token, plus the shapes an empty
# structured answer takes. None of these is ever a useful reply to a human.
_BARE_SENTINELS = {"null", "none", "[]", "{}", "nil", "undefined"}

# Verifier scaffolding (src/agent_loop.py::_run_verifier_subagent).
_VERIFIER_MARKERS = ("<actions_taken>", "<user_request>")
_VERIFIER_VERDICT = re.compile(r"^\s*VERIFICATION:\s*(SUCCESS|FAIL)\b", re.M)

# Memory extractor contract (services/memory/memory_extractor.py).
_MEMORY_CATEGORIES = {
    "identity", "preference", "fact", "contact", "project", "goal",
}

# Skill extractor contract (services/memory/skill_extractor.py).
_SKILL_KEYS = {"title", "problem", "solution", "steps"}


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL | re.IGNORECASE)


def _is_memory_extraction(payload) -> bool:
    """A JSON array of {"text", "category"} objects — and nothing else."""
    if not isinstance(payload, list) or not payload:
        return False
    for item in payload:
        if not isinstance(item, dict):
            return False
        if set(item.keys()) - {"text", "category", "id"}:
            return False
        if "text" not in item:
            return False
        category = item.get("category")
        if category is not None and str(category).lower() not in _MEMORY_CATEGORIES:
            return False
    return True


def _is_skill_extraction(payload) -> bool:
    """A JSON object carrying the skill extractor's required fields."""
    if not isinstance(payload, dict):
        return False
    return _SKILL_KEYS.issubset(set(payload.keys()))


def detect(response: str) -> Optional[str]:
    """Return a stable, loggable reason naming the subsystem when ``response`` is
    background output rather than a reply, else ``None``."""
    if not isinstance(response, str):
        return None
    body = _strip_think(response).strip()
    if not body or len(body) > MAX_INSPECT_CHARS:
        return None

    if body.lower() in _BARE_SENTINELS:
        return "bare-sentinel"

    if _VERIFIER_VERDICT.search(body) and any(m in body for m in _VERIFIER_MARKERS):
        return "completion-verifier"

    if body[0] in "[{":
        try:
            payload = json.loads(body)
        except (ValueError, TypeError):
            payload = None
        if payload is not None:
            if _is_memory_extraction(payload):
                return "memory-extractor"
            if _is_skill_extraction(payload):
                return "skill-extractor"
    return None


# Replaces leaked utility output in the user's view.
USER_NOTICE = (
    "⚠️ Something went wrong on my side: an internal background job's output was "
    "delivered into this chat instead of my reply to you. Nothing from it was "
    "saved. Please send your message again."
)


def guard_user_reply(response: str, *, session_id: str = "", where: str = "chat") -> tuple:
    """Screen a completion before it is shown/persisted as the reply.

    Returns ``(content, reason_or_None)``. On detection the content becomes
    :data:`USER_NOTICE` and an ERROR is logged with a ``[bg-crossover]`` marker.
    """
    reason = detect(response)
    if not reason:
        return response, None
    logger.error(
        "[bg-crossover] %s: refused to persist a %s completion as the assistant "
        "reply (session=%s, %d chars)",
        where, reason, session_id or "?", len(response or ""),
    )
    return USER_NOTICE, reason
