"""src/repair_packet.py — compact failure evidence to repair packet (PS-635).

The defect this module exists to prevent:

    a repair retry that re-sends the previous conversation.

When deterministic verification fails, the useful thing to send the worker is NOT
what it just said — it is WHAT FAILED, measured. Replaying a conversation invites
the worker to defend its earlier answer instead of reading the failure; it also
costs context that a 4 096-window node does not have.

So a repair packet carries only what is needed to fix the specific failure:

  * the original objective
  * the UNCHANGED acceptance criteria (a repair may not move the goalposts)
  * the changed files (paths + bounded diff)
  * the exact failing command
  * the failing test names and a bounded assertion excerpt
  * expected behaviour
  * budget remaining

and deliberately carries NO prior conversation, NO earlier repair packets and no
manager reasoning.

The excerpt is bounded twice — at capture and at render — because a repair
packet that quotes an entire test log is not compact, and on the small-window
node it would not fit at all.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Sequence

#: How much of a failing assertion / pytest output may travel. Chosen so a
#: repair packet plus a small contract still fits the 4 096-token node.
DEFAULT_MAX_EXCERPT = 2000

#: pytest's short-summary lines: "FAILED tests/x.py::test_y - AssertionError: ..."
_FAILED_RE = re.compile(r"^FAILED\s+(?P<test>\S+)(?:\s+-\s+(?P<why>.*))?$", re.M)
_ERROR_RE = re.compile(r"^ERROR\s+(?P<test>\S+)(?:\s+-\s+(?P<why>.*))?$", re.M)


@dataclass(frozen=True)
class VerificationFailure:
    """The measured failure, not a description of one."""

    test_command: str
    returncode: int
    failing_tests: tuple = ()
    reasons: tuple = ()
    excerpt: str = ""
    collection_error: bool = False

    def to_dict(self) -> dict:
        return {
            "test_command": self.test_command,
            "returncode": self.returncode,
            "failing_tests": list(self.failing_tests),
            "reasons": list(self.reasons),
            "excerpt": self.excerpt,
            "collection_error": self.collection_error,
        }


def parse_verification_failure(test_command: str, returncode: int, output: str, *,
                               max_excerpt: int = DEFAULT_MAX_EXCERPT) -> VerificationFailure:
    """Extract the failing test names, the reasons and one bounded excerpt.

    Reads pytest's own short summary rather than guessing from prose. A
    collection error is recorded as such: an import failure means the artifact is
    missing or unimportable, which is a DIFFERENT repair from an assertion
    failure, and conflating them sends the worker to fix the wrong thing.
    """
    text = output or ""
    failing: List[str] = []
    reasons: List[str] = []
    for match in _FAILED_RE.finditer(text):
        failing.append(match.group("test"))
        if match.group("why"):
            reasons.append(match.group("why").strip())
    for match in _ERROR_RE.finditer(text):
        failing.append(match.group("test"))
        if match.group("why"):
            reasons.append(match.group("why").strip())
    collection_error = bool(re.search(r"error during collection|ImportError|ModuleNotFoundError", text))
    return VerificationFailure(
        test_command=test_command,
        returncode=int(returncode),
        failing_tests=tuple(dict.fromkeys(failing)),
        reasons=tuple(dict.fromkeys(reasons)),
        excerpt=text[-max_excerpt:],
        collection_error=collection_error,
    )


def failure_fingerprint(failure: VerificationFailure) -> str:
    """Stable identity of a failure, for detecting a non-progressing loop.

    Covers the failing test NAMES and the shape of the reason, but not line
    numbers or file sizes: a retry that fails the same tests for the same reason
    has not made progress even if the diff changed, and a repair loop that keeps
    going will eventually produce something that passes by accident.
    """
    core = json.dumps({
        "tests": sorted(failure.failing_tests),
        "reasons": [re.sub(r"\d+", "#", r)[:200] for r in sorted(failure.reasons)],
        "collection_error": failure.collection_error,
    }, sort_keys=True)
    return hashlib.sha256(core.encode("utf-8")).hexdigest()[:16]


#: Limits that keep a repair packet small enough for the tightest node.
MAX_DIFF_CHARS_PER_FILE = 1200
MAX_CHANGED_FILES = 4


def _bounded_diff(text: str, limit: int = MAX_DIFF_CHARS_PER_FILE) -> str:
    """Keep the head of a diff and mark that it was cut.

    The head, not the tail: a unified diff's hunks begin with the change, and
    truncating from the front would remove the very lines being discussed — the
    same silent-truncation trap measured on the non-streaming `/api/generate`
    path (see FINDINGS F2).
    """
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[DIFF TRUNCATED]"


def _interface_projection(packet: Mapping) -> dict:
    """The packet's declared interface, projected for a repair packet.

    Copied VERBATIM in meaning: the repair packet carries the same declared input
    keys, normalised the same way, with a digest computed by the packet primitive's
    own definition. Nothing here may rename, reinterpret, infer or "improve" the
    interface — a repair that is allowed to restate the interface could quietly
    change the task, which is the same failure mode as letting it restate the
    acceptance criteria.
    """
    raw = packet.get("interface")
    try:
        from src.work_packet import coerce_interface, interface_digest_of

        fields = coerce_interface(raw)
    except Exception as exc:  # malformed interface: surface it, do not hide it
        return {"interface": raw, "interface_digest": "",
                "interface_error": str(exc)[:200]}
    return {"interface": [f.normalized() for f in fields],
            "interface_digest": interface_digest_of(fields)}


def build_repair_packet(packet: Mapping, *, failure: VerificationFailure,
                        changed_files: Mapping[str, str],
                        attempt: int, budget_remaining: int) -> dict:
    """Build the bounded repair packet for one failed attempt.

    The objective, the interface and the acceptance criteria are copied UNCHANGED
    on purpose, and this function accepts no parameter that could override any of
    them. A repair that is allowed to restate acceptance is how a failing task
    quietly becomes a passing one; a repair allowed to restate the interface is how
    a worker gets a different task without anyone recording it.
    """
    files: Dict[str, str] = {}
    for path, diff in list(changed_files.items())[:MAX_CHANGED_FILES]:
        files[path] = _bounded_diff(diff)
    return {
        "kind": "repair",
        "attempt": int(attempt),
        "budget_remaining": int(budget_remaining),
        "objective": packet.get("objective", ""),
        "contract": packet.get("contract", ""),
        **_interface_projection(packet),
        "acceptance_criteria": list(packet.get("acceptance_criteria") or ()),
        "write_scope": list(packet.get("write_scope") or ()),
        "negative_control": packet.get("negative_control", ""),
        "stop_conditions": list(packet.get("stop_conditions") or ()),
        "expected_behavior": ("the deterministic test named below must pass; "
                              "it is the same test as before and its acceptance "
                              "criteria have NOT changed"),
        "failing_command": failure.test_command,
        "failing_tests": list(failure.failing_tests),
        "failure_reasons": list(failure.reasons),
        "collection_error": failure.collection_error,
        "error_excerpt": failure.excerpt,
        "changed_files": files,
        "fingerprint": failure_fingerprint(failure),
        # Recorded so a reader can see what was deliberately withheld.
        "excluded": ["prior conversation", "prior repair packets", "manager reasoning"],
    }


def render_repair_context(repair: Mapping, *, max_chars: int = 6000) -> str:
    """Render the repair packet as bounded worker context.

    Section order mirrors the operator's repair-packet contract, and truncation
    is always marked so nobody mistakes a cut prompt for a complete one.
    """
    lines: List[str] = []
    lines.append("REPAIR REQUEST — the previous attempt FAILED DETERMINISTIC TESTS.")
    lines.append(f"this is attempt {repair.get('attempt')}; "
                 f"budget remaining {repair.get('budget_remaining')}")
    lines.append("")
    lines.append(f"OBJECTIVE: {repair.get('objective') or 'NONE'}")
    lines.append("CONTRACT (UNCHANGED -- implement exactly this):")
    contract = repair.get("contract") or ""
    lines.append(contract if contract else "NONE")
    lines.append("ACCEPTANCE (UNCHANGED — do not restate these):")
    for c in repair.get("acceptance_criteria") or []:
        lines.append(f"  - {c}")
    lines.append(f"WRITE_SCOPE: {', '.join(repair.get('write_scope') or []) or 'NONE'}")
    lines.append("INTERFACE (UNCHANGED -- these exact input keys are the contract):")
    iface = repair.get("interface") or []
    if iface:
        for item in iface:
            lines.append(f"  - {item}")
    else:
        lines.append("  - NONE")
    if repair.get("interface_error"):
        lines.append(f"  !! INTERFACE DECLARATION REJECTED: {repair['interface_error']}")
    if repair.get("interface_digest"):
        lines.append(f"INTERFACE_DIGEST: {repair['interface_digest']}")
    lines.append(f"EXPECTED_BEHAVIOR: {repair.get('expected_behavior')}")
    lines.append("")
    lines.append(f"FAILING_COMMAND: {repair.get('failing_command')}")
    if repair.get("failing_tests"):
        lines.append("FAILING_TESTS:")
        for t in repair["failing_tests"]:
            lines.append(f"  - {t}")
    if repair.get("failure_reasons"):
        lines.append("FAILURE_REASONS:")
        for r in repair["failure_reasons"]:
            lines.append(f"  - {r}")
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
    lines.append(repair.get("error_excerpt") or "")
    lines.append("")
    lines.append("Call the tool again with the corrected file content. Do not explain.")
    lines.append("Repair the DEFECT shown below. Do NOT change the objective, the "
                 "interface, the write scope or the acceptance criteria: those are "
                 "unchanged and are not yours to reinterpret.")
    text = "\n".join(lines)
    if len(text) > max_chars:
        marker = "\n...[CONTEXT TRUNCATED]"
        text = text[:max_chars - len(marker)] + marker
    return text
