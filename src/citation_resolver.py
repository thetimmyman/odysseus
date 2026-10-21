"""Citation identifier resolver for research sources.

Answers, for a cited DOI or arXiv identifier (a :class:`CitationRef`): is the
identifier well formed, does it exist at an authority, and does the cited
title / first-author surname match what the authority reports?

Stdlib only.  This module makes **no network call of its own**: the caller
injects a ``fetcher`` callable and this module only builds the authority URL
and interprets the already-parsed JSON it returns.  Nothing here imports a
network client or reads an environment variable, and the module is dormant --
no other module calls it.  Authority text is untrusted data: it is compared
(whitespace-collapsed, case-insensitive) and echoed only in truncated form,
never executed or interpreted.

Fetcher contract
----------------
``fetcher(url: str) -> dict | None``

* The URL is one this module builds:

  - DOI:   ``https://api.crossref.org/works/<percent-encoded doi>``
  - arXiv: ``https://export.arxiv.org/api/query?id_list=<id>``

* It returns the parsed JSON of the authority response, or ``None`` when the
  authority could not be reached.  The caller's fetcher is responsible for
  turning Crossref JSON and the arXiv Atom feed into these shapes:

  - Crossref: ``{"status": "ok", "message": {"title": [str],
    "author": [{"family": str}]}}`` -- a miss is ``{"status": "failed"}`` or
    a payload with no ``message``.
  - arXiv: ``{"entries": [{"title": str, "authors": [str full names]}]}``
    (the caller parses Atom into this shape) -- a miss is ``entries`` empty.

Statuses (see :class:`CitationResult`), with the same vocabulary as
``src/pinned_source.py``:

* ``TRUE``         an authority answered and confirmed the identifier (and
                   the supplied title / author, when any).
* ``FALSE``        an authority authoritatively said "no", the supplied
                   metadata does not match, or the identifier was malformed
                   and rejected before any lookup.
* ``UNRESOLVABLE`` no authority could answer (no fetcher, the fetcher raised
                   or returned ``None``), the authority confirmed the
                   identifier but returned none of the title / authors the
                   citation asks to be checked (``METADATA_UNAVAILABLE``), or
                   Crossref has no record of a DOI, which may be registered
                   with another agency (``REGISTRY_NOT_COVERED``).  A
                   no-answer is never ``FALSE``.
* ``FALSE`` for a missing arXiv id: arXiv is the sole registry for its own
                   identifiers (``NOT_FOUND``).
* ``NOT_SEARCHED`` no lookup was performed at all (provider disabled).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

__all__ = [
    "TRUE",
    "FALSE",
    "UNRESOLVABLE",
    "NOT_SEARCHED",
    "PROVIDER_DISABLED",
    "MALFORMED_ID",
    "NO_FETCHER",
    "AUTHORITY_UNREACHABLE",
    "NOT_FOUND",
    "REGISTRY_NOT_COVERED",
    "TITLE_MISMATCH",
    "AUTHOR_MISMATCH",
    "EXISTENCE_ONLY",
    "METADATA_UNAVAILABLE",
    "CitationRef",
    "CitationResult",
    "normalize_doi",
    "normalize_arxiv",
    "resolve_citation",
    "resolve_citations",
]

# --- Result statuses -------------------------------------------------------
TRUE = "TRUE"
FALSE = "FALSE"
UNRESOLVABLE = "UNRESOLVABLE"
NOT_SEARCHED = "NOT_SEARCHED"

# --- Flags -----------------------------------------------------------------
PROVIDER_DISABLED = "PROVIDER_DISABLED"
MALFORMED_ID = "MALFORMED_ID"
NO_FETCHER = "NO_FETCHER"
AUTHORITY_UNREACHABLE = "AUTHORITY_UNREACHABLE"
NOT_FOUND = "NOT_FOUND"
REGISTRY_NOT_COVERED = "REGISTRY_NOT_COVERED"
TITLE_MISMATCH = "TITLE_MISMATCH"
AUTHOR_MISMATCH = "AUTHOR_MISMATCH"
EXISTENCE_ONLY = "EXISTENCE_ONLY"
METADATA_UNAVAILABLE = "METADATA_UNAVAILABLE"

DOI_PREFIXES = ("https://doi.org/", "http://dx.doi.org/", "doi:")
CROSSREF_WORKS_URL = "https://api.crossref.org/works/"
ARXIV_QUERY_URL = "https://export.arxiv.org/api/query?id_list="

_DOI_RE = re.compile(r"10\.[0-9]{4,9}/[!-~]+")
_ARXIV_NEW_RE = re.compile(
    r"(?:(?:07(?:0[4-9]|1[0-2])|0[89](?:0[1-9]|1[0-2])|1[0-4](?:0[1-9]|1[0-2]))\.[0-9]{4}"
    r"|(?:1[5-9]|[2-9][0-9])(?:0[1-9]|1[0-2])\.[0-9]{5})"
    r"(?:v[1-9][0-9]*)?"
)
_ARXIV_OLD_RE = re.compile(
    r"[a-z][a-z-]*(?:\.[a-z]{2})?/[0-9]{2}(?:0[1-9]|1[0-2])[0-9]{3}"
    r"(?:v[1-9][0-9]*)?"
)
_WS_RE = re.compile(r"\s+")

# Echoed authority text is untrusted: never repeat more than this in detail.
MAX_DETAIL_TEXT = 200


@dataclass(frozen=True)
class CitationRef:
    """A cited research source (untrusted input; validated on resolve).

    ``kind``: ``doi`` | ``arxiv``.
    ``identifier``: the identifier exactly as cited.
    ``title``: optional title claimed by the citation.
    ``first_author_surname``: optional first-author surname claimed by the
    citation.
    """

    kind: str
    identifier: str
    title: Optional[str] = None
    first_author_surname: Optional[str] = None

    def __post_init__(self) -> None:
        for name in ("kind", "identifier", "title", "first_author_surname"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or None")


@dataclass(frozen=True)
class CitationResult:
    """Outcome of resolving one citation.

    ``status`` is one of the status constants; ``flags`` names every reason
    that applies (see the flag constants); ``detail`` is a short human
    explanation with any echoed authority text clipped;
    ``matched_title`` is the authority title when one was read.
    """

    ref: CitationRef
    status: str
    flags: List[str] = field(default_factory=list)
    detail: str = ""
    matched_title: Optional[str] = None


def normalize_doi(s: str) -> Optional[str]:
    """Normalise a DOI to lowercase ``10.NNNN/suffix``, or None if malformed.

    Accepts an optional ``https://doi.org/`` / ``http://dx.doi.org/`` /
    ``doi:`` prefix (case-insensitive) and surrounding whitespace.  The
    registrant prefix must be ``10.`` plus 4-9 digits and the suffix must be
    non-empty with no whitespace.
    """
    if not isinstance(s, str):
        return None
    value = s.strip()
    lowered = value.lower()
    for prefix in DOI_PREFIXES:
        if lowered.startswith(prefix):
            value = value[len(prefix):].strip()
            break
    value = value.lower()
    if not _DOI_RE.fullmatch(value):
        return None
    return value


def normalize_arxiv(s: str) -> Optional[str]:
    """Normalise an arXiv id, or None if malformed.

    Accepts new style ``YYMM.NNNN`` / ``YYMM.NNNNN`` with an optional
    ``vN``, and old style ``category/YYMMNNN`` (``hep-th/9901001``), each
    with an optional ``arXiv:`` prefix.  Returns the id with the prefix
    stripped and lowercased.  Syntax is ASCII-only, the ``YYMM`` month must
    be 01..12, new-style ids start at 0704 (April 2007), a new-style sequence is four digits before 1501 and five from
    1501 on, and a version suffix is ``v1`` or higher with no leading zero.
    """
    if not isinstance(s, str):
        return None
    value = s.strip()
    if value.lower().startswith("arxiv:"):
        value = value[len("arxiv:"):].strip()
    value = value.lower()
    if _ARXIV_NEW_RE.fullmatch(value) or _ARXIV_OLD_RE.fullmatch(value):
        return value
    return None


def _percent_encode(value: str) -> str:
    """Percent-encode a DOI for a URL path (unreserved chars stay as-is)."""
    out = []
    for byte in value.encode("utf-8"):
        char = chr(byte)
        if char.isascii() and (char.isalnum() or char in "-._~/"):
            out.append(char)
        else:
            out.append("%%%02X" % byte)
    return "".join(out)


def _clip(text: str) -> str:
    """Bound untrusted text echoed in ``detail``."""
    return text[:MAX_DETAIL_TEXT]


def _norm_text(text: str) -> str:
    """Whitespace-collapsed, case-insensitive comparison form."""
    return _WS_RE.sub(" ", text).strip().casefold()


def _first_str(values) -> Optional[str]:
    if isinstance(values, list):
        for value in values:
            if isinstance(value, str) and value.strip():
                return value
    return None


def _read_authority(kind: str, payload: dict):
    """Extract ``(title, authors, full_names)`` or None for a "not found".

    ``full_names`` says whether ``authors`` are full names (arXiv) rather
    than surnames (Crossref ``family``).
    """
    if kind == "doi":
        if payload.get("status") == "failed":
            return None
        message = payload.get("message")
        if not isinstance(message, dict):
            return None
        title = _first_str(message.get("title"))
        authors = []
        raw_authors = message.get("author")
        if isinstance(raw_authors, list):
            for author in raw_authors:
                if isinstance(author, dict) and isinstance(author.get("family"), str):
                    authors.append(author["family"])
        return title, authors, False

    entries = payload.get("entries")
    if not isinstance(entries, list) or not entries:
        return None
    entry = entries[0]
    if not isinstance(entry, dict):
        return None
    title = entry.get("title") if isinstance(entry.get("title"), str) else None
    authors = [name for name in entry.get("authors") or [] if isinstance(name, str)]
    return title, authors, True


def _author_matches(surname: str, authors: List[str], full_names: bool) -> bool:
    target = _norm_text(surname)
    for author in authors:
        name = _norm_text(author)
        if target == name:
            return True
        if full_names and target in name.split():
            return True
    return False


def resolve_citation(
    ref: CitationRef,
    *,
    fetcher: Optional[Callable] = None,
    enabled: bool = True,
) -> CitationResult:
    """Resolve one citation.

    ``fetcher``: ``fetcher(url) -> dict | None`` (see the module docstring);
    ``enabled``: provider switch; when False no lookup is performed.

    Order: provider switch, then normalise the identifier (a malformed one is
    rejected before any lookup), then the fetcher, then interpret its answer.
    """
    if not enabled:
        return CitationResult(
            ref,
            NOT_SEARCHED,
            [PROVIDER_DISABLED],
            "citation provider disabled; no lookup performed",
        )

    if ref.kind == "doi":
        identifier = normalize_doi(ref.identifier)
        url = CROSSREF_WORKS_URL + _percent_encode(identifier) if identifier else None
    elif ref.kind == "arxiv":
        identifier = normalize_arxiv(ref.identifier)
        url = ARXIV_QUERY_URL + identifier if identifier else None
    else:
        return CitationResult(
            ref,
            FALSE,
            [MALFORMED_ID],
            f"unknown citation kind {_clip(str(ref.kind))!r}",
        )

    if identifier is None or url is None:
        return CitationResult(
            ref,
            FALSE,
            [MALFORMED_ID],
            f"identifier {_clip(str(ref.identifier))!r} is not a well-formed {ref.kind} id",
        )

    if fetcher is None:
        return CitationResult(
            ref,
            UNRESOLVABLE,
            [NO_FETCHER],
            "no fetcher available to reach an authority",
        )

    try:
        payload = fetcher(url)
    except Exception as exc:
        return CitationResult(
            ref,
            UNRESOLVABLE,
            [AUTHORITY_UNREACHABLE],
            f"authority lookup raised {type(exc).__name__}",
        )

    if payload is None:
        return CitationResult(
            ref,
            UNRESOLVABLE,
            [AUTHORITY_UNREACHABLE],
            "authority could not be reached (fetcher returned None)",
        )
    if not isinstance(payload, dict):
        return CitationResult(
            ref,
            UNRESOLVABLE,
            [AUTHORITY_UNREACHABLE],
            "authority returned an unusable payload",
        )

    extracted = _read_authority(ref.kind, payload)
    if extracted is None:
        if ref.kind == "doi":
            # Crossref /works only indexes DOIs registered with Crossref.
            # A miss does not prove the DOI does not exist: it may be
            # registered with another agency such as DataCite.
            return CitationResult(
                ref,
                UNRESOLVABLE,
                [REGISTRY_NOT_COVERED],
                "Crossref has no record; the DOI may be registered with another agency",
            )
        return CitationResult(
            ref,
            FALSE,
            [NOT_FOUND],
            "authority reports the identifier does not exist",
        )

    title, authors, full_names = extracted

    # Compare every field the authority did return before treating a missing
    # field as a no-answer: a returned field that contradicts the citation is
    # a definitive FALSE, whatever the authority omitted.
    if ref.title is not None and title is not None:
        if _norm_text(ref.title) != _norm_text(title):
            return CitationResult(
                ref,
                FALSE,
                [TITLE_MISMATCH],
                f"cited title does not match authority title {_clip(title)!r}",
                title,
            )

    if ref.first_author_surname is not None and authors:
        if not _author_matches(ref.first_author_surname, authors, full_names):
            return CitationResult(
                ref,
                FALSE,
                [AUTHOR_MISMATCH],
                "cited first-author surname is not among the authority authors",
                title,
            )

    # Nothing returned disproved the citation, but a requested field could not
    # be checked: that is a no-answer, never TRUE and never FALSE.
    if (ref.title is not None and title is None) or (
        ref.first_author_surname is not None and not authors
    ):
        return CitationResult(
            ref,
            UNRESOLVABLE,
            [METADATA_UNAVAILABLE],
            "authority confirms the identifier but returned no metadata to compare",
            title,
        )

    if ref.title is None and ref.first_author_surname is None:
        return CitationResult(
            ref,
            TRUE,
            [EXISTENCE_ONLY],
            "identifier exists; no title or author was supplied to check",
            title,
        )

    return CitationResult(
        ref,
        TRUE,
        [],
        "identifier exists and the supplied metadata matches",
        title,
    )


def resolve_citations(refs: list, **kwargs) -> list:
    """Resolve many citations, preserving input order.

    Keyword arguments pass through to :func:`resolve_citation` unchanged.
    """
    return [resolve_citation(ref, **kwargs) for ref in refs]
