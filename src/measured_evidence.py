"""Measured evidence provider: our own benchmark/receipt files as sources.

Deep research may cite evidence we produced ourselves (benchmark results,
evidence receipts kept as files) and must REPORT - never silently resolve - a
conflict between a vendor claim and a measurement. Sources fall into three
classes, in rank order:

  1. ``measured`` (``rank`` 1, ``trust_class`` "authoritative"): results measured
     on our own hardware, kept under ``evidence/**`` or
     ``benchmark/<name>/results/**``.
  2. pinned primary sources (future work; ``src/pinned_source.py``).
  3. secondary sources.

This module builds only the measured class and the vendor-vs-measurement conflict
check, as a standalone unit that later work wires into the research loop.

Measurement convention
----------------------
A JSON evidence file may carry a top-level ``"measurements"`` list::

    {"measurements": [
        {"subject": "GPU decode", "metric": "throughput",
         "value": 12.0, "unit": "tok/s"}
    ]}

:func:`extract_measurements` reads only that convention from a source's text;
JSON or plain-text files without it simply have no measurements.

File text is untrusted data: it is hashed and returned, never executed or
interpreted. There is no network access, no environment reads, and no
module-level mutable state.
"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import stat
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MEASURED_RANK = 1
TRUST_CLASS = "authoritative"
SOURCE_KIND = "measured"
MAX_TEXT_BYTES = 65536
_MAX_FILES_SCANNED = 5000
_MAX_QUERY_CHARS = 2000
_MAX_QUERY_TOKENS = 32
_MAX_MEASUREMENTS_PER_FILE = 1000  # structured entries kept per source
_MAX_ENTRIES_WALKED = 20000  # files and directories listed, whatever their type
_MAX_BYTES_SCANNED = 256 * 1024 * 1024

OK = "OK"
NONE = "NONE"
NOT_SEARCHED = "NOT_SEARCHED"
UNAVAILABLE = "UNAVAILABLE"

_CANDIDATE_SUFFIXES = frozenset({".json", ".jsonl", ".md", ".txt", ".csv"})
_MAX_CANDIDATE_BYTES = 8 * 1024 * 1024


def _split_words(text: str) -> list[str]:
    """Words of ``text``: runs of letters and digits, with combining marks kept on their base letter."""
    words: list[str] = []
    current: list[str] = []
    for ch in text:
        if ch.isalnum() or unicodedata.category(ch).startswith("M"):
            current.append(ch)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return words


_CONFLICT_NOTE = "conflict: the measurement is authoritative; the vendor claim is reported, not resolved or averaged"


@dataclass(frozen=True)
class MeasuredSource:
    path: str
    url: str
    kind: str
    trust_class: str
    rank: int
    content_sha256: str  # sha256 of ``text`` exactly as returned (the run-record contract)
    file_sha256: str  # sha256 of the whole file on disk
    text: str
    truncated: bool
    size_bytes: int
    score: int
    # Parsed from the whole file when it is JSON, so a document cut at MAX_TEXT_BYTES keeps them.
    measurements: tuple | None = None  # None: not parsed from the whole file; () : parsed, none found
    measurements_capped: bool = False  # the file held more entries than _MAX_MEASUREMENTS_PER_FILE


@dataclass(frozen=True)
class LookupResult:
    status: str
    sources: tuple[MeasuredSource, ...]
    reason: str


@dataclass(frozen=True)
class Measurement:
    subject: str
    metric: str
    value: float
    unit: str
    path: str


@dataclass(frozen=True)
class Assertion:
    claim_id: str
    subject: str
    metric: str
    value: float
    unit: str
    origin: str


@dataclass(frozen=True)
class Conflict:
    claim_id: str
    subject: str
    metric: str
    claimed_origin: str
    claimed: float
    measured: float
    claimed_unit: str
    measured_unit: str
    measured_path: str
    relative_difference: float | None  # None: no finite ratio (zero measurement, overflow, or different units)
    note: str


@dataclass(frozen=True)
class ConflictReport:
    conflicts: tuple[Conflict, ...]
    agreements: tuple[Conflict, ...]
    uncomparable: tuple[Conflict, ...]


def find_measured_sources(query, *, root, enabled=False, limit=5) -> LookupResult:
    """Find measured-evidence files under ``root`` matching ``query``.

    With ``enabled=False`` the provider is off by configuration and no
    filesystem access happens at all. A missing or non-directory ``root`` is
    ``UNAVAILABLE`` (never ``NONE``). Scoring is deterministic and model-free:
    lowercase the query, split on non-alphanumerics, drop tokens shorter than
    three characters and duplicates, then score 3 per token found in the
    relative path plus 1 per token found in the first ``MAX_TEXT_BYTES`` of
    text. Zero-score files are dropped and results are ordered by score
    descending then path ascending.
    """
    if not enabled:
        return LookupResult(
            status=NOT_SEARCHED, sources=(),
            reason="measured-evidence provider is disabled",
        )
    if root is None or (isinstance(root, (str, os.PathLike)) and not str(os.fspath(root)).strip()):
        return LookupResult(
            status=UNAVAILABLE, sources=(),
            reason="no measured-evidence root is configured",
        )
    try:
        # Resolve the configured root once; every later open is no-follow from this real path, so
        # a swap of the root for a symlink after this point is refused rather than followed.
        root_path = Path(root).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        try:
            root_path = Path(root)
        except (TypeError, ValueError):
            root_path = None
    if root_path is None or "\x00" in str(root_path):
        return LookupResult(
            status=UNAVAILABLE, sources=(),
            reason="measured-evidence root is not a valid path",
        )
    try:
        root_is_dir = root_path.is_dir()
    except OSError as err:
        return LookupResult(
            status=UNAVAILABLE, sources=(),
            reason=f"measured-evidence root could not be read: {root_path}: {err.strerror or err.errno}",
        )
    if not root_is_dir:
        return LookupResult(
            status=UNAVAILABLE, sources=(),
            reason=f"measured-evidence root is not a directory: {root_path}",
        )
    tokens = _query_tokens(query)
    if not tokens:
        return LookupResult(
            status=NONE, sources=(),
            reason="query has no usable tokens (need at least one of 3+ characters)",
        )
    try:
        limit_n = int(limit)
    except (TypeError, ValueError, OverflowError):
        limit_n = 5
    limit_n = max(1, min(20, limit_n))

    try:
        root_fd = os.open(root_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError as err:
        return LookupResult(
            status=UNAVAILABLE, sources=(),
            reason=f"measured-evidence root could not be opened: {root_path}: {err.strerror or err.errno}",
        )
    try:
        # Work is bounded before the result limit applies: a fixed number of files and bytes per lookup,
        # and only the best ``limit_n`` candidates are kept in memory.
        best: list[MeasuredSource] = []
        matched = scanned = total_bytes = 0
        capped = False
        walk: dict[str, Any] = {}
        for rel in _iter_candidate_files(root_fd, walk):
            if rel.suffix.casefold() not in _CANDIDATE_SUFFIXES:
                continue
            if scanned >= _MAX_FILES_SCANNED or total_bytes >= _MAX_BYTES_SCANNED:
                capped = True
                break
            scanned += 1
            try:
                data = _read_regular_file(root_fd, rel)
            except _Unreadable as err:
                walk.setdefault("walk_error", str(err))
                continue
            if data is None:
                continue
            total_bytes += len(data)
            # Cut by bytes; dropping invalid sequences keeps the text within the byte cap.
            text = data[:MAX_TEXT_BYTES].decode("utf-8", errors="ignore")
            path_str = rel.as_posix()
            score = _score(path_str, text, tokens)
            if score <= 0:
                continue
            matched += 1
            parsed, parsed_capped = (_parse_measurements_capped(data, path_str)
                                     if rel.suffix.casefold() == ".json" else (None, False))
            best.append(MeasuredSource(
                path=path_str,
                url="measured:" + path_str,
                kind=SOURCE_KIND,
                trust_class=TRUST_CLASS,
                rank=MEASURED_RANK,
                content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                file_sha256=hashlib.sha256(data).hexdigest(),
                text=text,
                truncated=len(data) > MAX_TEXT_BYTES,
                size_bytes=len(data),
                score=score,
                measurements=parsed,
                measurements_capped=parsed_capped,
            ))
            best.sort(key=lambda source: (-source.score, source.path))
            del best[limit_n:]

        capped = capped or walk.get("walk_capped", False)
        if walk.get("walk_error") and not best:
            # Nothing was found and part of the tree could not be read: that is not "no evidence".
            return LookupResult(
                status=UNAVAILABLE, sources=(),
                reason=f"measured-evidence directory could not be read: {walk['walk_error']}",
            )
        note = f"; scan stopped at the per-lookup limit after {scanned} files" if capped else ""
        if not best and capped:
            # The scan ended at a ceiling before it could say there is nothing: that is not "none".
            return LookupResult(
                status=UNAVAILABLE, sources=(),
                reason="measured-evidence scan was incomplete; it stopped before finding a match" + note,
            )
        if not best:
            return LookupResult(
                status=NONE, sources=(),
                reason="no measured sources matched the query" + note,
            )
        return LookupResult(
            status=OK, sources=tuple(best),
            reason=f"{len(best)} of {matched} measured sources matched" + note,
        )
    finally:
        os.close(root_fd)

def rank_sources(sources) -> list:
    """Stable sort placing ``kind == "measured"`` entries ahead of the rest.

    Entries may be dicts or objects carrying a ``kind`` attribute; the relative
    order of non-measured entries is preserved.
    """
    return sorted(sources, key=lambda source: 0 if _entry_kind(source) == SOURCE_KIND else 1)


def as_run_record_sources(sources, start_index=1) -> list:
    """Shape measured sources like run-record source entries (plus ``text``)."""
    retrieved_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    records = []
    for offset, source in enumerate(sources):
        records.append({
            "source_id": f"M{start_index + offset}",
            "url": source.url,
            "title": source.path,
            "retrieved_at": retrieved_at,
            "content_sha256": source.content_sha256,
            "file_sha256": source.file_sha256,
            "chars": len(source.text),
            "kind": source.kind,
            "trust_class": source.trust_class,
            "rank": source.rank,
            "text": source.text,
        })
    return records


def extract_measurements(source) -> list:
    """Read the ``"measurements"`` convention from a source.

    Uses the measurements parsed from the whole file when the lookup provided them, otherwise
    the source's text. Malformed JSON, a non-list container, non-dict items, booleans,
    non-finite or overflowing numbers and missing or wrongly-typed fields are skipped silently.
    """
    prepared = getattr(source, "measurements", None)
    if prepared is not None:
        # An empty tuple is an answer (the whole file parsed, or failed to, with nothing usable);
        # re-parsing a truncated prefix would promote data from a file that did not parse whole.
        return list(prepared)
    return list(_parse_measurements(source.text, source.path))


def _parse_measurements(raw, path) -> tuple:
    return _parse_measurements_capped(raw, path)[0]


def _parse_measurements_capped(raw, path) -> tuple:
    """(measurements, capped): ``capped`` only when a valid entry beyond the cap was left out."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return (), False
    if not isinstance(data, dict):
        return (), False
    items = data.get("measurements")
    if not isinstance(items, list):
        return (), False
    measurements = []
    for item in items:
        if not isinstance(item, dict):
            continue
        subject = item.get("subject")
        metric = item.get("metric")
        unit = item.get("unit")
        value = _finite_number(item.get("value"))
        if not isinstance(subject, str) or not subject.strip():
            continue
        if not isinstance(metric, str) or not metric.strip():
            continue
        if not isinstance(unit, str) or not unit.strip():
            continue
        if value is None:
            continue
        measurements.append(Measurement(subject=subject, metric=metric, value=value, unit=unit, path=path))
        if len(measurements) > _MAX_MEASUREMENTS_PER_FILE:
            # Bounded: a compact file must not expand into an unbounded object graph.
            return tuple(measurements[:_MAX_MEASUREMENTS_PER_FILE]), True
    return tuple(measurements), False


def _finite_number(value):
    """``value`` if it is a finite number, else None (booleans, non-numbers, overflowing integers).

    An integer keeps its exact value: coercing a count above 2**53 to a float would merge distinct
    values, and Python integer arithmetic is exact. Only the float range is checked.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    if not math.isfinite(number):
        return None
    return value if isinstance(value, int) else number


def find_conflicts(assertions, measurements, *, rel_tolerance=0.2) -> ConflictReport:
    """Report vendor-claim vs measurement conflicts; never resolve them.

    Comparisons are grouped by case-folded, whitespace-normalised subject and
    metric. Equal units (case-sensitive symbols) within ``rel_tolerance`` of the
    measurement are agreements; outside tolerance they are conflicts with the
    measurement authoritative; differing units are uncomparable. Every matching
    measurement is compared. Assertions with no matching measurement appear
    nowhere (silence is not agreement).
    """
    tolerance = _finite_number(rel_tolerance)
    if tolerance is None or tolerance < 0:
        raise ValueError("rel_tolerance must be a finite number >= 0")
    index: dict[tuple[str, str], list[tuple[Measurement, float]]] = {}
    for measurement in measurements:
        value = _finite_number(getattr(measurement, "value", None))
        if value is None:
            continue  # a non-finite or overflowing measurement cannot be compared
        index.setdefault(_match_key(measurement.subject, measurement.metric), []).append((measurement, value))

    conflicts: list[Conflict] = []
    agreements: list[Conflict] = []
    uncomparable: list[Conflict] = []
    for assertion in assertions:
        claimed = _finite_number(getattr(assertion, "value", None))
        if claimed is None:
            continue  # an invalid claim value cannot be compared; it is neither a conflict nor an agreement
        matches = index.get(_match_key(assertion.subject, assertion.metric))
        if not matches:
            continue
        for measurement, measured in matches:
            if _unit_key(assertion.unit) != _unit_key(measurement.unit):
                # Different units: no ratio is meaningful, so none is reported.
                uncomparable.append(_comparison(
                    assertion, measurement, None,
                    f"different units ({assertion.unit!r} vs {measurement.unit!r}): no conversion attempted",
                ))
                continue
            ratio = None
            if measured == 0:
                relative = 0.0 if claimed == 0 else None
            else:
                # Exact arithmetic: an integer and a float that differ by one count must not be
                # rounded into agreement, whichever way each side was written in the JSON.
                ratio = abs(Fraction(claimed) - Fraction(measured)) / abs(Fraction(measured))
                try:
                    relative = float(ratio)
                except OverflowError:
                    relative = None
                if relative is not None and not math.isfinite(relative):
                    relative = None  # a ratio outside the float range is reported as incomputable
            # Classify on the exact ratio so rounding to float cannot turn a conflict into an agreement.
            within = (relative == 0.0) if measured == 0 else (ratio <= Fraction(tolerance))
            if relative is not None and within:
                agreements.append(_comparison(
                    assertion, measurement, relative,
                    "within tolerance: the measurement agrees with the claim",
                ))
            else:
                conflicts.append(_comparison(assertion, measurement, relative, _CONFLICT_NOTE))
    return ConflictReport(tuple(conflicts), tuple(agreements), tuple(uncomparable))


def measured_evidence_enabled() -> bool:
    from src.settings import get_setting
    return bool(get_setting("research_measured_evidence", False))


def measured_evidence_root():
    from src.settings import get_setting
    value = get_setting("research_measured_evidence_root", None)
    return str(value) if value else None


_SKIP_ERRNOS = frozenset({errno.ENOENT, errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.ENXIO})


class _Unreadable(Exception):
    """A candidate that exists but could not be read (permissions or I/O)."""


def _read_regular_file(root_fd, rel):
    """Read ``root/rel`` through descriptors anchored at ``root``, or None.

    Each directory component is opened relative to the previous descriptor with no-follow, and
    the file itself the same way, so no path component can be swapped for a symlink between the
    listing and the read. The size is taken from the open descriptor, and a file that changed
    while it was read (size or modification time) is rejected rather than returned half-old.
    """
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    parts = Path(rel).parts
    if not parts or ".." in parts:
        return None
    fds = []
    try:
        current = root_fd  # one verified descriptor for the whole lookup; never reopened by name
        for part in parts[:-1]:
            current = os.open(part, dir_flags, dir_fd=current)
            fds.append(current)
        fd = os.open(parts[-1], file_flags, dir_fd=current)
        fds.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_CANDIDATE_BYTES:
            return None
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read(_MAX_CANDIDATE_BYTES + 1)
        after = os.fstat(fd)
        if (len(data) != before.st_size or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns):
            return None
        return data
    except OSError as err:
        if err.errno in _SKIP_ERRNOS:
            return None  # an intentional skip: a symlink, a vanished entry, not a directory or file
        # Anything else (permissions, EMFILE, ESTALE, I/O) is an operational failure: the evidence
        # may exist but could not be checked.
        raise _Unreadable(f"{Path(rel).as_posix()}: {err.strerror or err.errno}") from err
    finally:
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass


def _comparison(assertion, measurement, relative, note) -> Conflict:
    return Conflict(
        claim_id=assertion.claim_id,
        subject=assertion.subject,
        metric=assertion.metric,
        claimed_origin=assertion.origin,
        claimed=_finite_number(assertion.value),
        measured=_finite_number(measurement.value),
        claimed_unit=assertion.unit,
        measured_unit=measurement.unit,
        measured_path=measurement.path,
        relative_difference=relative,
        note=note,
    )


def _match_key(subject, metric) -> tuple[str, str]:
    return (_normalize(subject), _normalize(metric))


def _unit_key(value) -> str:
    """Whitespace-normalised unit symbol; case is kept because MW and mW, MB and Mb differ."""
    return " ".join(str(value).split())


def _normalize(value) -> str:
    return " ".join(str(value).split()).casefold()


def _entry_kind(source):
    if isinstance(source, dict):
        return source.get("kind")
    return getattr(source, "kind", None)


def _query_tokens(query) -> list[str]:
    if not isinstance(query, str):
        return []
    tokens: list[str] = []
    seen: set[str] = set()
    # Bound the work a query can cause: only the head of an oversized query is read, and only the
    # first few distinct tokens are scored against every candidate.
    for part in _split_words(query[:_MAX_QUERY_CHARS].casefold()):
        # Three characters for ASCII words; scripts written without spaces (CJK and others) have
        # shorter meaningful tokens.
        if (len(part) >= 3 or (len(part) >= 2 and not part.isascii())) and part not in seen:
            seen.add(part)
            tokens.append(part)
            if len(tokens) >= _MAX_QUERY_TOKENS:
                break
    return tokens


def _score(path_str, text, tokens) -> int:
    path_lower = path_str.casefold()
    text_lower = text.casefold()
    total = 0
    for token in tokens:
        if token in path_lower:
            total += 3
        if token in text_lower:
            total += 1
    return total


def _scan_tree(dir_fd, rel, state, budget, depth):
    """Yield relative file paths below the directory ``dir_fd``, files before subdirectories, sorted.

    Everything is enumerated relative to descriptors (``os.scandir(fd)``, ``os.open(dir_fd=...)``),
    never by name from the configured root, so replacing or renaming the root's path while the
    lookup runs cannot redirect the listing to another tree. Entries are counted while a directory
    is read, so one very wide directory stops at ``_MAX_ENTRIES_WALKED``. Symlinks are never
    followed or yielded; anything that could not be read is recorded in ``state["walk_error"]``.
    """
    if depth > 32:
        if state is not None:
            state["walk_error"] = "directory nesting deeper than 32 levels"
        return
    dirs: list[str] = []
    files: list[str] = []
    try:
        with os.scandir(dir_fd) as it:
            for entry in it:
                budget[0] += 1
                if budget[0] > _MAX_ENTRIES_WALKED:
                    if state is not None:
                        state["walk_capped"] = True
                    return
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(entry.name)
                    elif entry.is_file(follow_symlinks=False):
                        files.append(entry.name)
                except OSError as err:
                    # A failed metadata lookup is not proof the entry is absent.
                    if state is not None:
                        state["walk_error"] = str(getattr(err, "filename", None) or entry.name)
                    continue
    except OSError as err:
        if state is not None:
            state["walk_error"] = str(getattr(err, "filename", None) or err.strerror or err.errno)
        return
    for name in sorted(files):
        yield rel / name
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for name in sorted(dirs):
        if state is not None and state.get("walk_capped"):
            return
        try:
            child_fd = os.open(name, flags, dir_fd=dir_fd)
        except OSError as err:
            if err.errno in _SKIP_ERRNOS:
                continue  # swapped for a symlink or removed since it was listed
            if state is not None:
                state["walk_error"] = f"{(rel / name).as_posix()}: {err.strerror or err.errno}"
            continue
        try:
            yield from _scan_tree(child_fd, rel / name, state, budget, depth + 1)
        finally:
            os.close(child_fd)


def _iter_candidate_files(root, state=None):
    """Relative paths under ``evidence/`` and ``benchmark/<name>/results/``, symlinked dirs skipped.

    ``root`` is the verified root descriptor (a path is accepted for convenience and opened here).
    This only lists; ``_read_regular_file`` re-validates every component when it opens a path.
    Every directory and file the walk looks at counts toward ``_MAX_ENTRIES_WALKED`` whether or not
    it is a candidate, so a tree of unsupported artifacts cannot make a lookup run unbounded; the
    walk then stops and ``state["walk_capped"]`` is set.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    own_fd = None
    if isinstance(root, int):
        root_fd = root
    else:
        own_fd = root_fd = os.open(root, flags)
    try:
        entries = 0
        for top in ("evidence", "benchmark"):
            try:
                top_fd = os.open(top, flags, dir_fd=root_fd)
            except OSError as err:
                if err.errno not in _SKIP_ERRNOS and state is not None:
                    # A subtree that cannot even be opened is a traversal error, not an absent one.
                    state["walk_error"] = f"{top}: {err.strerror or err.errno}"
                continue
            try:
                budget = [entries]
                for rel in _scan_tree(top_fd, Path(top), state, budget, 0):
                    parts = rel.parts
                    if top == "benchmark" and not (len(parts) >= 4 and parts[2] == "results"):
                        continue
                    yield rel
                entries = budget[0]
            finally:
                os.close(top_fd)
            if state is not None and state.get("walk_capped"):
                return
    finally:
        if own_fd is not None:
            os.close(own_fd)
