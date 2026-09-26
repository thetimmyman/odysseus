"""Athena, the Argo agent-crew supervisor.

Plans a quest into ordered subtasks, dispatches workers sequentially
(concurrency=1), multiplexes every child SSE event onto one redacted parent
stream, then synthesizes. Workers coordinate via the in-process Hermes bus and
a jailed Mnemosyne blackboard (``.argo/<run_id>`` under DATA_DIR). Roles default
to a local utility model and a read-only allowlist; ``write_mode=True`` widens
it and gates every side effect behind human approval. Budgets are checked
between dispatches plus an overall deadline, and a finally block always
expires pending gates and kills stray bg jobs.

``run_crew(...)`` is an async generator of SSE strings for ``agent_runs.start``.

Security invariants:
  * a falsy owner is rejected before any model resolution or DB write (it
    would make ``_resolve_model`` skip ``owner_filter`` and leak endpoint keys);
  * the same ``owner`` flows into every ``stream_agent_loop`` and
    ``execute_tool_block`` call;
  * read-only roles get only the allowlist, with MCP disabled and no gates;
  * blackboard paths are jailed to ``.argo/<run_id>`` before
    ``execute_tool_block``, whose roots also allow DATA_DIR and /tmp;
  * a crew worker can never spawn another crew.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncGenerator, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CrewBudget:
    """Runaway caps, enforced between dispatches and by a hard deadline that
    cancels the in-flight worker and cleans up bg jobs."""
    max_agents: int = 4
    max_total_rounds: int = 60
    token_budget: int = 0            # 0 = unlimited
    wall_clock_s: int = 600
    concurrency: int = 1             # fixed at 1: one local GPU
    recursion_depth: int = 1         # no crew-spawns-crew
    per_worker_rounds: int = 20      # MAX_AGENT_ROUNDS default
    stall_dispatches: int = 3        # unchanged-ledger dispatches => BLOCKED
    approval_timeout_s: float = 1800.0


# Read-only allowlist a default role is offered exactly; never gates.
READ_ONLY_ALLOWLIST: frozenset = frozenset({
    "read_file", "search_files", "find_files", "list_dir",
    "get_project", "web_search", "web_fetch", "suggest_document",
})

# Write mode adds side-effecting tools, each gated behind human approval.
WRITE_MODE_EXTRA: frozenset = frozenset({
    "write_file", "edit_file", "bash", "python",
})


def _all_tools() -> set:
    """The fixed built-in ``TOOL_TAGS`` set; excludes ``mcp__*`` tools, which
    can't be subtracted by name. Read-only roles disable MCP outright; in write
    mode the approval gate is the MCP control."""
    try:
        from src.agent_tools import TOOL_TAGS
        return set(TOOL_TAGS)
    except Exception:
        return set()


HOP_CAP = 12
_HERMES_ACTS = frozenset({
    "request", "inform", "propose", "query", "agree", "refuse", "done",
})


@dataclass
class Envelope:
    """A Hermes speech-act message; bodies are redacted before entering the queue."""
    id: str
    conversation: str
    sender: str                       # agent-id | "athena" | "human"
    to: str                           # agent-id | "athena" | "broadcast" | "human"
    act: str                          # request|inform|propose|query|agree|refuse|done
    body: str
    hops: int = 0
    in_reply_to: Optional[str] = None
    created_at: float = field(default_factory=time.time)


class HermesBus:
    """Per-crew-run fan-out bus; HOP_CAP drops runaway relays."""

    def __init__(self, run_id: str, owner: str) -> None:
        self.run_id = run_id
        self.owner = owner
        self._queues: Dict[str, asyncio.Queue] = {}
        self._seq = 0
        self._lock = asyncio.Lock()

    def register(self, agent_id: str) -> asyncio.Queue:
        q = self._queues.get(agent_id)
        if q is None:
            q = asyncio.Queue()
            self._queues[agent_id] = q
        return q

    async def next_id(self) -> str:
        async with self._lock:
            self._seq += 1
            return f"{self.run_id[:8]}-msg-{self._seq}"

    async def send(self, env: Envelope) -> bool:
        """Deliver an envelope (redacted); False if dropped (hop cap / bad act)."""
        from src.crew_approvals import _redact

        if env.act not in _HERMES_ACTS:
            logger.warning("Hermes: dropping bad act=%r", env.act)
            return False
        if env.hops > HOP_CAP:
            logger.warning("Hermes: HOP_CAP exceeded (hops=%d) — dropping %s", env.hops, env.id)
            return False
        env.body = _redact(env.body, self.owner)
        targets: List[str]
        if env.to == "broadcast":
            targets = [a for a in self._queues.keys() if a != env.sender]
        else:
            # "human"/"god" route to athena
            to = "athena" if env.to in ("human", "god") else env.to
            targets = [to]
        delivered = False
        for t in targets:
            q = self._queues.get(t)
            if q is None:
                continue
            try:
                q.put_nowait(env)
                delivered = True
            except Exception as e:
                logger.warning("Hermes: deliver to %s failed: %r", t, e)
        return delivered

    async def recv(self, agent_id: str, timeout: Optional[float] = None) -> Optional[Envelope]:
        q = self.register(agent_id)
        try:
            if timeout is None:
                return await q.get()
            return await asyncio.wait_for(q.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def gc(self) -> None:
        self._queues.clear()


class Mnemosyne:
    """Per-run blackboard under ``<DATA_DIR>/.argo/<run_id>``.

    ``_tool_path_roots()`` also allows DATA_DIR and /tmp, so every read/write
    goes through ``_mnemosyne_path``, which realpath-resolves and asserts
    containment in the run dir before any FS op. Single writer; values redacted.
    """

    def __init__(self, run_id: str, owner: str) -> None:
        self.run_id = run_id
        self.owner = owner
        self._lock = asyncio.Lock()
        from src.constants import DATA_DIR
        self.root = os.path.realpath(os.path.join(DATA_DIR, ".argo", run_id))

    def ensure_dir(self) -> str:
        """Create the run dir and notes/; raises if its realpath escapes .argo."""
        from src.constants import DATA_DIR
        argo_base = os.path.realpath(os.path.join(DATA_DIR, ".argo"))
        # The run dir's realpath must live directly under argo_base.
        if os.path.commonpath([self.root, argo_base]) != argo_base:
            raise ValueError("Mnemosyne root escapes the .argo base")
        os.makedirs(os.path.join(self.root, "notes"), exist_ok=True)
        return self.root

    def _mnemosyne_path(self, rel: str) -> str:
        """Resolve a blackboard-relative path, rejecting any escape before FS ops."""
        if not rel or not isinstance(rel, str):
            raise ValueError("blackboard path is required")
        if os.path.isabs(rel):
            raise ValueError("blackboard path must be relative")
        candidate = os.path.join(self.root, rel)
        resolved = os.path.realpath(candidate)
        try:
            common = os.path.commonpath([resolved, self.root])
        except ValueError:
            raise ValueError("blackboard path is on a different drive/root")
        if common != self.root:
            raise ValueError(f"blackboard path '{rel}' escapes the run dir")
        return resolved

    async def _write(self, rel: str, text: str) -> None:
        from src.crew_approvals import _redact
        path = self._mnemosyne_path(rel)
        body = _redact(text if isinstance(text, str) else str(text), self.owner)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # The jail helper is the security gate here, not execute_tool_block.
        await asyncio.to_thread(_atomic_write, path, body)

    async def _read(self, rel: str) -> str:
        path = self._mnemosyne_path(rel)
        if not os.path.exists(path):
            return ""
        return await asyncio.to_thread(_read_text, path)

    async def write_ledger(self, ledger: dict) -> None:
        async with self._lock:
            await self._write("ledger.json", json.dumps(ledger, default=str, indent=2))

    async def read_ledger(self) -> dict:
        raw = await self._read("ledger.json")
        if not raw.strip():
            return {"tasks": [], "updatedAt": time.time()}
        try:
            return json.loads(raw)
        except Exception:
            return {"tasks": [], "updatedAt": time.time()}

    async def append_note(self, agent_id: str, text: str) -> None:
        from src.crew_approvals import _redact
        safe_agent = os.path.basename(str(agent_id)) or "agent"
        rel = os.path.join("notes", f"{safe_agent}.md")
        async with self._lock:
            prev = await self._read(rel)
            body = _redact(text if isinstance(text, str) else str(text), self.owner)
            stamp = datetime.utcnow().isoformat()
            await self._write(rel, f"{prev}\n\n## {stamp}\n{body}".strip() + "\n")

    async def append_board(self, line: str) -> None:
        from src.crew_approvals import _redact
        async with self._lock:
            prev = await self._read("board.md")
            body = _redact(line if isinstance(line, str) else str(line), self.owner)
            await self._write("board.md", f"{prev}\n{body}".strip() + "\n")

    async def digest(self, max_chars: int = 2000) -> str:
        ledger = await self.read_ledger()
        tasks = ledger.get("tasks", [])
        lines = ["Voyage ledger:"]
        for t in tasks:
            lines.append(
                f"  - [{t.get('status', '?')}] {t.get('title', '')} "
                f"(assignee={t.get('assignee', '-')})"
            )
        board = await self._read("board.md")
        if board.strip():
            lines.append("\nVoyage log (tail):")
            lines.append(board.strip()[-max_chars:])
        out = "\n".join(lines)
        return out[:max_chars * 2]


def _atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _crew_candidates(owner: str) -> List[Tuple[str, str, Dict]]:
    """Owner-scoped ``(url, model, headers)`` candidates for crew LLM calls.

    Uses the utility fallback chain, else the owner's default model via
    ``_resolve_model`` (covers the single shared endpoint case). ``owner`` must
    be truthy so owner_filter always applies."""
    from src.endpoint_resolver import resolve_utility_fallback_candidates

    cands = list(resolve_utility_fallback_candidates(owner) or [])
    if cands:
        logger.info("crew: using configured utility fallback chain (%d candidate(s)) for owner", len(cands))
        return cands

    from src.ai_interaction import _resolve_model
    from src.settings import get_user_setting
    for key in ("utility_model", "task_model", "default_model"):
        spec = (get_user_setting(key, owner, "") or "").strip()
        if not spec:
            continue
        try:
            url, model, headers = _resolve_model(spec, owner=owner)
        except Exception as e:
            logger.info("crew: default-model fallback %s=%r unresolved (%r)", key, spec, e)
            continue
        if url and model:
            logger.info("crew: utility fallback chain empty; using configured %s=%r -> %s", key, spec, model)
            return [(url, model, headers)]
    logger.warning("crew: no LLM candidate resolves for owner (empty fallback chain AND no resolvable default model)")
    return []


def _resolve_role_model(spec: Optional[str], owner: str) -> Tuple[str, str, Dict]:
    """Resolve a role's model to (endpoint_url, model, headers): the per-role
    spec, else the first crew candidate. Raises if nothing resolves. ``owner``
    must be truthy."""
    from src.ai_interaction import _resolve_model

    if spec:
        try:
            return _resolve_model(spec, owner=owner)
        except Exception as e:
            logger.info("crew: role model '%s' unresolved (%r); using utility default", spec, e)
    cands = _crew_candidates(owner)
    for url, model, headers in cands:
        if url and model:
            return url, model, headers
    raise ValueError("No utility model endpoint configured for owner")


def _sse(event: dict, owner: str) -> str:
    """Serialize an event as an SSE string with the full payload redacted
    (secrets can appear in any field)."""
    from src.crew_approvals import _redact
    raw = "data: " + json.dumps(event, default=str) + "\n\n"
    return _redact(raw, owner)


async def _athena_plan(prompt: str, roles: List[dict], owner: str, max_subtasks: int) -> List[dict]:
    """One bounded LLM call splitting `prompt` into <= max_subtasks
    {title, detail, assignee_index}; degrades to a single subtask on failure."""
    from src.llm_core import llm_call_async_with_fallback

    role_lines = "\n".join(
        f"  {i}. {r.get('name', 'Argonaut')} ({r.get('role_kind', 'worker')})"
        for i, r in enumerate(roles)
    )
    sys = (
        "You are Athena, supervisor of an agent crew. Decompose the user's "
        "quest into a SHORT ordered list of independent subtasks, one per "
        f"available worker, at most {max_subtasks}. Reply with ONLY a JSON "
        'array: [{"title": "...", "detail": "...", "assignee_index": <int>}]. '
        "No prose, no code fences."
    )
    user = f"Quest:\n{prompt}\n\nAvailable workers:\n{role_lines}\n\nReturn the JSON array."
    cands = _crew_candidates(owner)
    try:
        raw = await llm_call_async_with_fallback(
            cands,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            timeout=60,
        )
    except Exception as e:
        logger.warning("crew: Athena plan call failed (%r); using single-subtask fallback", e)
        return [{"title": "Complete the quest", "detail": prompt, "assignee_index": 0}]

    plan = _parse_plan_json(raw)
    if not plan:
        return [{"title": "Complete the quest", "detail": prompt, "assignee_index": 0}]
    out: List[dict] = []
    for i, item in enumerate(plan[:max_subtasks]):
        idx = item.get("assignee_index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(roles):
            idx = i % max(1, len(roles))
        out.append({
            "title": str(item.get("title") or f"Subtask {i + 1}")[:200],
            "detail": str(item.get("detail") or item.get("title") or prompt),
            "assignee_index": idx,
        })
    return out or [{"title": "Complete the quest", "detail": prompt, "assignee_index": 0}]


def _parse_plan_json(raw: str) -> List[dict]:
    if not raw or not raw.strip():
        return []
    s = raw.strip()
    # Strip code fences if the model added them despite instructions.
    if s.startswith("```"):
        s = s.split("\n", 1)[-1]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    start = s.find("[")
    end = s.rfind("]")
    if start >= 0 and end > start:
        s = s[start:end + 1]
    try:
        data = json.loads(s)
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
    except Exception:
        pass
    return []


async def _athena_synthesize(prompt: str, worker_outputs: List[Tuple[str, str]], owner: str) -> str:
    """Final bounded LLM call summarizing worker outputs into a single answer."""
    from src.llm_core import llm_call_async_with_fallback

    digest = "\n\n".join(
        f"### {name}\n{(text or '').strip()[:3000]}" for name, text in worker_outputs
    ) or "(no worker output was produced)"
    sys = (
        "You are Athena, supervisor of an agent crew. Synthesize the workers' "
        "results into a single clear, complete answer to the user's quest. Be "
        "concise and do not invent results the workers did not produce."
    )
    user = f"Quest:\n{prompt}\n\nWorker results:\n{digest}\n\nFinal synthesis:"
    cands = _crew_candidates(owner)
    try:
        out = await llm_call_async_with_fallback(
            cands,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}],
            timeout=90,
        )
        return (out or "").strip() or digest
    except Exception as e:
        logger.warning("crew: Athena synthesis failed (%r); returning concatenated outputs", e)
        return digest


def _now() -> datetime:
    return datetime.utcnow()


def _persist_crew_run(crew_run_id, owner, prompt, crew_id, session_id, blackboard_dir):
    """Get-or-update the CrewRun row. The route inserts it synchronously for its
    owner check, so continue from an existing row (keeping its owner) instead
    of inserting a duplicate; insert fresh only when none exists."""
    from core.database import SessionLocal, CrewRun
    db = SessionLocal()
    try:
        row = db.query(CrewRun).filter(CrewRun.id == crew_run_id).first()
        if row is not None:
            row.blackboard_dir = blackboard_dir
            if not row.status:
                row.status = "running"
            if session_id and not row.session_id:
                row.session_id = session_id
            if crew_id and not row.crew_id:
                row.crew_id = crew_id
            db.commit()
            return
        row = CrewRun(
            id=crew_run_id, crew_id=crew_id, owner=owner, prompt=prompt,
            status="running", started_at=_now(),
            blackboard_dir=blackboard_dir, session_id=session_id,
        )
        db.add(row)
        db.commit()
    finally:
        db.close()


def _update_crew_run(crew_run_id, **fields):
    from core.database import SessionLocal, CrewRun
    db = SessionLocal()
    try:
        row = db.query(CrewRun).filter(CrewRun.id == crew_run_id).first()
        if row is not None:
            for k, v in fields.items():
                setattr(row, k, v)
            db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("crew: update CrewRun failed: %r", e)
    finally:
        db.close()


def _persist_agent_run(crew_run_id, crew_member_id, agent_id, role, subtask, model):
    from core.database import SessionLocal, CrewAgentRun
    aid = uuid.uuid4().hex
    db = SessionLocal()
    try:
        row = CrewAgentRun(
            id=aid, crew_run_id=crew_run_id, crew_member_id=crew_member_id,
            agent_id=agent_id, role=role, subtask=subtask, status="running",
            started_at=_now(), model=model,
        )
        db.add(row)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("crew: insert CrewAgentRun failed: %r", e)
    finally:
        db.close()
    return aid


def _update_agent_run(agent_run_id, **fields):
    from core.database import SessionLocal, CrewAgentRun
    db = SessionLocal()
    try:
        row = db.query(CrewAgentRun).filter(CrewAgentRun.id == agent_run_id).first()
        if row is not None:
            for k, v in fields.items():
                setattr(row, k, v)
            db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("crew: update CrewAgentRun failed: %r", e)
    finally:
        db.close()


async def _dispatch_worker(
    *,
    crew_run_id: str,
    owner: str,
    session_id: str,
    agent_id: str,
    role: dict,
    subtask: dict,
    write_mode: bool,
    budget: CrewBudget,
    mnemosyne: Mnemosyne,
    emit_q: asyncio.Queue,
) -> AsyncGenerator[str, None]:
    """Drive one worker and multiplex its SSE onto the parent crew stream,
    re-tagged and fully redacted.

    Parse contract (same as task_scheduler): skip ``data: [DONE]`` before
    json.loads, detect text by ``"delta" in data``, pass ``event: error``
    through, and suppress each child's [DONE].
    """
    from src.agent_loop import stream_agent_loop
    from src.stream_events import answer_delta as _answer_delta
    from src import tool_execution
    from src.crew_approvals import _redact

    role_name = role.get("name") or "Argonaut"
    role_kind = role.get("role_kind") or "worker"

    endpoint_url, model, headers = _resolve_role_model(role.get("model_spec"), owner)

    # disabled_tools = all built-in tools minus the offered allowlist.
    if write_mode:
        offered = set(role.get("enabled_tools") or READ_ONLY_ALLOWLIST) | WRITE_MODE_EXTRA
    else:
        # Read-only: only the allowlist (ignore wider per-role lists).
        offered = set(READ_ONLY_ALLOWLIST)
    disabled_tools = _all_tools() - offered

    agent_run_id = _persist_agent_run(
        crew_run_id, role.get("crew_member_id"), agent_id, f"{role_name}/{role_kind}",
        subtask.get("detail"), model,
    )

    digest = await mnemosyne.digest()
    persona = role.get("persona") or (
        f"You are {role_name}, an Argonaut on Athena's crew. Complete your "
        "assigned subtask thoroughly using your available tools, then state a "
        "concise final result. You share a blackboard with the crew."
    )
    system_content = f"{persona}\n\n--- Crew blackboard ---\n{digest}"
    user_content = (
        f"Your subtask: {subtask.get('title')}\n\n{subtask.get('detail', '')}"
    )
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content},
    ]

    crew_ctx = {
        "crew_run_id": crew_run_id,
        "agent_id": agent_id,
        "role": role_name,
        "gate_writes": write_mode,
    }

    # Gate context read by the leaf hook; `emit` pushes approval requests onto
    # the parent stream. State lives in crew_approvals.
    async def _gate_emit(event: dict) -> None:
        event = dict(event)
        event["role"] = role_name
        await emit_q.put(_sse(event, owner))

    gate_ctx = {
        "crew_run_id": crew_run_id,
        "owner": owner,
        "agent_id": agent_id,
        "write_mode": write_mode,
        "emit": _gate_emit,
        "approval_timeout": budget.approval_timeout_s,
    }

    crew_mode_token = tool_execution.set_crew_mode(True)
    crew_gate_token = tool_execution.set_crew_gate(gate_ctx)

    full_text = ""
    tool_results: List[str] = []
    rounds = 0
    worker_tokens = 0            # per-worker token tally (from metrics SSE)
    status = "running"
    err: Optional[str] = None

    yield _sse({
        "type": "crew_agent_start", "agent_id": agent_id, "role": role_name,
        "role_kind": role_kind, "subtask": subtask.get("title"), "model": model,
    }, owner)
    yield _sse({
        "type": "crew_handoff", "from": "athena", "to": agent_id,
        "subtask": subtask.get("title"),
    }, owner)

    try:
        async for event_str in stream_agent_loop(
            endpoint_url=endpoint_url,
            model=model,
            messages=messages,
            headers=headers,
            max_rounds=role.get("max_steps") or budget.per_worker_rounds,
            session_id=session_id,
            owner=owner,
            disabled_tools=disabled_tools,
            fallbacks=_crew_candidates(owner),
            crew_ctx=crew_ctx,
        ):
            if not event_str.startswith("data: "):
                # Bare `event: error` lines can't be re-tagged; forward as-is.
                yield _redact(event_str, owner)
                continue
            if event_str.startswith("data: [DONE]"):
                # The parent emits crew_done at the end of the run.
                continue
            try:
                data = json.loads(event_str[6:])
            except (json.JSONDecodeError, KeyError):
                yield _redact(event_str, owner)
                continue

            # Accumulate answer deltas only: reasoning deltas are still forwarded
            # to the UI but must not reach the next worker as a result.
            if "delta" in data:
                _ans = _answer_delta(data)
                if _ans is not None:
                    full_text += _ans
            elif data.get("type") == "tool_output":
                summary = data.get("stdout") or data.get("output") or data.get("result") or ""
                if isinstance(summary, str) and summary.strip():
                    tool_results.append(f"[{data.get('tool', '?')}] {summary[:500]}")
            elif data.get("type") == "agent_step":
                rounds = max(rounds, int(data.get("round") or 0))
            elif data.get("type") == "metrics":
                # The leaf's final metrics event carries real token usage for
                # the crew token_budget; prefer total_tokens, else the parts.
                _m = data.get("data") or {}
                _tok = _m.get("total_tokens")
                if _tok is None:
                    _tok = (_m.get("input_tokens") or 0) + (_m.get("output_tokens") or 0)
                try:
                    worker_tokens += int(_tok or 0)
                except (TypeError, ValueError):
                    pass

            data["agent_id"] = agent_id
            data["role"] = role_name
            t = data.get("type")
            if "delta" in data and t is None:
                data["type"] = "crew_agent_output"
            elif t in ("tool_start", "tool_output", "tool_progress", "agent_step",
                       "web_sources", "budget_exceeded", "metrics"):
                # Approval requests arrive via the emit callback; tool lifecycle
                # events become crew_agent_step.
                if t in ("tool_start", "tool_output", "tool_progress"):
                    data["type"] = "crew_agent_step"
            elif t == "crew_approval_request":
                pass
            yield _sse(data, owner)
    finally:
        tool_execution.reset_crew_gate(crew_gate_token)
        tool_execution.reset_crew_mode(crew_mode_token)

    if not full_text.strip():
        try:
            from src.llm_core import llm_call_async_with_fallback
            grace = "You ran out of steps. "
            if tool_results:
                grace += "Here's what your tools returned:\n" + "\n".join(tool_results[-5:])
            else:
                grace += "No tool results were captured."
            grace += "\n\nSummarize what you accomplished and what's still pending. Be concise."
            cands = [(endpoint_url, model, headers)] + _crew_candidates(owner)
            full_text = (await llm_call_async_with_fallback(
                cands,
                messages=[
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": grace},
                ],
                timeout=30,
            ) or "").strip()
        except Exception as e:
            logger.warning("crew: grace summarize failed: %r", e)
            if tool_results:
                full_text = "\n".join(tool_results[-5:])

    status = "success" if full_text.strip() else "error"
    err = None if full_text.strip() else "worker produced no output"
    result_text = full_text or "(no output)"

    try:
        await mnemosyne.append_note(agent_id, f"### {subtask.get('title')}\n{result_text}")
    except Exception as e:
        logger.warning("crew: blackboard note write failed: %r", e)
    _update_agent_run(
        agent_run_id, status=status, finished_at=_now(),
        result=result_text[:20000], error=err, rounds=rounds,
        tokens_used=worker_tokens or None,
    )
    yield _sse({
        "type": "crew_agent_output", "agent_id": agent_id, "role": role_name,
        "subtask": subtask.get("title"), "result": result_text,
        "status": status, "rounds": rounds,
    }, owner)

    # Yield the worker-result sentinel last so the dispatch loop collects it
    # after every SSE string without racing the stream tail.
    yield ("__worker_result__", agent_id, role_name, result_text, rounds, worker_tokens)


async def run_crew(
    owner: str,
    prompt: str,
    write_mode: bool = False,
    crew_id: Optional[str] = None,
    roles: Optional[List[dict]] = None,
    *,
    crew_run_id: Optional[str] = None,
    session_id: Optional[str] = None,
    budget: Optional[CrewBudget] = None,
    recursion_depth: int = 1,
) -> AsyncGenerator[str, None]:
    """Run an Argo crew quest end-to-end, yielding the multiplexed parent SSE.

    Plan -> sequential worker dispatch -> synthesis. Read-only by default;
    ``write_mode=True`` widens the allowlist and wires the approval gate. A falsy
    ``owner`` is rejected before any model resolution or DB write.
    """
    if not owner:
        raise ValueError("run_crew requires a non-empty owner (cross-owner secret-leak guard)")
    if recursion_depth and recursion_depth > 1:
        raise ValueError("crew recursion is not permitted (depth > 1)")

    budget = budget or CrewBudget()
    crew_run_id = crew_run_id or uuid.uuid4().hex
    roles = list(roles or [])
    if not roles:
        roles = [{"name": "Argonaut-1", "role_kind": "worker"}]
    # Cap workers at max_agents; Athena is not a dispatched worker.
    roles = roles[: max(1, budget.max_agents)]

    bus = HermesBus(crew_run_id, owner)
    mnemosyne = Mnemosyne(crew_run_id, owner)

    # Workers push SSE, gate emits and result sentinels here; the main loop
    # drains it so gate requests from execute_tool_block reach the stream.
    emit_q: asyncio.Queue = asyncio.Queue()

    blackboard_dir = None
    started = time.monotonic()
    deadline = started + max(1, budget.wall_clock_s)
    worker_outputs: List[Tuple[str, str]] = []
    total_rounds = 0
    total_tokens = 0            # run-level token tally for token_budget
    final_status = "running"
    final_error: Optional[str] = None
    final_result: Optional[str] = None

    try:
        try:
            blackboard_dir = mnemosyne.ensure_dir()
        except Exception as e:
            logger.error("crew: blackboard confine failed: %r", e)
            raise
        _persist_crew_run(crew_run_id, owner, prompt, crew_id, session_id, blackboard_dir)
        bus.register("athena")

        yield _sse({
            "type": "crew_agent_start", "agent_id": "athena", "role": "Athena",
            "role_kind": "planner", "crew_run_id": crew_run_id,
            "write_mode": bool(write_mode),
        }, owner)

        max_subtasks = min(budget.max_agents, len(roles))
        plan = await _athena_plan(prompt, roles, owner, max_subtasks)
        _update_crew_run(crew_run_id, plan=json.dumps(plan, default=str))
        # Athena is the ledger's sole writer.
        ledger = {
            "tasks": [
                {
                    "id": f"t{i}", "title": p.get("title"), "status": "todo",
                    "assignee": roles[p.get("assignee_index", 0) % len(roles)].get("name"),
                    "priority": i, "createdAt": time.time(), "updatedAt": time.time(),
                }
                for i, p in enumerate(plan)
            ],
            "updatedAt": time.time(),
        }
        await mnemosyne.write_ledger(ledger)
        yield _sse({"type": "crew_step", "agent_id": "athena", "phase": "planned",
                    "subtasks": [p.get("title") for p in plan]}, owner)

        sem = asyncio.Semaphore(budget.concurrency)
        last_ledger_hash = None
        stall_count = 0

        for i, sub in enumerate(plan):
            if i >= budget.max_agents:
                final_status = "blocked"
                yield _sse({"type": "crew_step", "agent_id": "athena",
                            "phase": "blocked", "reason": "max_agents"}, owner)
                break
            if total_rounds >= budget.max_total_rounds:
                final_status = "blocked"
                yield _sse({"type": "crew_step", "agent_id": "athena",
                            "phase": "blocked", "reason": "max_total_rounds"}, owner)
                break
            # Stop dispatching once finished workers exceed token_budget (0 = unlimited).
            if budget.token_budget and total_tokens >= budget.token_budget:
                final_status = "blocked"
                yield _sse({"type": "crew_step", "agent_id": "athena",
                            "phase": "blocked", "reason": "token_budget",
                            "tokens_used": total_tokens,
                            "token_budget": budget.token_budget}, owner)
                break
            if time.monotonic() >= deadline:
                final_status = "blocked"
                yield _sse({"type": "crew_step", "agent_id": "athena",
                            "phase": "blocked", "reason": "wall_clock"}, owner)
                break
            # Stall detection: unchanged ledger for K dispatches => BLOCKED.
            cur = await mnemosyne.read_ledger()
            cur_hash = hash(json.dumps(cur.get("tasks", []), sort_keys=True, default=str))
            if cur_hash == last_ledger_hash:
                stall_count += 1
            else:
                stall_count = 0
            last_ledger_hash = cur_hash
            if stall_count >= budget.stall_dispatches:
                final_status = "blocked"
                yield _sse({"type": "crew_step", "agent_id": "athena",
                            "phase": "blocked", "reason": "stall"}, owner)
                break

            role = roles[sub.get("assignee_index", 0) % len(roles)]
            agent_id = f"argonaut-{i + 1}"
            bus.register(agent_id)

            for t in ledger["tasks"]:
                if t.get("id") == f"t{i}":
                    t["status"] = "doing"
                    t["updatedAt"] = time.time()
            ledger["updatedAt"] = time.time()
            await mnemosyne.write_ledger(ledger)

            remaining = max(1.0, deadline - time.monotonic())
            worker_done = {"result": None}

            async def _drive_worker(_role=role, _sub=sub, _agent_id=agent_id):
                async for item in _dispatch_worker(
                    crew_run_id=crew_run_id, owner=owner, session_id=session_id,
                    agent_id=_agent_id, role=_role, subtask=_sub,
                    write_mode=write_mode, budget=budget,
                    mnemosyne=mnemosyne, emit_q=emit_q,
                ):
                    await emit_q.put(item)
                await emit_q.put(("__worker_done__", _agent_id))

            async with sem:
                drive_task = asyncio.create_task(_drive_worker())
                try:
                    while True:
                        try:
                            item = await asyncio.wait_for(emit_q.get(), timeout=remaining)
                        except asyncio.TimeoutError:
                            # Deadline hit: cancel the child (its finally kills
                            # the subprocess) and stop the run.
                            drive_task.cancel()
                            try:
                                await drive_task
                            except (asyncio.CancelledError, Exception):
                                pass
                            final_status = "blocked"
                            yield _sse({"type": "crew_step", "agent_id": "athena",
                                        "phase": "blocked", "reason": "wall_clock_inflight"}, owner)
                            break
                        if isinstance(item, tuple) and item and item[0] == "__worker_done__":
                            break
                        if isinstance(item, tuple) and item and item[0] == "__worker_result__":
                            # 6-tuple: (__worker_result__, agent, role, text, rounds, tokens)
                            _, w_agent, w_role, w_text, w_rounds = item[:5]
                            w_tokens = item[5] if len(item) > 5 else 0
                            worker_outputs.append((w_role, w_text))
                            total_rounds += int(w_rounds or 0)
                            total_tokens += int(w_tokens or 0)
                            # Persist so reconnects and the budget check see it.
                            _update_crew_run(crew_run_id, tokens_used=total_tokens)
                            continue
                        yield item
                        remaining = max(0.1, deadline - time.monotonic())
                    if final_status == "blocked":
                        break
                finally:
                    if not drive_task.done():
                        drive_task.cancel()
                        try:
                            await drive_task
                        except (asyncio.CancelledError, Exception):
                            pass

            for t in ledger["tasks"]:
                if t.get("id") == f"t{i}":
                    t["status"] = "done"
                    t["updatedAt"] = time.time()
            ledger["updatedAt"] = time.time()
            await mnemosyne.write_ledger(ledger)

        final_result = await _athena_synthesize(prompt, worker_outputs, owner)
        await mnemosyne.append_board(f"Synthesis:\n{final_result}")
        if final_status == "running":
            final_status = "success"
        _update_crew_run(
            crew_run_id, status=final_status, finished_at=_now(),
            result=(final_result or "")[:50000],
            tokens_used=total_tokens or None,
        )
        yield _sse({
            "type": "crew_done", "crew_run_id": crew_run_id, "status": final_status,
            "result": final_result, "agent_id": "athena", "role": "Athena",
        }, owner)

    except asyncio.CancelledError:
        final_status = "stopped"
        _update_crew_run(crew_run_id, status="stopped", finished_at=_now())
        raise
    except Exception as e:
        final_status = "error"
        final_error = str(e)
        logger.error("crew run %s failed: %r", crew_run_id, e, exc_info=True)
        _update_crew_run(crew_run_id, status="error", error=str(e)[:5000], finished_at=_now())
        try:
            yield _sse({"type": "crew_done", "crew_run_id": crew_run_id,
                        "status": "error", "error": str(e), "agent_id": "athena"}, owner)
        except Exception:
            pass
    finally:
        # Always expire pending gates, kill stray bg jobs and GC the bus, on
        # success, error, cancel and GeneratorExit.
        try:
            from src import crew_approvals
            await crew_approvals.expire_run_gates(crew_run_id)
        except Exception as e:
            logger.warning("crew: expire_run_gates failed: %r", e)
        try:
            if session_id:
                from src import bg_jobs
                bg_jobs.kill_for_session(session_id)
        except Exception as e:
            logger.warning("crew: kill_for_session failed: %r", e)
        try:
            bus.gc()
        except Exception:
            pass
