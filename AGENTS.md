# AGENTS.md — canonical agent guidance for this repository

This file is the instruction source for AI coding agents working in this repo.
`CLAUDE.md` points here. If guidance conflicts, this file wins for agent
behavior; `CONTRIBUTING.md` owns human contribution policy and visual style.
Keep this file short. Do not grow it into a second README.

## What this repo is

Odysseus: a self-hosted personal AI assistant. FastAPI backend (`app.py`),
route modules (`routes/`), core plumbing (`core/`, `src/`), feature services
(`services/`), in-repo MCP servers (`mcp_servers/`), static frontend
(`static/`), pytest suite (`tests/`), CLI/ops entry points (`scripts/`).

## Branch model

- `dev` — integration branch; open PRs against `dev`.
- `main` — curated release line. Never send agent work to `main` directly.
- One fix or feature per PR. No PRs mixing behavior change + formatting + refactor.

## Established implementation patterns

- **Paths and config are constants.** Every persisted file/dir has a named
  constant in `src/constants.py` (`AUTH_FILE`, `DATA_DIR`, `SETTINGS_FILE`,
  ...). Import it. Never re-derive paths from `__file__`, `/app/...`, or
  relative `"data/..."` strings. Internal URLs: `internal_api_base()`.
- Reuse existing helpers, constants, and UI widgets before inventing
  parallel ones. If a value is used twice and has no constant, add one.
- Conventional Commits: `type(scope): summary` (fix/feat/refactor/docs/test/chore/ci).
- Visual style (CSS variables, no Unicode emoji, screenshot required for UI
  changes) is specified in `CONTRIBUTING.md` — it applies to agents too.

## Verification commands — run what is relevant, report exactly what you ran

- **Lint ratchet (required for any PR touching Python):**
  `BASE=<pr-base-sha> scripts/lint-changed.sh`
  Fails on NEW E9/F findings in changed files vs `ci/lint-baseline.json`
  from the base branch. Exit 0 = pass, 1 = new findings, 2 = gate broken.
- Syntax: `python -m compileall -q app.py core routes src services scripts tests`
- JS syntax: `node --check static/js/<file-you-changed>.js`
- Tests: `python -m pytest tests/<relevant>.py` — run the tests that cover
  your change. The full suite is informational only (known flaky/env-dependent
  failures; CI runs it with `continue-on-error`). Do not cite full-suite
  green as a merge gate; it is not one.
- Ratchet self-test: `scripts/lint-ratchet-selftest.sh` (negative controls).

## Hard boundaries — never cross without explicit human authorization

- **Never edit `ci/lint-baseline.json` to make a check pass.** Normal PRs
  may only REDUCE it (via fixing findings). Any growth, and any weakening of
  `ruff.toml`, requires separate explicit review. The gate reads the
  baseline from the base commit, so editing it in your PR does not help.
- **Never add `continue-on-error`, `|| true`, or disabled/skipped tests to
  hide a failure.** A check that cannot run must fail loudly (exit 2), not
  silently pass.
- **`# noqa` suppression is forbidden by policy.** The ratchet cannot
  technically detect it (ruff honors noqa by design) — reviewers must
  check changed diffs for added `# noqa` comments. Known gap, stated plainly.
- Run the gate **after committing**: it refuses to run against a dirty
  Python working tree (its file list comes from HEAD, ruff reads disk).
- **Never mass-format or bulk-autofix existing application code** (no
  repo-wide `ruff --fix`, no blanket reformat).
- **Never commit secrets**: API keys, tokens, private logs, private hostnames,
  personal data. See `SECURITY.md` and `THREAT_MODEL.md`.
- **Authorization checks are load-bearing.** Routes/services enforce owner
  scoping (session/API-token owner checks, `can_use_*` gates). Never bypass,
  stub, or loosen an auth/authz check to make a test pass.
- **Product isolation.** Odysseus data, credentials, and queues stay inside
  Odysseus. Never wire another product's database, credentials, or services
  into it for convenience.
- Production restarts, deployments, and repository-protection changes are
  out of scope for agents.

## Evidence before claiming work complete

State which commands you ran and their exit codes. For UI changes, a
screenshot of the change in the running app is required (`CONTRIBUTING.md`).
If you could not run a check, say so explicitly — silence is not success.
Projected or intended improvements are not results.

## Dev tooling

- Dev-only lint: `pip install -r requirements-dev.txt` (pins `ruff==0.16.8`).
- Lint policy: `ruff.toml` — day-one selection is `E9` + `F` only.
- Ratchet: `scripts/lint-changed.sh` (docs in its header: rename, delete,
  and baseline-reduction behavior).
