# Proof vantage — command reference for identity verification

A "vantage" is the exact place a claim is checked from. To claim "the code I
edited is the code serving this UI/API", every link in the chain must be
verified from a vantage that can actually see it. Run the commands below and
record their outputs together; a claim backed by only some of them is
unverified at the missing links.

## 1. Source commit being edited

Run in the working checkout, before deploying:

```sh
git -C /path/to/odysseus rev-parse HEAD
git -C /path/to/odysseus status --porcelain   # empty output = tree matches HEAD
```

`git rev-parse HEAD` names the commit you are editing. A dirty tree means the
deployed bytes may not match any commit at all — check this first.

## 2. Source commit actually deployed

Verify what the running image/container was built from, not what your local
branch says:

```sh
# Image-level: the build provenance label recorded at build time
docker inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' \
  "$(docker compose -f docker-compose.yml images -q odysseus | head -1)"

# Container-level: the commit inside the live container's checkout (if retained)
docker compose exec odysseus git rev-parse HEAD
```

If neither is available, the deployed commit is unknown: the image must be
rebuilt with the revision label (or the checkout shipped) before identity can
be proven. `docker compose build --build-arg GIT_SHA="$(git rev-parse HEAD)"`
with a `LABEL org.opencontainers.image.revision=$GIT_SHA` in the Dockerfile
establishes this link.

## 3. Process / container serving the UI/API

Confirm the thing answering the port is the thing you deployed:

```sh
# Which process owns the app port (7000 by default, see docker-compose.yml)
sudo ss -ltnp | grep ":${APP_PORT:-7000}"

# Docker deployment: container name, image digest, start time
docker compose ps odysseus
docker inspect --format '{{.Name}} {{.Image}} {{.State.StartedAt}}' \
  "$(docker compose ps -q odysseus)"

# systemd deployment (odysseus-ui.service): unit's image/binary and start time
systemctl show odysseus-ui -p ExecStart -p MainPID -p ActiveEnterTimestamp
```

The PID/port from `ss` must match `MainPID` (systemd) or the container's PID
(docker). A listener that matches neither is an unidentified process.

## 4. Endpoint used

State the exact URL each verification request goes to, and prove nothing is
interposed between you and the process from step 3:

```sh
curl -sS http://127.0.0.1:${APP_PORT:-7000}/api/ready
curl -sS http://127.0.0.1:${APP_PORT:-7000}/api/version
```

Use loopback on the host that runs the container/unit. If the request goes
through a reverse proxy, the proxy's upstream config is part of the identity
chain and must be inspected too.

## 5. Exact execution profile per attempt

For each attempt, record which profile actually ran — engine, model, and
runtime configuration — from the server side, not from what was requested:

```sh
# Container: the effective environment of the running process (not the .env file)
docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' \
  "$(docker compose ps -q odysseus)" | sort

# systemd: the effective environment of the running unit
systemctl show odysseus-ui -p Environment -p EnvironmentFiles
```

Environment variables read at import time may differ from the process's
current environment; for a strict check read `/proc/<pid>/environ` for the
exact PID from step 3.

## 6. Host that ran deterministic verification

Record the host identity of the machine running every command above:

```sh
hostnamectl
# and, for the verification itself:
uname -a
```

Verification run on a different host than the deployment proves nothing about
that deployment unless each command is explicitly executed against it.

## Caveat — what is NOT identity proof

A successful restart is not identity proof. A responding model-list or version endpoint alone is not identity proof.
`/api/version` now exposes baked `status`, `git_sha`, `branch` and `built_at`
alongside the version number. Builds without complete injected metadata report
`unknown`. Use `deploy-odysseus.sh verify FULL_MAIN_SHA` to compare the live
application with the actual running image revision and intended main SHA.
See [the native release procedure](RELEASE.md). A host ping (`ping`, `curl -I` of any
always-on route) is not identity proof: it shows something is listening, not
which code is running. Liveness (`/api/health`) and readiness (`/api/ready`)
likewise show state, not identity. Identity requires the chain above:
edited commit → deployed revision → serving process → exact endpoint →
execution profile → verifying host, each verified from a vantage that sees
the real artifact.
