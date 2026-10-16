# One-shot changed-host measurement

`scripts/odysseus-measure-once` owns one normal P1 RTX lease and calls the
existing changed-host qualifier. It does not register, import, activate,
dispatch, renew an old profile, or change routing policy. Its only successful
output is a private new approximate 4K candidate. The earlier receipt remains
historical evidence.

The operator supplies the sealed previous receipt, registered specification,
trusted identity collector argv JSON, a new private output directory, a unique
holder, and the reviewed normal `halogen-lease` source path and SHA-256. The
shared lease client is unchanged. Its existing `_post` primitive runs in a
bounded worker; its unconditional context-manager release is not used.

Before any broker, identity or provider operation, the command proves local
output capture and termination of a detached descendant in two synthetic
systemd user units. `--qualify-boundary` runs only those proofs. There is no
proof-file, environment or command-line override for qualification.

Every worker uses `ExitType=cgroup`, `KillMode=control-group`, a runtime limit,
1 GiB memory, 32 tasks, one CPU, no swap and bounded output. The existing
`collect_process` drains both streams concurrently, checks the lease and
terminates the owned local unit on deadline, output overflow or uncertainty.
Provider workers have 140 seconds and 1 MiB stdout; identity collectors have
180 seconds and 512 KiB stdout. Both have 64 KiB stderr. Broker workers have
8 seconds and 16 KiB stdout. Acquisition waits at most 30 seconds using the
same normal FIFO arrival value; unknown acquisition responses are not replayed.
The run's worker admission deadline is 900 seconds. An outer owned controller
unit with a 900-second runtime limit is still required operationally.

The returned normal lease must identify the requested RTX pool, endpoint,
600-second TTL and existing 12-hex lease-ID contract. A fresh heartbeat ACK is
required before each of the two requests and each identity capture. A keeper
also heartbeats every 30 seconds. Any failure or conservative TTL deadline
makes validity false permanently. There is no lease resurrection.

The existing qualifier validates native completion, model, token and exact
probe tool arguments, then compares fresh post-identity and serving binding.
Only its successful return permits a fresh heartbeat and explicit release.
Only an acknowledged release permits atomic publication of `receipt.json`.
`ownership.json` retains the exact lease ID, holder, acquisition, heartbeat
and release acknowledgements separately from native provider evidence.
Exact owned unit IDs and operation kinds are flushed to the controller's
private stderr before launch, and retained with local stop results in final
ownership/failure evidence. No payload, endpoint or credential is logged.
If keeper cleanup itself fails, the original failure and UNKNOWN cleanup
remain recorded. Unit identity is a reconciliation key, not remote stop proof.
Failed runs preserve existing trial/identity/report evidence and `failure.json`;
an existing output directory is never reused. A qualification report can say
PASS while final release fails: the missing accepted receipt and UNRESOLVED
failure record remain authoritative for this caller.

Local process exit, socket closure, timeout and lease expiry do not prove
remote SSH descendant cleanup or provider cancellation. Failure never calls
release in `finally`, starts the second trial after uncertainty, accepts a
candidate, or claims free/exclusive provider capacity. The broker's existing
TTL may still expire; this command has no quarantine/fencing protocol. The
owner must reconcile an uncertain request before any retry, adoption or
further workload. No provider restart, model unload or pool policy change is
part of recovery. Two acknowledged native completions plus stable identity
prove only this measurement's completed requests, not global quiescence.

Source tests and the no-provider boundary proof are prerequisites. Actual
bounded measurement, review, explicit store compare-and-swap adoption, real
owner-authenticated admission, and operational pilot acceptance remain separate.
