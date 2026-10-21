# Harness-isolation experiment matrix

Reference protocol for OD-19. This document defines an experiment; it records
**no executed trials, validated Flash endpoint, winner, or runtime admission**.
The treatment is the harness and its pinned configuration. Outcome differences
are observations until the controls and uncertainty rules below are satisfied.

Related contracts: [Pi runtime](../pi-execution-runtime.md),
[measured verification](../measured-work-verification.md), and
[coordinator research](../ROUTING-COORDINATOR-MODEL-RESEARCH.md).
The last document names the intended candidate `Qwen3.8-Flash-Next`; that label
is not evidence of a currently served provider-native model identifier.

## Matrix and admission

Every `M`, `P`, `S`, `V`, and `B` below must resolve to the same immutable
experiment-manifest entry before any arm is admitted. Placeholders are not
launch arguments. Missing identity or unenforceable limits mean **BLOCKED**.

| Arm | Harness treatment | Permission profile | Model | Provider/pool | Source/task | Verifier | Budget |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A1 | Pi: exact version + config digest | Yolo-Auto equivalent, audited | M | P | S | V | B |
| A2 | OpenCode: exact version + config digest | Yolo-Auto equivalent, audited | M | P | S | V | B |
| A3 | Cline OpenAI-Compatible: exact version + config digest | Yolo-Auto equivalent, audited | M | P | S | V | B |

`Yolo-Auto` is the requested unattended permission policy, not a portable CLI
flag or an equal resource budget. Record each arm's actual permission settings,
allowed tools, read/write scope and denied operations; reject unequal authority.
No experiment permission authorizes a merge, deployment or production write.

### Fixed register (manifest required before execution)

- **M — model identity:** capture the exact provider-native request model ID,
  independently observed resolved model/build, immutable digest or revision when
  available, quantization, serving runtime/version, context and inference profile.
  All arms must resolve to that same identity; an alias string alone is insufficient.
  Pin temperature, top-p, output/reasoning limits and seed support explicitly.
  An unavailable immutable revision must be marked UNKNOWN, with the provider's
  actual stability evidence retained; without a defensible stable-build window,
  isolation is blocked. Never silently substitute another model.
- **P — provider:** same endpoint identity, account/capacity-pool identity, plan,
  rate class, runtime allocation and scheduling/concurrency policy. Store safe
  references, not credentials or private endpoint values in this document.
  Three harnesses sharing one account remain one capacity pool.
- **S — source:** exact task/corpus version, task class, source commit/snapshot
  digest, dependencies/toolchain, environment and input/context payload digests.
  Each trial starts from an isolated clean copy; no prior arm's patch, session,
  workspace history, cache-derived answer or verifier feedback is inherited.
- **V — verifier:** same authorized command, verifier files/digests, environment,
  deterministic pass rule and source-bound semantic rubric/reviewer protocol.
  Freeze these before trials; blind semantic reviewers to harness labels where
  feasible. No harness-specific scorer or post-hoc easier acceptance threshold.
- **B — execution opportunity:** fill numeric limits and units for every row
  below before launch. No default, blank or "unlimited" entry counts as matched.

| Limit | Common enforcement and retained evidence |
| --- | --- |
| Model turns/requests and tool calls | Same maxima; count retries, hidden auxiliary calls and compaction/summarization requests; observer must see every request |
| Input/context and cumulative output/reasoning tokens | Same context ceiling and cumulative token caps across all model calls; count provider-side reasoning if charged/limited; missing enforceable metering blocks admission |
| Wall-clock time | Same deadline from dispatch to terminal result, including retries/compaction; record queue wait separately under the same queue policy |
| Tool-command timeouts | Same per-command maximum and cumulative tool time; supervisor stops subprocesses on expiry |
| Attempts/repair and intervention | Same fixed attempt cap and feedback policy; first pass reported separately; primary trials have no manual steering |
| Provider concurrency and capacity | Same per-trial concurrency/rate envelope and reserved resource policy; no privileged arm or independent-pool assumption |

Harness configuration plus an independent supervisor/provider meter must enforce
B. Record configured maxima, actual consumption, cutoff reason and observed stop
for each trial. Prompt text asking an agent to stop is not enforcement. A timeout
at the common cap is an observed budget-exhausted outcome, not grounds to drop a
trial. A broken meter/enforcer is a protocol violation, retained separately.

### What differs, and what does not

Hold the task instructions, initial context content and tool capabilities fixed.
Harness-internal prompt framing, tool encoding, compaction, session management
and retry strategy are part of the treatment: record their exact versions/config
and observed behavior rather than pretending their requests are byte-identical.
All their resource use still counts against B. If one arm cannot expose or enforce
these controls, report an uncontrolled stack comparison separately; do not call
it an admitted harness-isolation trial. Harness/version/config or model/runtime
profile drift creates a new arm/block; never pool it into the frozen experiment.

## Replication and inference plan

Before execution, freeze the task corpus, exclusions, practical effect threshold,
analysis code/version, randomization seed and complete trial schedule. Choose at
least two task classes; results generalize only to this sampled corpus.

1. Use matched blocks `(task, repetition)`: all three arms receive the same task
   and reset state. Use at least 30 distinct tasks and 5 repetitions per task per
   arm as a design floor, not a claim of sufficient statistical power. Determine
   a larger fixed sample from the desired precision/power before seeing results.
2. Balance the six possible arm orders across blocks using a recorded randomized
   schedule; keep a single active trial and the same capacity conditions. Record
   time/load/provider drift. Warm-up policy must be identical and fixed in advance.
3. When all endpoints honor a controllable sampling seed, use the same recorded
   seed within a matched block and different seeds across repetitions. Record
   unsupported or unverified seed behavior as UNKNOWN; repeated trials and order
   balancing remain mandatory even with seeds. Never claim determinism from a seed.
4. Primary outcome is first-pass deterministic verification **and** source-bound
   semantic ACCEPTED, within B. Keep every scheduled/admitted execution in its
   denominator, including timeouts, harness faults and provider faults; distinguish
   failure categories. Unknown evidence is not success. Report exact paired-block
   completeness and a conservative missing-as-unsuccessful sensitivity result.
5. Estimate pairwise differences in first-pass accepted rate with task-clustered
   resampling: resample whole task IDs, retaining all their repetitions and arms.
   Freeze the bootstrap seed, 10,000 resamples and interval implementation.
   Report all three contrasts and familywise 95% intervals using Bonferroni
   per-contrast 98.333% intervals. Repeated runs of one task are not independent
   tasks. A degenerate or insufficient-data interval is INCONCLUSIVE.
6. Preregister a minimum meaningful improvement of 5 percentage points for this
   protocol. A positive harness-effect claim requires its adjusted interval wholly
   above +5 points, complete controls and robustness to the stated missing-data
   sensitivity. Otherwise report INCONCLUSIVE, even when the point estimate wins.
   Do not extend sampling after a near-win; a new sample requires a new protocol.
   Report latency, token use, intervention and eventual success as secondary
   descriptive outcomes, with N and missingness, without searching for a winner.

**A changed result must not be attributed to model quality when harness behavior
changed.** A single changed result also does not establish a harness effect:
sampling noise, provider load and measurement error remain alternative causes.
An admitted, replicated comparison supports a bounded treatment-effect estimate
for these pinned harness profiles and this corpus, never a universal ranking.

## Evidence and current implementation boundary

Repository contracts checked against `dev` commit
`7a41c3904d6c105756cd40bb3ffe4af95878bfd3`:

- `src/pi_config.py` defaults to `local-qwen` / `qwen3.8:27b`; it does not define
  `qwen3.8-flash` as an alias. No endpoint discovery or Flash probe was performed
  for this document. The exact served Flash ID/build is **UNKNOWN**, so M remains
  unfilled and this matrix is not launch-ready.
- `src/pi_runtime.py` and `src/pi_executions.py` expose execution/session,
  worktree and event evidence; they do not establish a cross-harness B enforcer.
  OpenCode/Cline parity and metering are unverified here. A future executor must
  demonstrate all three arms' enforcement with cutoff negative controls first.
- `src/execution_outcomes.py` projects immutable canonical execution evidence;
  `src/execution_outcome_store.py` retains outcome snapshots and
  `src/outcome_scorecard.py` provides descriptive, non-ranking aggregation.
  They do not implement this randomized trial or clustered inference plan.
  Store a versioned experiment sidecar linked by exact execution/raw-record
  hashes; do not invent canonical receipt fields or create a second authority.

For each scheduled trial retain the manifest/protocol hash, task/block/arm/order,
seed status, exact harness/provider/model identities, starting source digest,
ExecutionPackage and DispatchDecisionReceipt references, all AttemptReceipt and
VerificationReceipt references, semantic disposition, budget settings/consumption,
cutoffs, faults, interventions and metric source/completeness. Preserve raw
records and aborted/refused trials; never overwrite history after a repair.
LandingReceipt and delivered evidence are required to claim landed/delivered
work. Tracker Done or a harness's self-report cannot substitute. Missing cost or
usage provenance is ABSENT/UNKNOWN, never zero; shared capacity is not tripled.

Admission negative controls must show refusal on wrong resolved model, source
or verifier drift, unequal limits, missing metering and stale sessions; show
actual process cutoff at a deliberately small B. Retain a provider-timeout and
a harness-parser failure with separate classifications. Deterministic PASS plus
semantic REJECT must remain unsuccessful. Until those receipts and the frozen
manifest exist, the only supported status is **protocol documented; experiment
not executed**. No measured result or routing-policy change is claimed.
