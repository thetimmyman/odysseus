"""src/execution_ledger.py — canonical append-only execution ledger (PS-635).

Why this module exists:

    canonical state must live OUTSIDE conversational history.

A worker conversation is disposable; the task is not. Every run, attempt,
verification, repair and decision is recorded here as an append-only entry, and a
worker's context is *reconstructed* from these entries plus its packet. That is
what makes it safe to throw a conversation away — and what makes it possible to
hand a later strong-model reviewer the exact provenance of a result without
asking it to read a transcript.

Two properties are enforced mechanically rather than remembered:

**1. ``ACCEPTED`` is not a state a worker-driven run can reach.**
:data:`RESULT_ACCEPTED_CANDIDATE` is the highest terminal result the loop itself
can write; a full ``ACCEPTED`` exists only as a *separate* entry written by
:meth:`ExecutionLedger.record_strong_model_acceptance`, which requires a named
authority that is not a local worker. PS-579 measured that this model has no
reviewer authority, so self-approval is made unrepresentable rather than
discouraged.

**2. Entries are hash-chained.** Each entry carries ``prev_hash`` and
``entry_hash``, so :meth:`ExecutionLedger.verify_chain` can prove the ledger was
never rewritten. An append-only ledger that cannot detect a rewrite is just a
log with good intentions.

This module owns RECORDING. It does not pick targets, enforce privacy policy,
manage budgets, or decide whether to retry — those belong to PS-605 and to the
bounded loop in ``src/local_worker_loop.py``.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Sequence

#: Bump when the entry shape changes in a way a reader must know about. Readers
#: refuse an unknown major version rather than guessing at field meanings.
LEDGER_SCHEMA_VERSION = 1

#: Entry kinds. Kept small and explicit: an open-ended "metadata" blob defeats
#: the point of having a canonical record.
KIND_RUN = "run"
KIND_ATTEMPT = "attempt"
KIND_VERIFICATION = "verification"
KIND_REPAIR = "repair"
KIND_DECISION = "decision"
KIND_ACCEPTANCE = "acceptance"
KIND_FAILURE = "failure"
#: G2 (PS-635): an advisory manager proposal that PASSED deterministic
#: validation, recorded so the advice that influenced a run is canonical
#: evidence rather than something a transcript has to be trusted for.
KIND_PROPOSAL = "proposal"
#: A proposal that was refused, with its typed code. The refusal is evidence
#: too: "the manager asked for something it may not have" is a finding about
#: the manager, and dropping it would hide exactly that.
KIND_PROPOSAL_REFUSED = "proposal_refused"

KNOWN_KINDS = frozenset({KIND_RUN, KIND_ATTEMPT, KIND_VERIFICATION, KIND_REPAIR,
                         KIND_DECISION, KIND_ACCEPTANCE, KIND_FAILURE,
                         KIND_PROPOSAL, KIND_PROPOSAL_REFUSED})

#: Terminal results a run/attempt can carry. NOTE the absence of a plain
#: ``ACCEPTED`` — see the module docstring.
RESULT_ACCEPTED_CANDIDATE = "ACCEPTED_CANDIDATE"
RESULT_REJECTED = "REJECTED"
RESULT_BLOCKED = "BLOCKED"
RESULT_ESCALATE = "ESCALATE"
RESULT_IN_PROGRESS = "IN_PROGRESS"
#: Reachable ONLY through :meth:`ExecutionLedger.record_strong_model_acceptance`.
#: Every other write path is refused this value, which is how "a worker cannot
#: approve its own work" is enforced by the ledger rather than by convention.
RESULT_ACCEPTED = "ACCEPTED"

KNOWN_RESULTS = frozenset({RESULT_ACCEPTED_CANDIDATE, RESULT_REJECTED,
                           RESULT_BLOCKED, RESULT_ESCALATE, RESULT_IN_PROGRESS,
                           RESULT_ACCEPTED})

#: Failure classes, kept separate on purpose. A provider outage is NEVER model
#: evidence, and a context-sizing failure is neither.
FAILURE_TECHNICAL = "technical"
FAILURE_RUNTIME_PROVIDER = "runtime_provider"
FAILURE_CONTEXT = "context"
FAILURE_PROTOCOL = "protocol"
FAILURE_TOOL_CHANNEL = "tool_channel"
FAILURE_POLICY = "policy"
#: The packet itself is not dispatchable (missing id/objective/write scope/
#: interface/test command). An AUTHORING error, distinct from every runtime and
#: task failure class above: nothing was executed, and no model was involved.
FAILURE_PACKET_INVALID = "packet_invalid"

KNOWN_FAILURES = frozenset({FAILURE_TECHNICAL, FAILURE_RUNTIME_PROVIDER,
                            FAILURE_CONTEXT, FAILURE_PROTOCOL,
                            FAILURE_TOOL_CHANNEL, FAILURE_POLICY,
                            FAILURE_PACKET_INVALID})

#: Authorities that may NOT commit an acceptance. Anything naming a local worker
#: is refused; acceptance needs a stronger, separately-accountable authority.
LOCAL_WORKER_AUTHORITIES = frozenset({"local_worker", "local_qwen", "local_target"})

#: Only these authorities can commit an acceptance entry.
ACCEPTING_AUTHORITIES = frozenset({"operator", "strong_model"})



def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(payload: dict) -> str:
    """Deterministic JSON for hashing. Sorted keys, no incidental whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class LedgerEntry:
    """One immutable ledger record.

    ``entry_hash`` covers ``prev_hash`` plus the entry's own core fields, so
    altering any recorded value (or reordering, or deleting an entry) breaks
    :meth:`ExecutionLedger.verify_chain`.
    """

    entry_id: str
    seq: int
    ts: str
    run_id: str
    packet_id: str
    kind: str
    payload: dict
    prev_hash: str
    entry_hash: str

    def to_dict(self) -> dict:
        return {
            "schema": LEDGER_SCHEMA_VERSION,
            "entry_id": self.entry_id,
            "seq": self.seq,
            "ts": self.ts,
            "run_id": self.run_id,
            "packet_id": self.packet_id,
            "kind": self.kind,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }

    @staticmethod
    def compute_hash(seq: int, ts: str, run_id: str, packet_id: str,
                     kind: str, payload: dict, prev_hash: str) -> str:
        core = _canonical({
            "seq": seq, "ts": ts, "run_id": run_id, "packet_id": packet_id,
            "kind": kind, "payload": payload, "prev_hash": prev_hash,
        })
        return hashlib.sha256(core.encode("utf-8")).hexdigest()



class LedgerError(Exception):
    """Raised on an attempt to write something the ledger must not represent."""


class ExecutionLedger:
    """Append-only, hash-chained ledger backed by one JSONL file.

    Deliberately a thin file-backed structure rather than a database: PS-635's
    first requirement is that the canonical record exist and be independently
    inspectable, and a JSONL file can be read, diffed and archived by anything.
    Concurrency is an in-process lock plus one append per entry, which is correct
    for the single-writer-per-process shape the control plane has today.
    """

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._seq = 0
        self._last_hash = ""
        self._cache: List[LedgerEntry] = []
        self._loaded = False

    # ------------------------------------------------------------- loading ---
    def _load(self) -> None:
        if self._loaded:
            return
        self._cache = []
        if os.path.exists(self.path):
            with open(self.path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    raw = json.loads(line)
                    self._cache.append(LedgerEntry(
                        entry_id=raw["entry_id"], seq=int(raw["seq"]),
                        ts=raw["ts"], run_id=raw["run_id"],
                        packet_id=raw.get("packet_id", ""), kind=raw["kind"],
                        payload=raw.get("payload") or {},
                        prev_hash=raw.get("prev_hash", ""),
                        entry_hash=raw.get("entry_hash", ""),
                    ))
        self._seq = self._cache[-1].seq if self._cache else 0
        self._last_hash = self._cache[-1].entry_hash if self._cache else ""
        self._loaded = True

    # ------------------------------------------------------------ appending ---
    def append(self, kind: str, *, run_id: str, packet_id: str = "",
               payload: Optional[dict] = None) -> LedgerEntry:
        """Append one entry and return it. Never overwrites, never updates."""
        if kind not in KNOWN_KINDS:
            raise LedgerError(f"unknown entry kind: {kind!r}")
        body = dict(payload or {})
        self._reject_unrepresentable(kind, body)
        with self._lock:
            self._load()
            seq = self._seq + 1
            ts = _utc_iso()
            prev = self._last_hash
            entry_hash = LedgerEntry.compute_hash(seq, ts, run_id, packet_id,
                                                  kind, body, prev)
            entry = LedgerEntry(entry_id=uuid.uuid4().hex, seq=seq, ts=ts,
                                run_id=run_id, packet_id=packet_id, kind=kind,
                                payload=body, prev_hash=prev, entry_hash=entry_hash)
            directory = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(directory, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry.to_dict(), sort_keys=True) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._cache.append(entry)
            self._seq = seq
            self._last_hash = entry_hash
            return entry

    def _reject_unrepresentable(self, kind: str, payload: dict) -> None:
        """Refuse records that would let a worker authorise its own work."""
        if kind == KIND_ACCEPTANCE:
            authority = str(payload.get("authority", "")).strip()
            if authority in LOCAL_WORKER_AUTHORITIES:
                raise LedgerError(
                    "a local worker authority cannot commit an acceptance: "
                    f"{authority!r} (PS-579 measured no local reviewer authority)")
            if authority not in ACCEPTING_AUTHORITIES:
                raise LedgerError(
                    f"acceptance authority must be one of "
                    f"{sorted(ACCEPTING_AUTHORITIES)}, got {authority!r}")
        result = payload.get("result")
        if result is not None and result not in KNOWN_RESULTS:
            raise LedgerError(f"unknown result: {result!r}")
        if result == RESULT_ACCEPTED and kind != KIND_ACCEPTANCE:
            raise LedgerError(
                "ACCEPTED may only be written by record_strong_model_acceptance: "
                f"a {kind} entry cannot accept work")
        failure = payload.get("failure_class")
        if failure and failure not in KNOWN_FAILURES:
            raise LedgerError(f"unknown failure_class: {failure!r}")
        # G2 (PS-635): a proposal is ADVICE, and the ledger is where that stops
        # being a convention. An entry of either proposal kind must not carry a
        # lifecycle result (it would let advice read as state), and an ACCEPTED
        # proposal must not carry an authority request or a forbidden action --
        # a validated proposal that still asks for acceptance is a contradiction
        # in the record, not a judgement call for a later reader.
        if kind in (KIND_PROPOSAL, KIND_PROPOSAL_REFUSED):
            if payload.get("result"):
                raise LedgerError(
                    f"a {kind} entry must not carry a lifecycle result: a "
                    "proposal is advice, never state")
            if kind == KIND_PROPOSAL:
                if payload.get("requested_actions"):
                    raise LedgerError(
                        "an accepted proposal must not request an action: "
                        f"{payload.get('requested_actions')!r}")
                if payload.get("requested_authority"):
                    raise LedgerError(
                        "an accepted proposal must not request authority: "
                        f"{payload.get('requested_authority')!r}")


    # -------------------------------------------------------------- reading ---
    def entries(self) -> List[LedgerEntry]:
        """Every entry, in append order."""
        with self._lock:
            self._load()
            return list(self._cache)

    def entries_for(self, run_id: str) -> List[LedgerEntry]:
        return [e for e in self.entries() if e.run_id == run_id]

    def latest(self, run_id: str, kind: str) -> Optional[LedgerEntry]:
        found = [e for e in self.entries_for(run_id) if e.kind == kind]
        return found[-1] if found else None

    def attempts(self, run_id: str) -> List[LedgerEntry]:
        return [e for e in self.entries_for(run_id) if e.kind == KIND_ATTEMPT]

    def terminal_result(self, run_id: str) -> str:
        """Newest non-IN_PROGRESS result, else IN_PROGRESS."""
        for entry in reversed(self.entries_for(run_id)):
            result = entry.payload.get("result")
            if result and result != RESULT_IN_PROGRESS:
                return result
        return RESULT_IN_PROGRESS

    def failure_class(self, run_id: str) -> str:
        """Newest recorded failure class, or '' if the run has none.

        Read separately from ``terminal_result``: a run can be ``BLOCKED`` by an
        infrastructure outage and ``ESCALATE``d by a task failure, and the two
        must never be summarised into one word.
        """
        for entry in reversed(self.entries_for(run_id)):
            failure = entry.payload.get("failure_class")
            if failure:
                return failure
        return ""

    def verify_chain(self) -> tuple:
        """(ok, first_broken_seq). Detects any rewrite of the recorded history."""
        with self._lock:
            self._load()
            prev = ""
            for entry in self._cache:
                if entry.prev_hash != prev:
                    return False, entry.seq
                expected = LedgerEntry.compute_hash(
                    entry.seq, entry.ts, entry.run_id, entry.packet_id,
                    entry.kind, entry.payload, entry.prev_hash)
                if expected != entry.entry_hash:
                    return False, entry.seq
                prev = entry.entry_hash
            return True, None

    """Raised on an attempt to write something the ledger must not represent."""

    # ----------------------------------------------------------- write paths ---
    def record_run(self, *, run_id: str, packet_id: str, objective: str,
                   role: str, target_id: str, host: str, model: str,
                   runtime_version: str, worktree: str, write_scope: Sequence[str],
                   base_sha: str = "", interface_digest: str = "",
                   interface: Sequence[str] = ()) -> LedgerEntry:
        """Open a run and pin its execution identity.

        The identity fields live here rather than being inferred later, because a
        result whose target cannot be named is not evidence — PS-579's problem
        was a result attributed to a generic "local qwen". The interface digest is
        recorded for the same reason: it is what proves the worker was given the
        SAME declared input keys on every attempt, including the repair attempt.
        """
        payload = {
            "objective": objective, "role": role, "target_id": target_id,
            "host": host, "model": model, "runtime_version": runtime_version,
            "worktree": worktree, "write_scope": list(write_scope),
            "base_sha": base_sha, "result": RESULT_IN_PROGRESS,
            "interface_digest": interface_digest,
        }
        if interface:
            payload["interface"] = list(interface)
        return self.append(KIND_RUN, run_id=run_id, packet_id=packet_id,
                           payload=payload)

    def record_attempt(self, *, run_id: str, packet_id: str, attempt: int,
                       target_id: str, host: str, num_ctx: int,
                       served_context: Optional[int], elapsed_s: float,
                       rounds: int, artifacts: Sequence[str],
                       failure_class: str = "", status: str = "",
                       result: str = RESULT_IN_PROGRESS,
                       timings: Optional[dict] = None) -> LedgerEntry:
        payload = {
            "attempt": attempt, "target_id": target_id, "host": host,
            "num_ctx": num_ctx, "served_context": served_context,
            "elapsed_s": elapsed_s, "rounds": rounds,
            "artifacts": list(artifacts), "status": status, "result": result,
        }
        if failure_class:
            payload["failure_class"] = failure_class
        if timings:
            payload["timings"] = dict(timings)
        return self.append(KIND_ATTEMPT, run_id=run_id, packet_id=packet_id,
                           payload=payload)

    def record_verification(self, *, run_id: str, packet_id: str,
                            test_command: str, passed: bool, returncode: int,
                            summary: Sequence[str] = (),
                            excerpt: str = "", attempt: int = 0) -> LedgerEntry:
        """Record the deterministic verdict.

        ``passed`` comes from the test runner, never from a model. The excerpt is
        bounded HERE so the ledger cannot accumulate a full test log per attempt:
        the repair packet is built from this, and a repair packet that quotes the
        whole log is not compact.

        ``attempt`` says WHICH attempt this verdict belongs to. It is optional so
        older writers keep working, but a verification that cannot be attributed
        to an attempt is a verdict nobody can act on — found live (PS-635 G2, run
        g2-replan-control-20260914T221758Z): the manager projection joined
        verdicts to attempts by attempt number, the number was absent, and the
        advisory context reported a PASSING attempt 1 as FAIL. The projection now
        refuses to claim a failure it has no record for, and the loop tags every
        verdict it writes.
        """
        payload = {
            "test_command": test_command,
            "passed": bool(passed),
            "returncode": int(returncode),
            "summary": list(summary)[-3:],
            "excerpt": (excerpt or "")[-4000:],
            "result": (RESULT_ACCEPTED_CANDIDATE if passed else RESULT_REJECTED),
        }
        if attempt:
            payload["attempt"] = int(attempt)
        return self.append(KIND_VERIFICATION, run_id=run_id,
                           packet_id=packet_id, payload=payload)

    def record_repair(self, *, run_id: str, packet_id: str, attempt: int,
                      failure_class: str, fingerprint: str,
                      repair_packet: dict) -> LedgerEntry:
        return self.append(KIND_REPAIR, run_id=run_id, packet_id=packet_id,
                           payload={"attempt": attempt,
                                    "failure_class": failure_class,
                                    "fingerprint": fingerprint,
                                    "repair_packet": repair_packet})

    def record_decision(self, *, run_id: str, packet_id: str, decision: str,
                        reason: str, result: str) -> LedgerEntry:
        """Record the manager/reconciler stop-or-replan decision.

        ``decision`` is one of ``next`` / ``stop`` / ``escalate`` / ``blocked``.
        The reason is mandatory: an unexplained transition is exactly what makes a
        multi-worker history unreadable later.
        """
        if decision not in ("next", "stop", "escalate", "blocked"):
            raise LedgerError(f"unknown decision: {decision!r}")
        if not reason:
            raise LedgerError("a decision must record its reason")
        return self.append(KIND_DECISION, run_id=run_id, packet_id=packet_id,
                           payload={"decision": decision, "reason": reason,
                                    "result": result})

    def record_strong_model_acceptance(self, *, run_id: str, packet_id: str,
                                       authority: str, notes: str = "") -> LedgerEntry:
        """The ONLY path to acceptance. Refuses a local-worker authority."""
        return self.append(KIND_ACCEPTANCE, run_id=run_id, packet_id=packet_id,
                           payload={"authority": authority, "notes": notes,
                                    "result": RESULT_ACCEPTED})

    def record_proposal(self, *, run_id: str, packet_id: str, proposal: dict,
                        verdict: dict) -> LedgerEntry:
        """Record a VALIDATED advisory proposal.

        Only called with a verdict that PASSED deterministic validation; the
        ledger refuses the action/authority fields outright, so a proposal that
        reached here asking for acceptance would be rejected rather than stored.
        """
        payload = {
            "proposal_id": str(proposal.get("proposal_id", "")),
            "proposal_hash": str(proposal.get("proposal_hash", "")),
            "kind": str(proposal.get("kind", "")),
            "rationale": str(proposal.get("rationale", "")),
            "accepted": True,
            "verdict_code": "",
            "checks": list(verdict.get("checks") or ()),
        }
        approach = str(proposal.get("approach") or "")
        if approach:
            payload["approach"] = approach
            payload["approach_digest"] = str(verdict.get("approach_digest") or "")
        delta = proposal.get("packet_delta") or {}
        if delta:
            payload["packet_delta"] = dict(delta)
        if proposal.get("evidence_refs"):
            payload["evidence_refs"] = list(proposal["evidence_refs"])
        if proposal.get("requested_actions"):
            payload["requested_actions"] = list(proposal["requested_actions"])
        if proposal.get("requested_authority"):
            payload["requested_authority"] = list(proposal["requested_authority"])
        return self.append(KIND_PROPOSAL, run_id=run_id, packet_id=packet_id,
                           payload=payload)

    def record_proposal_refusal(self, *, run_id: str, packet_id: str,
                                proposal: dict, code: str, detail: str) -> LedgerEntry:
        """Record a REFUSED proposal with its typed code and what it asked for."""
        payload = {
            "proposal_id": str(proposal.get("proposal_id", "")),
            "proposal_hash": str(proposal.get("proposal_hash", "")),
            "kind": str(proposal.get("kind", "")),
            "rationale": str(proposal.get("rationale", "")),
            "accepted": False,
            "verdict_code": str(code),
            "verdict_detail": str(detail)[:600],
        }
        for name in ("requested_actions", "requested_authority", "evidence_refs"):
            if proposal.get(name):
                payload[name] = list(proposal[name])
        delta = proposal.get("packet_delta") or {}
        if delta:
            payload["packet_delta"] = dict(delta)
        return self.append(KIND_PROPOSAL_REFUSED, run_id=run_id,
                           packet_id=packet_id, payload=payload)

    def proposals(self, run_id: str) -> List[LedgerEntry]:
        """Accepted proposals for a run, in append order."""
        return [e for e in self.entries_for(run_id) if e.kind == KIND_PROPOSAL]

    def proposal_refusals(self, run_id: str) -> List[LedgerEntry]:
        return [e for e in self.entries_for(run_id)
                if e.kind == KIND_PROPOSAL_REFUSED]

    # ------------------------------------------------------- provenance views ---
    def provenance(self, run_id: str) -> dict:
        """Everything a later strong-model reviewer needs, and nothing else.

        Deliberately a SUMMARY: worker conversations are not replayed, because a
        reviewer handed a transcript reviews the transcript. What can be checked
        is the identity, the attempts, the deterministic verdicts and the
        decisions.
        """
        entries = self.entries_for(run_id)
        run = next((e for e in entries if e.kind == KIND_RUN), None)
        return {
            "run_id": run_id,
            "identity": run.payload if run else {},
            "attempts": [e.payload for e in entries if e.kind == KIND_ATTEMPT],
            "verifications": [e.payload for e in entries if e.kind == KIND_VERIFICATION],
            "repairs": [e.payload for e in entries if e.kind == KIND_REPAIR],
            "decisions": [e.payload for e in entries if e.kind == KIND_DECISION],
            "failures": [e.payload for e in entries if e.kind == KIND_FAILURE],
            "acceptances": [e.payload for e in entries if e.kind == KIND_ACCEPTANCE],
            "proposals": [e.payload for e in entries if e.kind == KIND_PROPOSAL],
            "proposal_refusals": [e.payload for e in entries
                                  if e.kind == KIND_PROPOSAL_REFUSED],
            "terminal_result": self.terminal_result(run_id),
            "failure_class": self.failure_class(run_id),
            "chain_ok": self.verify_chain()[0],
        }

