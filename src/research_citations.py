"""Runtime wiring for advisory citation verification in deep research.

``src.citation_resolver`` is a pure interpreter: it normalises an identifier,
builds the authority URL, and reads a payload supplied by an injected
``fetcher``.  This module supplies the missing runtime half:

* it recognises which *finding* URLs are authoritative identifier links
  (``doi.org`` / ``dx.doi.org`` DOI URLs and ``arxiv.org`` abs/pdf URLs) and
  extracts the identifier from the URL path only -- never from free text;
* it performs the bounded, capped authority lookups through a small injected
  transport seam, over HTTPS to the exact public authorities
  ``api.crossref.org`` and ``export.arxiv.org``;
* it hands the parsed Crossref JSON / arXiv Atom payload back to
  ``citation_resolver.resolve_citation`` so statuses and flags stay exactly
  the ones defined there; and
* it records a serialisable advisory result on the finding
  (``citation_verification``).

Nothing here is a service or a store: one :class:`CitationVerificationSession`
lives for one research run and keeps only an in-memory lookup count/cache.

Title matching is intentionally not requested.  A finding's ``title`` comes
from the fetch/extract path and may carry site decoration ("Paper -- Example
Blog"), so comparing it to an authority title would fabricate mismatches.
The resolver is therefore called with an identifier-only :class:`CitationRef`
and returns ``TRUE`` with the core's ``EXISTENCE_ONLY`` flag when the
identifier exists.

Advisory only: a result never removes, rewrites, or promotes a finding, and
authority text (``matched_title``) is never fed to a model as instructions.

Transport contract
------------------
``transport(url: str) -> bytes | None`` (an async callable).

* ``url`` is one built by ``citation_resolver`` and is checked here against
  the exact authority allowlist before it is passed on.
* any bytes up to :data:`MAX_AUTHORITY_BYTES` are the raw response body;
  ``None`` means the authority could not answer (transport error, non-200,
  oversized body, ...).
* Cancellation must propagate: implementations should not swallow
  ``asyncio.CancelledError``.

The default transport is :func:`http_transport`, built on the project's
installed ``httpx`` with a fixed timeout, redirects disabled, and a byte cap.
"""

from __future__ import annotations

import asyncio
import json
import re
import xml.etree.ElementTree as ET
from typing import Awaitable, Callable, Dict, Optional, Tuple
from urllib.parse import unquote, urlsplit

from src.citation_resolver import (
    AUTHORITY_UNREACHABLE,
    CROSSREF_WORKS_URL,
    UNRESOLVABLE,
    CitationRef,
    CitationResult,
    normalize_arxiv,
    normalize_doi,
    resolve_citation,
)

__all__ = [
    "MAX_LOOKUPS_PER_RUN",
    "MAX_AUTHORITY_BYTES",
    "AUTHORITY_TIMEOUT_SECONDS",
    "DOI_HOSTS",
    "ARXIV_HOSTS",
    "AUTHORITY_HOSTS",
    "citation_target",
    "serialize_result",
    "CitationVerificationSession",
    "http_transport",
]

# Per-run ceiling on *actual* authority requests.  Identifier duplicates reuse
# a completed result and do not spend the budget; requests already in flight
# are not deduplicated, so two concurrent findings for the same identifier can
# each count once.  Findings beyond the ceiling get an UNRESOLVABLE
# advisory result instead of a lookup.
MAX_LOOKUPS_PER_RUN = 10

# Response bodies are small JSON / Atom documents; anything larger is treated
# as an unanswerable authority rather than buffered or parsed.
MAX_AUTHORITY_BYTES = 1_048_576

# Fixed per-request wall-clock ceiling, well under the 10s bound.
AUTHORITY_TIMEOUT_SECONDS = 10.0

DOI_HOSTS = frozenset({"doi.org", "dx.doi.org"})
ARXIV_HOSTS = frozenset({"arxiv.org", "www.arxiv.org"})
AUTHORITY_HOSTS = frozenset({"api.crossref.org", "export.arxiv.org"})

_ATOM_NS = "{http://www.w3.org/2005/Atom}"
_ATOM_FEED_TAG = _ATOM_NS + "feed"
_ATOM_ENTRY_TAG = _ATOM_NS + "entry"
_ARXIV_ERROR_ID_PREFIXES = (
    "http://arxiv.org/api/errors#",
    "https://arxiv.org/api/errors#",
)
_ARXIV_ENTRY_VERSION_RE = re.compile(r"v[0-9]+$")
_CROSSREF_MISS = b'{"status": "failed"}'


def _authority_url(kind: str, identifier: str) -> Optional[str]:
    """The authority URL ``citation_resolver`` would fetch for this identifier.

    The resolver owns URL construction; capture it through the documented
    fetcher seam instead of duplicating the encoding rules here.  The fetcher
    returns ``None`` so the throw-away call resolves as UNRESOLVABLE; only the
    captured URL is used.
    """
    captured = []

    def _capture(url: str):
        captured.append(url)
        return None

    resolve_citation(CitationRef(kind=kind, identifier=identifier), fetcher=_capture)
    return captured[0] if len(captured) == 1 else None


def _host_of(url: str) -> Tuple[Optional[str], bool]:
    """Return ``(hostname, is_plain_https)`` for an absolute URL.

    A URL carrying userinfo, an explicit port, or a non-HTTPS scheme is not a
    plain authority URL; callers use the flag to reject it.  ``hostname`` is
    lower-cased by :mod:`urllib.parse`.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return None, False
    plain = parts.scheme.lower() == "https"
    if parts.username is not None or parts.password is not None:
        plain = False
    try:
        if parts.port is not None:
            plain = False
    except ValueError:
        plain = False
    return (parts.hostname or "").lower() or None, plain


def citation_target(url) -> Optional[Tuple[str, str]]:
    """``(kind, identifier)`` when ``url`` is an authoritative citation URL.

    Exact host matching: the hostname must be exactly one of ``doi.org`` /
    ``dx.doi.org`` (path starts with ``/10.``) or ``arxiv.org`` /
    ``www.arxiv.org`` (path starts with ``/abs/`` or ``/pdf/``).  Lookalikes
    such as ``doi.org.evil.example``, userinfo tricks, explicit ports, or
    non-HTTPS schemes are rejected.  The identifier is read from the URL path
    only and must pass the resolver's normaliser; anything else yields
    ``None`` and no lookup.
    """
    if not isinstance(url, str):
        return None
    host, plain = _host_of(url.strip())
    if not plain or host is None:
        return None

    path = urlsplit(url.strip()).path

    if host in DOI_HOSTS:
        if not path.startswith("/10."):
            return None
        identifier = normalize_doi(unquote(path[1:]))
        return ("doi", identifier) if identifier is not None else None

    if host in ARXIV_HOSTS:
        for prefix in ("/abs/", "/pdf/"):
            if path.startswith(prefix):
                raw = path[len(prefix):]
                if raw.endswith("/"):
                    raw = raw[:-1]
                if raw.endswith(".pdf"):
                    raw = raw[:-4]
                identifier = normalize_arxiv(raw)
                return ("arxiv", identifier) if identifier is not None else None
        return None

    return None


def serialize_result(result: CitationResult) -> Dict:
    """The advisory, JSON-serialisable record stored on a finding."""
    return {
        "identifier": result.ref.identifier,
        "status": result.status,
        "flags": list(result.flags),
        "detail": result.detail,
        "matched_title": result.matched_title,
    }


def _is_authority_url(url: str) -> bool:
    host, plain = _host_of(url)
    return plain and host in AUTHORITY_HOSTS


def _parse_crossref(raw: bytes, requested_doi: str) -> Optional[Dict]:
    """Crossref JSON, but only when it proves the identity that was requested.

    The live API puts the registered DOI at ``message.DOI``.  A success
    message without it, with an unparseable value, or naming a different work
    is not evidence about the requested DOI: return ``None`` so the core
    resolver reports the authority as unable to answer.  A documented miss
    (``status: failed``) or a payload with no ``message`` is passed through
    unchanged so the core keeps its ``REGISTRY_NOT_COVERED`` semantics.
    """
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    # Only a JSON object carries the shape the resolver reads.
    if not isinstance(payload, dict):
        return None
    if payload.get("status") != "ok":
        # ``failed`` is the documented registry miss; anything else cannot be
        # read as a successful authority answer.
        return payload if payload.get("status") == "failed" else None
    message = payload.get("message")
    if not isinstance(message, dict):
        # Let the core resolver report the registry-miss shape it defines.
        return payload
    requested = normalize_doi(requested_doi)
    registered = normalize_doi(message.get("DOI"))
    if requested is None or registered is None or registered != requested:
        return None
    return payload


def _child_text(element, suffix: str) -> Optional[str]:
    """Text of the exact Atom child, or None.

    Only fully namespaced Atom children count: an unnamespaced or
    suffix-namespaced lookalike is not evidence about an arXiv identifier.
    """
    for child in element:
        if child.tag == _ATOM_NS + suffix:
            return " ".join((child.text or "").split())
    return None


def _arxiv_entry_identifier(entry_id: Optional[str]) -> Optional[str]:
    """The normalised arXiv id an Atom entry ``<id>`` names, or None."""
    if not isinstance(entry_id, str):
        return None
    parts = urlsplit(entry_id.strip())
    if parts.scheme.lower() not in ("http", "https"):
        return None
    if (parts.hostname or "").lower() not in ARXIV_HOSTS:
        return None
    if not parts.path.startswith("/abs/"):
        return None
    return normalize_arxiv(parts.path[len("/abs/"):].strip("/"))


def _is_arxiv_error_entry(entry_id: Optional[str], title: str) -> bool:
    if isinstance(entry_id, str) and entry_id.strip().lower().startswith(
        _ARXIV_ERROR_ID_PREFIXES
    ):
        return True
    return title.strip().casefold() == "error"


def _arxiv_identity_matches(requested: str, returned: str) -> bool:
    """Whether ``returned`` is the canonical entry for ``requested``.

    A requested version must match exactly.  An unversioned request accepts
    the canonical versioned entry for the same base identifier.
    """
    if _ARXIV_ENTRY_VERSION_RE.search(requested):
        return returned == requested
    return _ARXIV_ENTRY_VERSION_RE.sub("", returned) == requested


def _parse_arxiv(raw: bytes, requested_id: str) -> Optional[Dict]:
    """An arXiv Atom answer, reduced to the core resolver's entry shape.

    The document must be an actual Atom feed: root ``feed`` in the exact
    ``http://www.w3.org/2005/Atom`` namespace.  Anything else -- an HTML
    error page, an unnamespaced document, a suffix namespace -- is not proof
    of anything and returns ``None`` (unavailable), never an empty feed.
    A valid feed with no entries is an authoritative "no such id"
    (``{"entries": []}``).  An error entry, or an entry whose ``<id>`` does
    not match the requested identifier, is unavailable: it is not evidence
    of existence and not an authoritative miss.
    """
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError, UnicodeDecodeError):
        return None
    except Exception:
        # Any other parser failure is also "the authority could not answer".
        return None
    if root.tag != _ATOM_FEED_TAG:
        return None

    requested = normalize_arxiv(requested_id)
    if requested is None:
        return None

    entry_count = 0
    for element in root:
        if element.tag != _ATOM_ENTRY_TAG:
            continue
        entry_count += 1
        entry_id = _child_text(element, "id")
        title = _child_text(element, "title") or ""
        if _is_arxiv_error_entry(entry_id, title):
            return None
        returned = _arxiv_entry_identifier(entry_id)
        if returned is None or not _arxiv_identity_matches(requested, returned):
            continue
        authors = []
        for child in element:
            if child.tag == _ATOM_NS + "author":
                name = _child_text(child, "name")
                if name:
                    authors.append(name)
        return {"entries": [{"title": title, "authors": authors}]}

    if entry_count == 0:
        # A well-formed Atom feed with no <entry> is an authoritative
        # "no such id"; parse failures above are unreachable instead.
        return {"entries": []}
    # Entries exist but none identifies the requested work: unknown, not a miss.
    return None


def _parse_authority(kind: str, identifier: str, raw: bytes) -> Optional[Dict]:
    if len(raw) > MAX_AUTHORITY_BYTES:
        return None
    if kind == "doi":
        return _parse_crossref(raw, identifier)
    if kind == "arxiv":
        return _parse_arxiv(raw, identifier)
    return None


async def http_transport(url: str) -> Optional[bytes]:
    """Default transport: HTTPS GET with a fixed timeout, no redirects, capped body.

    Refuses any URL outside the exact authority allowlist.  A Crossref 404 is
    a registry miss, not an unreachable authority, so it is reported as the
    resolver's miss shape (``{"status": "failed"}``) and yields
    ``REGISTRY_NOT_COVERED``.  Every other non-200, timeout, or transport
    exception yields ``None`` -> ``UNRESOLVABLE``.
    """
    if not _is_authority_url(url):
        return None

    async def _request() -> Optional[bytes]:
        import httpx

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(AUTHORITY_TIMEOUT_SECONDS),
            follow_redirects=False,
        ) as client:
            async with client.stream(
                "GET",
                url,
                headers={"Accept": "application/json, application/atom+xml;q=0.9, application/xml;q=0.8"},
            ) as response:
                if response.status_code == 404 and url.startswith(CROSSREF_WORKS_URL):
                    return _CROSSREF_MISS
                if response.status_code != 200:
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAX_AUTHORITY_BYTES:
                        return None
                    body.extend(chunk)
                return bytes(body)

    try:
        return await asyncio.wait_for(_request(), timeout=AUTHORITY_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        return None


class CitationVerificationSession:
    """Per-run advisory verification for findings, with a capped lookup budget.

    ``transport`` is the injectable seam described in the module docstring;
    tests pass a stub and make no network call.  When omitted the real HTTP
    transport is used.
    """

    def __init__(
        self,
        transport: Optional[Callable[[str], Awaitable[Optional[bytes]]]] = None,
        *,
        max_lookups: int = MAX_LOOKUPS_PER_RUN,
    ):
        self._transport = transport if transport is not None else http_transport
        # The per-run ceiling is a public bound: a caller may lower it, never
        # raise it, so a custom request cannot widen the lookup budget.
        self.max_lookups = min(MAX_LOOKUPS_PER_RUN, max(0, int(max_lookups)))
        self.lookups = 0
        self._cache: Dict[Tuple[str, str], Dict] = {}

    async def annotate(self, finding: Dict) -> Optional[Dict]:
        """Attach ``finding["citation_verification"]`` when the URL qualifies.

        Non-authoritative URLs are left completely untouched (no key, no
        lookup).  Returns the stored record, or ``None`` when nothing applied.
        """
        if not isinstance(finding, dict):
            return None
        target = citation_target(finding.get("url"))
        if target is None:
            return None
        record = await self._resolve(*target)
        finding["citation_verification"] = record
        return record

    async def _resolve(self, kind: str, identifier: str) -> Dict:
        key = (kind, identifier)
        cached = self._cache.get(key)
        if cached is not None:
            return dict(cached, flags=list(cached.get("flags") or []))

        if self.lookups >= self.max_lookups:
            record = serialize_result(CitationResult(
                CitationRef(kind=kind, identifier=identifier),
                UNRESOLVABLE,
                [AUTHORITY_UNREACHABLE],
                f"per-run authority lookup cap ({self.max_lookups}) reached; no lookup performed",
            ))
            self._cache[key] = record
            return dict(record, flags=list(record["flags"]))

        url = _authority_url(kind, identifier)
        payload = None
        if url is not None:
            # Reserve the budget before awaiting so concurrent findings cannot
            # overshoot the cap.
            self.lookups += 1
            payload = await self._fetch(kind, identifier, url)

        # The resolver remains the only place that interprets statuses and
        # flags; the transport's answer enters through its documented seam.
        result = resolve_citation(
            CitationRef(kind=kind, identifier=identifier),
            fetcher=lambda _url: payload,
        )
        record = serialize_result(result)
        self._cache[key] = record
        return dict(record, flags=list(record["flags"]))

    async def _fetch(self, kind: str, identifier: str, url: str) -> Optional[Dict]:
        if not _is_authority_url(url):
            return None
        try:
            raw = await self._transport(url)
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        if not isinstance(raw, (bytes, bytearray)):
            return None
        try:
            return _parse_authority(kind, identifier, bytes(raw))
        except asyncio.CancelledError:
            raise
        except Exception:
            # A malformed authority response is "unknown", never an exception
            # that could tear down the caller's extraction.
            return None
