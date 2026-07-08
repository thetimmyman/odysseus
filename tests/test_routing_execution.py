"""Phase 3 "Safe Execution" (spec Section 15): sandbox command allowlist +
metacharacter rejection, docker argv construction (network off, single rw
worktree mount, caps dropped), wall-clock timeout -> docker kill, output
truncation, ToolCallRecord persistence for allowed AND denied attempts, and
the git worktree lifecycle (clean-tree requirement, jailed create/remove,
apply --check, failed-patch revert). Never calls real docker."""
import hashlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.database as cdb
import src.routing_sandbox as rs
from src.routing_sandbox import is_command_allowed, run_in_sandbox
from src.routing_workdir import (
    apply_patch,
    create_worktree,
    data_root,
    remove_worktree,
    revert_worktree,
    worktrees_root,
)

SB_POLICY = {"sandbox": {
    "image": "python:3.12-slim",
    "cpus": 1,
    "memoryGb": 2,
    "pidsLimit": 64,
    "wallClockSeconds": 5,
    "maxOutputBytes": 100,
    "mountLabel": "",  # keep the plain-mount argv assertions isolated
    "allowedCommands": ["pytest", "python -m pytest", "ruff check", "make test"],
}}


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Point the harness data root (worktree jail + sandbox artifacts) at a
    throwaway dir -- data_root() reads the env per call, so this is enough."""
    d = tmp_path / "data"
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(d))
    return d


# ---------- command allowlist ----------
@pytest.mark.parametrize("cmd", [
    "pytest",
    "pytest -q",
    "pytest -q tests/test_x.py",
    "python -m pytest tests/",
    "ruff check src",
    "make test",
])
def test_allowlist_accepts(cmd):
    assert is_command_allowed(cmd, SB_POLICY) is True


@pytest.mark.parametrize("cmd", [
    "rm -rf /",                      # not on the allowlist at all
    "pytests",                       # prefix must match a whole token, not a substring
    "pytest; rm -rf /",              # ; metacharacter
    "pytest | tee out",              # | metacharacter
    "$(evil)",                       # $( metacharacter
    "pytest > out.txt",              # redirection
    "pytest < input",                # redirection
    "pytest && rm -rf /",            # & metacharacter
    "pytest `evil`",                 # backtick
    "pytest\nrm -rf /",              # newline
    "",                              # empty
    "   ",                           # whitespace only
])
def test_allowlist_rejects(cmd):
    assert is_command_allowed(cmd, SB_POLICY) is False


# ---------- docker argv construction (docker mocked) ----------
def _fake_run_factory(calls, stdout=b"ok", stderr=b"", returncode=0):
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=stderr)
    return fake_run


def test_run_in_sandbox_argv(tmp_path, data_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(rs.subprocess, "run", _fake_run_factory(calls))
    wt = tmp_path / "wt"
    wt.mkdir()

    result = run_in_sandbox(str(wt), "pytest -q", policy=SB_POLICY)

    assert result["allowed"] is True
    assert result["exit_code"] == 0
    assert result["timed_out"] is False
    (argv, kwargs) = calls[0]
    assert argv[:3] == ["docker", "run", "--rm"]
    # Network disabled.
    assert argv[argv.index("--network") + 1] == "none"
    # Filesystem allowlist: EXACTLY one host mount, the worktree rw at /work.
    assert argv.count("-v") == 1
    assert argv[argv.index("-v") + 1] == f"{os.path.realpath(str(wt))}:/work:rw"
    assert "--mount" not in argv and "--volume" not in argv and "--privileged" not in argv
    assert argv[argv.index("-w") + 1] == "/work"
    # Hardening + resource limits from policy.
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    # Non-root: runs as the host uid so worktree writes survive --cap-drop ALL.
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"
    assert argv[argv.index("--cpus") + 1] == "1"
    assert argv[argv.index("--memory") + 1] == "2g"
    assert argv[argv.index("--pids-limit") + 1] == "64"
    assert argv[argv.index("--tmpfs") + 1] == "/tmp"
    # Command exec'd without a shell: image then the shlex'd argv tail.
    image_idx = argv.index("python:3.12-slim")
    assert argv[image_idx + 1:] == ["pytest", "-q"]
    assert "sh" not in argv and "bash" not in argv
    # Wall clock enforced via subprocess timeout.
    assert kwargs["timeout"] == 5
    # Output archived.
    assert os.path.isfile(result["stdout_path"])
    with open(result["stdout_path"], "rb") as f:
        assert f.read() == b"ok"


def test_run_in_sandbox_mount_label_applied(tmp_path, data_dir, monkeypatch):
    """A policy mountLabel (SELinux relabel) is appended to the bind mount as
    a comma-separated option -- required on enforcing hosts or the container
    can't write to /work. Default policy carries "z"; empty disables it."""
    calls = []
    monkeypatch.setattr(rs.subprocess, "run", _fake_run_factory(calls))
    wt = tmp_path / "wt"
    wt.mkdir()
    labeled = {"sandbox": dict(SB_POLICY["sandbox"], mountLabel="z")}

    run_in_sandbox(str(wt), "pytest -q", policy=labeled)

    (argv, _kwargs) = calls[0]
    assert argv[argv.index("-v") + 1] == f"{os.path.realpath(str(wt))}:/work:rw,z"


def test_run_in_sandbox_timeout_kills_container(tmp_path, data_dir, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 5))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(rs.subprocess, "run", fake_run)
    wt = tmp_path / "wt"
    wt.mkdir()

    result = run_in_sandbox(str(wt), "pytest -q", policy=SB_POLICY)

    assert result["timed_out"] is True
    assert result["exit_code"] is None
    assert "wall clock" in result["error"]
    # The docker CLIENT died on timeout; the container must be killed by name.
    run_argv, kill_argv = calls
    name = run_argv[run_argv.index("--name") + 1]
    assert kill_argv == ["docker", "kill", name]


def test_run_in_sandbox_truncates_output(tmp_path, data_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(
        rs.subprocess, "run",
        _fake_run_factory(calls, stdout=b"x" * 5000, stderr=b"y" * 5000),
    )
    wt = tmp_path / "wt"
    wt.mkdir()

    result = run_in_sandbox(str(wt), "pytest", policy=SB_POLICY)

    assert result["stdout_truncated"] is True
    assert result["stderr_truncated"] is True
    assert os.path.getsize(result["stdout_path"]) == 100  # maxOutputBytes
    assert os.path.getsize(result["stderr_path"]) == 100


def test_run_in_sandbox_docker_unavailable(tmp_path, data_dir, monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(rs.subprocess, "run", fake_run)
    wt = tmp_path / "wt"
    wt.mkdir()

    result = run_in_sandbox(str(wt), "pytest", policy=SB_POLICY)
    # Infrastructure failure, not command failure.
    assert result["error"] == "docker_unavailable"
    assert result["allowed"] is True
    assert result["exit_code"] is None


# ---------- ToolCallRecord persistence ----------
def _db():
    engine = sqlalchemy.create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=sqlalchemy.pool.StaticPool,
    )
    cdb.Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine, autoflush=False)()


def _seed_run(db):
    # Commit the task before the run: these models carry no relationship()
    # objects, so a single flush won't FK-order the inserts itself (and
    # PRAGMA foreign_keys=ON is enforced for every engine in this app).
    db.add(cdb.RoutingTask(id="t1", title="t", objective="o",
                           task_type="bug_debug", repo_path="/tmp/nowhere"))
    db.commit()
    db.add(cdb.RoutingRun(id="r1", task_id="t1", status="running"))
    db.commit()
    return "r1"


def test_tool_call_record_persisted_for_allowed(tmp_path, data_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(rs.subprocess, "run", _fake_run_factory(calls))
    db = _db()
    run_id = _seed_run(db)
    wt = tmp_path / "wt"
    wt.mkdir()

    result = run_in_sandbox(str(wt), "pytest -q", policy=SB_POLICY, run_id=run_id,
                            db=db, policy_decision_id="policy-v-test")

    rec = db.get(cdb.ToolCallRecord, result["tool_call_record_id"])
    assert rec is not None
    assert rec.tool_name == "sandbox_cmd"
    assert rec.allowed is True
    assert rec.exit_code == 0
    assert rec.run_id == run_id
    assert rec.stdout_path == result["stdout_path"]
    assert rec.stderr_path == result["stderr_path"]
    assert rec.policy_decision_id == "policy-v-test"
    assert rec.started_at is not None and rec.completed_at is not None
    expected_hash = hashlib.sha256(f"pytest -q\0{wt}".encode()).hexdigest()
    assert rec.args_hash == expected_hash


def test_tool_call_record_persisted_for_denied(tmp_path, data_dir, monkeypatch):
    calls = []
    monkeypatch.setattr(rs.subprocess, "run", _fake_run_factory(calls))
    db = _db()
    run_id = _seed_run(db)
    wt = tmp_path / "wt"
    wt.mkdir()

    result = run_in_sandbox(str(wt), "pytest; rm -rf /", policy=SB_POLICY,
                            run_id=run_id, db=db)

    assert result["allowed"] is False
    assert result["error"] == "command_not_allowed"
    assert calls == [], "a denied command must never reach docker"
    rec = db.get(cdb.ToolCallRecord, result["tool_call_record_id"])
    assert rec is not None
    assert rec.allowed is False
    assert rec.exit_code is None
    assert rec.stdout_path is None and rec.stderr_path is None
    assert rec.policy_decision_id  # defaults to the routing policy version


# ---------- workdir: data_root override ----------
def test_data_root_honors_env_override(data_dir):
    assert data_root() == os.path.realpath(str(data_dir))
    assert worktrees_root() == os.path.join(os.path.realpath(str(data_dir)),
                                            "routing", "worktrees")


# ---------- workdir: worktree lifecycle on a real throwaway repo ----------
PATCH = """diff --git a/hello.txt b/hello.txt
--- a/hello.txt
+++ b/hello.txt
@@ -1 +1 @@
-hello
+goodbye
"""


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


def test_create_worktree_happy(repo, data_dir):
    wt = create_worktree(str(repo), "run-abc123")
    assert wt == os.path.join(worktrees_root(), "run-abc123")
    assert (Path(wt) / "hello.txt").read_text() == "hello\n"
    # Detached checkout of the same commit, registered as a real worktree.
    out = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                         capture_output=True, text=True, check=True)
    assert "run-abc123" in out.stdout


def test_create_worktree_refuses_dirty_source(repo, data_dir):
    (repo / "hello.txt").write_text("dirty edit\n")
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        create_worktree(str(repo), "run-dirty1")
    # Explicit waiver works (--allow-dirty plumbs to this kwarg).
    wt = create_worktree(str(repo), "run-dirty1", allow_dirty=True)
    # The worktree is at HEAD, NOT the dirty state.
    assert (Path(wt) / "hello.txt").read_text() == "hello\n"


def test_create_worktree_rejects_non_repo(tmp_path, data_dir):
    plain = tmp_path / "notarepo"
    plain.mkdir()
    with pytest.raises(ValueError, match="not a git repository"):
        create_worktree(str(plain), "run-abc123")


@pytest.mark.parametrize("bad_id", ["", "../evil", "a/b", "x;y", "a..b", ".hidden", "a b"])
def test_create_worktree_rejects_bad_run_id(repo, data_dir, bad_id):
    with pytest.raises(ValueError, match="invalid run_id"):
        create_worktree(str(repo), bad_id)


def test_apply_patch_happy(repo, data_dir):
    wt = create_worktree(str(repo), "run-apply1")
    result = apply_patch(wt, PATCH)
    assert result["applied"] is True
    assert result["error"] is None
    assert result["changed_files"] == ["hello.txt"]
    assert (Path(wt) / "hello.txt").read_text() == "goodbye\n"
    # The patch file itself must never land inside the worktree.
    assert not list(Path(wt).glob("*.diff"))
    # The SOURCE repo is untouched.
    assert (repo / "hello.txt").read_text() == "hello\n"


def test_apply_patch_rejects_malformed(repo, data_dir):
    wt = create_worktree(str(repo), "run-apply2")
    result = apply_patch(wt, "this is not a unified diff at all\n")
    assert result["applied"] is False
    assert result["error"]
    assert result["changed_files"] == []
    assert (Path(wt) / "hello.txt").read_text() == "hello\n"


def test_apply_patch_check_failure_leaves_tree_clean(repo, data_dir):
    wt = create_worktree(str(repo), "run-apply3")
    # Context doesn't match the file -> git apply --check must refuse.
    bad = PATCH.replace("-hello", "-something else")
    result = apply_patch(wt, bad)
    assert result["applied"] is False
    status = subprocess.run(["git", "-C", wt, "status", "--porcelain"],
                            capture_output=True, text=True, check=True)
    assert status.stdout.strip() == ""


def test_revert_worktree_restores(repo, data_dir):
    wt = create_worktree(str(repo), "run-revert1")
    assert apply_patch(wt, PATCH)["applied"] is True
    (Path(wt) / "untracked.tmp").write_text("junk")
    revert_worktree(wt)
    # Tracked content restored, untracked junk cleaned: pristine base tree.
    assert (Path(wt) / "hello.txt").read_text() == "hello\n"
    assert not (Path(wt) / "untracked.tmp").exists()


def test_remove_worktree_removes_legit(repo, data_dir):
    wt = create_worktree(str(repo), "run-rm1")
    remove_worktree(str(repo), wt)
    assert not os.path.exists(wt)
    out = subprocess.run(["git", "-C", str(repo), "worktree", "list"],
                         capture_output=True, text=True, check=True)
    assert "run-rm1" not in out.stdout


def test_remove_worktree_refuses_outside_jail(repo, tmp_path, data_dir):
    with pytest.raises(ValueError, match="worktree jail"):
        remove_worktree(str(repo), str(tmp_path))
    with pytest.raises(ValueError, match="worktree jail"):
        remove_worktree(str(repo), str(repo))
    # The jail root itself is not removable either.
    os.makedirs(worktrees_root(), exist_ok=True)
    with pytest.raises(ValueError, match="worktree jail"):
        remove_worktree(str(repo), worktrees_root())
