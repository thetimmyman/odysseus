# Explicit Inspector schema setup

The existing harness exposes `GET /api/harness/inspector/setup` and
`POST /api/harness/inspector/setup/schema`. Both require the existing real admin
cookie and a current account snapshot. Bearer and internal-tool callers are
refused. The handler uses the application's configured `SessionLocal`; the caller
cannot supply an engine, database URL, path, SQL or owner.

The status response reports `schema_state` (`ABSENT`, `COMPATIBLE` or `MISMATCH`),
the source-owned `expected_schema_hash`, object counts, the observed connection
settings, and whether maintenance is currently available. A missing, unreadable
or malformed settings source is unavailable. Maintenance reads bypass the normal
settings cache so an earlier disabled snapshot cannot authorize a later change.
Status is an observation; the addition checks the conditions again.

After normal source review, backup/restore qualification and an explicit
maintenance admission, submit exactly:

```json
{"expected_schema_hash":"<the hash returned by status>"}
```

The operation requires disabled execution, no installed Inspector owner and no
unresolved admission. On the existing qualified file-backed SQLite connection it
starts `BEGIN IMMEDIATE`, classifies the fixed objects, creates only the three
feature tables and twelve triggers when absent, and checks compatibility on that
same connection before committing. Partial or conflicting objects are refused.
An already compatible schema returns `changed: false`; this does not attribute a
previous addition to the current request.

Mid-addition failures and account/configuration changes observed before commit
roll back the schema transaction. Account snapshots are entry and finalization
checks, not an atomic lock across authentication, configuration and SQLite.
Changes in those other stores are not rolled back. A disconnected caller can
have an unknown acknowledgement; inspect status before considering another
explicit action.

Normal startup continues to check existing schema without creating it. Setup
does not prepare directories, configure an owner, enable execution, qualify a
provider or admit work. `COMPATIBLE` and `execution_qualification: NOT_RUN` are
separate facts. Supported execution/verification, current authority and profile
evidence, actual owner-cookie publication and the real work pilot still need
their own acceptance.

An uncommitted addition rolls back on failure. After a successful addition,
disable the feature through normal configuration and preserve its schema and
history when rolling back a release. Do not drop populated immutable tables or
delete original receipts. Native restart, crash durability and restored-state
acceptance must be verified with the maintained deployment/recovery workflow;
the isolated interface tests are source evidence only.

## App-owned transport proof

`POST /api/harness/inspector/setup/transport` accepts only
`{"expected_manifest_digest":"<lowercase SHA-256>"}` through the existing
admin-cookie gate and a same-origin request (or the normal non-browser client).
Execution must be disabled, the app owner stopped, and the existing database
and ownership guards available. The fixed private owner manifest must match
the current account incarnation and the expected digest.

The operation starts only the two fixed normal-output and detached-descendant
proofs through the explicitly configured SSH transport. It confirms the
app-owned clients and native executor unit/cgroup separately, then closes the
operational ownership guard. The response contains coarse observations for
that call. It creates no work database rows, provider/broker call, routing or
AttemptReceipt, profile adoption or reusable capability qualification. Normal
work must prove its transport again in its own invocation.

An incomplete proof retains an unresolved ownership guard and refuses another
invocation. Restart or elapsed time does not prove cleanup. Reconcile the exact
owned resources through the maintained operational controls before another
attempt; never delete routing history or treat SSH exit as provider cancellation.

Executor paths, source/interpreter/boot/credential bindings and the private
manifest are explicit normal operator preparation. The request cannot supply
a host, path, command, URL, SQL or proof override. Source fixtures do not
qualify those native prerequisites.

The explicit `runtime.execution_transport` descriptor selects the maintained
SSH helper and pins its installed source closure, interpreter and native
executor identity. `runtime.identity_source_pins` must cover the configured
collector executable and its absolute script/config arguments; stale bytes
refuse before collector execution. Static SSH configuration uses the existing
pinned read-only projection. Native workers receive the fixed normal user bus
and data location, without inherited Python, proxy, Git or Docker overrides.

The helper accepts only fixed capture kinds and exact birth-bound controls.
It records intent before launch and records native unit/cgroup identity before
work. Input, both output streams and time are bounded independently. The two
operational proofs do not establish cancellation of a provider request.
Configuration copies remain retained for reconciliation when native reader
termination or copy removal is unknown.

Verification uses the existing sandbox interface with an already installed
image resolved to its immutable ID. It records and inspects the exact container
ID before start. The normal network, mount, user, capability and resource
restrictions remain checked. Output is capped while reading. Cleanup addresses
only the inspected container ID and confirms its absence; an unknown create
acknowledgement cannot trigger start, retry or destruction by name. Receipts
retain observed container/image identity. Image/tool qualification and measured
task-specific verifier reports remain separate operational acceptance.
