# PR Blocker Audit

`scripts/pr_blocker_audit.py` is a small, read-only triage helper for maintainers who need to inspect open pull request overlap before reviewing or starting related work.

It is a triage helper, not a replacement for maintainer judgment.

## What it does

- Reads open PR metadata from a local JSON file or from `gh`.
- Reports files touched by more than one open PR.
- Groups active work into broad code areas.
- Ranks PRs with a deterministic heuristic score.
- Flags possible duplicate candidates based on title keyword overlap and changed-file similarity.
- Suggests quieter areas for conservative new work.
- Prints Markdown by default, compact terminal output when requested, or machine-readable JSON.

## What it does not do

- It does not post comments.
- It does not review, approve, label, close, merge, or otherwise mutate PRs.
- It does not add or run GitHub Actions.
- It does not import the Odysseus application package.
- It does not claim that a PR is definitely blocked or duplicated.

## Read-only safety guarantee

Offline mode only reads a local JSON file. Live mode runs read-only GitHub CLI commands:

```bash
gh pr list --repo OWNER/REPO --state open --limit 1000 --json number,title,author,files,mergeStateStatus,reviewDecision,updatedAt,url
```

If a PR from that list has missing or empty changed-file metadata, live mode fills it with read-only per-PR REST calls:

```bash
gh api --paginate "repos/OWNER/REPO/pulls/NUMBER/files?per_page=100"
```

If that GraphQL-backed command fails, it falls back to:

```bash
gh api --paginate "repos/OWNER/REPO/pulls?state=open&per_page=100"
```

Per-PR file fetching makes live overlap results useful, but it can be slower on repositories with hundreds of open PRs.

## Generate input JSON

For repeatable offline audits, capture PR metadata first:

```bash
gh pr list --repo OWNER/REPO --state open --limit 1000 --json number,title,author,files,mergeStateStatus,reviewDecision,updatedAt,url > open-prs.json
```

## Run offline mode

```bash
python3 scripts/pr_blocker_audit.py --input open-prs.json
```

## Run live mode

```bash
python3 scripts/pr_blocker_audit.py --repo OWNER/REPO
```

Live mode fetches up to 1000 open PRs by default. Use `--limit` to cap how many open PRs are fetched and analyzed, and `--top` to cap how many rows are displayed in ranked sections:

```bash
python3 scripts/pr_blocker_audit.py --repo OWNER/REPO --limit 50 --top 10
```

Live mode may take time on large PR queues because it fetches changed-file metadata for each PR that did not include it in the initial list response. Progress is shown on `stderr` by default only when `stderr` is a TTY:

```bash
python3 scripts/pr_blocker_audit.py --repo OWNER/REPO --progress auto
python3 scripts/pr_blocker_audit.py --repo OWNER/REPO --progress always
python3 scripts/pr_blocker_audit.py --repo OWNER/REPO --progress never
```

Use `--quiet` to suppress progress and non-fatal warning output. Progress and warnings never go to `stdout`, so redirected reports and `--output` files remain clean.

For a faster metadata-only scan, skip changed-file metadata entirely:

```bash
python3 scripts/pr_blocker_audit.py --repo OWNER/REPO --no-fetch-files
```

## JSON output

Use `--format json` for machine-readable output suitable for scripting or downstream tooling:

```bash
python3 scripts/pr_blocker_audit.py --input open-prs.json --format json
python3 scripts/pr_blocker_audit.py --input open-prs.json --format json --output report.json
```

JSON output is stable and deterministic for the same input. It uses `sort_keys=True` so field order does not vary between runs. It never includes ANSI escape codes, even with `--color always`. Progress text is always `stderr`-only and never appears in JSON output.

The top-level object contains these keys:

- `summary` — scalar overview: `total_prs_analyzed`, `unique_files_touched`, `prs_missing_changed_file_metadata`, `main_overlap_drivers`, `highest_risk_areas`, `recommended_first_review_target`
- `locked_areas` — list of objects with `area`, `files` (top paths as a string), `prs` (list of PR numbers), `why`, `priority`
- `hot_files` — list of objects with `file`, `pr_count`, `pr_numbers` (list of PR numbers); capped at `--top`
- `review_priorities` — ranked list with `rank`, `number`, `score`, `title`, `url`, `merge_state`, `review_decision`, `reasons` (list); capped at `--top`
- `duplicate_candidates` — list of objects with `pr_numbers` (list) and `titles` (list, one entry per PR in the group)
- `safer_areas` — list of strings

## Write output to a file

```bash
python3 scripts/pr_blocker_audit.py --input open-prs.json --output pr-blocker-report.md
python3 scripts/pr_blocker_audit.py --input open-prs.json --format json --output report.json
```

Markdown and JSON output never include ANSI color codes. ANSI codes are stripped defensively when writing any output file.

## Terminal output and color

Use terminal output for quick interactive scans:

```bash
python3 scripts/pr_blocker_audit.py --input open-prs.json --format terminal
```

Terminal output includes locked areas, hot files, review / blocker priorities, possible duplicate candidates, and safer areas.

Color is readability-only. It is never included in Markdown reports and is stripped defensively when writing output files. Color modes are:

```bash
python3 scripts/pr_blocker_audit.py --input open-prs.json --format terminal --color auto
python3 scripts/pr_blocker_audit.py --input open-prs.json --format terminal --color always
python3 scripts/pr_blocker_audit.py --input open-prs.json --format terminal --color never
```

`--no-color` is kept as an alias for `--color never`. With `--color auto`, color is used only for terminal output on a TTY when `NO_COLOR` is not set and output is not being written to a file.

## Interpret locked areas

Locked areas are broad categories with one or more open PRs. An area is higher priority when several PRs touch it, when PRs share files, or when the highest scoring PR in that area has risk signals. Treat this as a prompt to inspect the PRs together.

`PRs missing changed-file metadata` counts PRs that still had no changed-file paths after live file fetching, or PRs from offline input that did not include files. Those PRs can still appear in area summaries from title matching, but file overlap analysis is weaker for them.

`Docs / tooling / tests` is conservative: runtime PRs are not classified there just because they include tests or README changes. Docs-only, README-only, scripts-only, tests-only, or strongly titled docs/tooling/test work still maps there.

`Other / unclassified` is kept visible for PRs that do not match the area rules. When most of it comes from missing file metadata, the report summarizes that instead of letting long PR lists dominate the locked-area section.

## Interpret duplicate candidates

Duplicate candidates are labeled as possible duplicate / needs human review. The script groups PRs only when their file sets are highly similar and their titles share meaningful keywords. Similar PRs can still be complementary.

## Interpret heuristic scores

The review priority score is deterministic for the same input. Recency is measured against the newest parseable PR update timestamp in the input, and the score uses simple weights for:

- direct auth, bearer-token, API-token, privilege, or permission lifecycle signals
- security, secret, or data exposure keywords
- persistence, migration, database, SQLite, or Postgres keywords
- memory, vector, RAG, embedding, or retrieval keywords
- overlapping changed files
- clean merge state as a small actionability signal
- review state
- recently updated PRs when timestamp data exists

Higher scores mean "inspect earlier", not "correct" or "merge-ready". Broad PRs can score high because they overlap many files and may block other work, but they still need normal review and validation.

Dirty, blocked, conflicting, and unknown merge states are shown as risk/caution reasons. They do not add importance points by themselves.

## Work-item references and readiness

This section documents the boundary between what the PR description gate accepts as a
work-item reference and what it does not decide. It is the documentation half of the
reference-syntax behavior implemented in
[`../.github/scripts/check-pr-description.js`](../.github/scripts/check-pr-description.js),
covered by the contract tests in
[`../.github/scripts/check-pr-description.test.js`](../.github/scripts/check-pr-description.test.js)
and run in CI by
[`../.github/workflows/ci.yml`](../.github/workflows/ci.yml).

### The Linked Issue requirement

Every PR description needs a filled-in **Linked Issue** section containing at least one
recognized reference. The gate reads the description text of the open PR; PRs here target
`dev`, and `main` stays the curated release line. A missing **Linked Issue** section, or a
section with no recognized reference, is reported as a problem and the PR is labelled
`needs work`.

### Supported prefixes

These prefixes are recognized, exactly and only as listed:

- `OD` — native Odysseus work items
- `PS`
- `TMOS`
- `EOT`
- `EST`

GitHub issue references keep working unchanged: a bare `#NNN`, a closing keyword plus a
number (`Fixes #NNN`), or a full issue URL ending in `/issues/NNN`. A description may
carry both a GitHub reference and a prefixed work-item reference; either satisfies the
check.

### Identifier and boundary rules

For the prefixed forms:

- The prefix must be one of the supported set. Any other prefix is not a reference.
- The number must be a positive integer: zero is rejected, and leading zeros are rejected.
- The identifier must stand alone. It may not be glued to surrounding word characters or
  hyphens — no embedded prefixes, no underscore suffixes, no extra hyphenated segments,
  no trailing letters.
- HTML comments are stripped from the description before matching, so a reference that
  exists only inside a comment does not count.

The GitHub numeric forms are looser on the number itself (`#0` matches the pattern); the
positive-integer and leading-zero rules above apply to the prefixed forms only.

### Examples

All identifiers below are synthetic and illustrative.

Accepted:

| Reference | Why |
| --- | --- |
| `OD-7` | supported prefix, positive integer, stands alone |
| `(OD-52)` | surrounding punctuation is a valid boundary |
| `PS-1` | supported prefix, positive integer |
| `TMOS-104` | supported prefix, positive integer |
| `EOT-256` | supported prefix, positive integer |
| `EST-12` | supported prefix, positive integer |
| `Fixes #63` | closing keyword plus issue number |
| `#63` | bare issue number |
| `https://github.com/OWNER/REPO/issues/63` | full issue URL |

Rejected:

| Reference | Why |
| --- | --- |
| `OD-0` | zero is not a positive integer |
| `OD-05` | leading zero |
| `OD-` | empty number |
| `prefixOD-7` | prefix embedded in a longer word |
| `OD-7_suffix` | underscore suffix breaks the boundary |
| `OD-7-extra` | extra hyphenated segment |
| `OD-7x` | non-numeric suffix |
| `<!-- OD-7 -->` | comments are stripped before matching |
| `POS-12` | unsupported prefix |
| `12` | bare number, no prefix and no `#` |
| `See the relevant work item for context.` | prose, no reference at all |

A valid reference satisfies only the **Linked Issue** requirement. It never bypasses the
other description requirements (summary, type of change, duplicate-search checklist,
how-to-test detail).

### What acceptance does not mean

Acceptance is syntactic. The gate pattern-matches the PR description text; it does not
query the tracker, and it never has. A recognized reference therefore does not establish:

- that the work item exists;
- who owns it;
- whether it was admitted, in scope, or already completed;
- any authority to merge.

Likewise, the rankings in this guide — locked areas, hot files, review priority scores,
duplicate candidates — are triage prompts for maintainers. They do not establish readiness
either, and they do not read the tracker. Passing the description gate and passing the
normal review, CI, and branch protections are separate things; the gate is a formatting
check with a label side effect, not an approval and not a merge decision.

## Design note: intentional single-script layout

`pr_blocker_audit.py` is intentionally kept as one standalone script. The goal is to keep this maintainer/contributor workflow helper low-friction while broader repo tooling and test-suite conventions are still evolving. Splitting it into packages or modules is not ruled out, but is deferred until there is a clearer settled pattern to follow.

## Limitations

- Some PRs may still lack changed files if GitHub file metadata calls fail or metadata-only mode is used.
- Area classification is intentionally small and editable.
- Title keyword matching misses semantic duplicates.
- Heuristic scoring cannot know project strategy, reviewer availability, or hidden dependency chains.
- Empty or missing file metadata produces a valid report but weak overlap analysis.

## Validation

```bash
python3 -m py_compile scripts/pr_blocker_audit.py tests/test_pr_blocker_audit.py
python3 -m pytest tests/test_pr_blocker_audit.py -q
python3 scripts/pr_blocker_audit.py --help
node --test .github/scripts/check-pr-description.test.js
git diff --check
```
