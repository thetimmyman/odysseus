"""Phase 3 "Safe Execution" (spec Section 15): sandbox command allowlist +
metacharacter rejection, docker argv construction (network off, single rw
worktree mount, caps dropped), wall-clock timeout -> docker kill, output
truncation, ToolCallRecord persistence for allowed AND denied attempts, and
the git worktree lifecycle (clean-tree requirement, jailed create/remove,
apply --check, failed-patch revert). Never calls real docker."""
import hashlib
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import core.database as cdb
from tests.helpers.docker_cli import FakeDocker, block_native_transports

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


@pytest.fixture(autouse=True)
def no_native_transports(monkeypatch):
    block_native_transports(monkeypatch)


def sandbox_fixture(tmp_path, monkeypatch, *, stdout=b'ok', stderr=b''):
    calls = []
    docker = FakeDocker(calls, stdout=stdout, stderr=stderr)
    docker.artifacts = tmp_path / 'artifacts'
    monkeypatch.setattr(rs, '_bounded_docker', docker)
    work = tmp_path / 'work'
    work.mkdir()
    return docker, work


def test_run_in_sandbox_argv(tmp_path, data_dir, monkeypatch):
    docker, wt = sandbox_fixture(tmp_path, monkeypatch)
    result = run_in_sandbox(str(wt), 'pytest -q', policy=SB_POLICY, artifacts_dir=str(docker.artifacts))
    assert result['allowed'] and result['exit_code'] == 0 and not result['timed_out']
    argv = next(argv for argv, _ in docker.calls if argv[1] == 'create')
    assert argv[:2] == ['docker', 'create'] and '--rm' not in argv
    assert argv[argv.index('--network')+1] == 'none'
    assert argv.count('-v') == 1 and argv[argv.index('-v')+1] == f'{wt}:/work:rw'
    assert '--mount' not in argv and '--privileged' not in argv
    assert argv[argv.index('--cap-drop')+1] == 'ALL'
    assert argv[argv.index('--security-opt')+1] == 'no-new-privileges'
    assert argv[argv.index('--user')+1] == f'{os.getuid()}:{os.getgid()}'
    assert argv[argv.index('--cpus')+1] == '1'
    assert argv[argv.index('--memory')+1] == '2g'
    assert argv[argv.index('--pids-limit')+1] == '64'
    assert argv[argv.index('--tmpfs')+1] == '/tmp'
    assert argv[argv.index(docker.image)+1:] == ['pytest', '-q']
    assert result['container_id'] == docker.cid and result['image_id'] == docker.image
    assert result['cleanup_confirmed'] is True
    assert Path(result['stdout_path']).read_bytes() == b'ok'


@pytest.mark.parametrize('requested,expected', [(2, 2), (30, 5)])
def test_run_in_sandbox_packet_deadline_can_only_tighten_policy(tmp_path, data_dir, monkeypatch, requested, expected):
    docker, wt = sandbox_fixture(tmp_path, monkeypatch)
    result = run_in_sandbox(str(wt), 'pytest -q', policy=SB_POLICY, timeout_s=requested, artifacts_dir=str(docker.artifacts))
    assert all(0 < options['timeout'] <= expected for _, options in docker.calls)
    assert result['timeout_s'] == expected


def test_run_in_sandbox_mount_label_applied(tmp_path, data_dir, monkeypatch):
    docker, wt = sandbox_fixture(tmp_path, monkeypatch)
    labeled = {'sandbox': dict(SB_POLICY['sandbox'], mountLabel='z')}
    run_in_sandbox(str(wt), 'pytest -q', policy=labeled, artifacts_dir=str(docker.artifacts))
    argv = next(argv for argv, _ in docker.calls if argv[1] == 'create')
    assert argv[argv.index('-v')+1] == f'{wt}:/work:rw,z'


@pytest.mark.parametrize('fault', ['timeout', 'lost-create-ack', 'foreign-label', 'cleanup-failed'])
def test_unknown_container_never_starts_or_gets_destroyed_by_name(tmp_path, data_dir, monkeypatch, fault):
    docker, wt = sandbox_fixture(tmp_path, monkeypatch)
    docker.fault = fault
    result = run_in_sandbox(str(wt), 'pytest -q', policy=SB_POLICY, artifacts_dir=str(docker.artifacts))
    commands = [argv for argv, _ in docker.calls]
    assert all(argv[-1] == docker.cid for argv in commands if argv[1] in ('start', 'rm'))
    assert not any('name=' in part for argv in commands for part in argv)
    if fault in ('lost-create-ack', 'foreign-label'):
        assert not any(argv[1] in ('start', 'rm') for argv in commands)
    assert result['cleanup_confirmed'] is (fault == 'timeout')
    if fault == 'timeout':
        assert result['timed_out'] and 'wall clock' in result['error']
    else:
        assert result['error'] is not None and result['cleanup_state'] == 'UNKNOWN'


def test_run_in_sandbox_truncates_output(tmp_path, data_dir, monkeypatch):
    docker, wt = sandbox_fixture(tmp_path, monkeypatch, stdout=b'x'*5000, stderr=b'y'*5000)
    result = run_in_sandbox(str(wt), 'pytest', policy=SB_POLICY, artifacts_dir=str(docker.artifacts))
    assert result['stdout_truncated'] and result['stderr_truncated']
    assert os.path.getsize(result['stdout_path']) == 100 and os.path.getsize(result['stderr_path']) == 100
    assert result['cleanup_confirmed'] and result['error'] == 'sandbox output limit exceeded'


def test_run_in_sandbox_docker_unavailable(tmp_path, data_dir, monkeypatch):
    def unavailable(*args, **options):
        raise FileNotFoundError('docker')
    monkeypatch.setattr(rs, '_bounded_docker', unavailable)
    wt = tmp_path / 'wt'; wt.mkdir()
    result = run_in_sandbox(str(wt), 'pytest', policy=SB_POLICY)
    assert result['error'] == 'docker_unavailable' and result['exit_code'] is None


@pytest.mark.parametrize('stream', ['stdout', 'stderr'])
def test_stream_limit_stops_client_before_buffering_unbounded_output(stream, monkeypatch):
    original = subprocess.Popen
    children = []

    def owned_child(*args, **options):
        child = original(*args, **options)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, 'Popen', owned_child)
    script = "import os; " + ('os.write(1,b"x"*1000000)' if stream == 'stdout' else 'os.write(2,b"x"*1000000)')
    observed = rs._bounded_docker([sys.executable, '-I', '-c', script], timeout=2, limit=100)
    assert len(observed[stream]) <= 100
    assert observed[stream + '_truncated'] is True
    assert len(children) == 1
    child = children[0]
    assert child.stdout.closed and child.stderr.closed
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)
    with pytest.raises(ProcessLookupError):
        os.killpg(child.pid, 0)


@pytest.mark.parametrize('close_at', ['selector', 'stdout'])
def test_capture_close_failure_holds_but_still_kills_and_reaps_owned_child(monkeypatch, close_at, record_property):
    original_popen = subprocess.Popen
    original_selector = rs.selectors.DefaultSelector
    children = []
    attempts = []
    original_killpg = os.killpg
    close_failure = OSError('synthetic selector close failure')

    class FailingPipe:
        def __init__(self, pipe, label):
            self.pipe = pipe
            self.label = label
            self.raised = False

        def __getattr__(self, name):
            return getattr(self.pipe, name)

        def close(self):
            attempts.append(self.label + '-close')
            self.pipe.close()
            if self.label == close_at and not self.raised:
                self.raised = True
                raise close_failure

    def owned_child(*args, **options):
        child = original_popen(*args, **options)
        child.stdout = FailingPipe(child.stdout, 'stdout')
        child.stderr = FailingPipe(child.stderr, 'stderr')
        original_wait = child.wait

        def observed_wait(*args, **options):
            attempts.append('wait')
            return original_wait(*args, **options)

        child.wait = observed_wait
        children.append(child)
        return child

    class FailingClose:
        def __init__(self):
            self.selector = original_selector()

        def __getattr__(self, name):
            return getattr(self.selector, name)

        def close(self):
            attempts.append('selector-close')
            self.selector.close()
            if close_at == 'selector':
                raise close_failure

    def observed_killpg(pid, signum):
        if signum != 0:
            attempts.append('kill-group')
        return original_killpg(pid, signum)

    monkeypatch.setattr(subprocess, 'Popen', owned_child)
    monkeypatch.setattr(rs.selectors, 'DefaultSelector', FailingClose)
    monkeypatch.setattr(os, 'killpg', observed_killpg)
    try:
        with pytest.raises(RuntimeError, match='client cleanup unresolved') as failure:
            rs._bounded_docker([sys.executable, '-I', '-c', 'import os; os.write(1,b"x"*1000000)'],
                               timeout=2, limit=100)
        assert failure.value.__cause__ is close_failure
        record_property('cleanup_attempts_before_test_recovery', repr(attempts))
        record_property('original_cause_preserved', True)
        record_property('result_held_cleanup_unknown', True)
        assert attempts == ['selector-close', 'stdout-close', 'stderr-close', 'kill-group', 'wait']
        assert len(children) == 1
        child = children[0]
        assert child.stdout.closed and child.stderr.closed
        with pytest.raises(ChildProcessError):
            os.waitpid(child.pid, os.WNOHANG)
        with pytest.raises(ProcessLookupError):
            os.killpg(child.pid, 0)
    finally:
        # Also contain a deliberately broken pre-fix implementation: this
        # test owns this synthetic group and never signals any other PID.
        for child in children:
            if child.returncode is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait(timeout=1)
            child.stdout.close(); child.stderr.close()


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
    docker, wt = sandbox_fixture(tmp_path, monkeypatch)
    db = _db()
    run_id = _seed_run(db)
    result = run_in_sandbox(str(wt), "pytest -q", policy=SB_POLICY, run_id=run_id,
                            db=db, policy_decision_id="policy-v-test", artifacts_dir=str(docker.artifacts))

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
    docker, wt = sandbox_fixture(tmp_path, monkeypatch)
    calls = docker.calls
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


def test_data_root_honors_env_override(data_dir):
    assert data_root() == os.path.realpath(str(data_dir))
    assert worktrees_root() == os.path.join(os.path.realpath(str(data_dir)),
                                            "routing", "worktrees")


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
