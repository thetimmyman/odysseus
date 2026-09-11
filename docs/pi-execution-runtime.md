# Pi Execution Runtime

Pi Coding is Odysseus's **coding-agent execution plane**. Odysseus remains the
**control plane**.

| Plane | Owns |
| --- | --- |
| Odysseus | Jira/backlog integration, routing policy, model-role selection, provider/subscription selection, budgets and spend controls, provider health, work prioritization, audit scheduling, implementation scouting, governance policy, escalation decisions, evidence/history, operator controls, task lifecycle |
| Pi | inner coding-agent loop, reasoning/tool rounds, repository exploration, filesystem operations, shell execution, git operations, context lifecycle, compaction/session continuity, implementation execution, iterative test/fix/test |

Odysseus does **not** re-implement the inner loop, and does **not** inject a
second context-compaction system into a Pi run (`src/agent_loop.py` and
`src/context_compactor.py` are unchanged and remain the native path).

## Install

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent
pi --version        # 0.74.2 at time of writing
```

Pi keeps its state under `~/.pi/agent/` (`models.json`, `auth.json`,
`sessions/`). Odysseus launches it from outside the source tree.

## Local Qwen configuration

The default execution target is the local Qwen runtime already used by
Odysseus — Ollama's OpenAI-compatible API:

* provider id: `local-qwen`
* model id: `qwen3.8:27b`
* base URL: `http://localhost:11434/v1` (override with `ODYSSEUS_PI_LOCAL_BASE_URL`
  or `OLLAMA_BASE_URL`)

`src/pi_config.ensure_pi_model_config()` writes/merges the Pi `models.json`
provider entry (`api: openai-completions`, `compat.supportsDeveloperRole=false`,
`compat.supportsReasoningEffort=false`) and **only** ever registers the local
provider, so Pi's model picker cannot reach a premium provider.

## Runtime selection

```
ODYSSEUS_EXECUTION_RUNTIME = native | pi      # default: native
```

`native` is Odysseus's existing agent loop (unchanged, explicit compatibility
path). `pi` delegates the inner loop through the adapter.

## Adapter

`src/pi_runtime.py` (transport: Pi RPC mode, `pi --mode rpc`, JSON-lines over
stdio):

```python
runtime = get_pi_runtime()
record = await runtime.start(task=..., worktree=..., model=..., constraints=[...],
                             task_id=..., jira_ticket=..., odysseus_run_id=...)
runtime.events(record["execution_id"])          # mapped Odysseus events
runtime.status(record["execution_id"])          # lifecycle + liveness
await runtime.send(execution_id, message, streaming_behavior="steer")
await runtime.cancel(execution_id)              # Pi `abort`, then terminate
runtime.result(execution_id)                    # normalized terminal result
await runtime.resume(execution_id, message=...) # same Pi session file
```

Execution identity (`src/pi_executions.py`) is stored per execution under
`<DATA_DIR>/pi/executions/<id>.json` plus an append-only
`<id>.events.jsonl` ledger: odysseus run id, task id, Jira ticket, worktree,
repo path, base commit, branch, model, provider, runtime, Pi session id and
session file, timestamps, status, failure class/reason, files changed, tests
run, result.

## Event mapping

`src/pi_event_map.py` translates Pi 0.74.2 events
(`agent_start`, `turn_start/turn_end`, `message_start/update/end`,
`tool_execution_start/update/end`, `queue_update`, `compaction_start/end`,
`auto_retry_start/end`, `extension_error`, `agent_end`) into Odysseus events:
`execution_started`, `model_turn`, `tool_call`, `tool_result`,
`file_modification`, `shell_command`, `test_execution`, `warning`,
`context_event`, `message_delta`, `failure`, `completion`. Mapping is not
one-to-one by design — enough observability, not a rewrite of Pi's semantics.

## Failure states

`completed`, `cancelled`, `runtime_failure`, `provider_failure`,
`tool_failure`, `task_failure`, `input_required`, `worktree_mismatch`. A
failure is explicit: this integration never silently re-routes to another model
or runtime. `worktree_mismatch` is Odysseus's fail-closed refusal to run when Pi
would not be bound to the assigned worktree (HTTP 409 on the operator API).

## Permissions

A Pi execution is launched with a minimal environment allowlist
(PATH/HOME/locale/TMPDIR) — no provider API keys and no `ODYSSEUS_*` secrets —
inside the assigned worktree only. Pi cannot change routing policy, budgets or
governance, cannot select a premium provider, and cannot authorize deployment.

## Resume

Pi sessions are JSONL files. `resume()` relaunches Pi with `--session <file>`
pointing at the same session, so continuation is the same execution, not an
unrelated chat. Session files are located deterministically under the session
directory; the adapter also captures them from `get_session_stats`.

## Worktree assignment (invariant)

**Odysseus owns worktree assignment. Pi may operate only inside the worktree
explicitly assigned to that execution.**

Pi is never trusted to pick, remember, or default its own working directory:

1. Odysseus assigns the worktree for every execution (`start(worktree=...)`);
   resume re-derives it from the **execution record**, never from a caller and
   never from Pi's remembered session.
2. Pi is spawned with exactly that path as its process cwd.
3. Before the task prompt is sent, the adapter verifies three independent
   things against the assigned path:
   * the **live process cwd** (`/proc/<pid>/cwd` on Linux, `lsof` on macOS);
   * the **cwd Pi itself recorded** in its session header (`pi_session_cwd`);
   * the **git identity** of the assigned path — work-tree toplevel == assigned,
     branch, and HEAD vs. the recorded `starting_sha`.
4. Any mismatch **fails closed**: the prompt is never delivered, the Pi process
   is terminated, and the execution is recorded as `worktree_mismatch`
   (`failure_class: worktree_mismatch`, `worktree_verified: false`).
5. A resume whose recorded Pi session belongs to a **different** worktree is
   refused *before* Pi is spawned — a resumed session can never silently restore
   or switch to a stale worktree from a previous task.
6. Pi writes its session file lazily on some versions, so the recorded cwd is
   re-checked when the run ends; a late mismatch is refused rather than reported
   as a completed run.

Every execution record persists the assignment identity:
`assigned_worktree`, `worktree`, `actual_worktree`, `repo`, `repo_toplevel`,
`branch`, `starting_sha`, `pi_session_id`, `pi_session_cwd`, `worktree_verified`,
the raw `verification` evidence, and `refusals[]` (each refused attempt, with the
phase that refused it: `start`, `resume`, `resume-precheck`, `post-run`) plus
`previous_status` so a refusal never hides a prior outcome.

A branch that legitimately **advanced** from the recorded `starting_sha` is
accepted (`head_descends_from_starting_sha`); a HEAD on an unrelated lineage is
refused.

Regression coverage: `tests/test_pi_runtime.py::test_j…test_o`.

## Limitations

* Process isolation, not a container sandbox; `src/routing_sandbox.py`'s docker
  machinery is the follow-up boundary for untrusted repositories.
* Cancellation is cooperative (`abort`) with a terminate fallback.
* Only the local Qwen target is validated end-to-end so far.
