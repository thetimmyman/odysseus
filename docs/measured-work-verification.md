# Measured work-verification reports

The existing execution owner decodes `tmos-work-verification-v1` after the
existing workspace verifier has checked the command, source/verifier digests,
bounded streams, exact container identity and confirmed cleanup. This report
does not grant execution or qualify a tool, image, profile or routing target.

A legacy report contains `schema`, `tests_passed` and `tests_failed`, with
optional `positive_control` and `negative_control`. Unreported collected,
executed and skipped counters remain `None` in the canonical receipt. No tool
version is inferred from a dependency lock or desired configuration.

A measured report additionally supplies **all** of `tests_collected`,
`tests_executed`, `tests_skipped`, `tool_versions` and `provenance`, plus both
controls. The five counters must be plain nonnegative integers. Collected and
executed counts must be positive; collected equals executed plus skipped, and
executed equals passed plus failed. Skips are retained honestly; this decoder
does not decide whether they satisfy a particular readiness criterion.

`tool_versions` is a nonempty list of at most sixteen unique `[name, version]`
pairs. Names are lowercase identifiers; versions are nonempty printable ASCII
up to 128 characters. `container_image` is reserved for the independently
observed immutable image ID and cannot be supplied by the producer.

`provenance` contains exactly `command` and `verifier_digests`. The command
must equal the sealed normalized command. The complete unique `[path, SHA-256]`
set must equal the digests measured by the existing workspace verifier and
checked against the plan. Agreement proves binding to the frozen verifier;
it does not establish that an unqualified producer measured its claims.

Each control contains exactly `expected` and `observed`. Expected values are
`PASS` for the positive control and `FAIL` for the negative control. Observed
may be `PASS`, `FAIL` or `UNKNOWN`; unknown stays unknown. Main-run counters
are not copied to separate control receipts, whose counters remain unavailable.
All report bytes remain in the existing hashed stdout artifact. Unknown fields,
duplicate JSON keys, nonfinite values, partial measurements and conflicting
provenance are refused. The existing 64 KiB report bound remains enforced.

## Bounded Node/Vitest producer plan — not operationally qualified

There is no maintained Node/Vitest producer or qualified native verifier image
established by this change. No command allowance, container image, execution
package, owner manifest, credentials or profile is changed. This increment is
the strict decoder/projection; the following producer remains a prerequisite.

1. Use the repository's existing locked Node/Vitest/React dependencies. Qualify
   the actual normal verifier image and installed tool paths/versions before
   admitting a command. A Python image, lockfile or host package inventory is
   insufficient. Refuse missing tools; do not install during verification or
   fall back to a host interpreter.
2. Freeze one task-specific producer, its explicit Vitest configuration and
   independent verifier files outside worker write scope. Seal every file
   through the existing `VerificationPlan.verifier_paths/verifier_digests`.
   The frozen command has fixed arguments, uses the installed normal Vitest
   CLI, runs only the admitted verifier and emits this report. It does not run
   candidate package hooks or choose a shell command from request input.
   An exact command must undergo normal authorization; this plan grants none.
3. Invoke the existing Vitest JSON reporter with a fresh private output file.
   Cap child output while reading, share one finite deadline including child
   reap, and retain uncertainty on failure. Reuse the existing owned sandbox
   lifecycle; no alternate Docker or SSH process launcher is introduced.
4. Parse the actual bounded JSON report. Check unique expected case IDs,
   assertion statuses, suite failures, framework success and process exit.
   Derive collected from `numTotalTests`, executed from actual passed plus
   failed, and skipped from pending plus todo. Cross-check all totals against
   the assertions. For packet A, the frozen twenty cases include the named
   UTF-8 boundary refusals. Never substitute the expected count for a measured
   count or parse a passing console line as a report.
5. Read Node's actual runtime version and the actually loaded framework/package
   versions. Hash the actual producer/config/verifier files for provenance;
   do not copy desired pins into an observation. Retain raw framework reports
   and named results within the bounded stdout artifact or the existing sealed
   artifact interface. Their storage/capture must be accepted before admission.
6. Run a separately named positive witness against the current candidate and
   the unchanged independent verifier against an isolated temporary copy with
   the known wrong UTF-16-length implementation. Require an actual named UTF-8
   boundary assertion failure, with no import/framework error or skip, before
   reporting the negative observation `FAIL`. Never modify the candidate for
   a control or ship a preauthored passing candidate as the pilot output.
7. Independently review the exact producer and its report controls, then prove
   actual tools/image, command authorization, cleanup and source binding through
   normal operational acceptance. Only actual runs supply measured evidence.

The real two-packet TMOS pilot, independent semantic review, exact-source
landing, normal installation/runtime verification and separate Plane readiness
reconciliation remain required. This decoder's synthetic tests grant no pilot
credit. Physical phone pairing remains a separate desktop-independent step.
