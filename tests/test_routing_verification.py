"""Phase 4 mode-aware verification (spec Section 16): the MODE_INVARIANTS
table, task-type mode inference, analysis_only never accepting a patch,
bug_fix failing-case + regression semantics, weight-0 generated tests vs
promoted (blocking) ones, baseline fuzzing that blocks only NEW regressions,
strict stdout-equivalence confined to refactor_equivalence, fail-closed
denied commands, confidence-as-metadata (never a gate) + the overconfident-
failure calibration flag, persistence (scores merge + verification.json),
worktree hygiene, and the generated-test registry routes (create/list/
promote/demote audit trail). Docker is mocked exactly like
test_routing_execution.py; worktrees use real throwaway git repos."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling test-module import

import core.database as cdb
import src.routing_sandbox as rs
import src.routing_verification as rv
from src.routing_coordinator import VerificationMode
from src.routing_workdir import worktrees_root

SB_IMAGE = "python:3.12-slim"
SB_POLICY = {
    "sandbox": {
        "image": SB_IMAGE,
        "cpus": 1,
        "memoryGb": 2,
        "pidsLimit": 64,
        "wallClockSeconds": 5,
        "maxOutputBytes": 4096,
        "mountLabel": "",
        "allowedCommands": ["pytest", "python -m pytest", "make test"],
    },
    "verification": {
        "defaultMode": "regression_guard",
        "equivalenceStdoutComparison": True,
        "overconfidenceThreshold": 0.8,
    },
}

PATCH = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello
+goodbye
"""


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Throwaway harness data root (worktree jail + sandbox artifacts)."""
    d = tmp_path / "data"
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(d))
    return d


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "hello.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "-qm", "init"],
        check=True,
    )
    return path


def _db():
    engine = sqlalchemy.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    cdb.Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False)()


def _seed(db, repo, tmp_path, *, task_type="ci_triage", verification_mode=None,
          inputs=None, scores=None, patch=PATCH, response_text=None):
    """RoutingTask -> RoutingRun -> RoutingModelRun with an archived patch
    (and optionally an archived analysis response). Returns (task, model_run,
    run_dir) where run_dir is where manifest.json/verification.json live."""
    run_dir = tmp_path / "archive" / "task-1" / "run-1"
    attempt = run_dir / "001-profile"
    attempt.mkdir(parents=True, exist_ok=True)
    artifacts = {}
    if patch is not None:
        p = attempt / "patch.diff"
        p.write_text(patch)
        artifacts["patch_path"] = str(p)
    if response_text is not None:
        r = attempt / "response.md"
        r.write_text(response_text)
        artifacts["response_text_path"] = str(r)
    db.add(cdb.RoutingTask(id="task-1", title="t", objective="o",
                           task_type=task_type, repo_path=str(repo),
                           inputs=json.dumps(inputs or {}),
                           verification_mode=verification_mode))
    db.commit()
    db.add(cdb.RoutingRun(id="run-1", task_id="task-1", status="running"))
    db.commit()
    mr = cdb.RoutingModelRun(id="mr-1", run_id="run-1",
                             artifacts=json.dumps(artifacts),
                             scores=json.dumps(scores) if scores is not None else None)
    db.add(mr)
    db.commit()
    return db.get(cdb.RoutingTask, "task-1"), mr, run_dir


def _add_generated_test(db, cmd, authority, promoted=False, test_id="gt-1"):
    db.add(cdb.GeneratedTest(id=test_id, task_id="task-1", authority=authority,
                             command=cmd, promoted=promoted))
    db.commit()
    return test_id


def _is_patched(worktree: str) -> bool:
    return (Path(worktree) / "hello.txt").read_text().strip() == "goodbye"


def _fake_docker(monkeypatch, behavior):
    """Docker mock dispatching on the exec'd command; each behavior callable
    receives the mounted worktree path so it can react to whether the patch
    is applied there (exactly how fuzz/equivalence baselining differs).
    behavior: {cmd_string: fn(worktree_path) -> (returncode, stdout_bytes)}"""
    calls = []
    real_run = subprocess.run  # git (worktree lifecycle) must keep working

    def fake_run(argv, **kwargs):
        if argv[:1] != ["docker"]:
            return real_run(argv, **kwargs)
        if argv[:2] != ["docker", "run"]:
            return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
        worktree = argv[argv.index("-v") + 1].split(":")[0]
        cmd = " ".join(argv[argv.index(SB_IMAGE) + 1:])
        rc, out = behavior[cmd](worktree)
        calls.append({"cmd": cmd, "worktree": worktree, "rc": rc})
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr=b"")

    monkeypatch.setattr(rs.subprocess, "run", fake_run)
    return calls


def _always(rc, out=b"out"):
    return lambda _wt: (rc, out)


def _by_tree(orig_rc, patched_rc, out=b"out"):
    return lambda wt: ((patched_rc if _is_patched(wt) else orig_rc), out)


def _verify(db, task, mr, **kwargs):
    kwargs.setdefault("policy", SB_POLICY)
    return rv.verify_model_run(db, task, mr, **kwargs)


# ---------- MODE_INVARIANTS completeness (the spec Section 16 table) ----------
def test_mode_invariants_encodes_the_spec_table():
    assert set(rv.MODE_INVARIANTS) == {m.value for m in VerificationMode}

    def table(mode):
        return [(l["layer"], l["source"], l["blocking"]) for l in rv.MODE_INVARIANTS[mode]]

    # regression_guard: existing tests blocking; generated tests ADVISORY;
    # new regressions (baseline fuzz) blocking.
    assert table("regression_guard") == [
        ("existing_tests", "existing_tests", True),
        ("generated_tests", "acceptance_tests", False),
        ("baseline_fuzz", "fuzz", True),
    ]
    # bug_fix: original failing case + no unrelated regressions, all blocking.
    assert table("bug_fix") == [
        ("original_failing_case", "original_failing_case", True),
        ("existing_tests", "existing_tests", True),
        ("baseline_fuzz", "fuzz", True),
    ]
    # feature_addition: existing tests + human acceptance criteria blocking.
    assert table("feature_addition") == [
        ("existing_tests", "existing_tests", True),
        ("acceptance_tests", "acceptance_tests", True),
    ]
    # refactor_equivalence: the ONLY mode with the strict equivalence layer.
    assert table("refactor_equivalence") == [
        ("existing_tests", "existing_tests", True),
        ("behavioral_equivalence", "equivalence", True),
        ("baseline_fuzz", "fuzz", True),
    ]
    # security_fix: assertions gate; prior behavior may change (tests advisory).
    assert table("security_fix") == [
        ("security_assertions", "security_assertions", True),
        ("existing_tests", "existing_tests", False),
    ]
    # analysis_only: no layers, and NEVER accepts a patch.
    assert rv.MODE_INVARIANTS["analysis_only"] == []
    assert rv.MODE_ACCEPTS_PATCH["analysis_only"] is False
    assert all(rv.MODE_ACCEPTS_PATCH[m] for m in rv.MODE_INVARIANTS if m != "analysis_only")

    # Strict equivalence is available in refactor_equivalence ONLY.
    for mode, layers in rv.MODE_INVARIANTS.items():
        if mode != "refactor_equivalence":
            assert all(l["source"] != "equivalence" for l in layers), mode


# ---------- infer_mode ----------
@pytest.mark.parametrize("task_type,expected", [
    ("bug_debug", "bug_fix"),
    ("implementation", "feature_addition"),
    ("diff_review", "analysis_only"),
    ("feature_plan", "analysis_only"),
    ("ci_triage", "regression_guard"),
    ("release_readiness", "regression_guard"),
    ("unknown", "regression_guard"),
])
def test_infer_mode(task_type, expected):
    task = cdb.RoutingTask(id="x", title="t", objective="o",
                           task_type=task_type, repo_path=".")
    assert rv.infer_mode(task, policy=SB_POLICY) == expected


def test_infer_mode_fallback_honors_policy_default_mode():
    task = cdb.RoutingTask(id="x", title="t", objective="o",
                           task_type="ci_triage", repo_path=".")
    policy = {"verification": {"defaultMode": "bug_fix"}}
    assert rv.infer_mode(task, policy=policy) == "bug_fix"
    # An invalid configured default fails safe to regression_guard.
    assert rv.infer_mode(task, policy={"verification": {"defaultMode": "nope"}}) == "regression_guard"


# ---------- analysis_only ----------
def test_analysis_only_never_accepts_patch_even_when_commands_would_pass(
        tmp_path, data_dir, repo, monkeypatch):
    calls = _fake_docker(monkeypatch, {"pytest -q": _always(0)})
    db = _db()
    # diff_review infers analysis_only; test_commands present and would pass.
    task, mr, _run_dir = _seed(db, repo, tmp_path, task_type="diff_review",
                               inputs={"test_commands": ["pytest -q"]},
                               response_text="# analysis\nevidence here\n")

    result = _verify(db, task, mr)

    assert result["mode"] == "analysis_only"
    assert result["patch_accepted"] is False
    assert result["passed"] is True  # evidence-complete report
    assert result["layers"] == []
    assert calls == [], "analysis_only must never execute verification commands"
    assert any("NEVER accepts a patch" in n for n in result["notes"])
    # No worktree was ever created.
    assert not os.path.isdir(worktrees_root()) or os.listdir(worktrees_root()) == []


def test_analysis_only_fails_on_missing_evidence(tmp_path, data_dir, repo, monkeypatch):
    _fake_docker(monkeypatch, {})
    db = _db()
    task, mr, _run_dir = _seed(db, repo, tmp_path, task_type="feature_plan",
                               response_text=None)
    result = _verify(db, task, mr)
    assert result["passed"] is False
    assert result["patch_accepted"] is False


# ---------- bug_fix ----------
BUG_FIX_INPUTS = {
    "test_commands": ["pytest -q"],
    "failing_case_commands": ["pytest -q tests/test_bug.py"],
}


def test_bug_fix_passes_when_failing_case_and_tests_pass(tmp_path, data_dir, repo, monkeypatch):
    _fake_docker(monkeypatch, {
        "pytest -q": _always(0),
        # The reproducer failed pre-patch; on the patched tree it passes.
        "pytest -q tests/test_bug.py": _by_tree(1, 0),
    })
    db = _db()
    task, mr, run_dir = _seed(db, repo, tmp_path, task_type="bug_debug",
                              inputs=BUG_FIX_INPUTS, scores={"correctness": 4})

    result = _verify(db, task, mr)

    assert result["mode"] == "bug_fix"
    assert result["patch_applied"] is True
    assert result["passed"] is True
    assert result["patch_accepted"] is True
    by_layer = {l["layer"]: l for l in result["layers"]}
    assert by_layer["original_failing_case"]["passed"] is True
    assert by_layer["existing_tests"]["passed"] is True
    assert by_layer["baseline_fuzz"]["skipped"] is True  # no fuzz registry rows
    # Commands carry the audit linkage.
    cmd = by_layer["existing_tests"]["commands"][0]
    assert cmd["cmd"] == "pytest -q" and cmd["exit_code"] == 0
    assert cmd["tool_call_record_id"]
    assert db.get(cdb.ToolCallRecord, cmd["tool_call_record_id"]) is not None

    # Persistence: scores MERGED (other keys survive) + verification.json
    # archived in the run dir next to manifest.json.
    scores = json.loads(db.get(cdb.RoutingModelRun, "mr-1").scores)
    assert scores["correctness"] == 4
    assert scores["verification"]["passed"] is True
    archived = json.loads((run_dir / "verification.json").read_text())
    assert archived["model_run_id"] == "mr-1"
    assert archived["passed"] is True

    # Worktree hygiene: nothing left behind, source repo untouched.
    assert os.listdir(worktrees_root()) == []
    assert (repo / "hello.txt").read_text() == "hello\n"


def test_bug_fix_fails_when_unrelated_tests_regress(tmp_path, data_dir, repo, monkeypatch):
    _fake_docker(monkeypatch, {
        "pytest -q": _by_tree(0, 1),  # regression introduced by the patch
        "pytest -q tests/test_bug.py": _by_tree(1, 0),
    })
    db = _db()
    task, mr, _run_dir = _seed(db, repo, tmp_path, task_type="bug_debug",
                               inputs=BUG_FIX_INPUTS)
    result = _verify(db, task, mr)
    assert result["passed"] is False
    assert result["patch_accepted"] is False
    by_layer = {l["layer"]: l for l in result["layers"]}
    assert by_layer["original_failing_case"]["passed"] is True
    assert by_layer["existing_tests"]["passed"] is False


def test_bug_fix_missing_failing_case_skips_layer_with_note(tmp_path, data_dir, repo, monkeypatch):
    _fake_docker(monkeypatch, {"pytest -q": _always(0)})
    db = _db()
    task, mr, _run_dir = _seed(db, repo, tmp_path, task_type="bug_debug",
                               inputs={"test_commands": ["pytest -q"]})
    result = _verify(db, task, mr)
    by_layer = {l["layer"]: l for l in result["layers"]}
    assert by_layer["original_failing_case"]["skipped"] is True
    assert any("failing_case_commands" in n for n in by_layer["original_failing_case"]["notes"])
    assert result["passed"] is True


# ---------- generated-test authority (weight 0 until promoted) ----------
def test_unpromoted_generated_test_failure_never_flips_passed_but_promoted_does(
        tmp_path, data_dir, repo, monkeypatch):
    _fake_docker(monkeypatch, {
        "pytest -q": _always(0),
        "pytest -q tests/test_gen.py": _always(1),  # the generated test fails
    })
    db = _db()
    task, mr, _run_dir = _seed(db, repo, tmp_path,
                               verification_mode="feature_addition",
                               inputs={"test_commands": ["pytest -q"]})
    _add_generated_test(db, "pytest -q tests/test_gen.py",
                        rv.TestAuthority.GENERATED_AMPLIFICATION_TEST.value)

    result = _verify(db, task, mr)
    assert result["passed"] is True, "weight-0 generated test must not block"
    acceptance = {l["layer"]: l for l in result["layers"]}["acceptance_tests"]
    assert acceptance["passed"] is True
    assert acceptance["commands"][0]["advisory"] is True
    assert acceptance["commands"][0]["exit_code"] == 1
    assert any("advisory" in n for n in acceptance["notes"])

    # Promote (the persistent human authority grant) -> same failure now blocks.
    row = db.get(cdb.GeneratedTest, "gt-1")
    row.promoted = True
    db.commit()
    result2 = _verify(db, task, mr)
    assert result2["passed"] is False
    acceptance2 = {l["layer"]: l for l in result2["layers"]}["acceptance_tests"]
    assert acceptance2["passed"] is False
    assert acceptance2["commands"][0]["advisory"] is False


def test_human_authored_acceptance_command_blocks_natively(tmp_path, data_dir, repo, monkeypatch):
    _fake_docker(monkeypatch, {
        "pytest -q": _always(0),
        "pytest -q tests/test_accept.py": _always(1),
    })
    db = _db()
    task, mr, _run_dir = _seed(db, repo, tmp_path,
                               verification_mode="feature_addition",
                               inputs={"test_commands": ["pytest -q"],
                                       "acceptance_commands": ["pytest -q tests/test_accept.py"]})
    result = _verify(db, task, mr)
    assert result["passed"] is False  # task-author criteria are human-authored


def test_generated_tests_are_advisory_layer_in_regression_guard(tmp_path, data_dir, repo, monkeypatch):
    """regression_guard's spec row is 'generated tests advisory': the whole
    generated_tests layer is blocking=False there, so even a promoted test's
    failure reports without gating (promotion gates in acceptance modes)."""
    _fake_docker(monkeypatch, {
        "pytest -q": _always(0),
        "pytest -q tests/test_gen.py": _always(1),
    })
    db = _db()
    task, mr, _run_dir = _seed(db, repo, tmp_path,
                               verification_mode="regression_guard",
                               inputs={"test_commands": ["pytest -q"]})
    _add_generated_test(db, "pytest -q tests/test_gen.py",
                        rv.TestAuthority.GENERATED_AMPLIFICATION_TEST.value, promoted=True)
    result = _verify(db, task, mr)
    generated = {l["layer"]: l for l in result["layers"]}["generated_tests"]
    assert generated["blocking"] is False
    assert generated["passed"] is False  # reported truthfully...
    assert result["passed"] is True      # ...but never gates in this mode


# ---------- baseline fuzz ----------
def test_fuzz_new_regression_blocks(tmp_path, data_dir, repo, monkeypatch):
    calls = _fake_docker(monkeypatch, {
        "pytest -q": _always(0),
        "pytest -q fuzz_case.py": _by_tree(0, 1),  # ok on original, fails patched
    })
    db = _db()
    task, mr, _run_dir = _seed(db, repo, tmp_path,
                               verification_mode="regression_guard",
                               inputs={"test_commands": ["pytest -q"]})
    _add_generated_test(db, "pytest -q fuzz_case.py",
                        rv.TestAuthority.GENERATED_FUZZ_CASE.value)

    result = _verify(db, task, mr)

    assert result["passed"] is False
    fuzz = {l["layer"]: l for l in result["layers"]}["baseline_fuzz"]
    assert fuzz["passed"] is False
    rec = fuzz["commands"][0]
    assert rec["new_regression"] is True
    assert rec["original_exit_code"] == 0 and rec["exit_code"] == 1
    assert rec["tool_call_record_id"] and rec["original_tool_call_record_id"]
    # The case genuinely ran against BOTH worktrees.
    fuzz_calls = [c for c in calls if c["cmd"] == "pytest -q fuzz_case.py"]
    assert len(fuzz_calls) == 2
    assert len({c["worktree"] for c in fuzz_calls}) == 2


def test_fuzz_pre_existing_failure_does_not_block(tmp_path, data_dir, repo, monkeypatch):
    _fake_docker(monkeypatch, {
        "pytest -q": _always(0),
        "pytest -q fuzz_case.py": _always(1),  # fails on BOTH trees: pre-existing
    })
    db = _db()
    task, mr, _run_dir = _seed(db, repo, tmp_path,
                               verification_mode="regression_guard",
                               inputs={"test_commands": ["pytest -q"]})
    _add_generated_test(db, "pytest -q fuzz_case.py",
                        rv.TestAuthority.GENERATED_FUZZ_CASE.value)

    result = _verify(db, task, mr)

    assert result["passed"] is True
    fuzz = {l["layer"]: l for l in result["layers"]}["baseline_fuzz"]
    assert fuzz["passed"] is True
    assert fuzz["commands"][0]["new_regression"] is False
    assert fuzz["commands"][0]["pre_existing_failure"] is True
    assert any("pre-existing" in n for n in fuzz["notes"])


# ---------- strict equivalence (refactor_equivalence ONLY) ----------
EQ_INPUTS = {"test_commands": ["pytest -q"],
             "equivalence_commands": ["pytest -q tests/test_eq.py"]}


def _eq_behavior():
    return {
        "pytest -q": _always(0),
        # Same exit code but DIFFERENT stdout between original and patched.
        "pytest -q tests/test_eq.py":
            lambda wt: (0, b"B\n" if _is_patched(wt) else b"A\n"),
    }


def test_equivalence_stdout_mismatch_fails_in_refactor_equivalence(
        tmp_path, data_dir, repo, monkeypatch):
    _fake_docker(monkeypatch, _eq_behavior())
    db = _db()
    task, mr, _run_dir = _seed(db, repo, tmp_path,
                               verification_mode="refactor_equivalence",
                               inputs=EQ_INPUTS)
    result = _verify(db, task, mr)
    assert result["passed"] is False
    eq = {l["layer"]: l for l in result["layers"]}["behavioral_equivalence"]
    assert eq["passed"] is False
    assert eq["commands"][0]["stdout_match"] is False
    assert any("byte-wise" in n for n in eq["notes"])


@pytest.mark.parametrize("mode", ["regression_guard", "bug_fix", "feature_addition",
                                  "security_fix"])
def test_equivalence_mismatch_is_irrelevant_outside_refactor_equivalence(
        tmp_path, data_dir, repo, monkeypatch, mode):
    _fake_docker(monkeypatch, _eq_behavior())
    db = _db()
    task, mr, _run_dir = _seed(db, repo, tmp_path, verification_mode=mode,
                               inputs=EQ_INPUTS)
    result = _verify(db, task, mr)
    assert all(l["source"] != "equivalence" for l in result["layers"])
    assert result["passed"] is True


def test_equivalence_stdout_match_passes(tmp_path, data_dir, repo, monkeypatch):
    _fake_docker(monkeypatch, {
        "pytest -q": _always(0),
        "pytest -q tests/test_eq.py": _always(0, b"same\n"),
    })
    db = _db()
    task, mr, _run_dir = _seed(db, repo, tmp_path,
                               verification_mode="refactor_equivalence",
                               inputs=EQ_INPUTS)
    result = _verify(db, task, mr)
    assert result["passed"] is True
    eq = {l["layer"]: l for l in result["layers"]}["behavioral_equivalence"]
    assert eq["commands"][0]["stdout_match"] is True


# ---------- fail-closed on denied commands ----------
def test_denied_command_fails_blocking_layer_closed(tmp_path, data_dir, repo, monkeypatch):
    calls = _fake_docker(monkeypatch, {})  # docker must never be reached
    db = _db()
    task, mr, _run_dir = _seed(db, repo, tmp_path,
                               verification_mode="regression_guard",
                               inputs={"test_commands": ["rm -rf /"]})
    result = _verify(db, task, mr)
    assert result["passed"] is False
    assert result["patch_accepted"] is False
    existing = {l["layer"]: l for l in result["layers"]}["existing_tests"]
    assert existing["passed"] is False
    assert existing["commands"][0]["error"] == "command_not_allowed"
    assert existing["commands"][0]["exit_code"] is None
    assert any("denied" in n for n in existing["notes"])
    assert calls == [], "a denied command must never reach docker"
    # DENIED attempts are audited too.
    rec = db.get(cdb.ToolCallRecord, existing["commands"][0]["tool_call_record_id"])
    assert rec is not None and rec.allowed is False


# ---------- confidence: metadata only, NEVER a gate ----------
@pytest.mark.parametrize("tests_pass", [True, False])
@pytest.mark.parametrize("confidence", [0.1, 0.79, 0.8, 0.95])
def test_confidence_never_changes_passed(tmp_path, data_dir, repo, monkeypatch,
                                          tests_pass, confidence):
    _fake_docker(monkeypatch, {"pytest -q": _always(0 if tests_pass else 1)})
    db = _db()
    task, mr, _run_dir = _seed(db, repo, tmp_path,
                               verification_mode="regression_guard",
                               inputs={"test_commands": ["pytest -q"]},
                               scores={"confidence": confidence})
    result = _verify(db, task, mr)
    assert result["passed"] is tests_pass  # command outcomes alone decide
    assert result["confidence"]["value"] == confidence
    assert result["confidence"]["neverBlocks"] is True
    # Calibration flag: overconfident FAILURES only (threshold 0.8).
    if not tests_pass and confidence >= 0.8:
        assert result["calibration"] == {"overconfidentFailure": True}
    else:
        assert "calibration" not in result


def test_overconfident_failure_calibration_note(tmp_path, data_dir, repo, monkeypatch):
    _fake_docker(monkeypatch, {"pytest -q": _always(1)})
    db = _db()
    task, mr, _run_dir = _seed(db, repo, tmp_path,
                               verification_mode="regression_guard",
                               inputs={"test_commands": ["pytest -q"]},
                               scores={"confidence": 0.9, "correctness": 2})
    result = _verify(db, task, mr)
    assert result["passed"] is False
    assert result["calibration"] == {"overconfidentFailure": True}
    # Persisted for WP6's calibration stats, other score keys intact.
    scores = json.loads(db.get(cdb.RoutingModelRun, "mr-1").scores)
    assert scores["verification"]["calibration"] == {"overconfidentFailure": True}
    assert scores["correctness"] == 2 and scores["confidence"] == 0.9


# ---------- worktree retention ----------
def test_keep_worktree_keeps_patched_only(tmp_path, data_dir, repo, monkeypatch):
    _fake_docker(monkeypatch, {
        "pytest -q": _always(0),
        "pytest -q fuzz_case.py": _always(0),
    })
    db = _db()
    task, mr, _run_dir = _seed(db, repo, tmp_path,
                               verification_mode="regression_guard",
                               inputs={"test_commands": ["pytest -q"]})
    _add_generated_test(db, "pytest -q fuzz_case.py",
                        rv.TestAuthority.GENERATED_FUZZ_CASE.value)

    result = _verify(db, task, mr, keep_worktree=True)

    leftovers = os.listdir(worktrees_root())
    assert len(leftovers) == 1, "only the PATCHED worktree may be kept"
    kept = os.path.join(worktrees_root(), leftovers[0])
    assert result["worktree_path"] == kept
    assert _is_patched(kept)


# ---------- registry routes: create/list + promote/demote audit roundtrip ----------
def test_generated_test_routes_promote_demote_roundtrip(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    from test_routing_harness_routes import ADMIN, _make_app_and_client
    import routes.routing_harness_routes as rh

    _app, client = _make_app_and_client()
    s = rh.SessionLocal()
    s.add(cdb.RoutingTask(id="task-r", title="t", objective="o",
                          task_type="implementation", repo_path="."))
    s.commit()
    s.close()

    # Validation: unknown authority and non-allowlisted command both 400.
    bad_auth = client.post("/api/harness/tests", headers=ADMIN, json={
        "task_id": "task-r", "authority": "totally_made_up", "command": "pytest -q"})
    assert bad_auth.status_code == 400
    bad_cmd = client.post("/api/harness/tests", headers=ADMIN, json={
        "task_id": "task-r", "authority": "generated_amplification_test",
        "command": "pytest; rm -rf /"})
    assert bad_cmd.status_code == 400
    no_task = client.post("/api/harness/tests", headers=ADMIN, json={
        "task_id": "nope", "authority": "generated_amplification_test",
        "command": "pytest -q"})
    assert no_task.status_code == 404

    create = client.post("/api/harness/tests", headers=ADMIN, json={
        "task_id": "task-r", "authority": "generated_amplification_test",
        "command": "pytest -q tests/test_gen.py", "origin_model_run_id": "mr-9"})
    assert create.status_code == 200, create.text
    body = create.json()
    test_id = body["id"]
    # Weight-0 default: generated rows start unpromoted and advisory.
    assert body["promoted"] is False
    assert body["blocking_eligible"] is False

    lst = client.get("/api/harness/tests?task_id=task-r", headers=ADMIN)
    assert lst.status_code == 200
    assert [t["id"] for t in lst.json()] == [test_id]

    # Promote: the human authority grant — persistent AND auditable.
    pr = client.post(f"/api/harness/tests/{test_id}/promote", headers=ADMIN)
    assert pr.status_code == 200, pr.text
    promoted = pr.json()
    assert promoted["promoted"] is True
    assert promoted["blocking_eligible"] is True
    assert promoted["promoted_by"] == "admin"
    assert promoted["promoted_at"]
    assert "promoted by admin" in promoted["notes"]

    # Demote: back to advisory; the notes trail keeps the full history.
    dm = client.post(f"/api/harness/tests/{test_id}/demote", headers=ADMIN)
    assert dm.status_code == 200, dm.text
    demoted = dm.json()
    assert demoted["promoted"] is False
    assert demoted["blocking_eligible"] is False
    assert demoted["promoted_by"] is None and demoted["promoted_at"] is None
    assert "promoted by admin" in demoted["notes"]
    assert "demoted by admin" in demoted["notes"]

    # Human-authored acceptance tests are blocking-eligible without promotion.
    human = client.post("/api/harness/tests", headers=ADMIN, json={
        "task_id": "task-r", "authority": "human_authored_acceptance_test",
        "command": "pytest -q tests/test_accept.py"})
    assert human.status_code == 200
    assert human.json()["blocking_eligible"] is True

    missing = client.post("/api/harness/tests/nope/promote", headers=ADMIN)
    assert missing.status_code == 404
