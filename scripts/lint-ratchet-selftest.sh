#!/usr/bin/env bash
# lint-ratchet-selftest.sh — negative controls for the lint ratchet gate.
#
# Builds throwaway synthetic git repos under /tmp and runs the REAL
# scripts/lint-changed.sh against them through the same entry point CI
# uses. Never touches the working application or this repo's history.
#
# Controls (expected exit codes):
#   1  tolerated existing finding .......................... 0
#   2  new undefined name in changed file ................. 1
#   3  new syntax error in changed file ................... 1
#   4  fix one old finding + add a different one
#      (total count unchanged) ............................ 1
#   5  candidate edits its own baseline to hide a new
#      violation ......................................... 1
#   6a harmless line shift of a baseline finding ......... 0
#   6b rename of a file carrying baseline findings ........ 1  (documented)
#   7a BASE unset ........................................ 2
#   7b ruff unavailable (RUFF_BIN override) ............... 2
#   7c base commit has no baseline file ................... 2
#   7d broken ruff.toml .................................. 2
#   8a clean baseline bootstrap (no .py changes) ......... 0
#   8b bootstrap with .py changes ....................... 2
#   9  baseline-only growth (clean py change) ........... 1
#  10  baseline deleted in HEAD (with py change) ....... 2
#  11  BASE unresolvable ............................... 2
#  12  weakened ruff.toml (select=["E9"]) ............. 2  (canary)
#  13  F811 line shift tolerated (normalized keys) .... 0
#  14  dirty working tree .............................. 2
#
# Exit: 0 only if every control produced its expected code.
set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
GATE="$REPO_ROOT/scripts/lint-changed.sh"
RUFF_CFG="$REPO_ROOT/ruff.toml"
[ -x "$GATE" ] || { echo "selftest: gate script missing/not executable: $GATE" >&2; exit 2; }

PASS=0; FAIL=0
WORKROOT="$(mktemp -d /tmp/lint-ratchet-selftest.XXXXXX)"
trap 'rm -rf "$WORKROOT"' EXIT

# make_repo <name>: creates a synthetic repo at $WORKROOT/<name> whose base
# commit contains: legacy.py with one baseline F821 finding, clean app.py,
# ci/lint-baseline.json generated from that state, ruff.toml.
make_repo() {
  local d="$WORKROOT/$1"
  mkdir -p "$d/ci"
  git -C "$d" init -q
  git -C "$d" config user.email selftest@example.invalid
  git -C "$d" config user.name selftest
  cp "$RUFF_CFG" "$d/ruff.toml"
  printf 'def f():\n    return UndefinedThing\n' > "$d/legacy.py"
  printf 'x = 1\n' > "$d/app.py"
  printf 'import sys\nimport sys\nprint(sys)\n' > "$d/f811.py"
  ( cd "$d" && python3 - <<'PY'
import json, collections, subprocess, os, re
raw = subprocess.run(['ruff','check','--config','ruff.toml','--output-format=json','.'],
                    capture_output=True, text=True).stdout
agg = collections.defaultdict(collections.Counter)
for f in json.loads(raw):
    p = os.path.relpath(f['filename'], os.getcwd())
    agg[p][re.sub(r'line \d+', 'line N', f"{f['code']} {f['message']}")] += 1
out = {"version": 1, "generated_from_base": "selftest-base", "tool": "ruff",
       "rules": ["E9", "F"],
       "findings": {k: dict(sorted(v.items())) for k, v in sorted(agg.items())}}
json.dump(out, open('ci/lint-baseline.json', 'w'), indent=1, sort_keys=True)
PY
  ) || { echo "selftest: baseline generation failed" >&2; exit 2; }
  git -C "$d" add -A && git -C "$d" commit -qm base
  echo "$d"
}

# check <label> <expected-exit> -- run gate inside repo dir (cwd passed via $1st arg of run)
run_gate() { # <repo> [extra env assignments as prefix string]
  local d="$1"; shift
  ( cd "$d" && env "$@" "$GATE" >"$WORKROOT/last_gate_out.txt" 2>&1; echo $? )
}

expect() { # <label> <expected> <actual>
  if [ "$2" = "$3" ]; then
    echo "PASS  $1 (exit $3 as expected)"; PASS=$((PASS+1))
  else
    echo "FAIL  $1 (expected exit $2, got $3)"; FAIL=$((FAIL+1))
    [ -f "$WORKROOT/last_gate_out.txt" ] && sed 's/^/      | /' "$WORKROOT/last_gate_out.txt" | head -12
  fi
}

# --- control 1: existing baseline finding tolerated ------------------------
d=$(make_repo c1)
git -C "$d" commit -q --allow-empty -m noop
printf '# touched\nx = 1\ny = 2\n' > "$d/app.py"
git -C "$d" commit -qam change
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
expect "1 tolerated existing finding" 0 "$(run_gate "$d" "BASE=$base")"

# --- control 2: new undefined name -----------------------------------------
d=$(make_repo c2)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
printf 'print(brand_new_undefined_name)\n' >> "$d/app.py"
git -C "$d" commit -qam new-undefined
expect "2 new undefined name" 1 "$(run_gate "$d" "BASE=$base")"

# --- control 3: new syntax error -------------------------------------------
d=$(make_repo c3)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
printf 'def broken(:\n    pass\n' >> "$d/app.py"
git -C "$d" commit -qam syntax-error
expect "3 new syntax error" 1 "$(run_gate "$d" "BASE=$base")"

# --- control 4: fix one old finding, add a different one (same total) -----
d=$(make_repo c4)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
printf 'UndefinedThing = 1\ndef f():\n    return UndefinedThing\n' > "$d/legacy.py"   # fixes F821
printf 'print(another_undefined)\n' >> "$d/app.py"                                  # adds F821
git -C "$d" commit -qam swap-finding
expect "4 fix-one-add-another (same total)" 1 "$(run_gate "$d" "BASE=$base")"

# --- control 5: candidate baseline edit cannot hide a new violation --------
d=$(make_repo c5)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
printf 'print(sneaky_undefined)\n' >> "$d/app.py"
# try to bless it by editing the checked-in baseline
python3 - "$d" <<'PY'
import json, sys
p = sys.argv[1] + '/ci/lint-baseline.json'
b = json.load(open(p))
b['findings'].setdefault('app.py', {})['F821 Undefined name `sneaky_undefined`'] = 1
json.dump(b, open(p, 'w'), indent=1, sort_keys=True)
PY
git -C "$d" commit -qam violation-plus-blessing
expect "5 self-blessed baseline still fails" 1 "$(run_gate "$d" "BASE=$base")"

# --- control 6a: harmless line shift tolerated -----------------------------
d=$(make_repo c6a)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
printf 'A = 1\nB = 2\n\n\n' | cat - "$d/legacy.py" > "$d/legacy.py.new" && mv "$d/legacy.py.new" "$d/legacy.py"
git -C "$d" commit -qam line-shift
expect "6a harmless line shift" 0 "$(run_gate "$d" "BASE=$base")"

# --- control 6b: rename of file with findings fails (documented) ----------
d=$(make_repo c6b)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
git -C "$d" mv legacy.py legacy_renamed.py
git -C "$d" commit -qm rename-with-findings
expect "6b rename with baseline findings" 1 "$(run_gate "$d" "BASE=$base")"

# --- control 7a: BASE unset -------------------------------------------------
d=$(make_repo c7a)
printf 'print(x_undefined)\n' >> "$d/app.py"; git -C "$d" commit -qam x
expect "7a BASE unset fails closed" 2 "$(run_gate "$d")"

# --- control 7b: ruff missing (stripped PATH) ------------------------------
d=$(make_repo c7b)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
printf 'print(x_undefined)\n' >> "$d/app.py"; git -C "$d" commit -qam x
expect "7b unavailable ruff fails closed" 2 "$(run_gate "$d" "BASE=$base" "RUFF_BIN=/nonexistent-ruff-xyz")"

# --- control 7c: base has no baseline file ---------------------------------
d=$(make_repo c7c)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
git -C "$d" rm -q ci/lint-baseline.json && git -C "$d" commit -qm drop-baseline
# gate run with BASE=base still sees baseline at base; instead point BASE at a
# commit WITHOUT the baseline: use HEAD as base and change something else.
printf 'print(z_undefined)\n' >> "$d/app.py"; git -C "$d" commit -qam after
head_no_base=$(git -C "$d" rev-parse HEAD~1)
expect "7c base without baseline fails closed" 2 "$(run_gate "$d" "BASE=$head_no_base")"

# --- control 7d: broken ruff.toml ------------------------------------------
d=$(make_repo c7d)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
printf 'this is not valid toml [[[\n' > "$d/ruff.toml"
git -C "$d" commit -qam broken-config
expect "7d broken config fails closed" 2 "$(run_gate "$d" "BASE=$base")"

# --- control 8: baseline bootstrap ----------------------------------------
mk_boot() { # repo whose base commit has NO baseline file
  local d="$WORKROOT/$1"
  mkdir -p "$d"
  git -C "$d" init -q
  git -C "$d" config user.email selftest@example.invalid
  git -C "$d" config user.name selftest
  cp "$RUFF_CFG" "$d/ruff.toml"
  printf 'x = 1\n' > "$d/app.py"
  git -C "$d" add -A && git -C "$d" commit -qm base-no-baseline
  echo "$d"
}
add_baseline_only() { # HEAD adds baseline + docs, no .py changes
  local d="$1"
  python3 -c "import json,os,sys; os.makedirs(sys.argv[1]+'/ci',exist_ok=True); json.dump({'version':1,'generated_from_base':'boot','tool':'ruff','rules':['E9','F'],'findings':{}}, open(sys.argv[1]+'/ci/lint-baseline.json','w'))" "$d"
  printf '# policy\n' > "$d/docs-lint-policy.md"
  git -C "$d" add -A && git -C "$d" commit -qm introduce-baseline
}
d=$(mk_boot c8a)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
add_baseline_only "$d"
expect "8a clean baseline bootstrap (no .py changes)" 0 "$(run_gate "$d" "BASE=$base")"

d=$(mk_boot c8b)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
add_baseline_only "$d"
printf 'print(boot_undefined)\n' >> "$d/app.py"; git -C "$d" commit -qam py-change-too
expect "8b bootstrap with .py changes fails closed" 2 "$(run_gate "$d" "BASE=$base")"

# --- control 9: baseline-only growth with clean py change -------------------
d=$(make_repo c9)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
printf '# clean comment change\n' >> "$d/app.py"
python3 -c "import json,sys; p=sys.argv[1]+'/ci/lint-baseline.json'; b=json.load(open(p)); b['findings']['ghost.py']={'F821 Undefined name \`ghost\`': 3}; json.dump(b, open(p,'w'), indent=1, sort_keys=True)" "$d"
git -C "$d" add -A && git -C "$d" commit -qm clean-change-plus-baseline-growth
expect "9 baseline growth caught despite clean code" 1 "$(run_gate "$d" "BASE=$base")"

# --- control 10: baseline deleted in HEAD -----------------------------------
d=$(make_repo c10)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
printf '# clean\n' >> "$d/app.py"; git -C "$d" add -A
git -C "$d" rm -q ci/lint-baseline.json && git -C "$d" commit -qm clean-change-drop-baseline
expect "10 baseline deleted in HEAD fails closed" 2 "$(run_gate "$d" "BASE=$base")"

# --- control 11: BASE unresolvable ------------------------------------------
d=$(make_repo c11)
printf 'print(u)\n' >> "$d/app.py"; git -C "$d" commit -qam x
expect "11 unresolvable BASE fails closed" 2 "$(run_gate "$d" "BASE=deadbeefdeadbeefdeadbeefdeadbeefdeadbe")"

# --- control 12: weakened ruff.toml (canary) --------------------------------
d=$(make_repo c12)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
printf '[lint]\nselect = ["E9"]\n' > "$d/ruff.toml"
git -C "$d" commit -qam weaken-rules
expect "12 weakened ruff.toml fails canary" 2 "$(run_gate "$d" "BASE=$base")"

# --- control 13: F811 line shift tolerated (normalized keys) ----------------
d=$(make_repo c13)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
printf '# shifted down\n' | cat - "$d/f811.py" > "$d/f811.py.new" && mv "$d/f811.py.new" "$d/f811.py"
git -C "$d" commit -qam shift-above-f811
expect "13 F811 line shift tolerated" 0 "$(run_gate "$d" "BASE=$base")"

# --- control 14: dirty working tree ------------------------------------------
d=$(make_repo c14)
base=$(git -C "$d" rev-list --max-parents=0 HEAD | tail -1)
printf 'print(dirty_uncommitted)\n' >> "$d/app.py"   # NOT committed
expect "14 dirty tree fails closed" 2 "$(run_gate "$d" "BASE=$base")"

echo
echo "selftest: $PASS passed, $FAIL failed (workdir: $WORKROOT)"
[ "$FAIL" -eq 0 ]
