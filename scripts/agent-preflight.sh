#!/usr/bin/env bash
set -euo pipefail

# Always operate from the canonical repo root, regardless of caller's CWD.
cd /app/work/odysseus

EXPECTED_TOP="/app/work/odysseus"
EXPECTED_REMOTE="git@github.com:thetimmyman/odysseus.git"

top="$(git rev-parse --show-toplevel 2>/dev/null || true)"
branch="$(git branch --show-current 2>/dev/null || true)"
origin="$(git remote get-url origin 2>/dev/null || true)"

echo "== Odysseus agent preflight =="
echo "top:    ${top}"
echo "branch: ${branch}"
echo "origin: ${origin}"
echo

if [ "$top" != "$EXPECTED_TOP" ]; then
  echo "STOP: wrong repo path. Expected $EXPECTED_TOP"
  exit 2
fi

if [ "$origin" != "$EXPECTED_REMOTE" ]; then
  echo "STOP: wrong origin remote. Expected $EXPECTED_REMOTE"
  exit 3
fi

case "$branch" in
  dev|work/*)
    ;;
  main|dev-preview|fork-baseline|"")
    echo "STOP: unsafe branch for implementation: $branch"
    exit 4
    ;;
  *)
    echo "WARN: non-standard branch name: $branch"
    ;;
esac

echo "== status =="
git status --short
echo

echo "== recent commits =="
git log --oneline -5 --decorate
echo

echo "== remote reachability =="
git fetch origin --prune --dry-run >/dev/null 2>&1 || {
  echo "STOP: cannot reach origin"
  exit 5
}

echo "PASS: canonical repo verified"
