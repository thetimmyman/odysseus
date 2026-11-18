#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")/.."
mode=${1:-setup}
[[ $# -le 1 && $mode =~ ^(setup|--check|--verify)$ ]] || { echo 'usage: scripts/agent-bootstrap.sh [--check|--verify]' >&2; exit 2; }
for tool in uv mise; do command -v "$tool" >/dev/null || { echo "missing $tool" >&2; exit 1; }; done
if [[ $mode != --check ]]; then
    if [[ ! -e .venv ]]; then uv venv --python 3.11 .venv; fi
    [[ ! -L .venv ]] || { echo 'use a private worktree venv' >&2; exit 1; }
    uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt
    if ! mise where node@20 >/dev/null 2>&1; then mise install node@20; fi
fi
[[ -x .venv/bin/ruff && -x .venv/bin/pytest ]] || { echo 'missing the development venv' >&2; exit 1; }
uv pip check --python .venv/bin/python
node_dir=$(mise where node@20)
export PATH="$PWD/.venv/bin:$node_dir/bin:$PATH"
if [[ ! -f node_modules/.package-lock.json ]] || ! npm ls --depth=0 >/dev/null 2>&1; then
    [[ $mode != --check ]] || { echo 'missing node dependencies' >&2; exit 1; }
    npm ci --ignore-scripts --no-audit --no-fund
fi
if [[ $mode == --verify ]]; then
    scripts/lint-ratchet-selftest.sh
    python -m pytest -q tests/test_build_identity.py tests/test_deployment_controls.py
fi
