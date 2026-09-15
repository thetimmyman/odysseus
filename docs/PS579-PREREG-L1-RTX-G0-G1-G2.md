# PS-579 l1-interface RTX4500 G0/G1/G2 paired preregistration

This is the second internal source-bound PS-579 task. It is sealed before the
first arm execution; outcomes cannot change the arm definitions, scoring, retry
rule, or stop rule.

## Task and source identity

- Repository: `pewdiepie-archdaemon/odysseus`
- Evaluation worktree commit: `61719cbc146ee186c6ff6ebebe1280b36b46f787`
- Exact packet source base: `9efb53bc248d82a866b6c7011864abdf3e6c79e7`
- Packet: `L1-ledger-note-rtx`
- Case: `l1-interface`
- Write scope: `src/ledger_note.py`; absent at the exact packet base
- Canonical interface digest: `1d6eda6c43ddca1a`
- Interface fields: `subject_name` required `str`; `verbatim_lines` required
  `list[str]`; `block_reason` optional `str`
- Verifier: `tests/test_ledger_note_ps635.py`
- Verifier SHA-256: `ae04259cd7f483f6b04ca8b250b5f0ef07ce8bd6f9bd58a56bb04ba0b2598489`
- Hidden fixture: none separate from the harness-owned verifier; the verifier
  itself is sealed into the VerificationPlan before dispatch

The canonical prior execution is documented in
`docs/local-targets/PS638-LIVE-PREREG.md`: run
`l1-interface-20260914T170257Z`, 8 passed, one attempt, zero repairs, one model
call, 63.2 seconds, valid ledger chain, and all five evidence requirements
satisfied. Its raw PS-638 package is not present in the durable PS-579 evidence
directory; this replay preserves a new complete chain.

## Execution profile gate

- Target: `local-rtx4500` / host `minipc`
- Profile: `local-rtx4500:ollama-cuda:0.32.11:qwen3.8:27b:unknown:ctx32768:d94d964641c7`
- Runtime: Ollama `0.32.11`, CUDA backend
- Model digest: `d94d964641c751ddc0ae3d905770095e1c13bc2615964fdc41d0e89ccdc26f28`
- Configured/served/measured-safe context: `32768` / `32768` / `32768`
- PS-632 current receipt at preregistration seal: `38263126c5312bd34c4597fd29e2039f47452ea9648c2f64234214952ac4c968`
- Receipt store: `/home/tdefreest/scratch/ps632-store`
- Store verification must be green and the receipt must be live immediately
  before each arm; any material profile identity drift stops the block

## Arms and changed variable

The only changed variable is orchestration arm. Packet, source base, interface,
verifier, target preference, profile, permissions, context and deterministic
scoring remain fixed.

- **G0:** existing raw/local worker baseline; one dispatch and one deterministic
  verifier, no repair retry or replanner
- **G1:** frozen immutable WorkPacket -> fresh bounded context -> worker ->
  deterministic verifier -> compact repair evidence -> fresh retry only after a
  genuine qualifying technical failure
- **G2:** frozen G1 plus bounded validated replanning only at the existing eligible
  no-progress/repeated-failure boundary; a PASS ends with zero planner calls

## Hypotheses

1. A first-attempt PASS produces no G1 repair call.
2. A first-attempt PASS produces no G2 planner/replanner call.
3. G1/G2 happy-path overhead is measured, not assumed.
4. Repair uplift and replan uplift remain unobserved unless naturally reached.

## Scoring, failure taxonomy, retry and stop rules

- Deterministic PASS is the verifier exiting 0 with 8 passed and 0 failed.
- Terminal success is `ACCEPTED_CANDIDATE` with a validator-`VERIFIED` PS-638
  EvidencePackage. It is not delivered or landed work.
- Report first-attempt PASS separately from post-repair PASS.
- G0 stops after its one attempt. G1/G2 use the existing bounded case budget;
  retry occurs only after a genuine technical verifier failure.
- G2 replanning is permitted only where frozen semantics identify a genuine
  no-progress/repeated-failure boundary. No synthetic failure is introduced.
- Classify, before scoring: `PACKET_INVALID`, `CONTEXT_BLOCKED`, infrastructure/
  runtime, technical implementation failure, deterministic verifier failure,
  no-progress/replan-eligible, and evidence-chain failure.
- Stop an arm on packet invalidity, context blocking, infrastructure/runtime
  failure, broken receipt binding, broken evidence binding, ambiguous verifier
  identity, or material profile drift. These are not worker repair successes or
  failures.

## Evidence and metrics

Every arm must retain raw output and the complete chain:

`ExecutionPackage -> DispatchDecisionReceipt -> PS-632 receipt reference ->
AttemptReceipt(s) -> VerificationReceipt(s) -> EvidencePackage -> validator`

Record deterministic outcome, attempts, repairs, replanner calls, model calls,
prompt/completion tokens, wall-clock time, verifier time, runtime/API latency
where measurable, failure class, accepted-candidate status, and chain validity.

Primary descriptive rate: `accepted candidates / wall-clock hours`. With two
internal task identities, report paired task deltas and cumulative totals, but
make no statistically established G1/G2 uplift claim.

The block stops after all three `l1-interface` arms are sealed. Framework,
Terminal-Bench, Pi Local Coding Bench, PS-578 landing work, challenger behavior,
G1/G2 changes and manufactured repair/replan events are out of scope.
