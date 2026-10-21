# One-shot local profile measurement

`scripts/odysseus-measure-once` owns one normal P1 RTX lease and calls the
existing changed-host qualifier. It does not register, import, activate,
dispatch, renew an old profile, or change routing policy. Its only successful
output is a private new approximate candidate at the declared context floor
(4K by default). The earlier receipt remains historical evidence.

`--context-floor <tokens>` declares the floor the measurement must prove. It must
be a multiple of 1024 between 4096 and 131072, and the provider's own reported
prompt tokens must reach it: the floor is never inferred from configuration, and
a profile configured for a larger context does not become evidence of one. The
floor must also fit the servable context: the engine's per-request slot limit when
the profile records one, otherwise the configured context. A larger floor is
refused before any lease or provider request. The upper bound exists because the prompt is sized to the floor and must stay inside
the bounded worker HTTP payload. The probe is sized on characters rather than
tokens, deliberately overshooting, because no tokenizer is available at this
layer: a denser tokenizer costs prefill time, a sparser one is refused, and
neither can produce a false pass. The sealed report records `context_floor`, so a
receipt cannot be read as a floor it did not measure.

`--new-profile <separate-descriptor.json>` instead measures a genuinely new
request contract. It is mutually exclusive with `--receipt`, changed-route,
boundary-only and worker modes. Supply a separate proposed specification and
native collector config/argv; never edit the historical receipt, specification
or collector config to make them pass a different contract. The descriptor is
sealed schema2 `UNQUALIFIED`, with inference role only, an empty qualification
reference and supersedes, unknown health, and no measured capabilities,
safe/semantic/engine context, served-context observation or load measurements.
Its specification has an empty qualification reference. A descriptor is a
proposed exact identity, not evidence that the profile is usable.

This mode supports only the existing deterministic native Ollama contract:
numeric zero temperature (preserving integer/float representation), native
boolean `think=false`, integer512 output limit, and the exact integer configured
context. Boolean numbers, nonfinite values, string thinking labels and unknown
options are refused. Optional seed and top_p retain their typed values and are
part of the exact observed material; template and runtime-environment hashes
remain evidence metadata, not wire options. The native collector must observe
every material field of this new descriptor exactly before and after the
same two trials, with fresh timestamps and an unchanged serving-process binding.
It does not use the old changed-route exception to hide other drift.

Fresh-profile measurement uses the same local process proofs, projection,
normal owned lease, bounded native capture, postcheck and cleanup-before-release
flow described below. Only successful trials, stable post-identity and the
acknowledged release permit atomic publication of a new approximate candidate at
the declared floor, with its own report anchor and empty supersedes. The descriptor remains
unqualified and unchanged. No import, registration, activation, routing or
pilot admission follows. A configured or advertised larger context does not
become evidence of itself: a measurement at 4K cannot be read as 32K evidence, and
only a run whose `--context-floor` and reported prompt tokens both reach the
higher figure may claim it. Existing renewal, changed-host and changed-route
inputs and guards remain unchanged.

`--changed-route --current-spec <separate-file>` explicitly measures a newly
observed hostname instead. The old receipt/specification stays immutable. The
proposed specification may change only the credential-free hostname, with all
other URL components and all remaining specification fields canonically equal.
The normal lease binds the proposed endpoint. The existing native collector
must independently bind that actual route to the published serving container,
unchanged runtime/artifact, GPU and request options. Only the observed kernel,
boot identity and endpoint URL may differ from historical material. This mode
derives an UNQUALIFIED new profile and requires the same two complete trials
and stable fresh post-identity before a new receipt at the declared floor with
its own anchor.
It requires an explicit owned transport, with no unbounded default fallback.
The default changed-host mode and same-profile renewal still refuse endpoint
changes. No alias equivalence, broker configuration change or activation is
implied by proposing a current specification.

The operator supplies the sealed previous receipt, registered specification,
trusted identity collector argv JSON, a new private output directory, a unique
holder, and the reviewed normal `halogen-lease` source path and SHA-256. The
shared lease client is unchanged. Its existing `_post` primitive runs in a
bounded worker; its unconditional context-manager release is not used.

Before any broker, identity or provider operation, the command proves local
output capture and termination of a detached descendant in two synthetic
systemd user units. `--qualify-boundary` runs only those proofs. There is no
proof-file, environment or command-line override for qualification.

An unprivileged systemd user unit can map only the caller into its user
namespace while retaining `ProtectControlGroups=yes`. A host-root-owned SSH
configuration can consequently fail SSH's owner check inside that unit.
`--ssh-config-manifest <private-file> --ssh-config-manifest-sha256 <pin>`
supports an explicit read-only projection without changing host files,
authentication material, the collector argv or SSH's native target parser.
The flags belong to the controller, including boundary-only mode; they are
refused in HTTP/broker workers. The manifest must be caller-owned mode0600,
regular, not a symlink, at most64KiB, and match the supplied source pin.

The `PINNED_SSH_CONFIG_V1` manifest contains `home`, at most two `roots`
(`user` and/or `system`) and at most four `files`. The home must match the
caller account used by native SSH. Root paths and Include bases are fixed:
the account's `.ssh/config` and `/etc/ssh/ssh_config`, respectively. They
cannot be replaced with arbitrary manifest paths. Each file records `path`,
`resolved_path`, `link` (literal symlink target or null), `uid`, `mode` and
`sha256`. All actual configuration paths, owners, bodies and pins stay in
private operator evidence outside the repository. The controller checks
root-or-caller ownership, no group/world write, exact original mode, bytes,
link and resolved target. It conservatively verifies static Include membership
across all sections, using the root's Include base through nested includes.
Included logical and resolved sources must use `.conf` names.
Root symlinks may resolve only to a native config basename or `.conf` file.
This prevents a config alias from laundering a named key/store before reading it.
Private-key headers and non-directive content are refused before copies
are created. Named keys and stores cannot be admitted as roots or Include inputs.
Native Host patterns and option values are never classified as key/store data.
It does not interpret Host/Match selection or option precedence. Dynamic
Include tokens, named-home expansion, directory globs, ambiguous bases,
unlisted sources, extra sources and unsupported paths are refused. The
native parser remains authoritative. Only explicit fixed config roots and
their pinned static Includes are copied; recognized standalone key/store
paths and non-config formats are refused. This is not a general credential
or content classifier. Config bytes stay private and may contain authentication
references. No additional path discovery follows IdentityFile, known-host or
other authentication directives, and the target is never reconstructed here.

Exact configuration copies use a private0700 temporary directory outside
checkouts, with caller-owned0600 files. Identity captures and both local
proof units add unit-specific `BindReadOnlyPaths` at the same resolved
configuration targets; existing logical symlinks remain intact. The backing
copy directory is itself read-only inside the unit, closing an alternate
writable path. No `ProtectControlGroups`, descendant, resource, runtime or
privilege restriction is removed. Broker and provider units are unchanged.
The first local proof must directly verify projected owner/mode/bytes/link,
read-only mounts and EROFS on a non-writing write-open attempt. Copies on
the caller's writable filesystem cannot pass that proof. The second proof
still terminates a detached descendant with the same projection applied.

The controller rechecks original files, Include membership and private copies
before each projected unit and after qualification and every identity capture,
including failure. Changed or unavailable sources refuse instead of refreshing
pins. A secondary postcheck failure does not replace the primary capture
failure. The owner finalizes private-copy verification and cleanup before
release and atomic receipt publication. A failed cleanup prevents either,
and its fixed status is retained separately from the primary failure.
Failure-path cleanup cannot emit arbitrary exception text, mask the primary
refusal or silently retry a failed removal. No new copy cleanup runs after a
receipt is published. Temporary-copy removal does not prove unit or remote termination.
If construction fails after creating copies and removal is unconfirmed, a
sanitized preparation refusal retains the primary error privately. The
controller's existing private stderr channel receives a fixed failure class,
cleanup UNKNOWN and the exact owned temporary directory for reconciliation.
Original config paths, bodies and arbitrary exception messages are excluded;
only enums may be forwarded to public diagnostics or tracker summaries.
Source tests, a successful projection proof or a local metadata parser run
cannot qualify a provider or renew historical freshness: fresh native identity,
the two bounded requests and stable post-identity remain required.

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
