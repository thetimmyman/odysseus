"""Evaluation mode: a DRAFT ruling skeleton, its validator and its renderer.

Evaluation mode answers "should TMOS adopt, adapt, reference, build, reject or
defer X".  Its output is a DRAFT ruling in the house format; a human accepts
it.  This module holds only the deterministic pieces -- no model call, no
network access, no filesystem reads:

* :func:`new_ruling_skeleton` starts every house-format section at
  ``UNKNOWN`` and leaves the verdict unset;
* :func:`fill_section` records text only when it comes with evidence refs;
* :func:`find_incumbent_overlap` looks for an existing owner before anything
  new is proposed (the search is injected, so this module never shells out to
  ``tools/tmos`` and never touches the network);
* :func:`validate_ruling` returns the draft's problems (empty list = valid);
* :func:`render_ruling_markdown` renders the draft, statuses and all.

A section carries exactly one status:

* ``FILLED``  -- text plus at least one evidence ref;
* ``UNKNOWN`` -- evidence was looked for and could not answer (the search
  failed, or returned something unusable);
* ``SKIPPED`` -- not attempted.

Nothing here is filled by synthesis: unevidenced text never becomes
``FILLED``, and a search that fails says so instead of looking filled.  A
search that ran and found nothing is not a failure: it is evidence of absence,
so it fills the overlap section with :data:`NO_MATCH_SECTION_TEXT` and
:data:`NO_MATCH_SECTION_REF`, while a search that raised stays ``UNKNOWN``.

Stdlib only.  The one first-party import is the pin-result vocabulary
(``TRUE`` and the ``UNPINNED`` flag) from :mod:`src.pinned_source`, so the
ruling validator and the pin resolver share one vocabulary instead of a
second copy of the strings.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Optional

from src.pinned_source import TRUE, UNPINNED

__all__ = [
    "RULING_SECTIONS",
    "RULING_VERDICTS",
    "FILLED",
    "UNKNOWN",
    "SKIPPED",
    "SECTION_STATUSES",
    "DRAFT_FOOTER",
    "NO_MATCH_SECTION_TEXT",
    "NO_MATCH_SECTION_REF",
    "RulingSection",
    "RulingDraft",
    "IncumbentOverlap",
    "new_ruling_skeleton",
    "fill_section",
    "find_incumbent_overlap",
    "validate_ruling",
    "render_ruling_markdown",
]

# --- House format ------------------------------------------------------------
# Ordered: this is the order a ruling is rendered in.
RULING_SECTIONS = (
    "ruling",
    "pinned_source_identity",
    "licence",
    "incumbent_overlap",
    "integration_cost",
    "results_and_controls",
    "authority_reconsideration_removal",
    "reproduction",
)

RULING_VERDICTS = ("adopt", "adapt", "reference", "build", "reject", "defer")

# Verdicts that propose something new: nothing may be proposed before an
# existing owner was searched for, or without an identity to pin.
_PROPOSAL_VERDICTS = ("adopt", "adapt", "build")
# Verdicts whose claim rests on a pinned primary source.
_PINNED_VERDICTS = ("adopt", "adapt")

# --- Section statuses --------------------------------------------------------
FILLED = "FILLED"
UNKNOWN = "UNKNOWN"
SKIPPED = "SKIPPED"
SECTION_STATUSES = (FILLED, UNKNOWN, SKIPPED)

DRAFT_FOOTER = "DRAFT: a human must accept this ruling."

# A search that ran and found no existing owner.  This is evidence of absence,
# not a failure, so it fills the overlap section -- with a ref that says which
# search produced it, never a match's name.
NO_MATCH_SECTION_TEXT = "Searched for an existing owner; none found."
NO_MATCH_SECTION_REF = "overlap-search:no-match"


# The section a pin result's identity is cited by, unless the result names one.
_DEFAULT_PIN_SECTION = "pinned_source_identity"


@dataclass(frozen=True)
class RulingSection:
    """One house-format section: its status, its text and its evidence refs.

    ``text`` is only meaningful while ``status`` is ``FILLED``; the other
    statuses render as the status word alone.
    """

    status: str = UNKNOWN
    text: str = ""
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in SECTION_STATUSES:
            raise ValueError(
                f"unknown section status {self.status!r}; expected one of: "
                f"{', '.join(SECTION_STATUSES)}"
            )


@dataclass(frozen=True)
class RulingDraft:
    """A draft ruling: the candidate, the question, a verdict and the sections.

    ``verdict`` is ``None`` until someone states one (see :meth:`with_verdict`).
    ``sections`` is an immutable name-keyed mapping; every function in this
    module returns a NEW draft rather than editing one in place.
    """

    candidate: str
    question: str
    verdict: Optional[str] = None
    sections: Mapping[str, RulingSection] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A read-only view: a draft handed to a caller cannot grow a filled
        # section behind the validator's back.
        object.__setattr__(self, "sections", MappingProxyType(dict(self.sections)))

    def section(self, name: str) -> RulingSection:
        """The named section; ``ValueError`` for a name outside the house format."""
        _require_section(name)
        return self.sections.get(name) or RulingSection()

    def with_verdict(self, verdict: Optional[str]) -> "RulingDraft":
        """A NEW draft with ``verdict`` set (``None`` clears it)."""
        return replace(self, verdict=verdict)


@dataclass(frozen=True)
class IncumbentOverlap:
    """Result of looking for an existing owner of a candidate.

    ``matches`` are the entries the injected search returned, in order (each a
    dict with ``name``, ``owner`` and ``source``).  A search that failed is
    ``UNKNOWN`` with the exception CLASS name in ``detail``: the exception
    message is not repeated, because it may carry private text.  A search that
    completed and found nothing is ``FILLED`` with no ``matches`` and
    :data:`NO_MATCH_SECTION_TEXT` in ``detail`` -- an answer, distinct from a
    failure.
    """

    status: str
    matches: tuple[Mapping[str, str], ...] = ()
    detail: str = ""

    def __post_init__(self) -> None:
        if self.status not in SECTION_STATUSES:
            raise ValueError(
                f"unknown overlap status {self.status!r}; expected one of: "
                f"{', '.join(SECTION_STATUSES)}"
            )

    @property
    def section_text(self) -> str:
        """Text for the ``incumbent_overlap`` section (see :func:`fill_section`)."""
        if not self.matches:
            return self.detail
        lines = ["Incumbent overlap found:"]
        lines.extend(f"- {_format_match(match)}" for match in self.matches)
        return "\n".join(lines)

    @property
    def section_refs(self) -> tuple[str, ...]:
        """Evidence refs for the ``incumbent_overlap`` section.

        Only a match's non-blank ``source`` is evidence: a match without one
        supplies no ref, so :func:`fill_section` leaves the section
        ``UNKNOWN`` instead of citing a bare name.  A completed no-match
        search cites :data:`NO_MATCH_SECTION_REF`; a failure cites nothing.
        """
        if not self.matches:
            return (NO_MATCH_SECTION_REF,) if self.status == FILLED else ()
        refs: list[str] = []
        for match in self.matches:
            ref = _match_field(match, "source")
            if ref and ref.strip():
                refs.append(ref.strip())
        return tuple(refs)


def _require_section(name: str) -> None:
    """Raise ``ValueError`` for a section name outside :data:`RULING_SECTIONS`."""
    if name not in RULING_SECTIONS:
        raise ValueError(
            f"unknown ruling section {name!r}; expected one of: "
            f"{', '.join(RULING_SECTIONS)}"
        )


def _clean_refs(evidence_refs) -> tuple[str, ...]:
    """Evidence refs as a tuple of non-blank strings (a lone string is one ref).

    A non-string is not evidence: it is dropped, never stringified, so
    ``None``, ``0`` or a mapping cannot be cited as though it were a source.
    """
    if evidence_refs is None:
        return ()
    if isinstance(evidence_refs, str):
        evidence_refs = (evidence_refs,)
    try:
        items = list(evidence_refs)
    except TypeError:
        return ()
    refs: list[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        ref = item.strip()
        if ref:
            refs.append(ref)
    return tuple(refs)


def _match_field(match, key: str) -> Optional[str]:
    """A string field of a search match, or ``None`` when absent/not a string."""
    if not isinstance(match, Mapping):
        return None
    value = match.get(key)
    return value if isinstance(value, str) else None


def _format_match(match) -> str:
    """One deterministic line per incumbent match."""
    if not isinstance(match, Mapping):
        return str(match)
    name = _match_field(match, "name") or "unnamed"
    owner = _match_field(match, "owner") or "unknown owner"
    source = _match_field(match, "source") or "unspecified source"
    return f"{name} (owner: {owner}, source: {source})"


# --- Public API --------------------------------------------------------------
def new_ruling_skeleton(candidate: str, question: str) -> RulingDraft:
    """A DRAFT ruling for ``candidate``: every section ``UNKNOWN``, no verdict.

    ``candidate`` and ``question`` are stored verbatim.  A skeleton states
    nothing: an empty section is a question for the evidence, not a claim.
    """
    return RulingDraft(
        candidate=candidate,
        question=question,
        verdict=None,
        sections={name: RulingSection() for name in RULING_SECTIONS},
    )


def fill_section(
    draft: RulingDraft,
    name: str,
    text: str,
    evidence_refs: Sequence[str] = (),
) -> RulingDraft:
    """Record ``text`` for section ``name``, but only with evidence behind it.

    ``FILLED`` requires both non-blank ``text`` and at least one evidence ref.
    The other outcomes are deliberate and documented:

    * a name outside :data:`RULING_SECTIONS` -> ``ValueError``;
    * blank text (empty or whitespace only) -> ``ValueError``;
    * no evidence refs -> the section is ``UNKNOWN`` and the text is NOT
      stored.  Unevidenced text never stands, so there is nothing to render:
      an empty section is a question, not a claim.  Only non-blank strings
      are evidence: a non-string ref is dropped, never stringified, and a
      mixed sequence keeps just its real refs, in order.

    Returns a NEW draft; ``draft`` is never mutated.
    """
    _require_section(name)
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"section {name!r} needs non-blank text")
    refs = _clean_refs(evidence_refs)
    filled = RulingSection(FILLED, text, refs) if refs else RulingSection(UNKNOWN)
    sections = dict(draft.sections)
    sections[name] = filled
    return replace(draft, sections=sections)


def find_incumbent_overlap(
    candidate: str,
    search: Optional[Callable[[str], list]] = None,
) -> IncumbentOverlap:
    """Look for an existing owner before anything new is proposed.

    ``search`` is injected: ``(candidate) -> [{"name", "owner", "source"}, ...]``.
    With ``search=None`` nothing is attempted and the result is ``SKIPPED`` --
    this module never shells out to ``tools/tmos`` and never touches the
    network.

    * matches returned -> ``FILLED``; :attr:`IncumbentOverlap.section_text`
      lists each one (pass it, with :attr:`IncumbentOverlap.section_refs`, to
      :func:`fill_section`);
    * an empty list -> ``FILLED``: the search COMPLETED and found no existing
      owner, which is evidence of absence.  The text is
      :data:`NO_MATCH_SECTION_TEXT` and the single ref is
      :data:`NO_MATCH_SECTION_REF`, so a ``build`` may proceed past the
      overlap gate;
    * the search raising -> ``UNKNOWN`` carrying the exception CLASS name in
      ``detail``.  The message is not repeated (it may carry private text),
      but the failure is never swallowed and never reported as ``FILLED``, so
      it is never an all-clear;
    * a result that is not a list -> ``UNKNOWN`` naming its type.
    """
    if search is None:
        return IncumbentOverlap(SKIPPED, (), "no overlap search was attempted")
    try:
        found = search(candidate)
    except Exception as exc:
        # Any failure is a no-answer, not an all-clear: name the class.
        return IncumbentOverlap(UNKNOWN, (), f"overlap search raised {type(exc).__name__}")
    if not isinstance(found, list):
        return IncumbentOverlap(
            UNKNOWN, (), f"overlap search returned {type(found).__name__}, not a list"
        )
    matches = tuple(found)
    if not matches:
        return IncumbentOverlap(FILLED, (), NO_MATCH_SECTION_TEXT)
    return IncumbentOverlap(FILLED, matches, "")


def _status(draft: RulingDraft, name: str) -> str:
    """Status of a section; a section missing from the draft counts as ``UNKNOWN``."""
    section = draft.sections.get(name)
    return section.status if section is not None else UNKNOWN


def _pin_section(pin) -> str:
    """The section that cites ``pin``: its ``section`` attribute, or the default."""
    name = getattr(pin, "section", None)
    if isinstance(name, str) and name in RULING_SECTIONS:
        return name
    return _DEFAULT_PIN_SECTION


def validate_ruling(
    draft: RulingDraft,
    pin_results: Optional[Sequence] = None,
) -> list[str]:
    """Problems with ``draft``; an empty list means it may go to a human.

    Rules, each with its own message:

    1. a verdict set but not in :data:`RULING_VERDICTS`;
    2. ``adopt`` / ``adapt`` / ``build`` while ``incumbent_overlap`` is
       ``UNKNOWN`` or ``SKIPPED``: nothing may be proposed before an existing
       owner was searched for;
    3. ``adopt`` / ``adapt`` while ``pinned_source_identity`` is not
       ``FILLED``: the identity of the source being adopted must be pinned;
    4. a ``FILLED`` section with no evidence refs (only a non-blank string
       ref counts; a blank or non-string ref is not evidence);
    5. a ``FILLED`` section with blank text;
    6. a pin result whose ``status`` is not ``TRUE`` while the section that
       cites it is ``FILLED``;
    7. a pin result carrying the ``UNPINNED`` flag while the section that
       cites it is ``FILLED``.

    ``pin_results`` are duck-typed: objects with a ``flags`` list and a
    ``status``.  Only ``status == TRUE`` confirms a citation, so ``FALSE``,
    ``UNRESOLVABLE``, ``NOT_SEARCHED`` and a missing status are all problems:
    the status appears in the message.  The ``UNPINNED`` flag is reported even
    for a ``TRUE`` result -- an unpinned citation cannot back a filled section
    whatever it resolved to.  A pin result's ``section`` attribute names the
    section citing it; without one it is ``pinned_source_identity``.  A
    section missing from the draft counts as ``UNKNOWN``.
    """
    problems: list[str] = []

    # 1) A verdict outside the house list (including a wrong case).
    verdict = draft.verdict
    if verdict is not None and verdict not in RULING_VERDICTS:
        problems.append(
            f"verdict {verdict!r} is not a house verdict; expected one of: "
            f"{', '.join(RULING_VERDICTS)}"
        )

    # 2) A proposal with no incumbent-overlap search behind it.
    if verdict in _PROPOSAL_VERDICTS:
        overlap_status = _status(draft, "incumbent_overlap")
        if overlap_status in (UNKNOWN, SKIPPED):
            problems.append(
                f"verdict {verdict!r} cannot be proposed before an existing owner "
                f"was searched for; section 'incumbent_overlap' is {overlap_status}"
            )

    # 3) An adoption whose source identity is not pinned.
    if verdict in _PINNED_VERDICTS:
        identity_status = _status(draft, "pinned_source_identity")
        if identity_status != FILLED:
            problems.append(
                f"verdict {verdict!r} needs a pinned source identity; section "
                f"'pinned_source_identity' is {identity_status}"
            )

    # 4) A filled section with nothing behind it.  Only a non-blank string is
    #    evidence; a blank or non-string ref does not back a claim.
    for name in RULING_SECTIONS:
        section = draft.sections.get(name)
        if (
            section is not None
            and section.status == FILLED
            and not _clean_refs(section.evidence_refs)
        ):
            problems.append(f"section {name!r} is FILLED without evidence refs")

    # 5) A filled section with nothing to say (a section built directly, not
    #    through fill_section, can carry blank text behind a FILLED status).
    for name in RULING_SECTIONS:
        section = draft.sections.get(name)
        if section is None or section.status != FILLED:
            continue
        text = section.text if isinstance(section.text, str) else ""
        if not text.strip():
            problems.append(f"section {name!r} is FILLED with blank text")

    # 6/7) A pin result cited by a filled section that does not back it.
    for pin in pin_results or ():
        name = _pin_section(pin)
        if _status(draft, name) != FILLED:
            continue
        status = getattr(pin, "status", None)
        flags = getattr(pin, "flags", None) or ()
        if status != TRUE:
            label = status if isinstance(status, str) and status else "no status"
            problems.append(
                f"section {name!r} is FILLED but cites a pin result that is not "
                f"{TRUE} ({label})"
            )
        elif UNPINNED in flags:
            problems.append(
                f"section {name!r} is FILLED but cites an UNPINNED pin result ({status})"
            )

    return problems


def render_ruling_markdown(draft: RulingDraft) -> str:
    """Render the draft in house format, deterministically and without filler.

    The first line is the bold ruling line, then the sections in
    :data:`RULING_SECTIONS` order, then :data:`DRAFT_FOOTER`.  ``UNKNOWN`` and
    ``SKIPPED`` sections print exactly that word: a draft never invents text to
    fill a gap.  The same draft always renders byte-identically.
    """
    verdict = draft.verdict if draft.verdict else UNKNOWN
    lines = [
        f"**Ruling: {verdict}**",
        "",
        f"Candidate: {draft.candidate}",
        f"Question: {draft.question}",
        "",
    ]
    for name in RULING_SECTIONS:
        section = draft.sections.get(name) or RulingSection()
        lines.append(f"## {name}")
        lines.append("")
        if section.status == FILLED:
            lines.append(section.text)
            lines.append("")
            lines.append("Evidence: " + ", ".join(section.evidence_refs))
        else:
            lines.append(section.status)
        lines.append("")
    lines.append(DRAFT_FOOTER)
    return "\n".join(lines) + "\n"
