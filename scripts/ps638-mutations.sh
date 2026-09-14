#!/usr/bin/env bash
# Mutation-test the PS-638 evidence invariants.
#
# An invariant only matters if a test fails when the invariant is removed. This
# script deletes one rule at a time from the shipped modules, runs the PS-638
# suite, and reports which tests went red. Every mutation below MUST produce at
# least one failure, and the script says so explicitly at the end.
#
# Usage:  bash scripts/ps638-mutations.sh [worktree]
# Restores every mutated file, including on interrupt.
#
# PROCESS TRAP this script now guards against, found live 2026-09-14: a run killed
# mid-mutation leaves the mutated source ON DISK. The next run then starts from a
# contaminated baseline — an unrelated test is already red, and the next mutation
# to touch the same text reports "COULD NOT APPLY" because it is already applied.
# A mutation result from a contaminated baseline is worthless, so the script now
# runs the suite once BEFORE mutating and refuses to continue unless it is green.
set -uo pipefail

WT=${1:-$(cd "$(dirname "$0")/.." && pwd)}
TESTS="tests/test_evidence_package_ps638.py tests/test_evidence_contract_ps638.py"
BACKUP=$(mktemp -d)
FAILED=0
APPLIED=0

run_tests() {
  (cd "$WT" && timeout 600 python3 -m pytest $TESTS -q -p no:cacheprovider --tb=no -rf 2>&1)
}

restore_all() {
  for f in src/evidence_package.py src/evidence_contract.py; do
    [[ -f "$BACKUP/$(basename "$f")" ]] && cp "$BACKUP/$(basename "$f")" "$WT/$f"
  done
}
trap restore_all EXIT INT TERM

for f in src/evidence_package.py src/evidence_contract.py; do
  cp "$WT/$f" "$BACKUP/$(basename "$f")"
done

# ---- baseline gate: a contaminated baseline is not a measurement ---------------
baseline=$(run_tests)
if printf '%s\n' "$baseline" | grep -qE '^FAILED |failed,'; then
  echo "BASELINE IS NOT GREEN — refusing to mutate:"
  printf '%s\n' "$baseline" | grep -E '^FAILED |failed,' | head -10
  echo "(a previous run was probably killed mid-mutation; restore src/ and retry)"
  exit 2
fi
echo "baseline green: $(printf '%s\n' "$baseline" | grep -E 'passed' | tail -1)"
echo

mutate() { # name file python_expr
  local name="$1" file="$2" expr="$3"
  restore_all
  if ! python3 -c "
import pathlib, sys
p = pathlib.Path('$WT/$file')
s = p.read_text()
new = ($expr)
if new == s:
    print('APPLY-FAILED'); sys.exit(3)
p.write_text(new)
" ; then
    echo "### $name: COULD NOT APPLY (source moved?) ###"; FAILED=$((FAILED+1)); return
  fi
  APPLIED=$((APPLIED+1))
  local out; out=$(run_tests)
  local summary; summary=$(printf '%s\n' "$out" | grep -E '^[0-9]+ (passed|failed)|failed,' | tail -1)
  local red; red=$(printf '%s\n' "$out" | grep -E '^FAILED ' | sed 's/^FAILED //; s/ - .*//' | head -6)
  echo "### $name ###"
  echo "    ${summary:-no summary}"
  if [[ -n "$red" ]]; then
    printf '    red: %s\n' $red
  else
    echo "    red: NONE  <-- MUTATION SURVIVED"
    FAILED=$((FAILED+1))
  fi
}

# --- the interface must actually be checked -----------------------------------
mutate "M1 interface digest not re-checked" src/evidence_package.py \
  "s.replace('        if not recorded or recorded != recomputed:', '        if False:')"

mutate "M2 context projection not checked for the interface" src/evidence_package.py \
  "s.replace('        missing = [line for line in interface if line not in text]\n        if missing:', '        missing = []\n        if False:')"

mutate "M3 rendered context not required" src/evidence_package.py \
  "s.replace('        if not isinstance(ref, Mapping):\n            issues.append(ValidationIssue(\n                RENDERED_CONTEXT_MISSING,', '        if False:\n            issues.append(ValidationIssue(\n                RENDERED_CONTEXT_MISSING,')"

# --- retry history and provenance --------------------------------------------
mutate "M4 retry history may skip attempts" src/evidence_package.py \
  "s.replace('        if numbers != list(range(1, len(numbers) + 1)):', '        if False:')"

mutate "M5 recorded outcome trusted over the exit code" src/evidence_package.py \
  "s.replace('        if recorded and recorded != derived:', '        if False:')"

mutate "M6 verifier identity digest not compared" src/evidence_package.py \
  "s.replace('            if digest not in planned_digests:', '            if False:')"

mutate "M7 dispatch target not compared" src/evidence_package.py \
  "s.replace('        if authorized_targets and actual_target not in authorized_targets:', '        if False:')"

mutate "M8 writes outside the authorized scope allowed" src/evidence_package.py \
  "s.replace('        outside = sorted(p for p in actual_writes if p not in effective_scope)\n        if outside:', '        outside = []\n        if False:')"

# --- source binding and baseline ---------------------------------------------
mutate "M9 source drift does not invalidate evidence" src/evidence_package.py \
  "s.replace('        if not current or current not in accepted:', '        if False:')"

mutate "M10 pre-existing claim needs no baseline" src/evidence_package.py \
  "s.replace('        if receipt.get(\"claimed_preexisting\"):', '        if False:')"

# --- requirement closure ------------------------------------------------------
mutate "M12 worker-authored proof closes an independent requirement" src/evidence_contract.py \
  "s.replace('        if not self.requires_independence:\n            return proof_class in KNOWN_INDEPENDENCE\n        return proof_class in INDEPENDENT_CLASSES', '        return proof_class in KNOWN_INDEPENDENCE')"

mutate "M13 unresolved mandatory requirement does not block" src/evidence_package.py \
  "s.replace('        if state == STATE_UNRESOLVED:', '        if False:')"

mutate "M14 sealed requirement states not compared" src/evidence_package.py \
  "s.replace('        elif recorded[requirement_id] != state:', '        elif False:')"

mutate "M15 sealed package hash not verified" src/evidence_package.py \
  "s.replace('    if not evidence_package_hash_is_valid(payload):', '    if False:')"

mutate "M16 secret-shaped fixtures not scanned" src/evidence_package.py \
  "s.replace('    hits = find_secret_shaped(payload)\n    if hits:', '    hits = ()\n    if False:')"

mutate "M17 artifacts never rejected as unavailable" src/evidence_package.py \
  "s.replace('        if data is None:\n            issues.append(ValidationIssue(\n                ARTIFACT_UNAVAILABLE,', '        if False:\n            issues.append(ValidationIssue(\n                ARTIFACT_UNAVAILABLE,')"

mutate "M18 artifact contents not scanned for secrets" src/evidence_package.py \
  "s.replace('        leaked = find_secret_shaped(text, prefix=label)\n        if leaked:', '        leaked = ()\n        if False:')"

mutate "M19 verifications may disagree about their tree" src/evidence_package.py \
  "s.replace('    if len(verified) > 1:', '    if False:')"

mutate "M20 an unexplained tree difference is ignored" src/evidence_package.py \
  "s.replace('    if not wrote:', '    if False:')"

restore_all
echo
echo "mutations applied: $APPLIED"
if [[ $FAILED -eq 0 ]]; then
  echo "ALL MUTATIONS KILLED — every rule is load-bearing for at least one test."
else
  echo "SURVIVING MUTATIONS: $FAILED — those rules are not covered."
fi
exit $FAILED
