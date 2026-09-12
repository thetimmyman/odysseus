"""Pi 0.85.1 run-settlement semantics at the Odysseus adapter boundary.

Pi 0.85.1 ends EVERY attempt with ``agent_end`` and follows the final one with
``agent_settled``; a failed model/provider run may STILL exit rc=0. These tests
pin the adapter's terminal classification so a dead endpoint or an invalid model
can never be recorded as a successful ``completed`` execution.

    A 0.85.1 success (agent_end + agent_settled)         -> completed
    B 0.85.1 retry-then-success (settled)                -> completed
    C 0.85.1 retry exhaustion (settled, rc=0, no output) -> provider_failure
    D dead endpoint / invalid model shape (settled rc=0) -> not completed
    E 0.74.2 legacy (agent_end only, no agent_settled)   -> completed
    F cancel                                             -> cancelled
    G resume (settled)                                   -> still resumes
"""
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.pi_config as pc  # noqa: E402
import src.pi_event_map as pem  # noqa: E402
import src.pi_executions as pe  # noqa: E402
from src.pi_runtime import PiRuntime  # noqa: E402

STUB = str(Path(__file__).resolve().parent / "helpers" / "pi_stub.py")


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    (r / "a.txt").write_text("alpha\n")
    _git(r, "add", ".")
    _git(r, "commit", "-qm", "init")
    return r


@pytest.fixture
def worktree(repo, tmp_path):
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", str(wt), "-b", "task/pi-one")
    return wt


@pytest.fixture
def pi_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(data))
    monkeypatch.setenv("ODYSSEUS_PI_BIN", STUB)
    monkeypatch.setenv("ODYSSEUS_PI_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("ODYSSEUS_PI_MODELS_JSON", str(tmp_path / "models.json"))
    return data


def configure(worktree, **cfg):
    (Path(worktree) / ".pi_stub.json").write_text(json.dumps(cfg))


async def wait_terminal(runtime, execution_id, timeout=25.0):
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        status = runtime.status(execution_id)
        if status.get("status") in pe.TERMINAL_STATUSES:
            return status
        await asyncio.sleep(0.05)
    return runtime.status(execution_id)


# --- event mapping ---------------------------------------------------------
def test_agent_settled_maps_to_run_settled():
    mapped = pem.map_pi_event({"type": "agent_settled", "messages": [{"role": "assistant"}]})
    assert [m["type"] for m in mapped] == [pem.RUN_SETTLED]


# --- Node 22 launcher determinism -----------------------------------------
def test_pi_environment_prepends_pi_launch_dir(monkeypatch):
    # An absolute pi binary (as the adapter uses for the stub, and as an
    # operator may set for a Nix/wrapper install) pins its own directory first,
    # so its sibling node - the runtime Pi actually uses - wins over any other
    # node earlier in the inherited PATH.
    monkeypatch.setenv("ODYSSEUS_PI_BIN", "/opt/pin/bin/pi")
    monkeypatch.setenv("PATH", "/mise/node20/bin:/usr/bin:/bin")
    env = pc.pi_environment()
    assert env["PATH"].split(os.pathsep)[0] == "/opt/pin/bin"
    assert env["PATH"].split(os.pathsep)[1:3] == ["/mise/node20/bin", "/usr/bin"]


# --- A: 0.85.1 success -----------------------------------------------------
async def test_a_pi_085_success_settles_completed(repo, worktree, pi_env):
    configure(worktree, session_id="stub-settle-a", settle=True, linger=True)
    runtime = PiRuntime()
    record = await runtime.start(task="Do the work.", worktree=str(worktree))
    final = await wait_terminal(runtime, record["execution_id"])
    assert final["status"] == pe.STATUS_COMPLETED
    assert final["worktree_verified"] is True
    assert runtime.result(record["execution_id"])["final_text"]
    events = {e["type"] for e in runtime.events(record["execution_id"])}
    assert pem.RUN_SETTLED in events


# --- B: retry then success -------------------------------------------------
async def test_b_pi_085_retry_then_success_is_completed(repo, worktree, pi_env):
    configure(worktree, session_id="stub-settle-b", scenario="retry_then_ok",
              settle=True, linger=True)
    runtime = PiRuntime()
    record = await runtime.start(task="Recover from a flaky attempt.", worktree=str(worktree))
    final = await wait_terminal(runtime, record["execution_id"])
    assert final["status"] == pe.STATUS_COMPLETED
    assert runtime.result(record["execution_id"])["final_text"]
    events = [e["type"] for e in runtime.events(record["execution_id"])]
    assert pem.WARNING in events          # the retry was observed
    assert pem.COMPLETION in events


# --- C: retry exhaustion ---------------------------------------------------
async def test_c_pi_085_retry_exhaustion_is_provider_failure(repo, worktree, pi_env):
    configure(worktree, session_id="stub-settle-c", scenario="retry_exhausted",
              attempts=3, settle=True, linger=True, exit_code=0)
    runtime = PiRuntime()
    record = await runtime.start(task="Will never succeed.", worktree=str(worktree))
    final = await wait_terminal(runtime, record["execution_id"])
    assert final["status"] == pe.STATUS_PROVIDER_FAILURE
    assert final["status"] != pe.STATUS_COMPLETED
    assert final["failure_class"] == "provider_failure"
    assert not runtime.result(record["execution_id"])["final_text"]


# --- D: dead endpoint / invalid model shape --------------------------------
async def test_d_settled_rc0_without_output_is_never_completed(repo, worktree, pi_env):
    # Exact bug shape: agent_end per attempt + auto_retry_start + agent_settled,
    # process exits rc=0, no assistant output. Must fail closed.
    configure(worktree, session_id="stub-settle-d", scenario="retry_exhausted",
              attempts=2, settle=True, exit_code=0)
    runtime = PiRuntime()
    record = await runtime.start(task="Dead endpoint.", worktree=str(worktree))
    final = await wait_terminal(runtime, record["execution_id"])
    assert final["status"] != pe.STATUS_COMPLETED
    assert final["failure_class"] == "provider_failure"


# --- E: 0.74.2 legacy ------------------------------------------------------
async def test_e_pi_074_legacy_success_is_completed(repo, worktree, pi_env):
    # No agent_settled and immediate exit: agent_end / rc=0 ends the run.
    configure(worktree, session_id="stub-settle-e", settle=False)
    runtime = PiRuntime()
    record = await runtime.start(task="Legacy run.", worktree=str(worktree))
    final = await wait_terminal(runtime, record["execution_id"])
    assert final["status"] == pe.STATUS_COMPLETED
    events = {e["type"] for e in runtime.events(record["execution_id"])}
    assert pem.RUN_SETTLED not in events


# --- F: cancel -------------------------------------------------------------
async def test_f_cancel_still_cancelled(repo, worktree, pi_env):
    configure(worktree, session_id="stub-settle-f", scenario="slow")
    runtime = PiRuntime()
    record = await runtime.start(task="Long run.", worktree=str(worktree))
    await asyncio.sleep(0.5)
    assert await runtime.cancel(record["execution_id"]) is True
    final = await wait_terminal(runtime, record["execution_id"])
    assert final["status"] == pe.STATUS_CANCELLED


# --- G: resume -------------------------------------------------------------
async def test_g_resume_after_settled_run(repo, worktree, pi_env):
    configure(worktree, session_id="stub-settle-g", settle=True, linger=True)
    runtime = PiRuntime()
    record = await runtime.start(task="Step one.", worktree=str(worktree))
    eid = record["execution_id"]
    first = await wait_terminal(runtime, eid)
    assert first["status"] == pe.STATUS_COMPLETED
    session_id = first["pi_session_id"]

    resumed_runtime = PiRuntime()
    await resumed_runtime.resume(eid, message="Step two.")
    terminal = await wait_terminal(resumed_runtime, eid)
    assert terminal["status"] == pe.STATUS_COMPLETED
    assert terminal["pi_session_id"] == session_id
    assert terminal["worktree_verified"] is True


# --- D2/D3: invalid-model shape (silent settle, no retry, no output) -------
async def test_d2_silent_settle_noop_is_not_completed(repo, worktree, pi_env):
    # No retry event, no output, no tool call, settles and exits 0. Still fails.
    configure(worktree, session_id="stub-noop-d2", scenario="settle_noop",
              settle=True, exit_code=0)
    runtime = PiRuntime()
    record = await runtime.start(task="Invalid model.", worktree=str(worktree))
    final = await wait_terminal(runtime, record["execution_id"])
    assert final["status"] != pe.STATUS_COMPLETED
    assert final["status"] == pe.STATUS_RUNTIME_FAILURE
    assert final["failure_class"] == "runtime_failure"


async def test_d3_silent_settle_with_model_error_is_provider_failure(repo, worktree, pi_env):
    # Same silent settle but with Pi's merged-stderr model warning present.
    configure(worktree, session_id="stub-noop-d3", scenario="settle_noop",
              settle=True, exit_code=0,
              stderr_note='Warning: Model "no-such-model-xyz" not found for provider "local-qwen". Using custom model id.')
    runtime = PiRuntime()
    record = await runtime.start(task="Invalid model.", worktree=str(worktree))
    final = await wait_terminal(runtime, record["execution_id"])
    assert final["status"] == pe.STATUS_PROVIDER_FAILURE
    assert final["failure_class"] == "provider_failure"


def test_pi_environment_moves_launch_dir_to_front_when_present(monkeypatch):
    # The hazard PATH has the nvm bin present but NOT first (mise Node 20 ahead
    # of it); the launcher must still promote the pi directory to the front.
    monkeypatch.setenv("ODYSSEUS_PI_BIN", "/opt/pin/bin/pi")
    monkeypatch.setenv("PATH", "/mise/node20/bin:/opt/pin/bin:/usr/bin")
    env = pc.pi_environment()
    parts = env["PATH"].split(os.pathsep)
    assert parts[0] == "/opt/pin/bin"
    assert parts.count("/opt/pin/bin") == 1
    assert "/mise/node20/bin" in parts


async def test_d4_error_stop_reason_is_provider_failure(repo, worktree, pi_env):
    # Pi 0.85.1 carries stopReason=error + errorMessage on the failed message /
    # agent_end: protocol-native evidence, no stderr parsing needed.
    configure(worktree, session_id="stub-noop-d4", scenario="settle_noop",
              settle=True, exit_code=0,
              error_stop='404: {"message":"model not found"}')
    runtime = PiRuntime()
    record = await runtime.start(task="Invalid model.", worktree=str(worktree))
    final = await wait_terminal(runtime, record["execution_id"])
    assert final["status"] == pe.STATUS_PROVIDER_FAILURE
    assert final["failure_class"] == "provider_failure"
    assert "model not found" in (final["failure_reason"] or "")


async def test_h_transient_tool_error_does_not_fail_successful_run(repo, worktree, pi_env):
    # A tool error mid-run must not relabel an otherwise successful, settled run
    # (the real local-Qwen completion path can hit a transient command failure).
    configure(worktree, session_id="stub-ok-toolerr", settle=True, linger=True,
              tool_error=True)
    runtime = PiRuntime()
    record = await runtime.start(task="Do work with a transient tool error.",
                                 worktree=str(worktree))
    final = await wait_terminal(runtime, record["execution_id"])
    assert final["status"] == pe.STATUS_COMPLETED
    assert runtime.result(record["execution_id"])["final_text"]
