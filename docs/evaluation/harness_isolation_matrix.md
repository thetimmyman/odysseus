# Harness-isolation experiment matrix

Reference doc for the harness-isolation experiment (epic OD-19). The purpose of
this matrix is to make the experiment readable as an experiment: exactly one
variable is allowed to vary across arms, everything else is held fixed, and any
result difference is attributed only to the variable that actually changed.

Related docs: `docs/pi-execution-runtime.md` (Pi arm runtime and adapter),
`docs/measured-work-verification.md` (verifier and evidence binding),
`docs/ROUTING-COORDINATOR-MODEL-RESEARCH.md` (fixed provider decisions).

## The matrix

| Arm | Harness (varying) | Execution mode (fixed) | Model (fixed) | Source (fixed) | Verifier (fixed) | Provider (fixed) |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | Pi | Yolo-Auto | qwen3.8-flash | shared task source | shared verifier | shared OpenAI-compatible provider |
| A2 | OpenCode | Yolo-Auto | qwen3.8-flash | shared task source | shared verifier | shared OpenAI-compatible provider |
| A3 | Cline | Yolo-Auto | qwen3.8-flash | shared task source | shared verifier | shared OpenAI-compatible provider |

## Fixed-variable register

- **Execution mode — Yolo-Auto:** the same unattended execution mode/policy is
  configured identically for every arm; no arm gets a per-harness mode tweak.
- **Model — qwen3.8-flash:** one model, same model id and version, served to
  every arm through the same provider endpoint. This matches the fixed-provider
  decision recorded in `docs/ROUTING-COORDINATOR-MODEL-RESEARCH.md`.
- **Source:** every arm starts from the same task source (same worktree base
  commit and task inputs); arms do not cherry-pick their own starting state.
- **Verifier:** all arms are scored by the same existing workspace verifier
  with the same verifier files and digests (see
  `docs/measured-work-verification.md`); no arm-specific verifier or scorer.
- **Provider:** one shared provider endpoint for all arms; no arm may reach a
  different provider, plan, or rate class.

## Single varying variable

The **harness** (Pi, OpenCode, Cline) is the only variable that differs between
arms. Everything listed above must be byte-identical (or digest-identical, where
digests exist) across arms for a run to count as part of this matrix.

## Attribution rule

**A changed result must not be attributed to model quality when harness
behavior changed.** If any fixed variable drifted for an arm — mode, model
build, source snapshot, verifier, or provider path — that arm's result is out
of matrix and must be re-run before any comparison or conclusion is drawn.
Differences between arms are attributable only to the harness itself; the
model is held constant precisely so it cannot be the explanation.
