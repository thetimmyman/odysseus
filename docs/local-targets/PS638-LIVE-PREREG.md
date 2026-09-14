# Pre-registration — PS-638-routed live runs on RTX4500 (2026-09-14)

Written and committed BEFORE the runs. Outcomes are appended, never rewritten.

## What is new here, and what is not

Not new: PS-635 already ran the interface-delivery control (L1) and the G1a..G1d2
series, and those results stand as committed.

New, and the reason for this document: **those runs emitted ledger entries and a
hand-written Markdown account, not a sealed PS-638 package.** This run drives the
same loop with the PS-638 evidence layer attached, so every claim below is bound
to an ExecutionPackage hash, an AttemptReceipt per attempt, a VerificationReceipt
with a DERIVED verdict, a requirement closure, and a validator verdict — all of it
source-bound and re-runnable from the committed harness at
`scripts/ps635-live/live_run.py`.

## Setup (fixed before the runs)

| | |
| --- | --- |
| base | `c7cce461` (PS-638 v1) plus the harness commit |
| harness | `scripts/ps635-live/live_run.py` (committed) |
| target | `local-rtx4500` = `minipc` = tailnet `tacticusanalytics` |
| runtime / model | ollama 0.32.11 / `qwen3.8:27b` |
| context | `num_ctx` 32768 (measured safe floor), generation reserve default |
| tools | exactly one: `write_file`, path restricted to the packet write scope |
| hidden verifier hashes | sealed into the VerificationPlan BEFORE any model call |
| attempts | `max_attempts=3`; one fresh context per attempt, repair carries the ORIGINAL contract and interface |
| routing | explicit operator pin; the receipt records `explicit_pin` and claims NO policy reference |
| network | probed non-destructively; no host config edited, no daemon restarted |

## Run E1 — the interface-delivery control, through the receipt path

Case `l1-interface` (`src/ledger_note.py`, verifier `tests/test_ledger_note_ps635.py`).
The key names appear ONLY in `WorkPacket.interface`; the harness REFUSES to
proceed if a name leaks into the objective, contract, criteria, file name or
prompt.

Pre-registered predictions:

1. attempt 1 PASSES (`8 passed`), terminal `ACCEPTED_CANDIDATE`, **0 repairs**.
2. The sealed package VALIDATES (`VERIFIED`), with all mandatory requirements
   `SATISFIED`.
3. The sealed interface line is present in the rendered-context artifact that the
   package references, so delivery is checkable from the package alone.

## Run E2 — the first GENUINE repair case

Case `g1e-bounded-cache` (`src/bounded_cache.py`, hidden verifier
`scripts/ps635-live/hidden/test_bounded_cache_ps635.py`).

Why a stateful task: the G1a..G1d2 series one-shotted 6 rules, 9 interacting
rules, multi-form assertion parsing and a 7-rule algorithmic packet. A repair case
needs genuine internal state. This packet states FOURTEEN rules for an LRU cache —
cumulative counters that survive `clear()`, a `purge` that is neither hit nor
miss, `set_capacity` evicting and counting, MRU-first `keys()`, and
equality-not-identity key handling. Nothing is hidden: every rule is written out,
so a first-attempt miss is an IMPLEMENTATION DEFECT.

The hidden verifier was validated against a reference implementation (19 passed)
and against two deliberately wrong ones before the run:
`clear()` resetting the counters fails exactly one test, and MRU-eviction fails
exactly the named control. It is not being trusted because it is green.

Pre-registered predictions:

1. **Attempt 1 FAILS deterministically** on at least one stated rule, with a real
   assertion visible in the verifier output.
2. The repair packet carries the SAME `interface_digest` and the contract
   verbatim.
3. **Attempt 2 PASSES** → `ACCEPTED_CANDIDATE`, 2 attempts, 1 repair; the sealed
   package VALIDATES, and it still contains BOTH attempts (no earlier red erased).
4. If attempt 2 fails with the same fingerprint: **ESCALATE**, no third dispatch.
   The package is still sealed, and it is REJECTED by the validator with named
   reasons. That is a legitimate outcome and will be reported as a negative
   result.

## Run E3 — the missing-interface NEGATIVE control

Case `neg-no-interface`: identically specified to E2, with `interface` removed.

Pre-registered prediction: the package is refused as `PACKET_INVALID` **before any
budget or model call** (`model_calls == 0`), and **no EvidencePackage is sealed** —
because a sealed package about a run that never happened would be a document about
nothing.

---

## OUTCOME of E3 (appended after the run)

Run `neg-no-interface-20260914T170246Z`.

| pre-registered prediction | outcome |
| --- | --- |
| refused as `PACKET_INVALID`, 0 model calls, no package sealed | **CONFIRMED** |

`data/live/neg-no-interface-20260914T170246Z/preflight.json`:

```json
{"result": "PACKET_INVALID", "model_calls": 0, "package_sealed": false,
 "reason": "packet is not packageable: interface must be non-empty: a writable
            packet must declare the input keys its contract promises the worker"}
```

No model was contacted and no evidence package exists, which is the correct shape:
the refusal IS the evidence.

## OUTCOME of E1 (appended after the run)

Run `l1-interface-20260914T170257Z`, target `local-rtx4500`, base `654b36fc`.

| pre-registered prediction | outcome |
| --- | --- |
| 1. attempt 1 passes, `ACCEPTED_CANDIDATE`, 0 repairs | **CONFIRMED** — `8 passed`, 1 attempt, 0 repairs |
| 2. the sealed package VALIDATES, mandatory requirements SATISFIED | **CONFIRMED** — `ok: true`, all five `SATISFIED` |
| 3. the sealed interface is present in the referenced context artifact | **CONFIRMED** — checked from the package, not from the intention |

Numbers: 1 model call, 63.2 s, 897-char artifact, ledger chain `True`, decision
`stop`.

Cross-session check worth noting: the run-entry `interface_digest` is
`1d6eda6c43ddca1a` — **byte-identical to the committed L1 control** run on
2026-09-14 at base `9efb53bc`. Two sessions, two harnesses, one interface identity.

Independent re-derivation from the sealed package alone:

```
subject_name   in INTERFACE section=True, elsewhere=False
verbatim_lines in INTERFACE section=True, elsewhere=False
block_reason   in INTERFACE section=True, elsewhere=False
```

So delivery is checkable from the package, and the key names appear nowhere else in
the projection.

Validator states: `deterministic_verification`, `source_binding`,
`negative_control`, `scope_check`, `context_delivery` — all `SATISFIED`.

### Harness defect found during E1 (recorded, not hidden)

The run sealed its package, validated it, wrote every receipt and artifact — and
then crashed while assembling `run_summary.json` (a `NameError`: a parameter was
not threaded into the helper that writes the summary). **The evidence was never at
risk.** The repair was to *re-derive* the summary from the sealed records
(`live_run.py --summarise RUN_DIR`) rather than re-run the experiment and spend
another model turn. The recovered summary says so in its own text.

---

## OUTCOME of E2 (appended after the run)

Run `g1e-bounded-cache-20260914T171455Z`, target `local-rtx4500`, base `654b36fc`.

| pre-registered prediction | outcome |
| --- | --- |
| 1. attempt 1 FAILS on at least one stated rule | **FALSIFIED** — attempt 1 passed `19 passed` |
| 2. the repair packet carries the same interface digest | did not occur |
| 3. attempt 2 passes → 2 attempts, 1 repair | did not occur |
| 4. repeated fingerprint → escalate | did not occur |

Result: `ACCEPTED_CANDIDATE`, **1 attempt, 0 repairs**, 1 model call, 20.4 s,
`19 passed`, control `True`, artifact 2 264 bytes
(`sha256 fe02cba19c167454…`), `interface_digest 3c9d24e49d07e135`.

The sealed package VALIDATES with every mandatory requirement `SATISFIED`:

```
package_hash          0358f82b4a336673…
dispatch_receipt_hash 75bce8c7ba304fc0…
evidence_package_hash 170738bbae9c1660…
validation_ok         True          validation_reasons []
requirement states    5 × SATISFIED
```

**So G1 is still NOT satisfied, and this document says so.** Fourteen stated rules
for a stateful LRU cache — cumulative counters surviving `clear()`, `purge` that is
neither hit nor miss, `set_capacity` evicting and counting, MRU-first `keys()`,
equality-not-identity keys — were all implemented correctly on the first attempt,
from a 1 258-token prompt, in 20 seconds.

### The standing result, stated plainly

`local-rtx4500` has now one-shotted **every** fully-specified packet in this
series: 6 rules (G1a), 9 interacting rules (G1b), multi-form assertion parsing
(G1c), a 7-rule algorithmic graph task (G1d2), and a 14-rule stateful class (G1e).
Five packets, five first-attempt passes.

The actionable reading is not "the model is good" but **"difficulty within the
fully-specified bounded-packet shape is not what produces a repair case"**. A
self-contained pure or stateful module is exactly what this target is good at.
A genuine G1 case therefore has to come from a different axis — realistically sized
multi-file engineering work against existing repository constraints, where the
first attempt fails because the change exceeds one fresh context, not because a
rule was subtle. That is a task-shape conclusion, and it is what this ticket should
pursue next. Manufacturing a failure is still not on the table: a synthetic defect
would prove nothing about the repair loop.

### Harness defect that preceded this run (recorded, not hidden)

The FIRST attempt at E2 escalated — and it was **my defect, not the model's**.

The case declared its hidden verifier at `tests/test_bounded_cache_ps635.py`, while
the file actually lives at `scripts/ps635-live/hidden/test_bounded_cache_ps635.py`
(deliberately outside `tests/` so the repo suite is unaffected). The VerificationPlan
sealed the string `<absent>` as the verifier's identity, pytest exited **4** with
`ERROR: file or directory not found`, the repair packet was built from an empty
summary, attempt 2 reproduced the identical fingerprint, and the loop escalated
correctly on a failure that judged *nothing*. Two model turns were spent, and the
model's code was never read.

This is the FOURTH time in this work that a red result came from the harness (a
streaming tool-call parser, the Slice A packet contract, a wrong G1d literal, now a
verifier path). It is recorded as such and is **NOT** counted as evidence about the
model's repair ability.

Two fixes came out of it, and both are in the shipped code with mutation coverage:

1. **`VerificationPlan.missing_verifiers`**, and `build_execution_package` now
   REFUSES a plan that names a verifier artifact with no real digest. A sealed
   `<absent>` is not an identity. This would have stopped the run before dispatch.
2. A dormant rule was **withdrawn on this evidence**: the validator had required
   all verification receipts to agree on the tree they ran against. A repair run
   verifies a *different* tree after each attempt — that is what a repair is — so
   the rule would have rejected correct evidence, and worse, would have pushed
   authors into recording one shared digest for several different trees. Its
   replacement is stronger: a caller-supplied `current_source` must match a tree
   that was actually verified, and a verified tree that differs from the sealed
   input must be explained by a recorded write.

---

## Regression state at `aedda95e` (this session's head)

Full repository suite: **3 failed, 3794 passed, 3 skipped** in 282.7 s.

```
FAILED tests/test_gpu_compose_standalone.py::test_nvidia_standalone_equals_base_plus_overlay
FAILED tests/test_gpu_compose_standalone.py::test_amd_standalone_equals_base_plus_overlay
FAILED tests/test_gpu_compose_standalone.py::test_amd_odysseus_adds_only_overlay
```

**Classification, stated as narrowly as the evidence allows.** PS-579 records these
same three as pre-existing at `56ff059f`. This session did NOT run an exact-base
comparator, so it does **not** claim they are proven pre-existing — that claim
requires a baseline VerificationReceipt bound to the base SHA with a matching
normalized failure fingerprint, and none was produced here.

What IS proven, mechanically: `git diff --name-only 56ff059f..HEAD` lists every file
every commit on this branch touched, and it contains **no compose file and not the
failing test file**. So this branch cannot have caused these three failures. That is
a weaker statement than a baseline receipt and it is the one made.

Everything else is green, including the 119 new PS-638 tests, the 19-test hidden
verifier against the committed artifact, and the pre-existing PS-635/PS-632 suites.
`scripts/ps638-mutations.sh`: 20 mutations applied, all 20 killed, baseline-gated.

---

## Process traps found this session (recorded once, with evidence)

**T1 — a mutation harness killed mid-run contaminates the next run.**
The first `bash scripts/ps638-mutations.sh` invocation exceeded the tool's 30 s
limit and was terminated while M17's mutation was applied. `src/evidence_package.py`
was left with `if False:` in place of `if data is None:`. The next invocation then
reported a red test under EVERY mutation (an unrelated test proved nothing) and M17
itself reported `APPLY-FAILED`, because it was already applied. A mutation result
from a contaminated baseline is worthless. Fixed in the script: it now runs the
suite once BEFORE mutating and refuses to continue unless the baseline is green,
saying so explicitly. Evidence: the mutation logs at `/tmp/ps638-mut{,2,3,4,5}.log`
during this session, and the script's baseline-gate text.

**T2 — `run_commands` entries execute CONCURRENTLY, and it bit twice.**
(a) A single call pairing "copy the reference implementation and run the hidden
verifier" with "copy a deliberately wrong implementation" had the second entry
overwrite `src/bounded_cache.py` before the first entry's pytest read it, so the
*reference* run reported the wrong implementation's failures and looked like a
regression in the verifier.
(b) A `grep` for mutation leftovers ran while a mutation was applied and reported a
mutation that the restore had already reverted.
Both were fixed by issuing dependent steps as ONE ordered shell command. This is the
same trap the operator's notes already list; it is recorded here because it produced
two actually-misleading intermediate results this session, which is stronger evidence
than a warning.

**T3 — a hidden verifier must be checked against a reference implementation first.**
Two real bugs in the G1e verifier were found this way before the run: an ordering
assertion placed after a `get()` that legitimately reorders, and an identity
assertion comparing two literal tuples that CPython constant-folds into one object
(`("k",) is not ("k",)` is False). Without a reference implementation to run against,
the second would have produced a permanent false red on a correct artifact — the
same failure mode as G1d, where a wrong hardcoded literal in the harness made the
loop escalate on a defect that did not exist.




