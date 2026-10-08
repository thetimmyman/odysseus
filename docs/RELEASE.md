# Native release and recovery

Changes land in `dev`. After the intended release has passed required checks,
promote that exact validated commit to `main` using the curated release process.
Production deploys a full main commit, never a moving tag or a checkout guess.
The default GitHub branch remains `dev` for contributions; production authority
is `main`.

On the deployment host, use a clean canonical checkout and record its current
commit, running image ID and active Compose configuration before the change.
Fetch the validated release and establish a tracking `main` checkout if it does
not yet exist. Preserve any older divergent local branch under a separate name;
do not reset or discard it. No production image changes just because Git moves.

```bash
./deploy-odysseus.sh FULL_40_CHARACTER_MAIN_SHA
./deploy-odysseus.sh verify FULL_40_CHARACTER_MAIN_SHA
```

The deployment command requires that SHA to equal the fetched main tip. It
reads the running service's Compose labels, resolves the active settings into a
private rollback packet, pins the previous image by ID, and builds a Git archive
of the candidate. Untracked files, ignored credentials and local edits cannot
enter that archive. The Dockerfile bakes source SHA, branch and build time into
a root-owned read-only file at `/usr/local/share/odysseus/build-identity.json`
and OCI labels. Its parent stays root-owned outside the writable `/app` tree;
the post-startup canary checks that the application user cannot write either.
Runtime Git or environment values
cannot replace that identity. The GitHub image publisher supplies the same
build arguments; an ordinary development build without them reports unknown.

Before activation, an isolated container starts with no network, credentials or
live data mounts. Startup, readiness, version/source/image agreement and denial
of unauthenticated session access must pass. A code/identity bind mount is
refused. Activation preserves native settings, mounts and supporting services.
A failed readiness or identity check restores the recorded previous image and
returns failure. The exact restore command is printed on success:

```bash
./deploy-odysseus.sh rollback /absolute/path/to/release-packet
./deploy-odysseus.sh reapply /absolute/path/to/release-packet
```

Reapply uses the same canary-qualified image without rebuilding. The checkout
must still be the packet's main SHA. Configuration hashes are checked before
restore/reapply; modified packets fail. An unrelated running image prevents
reapply.

Packets default to the sibling `releases/` directory, outside the checkout.
They contain private resolved Compose settings: keep their directory mode 700
and files private. Do not print, attach or commit their contents. The public
`receipt.json` contains image/source identities and configuration hashes only.
Historical images may have unknown source identity; retain that distinction.
Rollback verifies the exact prior image and readiness, without pretending its
checkout SHA identifies the old image.

`ODYSSEUS_REPO_DIR`, `ODYSSEUS_DEPLOY_STATE_DIR`, `ODYSSEUS_CONTAINER` and
`ODYSSEUS_BASE_URL` can select the native installation. Production branch is
`main`. The script does not weaken authentication or modify backup schedules.

A fresh-data canary does not qualify database migrations or backup integrity.
Before first activation or a data/schema change, take a native consistent backup
and test the intended data transition/recovery on private copies. For PS-621,
the existing database and model source hashes match the running image; retain
the measured result and current backup evidence in Plane. Image rollback does
not undo data migrations. The application/database, external adapters, inference
profiles and backup restore qualification retain their separate evidence.
