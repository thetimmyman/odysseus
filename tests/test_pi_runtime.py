"""Tests A–I at the Odysseus ⇄ Pi adapter boundary (``src/pi_runtime.py``).

These run against ``tests/helpers/pi_stub.py`` — a stand-in ``pi`` binary that
speaks the verified Pi 0.74.2 RPC protocol without needing a live model, so the
adapter's start/observe/status/send/cancel/result/resume contract and its
least-privilege launch environment can be asserted deterministically.

    A Start            B Identity           C Worktree isolation
    D Events           E Completion         F Failure
    G Cancellation     H Resume             I Routing authority

Plus worktree-assignment enforcement (Odysseus owns the worktree; Pi may only
operate inside the worktree explicitly assigned to that execution):

    J fresh execution uses the assigned worktree
    K stale/remembered Pi cwd is refused (fail closed, task never sent)
    L mismatched session cwd blocks execution
    M resume preserves the original assigned worktree
    N one task cannot resume into another task's worktree
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
from src.pi_runtime import PiRuntime, WorktreeMismatch  # noqa: E402

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
    """Point the adapter at the stub and a throwaway data root.

    Premium provider credentials are present in the PARENT environment so test I
    can prove they never reach a Pi execution.
    """
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(data))
    monkeypatch.setenv("ODYSSEUS_PI_BIN", STUB)
    monkeypatch.setenv("ODYSSEUS_PI_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setenv("ODYSSEUS_PI_MODELS_JSON", str(tmp_path / "models.json"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-openrouter-premium")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-anthropic-premium")
    monkeypatch.setenv("ODYSSEUS_INTERNAL_TOKEN", "ody_internal_secret")
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
        await asyncio.sleep(0.1)
    return runtime.status(execution_id)
# --- Test A — Start --------------------------------------------------------
async def test_a_start_creates_execution_record(repo, worktree, pi_env):
    configure(worktree, session_id="stub-session-a")
    runtime = PiRuntime()
    record = await runtime.start(
        task="Add a Jira integration settings entry.",
        worktree=str(worktree),
        model="local-qwen3.8-27b",
        constraints=["Do not touch production migrations."],
        odysseus_run_id="run-42",
        jira_ticket="PS-999",
    )
    assert record["execution_id"]
    assert record["worktree"] == os.path.realpath(worktree)
    assert record["branch"] == "task/pi-one"
    assert record["base_commit"]
    assert record["model"] == pc.DEFAULT_PI_MODEL_ID
    assert record["provider"] == pc.DEFAULT_PI_PROVIDER

    final = await wait_terminal(runtime, record["execution_id"])
    assert final["status"] == pe.STATUS_COMPLETED


# --- Test B — Identity -----------------------------------------------------
async def test_b_execution_id_maps_to_pi_session_id(repo, worktree, pi_env):
    configure(worktree, session_id="stub-session-b")
    runtime = PiRuntime()
    record = await runtime.start(task="Do a small change.", worktree=str(worktree))
    eid = record["execution_id"]
    final = await wait_terminal(runtime, eid)

    assert final["pi_session_id"] == "stub-session-b"
    assert final["pi_session_id"] != eid
    assert final["odysseus_run_id"] == eid
    # Persisted, not just in-memory.
    on_disk = pe.get_execution(eid)
    assert on_disk["pi_session_id"] == "stub-session-b"
    assert on_disk["pi_session_file"]


# --- Test C — Worktree isolation -------------------------------------------
async def test_c_pi_edits_only_the_assigned_worktree(repo, worktree, pi_env):
    configure(worktree, session_id="stub-session-c", write_files=["stub_output.txt"])
    runtime = PiRuntime()
    record = await runtime.start(task="Write a file.", worktree=str(worktree))
    eid = record["execution_id"]
    await wait_terminal(runtime, eid)

    assert (worktree / "stub_output.txt").exists()
    # The source repo (and its main worktree) are untouched.
    assert not (repo / "stub_output.txt").exists()
    result = runtime.result(eid)
    assert "stub_output.txt" in result["files_changed"]


# --- Test D — Events -------------------------------------------------------
async def test_d_useful_events_are_observable(repo, worktree, pi_env):
    configure(worktree, session_id="stub-session-d", run_command="pytest -q",
              write_files=["mod.py"])
    runtime = PiRuntime()
    record = await runtime.start(task="Implement and test.", worktree=str(worktree))
    eid = record["execution_id"]
    await wait_terminal(runtime, eid)

    events = runtime.events(eid)
    types = {e["type"] for e in events}
    assert pem.EXECUTION_STARTED in types
    assert pem.TEST_EXECUTION in types        # bash running pytest was classified
    assert pem.FILE_MODIFICATION in types     # write tool classified
    assert pem.TOOL_RESULT in types
    assert pem.COMPLETION in types
    assert pem.MODEL_TURN in types
    # Ledger is durable on disk too.
    assert pe.read_events(eid) == events and events



# --- Test E — Completion ---------------------------------------------------
async def test_e_success_produces_explicit_completed_result(repo, worktree, pi_env):
    configure(worktree, session_id="stub-session-e")
    runtime = PiRuntime()
    record = await runtime.start(task="Finish the work.", worktree=str(worktree))
    eid = record["execution_id"]
    await wait_terminal(runtime, eid)

    result = runtime.result(eid)
    assert result["status"] == pe.STATUS_COMPLETED
    assert result["failure_class"] is None
    assert "Implemented the change" in (result["final_text"] or "")
    assert result["ended_at"]


# --- Test F — Failure ------------------------------------------------------
@pytest.mark.parametrize("scenario,expected", [
    ("provider_fail", pe.STATUS_PROVIDER_FAILURE),
    ("tool_fail", pe.STATUS_TOOL_FAILURE),
    ("instant_fail", pe.STATUS_RUNTIME_FAILURE),
])
async def test_f_failures_map_to_explicit_states(repo, worktree, pi_env, scenario, expected):
    configure(worktree, scenario=scenario)
    runtime = PiRuntime()
    record = await runtime.start(task="This one fails.", worktree=str(worktree))
    eid = record["execution_id"]
    final = await wait_terminal(runtime, eid)

    assert final["status"] == expected, f"{scenario} -> {final['status']}"
    assert final["status"] != pe.STATUS_COMPLETED
    assert final["failure_class"]
    assert final["failure_reason"]
    # No silent re-route: the record names the runtime/model that failed.
    assert final["runtime"] == pc.RUNTIME_PI
    result = runtime.result(eid)
    assert result["status"] == expected


# --- Test G — Cancellation -------------------------------------------------
async def test_g_cancel_stops_an_active_execution(repo, worktree, pi_env):
    configure(worktree, scenario="slow")
    runtime = PiRuntime()
    record = await runtime.start(task="Long running.", worktree=str(worktree))
    eid = record["execution_id"]
    assert runtime.status(eid)["status"] == pe.STATUS_RUNNING

    cancelled = await runtime.cancel(eid)
    assert cancelled
    final = await wait_terminal(runtime, eid)
    assert final["status"] == pe.STATUS_CANCELLED
    assert not final["runtime_alive"]
# --- Adapter surface smoke test -------------------------------------------
def test_ensure_model_config_does_not_clobber_existing_endpoint(tmp_path, monkeypatch):
    """An operator/deployment-managed endpoint must survive a config rewrite."""
    models_json = tmp_path / "models.json"
    models_json.write_text(json.dumps({
        "providers": {"local-qwen": {"baseUrl": "http://framework.example:11434/v1",
                                     "models": [{"id": "qwen3.8:27b"}]}}
    }))
    monkeypatch.setenv("ODYSSEUS_PI_MODELS_JSON", str(models_json))
    monkeypatch.setenv("ODYSSEUS_PI_LOCAL_BASE_URL", "http://localhost:11434/v1")

    pc.ensure_pi_model_config()

    data = json.loads(models_json.read_text())
    entry = data["providers"]["local-qwen"]
    assert entry["baseUrl"] == "http://framework.example:11434/v1"
    assert entry["api"] == "openai-completions"
    assert entry["compat"]["supportsDeveloperRole"] is False


def test_hardened_agent_dir_isolates_pi_config(tmp_path, monkeypatch):
    """A dedicated agent dir keeps Pi off the operator's premium credentials."""
    agent = tmp_path / "pi-agent"
    monkeypatch.setenv("ODYSSEUS_PI_AGENT_DIR", str(agent))
    monkeypatch.delenv("ODYSSEUS_PI_MODELS_JSON", raising=False)

    written = pc.ensure_pi_model_config()

    assert written == str(agent / "models.json")
    assert (agent / "models.json").exists()
    assert not (agent / "auth.json").exists()
    env = pc.pi_environment()
    assert env["PI_CODING_AGENT_DIR"] == str(agent)
    assert not [k for k in env if k.startswith("ODYSSEUS_")]


def test_route_surface_exposes_adapter_contract():
    """The operator API exposes start/observe/status/send/cancel/result/resume."""
    from routes.pi_runtime_routes import setup_pi_runtime_routes

    paths = {getattr(r, "path", "") for r in setup_pi_runtime_routes().routes}
    assert "/api/pi/executions" in paths
    assert "/api/pi/executions/{execution_id}" in paths
    assert "/api/pi/executions/{execution_id}/events" in paths
    assert "/api/pi/executions/{execution_id}/result" in paths
    assert "/api/pi/executions/{execution_id}/send" in paths
    assert "/api/pi/executions/{execution_id}/cancel" in paths
    assert "/api/pi/executions/{execution_id}/resume" in paths



# --- Test H — Resume -------------------------------------------------------
async def test_h_resume_continues_the_same_pi_session(repo, worktree, pi_env):
    configure(worktree, session_id="stub-session-h")
    runtime = PiRuntime()
    record = await runtime.start(task="Step one.", worktree=str(worktree))
    eid = record["execution_id"]
    final = await wait_terminal(runtime, eid)
    session_file = final["pi_session_file"]
    assert session_file

    # A fresh runtime (process state gone) resumes the SAME execution.
    resumed_runtime = PiRuntime()
    resumed = await resumed_runtime.resume(eid, message="Continue with step two.")
    assert resumed is not None

    terminal = await wait_terminal(resumed_runtime, eid)
    assert terminal["status"] == pe.STATUS_COMPLETED
    # Same Pi session, same Odysseus execution.
    assert terminal["pi_session_id"] == final["pi_session_id"]
    args = json.loads((worktree / ".pi_stub_args.json").read_text())
    assert "--session" in args
    assert args[args.index("--session") + 1] == session_file


# --- Test I — Routing authority --------------------------------------------
async def test_i_pi_cannot_touch_routing_or_secrets(repo, worktree, pi_env):
    configure(worktree, session_id="stub-session-i")
    runtime = PiRuntime()
    record = await runtime.start(task="Small change.", worktree=str(worktree))
    await wait_terminal(runtime, record["execution_id"])

    # 1. Pi's environment carries no premium/provider credentials and no
    #    Odysseus internals, even though the parent has them.
    child_env = json.loads((worktree / ".pi_stub_env.json").read_text())
    for forbidden in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "ODYSSEUS_INTERNAL_TOKEN"):
        assert forbidden not in child_env, f"{forbidden} leaked into Pi"
    assert not [k for k in child_env if k.startswith("ODYSSEUS_")]

    # 2. Pi's models.json exposes only the local execution provider.
    models = json.loads(Path(pc.models_config_path()).read_text())
    assert set(models["providers"]) == {pc.DEFAULT_PI_PROVIDER}

    # 3. The adapter surface has no budget/policy mutation authority.
    forbidden_attrs = ("set_budget", "set_policy", "change_routing", "set_model_policy",
                       "authorize_deploy", "set_provider", "grant")
    for attr in forbidden_attrs:
        assert not hasattr(runtime, attr), f"adapter must not expose {attr}"

    # 4. Runtime selection is external; native remains the default.
    assert pc.execution_runtime() == pc.RUNTIME_NATIVE



# --- Worktree assignment enforcement ---------------------------------------
def prompts_sent():
    """Prompts actually delivered to the stub Pi (cwd-independent log)."""
    path = os.path.join(pc.session_dir(), "pi_stub_prompts.jsonl")
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


async def test_j_fresh_execution_uses_assigned_worktree(repo, worktree, pi_env):
    """A fresh run is launched in, and verified against, the assigned worktree."""
    configure(worktree, session_id="stub-session-j")
    runtime = PiRuntime()
    record = await runtime.start(task="Do the work.", worktree=str(worktree))
    eid = record["execution_id"]
    final = await wait_terminal(runtime, eid)

    assigned = os.path.realpath(worktree)
    assert final["status"] == pe.STATUS_COMPLETED
    assert final["assigned_worktree"] == assigned
    assert final["worktree"] == assigned
    assert final["actual_worktree"] == assigned          # where Pi really ran
    assert final["pi_session_cwd"] == assigned           # what Pi itself recorded
    assert final["worktree_verified"] is True
    assert final["repo_toplevel"] == assigned
    assert final["starting_sha"] == final["base_commit"]
    assert final["pi_session_id"] == "stub-session-j"
    assert final["verification"]["process_cwd"] == assigned
    assert len(prompts_sent()) == 1                       # task was delivered once


async def test_k_stale_pi_cwd_is_refused(repo, worktree, tmp_path, pi_env):
    """A Pi that lands in a remembered/stale directory never gets the task."""
    stale = tmp_path / "stale-pi-worktree"
    stale.mkdir()
    (stale / "leftover.txt").write_text("previous task\n")
    configure(worktree, session_id="stub-session-k", chdir_to=str(stale))

    runtime = PiRuntime()
    with pytest.raises(WorktreeMismatch) as excinfo:
        await runtime.start(task="Should never run.", worktree=str(worktree))

    eid = excinfo.value.execution_id
    assert eid
    record = pe.get_execution(eid)
    assert record["status"] == pe.STATUS_WORKTREE_MISMATCH
    assert record["failure_class"] == "worktree_mismatch"
    assert record["worktree_verified"] is False
    assert record["assigned_worktree"] == os.path.realpath(worktree)
    assert prompts_sent() == []                    # fail closed: no task sent
    assert not (worktree / "stub_output.txt").exists()


async def test_l_mismatched_session_cwd_blocks_execution(repo, worktree, tmp_path, pi_env):
    """A session whose recorded cwd is elsewhere is refused outright."""
    other = tmp_path / "somewhere-else"
    other.mkdir()
    configure(worktree, session_id="stub-session-l", session_cwd=str(other))

    runtime = PiRuntime()
    with pytest.raises(WorktreeMismatch) as excinfo:
        await runtime.start(task="Should never run.", worktree=str(worktree))

    record = pe.get_execution(excinfo.value.execution_id)
    assert record["status"] == pe.STATUS_WORKTREE_MISMATCH
    assert prompts_sent() == []
    assert "assigned worktree" in (record["failure_reason"] or "")


async def test_m_resume_preserves_assigned_worktree(repo, worktree, pi_env):
    """Resume re-derives the worktree from the record, not from Pi's memory."""
    configure(worktree, session_id="stub-session-m")
    runtime = PiRuntime()
    record = await runtime.start(task="Step one.", worktree=str(worktree))
    eid = record["execution_id"]
    final = await wait_terminal(runtime, eid)
    assert final["status"] == pe.STATUS_COMPLETED
    session_file = final["pi_session_file"]
    assert session_file

    # A brand-new runtime resumes: no in-memory state to lean on.
    resumed_runtime = PiRuntime()
    await resumed_runtime.resume(eid, message="Step two.")
    terminal = await wait_terminal(resumed_runtime, eid)

    assigned = os.path.realpath(worktree)
    assert terminal["status"] == pe.STATUS_COMPLETED
    assert terminal["assigned_worktree"] == assigned
    assert terminal["worktree"] == assigned
    assert terminal["pi_session_cwd"] == assigned
    assert terminal["worktree_verified"] is True
    assert terminal["pi_session_id"] == final["pi_session_id"]   # same session
    args = json.loads((worktree / ".pi_stub_args.json").read_text())
    assert args[args.index("--session") + 1] == session_file


async def test_o_late_recorded_session_cwd_is_caught(repo, worktree, tmp_path, pi_env):
    """Defense in depth for Pi's lazily-written session file.

    Real Pi may only write its session file once a run starts, so the live check
    cannot see the recorded cwd beforehand. When it turns out to point at another
    worktree, the run is refused rather than reported as done.
    """
    other = tmp_path / "elsewhere"
    other.mkdir()
    configure(worktree, session_id="stub-session-o", session_cwd=str(other),
              session_file_late=True)

    runtime = PiRuntime()
    record = await runtime.start(task="Small change.", worktree=str(worktree))
    final = await wait_terminal(runtime, record["execution_id"])

    assert final["status"] == pe.STATUS_WORKTREE_MISMATCH
    assert final["failure_class"] == "worktree_mismatch"
    assert final["worktree_verified"] is False
    assert final["pi_session_cwd"] == os.path.realpath(other)


async def test_p_advanced_branch_is_accepted(repo, worktree, pi_env):
    """A branch that advanced from the assigned starting SHA stays valid."""
    configure(worktree, session_id="stub-session-p")
    runtime = PiRuntime()
    record = await runtime.start(task="Step one.", worktree=str(worktree))
    eid = record["execution_id"]
    final = await wait_terminal(runtime, eid)
    assert final["status"] == pe.STATUS_COMPLETED
    starting = pe.get_execution(eid)["starting_sha"]

    # The task's own branch advances — the worktree is still this task's.
    (worktree / "new_module.py").write_text("x = 1\n")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-qm", "task work")

    resumed_runtime = PiRuntime()
    await resumed_runtime.resume(eid, message="Step two.")
    terminal = await wait_terminal(resumed_runtime, eid)

    assert terminal["status"] == pe.STATUS_COMPLETED
    assert terminal["worktree_verified"] is True
    assert terminal["verification"]["head_descends_from_starting_sha"] is True
    assert terminal["starting_sha"] == starting        # assignment is unchanged


async def test_q_unrelated_head_is_refused(repo, worktree, pi_env):
    """A worktree whose HEAD is not the assigned lineage is refused."""
    configure(worktree, session_id="stub-session-q")
    runtime = PiRuntime()
    record = await runtime.start(task="Step one.", worktree=str(worktree))
    eid = record["execution_id"]
    final = await wait_terminal(runtime, eid)
    assert final["status"] == pe.STATUS_COMPLETED

    # The recorded starting SHA no longer belongs to this worktree's history.
    tampered = pe.get_execution(eid)
    tampered["starting_sha"] = "0" * 40
    pe.save_execution(tampered)

    fresh_runtime = PiRuntime()
    with pytest.raises(WorktreeMismatch) as excinfo:
        await fresh_runtime.resume(eid, message="Step two.")
    assert "descended from it" in str(excinfo.value)
    assert pe.get_execution(eid)["status"] == pe.STATUS_WORKTREE_MISMATCH


async def test_n_cannot_resume_into_another_tasks_worktree(repo, worktree, tmp_path, pi_env):
    """One task's session can never be resumed into another task's worktree."""
    configure(worktree, session_id="stub-session-n")
    runtime = PiRuntime()
    record = await runtime.start(task="Task A.", worktree=str(worktree))
    eid = record["execution_id"]
    final = await wait_terminal(runtime, eid)
    assert final["status"] == pe.STATUS_COMPLETED
    sent_before = len(prompts_sent())

    # Task B's worktree (same repository, different task/checkout).
    wt_b = tmp_path / "wt-task-b"
    _git(repo, "worktree", "add", str(wt_b), "-b", "task/pi-two")

    # Something re-points task A's execution at task B's worktree.
    tampered = pe.get_execution(eid)
    tampered["assigned_worktree"] = os.path.realpath(wt_b)
    tampered["worktree"] = os.path.realpath(wt_b)
    pe.save_execution(tampered)

    fresh_runtime = PiRuntime()
    with pytest.raises(WorktreeMismatch) as excinfo:
        await fresh_runtime.resume(eid, message="Continue task A.")
    assert excinfo.value.execution_id == eid
    assert "different worktree" in str(excinfo.value)

    after = pe.get_execution(eid)
    assert after["status"] == pe.STATUS_WORKTREE_MISMATCH
    assert after["failure_class"] == "worktree_mismatch"
    assert after["previous_status"] == pe.STATUS_COMPLETED     # history preserved
    assert after["refusals"] and after["refusals"][-1]["phase"] == "resume-precheck"
    assert after["worktree_verified"] is False
    assert len(prompts_sent()) == sent_before      # nothing new was delivered
    # Task B's worktree was never touched by the refused resume.
    assert not (wt_b / "stub_output.txt").exists()
