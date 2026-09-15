# PS-579 - lineage reconciliation (2026-09-15)

Reconciliation-only slice. No benchmark task was run, no fourth task was started,
and no Framework/Halogen work was touched. Production G0/G1/G2 semantics were NOT
modified: `git diff --stat 1fc99c2e..HEAD -- src/` is EMPTY on both lineages.

## 1. The two lineages, exactly

`1fc99c2e` ("PS-632: seal the live discovery+dispatch evidence chain") is the
**merge-base** of the two branches. The canonical lineage forked from the
independent lineage's PS-632 tip, so both already share the PS-632 registry, the
PS-605 routing integration (`acc0ebda`) and the frozen G1/G2 code (`f96ebc20`).

| | canonical | independent |
| --- | --- | --- |
| branch | `ps-579-rtx-g0g1g2` | `work/ps632-capability-registry` |
| HEAD at reconciliation | `b5632c71` | `de260ae8` (accounting correction since committed as `36a01b56`) |
| worktree | `/home/tdefreest/Documents/PersonalOS/odysseus-github-ps579` | `/home/tdefreest/scratch/worktrees/ps632-capability` |
| merge-base | `1fc99c2e` | `1fc99c2e` |
| commits unique to the branch | 11 | 7 |
| `git status` | clean | clean |
| `src/` changes vs merge-base | **none** | **none** |

Canonical unique commits (`1fc99c2e..b5632c71`): `61719cbc` seal RTX G0/G1/G2
G0/G1/G2 evaluation harness - `cb78d991` preregister l1 RTX replay - `16549d0e`
preregister PS-639 RTX paired evaluation - `2c92b09f` bound PS-639 G2 timing
anomaly - `46c0c46f` capture pre-request RTX runtime state - `2a6846bd` preregister
balanced PS-639 timing control - `9b1259f1` record balanced PS-639 timing results -
`bc71eced` add existing G1d2 topo sort packet - `1c0df6c1` preregister fourth PS-579
topo task - `b5632c71` pin dispatch freshness fixture clock.

Independent unique commits (`1fc99c2e..de260ae8`): `982d832f` replay the frozen
G0/G1/G2 arms (arm switch, per-cell sealed preregistration, pre-state-correct matrix
driver) - `744568b9` refusal path for the PS-632 receipt shape + liveness re-measured
per cell - `50ee3f27` ledger-chain verdict read in the shape the ledger returns -
`fb2a19ae` a probe fault is not an arm outcome - `6371c4d6` G0/G1/G2 replay results -
`2bf90dcf` shared-host/store caveat - `de260ae8` dispatch fixture seeded from the
pinned clock.

Canonical adds: 8 prereg/result/forensics docs; `scripts/ps635-live/live_run.py`
(+48/-16: arm switch, refusal record-shape tolerance, per-round RTX runtime-state
probe); `scripts/ps635-live/ollama_client.py` (+40: `runtime_state()`);
`scripts/ps635-live/cases.py` (+68: `G1d2ToposortCase`, the preregistered fourth
packet).
Independent adds: `scripts/ps635-live/arms.py` + `scripts/ps579-eval/run_matrix.py`
(arm definitions, pre-state-correct matrix driver, per-cell probe refresh);
`scripts/ps635-live/live_run.py` (arm switch + refusal-path fix); 2 docs; 2 test
files.

## 2. Evidence authority

* **`ps-579-rtx-g0g1g2` is the canonical PS-579 evaluation/evidence lineage.** It
owns benchmark adoption and cumulative reporting.
* **`work/ps632-capability-registry @ de260ae8` is
`INDEPENDENT_REPLICATION_AND_HARNESS_FINDINGS`.** Its run set is replication
evidence over the SAME three task identities, plus harness-correctness findings. It
does not add distinct tasks and must not be counted as corpus diversity.

## 3. Fix dispositions (each inspected independently)

| finding | canonical state | disposition |
| --- | --- | --- |
| A. refusal path used pre-PS-632 record names | **already fixed** (`measured_capabilities()` + `getattr(r, "target_id", None) or getattr(r, "host_id", "")`) | **ALREADY PRESENT**. Verified by executing canonical's own `seal_dispatch_refusal` against a real `TargetCapabilityReceipt`: `target_id=local-rtx4500`, `health=healthy`, `proven=['native_tools']`, refusal hash intact. Not vulnerable; nothing to port. |
| B. PS-632 liveness must be re-measured, not extended | canonical documents the zero-call refusal and reran the affected cell "after a fresh receipt gate" (`PS579-PS639-BALANCED-ORDER-RESULTS.md`) | **ALREADY PRESENT / no defect**. The 300 s boundary was NOT extended on either lineage. Operational rule: resolve a FRESH receipt before a cell. Independent harness does it per cell; canonical did it by re-running the refused cell. |
| C. concurrent capability-probe fault, zero-call refusal | canonical preserves the zero-call refusal separately from its 9 valid cells | **ALREADY PRESENT**. No probe redesign needed or done. The independent lineage additionally NAMES the class (`PROBE FAULT`), retries the probe up to 3x, records every attempt, and excludes fault cells from arm statistics. Canonical's zero-call refusal was likewise never counted as an accepted cell. |
| D. `tests/test_dispatch_boundary.py` wall-clock seed | **already fixed** at `b5632c71` | **ALREADY PRESENT**. Independently converged on the identical fix - `SEEDED_AT = NOW.replace(tzinfo=None) - timedelta(minutes=30)` - plus a regression control of the same name and shape (`test_the_fixture_clock_is_pinned_to_the_decision_clock`). The original failure was reproduced on canonical before the fix (17 failed / 5 passed at 16:07 UTC) and the fix is structural, not a widened margin. |

Nothing was ported, because nothing was missing. Nothing was rejected or deferred.
One harness-fidelity note is recorded instead of changed (below).

### Harness-fidelity note (observation only, no cell affected)

Canonical's G2 arm passes the case's own `max_replans`; for `PS639ComposeDriftCase`
and `G1eBoundedCacheCase` that value is 0, so at a stall the advisor would have been
consulted but could not have produced a replan. The independent lineage's G2 arm
forces `max_replans=1`. `consult_planner` returns early only when `advise is None`,
so the difference is real but inert: **zero stalls occurred in either lineage**, so
no observed cell is affected. This is a harness configuration difference, not a
frozen-semantics change, and it is flagged for the fourth-task preregistration
rather than edited into an existing evidence window.

## 4. No production semantic drift

* `git diff --stat 1fc99c2e..b5632c71 -- src/` : **empty**.
* `git diff --stat 1fc99c2e..de260ae8 -- src/` : **empty**.
* Both lineages' non-`src` deltas are harness, tests, and docs only.
* Classification of everything reconciled here: *benchmark harness correctness*,
*deterministic test correctness*, and *evidence plumbing compatibility*. There is
NO ported change in the *production behavior change* class, so nothing needed to
stop for review.

## 5. Corrected independent benchmark-cell accounting

The independent report said "3 items x 3 arms = 10 cells", then "10/10 cells
ACCEPTED_CANDIDATE". That is internally inconsistent: three tasks x three arms is
NINE benchmark cells. The tenth dispatched cell is a G2-CONFIG CONTROL on an
existing task, not a new task. Reconstructed from the 31 sealed cell directories
(`data/live/*/ps579_cell.json` + `run_summary.json` + `result.json`):

**Corrected count: 9/9 valid benchmark cells**, all `ACCEPTED_CANDIDATE` on attempt
1, one model call each, zero repairs, zero planner calls, validator VERIFIED.

| task | arm | cell | model calls | outcome |
| --- | --- | --- | --- | --- |
| l1-interface | G0 | `l1-interface-g0-20260915T143653Z` | 1 | ACCEPTED_CANDIDATE |
| l1-interface | G1 | `l1-interface-g1-20260915T143707Z` | 1 | ACCEPTED_CANDIDATE |
| l1-interface | G2 | `l1-interface-g2-20260915T143722Z` | 1 | ACCEPTED_CANDIDATE |
| g1e-bounded-cache | G0 | `g1e-bounded-cache-g0-20260915T143737Z` | 1 | ACCEPTED_CANDIDATE |
| g1e-bounded-cache | G1 | `g1e-bounded-cache-g1-20260915T143801Z` | 1 | ACCEPTED_CANDIDATE |
| g1e-bounded-cache | G2 | `g1e-bounded-cache-g2-20260915T143826Z` | 1 | ACCEPTED_CANDIDATE |
| ps639-compose-drift | G0 | `ps639-compose-drift-g0-20260915T143914Z` | 1 | ACCEPTED_CANDIDATE |
| ps639-compose-drift | G1 | `ps639-compose-drift-g1-20260915T144220Z` | 1 | ACCEPTED_CANDIDATE |
| ps639-compose-drift | G2 | `ps639-compose-drift-g2-20260915T144527Z` | 1 | ACCEPTED_CANDIDATE |

**The tenth dispatched cell — control, NOT task diversity:**

| cell | what it is | model calls | outcome | treatment |
| --- | --- | --- | --- | --- |
| `g2-replan-control-20260915T143850Z` | the COMMITTED frozen G2 configuration (`G2ReplanControlCase`) applied to the SAME packet as `g1e-bounded-cache` (`G1e-bounded-cache-rtx`, same verifier, same pre-state) | 1 | ACCEPTED_CANDIDATE | retained as an in-lineage control cell; does NOT count as a distinct task and does NOT raise `n` |

**Zero-model-call cells — never benchmark successes (15):**

| cells | class | model calls | sealed refusal | treatment |
| --- | --- | --- | --- | --- |
| 5 (`l1-interface-g2-20260915T141618Z`, `g1e-bounded-cache-g0-20260915T141710Z`, `g1e-bounded-cache-g1-20260915T141801Z`, `g2-replan-control-20260915T141955Z`, `ps639-compose-drift-g1-20260915T142736Z`) | `PROBE FAULT` — `privacy_local_only_no_eligible_target` / `role_not_supported`, receipt had no `native_tools`, correctly fail-closed | 0 | yes | excluded from arm statistics; preserved as evidence |
| 10 (`*-20260915T1356**`) | attempt 0, pre-fix harness crashed while sealing the refusal (`AttributeError`) so no refusal was ever written | 0 | no | excluded; the run that exposed finding A |

**Superseded model cells — not reported as results (6):** 5 from the contended
window (`l1-interface-g0-20260915T141500Z`, `l1-interface-g1-20260915T141516Z`,
`g1e-bounded-cache-g2-20260915T141843Z`, `ps639-compose-drift-g0-20260915T142043Z`,
`ps639-compose-drift-g2-20260915T142813Z`) and 1 from the aborted attempt 1
(`l1-interface-g0-20260915T141041Z`). They were taken while a second workload shared
the host (see 6), so they are discarded rather than averaged with the clean set.

**Correction to the independent report's own text:** it says "4 cells" for the
probe-fault class; the exact number is **5**. Restated here so the accounting is
exact rather than approximately right.

Accounted totals: 9 valid benchmark cells + 1 control + 15 zero-call refusals +
6 superseded model cells = 31 sealed cell directories, which is every cell written.

## 6. Replication evidence retained (metadata)

The independent run set is NOT deleted, overwritten or merged into canonical. It
stays where it was produced and is referenced here as replication:

| field | value |
| --- | --- |
| independent lineage | `work/ps632-capability-registry` |
| independent commit | `de260ae8` (harness as of `982d832f`..`fb2a19ae`) |
| harness version | `scripts/ps635-live/live_run.py --arm {g0,g1,g2}` + `scripts/ps635-live/arms.py` + driver `scripts/ps579-eval/run_matrix.py`; canonical's harness does NOT carry these files |
| results | `docs/local-targets/PS579-G0G1G2-RESULTS.md` and `PS579-G0G1G2-PREREG.md` on that branch |
| raw cell evidence | `/home/tdefreest/scratch/worktrees/ps635-live-subject/data/live/**` plus `ps579-matrix-clean.json` |
| run window | 2026-09-15 14:36:40Z-14:45:31Z (clean, reported set); 13:56-14:28Z (superseded attempts) |
| shared-host limitation | a concurrent independent PS-579 workload on the same RTX4500 caused the 5 probe-fault refusals and inflated wall times in the superseded window; the clean window shows 0 probe faults and arm wall times within 0.2 s |
| capability-store limitation | `/home/tdefreest/scratch/ps632-store` was SHARED by both lineages, so a receipt written by another session could be the current one when a cell read it; every cell still bound the receipt it was routed on (`receipt_ref_matches_store=true`) |
| task overlap with canonical | 3 of 3 canonical tasks (l1-interface, g1e-bounded-cache, ps639-compose-drift) - full overlap, so this is replication only |
| effect on canonical `n` | **none**. `n = 3` distinct tasks before and after. |
| allowed claim | reproducibility/supporting evidence, and harness-correctness findings |
| disallowed claim | additional task diversity, additional corpus coverage, or an independent increase in sample size |

## 7. PS-639 timing reconciliation: CORROBORATES

Canonical balanced-order control (third valid cell rerun once after a fresh receipt
gate, 9 valid cells): G0 mean `183.159 s`, G2 mean `183.438 s`, spread far under any
effect size one could attribute to orchestration; classified `RUNTIME_VARIANCE`.

Independent clean run (3 cells, uncontended window, same task/base):
G0 `180.947 s`, G1 `182.811 s`, G2 `183.768 s`.

**Conclusion: `INDEPENDENT_REPLICATION_CORROBORATES_NO_MEASURABLE_G2_ARM_PENALTY`.**
The G2-minus-G0 gap is +2.821 s independently against +0.279 s canonically, both
tiny against a task whose per-cell total is ~181 s and whose observed host variance
reaches ~20 s (canonical Set A G1 at `201.408 s`). The independent set does not
contradict the canonical classification and does not create a new timing question.
**No new timing investigation is opened.**

## 8. Frozen references confirmed untouched

| reference | state |
| --- | --- |
| PS-635 frozen G2 | `f96ebc20` present, worktree `/home/tdefreest/worktrees/s7-loop` at `f96ebc20`, clean |
| PS-605 frozen | `aef0e0d9` present, worktree at `aef0e0d9`, clean |
| PS-605 x PS-635 integration | `acc0ebda` present, worktree at `acc0ebda`, clean |
| subject/live evidence worktree | `/home/tdefreest/scratch/worktrees/ps635-live-subject` at `d284bf9d`, clean |

## 9. Empirical-gap policy (unchanged)

Still UNOBSERVED and not manufactured: natural failed-first-attempt repair,
post-repair PASS, no-progress -> validated replan, manager regression, truncation
recovery, semantic-review escape. No failure-conditioned corpus was created here and
no fourth task was selected or run.

## 10. Verification performed (deterministic only; no model task rerun)

Canonical @ `b5632c71`, all runs on 2026-09-15 well past the old failure threshold
the fixture used to depend on:

| check | result |
| --- | --- |
| `tests/test_dispatch_boundary.py`, three consecutive runs at 16:28 UTC | 23 passed, 23 passed, 23 passed |
| full suite at 16:28-16:33 UTC | **3956 passed, 3 skipped, 0 failed**, exit 0 |
| time-bomb reproduction BEFORE `b5632c71` (at 16:07 UTC) | 17 failed, 5 passed - the original failure mode, reproduced non-destructively on the canonical branch |
| canonical `seal_dispatch_refusal` against a real `TargetCapabilityReceipt` | seals correctly; `target_id`, `health`, `proven` and refusal hash all intact |

Independent @ `de260ae8` (accounting correction since committed as `36a01b56`):
**3974 passed, 3 skipped, 0 failed**, and `tests/test_dispatch_boundary.py` passes
in isolation as well as in the full run. The prior wall-clock-dependent failure is
gone for a structural reason, not a widened margin: the fixture no longer reads the
wall clock at all, and a control asserts that the 30-minute relationship to the
pinned decision clock is exact and does not track "now".

No benchmark model task was rerun, on either lineage, because nothing was ported
and no evidence validity or run semantics changed.

## 11. Canonical corpus state and next bounded action

`n = 3` distinct tasks, unchanged: `G1e-bounded-cache-rtx`, `l1-interface`,
`PS639-compose-drift-rtx`. Balanced-order PS-639 repeats are TIMING CONTROLS and
independent repetitions are REPLICATION; neither increases distinct-task diversity.
The fourth packet (`G1d2-toposort`, preregistered in
`docs/PS579-PREREG-FOURTH-G1D2-TOPOSORT-RTX.md`, packet landed at `bc71eced`) is
ADOPTED BUT NOT RUN.

**Recommended next bounded action: `PROCEED_FOURTH_NATURALISTIC_TASK`.**
The canonical lineage is clean, every independent correctness finding is
reconciled (all four were already present), the accounting is corrected, no
semantic conflict remains, and the fourth task is already preregistered. This
recommendation does NOT authorise a failure-conditioned corpus, which must wait
until the ordinary internal corpus is frozen or separately preregistered.
