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
