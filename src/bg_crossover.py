"""Tripwire for background-utility completions landing in a user's chat.

Odysseus runs several *background* prompts against the same model and endpoint
as the user's chat turn: memory extraction, skill extraction, the completion
verifier. Each one has a rigid, machine-readable output contract that no reply
to a human ever satisfies. That makes them cheaply recognisable, and on
2026-08-24/25 two of them were persisted verbatim as the assistant's reply in a
live session (POS-AI-23):

* session ``8670f5ae`` 2026-08-24 17:43:13 — the memory extractor's
  ``[{"text": ..., "category": "identity"}, ...]`` array, answering the user
  message "yes install whatever we need to make this work";
* session ``2c490607`` 2026-08-25 11:42:43 — the skill extractor's literal
  ``null`` decline token (the string "return null" appears nowhere in this
  codebase except that extractor's prompt), answering a FizzBuzz request.

Both were written by the normal chat finaliser, with normal chat metadata, so
nothing downstream could tell they were wrong. This module gives the finaliser a
way to tell. It is deliberately a *detector*, not a fix: the isolation work
(``src/llm_lane.py``, ``src/background_tasks.py``) is what should stop the
crossover happening; this is what makes it impossible to happen *silently* if
some path we have not found still leaks.

Kept intentionally narrow — every rule matches a whole response against an
exact background output contract, never a substring of a longer human answer.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Only inspect short replies. Every background contract here produces a small
# payload; a long, discursive answer is a real reply even if it happens to
# contain one of these shapes somewhere inside it.
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
    """Return a short reason when ``response`` is a background subsystem's
    output rather than a reply to the user, else ``None``.

    The reason string is stable and safe to log/store — it names the
    subsystem, never the content.
    """
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


# Shown to the user in place of the leaked utility output. Says what happened
# and what to do, rather than presenting `null` as an answer.
USER_NOTICE = (
    "⚠️ Something went wrong on my side: an internal background job's output was "
    "delivered into this chat instead of my reply to you. Nothing from it was "
    "saved. Please send your message again."
)


def guard_user_reply(response: str, *, session_id: str = "", where: str = "chat") -> tuple:
    """Screen a completion that is about to be shown/persisted as the assistant's
    reply.

    Returns ``(content, reason_or_None)``. When a crossover is detected the
    content is replaced with :data:`USER_NOTICE` and an ERROR is logged with a
    stable ``[bg-crossover]`` marker so it can be alerted on.
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
