"""crew_orchestrator.py — "Athena", the Argo agent-crew supervisor (Tier-1).

This is the genuinely-new control logic of the Argo crew. It:

  * decomposes a quest into a small ordered list of subtasks (the **planner**
    step, Athena), then dispatches 2-3 worker Argonauts **sequentially**
    (concurrency=1 — one iGPU), then runs a **synthesis** step,
  * drives each worker with `task_scheduler._run_agent_loop`'s consume/parse
    recipe but, instead of consume-and-discard, MULTIPLEXES every child SSE
    event onto a single parent crew stream (re-tagged with agent_id/role,
    fully secret-redacted via `crew_approvals._redact`),
  * coordinates workers through an in-process **Hermes** speech-act bus and a
    confined **Mnemosyne** blackboard (`.argo/<run_id>` under DATA_DIR,
    commonpath-jailed),
  * defaults every role to a local utility model and a READ-ONLY tool
    allowlist (Decision B) so the MVP dogfood never trips an approval; a
    per-run `write_mode=True` widens the allowlist and WIRES the Oracle's-seal
    gate (in `tool_execution.execute_tool_block`) for every side effect,
  * enforces wall-clock / round / agent / token budgets as between-dispatch
    checks AND an overall asyncio deadline that cancels the in-flight worker,
  * ALWAYS cleans up in a try/finally: expire every pending gate
    (`crew_approvals.expire_run_gates`) + kill stray bg jobs
    (`bg_jobs.kill_for_session`).

The public surface is ``run_crew(...)`` — an async generator of SSE strings.
The caller passes the whole generator to ``agent_runs.start(crew_run_id, gen)``
and streams it via ``agent_runs.subscribe(crew_run_id)``.

Security invariants (do NOT weaken):
  * a falsy owner is rejected BEFORE any model resolution or DB write
    (a falsy owner makes `_resolve_model` skip `owner_filter` and leak foreign
    endpoint api_keys, `ai_interaction.py:88`),
  * the SAME `owner` flows into EVERY `stream_agent_loop(owner=owner)` AND
    (via the gate context + the leaf) `execute_tool_block(owner=owner)`,
  * a read-only role is offered ONLY the enumerated read-only allowlist and
    runs with `crew_ctx={"gate_writes": False}` so MCP is disabled and nothing
    gates,
  * Mnemosyne reads/writes are commonpath-jailed to `.argo/<run_id>` BEFORE
    they reach `execute_tool_block` (whose `_tool_path_roots` also allows
    DATA_DIR+/tmp, so project_root confinement is NOT enough),
  * a crew worker may NOT spawn another crew (run_crew is never exposed as a
    tool; depth>1 is refused).
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

# ---------------------------------------------------------------------------
# Budgets / caps
# ---------------------------------------------------------------------------


@dataclass
class CrewBudget:
    """Runaway-cap object. Enforced as between-dispatch checks AND a hard
    asyncio deadline that cancels the in-flight worker (killing its
    subprocesses) and triggers bg-job cleanup."""
    max_agents: int = 4
    max_total_rounds: int = 60
    token_budget: int = 0            # 0 = unlimited
    wall_clock_s: int = 600
    concurrency: int = 1             # HARD-WIRED 1 for Tier-1 (one iGPU)
    recursion_depth: int = 1         # no crew-spawns-crew
    per_worker_rounds: int = 20      # MAX_AGENT_ROUNDS default
    stall_dispatches: int = 3        # unchanged-ledger dispatches => BLOCKED
    approval_timeout_s: float = 1800.0


# The ENUMERATED read-only allowlist (Decision B). A default role is offered
# EXACTLY these — no globs. In read-only mode nothing here ever gates.
READ_ONLY_ALLOWLIST: frozenset = frozenset({
    "read_file", "search_files", "find_files", "list_dir",
    "get_project", "web_search", "web_fetch", "suggest_document",
})

# In write mode the allowlist widens to add the side-effecting tools — each of
# which is then gated behind the Oracle's seal.
WRITE_MODE_EXTRA: frozenset = frozenset({
    "write_file", "edit_file", "bash", "python",
})


def _all_tools() -> set:
    """The built-in tool universe to subtract from. NOTE: this is the fixed
    `TOOL_TAGS` set and does NOT include dynamically-namespaced `mcp__*` tools
    — per-role allowlists cannot subtract an MCP tool by name. For a read-only
    role MCP is disabled outright (`crew_ctx.gate_writes=False` -> `mcp_mgr=None`
    in the leaf); for a write-mode role the approval gate is the real MCP
    control."""
    try:
        from src.agent_tools import TOOL_TAGS
        return set(TOOL_TAGS)
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Hermes — in-process per-run speech-act pub/sub
# ---------------------------------------------------------------------------

HOP_CAP = 12
_HERMES_ACTS = frozenset({
    "request", "inform", "propose", "query", "agree", "refuse", "done",
})


@dataclass
class Envelope:
    """A Hermes speech-act message. Bodies are redacted BEFORE entering the
    queue (`HermesBus.send` redacts), so a secret never lands in an inbox."""
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
    """In-process per-crew-run fan-out bus. Lifetime = the crew run; the
    orchestrator's finally GCs it. HOP_CAP drops runaway relays."""

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
        """Deliver an envelope. Returns False if dropped (hop cap / bad act).
        Redacts the body before it enters any inbox."""
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
            # "human"/"god" route to athena (Tier-1)
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


# ---------------------------------------------------------------------------
# Mnemosyne — confined per-run blackboard scratch dir
# ---------------------------------------------------------------------------


class Mnemosyne:
    """Per-run blackboard under ``<DATA_DIR>/.argo/<run_id>``.

    SECURITY: `_tool_path_roots()` also allows DATA_DIR + /tmp, so project_root
    confinement is NOT a jail by itself. EVERY read/write goes through
    `_mnemosyne_path`, which realpath-resolves the target and asserts
    `commonpath([resolved, root]) == root` — rejecting `..`, absolute escapes,
    symlink escapes, and anything under DATA_DIR-but-outside-the-run-dir or
    /tmp — BEFORE any FS op. Single async writer + Lock; all values redacted.
    """

    def __init__(self, run_id: str, owner: str) -> None:
        self.run_id = run_id
        self.owner = owner
        self._lock = asyncio.Lock()
        from src.constants import DATA_DIR
        # The jailed root: <DATA_DIR>/.argo/<run_id>, fully realpath-resolved.
        self.root = os.path.realpath(os.path.join(DATA_DIR, ".argo", run_id))

    def ensure_dir(self) -> str:
        """Create the run dir (and notes/) under the jailed root. Returns the
        realpath. Raises if the realpath escapes <DATA_DIR>/.argo."""
        from src.constants import DATA_DIR
        argo_base = os.path.realpath(os.path.join(DATA_DIR, ".argo"))
        # The run dir's realpath must live directly under argo_base.
        if os.path.commonpath([self.root, argo_base]) != argo_base:
            raise ValueError("Mnemosyne root escapes the .argo base")
        os.makedirs(os.path.join(self.root, "notes"), exist_ok=True)
        return self.root

    def _mnemosyne_path(self, rel: str) -> str:
        """Resolve a blackboard-relative path and assert it stays inside the
        run dir. Rejects '..'/absolute/symlink escapes BEFORE any FS op."""
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
        # Single async writer under the lock. Raw confined write inside the
        # already-commonpath-jailed run dir (the helper is the security gate,
        # not execute_tool_block's broader roots).
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


# ---------------------------------------------------------------------------
# Model resolution helper
# ---------------------------------------------------------------------------


def _crew_candidates(owner: str) -> List[Tuple[str, str, Dict]]:
    """Owner-scoped LLM candidate list for every crew (Athena) call.

    Prefers the explicitly-configured utility fallback CHAIN
    (`resolve_utility_fallback_candidates`). When that chain is empty — the
    common single-shared-endpoint deployment, where only `utility_model` /
    `task_model` / `default_model` are set but no `*_fallbacks` list exists —
    fall back to resolving the owner's configured default model directly via
    `_resolve_model` (the SAME path chat uses, which finds the shared
    owner=None endpoint). Returns a list of `(url, model, headers)` tuples in
    the shape `llm_call_async_with_fallback` iterates (`for url, model, headers
    in cands`); `[]` only if nothing resolves at all.

    `owner` MUST be truthy (caller rejects falsy owner first) so the
    owner_filter is always applied and no foreign endpoint key can leak."""
    from src.endpoint_resolver import resolve_utility_fallback_candidates

    cands = list(resolve_utility_fallback_candidates(owner) or [])
    if cands:
        logger.info("crew: using configured utility fallback chain (%d candidate(s)) for owner", len(cands))
        return cands

    # Fallback chain empty — resolve the owner's configured default model directly.
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
    """Resolve a role's model to (endpoint_url, model, headers).

    Prefers the explicit per-role spec via `_resolve_model(spec, owner)`; on
    failure (or no spec) falls back to the FIRST owner-scoped crew candidate
    (`_crew_candidates`) — i.e. every role defaults to a local utility model.
    Raises if nothing resolves. `owner` MUST be truthy (caller rejects falsy
    owner first), so the owner_filter is always applied and no foreign endpoint
    key can leak."""
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


# ---------------------------------------------------------------------------
# SSE helpers (multiplexer)
# ---------------------------------------------------------------------------


def _sse(event: dict, owner: str) -> str:
    """Serialize an event dict to a `data: {...}\\n\\n` SSE string with the
    FULL payload redacted (fix #1 — secrets leak through many fields, not just
    `command`)."""
    from src.crew_approvals import _redact
    raw = "data: " + json.dumps(event, default=str) + "\n\n"
    return _redact(raw, owner)


# ---------------------------------------------------------------------------
# Athena — planner + synthesis (bounded LLM calls)
# ---------------------------------------------------------------------------


async def _athena_plan(prompt: str, roles: List[dict], owner: str, max_subtasks: int) -> List[dict]:
    """One bounded LLM call: decompose `prompt` into <= max_subtasks ordered
    subtasks. Owner-scoped utility fallbacks. Returns a list of
    {title, detail, assignee_index}. Degrades to a single subtask on failure."""
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
    # Clamp to max_subtasks and to valid assignee indices.
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
    # Find the first JSON array.
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


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.utcnow()


def _persist_crew_run(crew_run_id, owner, prompt, crew_id, session_id, blackboard_dir):
    """GET-OR-UPDATE the CrewRun row. The /api/crew/run route INSERTs an
    owner-stamped row SYNCHRONOUSLY (so its pre-subscribe owner-compare can't
    404 before this scheduled body runs). If that row already exists we must
    NOT insert a second one (the id is the PK -> IntegrityError); instead we
    CONTINUE from it, filling in the fields the route didn't set (blackboard_dir)
    and re-asserting the running status. Falls back to a fresh INSERT when no
    row exists yet (e.g. a direct run_crew() caller that didn't pre-insert)."""
    from core.database import SessionLocal, CrewRun
    db = SessionLocal()
    try:
        row = db.query(CrewRun).filter(CrewRun.id == crew_run_id).first()
        if row is not None:
            # Existing row (route pre-inserted it). Do NOT insert a duplicate;
            # continue using it. Keep the owner the route stamped; only fill in
            # what the route couldn't know yet.
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


# ---------------------------------------------------------------------------
# Worker dispatch (the multiplexer core)
# ---------------------------------------------------------------------------


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
    """Drive ONE worker via stream_agent_loop and MULTIPLEX its SSE onto the
    parent crew stream. Yields re-tagged, redacted parent SSE strings.

    The parse contract is copied verbatim from task_scheduler.py:1578-1591:
      * skip a literal `data: [DONE]` before json.loads,
      * detect text by `"delta" in data` (NOT type=="delta"),
      * pass through `event: error`,
      * SUPPRESS each child's [DONE] (the parent crew_done is emitted later).
    Every emitted payload is `_redact(...)`-scrubbed in full (fix #1).
    """
    from src.agent_loop import stream_agent_loop
    from src import tool_execution
    from src.crew_approvals import _redact

    role_name = role.get("name") or "Argonaut"
    role_kind = role.get("role_kind") or "worker"

    # 1. Resolve this role's model (defaults to a local utility model).
    endpoint_url, model, headers = _resolve_role_model(role.get("model_spec"), owner)

    # 2. Compute disabled_tools = (all built-in tools) MINUS the offered allowlist.
    if write_mode:
        offered = set(role.get("enabled_tools") or READ_ONLY_ALLOWLIST) | WRITE_MODE_EXTRA
    else:
        # Read-only default: ONLY the enumerated allowlist (ignore any wider
        # per-role list — Decision B), so nothing gates and MCP is disabled.
        offered = set(READ_ONLY_ALLOWLIST)
    disabled_tools = _all_tools() - offered

    # 3. Persist the child run row.
    agent_run_id = _persist_agent_run(
        crew_run_id, role.get("crew_member_id"), agent_id, f"{role_name}/{role_kind}",
        subtask.get("detail"), model,
    )

    # 4. Build messages (system = persona + Mnemosyne digest; user = subtask).
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

    # 5. The per-worker gate context the leaf hook reads. `emit` pushes a
    #    crew_approval_request onto the parent stream (via the multiplexer
    #    queue). State lives in crew_approvals only; this just opens/awaits it.
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

    # 6. set_crew_mode (foreground #!bg) + set_crew_gate (Oracle's seal) around
    #    the whole worker dispatch; reset both in finally.
    crew_mode_token = tool_execution.set_crew_mode(True)
    crew_gate_token = tool_execution.set_crew_gate(gate_ctx)

    full_text = ""
    tool_results: List[str] = []
    rounds = 0
    worker_tokens = 0            # fix #3: per-worker token tally (from metrics SSE)
    status = "running"
    err: Optional[str] = None

    # Announce the worker + the handoff.
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
            # --- copy task_scheduler.py:1578-1591 parse contract ---
            if not event_str.startswith("data: "):
                # Non-`data:` lines = the multi-line `event: error` form. Pass
                # through verbatim (still redacted), re-tagged is not possible
                # for the bare event line, so forward as-is.
                yield _redact(event_str, owner)
                continue
            if event_str.startswith("data: [DONE]"):
                # SUPPRESS each child's [DONE]; the parent crew_done is emitted
                # at the very end of the run.
                continue
            try:
                data = json.loads(event_str[6:])
            except (json.JSONDecodeError, KeyError):
                # Unparseable data line — forward redacted, don't crash the lane.
                yield _redact(event_str, owner)
                continue

            # Accumulate text/tool results exactly as the scheduler does.
            if "delta" in data:
                full_text += data.get("delta") or ""
            elif data.get("type") == "tool_output":
                summary = data.get("stdout") or data.get("output") or data.get("result") or ""
                if isinstance(summary, str) and summary.strip():
                    tool_results.append(f"[{data.get('tool', '?')}] {summary[:500]}")
            elif data.get("type") == "agent_step":
                rounds = max(rounds, int(data.get("round") or 0))
            elif data.get("type") == "metrics":
                # fix #3: the leaf emits a final {"type":"metrics","data":{...}}
                # with real token usage (agent_loop._compute_final_metrics:
                # total_tokens = input_tokens + output_tokens). Tally it for the
                # crew token_budget. Prefer total_tokens; fall back to the parts.
                _m = data.get("data") or {}
                _tok = _m.get("total_tokens")
                if _tok is None:
                    _tok = (_m.get("input_tokens") or 0) + (_m.get("output_tokens") or 0)
                try:
                    worker_tokens += int(_tok or 0)
                except (TypeError, ValueError):
                    pass

            # Re-tag: inject agent_id/role and map child event -> crew_* type.
            data["agent_id"] = agent_id
            data["role"] = role_name
            t = data.get("type")
            if "delta" in data and t is None:
                data["type"] = "crew_agent_output"
            elif t in ("tool_start", "tool_output", "tool_progress", "agent_step",
                       "web_sources", "budget_exceeded", "metrics"):
                # crew_approval_request already comes through the emit callback;
                # tool lifecycle / step events become crew_agent_step.
                if t in ("tool_start", "tool_output", "tool_progress"):
                    data["type"] = "crew_agent_step"
                # leave agent_step/metrics/etc typed but re-tagged
            elif t == "crew_approval_request":
                # Belt-and-suspenders: if a leaf ever yields this directly,
                # keep it as-is (already carries agent_id).
                pass
            yield _sse(data, owner)
    finally:
        tool_execution.reset_crew_gate(crew_gate_token)
        tool_execution.reset_crew_mode(crew_mode_token)

    # Grace summarize if the worker produced no final text (scheduler recipe).
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

    # Persist worker result to the blackboard + DB; emit the worker output card.
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

    # Final item: the worker-result sentinel, yielded LAST (after every SSE
    # string) so the dispatch loop collects it for synthesis without racing the
    # tail of the stream. The driver re-puts each yielded item onto emit_q in
    # order, so this lands after the crew_agent_output above.
    yield ("__worker_result__", agent_id, role_name, result_text, rounds, worker_tokens)


# ---------------------------------------------------------------------------
# Public surface — run_crew
# ---------------------------------------------------------------------------


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
    """Run an Argo crew quest end-to-end, yielding the MULTIPLEXED parent SSE
    stream. Hand the whole generator to ``agent_runs.start(crew_run_id, gen)``.

    Topology: Athena plan -> sequential (concurrency=1) worker dispatch ->
    Athena synthesis. READ-ONLY by default (no gate ever fires); write_mode=True
    widens the allowlist and wires the Oracle's-seal gate.

    `owner` (None or "") is REJECTED before ANY model resolution or DB write.
    """
    # ---- HARD security gate: reject falsy owner FIRST. ----
    if not owner:
        raise ValueError("run_crew requires a non-empty owner (cross-owner secret-leak guard)")
    # ---- Recursion cap: a crew worker must never spawn another crew. ----
    if recursion_depth and recursion_depth > 1:
        raise ValueError("crew recursion is not permitted (depth > 1)")

    budget = budget or CrewBudget()
    crew_run_id = crew_run_id or uuid.uuid4().hex
    roles = list(roles or [])
    if not roles:
        roles = [{"name": "Argonaut-1", "role_kind": "worker"}]
    # Cap the number of workers by max_agents (reserve none for Athena — she is
    # the planner/synthesizer, not a dispatched worker).
    roles = roles[: max(1, budget.max_agents)]

    bus = HermesBus(crew_run_id, owner)
    mnemosyne = Mnemosyne(crew_run_id, owner)

    # Multiplexer queue: worker generators push parent SSE strings (and gate
    # emits + worker-result sentinels) here; the main loop drains it so a gate
    # request surfaced from inside execute_tool_block reaches the stream.
    emit_q: asyncio.Queue = asyncio.Queue()

    blackboard_dir = None
    started = time.monotonic()
    deadline = started + max(1, budget.wall_clock_s)
    worker_outputs: List[Tuple[str, str]] = []
    total_rounds = 0
    total_tokens = 0            # fix #3: run-level token tally for token_budget
    final_status = "running"
    final_error: Optional[str] = None
    final_result: Optional[str] = None

    try:
        # 1. Confine the blackboard + persist the run row.
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

        # 2. Plan (Athena).
        max_subtasks = min(budget.max_agents, len(roles))
        plan = await _athena_plan(prompt, roles, owner, max_subtasks)
        _update_crew_run(crew_run_id, plan=json.dumps(plan, default=str))
        # Seed the ledger (Athena = sole writer).
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

        # 3. Sequential dispatch (concurrency=1) with budget + deadline.
        sem = asyncio.Semaphore(budget.concurrency)
        last_ledger_hash = None
        stall_count = 0

        for i, sub in enumerate(plan):
            # ---- between-dispatch budget checks ----
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
            # fix #3: token_budget enforcement (0 = unlimited). Once the running
            # tally from finished workers exceeds the budget, stop dispatching
            # further workers and mark the run budget-exceeded.
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

            # Mark the ledger task in-flight (Athena sole writer).
            for t in ledger["tasks"]:
                if t.get("id") == f"t{i}":
                    t["status"] = "doing"
                    t["updatedAt"] = time.time()
            ledger["updatedAt"] = time.time()
            await mnemosyne.write_ledger(ledger)

            # ---- dispatch ONE worker under the semaphore, with the overall
            #      deadline as a hard cancel that kills the in-flight child ----
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
                    # Drain the multiplexer queue until this worker signals done,
                    # subject to the overall wall-clock deadline.
                    while True:
                        try:
                            item = await asyncio.wait_for(emit_q.get(), timeout=remaining)
                        except asyncio.TimeoutError:
                            # Deadline hit mid-worker: cancel the in-flight child
                            # (kills its subprocess via the loop's CancelledError
                            # finally) and stop the run.
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
                            # Persist the running token total on the CrewRun so a
                            # reconnect / the budget check below both see it.
                            _update_crew_run(crew_run_id, tokens_used=total_tokens)
                            continue
                        # Normal SSE string from the worker / gate emit.
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

            # Mark the ledger task done.
            for t in ledger["tasks"]:
                if t.get("id") == f"t{i}":
                    t["status"] = "done"
                    t["updatedAt"] = time.time()
            ledger["updatedAt"] = time.time()
            await mnemosyne.write_ledger(ledger)

        # 4. Synthesize (Athena).
        final_result = await _athena_synthesize(prompt, worker_outputs, owner)
        await mnemosyne.append_board(f"Synthesis:\n{final_result}")
        if final_status == "running":
            final_status = "success"
        _update_crew_run(
            crew_run_id, status=final_status, finished_at=_now(),
            result=(final_result or "")[:50000],
            tokens_used=total_tokens or None,   # fix #3: final token tally
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
        # ALWAYS: expire every pending gate for this run (wake abandoned
        # waiters), kill any stray bg jobs, GC the Hermes bus. This runs on
        # success, error, CancelledError, AND GeneratorExit (agent_runs.stop()).
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
