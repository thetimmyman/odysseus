#!/usr/bin/env bash
# Native exact-source deployment. Preserves the actual active Compose settings.
# deploy-odysseus.sh FULL_MAIN_SHA | verify FULL_MAIN_SHA | rollback/reapply RELEASE_DIR
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${ODYSSEUS_REPO_DIR:-$SCRIPT_DIR}"
STATE_DIR="${ODYSSEUS_DEPLOY_STATE_DIR:-$REPO_DIR/../releases}"
CONTAINER="${ODYSSEUS_CONTAINER:-odysseus-odysseus-1}"
BASE_URL="${ODYSSEUS_BASE_URL:-http://127.0.0.1:7000}"
SERVICE=odysseus
cd "$REPO_DIR"

die() { echo "deploy: $*" >&2; exit 1; }
full_sha() { [[ "$1" =~ ^[0-9a-f]{40}$ ]] || die "a full 40-character main SHA is required"; }
image_id() { docker inspect "$CONTAINER" --format '{{.Image}}'; }
verify_source() {
    local expected="$1" image revision
    full_sha "$expected"
    image="$(image_id)"
    revision="$(docker image inspect "$image" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
    curl --fail --silent --show-error --max-time 10 "$BASE_URL/api/version" |
        (cd "$SCRIPT_DIR"; python3 -m src.build_identity verify --expect-sha "$expected" --image-revision "$revision" --branch main)
}
wait_ready() {
    for ((attempt=0; attempt<45; attempt++)); do
        if curl --fail --silent --show-error --max-time 2 "$BASE_URL/api/ready" 2>/dev/null |
            python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("ready") is True else 1)' 2>/dev/null; then
            return 0
        fi
        sleep 2
    done
    return 1
}
compose_saved() {
    docker compose --project-name odysseus --project-directory "$REPO_DIR" -f "$1" up -d --no-deps "$SERVICE"
}
rollback() {
    local release="$1" previous
    [[ -f "$release/rollback.compose.json" && -f "$release/receipt.json" ]] || die "incomplete rollback packet"
    previous="$(python3 - "$release" <<'PYCODE'
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]);r=json.loads((root/'receipt.json').read_text())
assert hashlib.sha256((root/'rollback.compose.json').read_bytes()).hexdigest()==r['rollback_config_sha256'], 'rollback packet changed'
print(r['previous_image'])
PYCODE
)"
    compose_saved "$release/rollback.compose.json"
    [[ "$(image_id)" == "$previous" ]] || die "rollback image mismatch"
    wait_ready || die "rollback readiness failed"
    echo "Restored previous image $previous; historical source identity is recorded in $release/receipt.json"
}

activate_saved() {
    local release="$1" sha image previous current revision
    read -r sha image previous < <(python3 - "$release" <<'PYCODE'
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]);r=json.loads((root/'receipt.json').read_text())
assert r['canary_verified'] is True
assert hashlib.sha256((root/'activate.compose.json').read_bytes()).hexdigest()==r['activate_config_sha256'], 'activation packet changed'
print(r['target_sha'],r['image'],r['previous_image'])
PYCODE
)
    full_sha "$sha"
    [[ "$(git branch --show-current)" == main && "$(git rev-parse HEAD)" == "$sha" ]] || die "checkout must be the recorded main release"
    current="$(image_id)"
    [[ "$current" == "$previous" || "$current" == "$image" ]] || die "running image changed outside this release packet"
    revision="$(docker image inspect "$image" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
    [[ "$revision" == "$sha" ]] || die "retained candidate revision mismatch"
    if ! { compose_saved "$release/activate.compose.json" && wait_ready && verify_source "$sha" && [[ "$(image_id)" == "$image" ]]; }; then
        echo "Activation failed; restoring recorded previous image." >&2
        rollback "$release"
        return 1
    fi
    echo "Verified main source $sha; image $image"
}

case "${1:-}" in
    verify) [[ $# == 2 ]] || die "usage: $0 verify FULL_MAIN_SHA"; verify_source "$2"; exit ;;
    rollback) [[ $# == 2 ]] || die "usage: $0 rollback RELEASE_DIR"; rollback "$2"; exit ;;
    reapply) [[ $# == 2 ]] || die "usage: $0 reapply RELEASE_DIR"; activate_saved "$2"; exit ;;
esac
[[ $# == 1 ]] || die "usage: $0 FULL_MAIN_SHA | verify FULL_MAIN_SHA | rollback/reapply RELEASE_DIR"
SHA="$1"; full_sha "$SHA"
[[ "${ODYSSEUS_BRANCH:-main}" == main ]] || die "production authority must be main"
[[ -z "$(git status --porcelain)" ]] || die "checkout has unrelated changes; use a clean deployment checkout"
git fetch origin main
[[ "$(git rev-parse FETCH_HEAD)" == "$SHA" ]] || die "candidate must equal the fetched main tip"

# Snapshot only the running service's native configuration; never print it.
FILES="$(docker inspect "$CONTAINER" --format '{{ index .Config.Labels "com.docker.compose.project.config_files" }}')"
PROJECT="$(docker inspect "$CONTAINER" --format '{{ index .Config.Labels "com.docker.compose.project" }}')"
[[ "$PROJECT" == odysseus && -n "$FILES" && "$FILES" != '<no value>' ]] || die "running Compose authority is unavailable"
mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
RELEASE="$(mktemp -d "$STATE_DIR/$SHA.XXXXXX")"
PREVIOUS="$(image_id)"
PREVIOUS_REVISION="$(docker image inspect "$PREVIOUS" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
CHECKOUT_BEFORE="$(git rev-parse HEAD)"
CHECKOUT_BRANCH="$(git branch --show-current)"
curl --fail --silent --show-error --max-time 10 "$BASE_URL/api/version" > "$RELEASE/previous-version.json"
COMPOSE=(docker compose --project-name "$PROJECT" --project-directory "$REPO_DIR")
IFS=',' read -r -a CONFIG_FILES <<< "$FILES"
for file in "${CONFIG_FILES[@]}"; do COMPOSE+=(-f "$file"); done
"${COMPOSE[@]}" config --format json > "$RELEASE/rollback.compose.json"
# The previous packet must restore the exact image, even if its old tag moved.
(cd "$SCRIPT_DIR"; python3 - "$RELEASE" "$PREVIOUS" "$SHA" "$PREVIOUS_REVISION" "$CHECKOUT_BEFORE" "$CHECKOUT_BRANCH" <<'PY'
import hashlib,json,sys
from src.build_identity import verify
from pathlib import Path
root=Path(sys.argv[1]);p=root/'rollback.compose.json';value=json.loads(p.read_text())
service=value['services']['odysseus']
code_targets=('/app/app.py','/app/src','/app/core','/app/routes','/app/services','/app/static','/app/mcp_servers','/app/.build-identity.json')
for volume in service.get('volumes',[]):
 target=volume.get('target','')
 if target=='/app' or any(target==p or target.startswith(p+'/') for p in code_targets):
  raise SystemExit('refusing runtime mount over baked application code/identity')
service['image']=sys.argv[2];service.pop('build',None)
p.write_text(json.dumps(value,indent=2)+'\n')
api=json.loads((root/'previous-version.json').read_text())
previous_source={'status':'unknown','git_sha':None,'branch':None,'built_at':None}
try:
 old=verify(api,api.get('git_sha',''),sys.argv[4],branch=api.get('branch',''))
 previous_source={**old,'status':'pinned'}
except (ValueError,TypeError): pass
(root/'receipt.json').write_text(json.dumps({'previous_image':sys.argv[2],'target_sha':sys.argv[3],
 'previous_source':previous_source,'checkout_before':{'git_sha':sys.argv[5],'branch':sys.argv[6]},
 'rollback_config_sha256':hashlib.sha256(p.read_bytes()).hexdigest()},indent=2)+'\n')
PY
)
BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
# An archive prevents ignored/untracked secrets or a modified working tree entering the image.
git archive --format=tar "$SHA" | docker build \
    --build-arg "ODYSSEUS_BUILD_GIT_SHA=$SHA" --build-arg ODYSSEUS_BUILD_BRANCH=main \
    --build-arg "ODYSSEUS_BUILD_BUILT_AT=$BUILT_AT" -t "odysseus:source-$SHA" -
IMAGE="$(docker image inspect "odysseus:source-$SHA" --format '{{.Id}}')"
[[ "$IMAGE" =~ ^sha256:[0-9a-f]{64}$ ]] || die "built image has no immutable identity"
REVISION="$(docker image inspect "$IMAGE" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')"
[[ "$REVISION" == "$SHA" ]] || die "candidate image revision differs from source"

# Import/startup/version/readiness canary: no network, credentials or live data mounts.
CANARY="odysseus-canary-${SHA:0:12}-$$"
cleanup() { docker rm -f "$CANARY" >/dev/null 2>&1; }
trap cleanup EXIT
docker run -d --name "$CANARY" --network none \
    --tmpfs /app/data:uid=1000,gid=1000 --tmpfs /app/logs:uid=1000,gid=1000 \
    -e ODYSSEUS_INPROCESS_POLLERS=0 -e ODYSSEUS_INPROCESS_TASKS=0 -e AUTH_ENABLED=true "$IMAGE" >/dev/null
CANARY_OK=0
for ((attempt=0; attempt<45; attempt++)); do
    if docker exec "$CANARY" python -c 'import json,urllib.request; r=json.load(urllib.request.urlopen("http://127.0.0.1:7000/api/ready",timeout=2)); assert r["ready"] is True' >/dev/null 2>&1; then CANARY_OK=1; break; fi
    sleep 2
done
[[ "$CANARY_OK" == 1 ]] || die "isolated canary failed; production was not changed"
docker exec "$CANARY" python -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:7000/api/version",timeout=5).read().decode())' |
    (cd "$SCRIPT_DIR"; python3 -m src.build_identity verify --expect-sha "$SHA" --image-revision "$REVISION" --branch main)
docker exec "$CANARY" python -c 'import urllib.request,urllib.error
try:
 r=urllib.request.urlopen("http://127.0.0.1:7000/api/sessions",timeout=5); raise SystemExit("unauthenticated session access unexpectedly succeeded")
except urllib.error.HTTPError as e:
 assert e.code in (401,403), e.code'
cleanup
trap - EXIT
python3 - "$RELEASE" "$IMAGE" "$BUILT_AT" <<'PY'
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1]);value=json.loads((root/'rollback.compose.json').read_text());value['services']['odysseus']['image']=sys.argv[2]
(root/'activate.compose.json').write_text(json.dumps(value,indent=2)+'\n')
p=root/'receipt.json';r=json.loads(p.read_text());r.update(image=sys.argv[2],built_at=sys.argv[3],branch='main',canary_verified=True,activate_config_sha256=hashlib.sha256((root/'activate.compose.json').read_bytes()).hexdigest());p.write_text(json.dumps(r,indent=2)+'\n')
PY
# Release policy: main is promoted from a validated dev commit before this command.
git switch main
git merge --ff-only "$SHA"
[[ "$(git rev-parse HEAD)" == "$SHA" ]] || die "main checkout differs from the candidate"
activate_saved "$RELEASE"
printf 'Rollback: %q rollback %q\n' "$REPO_DIR/deploy-odysseus.sh" "$RELEASE"
