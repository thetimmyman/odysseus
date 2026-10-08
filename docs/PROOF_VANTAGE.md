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

Follow [the native release procedure](RELEASE.md). On the deployment host,
verify the full intended production `main` commit against both the application's
baked identity and the actual running image:

```sh
./deploy-odysseus.sh verify FULL_40_CHARACTER_MAIN_SHA
```

The command checks readiness, the bound endpoint, application build identity
and the running image's OCI revision. A checkout's Git HEAD, inside or outside
the container, does not establish image provenance. Missing or disagreeing
immutable identity remains unknown or conflicting; shipping a checkout does
not repair that proof. Build and release through the native procedure.

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

Use [PS-632's native capability receipts](ps632-capability-receipts.md) and the
attempt's sealed dispatch/execution evidence. Record the exact selected profile
ID, bound capability receipt hash, runtime/model material identity, measured
context/capabilities and qualification/health clocks. Resolve the bound receipt
through the native store and check current active authority and material identity
when assessing a future dispatch. A requested profile or a model-list response
does not prove which profile executed an attempt.

The TMOS maintenance view exposes selected registry metadata without running a
model or renewing evidence. Its current eligibility result is separate from
historical execution proof. Examples and default TTLs in the contract are not
measurements of an installed profile. Read each actual receipt's measured limits
and clocks; an advertised context is not its measured safe working context.

Do not print or attach process/container environments, environment files or
`/proc/<pid>/environ`. They can contain credentials and are not qualification
evidence. Export only the selected non-secret identity/evidence fields above.

## 6. Host that ran deterministic verification

For a live verification command, record the host on which it actually executes:

```sh
hostnamectl
# and, for the verification itself:
uname -a
```

For GitHub Actions, record the exact source SHA/run and the actual job's runner
name, group and labels from its job record. A workflow's `runs-on` setting or a
host's registered verifier role alone does not prove it ran a particular check.
Keep the deployment host, inference target and CI runner as separate facts.
Remote verification must identify both its target endpoint and execution host.

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
