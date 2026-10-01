#!/usr/bin/env bash
# lint-changed.sh — ratchet gate for Python lint findings (Stage 1).
#
# Rule: changed Python files may retain baseline findings but must not add
# new ones. "New" is tracked per (file, rule + message) occurrence count,
# NOT as a total count — fixing one old finding never buys permission to
# add a different one, and harmless line shifts are tolerated because the
# baseline records no line numbers.
#
# The baseline is read from the BASE commit (git show), never from the
# working tree: a PR cannot bless its own violations by editing the
# baseline. A PR that reduces the baseline is fine (informational); a PR
# that grows it fails and needs separate explicit review.
#
# Usage:  BASE=<commit-ish> scripts/lint-changed.sh
#   CI supplies the PR base SHA. No default branch name is assumed.
#
# Exit codes:
#   0  pass (no new findings; nothing changed also passes)
#   1  gate failure (new findings, or baseline growth in this PR)
#   2  gate cannot run (missing BASE, unresolvable base, missing/broken
#      ruff, missing/broken baseline or config) — fails closed, never a
#      silent success.
#
# Documented behaviors:
#   - Rename: the baseline tracks paths. Renaming a file that still has
#     baseline findings fails (the new path is a new file). Fix the
#     findings first, or land a reviewed baseline move as its own change.
#   - Delete: removing a file leaves a stale baseline entry; harmless.
#     Prune stale entries in a reduction-only baseline PR.
#   - Reduction: fixing findings is always allowed; land the matching
#     baseline reduction in the same or a follow-up PR (reduction-only).
set -u

die2()   { echo "lint-changed: ERROR (gate cannot run): $*" >&2; exit 2; }
fail1()  { echo "lint-changed: FAIL: $*" >&2; exit 1; }

BASE="${BASE:-${1:-}}"
[ -n "$BASE" ] || die2 "BASE (comparison commit) is required, e.g. BASE=\$PR_BASE_SHA scripts/lint-changed.sh"
git rev-parse --verify "${BASE}^{commit}" >/dev/null 2>&1 || die2 "BASE '$BASE' does not resolve to a commit"

# --- locate ruff (fail closed if unavailable) -------------------------------
RUFF="${RUFF_BIN:-}"
if [ -z "$RUFF" ]; then
  if command -v ruff >/dev/null 2>&1; then
    RUFF="ruff"
  elif python3 -m ruff --version >/dev/null 2>&1; then
    RUFF="python3 -m ruff"
  else
    die2 "ruff not found (pip install -r requirements-dev.txt) — cannot certify, failing closed"
  fi
fi
$RUFF --version >/dev/null 2>&1 || die2 "ruff present but not runnable — failing closed"

# --- baseline comes from the BASE commit, never the working tree ------------
BASELINE_TMP="$(mktemp)"
RAW_TMP="$(mktemp)"
trap 'rm -f "$BASELINE_TMP" "$RAW_TMP"' EXIT
if ! git show "${BASE}:ci/lint-baseline.json" > "$BASELINE_TMP" 2>/dev/null; then
  # Bootstrap: the baseline itself is being introduced by this change.
  # Allowed ONLY when this change adds the baseline AND touches no Python
  # files — with no changed .py files there is nothing that could be
  # self-blessed. Any other shape fails closed.
  if git cat-file -e "HEAD:ci/lint-baseline.json" 2>/dev/null \
     && [ -z "$(git diff --name-only --diff-filter=ACMR "${BASE}" HEAD -- '*.py')" ]; then
    echo "lint-changed: BOOTSTRAP — baseline introduced by this change; no changed Python files to lint. Pass." >&2
    exit 0
  fi
  die2 "no ci/lint-baseline.json at base '$BASE' and this is not a clean baseline bootstrap — cannot certify, failing closed"
fi

# --- changed Python files (added/copied/modified/renamed) -------------------
mapfile -t CHANGED < <(git diff --name-only --diff-filter=ACMR "${BASE}" HEAD -- '*.py')

# --- config sanity: a change that touches ruff.toml must actually load -----
# Without this, a PR that breaks the config while touching no .py file
# would slip past the early exit below with a silently dead linter.
if git diff --name-only "${BASE}" HEAD -- ruff.toml | grep -q .; then
  set +e
  echo "" | $RUFF check --config ruff.toml - >/dev/null 2>&1
  crc=$?
  set -e
  [ "$crc" -le 1 ] || die2 "ruff.toml changed and does not load (ruff exit $crc) — failing closed"
fi

if [ "${#CHANGED[@]}" -eq 0 ]; then
  echo "lint-changed: no changed Python files — nothing to check"
  exit 0
fi

# --- run ruff on changed files only ----------------------------------------
set +e
$RUFF check --config ruff.toml --output-format=json -- "${CHANGED[@]}" > "$RAW_TMP" 2>/tmp/lint-changed-ruff-stderr.$$
rc=$?
set -e
if [ "$rc" -gt 1 ]; then
  cat /tmp/lint-changed-ruff-stderr.$$ >&2 || true
  die2 "ruff exited $rc (configuration/tool error) — failing closed"
fi

# --- compare per (file, rule+message) occurrence counts --------------------
python3 - "$BASELINE_TMP" "$RAW_TMP" "${BASE}" <<'PYEOF'
import json, sys, collections

baseline_path, raw_path, base_sha = sys.argv[1], sys.argv[2], sys.argv[3]

def load_json(path, what):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError) as e:
        print(f"lint-changed: ERROR (gate cannot run): {what} is unreadable/invalid: {e}", file=sys.stderr)
        sys.exit(2)

baseline = load_json(baseline_path, "ci/lint-baseline.json (from base)")
if not isinstance(baseline.get("findings"), dict):
    print("lint-changed: ERROR (gate cannot run): baseline has no 'findings' object", file=sys.stderr)
    sys.exit(2)
base_findings = baseline["findings"]

cand = collections.defaultdict(collections.Counter)
for f in load_json(raw_path, "ruff JSON output"):
    import os
    p = f.get("filename", "")
    p = os.path.relpath(p, os.getcwd()) if os.path.isabs(p) else p
    cand[p][f"{f.get('code')} {f.get('message')}"] += 1

violations = []
for path, ctr in sorted(cand.items()):
    allowed = base_findings.get(path, {})
    if not isinstance(allowed, dict):
        allowed = {}
    for key, n in sorted(ctr.items()):
        cap = allowed.get(key, 0)
        if n > cap:
            violations.append((path, key, cap, n))

# informational: findings fixed vs baseline (eligible for baseline reduction)
fixed = 0
for path, keys in base_findings.items():
    if not isinstance(keys, dict):
        continue
    for key, cap in keys.items():
        n = cand.get(path, collections.Counter()).get(key, 0)
        if n < cap:
            fixed += cap - n

if violations:
    print(f"lint-changed: {len(violations)} new lint finding kind(s) vs baseline at {base_sha}:", file=sys.stderr)
    for path, key, cap, n in violations:
        where = "new (baseline has none)" if cap == 0 else f"baseline allows {cap}, found {n}"
        print(f"  {path}: {key} — {where}", file=sys.stderr)
    print("Fix the new findings. Do NOT edit ci/lint-baseline.json or ruff.toml to pass this gate.", file=sys.stderr)
    sys.exit(1)

print(f"lint-changed: PASS — no new E9/F findings in {len(cand)} changed file(s)"
      + (f" ({fixed} baseline finding(s) fixed; a reduction-only baseline update is welcome)" if fixed else ""))
PYEOF
gate_rc=$?

# --- belt-and-braces: did this change grow the baseline file itself? -------
if [ $gate_rc -eq 0 ] && git diff --name-only "${BASE}" HEAD -- ci/lint-baseline.json | grep -q .; then
  PR_BASELINE_TMP="$(mktemp)"; trap 'rm -f "$BASELINE_TMP" "$RAW_TMP" "$PR_BASELINE_TMP"' EXIT
  git show "HEAD:ci/lint-baseline.json" > "$PR_BASELINE_TMP" 2>/dev/null || die2 "baseline deleted in HEAD — baseline removal requires explicit review"
  if ! python3 - "$BASELINE_TMP" "$PR_BASELINE_TMP" <<'PYEOF'
import json, sys
old = json.load(open(sys.argv[1]))["findings"]
new = json.load(open(sys.argv[2]))["findings"]
def flat(d):
    out = {}
    for path, keys in d.items():
        if isinstance(keys, dict):
            for k, v in keys.items():
                out[(path, k)] = v
    return out
o, n = flat(old), flat(new)
growth = [(k, o.get(k, 0), v) for k, v in n.items() if v > o.get(k, 0)]
if growth:
    print(f"baseline growth: {len(growth)} entr(ies) added/increased", file=sys.stderr)
    for (k, a, b) in growth[:10]:
        print(f"  {k[0]}: {k[1]} — {a} -> {b}", file=sys.stderr)
    sys.exit(1)
print("lint-changed: baseline touched — reduction-only, allowed")
PYEOF
  then
    fail1 "this PR grows ci/lint-baseline.json — baseline expansion requires separate explicit review"
  fi
fi

exit $gate_rc
