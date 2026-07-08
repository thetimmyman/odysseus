"""Phase 6 (spec Section 19) knowledge base: evidence-required creation, the
full lifecycle transition matrix (legal transitions succeed, illegal ones
409), validated-only advisory-labeled retrieval, draft_from_run template
assembly, admin-cookie route gating, and the ADVISORY-ONLY invariant —
knowledge is context, never policy: nothing in src/ may consume the retrieval
surface for gating/veto decisions.

Route tests reuse test_routing_harness_routes' stub-auth pattern (bare
FastAPI app + in-memory sqlite + header-stamped request.state); function
tests use the in-memory StaticPool session pattern from
test_routing_governance."""
import json
import os
import re
import sys
import uuid
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy.orm import sessionmaker
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _auth_disabled_env(monkeypatch):
    # Scoped per-test (see test_routing_harness_routes for why a bare
    # module-level env write would leak into the whole suite).
    monkeypatch.setenv("AUTH_ENABLED", "false")


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import core.database as cdb  # noqa: E402
import routes.routing_harness_routes as rh  # noqa: E402
from src.routing_knowledge import (  # noqa: E402
    ADVISORY_NOTE,
    KnowledgeTransitionError,
    create_draft,
    draft_from_run,
    expire_entry,
    reject_entry,
    retrieve_validated,
    supersede_entry,
    validate_entry,
)

ADMIN = {"x-test-user": "admin"}
NON_ADMIN = {"x-test-user": "bob"}

EVIDENCE = [{"type": "run", "id": "run-1"}]


def _db():
    engine = sqlalchemy.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    cdb.Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False)()


def _draft(db, **kw):
    kw.setdefault("title", "t")
    kw.setdefault("body", "b")
    kw.setdefault("evidence", list(EVIDENCE))
    return create_draft(db, **kw)


# ---------- evidence-required creation ----------
def test_create_requires_nonempty_evidence():
    db = _db()
    for bad in (None, [], "not-a-list", {}):
        with pytest.raises(ValueError):
            create_draft(db, title="t", body="b", evidence=bad)
    assert db.query(cdb.KnowledgeBaseEntry).count() == 0

    row = _draft(db)
    assert row.status == "draft"
    assert json.loads(row.evidence) == EVIDENCE
    assert row.created_by == "human"
    assert "created as draft by human" in row.audit_log


def test_create_requires_title_and_body():
    db = _db()
    with pytest.raises(ValueError):
        create_draft(db, title="  ", body="b", evidence=list(EVIDENCE))
    with pytest.raises(ValueError):
        create_draft(db, title="t", body="", evidence=list(EVIDENCE))


# ---------- lifecycle: legal transitions ----------
def test_draft_validate_records_actor():
    db = _db()
    row = validate_entry(db, _draft(db).id, "tim")
    assert row.status == "validated"
    assert row.validated_by == "tim"
    assert row.validated_at is not None
    assert "validated by tim" in row.audit_log


def test_draft_reject_records_actor():
    db = _db()
    row = reject_entry(db, _draft(db).id, "tim")
    assert row.status == "rejected"
    assert "rejected by tim" in row.audit_log


def test_validated_supersede_links_replacement():
    db = _db()
    old = validate_entry(db, _draft(db).id, "tim")
    new = _draft(db, title="replacement")
    row = supersede_entry(db, old.id, "tim", new.id)
    assert row.status == "superseded"
    assert row.superseded_by_id == new.id
    assert f"superseded by entry {new.id}" in row.audit_log


def test_validated_expire_requires_rationale():
    db = _db()
    row = validate_entry(db, _draft(db).id, "tim")
    with pytest.raises(ValueError):
        expire_entry(db, row.id, "tim", "   ")
    row = expire_entry(db, row.id, "tim", "substantial code change in area X")
    assert row.status == "expired"
    assert row.expires_rationale == "substantial code change in area X"
    assert row.expired_at is not None


def test_expired_revalidate_only_with_explicit_flag():
    """Judgment call from the spec: expired (unlike rejected/superseded) may
    return to validated, but ONLY via an explicit human flag — never as a
    default validate."""
    db = _db()
    row = validate_entry(db, _draft(db).id, "tim")
    row = expire_entry(db, row.id, "tim", "area rewritten")
    # Without the flag: illegal.
    with pytest.raises(KnowledgeTransitionError):
        validate_entry(db, row.id, "tim")
    row = validate_entry(db, row.id, "tim", revalidate_expired=True)
    assert row.status == "validated"
    assert row.expired_at is None and row.expires_rationale is None
    # The expiry survives in the audit trail even though the columns cleared.
    assert "area rewritten" in row.audit_log
    assert "re-validated from expired by tim" in row.audit_log


# ---------- lifecycle: illegal transitions ----------
def test_illegal_transition_matrix():
    """Every non-legal (status, action) pair raises KnowledgeTransitionError.
    Legal set: draft->validate, draft->reject, validated->supersede,
    validated->expire, expired->validate(flag)."""
    legal = {
        ("draft", "validate"), ("draft", "reject"),
        ("validated", "supersede"), ("validated", "expire"),
        ("expired", "revalidate"),
    }
    actions = {
        "validate": lambda db, eid: validate_entry(db, eid, "tim"),
        "revalidate": lambda db, eid: validate_entry(db, eid, "tim", revalidate_expired=True),
        "reject": lambda db, eid: reject_entry(db, eid, "tim"),
        "supersede": lambda db, eid: supersede_entry(db, eid, "tim", _draft(db).id),
        "expire": lambda db, eid: expire_entry(db, eid, "tim", "reason"),
    }

    def _in_status(db, status):
        row = _draft(db)
        if status == "draft":
            return row
        if status == "rejected":
            return reject_entry(db, row.id, "setup")
        row = validate_entry(db, row.id, "setup")
        if status == "validated":
            return row
        if status == "expired":
            return expire_entry(db, row.id, "setup", "setup rationale")
        if status == "superseded":
            return supersede_entry(db, row.id, "setup", _draft(db).id)
        raise AssertionError(status)

    checked = 0
    for status in ("draft", "validated", "rejected", "superseded", "expired"):
        for action, fn in actions.items():
            # revalidate on draft is just validate-with-flag: legal on draft.
            key = (status, "validate" if (status == "draft" and action == "revalidate") else action)
            if key in legal:
                continue
            db = _db()
            row = _in_status(db, status)
            with pytest.raises(KnowledgeTransitionError):
                fn(db, row.id)
            db.refresh(row)
            assert row.status == status, f"{status}/{action} mutated the row"
            checked += 1
    assert checked >= 18  # 25 pairs minus the legal ones


def test_supersede_replacement_must_exist_and_differ():
    db = _db()
    row = validate_entry(db, _draft(db).id, "tim")
    with pytest.raises(ValueError):
        supersede_entry(db, row.id, "tim", "no-such-entry")
    with pytest.raises(ValueError):
        supersede_entry(db, row.id, "tim", row.id)
    with pytest.raises(ValueError):
        supersede_entry(db, row.id, "tim", "")
    db.refresh(row)
    assert row.status == "validated"


# ---------- retrieval: validated-only, advisory-labeled ----------
def test_retrieve_validated_only_and_advisory_labeled():
    db = _db()
    validated = validate_entry(db, _draft(db, title="keep", category="bug_debug").id, "tim")
    _draft(db, title="still-draft")                       # draft: excluded
    reject_entry(db, _draft(db).id, "tim")                # rejected: excluded
    expired = validate_entry(db, _draft(db).id, "tim")
    expire_entry(db, expired.id, "tim", "gone")           # expired: excluded
    sup = validate_entry(db, _draft(db).id, "tim")
    supersede_entry(db, sup.id, "tim", validated.id)      # superseded: excluded

    items = retrieve_validated(db)
    assert [it["entry"]["id"] for it in items] == [validated.id]
    for it in items:
        assert it["advisory"] is True
        assert it["note"] == ADVISORY_NOTE
        # No gate/veto/block lever anywhere in the wrapper.
        assert not any(k in it for k in ("gate", "veto", "block", "blocking", "required"))


def test_retrieve_filters_category_tag_task_type():
    db = _db()
    task = cdb.RoutingTask(id="t-kb", title="t", objective="o",
                           task_type="bug_debug", repo_path=".")
    db.add(task)
    db.commit()
    a = validate_entry(db, _draft(db, title="a", category="bug_debug",
                                  tags=["retry"], source_task_id="t-kb").id, "tim")
    validate_entry(db, _draft(db, title="b", category="feature_plan").id, "tim")

    assert [i["entry"]["id"] for i in retrieve_validated(db, category="bug_debug")] == [a.id]
    assert [i["entry"]["id"] for i in retrieve_validated(db, tag="retry")] == [a.id]
    assert [i["entry"]["id"] for i in retrieve_validated(db, task_type="bug_debug")] == [a.id]
    assert retrieve_validated(db, tag="nope") == []
    assert retrieve_validated(db, task_type="ci_triage") == []


# ---------- draft_from_run ----------
def _seed_model_run(db, *, scores=None, artifacts=None, with_manifest=True,
                    lesson_history=None):
    db.add(cdb.RoutingModelProfile(
        id="p-kb", model="test/lesson-model", roles=json.dumps(["scout"]),
        context_window=32768, is_free=True, enabled=True))
    db.commit()
    db.add(cdb.RoutingTask(id="t-src", title="Fix the flaky retry",
                           objective="stop the retry storm",
                           task_type="bug_debug", repo_path="."))
    db.commit()
    db.add(cdb.RoutingRun(id="r-src", task_id="t-src", status="succeeded",
                          summary="patched backoff"))
    db.commit()
    if with_manifest:
        db.add(cdb.RunManifestRecord(id="m-src", run_id="r-src", manifest="{}"))
        db.commit()
    if lesson_history:
        # A separate scored run so model_lesson_gen_by_task has an aggregate.
        rid = str(uuid.uuid4())
        db.add(cdb.RoutingRun(id=rid, task_id="t-src", status="succeeded"))
        db.commit()
        db.add(cdb.RoutingModelRun(id=str(uuid.uuid4()), run_id=rid,
                                   model_profile_id="p-kb", completed=True,
                                   scores=json.dumps(lesson_history)))
        db.commit()
    mr = cdb.RoutingModelRun(
        id="mr-src", run_id="r-src", model_profile_id="p-kb", completed=True,
        scores=json.dumps(scores) if scores else None,
        artifacts=json.dumps(artifacts) if artifacts else None,
    )
    db.add(mr)
    db.commit()
    return mr


def test_draft_from_run_builds_grounded_template():
    db = _db()
    mr = _seed_model_run(
        db,
        scores={"verification": {"mode": "bug_fix", "passed": True,
                                 "patch_accepted": True}},
        artifacts={"response_text_path": "/arc/response.md",
                   "patch_path": "/arc/patch.diff"},
        lesson_history={"plan_quality": 4.0, "adversarial_review_quality": 3.0},
    )
    row = draft_from_run(db, mr)

    assert row.status == "draft"
    assert row.title == "Lesson: Fix the flaky retry"       # title from task
    assert row.category == "bug_debug"
    assert row.source_task_id == "t-src"
    assert row.source_model_run_id == "mr-src"
    assert row.created_by == "test/lesson-model"            # model label

    ev = json.loads(row.evidence)
    types = {e["type"] for e in ev}
    assert {"model_run", "run", "task", "run_manifest", "artifact",
            "verification"} <= types
    assert {"type": "model_run", "id": "mr-src"} in ev
    assert {"type": "run", "id": "r-src"} in ev
    assert {"type": "run_manifest", "id": "m-src"} in ev
    paths = {e["path"] for e in ev if e["type"] == "artifact"}
    assert paths == {"/arc/response.md", "/arc/patch.diff"}
    verif = next(e for e in ev if e["type"] == "verification")
    assert verif["passed"] is True and verif["patch_accepted"] is True

    # Body: structured template around the run's artifacts, no LLM prose;
    # explicit edit-before-validation note; WP6 lesson-gen score quoted.
    assert "stop the retry storm" in row.body
    assert "patched backoff" in row.body
    assert "/arc/patch.diff" in row.body
    assert "EDIT BEFORE VALIDATION" in row.body
    assert "advisory context only" in row.body
    assert "lesson-gen score (advisory): 3.5" in row.body   # (4.0+3.0)/2


def test_draft_from_run_without_verification_or_manifest():
    db = _db()
    mr = _seed_model_run(db, with_manifest=False)
    row = draft_from_run(db, mr)
    ev = json.loads(row.evidence)
    assert {"type": "model_run", "id": "mr-src"} in ev      # still grounded
    types = {e["type"] for e in ev}
    assert "run_manifest" not in types and "verification" not in types
    assert "Verification: none persisted" in row.body
    assert "not yet scored" in row.body


# ---------- routes ----------
class _StubAuthManager:
    is_configured = True

    def is_admin(self, user):
        return user == "admin"

    def get_privileges(self, user):
        return {"security_admin": user == "sec"}


def _make_app_and_client():
    engine = sqlalchemy.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    cdb.Base.metadata.create_all(bind=engine)
    rh.SessionLocal = sessionmaker(bind=engine, autoflush=False)

    app = FastAPI()
    app.state.auth_manager = _StubAuthManager()
    rh.setup_routing_harness_routes()
    app.include_router(rh.router)

    @app.middleware("http")
    async def _stamp(request, call_next):
        user = request.headers.get("x-test-user")
        if user:
            request.state.current_user = user
        return await call_next(request)

    return app, TestClient(app)


def _post_entry(client, **overrides):
    body = {"title": "route lesson", "body": "body", "evidence": list(EVIDENCE)}
    body.update(overrides)
    return client.post("/api/harness/knowledge", headers=ADMIN, json=body)


def test_routes_create_requires_evidence_400():
    _app, client = _make_app_and_client()
    r = _post_entry(client, evidence=[])
    assert r.status_code == 400
    assert "evidence" in r.json()["detail"]
    r = _post_entry(client)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft"


def test_routes_lifecycle_actor_from_admin_cookie_and_409s():
    _app, client = _make_app_and_client()
    eid = _post_entry(client).json()["id"]

    v = client.post(f"/api/harness/knowledge/{eid}/validate", headers=ADMIN, json={})
    assert v.status_code == 200, v.text
    assert v.json()["validated_by"] == "admin"              # actor from the gate

    # validated -> reject is illegal -> 409
    assert client.post(f"/api/harness/knowledge/{eid}/reject",
                       headers=ADMIN, json={}).status_code == 409

    # supersede: replacement required (422 from pydantic without a body field;
    # 400 when the replacement doesn't exist)
    assert client.post(f"/api/harness/knowledge/{eid}/supersede",
                       headers=ADMIN, json={"replacement_id": "ghost"}).status_code == 400
    rep = _post_entry(client, title="replacement").json()["id"]
    s = client.post(f"/api/harness/knowledge/{eid}/supersede",
                    headers=ADMIN, json={"replacement_id": rep})
    assert s.status_code == 200, s.text
    assert s.json()["superseded_by_id"] == rep

    # superseded is terminal: validate (even with the expired flag) -> 409
    assert client.post(f"/api/harness/knowledge/{eid}/validate", headers=ADMIN,
                       json={"revalidate_expired": True}).status_code == 409

    # expire path + expired re-validation flag
    e2 = _post_entry(client, title="second").json()["id"]
    client.post(f"/api/harness/knowledge/{e2}/validate", headers=ADMIN, json={})
    assert client.post(f"/api/harness/knowledge/{e2}/expire", headers=ADMIN,
                       json={"rationale": ""}).status_code == 400
    x = client.post(f"/api/harness/knowledge/{e2}/expire", headers=ADMIN,
                    json={"rationale": "area rewritten"})
    assert x.status_code == 200, x.text
    assert client.post(f"/api/harness/knowledge/{e2}/validate",
                       headers=ADMIN, json={}).status_code == 409
    rv = client.post(f"/api/harness/knowledge/{e2}/validate", headers=ADMIN,
                     json={"revalidate_expired": True})
    assert rv.status_code == 200, rv.text
    assert rv.json()["status"] == "validated"

    # unknown entry -> 404
    assert client.post("/api/harness/knowledge/nope/validate",
                       headers=ADMIN, json={}).status_code == 404


def test_routes_list_filters_and_drafts_first():
    _app, client = _make_app_and_client()
    d1 = _post_entry(client, title="old draft").json()["id"]
    v1 = _post_entry(client, title="to validate").json()["id"]
    client.post(f"/api/harness/knowledge/{v1}/validate", headers=ADMIN, json={})
    d2 = _post_entry(client, title="new draft").json()["id"]

    all_rows = client.get("/api/harness/knowledge", headers=ADMIN).json()
    assert [r["id"] for r in all_rows] == [d2, d1, v1]      # drafts first, newest first

    drafts = client.get("/api/harness/knowledge?status=draft", headers=ADMIN).json()
    assert {r["id"] for r in drafts} == {d1, d2}
    assert all(r["status"] == "draft" for r in drafts)


def test_routes_retrieve_validated_advisory_only():
    _app, client = _make_app_and_client()
    _post_entry(client, title="draft stays out")
    v = _post_entry(client, title="the lesson", category="bug_debug").json()["id"]
    client.post(f"/api/harness/knowledge/{v}/validate", headers=ADMIN, json={})

    r = client.get("/api/harness/knowledge/retrieve", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["advisory"] is True
    assert "never policy" in body["note"]
    assert [i["entry"]["id"] for i in body["items"]] == [v]
    assert all(i["advisory"] is True and i["note"] == ADVISORY_NOTE
               for i in body["items"])


def test_routes_draft_from_run():
    _app, client = _make_app_and_client()
    missing = client.post("/api/harness/knowledge/draft-from-run",
                          headers=ADMIN, json={"model_run_id": "ghost"})
    assert missing.status_code == 404

    s = rh.SessionLocal()
    _seed_model_run(s, artifacts={"response_text_path": "/arc/r.md"})
    s.close()
    r = client.post("/api/harness/knowledge/draft-from-run",
                    headers=ADMIN, json={"model_run_id": "mr-src"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "draft"
    assert d["source_model_run_id"] == "mr-src"
    assert any(e.get("type") == "model_run" for e in d["evidence"])


# ---------- auth gating (AUTH_ENABLED=true, real gate code) ----------
class TestKnowledgeAuthGating:
    def setup_method(self):
        os.environ["AUTH_ENABLED"] = "true"

    def teardown_method(self):
        os.environ["AUTH_ENABLED"] = "false"

    GATED = [
        ("get", "/api/harness/knowledge", None),
        ("get", "/api/harness/knowledge/retrieve", None),
        ("post", "/api/harness/knowledge",
         {"title": "t", "body": "b", "evidence": EVIDENCE}),
        ("post", "/api/harness/knowledge/x/validate", {}),
        ("post", "/api/harness/knowledge/x/reject", {}),
        ("post", "/api/harness/knowledge/x/supersede", {"replacement_id": "y"}),
        ("post", "/api/harness/knowledge/x/expire", {"rationale": "r"}),
        ("post", "/api/harness/knowledge/draft-from-run", {"model_run_id": "x"}),
    ]

    def _call(self, client, method, path, body, headers):
        kwargs = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        return getattr(client, method)(path, **kwargs)

    def test_unauthenticated_and_non_admin_rejected_403(self):
        _app, client = _make_app_and_client()
        for method, path, body in self.GATED:
            assert self._call(client, method, path, body, {}).status_code == 403, path
            assert self._call(client, method, path, body, NON_ADMIN).status_code == 403, path

    def test_admin_cookie_allowed(self):
        _app, client = _make_app_and_client()
        assert client.get("/api/harness/knowledge", headers=ADMIN).status_code == 200
        assert client.get("/api/harness/knowledge/retrieve", headers=ADMIN).status_code == 200


# ---------- the advisory-only invariant ----------
def test_kb_retrieval_cannot_flip_routing_or_verification():
    """Knowledge is context, never policy. Two teeth:

    1. Every retrieval item is advisory-labeled (asserted above too) and the
       wrapper carries no gate/veto/block lever.
    2. Grep the decision-making code: no module under src/ (other than
       routing_knowledge itself) imports or references the knowledge module
       or its retrieval surface — so route_task / verify_model_run /
       budget / escalation / coordinator gates structurally CANNOT consume a
       KB entry. (routes/ may serve it read-only to the admin UI; that is
       display, not decision.)"""
    db = _db()
    v = validate_entry(db, _draft(db).id, "tim")
    for item in retrieve_validated(db):
        assert item["advisory"] is True
        assert item["note"] == ADVISORY_NOTE

    src_dir = Path(ROOT) / "src"
    offenders = []
    for path in sorted(src_dir.glob("*.py")):
        if path.name == "routing_knowledge.py":
            continue
        text = path.read_text(errors="replace")
        if re.search(r"routing_knowledge|retrieve_validated|KnowledgeBaseEntry", text):
            offenders.append(path.name)
    assert offenders == [], (
        f"src modules referencing the knowledge surface: {offenders} — "
        "knowledge entries are advisory context and must never enter "
        "routing/verification/budget decisions")

    # And the routing engine's decision for a task is byte-identical whether
    # or not validated KB entries exist (route_task has no KB input at all).
    import types
    from src.routing_engine import route_task
    db2 = _db()
    db2.add(cdb.RoutingModelProfile(
        id="p-x", model="m", roles=json.dumps(["scout"]),
        context_window=32768, is_free=True, enabled=True))
    db2.commit()
    stub = types.SimpleNamespace(
        id="t-x", task_type="bug_debug", risk="low",
        allow_free_models=True, allow_paid_models=False,
        allow_premium_models=False, data_sensitivity="internal")
    bundle = {"metadata": {"token_estimate": 10}, "files": []}
    before = route_task(db2, stub, bundle)["candidates"]
    validate_entry(db2, _draft(db2, category="bug_debug").id, "tim")
    assert route_task(db2, stub, bundle)["candidates"] == before
