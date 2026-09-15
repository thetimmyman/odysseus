# PS-605 branch integration plan

**Status: plan only. Nothing below has been executed.** No merge, cherry-pick or
push has been performed from this branch.

## What exists, and where

| Branch | Head | Base | Contents |
| --- | --- | --- | --- |
| `work/ps-605-domain-policy` | this worktree | `def0119b` + 1 commit (`e6de4a79`) + the consumption slice | the deterministic selector, the dispatch boundary, the production wiring, the two domain controls |
| `work/ps-635-loop-demo` | `f96ebc20` | `def0119b` + 20 commits | PS-635 G2 (frozen), PS-638's execution/evidence envelope, PS-639's standalone-Compose fix, the live harness |

Common ancestor: **`def0119b`** (both branches are `def0119b` + their own linear
series; neither contains the other).

## Overlap analysis (measured, not assumed)

* `git diff --name-only def0119b f96ebc20` → **49 files**, all under
  `src/{attempt_receipt,bounded_cache,evidence_*,execution_ledger,execution_package,
  interval_merge,ledger_note,local_targets,local_worker_loop,repair_packet,replanner,
  source_snapshot,topo_sort,verifier_evidence,window_stats,work_packet,worker_context}.py`,
  `scripts/ps635-live/*`, `scripts/ps638-mutations.sh`, `docs/local-targets/*`,
  `tests/test_*ps6*.py`, and the two `docker-compose.gpu-*.yml`.
* This branch's files: `src/dispatch_routing.py`, `src/dispatch_boundary.py`,
  `src/routing_executor.py`, `src/routing_domain_policy.py`,
  `tests/test_dispatch_routing.py`, `tests/test_dispatch_boundary.py`,
  `tests/test_dispatch_wiring.py`, `tests/test_routing_domain_policy.py`,
  `tests/__init__.py`, `scripts/ps605-domain-controls.py`, `docs/PS605-*.md`.
* **File intersection: empty.** The merge is therefore textually clean, and the
  interesting work is the *semantic* list below rather than conflict resolution.

## Semantic couplings that still need checking on the merged head

1. **The PS-638 receipt contract is the only real interface between the branches.**
   `src/dispatch_routing.py` freezes `PS638_RECEIPT_FIELDS` /
   `PS638_RECEIPT_CORE_FIELDS` and emits `decision.to_ps638_receipt_kwargs()`.
   After merging, the merged head must be able to construct PS-638's
   `DispatchDecisionReceipt` from exactly those kwargs
   (`make_dispatch_receipt(**decision.to_ps638_receipt_kwargs())`). That assertion
   needs both branches present, so it belongs **on the merged head**, not on either
   side alone — it is the first test to add post-merge, and the first thing to run.
2. **`tests/__init__.py` was tried and is NOT needed.** An empty `tests/__init__.py`
   was added here so the wiring test could import the boundary fixtures
   (`from tests.test_dispatch_boundary import _db, _seed`); it switches pytest's
   import mode suite-wide. The repository already imports its own test modules that
   way (`from tests.helpers.import_state import …`,
   `from tests.test_null_owner_gates import …`) through PEP 420 namespace packages
   with `tests/conftest.py` putting the project root on `sys.path`, so the file was
   removed again and the suite is green both ways (3569 passed with it, and with it
   removed). **The branch no longer carries that suite-wide change.**
3. **PS-639 drift.** `test_gpu_compose_standalone.py` has 3 failures at `def0119b`
   (exact-base proven, `test_amd_standalone_equals_base_plus_overlay` among them).
   `f96ebc20`'s series fixes them at `1e362f10`. Do **not** re-fix them here: the
   merge is what brings the fix, and until then this branch's suite is expected to
   run green *excluding* that file.
4. **No schema interaction.** Neither branch adds columns to
   `RoutingModelProfile`/`ModelEndpoint`; PS-605 derives profiles and receipts from
   those rows read-only, PS-635 never touches them. No migration ordering issue.
5. **Private imports.** `src/dispatch_boundary.py` uses `src.routing_engine`'s
   `_SENSITIVITY_RANK`, `_remote_ceiling_rank`, `_endpoint_is_local`, `ROLE_BY_TASK`,
   `_DEFAULT_ROLES` and `src.endpoint_resolver.resolve_endpoint_by_id`. PS-635 does
   not modify either module, so the merge is safe — but this is the coupling most
   likely to break on a *later* change, and the intent is to move to the public
   seams (`routing_policy.remote_sensitivity_ceiling()`, a public locality helper)
   when one is needed for another reason.
6. **The harness still pins by hand.** `scripts/ps635-live/live_run.py` chooses its
   target from an operator pin. Consuming `select_target` there is the follow-up
   slice — deliberately *not* part of this one, so the merge stays a library merge
   plus two consumers (`scripts/odysseus-run` via `execute_candidates`, and the new
   control script), not a harness rewrite.

## Intended integration base and order

**Base: `work/ps-635-loop-demo @ f96ebc20`.** Merge PS-605 **into** it, not the
other way round:

* that branch holds PS-638's envelope, which is the *consumer* of the receipt this
  slice produces, and PS-639's fix, which is what makes one green suite possible;
* the PS-605 series is the smaller, newer delta and lands as one merge commit whose
  parent pair is exactly (f96ebc20, ps-605 head).

Sketch (not run):

```bash
# in a fresh worktree off the loop-demo branch, never in either live worktree
git worktree add ~/scratch/merge/ps635-ps605 work/ps-635-loop-demo
cd ~/scratch/merge/ps635-ps605
git merge --no-ff work/ps-605-domain-policy      # expect: clean, 0 conflicts
python3 -m pytest tests -q                       # gate 1: full suite
python3 -m pytest tests/test_gpu_compose_standalone.py -q   # gate 2: PS-639 fix intact
# gate 3: the cross-branch contract test (added on the merged head)
python3 -m pytest tests/test_ps638_receipt_contract.py -q
```

Then, and only with direction: `origin/dev` gets **one** merge from the merged
head, not two.

## Explicitly out of scope for the merge

G2 replanning, framework runtime qualification, mechanical landing, cockpit/UI,
MS-R1 implementation, broad package migration — and `explicit_pin` stays available
as an explicit policy, never as the silent default.
