#!/usr/bin/env bash
# deploy-odysseus.sh — repeatable deploy from the canonical repo checkout.
#
# Usage: ./deploy-odysseus.sh
#
# Host-specific settings come from the environment (or the compose .env file):
#   ODYSSEUS_REPO_DIR   repo checkout to deploy from   (default: this script's dir)
#   ODYSSEUS_BRANCH     branch to fast-forward deploy  (default: dev)
#   ODYSSEUS_LAN_URL    optional extra health-check URL, e.g. http://192.168.1.50:7000
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${ODYSSEUS_REPO_DIR:-$SCRIPT_DIR}"
SERVICE="odysseus"
BRANCH="${ODYSSEUS_BRANCH:-dev}"

cd "$REPO_DIR"

# Pick up ODYSSEUS_LAN_URL etc. from the compose .env if not already exported.
if [ -z "${ODYSSEUS_LAN_URL:-}" ] && [ -f .env ]; then
    ODYSSEUS_LAN_URL="$(sed -n 's/^ODYSSEUS_LAN_URL=//p' .env | tail -1)"
fi

echo "=== 1. Fetching origin ==="
git fetch origin --prune

echo "=== 2. Switching to $BRANCH ==="
git switch "$BRANCH"

echo "=== 3. Fast-forward pulling ==="
git pull --ff-only origin "$BRANCH"

echo "=== 4. Building $SERVICE ==="
docker compose build "$SERVICE"

echo "=== 5. Recreating $SERVICE ==="
docker compose up -d --no-deps "$SERVICE"

echo "=== 6. Waiting for health check ==="
for i in $(seq 1 15); do
    sleep 2
    STATUS=$(docker compose ps --format "{{.Status}}" "$SERVICE" 2>/dev/null || true)
    if echo "$STATUS" | grep -q "healthy"; then
        echo "Container is healthy!"
        break
    fi
    if [ "$i" -eq 15 ]; then
        echo "WARNING: Container did not become healthy within 30s"
        docker compose logs --tail=30 "$SERVICE"
        exit 1
    fi
done

echo "=== 7. Health check ==="
curl -fsS -o /dev/null -w "localhost: HTTP %{http_code}\n" http://127.0.0.1:7000/
if [ -n "${ODYSSEUS_LAN_URL:-}" ]; then
    curl -fsS -o /dev/null -w "LAN:      HTTP %{http_code}\n" "$ODYSSEUS_LAN_URL/"
fi

echo "=== Deploy complete ==="
echo "Commit: $(git rev-parse --short HEAD)"
