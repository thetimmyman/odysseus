# Durable Agent Continuation State

## Problem

Long implementation tasks currently require repeated manual `continue` prompts even when the agent is not blocked. This creates avoidable interruption, context drift, duplicated planning, and inconsistent task-mode/tool-policy behavior across long coding sessions.

Observed failure modes:

- The agent stops after planning or a partial file group instead of continuing through the implementation checklist.
- The frontend stream or output window ends, and the agent waits for the user even though the next action is known.
- `Continue` can resume without the prior implementation mode, workspace, plan, active checklist, or tool policy.
- Attached PDFs/specifications can cause the agent to drift into document-edit mode instead of treating documents as reference material for code implementation.
- Long-running repository work becomes unreliable because continuation depends on manual user prompts.

## Goal

Implement durable continuation state so long implementation tasks can proceed until they are complete, verified, or genuinely blocked.

## Required behavior

The agent must persist and restore the following across stream boundaries, output truncation, user `continue` prompts, and reconnects:

- Active task mode, such as code implementation vs. document editing.
- Workspace/repository path.
- Current plan and checklist.
- Current checklist item.
- Files touched so far.
- Pending edits or unresolved conflicts.
- Tool policy and available tool class.
- Verification state, including commands run and pass/fail results.
- Stop reason, if any.

## Auto-continue rules

For code-implementation tasks, the agent should continue automatically until one of these stop reasons applies:

1. The implementation is complete and verified.
2. A required secret, credential, or user-specific value is missing.
3. A destructive, production, or external mutation would be required.
4. Tests fail and user judgment is needed to choose between approaches.
5. A merge conflict or code ambiguity requires explicit user decision.
6. The task is blocked by missing repository state or unavailable tooling.

The agent should not stop merely because:

- It completed a planning step.
- It finished one file group.
- It reached an output summary boundary.
- It needs to run the next obvious verification command.
- A specification document is attached.

## Continue command behavior

When the user sends `continue`, the system must restore the same:

- Task mode.
- Workspace.
- Plan/checklist.
- Current step.
- Tool policy.
- Relevant context from previous tool outputs.
- Verification state.

The continuation response should start by stating:

- Current checklist item.
- Files already changed.
- Next action.
- Whether the task is continuing, blocked, failed, or complete.

## Document/spec handling

Attached specifications, PDFs, and review packets must be treated as reference-only unless the user explicitly asks to edit them.

For code implementation requests:

- Do not activate document-edit tools just because a document is open.
- Do not mutate attached PDFs or generated document artifacts.
- Do not make active-document context override repository/workspace context.
- Preserve code-implementation mode when the user says `continue`.

## Implementation notes

Prefer an additive design:

- A durable continuation-state model/table or JSON store.
- A small state machine for stop reasons.
- Explicit task-mode and tool-policy fields.
- Backend route or session metadata support for reading/writing continuation state.
- Frontend handling for stream-boundary and resume UX.
- Tests for mode preservation and auto-continue behavior.

## Acceptance criteria

- Long code-implementation tasks can auto-continue through multiple checklist steps without manual prompts.
- `continue` resumes the same workspace, mode, checklist, and tool policy.
- Attached documents remain reference-only during code implementation unless explicitly edited.
- Stop reasons are explicit and machine-readable.
- A completed run reports files changed, commands run, pass/fail status, and remaining gaps.
- Tests cover continuation across stream boundaries, max-output truncation, attached-document context, and user `continue` prompts.

## Non-goals

- Do not add autonomous production deployment.
- Do not allow destructive repository or external-service mutations without explicit user authorization.
- Do not bypass existing tool-security boundaries.
- Do not silently continue after a test failure that requires design judgment.
