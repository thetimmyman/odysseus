"""Compact failure parsing, fingerprinting, and fresh repair-context
construction for bounded worker retries.

The defect this module exists to prevent:

    a repair retry that re-sends the previous conversation.

When deterministic verification fails, the useful thing to send the worker is not
what it just said — it is WHAT FAILED, measured. Replaying a conversation invites
the worker to defend its earlier answer instead of reading the failure, and it
costs context a small-window node does not have.

So a repair context carries only:

  * the SAME immutable packet facts — objective, contract, the declared
    interface verbatim (normalized by ``src.work_packet``'s one definition and
    digested by it, so a repair cannot restate or "improve" the interface),
    write scope and acceptance criteria;
  * the UNCHANGED verifier contract (command + identity);
  * bounded deterministic failure evidence (failing tests, reasons, one capped
    excerpt, changed files with capped diffs).

and deliberately carries NO prior conversation, NO earlier repair context and no
manager reasoning. The excerpt is bounded at capture and the rendered context is
bounded at render — a repair packet that quotes an entire test log is not compact.

The fingerprint covers the failing test NAMES and the SHAPE of the reasons (not
line numbers or file sizes), so "the repair loop converged" is a comparison of
fingerprints, never of prose.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping, Optional

#: How much of the runner output may travel in a repair context. Chosen so a
#: repair context plus the unchanged contract still fits the tightest node.
DEFAULT_MAX_EXCERPT = 2000

#: Limits that keep a repair context small enough for the tightest node.
MAX_DIFF_CHARS_PER_FILE = 1200
MAX_CHANGED_FILES = 4

#: pytest's short-summary lines: "FAILED tests/x.py::test_y - AssertionError: ..."
_FAILED_RE = re.compile(r"^FAILED\s+(?P<test>\S+)(?:\s+-\s+(?P<why>.*))?$", re.M)
_ERROR_RE = re.compile(r"^ERROR\s+(?P<test>\S+)(?:\s+-\s+(?P<why>.*))?$", re.M)
_COLLECTION_RE = re.compile(
    r"(?:error during collection|^ERROR\s+collecting\s+\S+)", re.I | re.M)


@dataclass(frozen=True)
class VerificationFailure:
    """The measured failure, not a description of one."""

    test_command: str
    exit_code: int
    failing_tests: tuple = ()
    reasons: tuple = ()
    excerpt: str = ""
    collection_error: bool = False

    def to_dict(self) -> dict:
        return {
            "test_command": self.test_command,
            "exit_code": self.exit_code,
            "failing_tests": list(self.failing_tests),
            "reasons": list(self.reasons),
            "excerpt": self.excerpt,
            "collection_error": self.collection_error,
        }


def parse_verification_failure(test_command: str, exit_code: int, output: str, *,
                               max_excerpt: int = DEFAULT_MAX_EXCERPT) -> VerificationFailure:
    """Extract failing test names, reasons and one bounded excerpt.

    Reads pytest's own short summary rather than guessing from prose. A
    collection error is recorded as such: an import failure means the artifact
    is missing or unimportable, which is a DIFFERENT repair from an assertion
    failure, and conflating them sends the worker to fix the wrong thing.
    """
    if (isinstance(max_excerpt, bool) or not isinstance(max_excerpt, int)
            or max_excerpt < 0 or max_excerpt > DEFAULT_MAX_EXCERPT):
        raise ValueError(
            f"max_excerpt must be an integer between 0 and {DEFAULT_MAX_EXCERPT}")
    text = output or ""
    failing, reasons = [], []
    for match in _FAILED_RE.finditer(text):
        failing.append(match.group("test"))
        if match.group("why"):
            reasons.append(match.group("why").strip())
    for match in _ERROR_RE.finditer(text):
        failing.append(match.group("test"))
        if match.group("why"):
            reasons.append(match.group("why").strip())
    return VerificationFailure(
        test_command=test_command or "",
        exit_code=int(exit_code),
        failing_tests=tuple(dict.fromkeys(failing)),
        reasons=tuple(dict.fromkeys(reasons)),
        excerpt=text[-max_excerpt:] if max_excerpt else "",
        collection_error=bool(_COLLECTION_RE.search(text)),
    )


def failure_fingerprint(failure: VerificationFailure) -> str:
    """Stable identity of a failure, for detecting a non-progressing loop.

    Covers the failing test NAMES and the shape of the reasons, but not line
    numbers or file sizes: a retry that fails the same tests for the same reason
    has not made progress even if the diff changed, and a loop that keeps making
    the same mistake will eventually "pass" by accident.
    """
    core = json.dumps({
        "tests": sorted(failure.failing_tests),
        "reasons": [re.sub(r"\d+", "#", r)[:200] for r in sorted(failure.reasons)],
        "collection_error": failure.collection_error,
    }, sort_keys=True)
    return hashlib.sha256(core.encode("utf-8")).hexdigest()[:16]


def _bounded_diff(text: str, limit: int = MAX_DIFF_CHARS_PER_FILE) -> str:
    """Keep the HEAD of a diff and mark that it was cut.

    The head, not the tail: a unified diff's hunks begin with the change, and
    truncating from the front would remove exactly the lines being discussed —
    the same silent-truncation trap the non-streaming paths fell into.
    """
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[DIFF TRUNCATED]"


def _interface_projection(execution_package: Mapping) -> dict:
    """The package's declared interface, projected WITHOUT interpretation.

    The interface travels as its normalized lines (the EXACT strings the worker
    will be shown and the evidence will hash) plus the digest computed by
    ``src.work_packet``'s own definition — the same digest the sealed
    ``ExecutionPackage`` records, so a repair context whose interface drifts is
    detectable, not merely discouraged.
    """
    lines = [str(line) for line in (execution_package.get("interface") or ())]
    from src.work_packet import interface_digest_from_normalized

    return {
        "interface": lines,
        "interface_digest": interface_digest_from_normalized(lines),
    }


def build_repair_context(execution_package: Mapping, *,
                         failure: VerificationFailure,
                         changed_files: Mapping,
                         attempt: int, budget_remaining: int) -> dict:
    """Build the bounded repair context for one failed attempt.

    The objective, contract, interface, write scope and acceptance criteria are
    copied UNCHANGED on purpose, and this function accepts no parameter that
    could override any of them. A repair that is allowed to restate acceptance
    is how a failing task quietly becomes a passing one; one allowed to restate
    the interface is how a worker gets a different task without anyone
    recording it. ``changed_files`` is current-fail evidence (path -> diff text)
    and is the only content besides the failure that may be bounded.
    """
    files: dict = {}
    for path, diff in list((changed_files or {}).items())[:MAX_CHANGED_FILES]:
        files[str(path)] = _bounded_diff(str(diff))
    return {
        "kind": "repair",
        "attempt": int(attempt),
        "budget_remaining": int(budget_remaining),
        "objective": str(execution_package.get("objective") or ""),
        "contract": str(execution_package.get("contract") or ""),
        **_interface_projection(execution_package),
        "acceptance_criteria": [str(c) for c in
                                (execution_package.get("acceptance_criteria") or ())],
        "write_scope": [str(p) for p in (execution_package.get("write_scope") or ())],
        "verifier": {
            "verifier_id": str((execution_package.get("verification") or {}).get("verifier_id") or ""),
            "command": str((execution_package.get("verification") or {}).get("command") or ""),
        },
        "expected_behavior": ("the deterministic test named below must pass; "
                              "it is the SAME test as before and the acceptance "
                              "criteria have NOT changed"),
        "failing_command": failure.test_command,
        "failing_tests": list(failure.failing_tests),
        "failure_reasons": list(failure.reasons),
        "collection_error": failure.collection_error,
        "error_excerpt": failure.excerpt,
        "changed_files": files,
        "fingerprint": failure_fingerprint(failure),
        # Recorded so a reader can see what was deliberately withheld.
        "excluded": ["prior conversation", "prior repair contexts",
                     "manager reasoning"],
    }


_CONTEXT_TRUNC_MARKER = "\n...[CONTEXT TRUNCATED]"


class RepairContextTooLarge(ValueError):
    """The immutable task contract alone exceeds the declared repair budget."""


def render_repair_context(repair: Mapping, *, max_chars: int = 6000) -> str:
    """Render a repair context as bounded worker text.

    One function defines what the worker sees; the attempt receipt hashes the
    output of this function, so "what the worker was shown" cannot be derived a
    second way. Truncation is always marked so a cut prompt can never be
    mistaken for a complete one.
    """
    lines = []
    lines.append("REPAIR REQUEST — the previous attempt FAILED DETERMINISTIC TESTS.")
    lines.append(f"this is attempt {repair.get('attempt')}; "
                 f"budget remaining {repair.get('budget_remaining')}")
    lines.append("")
    lines.append(f"OBJECTIVE: {repair.get('objective') or 'NONE'}")
    lines.append("CONTRACT (UNCHANGED -- implement exactly this):")
    contract = str(repair.get("contract") or "")
    lines.append(contract if contract else "NONE")
    lines.append("ACCEPTANCE (UNCHANGED — do not restate these):")
    for criterion in repair.get("acceptance_criteria") or []:
        lines.append(f"  - {criterion}")
    lines.append(f"WRITE_SCOPE: {', '.join(repair.get('write_scope') or []) or 'NONE'}")
    lines.append("INTERFACE (UNCHANGED -- these exact input keys are the contract):")
    interface = repair.get("interface") or []
    if interface:
        for item in interface:
            lines.append(f"  - {item}")
    else:
        lines.append("  - NONE")
    if repair.get("interface_digest"):
        lines.append(f"INTERFACE_DIGEST: {repair['interface_digest']}")
    lines.append(f"EXPECTED_BEHAVIOR: {repair.get('expected_behavior')}")
    lines.append("")
    lines.append("VERIFIER CONTRACT (UNCHANGED):")
    lines.append(f"  verifier: {repair.get('verifier', {}).get('verifier_id') or 'NONE'}")
    lines.append(f"  command:  {repair.get('verifier', {}).get('command') or 'NONE'}")
    lines.append("")
    lines.append(f"FAILING_COMMAND: {repair.get('failing_command')}")
    if repair.get("failing_tests"):
        lines.append("FAILING_TESTS:")
        for test in repair["failing_tests"]:
            lines.append(f"  - {test}")
    if repair.get("failure_reasons"):
        lines.append("FAILURE_REASONS:")
        for reason in repair["failure_reasons"]:
            lines.append(f"  - {reason}")
    if repair.get("collection_error"):
        lines.append("NOTE: the previous attempt could not even be IMPORTED "
                     "(collection error). Fix the module/interface before behaviour.")
    files = repair.get("changed_files") or {}
    if files:
        lines.append("")
        lines.append("CHANGED_FILES:")
        for path, diff in files.items():
            lines.append(f"--- {path} ---")
            lines.append(diff)
    lines.append("")
    lines.append("ERROR_EXCERPT:")
    lines.append(str(repair.get("error_excerpt") or ""))
    lines.append("")
    lines.append("Repair the DEFECT shown above. Do NOT change the objective, the "
                 "interface, the write scope or the acceptance criteria: those are "
                 "unchanged and are not yours to reinterpret.")
    text = "\n".join(lines)
    limit = int(max_chars)
    if limit <= 0:
        raise RepairContextTooLarge("repair context budget must be positive")
    if len(text) <= limit:
        return text

    # The objective, contract, acceptance, scope, interface and verifier are
    # immutable. Never satisfy a character budget by cutting those fields.
    # The evidence portion starts at FAILING_COMMAND and may be omitted or
    # shortened, with an explicit marker, if it does not fit.
    # Rebuild from the line boundary (including verifier id and command).
    evidence_start = next((i for i, line in enumerate(lines)
                           if line.startswith("FAILING_COMMAND:")), len(lines))
    prefix_lines = lines[:evidence_start]
    prefix = "\n".join(prefix_lines).rstrip()
    if len(prefix) + len(_CONTEXT_TRUNC_MARKER) > limit:
        raise RepairContextTooLarge(
            "immutable objective/contract/interface/verifier exceeds repair budget")
    suffix = "\n".join(lines[len(prefix_lines):])
    room = limit - len(prefix) - len(_CONTEXT_TRUNC_MARKER) - 1
    evidence = suffix[:max(0, room)]
    return prefix + "\n" + evidence + _CONTEXT_TRUNC_MARKER


def render_initial_context(execution_package: Mapping) -> str:
    """The FRESH first-attempt context, built from the immutable package alone.

    Every attempt in the loop is given a fresh context: the first one renders
    the package, later ones render repair contexts. Neither ever carries prior
    conversation. The declared interface lines appear VERBATIM (the same
    normalized strings the canonical evidence validator searches for), so the
    context-projection check holds for attempt 1 exactly as for repairs.
    """
    lines = []
    lines.append("WORK REQUEST — implement to the declared contract.")
    lines.append("")
    lines.append(f"OBJECTIVE: {str(execution_package.get('objective') or '')}")
    lines.append("CONTRACT (implement exactly this):")
    contract = str(execution_package.get("contract") or "")
    lines.append(contract if contract else "NONE")
    lines.append("ACCEPTANCE:")
    for criterion in execution_package.get("acceptance_criteria") or []:
        lines.append(f"  - {str(criterion)}")
    lines.append(f"WRITE_SCOPE: {', '.join(str(p) for p in
                                           (execution_package.get('write_scope') or ())) or 'NONE'}")
    lines.append("INTERFACE (declare every input key by EXACTLY this name):")
    interface = [str(line) for line in (execution_package.get("interface") or ())]
    if interface:
        for item in interface:
            lines.append(f"  - {item}")
    else:
        lines.append("  - NONE")
    if interface:
        from src.work_packet import interface_digest_from_normalized
        lines.append(f"INTERFACE_DIGEST: {interface_digest_from_normalized(interface)}")
    lines.append("")
    lines.append(f"TEST (deterministic, run by the harness, not by you):")
    verification = execution_package.get("verification") or {}
    lines.append(f"  verifier: {str(verification.get('verifier_id') or '')}")
    lines.append(f"  command:  {str(verification.get('command') or '')}")
    lines.append("")
    lines.append("Write only within the declared write scope. Do not explain; emit "
                 "the artifact the contract describes.")
    return "\n".join(lines)
