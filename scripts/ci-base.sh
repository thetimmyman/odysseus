#!/usr/bin/env bash
# A newly established release branch has no event.before; compare its first parent.
set -euo pipefail
before="${1:?comparison base required}"
head="${2:-HEAD}"
if [[ "$before" =~ ^0{40}$ ]]; then
    git rev-parse --verify "$head^1^{commit}"
else
    git rev-parse --verify "$before^{commit}"
fi
