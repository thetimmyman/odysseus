# Odysseus Agent Context

Canonical rules for automated agents operating in this repository.

## Repo facts
- Path: `/app/work/odysseus`
- Origin: `git@github.com:thetimmyman/odysseus.git`
- Active branch: `dev`
- Stable branch: `main`

## Agent rules
- Agents MUST run `scripts/agent-preflight.sh` before editing any files.
- NEVER implement on `main`, `dev-preview`, or `fork-baseline`.
- Work branches MUST be named `work/*`.
- Pull-request target is `dev`.
- Do NOT commit `.bak` files, temp patches, static exports, or unrelated style changes.
- Specs should live in `docs/specs`, not in active PDF/document tabs.
