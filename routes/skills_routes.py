# routes/skills_routes.py
"""REST API for the Skills system.

The on-disk format is SKILL.md (frontmatter + structured body) under
`data/skills/<category>/<name>/`. Old shape (`title`, `problem`, `solution`,
`steps`) still accepted on input — they're translated to the new fields
(`description`, `when_to_use`, `body_extra`, `procedure`).
"""

import logging
from typing import List, Optional

import httpx

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from src import skill_audit

from services.memory.skills import SkillsManager
from src.auth_helpers import get_current_user
from src.background_tasks import spawn as _spawn_background
from core.middleware import require_admin

logger = logging.getLogger(__name__)


class SkillAddRequest(BaseModel):
    # New schema (preferred)
    name: Optional[str] = Field(None, max_length=80)
    description: Optional[str] = Field(None, max_length=200)
    category: str = Field("general", max_length=40)
    tags: List[str] = Field(default_factory=list)
    platforms: List[str] = Field(default_factory=list)
    requires_toolsets: List[str] = Field(default_factory=list)
    fallback_for_toolsets: List[str] = Field(default_factory=list)
    when_to_use: Optional[str] = Field(None, max_length=2000)
    procedure: List[str] = Field(default_factory=list)
    pitfalls: List[str] = Field(default_factory=list)
    verification: List[str] = Field(default_factory=list)
    status: str = "draft"
    version: str = "1.0.0"
    confidence: float = 0.8
    # Manual adds via this endpoint are human-authored → "user", which exempts
    # them from auto-dedup and cap-eviction in add_skill. (The agent's own
    # skill writes go through do_manage_skills with source="learned".)
    source: str = "user"
    teacher_model: Optional[str] = None
    session_id: Optional[str] = None

    # Old schema (back-compat)
    title: Optional[str] = Field(None, max_length=200)
    problem: Optional[str] = Field(None, max_length=2000)
    solution: Optional[str] = Field(None, max_length=5000)
    steps: List[str] = Field(default_factory=list)


class SkillImportUrlRequest(BaseModel):
    url: str = Field(..., min_length=8, max_length=2000)


class SkillUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    tags: Optional[List[str]] = None
    platforms: Optional[List[str]] = None
    requires_toolsets: Optional[List[str]] = None
    fallback_for_toolsets: Optional[List[str]] = None
    when_to_use: Optional[str] = None
    procedure: Optional[List[str]] = None
    pitfalls: Optional[List[str]] = None
    verification: Optional[List[str]] = None
    status: Optional[str] = None
    version: Optional[str] = None
    confidence: Optional[float] = None
    body_extra: Optional[str] = None
    # Old shape
    title: Optional[str] = None
    problem: Optional[str] = None
    solution: Optional[str] = None
    steps: Optional[List[str]] = None




def setup_skills_routes(skills_manager: SkillsManager) -> APIRouter:
    router = APIRouter(prefix="/api/skills", tags=["skills"])

    def _owner(request: Request) -> Optional[str]:
        return get_current_user(request)

    def _verify_owner(skill: dict, user: Optional[str]):
        if user is None:
            return
        # SECURITY: strict check — previously `sk_owner and sk_owner != user`
        # let any user mutate/read a skill that happened to have no owner
        # field (legacy or un-stamped writes), since the truthiness guard
        # short-circuited the comparison. Treat missing owner as not-owned.
        if skill.get("owner") != user:
            raise HTTPException(404, "Skill not found")

    def _fire_skill_added(user: Optional[str]):
        try:
            from src.event_bus import fire_event
            fire_event("skill_added", user)
        except Exception:
            logger.debug("skill_added event dispatch failed", exc_info=True)

    @router.get("")
    async def list_skills(request: Request):
        user = _owner(request)
        skills = skills_manager.load(owner=user)
        return {"skills": skills, "count": len(skills)}

    @router.get("/index")
    async def get_index(request: Request):
        """The lightweight `[{name, description, category}]` list that the
        agent's system prompt sees. Useful for the UI's "what does the model
        actually have access to?" view."""
        user = _owner(request)
        idx = skills_manager.index_for(owner=user)
        return {"index": idx, "count": len(idx)}

    @router.get("/slash-catalog")
    async def get_slash_catalog(request: Request):
        """Return skills that are available as slash commands.

        Mirrors the agent prompt's published-skill index so the UI never offers
        a slash command the model would not normally be allowed to discover.
        """
        user = _owner(request)
        all_skills = {s.get("name"): s for s in skills_manager.load(owner=user)}
        entries = []
        for s in skills_manager.index_for(owner=user):
            name = (s.get("name") or "").strip()
            if not name:
                continue
            full = all_skills.get(name) or {}
            category = (s.get("category") or full.get("category") or "general").strip() or "general"
            entries.append({
                "type": "skill",
                "token": f"/{name}",
                "name": name,
                "category": f"Skills / {category}",
                "help": s.get("description") or full.get("description") or "",
                "usage": f"/{name} <request>",
                "uses": int(full.get("uses") or 0),
                "last_used": full.get("last_used"),
            })
        entries.sort(key=lambda row: row["name"])
        return {"skills": entries, "count": len(entries)}

    @router.get("/builtin")
    async def list_builtin_skills(request: Request):
        """Read-only list of the agent's built-in tool capabilities (research,
        sessions, tasks, email, etc.) — the things it natively knows how to do.
        Surfaced so the Skills tab can show them in a separate "Built-in"
        section alongside the user's learned SKILL.md skills. Sourced from
        agent_loop.TOOL_SECTIONS (the same descriptions the model is given)."""
        import re

        def _clean(raw: str) -> str:
            s = raw or ""
            s = re.sub(r"```.*?```", "", s, flags=re.S)   # drop code fences (incl. inline ```name```)
            s = re.sub(r"\s+", " ", s).strip()
            s = re.sub(r"^[-–—:\s]+", "", s)              # drop leftover "- — " / ": " bullet prefix
            return s[:240]

        try:
            from src.agent_loop import TOOL_SECTIONS, get_builtin_overrides
        except Exception as e:
            return {"builtin": [], "count": 0, "error": str(e)}

        overrides = get_builtin_overrides()
        out = []
        for key, raw in TOOL_SECTIONS.items():
            names = key if isinstance(key, tuple) else (key,)
            for nm in names:
                if isinstance(nm, str):
                    overridden = nm in overrides
                    eff = overrides.get(nm, raw)
                    out.append({
                        "name": nm,
                        "description": _clean(eff),
                        "is_overridden": overridden,
                    })
        out.sort(key=lambda x: x["name"])
        return {"builtin": out, "count": len(out)}

    @router.get("/builtin/{name}")
    async def get_builtin_skill(name: str, request: Request):
        """Full text of a built-in tool's instruction block — the override
        if one is set, plus the shipped default (for the revert button)."""
        try:
            from src.agent_loop import TOOL_SECTIONS, get_builtin_overrides
        except Exception as e:
            raise HTTPException(500, str(e))
        default = None
        for key, raw in TOOL_SECTIONS.items():
            names = key if isinstance(key, tuple) else (key,)
            if name in names:
                default = raw
                break
        if default is None:
            raise HTTPException(404, f"No built-in tool named {name!r}")
        overrides = get_builtin_overrides()
        return {
            "name": name,
            "text": overrides.get(name, default),
            "default": default,
            "is_overridden": name in overrides,
        }

    @router.put("/builtin/{name}")
    async def set_builtin_override(name: str, request: Request):
        """Save a user override for a built-in tool's instruction block.
        WARNING surfaced in the UI — this changes how the assistant is
        told to use a native tool."""
        require_admin(request)
        from src.agent_loop import TOOL_SECTIONS
        valid = set()
        for key in TOOL_SECTIONS:
            valid.update(key if isinstance(key, tuple) else (key,))
        if name not in valid:
            raise HTTPException(404, f"No built-in tool named {name!r}")
        body = await request.json()
        text = (body or {}).get("text", "")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(400, "text is required")
        from src.settings import save_settings, load_settings
        settings = load_settings()
        ov = settings.get("builtin_tool_overrides")
        if not isinstance(ov, dict):
            ov = {}
        ov[name] = text
        settings["builtin_tool_overrides"] = ov
        save_settings(settings)
        return {"ok": True, "name": name, "is_overridden": True}

    @router.delete("/builtin/{name}")
    async def reset_builtin_override(name: str, request: Request):
        """Revert a built-in tool to its shipped instruction block."""
        require_admin(request)
        from src.settings import load_settings, save_settings
        settings = load_settings()
        ov = settings.get("builtin_tool_overrides")
        if isinstance(ov, dict) and name in ov:
            del ov[name]
            settings["builtin_tool_overrides"] = ov
            save_settings(settings)
        return {"ok": True, "name": name, "is_overridden": False}

    @router.post("/import-from-url")
    async def import_skill_from_url(request: Request, body: SkillImportUrlRequest):
        """Install a SKILL.md bundle from a public GitHub URL (skills.sh links supported)."""
        require_admin(request)
        user = _owner(request)
        from services.memory.skill_importer import (
            SkillImportError,
            fetch_skill_bundle,
        )

        try:
            files, _src = fetch_skill_bundle(body.url.strip())
            entry = skills_manager.import_bundle_from_files(
                files,
                owner=user,
                source_url=body.url.strip(),
            )
        except SkillImportError as e:
            raise HTTPException(400, str(e)) from e
        except httpx.HTTPError as e:
            logger.warning("skill import fetch failed: %s", e)
            detail = str(e).strip() or "Could not download skill from URL"
            raise HTTPException(502, detail) from e
        except Exception as e:
            logger.error("skill import failed: %s", e)
            raise HTTPException(500, "Skill import failed") from e

        _fire_skill_added(user)
        return {"ok": True, "skill": entry, "files": len(files)}

    @router.post("/add")
    async def add_skill(request: Request, body: SkillAddRequest):
        user = _owner(request)
        entry = skills_manager.add_skill(
            # New shape
            name=body.name,
            description=body.description,
            category=body.category,
            tags=body.tags,
            platforms=body.platforms,
            requires_toolsets=body.requires_toolsets,
            fallback_for_toolsets=body.fallback_for_toolsets,
            when_to_use=body.when_to_use,
            procedure=body.procedure,
            pitfalls=body.pitfalls,
            verification=body.verification,
            status=body.status,
            version=body.version,
            confidence=body.confidence,
            source=body.source,
            teacher_model=body.teacher_model,
            session_id=body.session_id,
            owner=user,
            # Old shape (manager translates)
            title=body.title or "",
            problem=body.problem or "",
            solution=body.solution or "",
            steps=body.steps,
        )
        if not entry.get("_deduped"):
            _fire_skill_added(user)
        return {"ok": True, "deduped": bool(entry.get("_deduped")), "skill": entry}

    @router.post("/{skill_id}/invoke")
    async def invoke_skill(request: Request, skill_id: str):
        """Build a skill-pinned prompt for slash-command invocation.

        This is intentionally server-side so availability, ownership, and usage
        accounting use the same rules as the SkillsManager.
        """
        user = _owner(request)
        try:
            body = await request.json()
        except Exception:
            body = {}
        request_text = (body.get("request") or "").strip() if isinstance(body, dict) else ""

        invokable = {
            s.get("name"): s for s in skills_manager.index_for(owner=user)
            if (s.get("name") or "").strip()
        }
        match = invokable.get(skill_id)
        if not match:
            raise HTTPException(404, "Skill is not available for slash invocation")

        name = match.get("name")
        md = skills_manager.read_skill_md(name, owner=user)
        if md is None:
            raise HTTPException(404, "Skill source unavailable")

        skills_manager.record_use(name, owner=user)
        message = (
            "Apply the skill below to my request, following its Procedure / Pitfalls / Verification.\n\n"
            f"--- BEGIN SKILL ---\n{md}\n--- END SKILL ---\n\n"
            + (f"Request: {request_text}" if request_text else "Request: (use the skill as appropriate)")
        )
        return {
            "ok": True,
            "type": "skill",
            "name": name,
            "command": f"/{name}",
            "message": message,
        }

    @router.get("/{skill_id}")
    async def get_skill(request: Request, skill_id: str):
        user = _owner(request)
        skills = skills_manager.load(owner=user)
        for sk in skills:
            if sk.get("name") == skill_id or sk.get("id") == skill_id:
                return sk
        raise HTTPException(404, "Skill not found")

    @router.get("/{skill_id}/markdown")
    async def get_skill_markdown(request: Request, skill_id: str):
        """Return the raw SKILL.md text — used by the slash-invocation flow
        and the editor's 'view source' affordance."""
        user = _owner(request)
        skills = skills_manager.load(owner=user)
        match = next((s for s in skills if s.get("name") == skill_id or s.get("id") == skill_id), None)
        if not match:
            raise HTTPException(404, "Skill not found")
        _verify_owner(match, user)
        md = skills_manager.read_skill_md(match.get("name"), owner=user)
        if md is None:
            raise HTTPException(404, "Skill source unavailable (legacy entry?)")
        return {"name": match.get("name"), "markdown": md}

    @router.post("/{skill_id}/test")
    async def test_skill(request: Request, skill_id: str):
        """Kick off a background skill test (agent run + LLM judge). Returns
        immediately; the run executes server-side so it survives the modal being
        closed. Poll GET /{skill_id}/test-status for progress + verdict.
        On completion it records the verdict and nudges the skill's confidence
        to match (pass→0.95, needs_work→0.6, fail→0.4; inconclusive/unknown leave
        it untouched). It never changes the skill's published/draft STATUS."""
        import time as _time
        from src.endpoint_resolver import resolve_endpoint

        user = _owner(request)
        body = await request.json()
        task = (body.get("task") or "").strip()

        skills = skills_manager.load(owner=user)
        match = next((s for s in skills if s.get("name") == skill_id or s.get("id") == skill_id), None)
        if not match:
            raise HTTPException(404, "Skill not found")
        _verify_owner(match, user)
        name = match.get("name")
        md = skills_manager.read_skill_md(name, owner=user) or ""

        if not task:
            task = skill_audit._skill_test_task(match)

        # Prefer the configured DEFAULT (→ Utility) model — not the current chat
        # session's model. Fall back to the caller's session model only if unset.
        url, model, headers = resolve_endpoint("default")
        if not url or not model:
            url = url or ((body.get("endpoint_url") or "").strip() or None)
            model = model or ((body.get("model") or "").strip() or None)
            if headers is None and isinstance(body.get("headers"), dict):
                headers = body.get("headers")
        if not url or not model:
            raise HTTPException(400, "No model configured — set a Default or Utility model in Settings.")

        # Normalize against the endpoint's served models (avoids 404 model drift).
        try:
            from src.llm_core import list_model_ids
            _avail = list_model_ids(url, headers=headers)
            if _avail and model not in _avail:
                import os as _os
                _base = _os.path.basename((model or "").rstrip("/"))
                _match = next((a for a in _avail if _os.path.basename(a.rstrip("/")) == _base), None)
                model = _match or _avail[0]
        except Exception as _e:
            logger.warning(f"Skill-test model resolve failed: {_e}")

        key = (user or "", name)
        skill_audit._skill_test_jobs[key] = {
            "status": "running",
            "task": task,
            "model": model,
            "skill": name,
            "started": _time.time(),
            "log": [{"type": "skill_test_start", "task": task, "skill": name, "model": model}],
            "verdict": None,
        }
        _spawn_background(skill_audit._run_skill_test_job(key, name, md, task, url, model, headers, user, skills_manager),
                          name="skill-test-job")
        return {"ok": True, "status": "running", "skill": name, "model": model}

    @router.get("/{skill_id}/test-status")
    async def test_skill_status(request: Request, skill_id: str):
        """Current background-test state for a skill (status / log / verdict)."""
        user = _owner(request)
        skills = skills_manager.load(owner=user)
        match = next((s for s in skills if s.get("name") == skill_id or s.get("id") == skill_id), None)
        name = (match or {}).get("name", skill_id)
        job = skill_audit._skill_test_jobs.get((user or "", name))
        if not job:
            return {"status": "none"}
        return {
            "status": job["status"],
            "task": job.get("task"),
            "model": job.get("model"),
            "log": job.get("log", []),
            "verdict": job.get("verdict"),
        }

    @router.post("/audit-all")
    async def audit_all_skills(request: Request):
        """Kick off a background audit of skills: each is tested + judged; if it
        needs work the model self-edits and retries; if a teacher model is
        configured it escalates; a skill that still fails is demoted to draft
        (never deleted). Poll GET /audit-status. Body:
        {scope: 'drafts'|'unchecked'|'all', names?: [...], skip_audited?: bool}. Default 'all'
        means every visible skill, including already-published skills, so audit
        can publish or demote according to the confidence threshold."""
        import time as _time

        user = _owner(request)
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        scope = (body.get("scope") or "all").lower()
        requested_names = body.get("names")
        skip_audited = bool(body.get("skip_audited"))

        key = (user or "",)
        existing = skill_audit._skill_audit_jobs.get(key)
        if existing and existing.get("status") == "running":
            return {
                "ok": True, "status": "running", "total": existing.get("total", 0),
                "done": existing.get("done", 0), "model": existing.get("model"),
            }

        # Worker model (Default, normalized) + optional teacher — shared resolver.
        try:
            url, model, headers, teacher = skill_audit._resolve_audit_models()
        except ValueError as e:
            raise HTTPException(400, str(e))

        skills = skills_manager.load(owner=user)
        by_name = {s.get("name"): s for s in skills if s.get("name")}
        if isinstance(requested_names, list):
            names = []
            seen = set()
            for raw in requested_names:
                nm = str(raw or "").strip()
                if not nm or nm in seen or nm not in by_name:
                    continue
                if scope not in ("all", "selected") and (by_name[nm].get("status") or "draft") == "published":
                    continue
                if skip_audited and by_name[nm].get("audit_verdict"):
                    continue
                names.append(nm)
                seen.add(nm)
            scope = "selected" if requested_names else scope
        elif scope == "all":
            names = [
                s.get("name") for s in skills
                if s.get("name") and (not skip_audited or not s.get("audit_verdict"))
            ]
        else:
            scope = "unchecked" if scope == "drafts" else scope
            names = [
                s.get("name") for s in skills
                if s.get("name")
                and (s.get("status") or "draft") != "published"
                and not s.get("audit_verdict")
            ]
        if not names:
            return {"ok": True, "status": "done", "total": 0, "results": [], "log": ["No skills to audit."]}

        skill_audit._skill_audit_jobs[key] = {
            "status": "running", "scope": scope, "model": model,
            "teacher": teacher[1] if teacher else None,
            "total": len(names), "done": 0, "current": None,
            "results": [], "log": [f"Auditing {len(names)} skill(s) with {model}" + (f"; teacher {teacher[1]}" if teacher else "")],
            "started": _time.time(), "cancel": False,
        }
        task = _spawn_background(skill_audit._run_audit_all_job(key, skills_manager, names, url, model, headers, teacher, user),
                                 name="skill-audit-job")
        skill_audit._skill_audit_jobs[key]["task"] = task
        return {"ok": True, "status": "running", "total": len(names), "model": model}

    @router.get("/audit-all/status")
    async def audit_status(request: Request):
        user = _owner(request)
        job = skill_audit._skill_audit_jobs.get((user or "",))
        if not job:
            return {"status": "none"}
        return {
            "status": job["status"], "scope": job.get("scope"),
            "total": job.get("total", 0), "done": job.get("done", 0),
            "current": job.get("current"), "model": job.get("model"), "teacher": job.get("teacher"),
            "results": job.get("results", []), "log": job.get("log", []),
            "started": job.get("started"), "finished": job.get("finished"),
        }

    @router.post("/audit-all/cancel")
    async def audit_cancel(request: Request):
        user = _owner(request)
        job = skill_audit._skill_audit_jobs.get((user or "",))
        if job:
            job["cancel"] = True
            job["status"] = "cancelled"
            job["current"] = None
            task = job.get("task")
            if task and not task.done():
                task.cancel()
        return {"ok": True, "status": "cancelled" if job else "none"}

    @router.post("/{skill_id}/markdown")
    async def save_skill_markdown(request: Request, skill_id: str):
        """Replace SKILL.md with new raw content. Parses + validates first."""
        from services.memory.skill_format import Skill
        user = _owner(request)
        body = await request.json()
        new_content = body.get("markdown")
        if not isinstance(new_content, str) or not new_content.strip():
            raise HTTPException(400, "markdown is required")
        skills = skills_manager.load(owner=user)
        match = next((s for s in skills if s.get("name") == skill_id or s.get("id") == skill_id), None)
        if not match:
            raise HTTPException(404, "Skill not found")
        _verify_owner(match, user)
        try:
            sk = Skill.from_markdown(new_content)
        except Exception as e:
            raise HTTPException(400, f"Could not parse SKILL.md: {e}")
        # Never rename on save: a changed `name` in the markdown would move
        # the skill dir (update_skill) and orphan the original id, so a later
        # delete 404s (#1333). Pin to the stored name, like skill_audit._apply_skill_md.
        sk.name = match.get("name")
        if not sk.owner:
            sk.owner = match.get("owner") or user
        ok = skills_manager.update_skill(match.get("name"), {
            "name": sk.name,
            "description": sk.description,
            "version": sk.version,
            "category": sk.category,
            "tags": sk.tags,
            "platforms": sk.platforms,
            "requires_toolsets": sk.requires_toolsets,
            "fallback_for_toolsets": sk.fallback_for_toolsets,
            "status": sk.status,
            "confidence": sk.confidence,
            "source": sk.source,
            "teacher_model": sk.teacher_model,
            "owner": sk.owner,
            "when_to_use": sk.when_to_use,
            "procedure": sk.procedure,
            "pitfalls": sk.pitfalls,
            "verification": sk.verification,
            "body_extra": sk.body_extra,
        }, owner=user)
        if not ok:
            raise HTTPException(500, "Update failed")
        # Manual markdown edits can create or substantially rewrite a draft
        # skill without going through /add. Treat unaudited saves as new audit
        # candidates so the event-driven Skills Audit pipeline still runs.
        if not match.get("audit_verdict"):
            _fire_skill_added(user)
        return {"ok": True, "name": sk.name}

    @router.put("/{skill_id}")
    async def update_skill(request: Request, skill_id: str, body: SkillUpdateRequest):
        user = _owner(request)
        skills = skills_manager.load(owner=user)
        match = next((s for s in skills if s.get("name") == skill_id or s.get("id") == skill_id), None)
        if not match:
            raise HTTPException(404, "Skill not found")
        _verify_owner(match, user)

        updates = body.dict(exclude_none=True)
        if not updates:
            return {"ok": True}
        ok = skills_manager.update_skill(match.get("name"), updates, owner=user)
        if not ok:
            raise HTTPException(404, "Skill not found")
        if not match.get("audit_verdict"):
            _fire_skill_added(user)
        return {"ok": True}

    @router.delete("/{skill_id}")
    async def delete_skill(request: Request, skill_id: str):
        user = _owner(request)
        skills = skills_manager.load(owner=user)
        match = next((s for s in skills if s.get("name") == skill_id or s.get("id") == skill_id), None)
        if not match:
            raise HTTPException(404, "Skill not found")
        _verify_owner(match, user)
        ok = skills_manager.delete_skill(match.get("name"), owner=user)
        if not ok:
            raise HTTPException(404, "Skill not found")
        return {"ok": True}

    @router.post("/search")
    async def search_skills(request: Request):
        body = await request.json()
        query = body.get("query", "")
        if not query.strip():
            raise HTTPException(400, "query is required")
        user = _owner(request)
        skills = skills_manager.load(owner=user)
        results = skills_manager.get_relevant_skills(query, skills, max_items=10)
        return {"skills": results, "query": query, "count": len(results)}

    return router
