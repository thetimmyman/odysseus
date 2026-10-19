"""Pinned source resolver.

Answers, for a pinned source citation (a :class:`PinRef`): does the pinned
reference (commit / tag / version / branch) exist, and does the file content
cited at that pin really exist there, optionally containing a quoted passage
verbatim (after whitespace collapse)?

Stdlib only.  Remote resolution goes through a caller-injected ``fetcher``
callable -- this module never imports a network client, reads no environment
variable for behavior (``PATH`` is used only to locate git), and keeps no global state.  Pin text is untrusted
data: it is hashed and compared, never executed or interpreted.

Statuses (see :class:`PinResult`):

* ``TRUE``         a lookup was actually performed and authoritatively
                   confirmed the pin (and the quote, if any).
* ``FALSE``        a source authoritatively answered "no" (ref, path or
                   quote does not exist), or the citation input was
                   malformed and rejected before any lookup.
* ``UNRESOLVABLE`` no source could answer (no local repo, no fetcher,
                   the fetcher failed, or a lookup timed out).  A no-answer
                   is never reported as FALSE.
* ``NOT_SEARCHED`` no lookup was performed at all (provider disabled, or
                   the citation is ungrounded because there is nothing to
                   search).  Ungrounded is never TRUE.

Unpinned citations carry the ``UNPINNED`` flag.  Branch-class refs
(``main``, ``latest``, ...) are resolved and may be TRUE for existence, but
always flag that the citation can drift; a citation with no ref at all is
``UNPINNED`` + ``NOT_SEARCHED``.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Callable, List, Optional

__all__ = [
    "TRUE",
    "FALSE",
    "UNRESOLVABLE",
    "NOT_SEARCHED",
    "UNPINNED",
    "PinRef",
    "PinResult",
    "classify_ref",
    "resolve_pin",
    "resolve_pins",
]

# --- Result statuses -------------------------------------------------------
TRUE = "TRUE"
FALSE = "FALSE"
UNRESOLVABLE = "UNRESOLVABLE"
NOT_SEARCHED = "NOT_SEARCHED"

# --- Flags -------------------------------------------------------------------
UNPINNED = "UNPINNED"

_PIN_KINDS = ("repo_file", "release_notes", "docs_version")

# Branch names that exist in almost every repo and therefore never pin.
_KNOWN_BRANCHES = frozenset(
    {"main", "master", "head", "latest", "stable", "develop", "dev"}
)

# --- Conservative allow-lists (refs, repos, paths) ---------------------------
# Commit: 7-40 bare hex chars.
_COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,64}")  # sha1 (40) or sha256 (64) object ids
# Tag/version: 1.2.3, v1.2.3, with an optional -rc1 / +build suffix.
_VERSION_RE = re.compile(
    r"v?\d{1,9}(?:\.\d{1,9}){1,9}(?:[-+][0-9A-Za-z][0-9A-Za-z.\-]*)?"
)
# A plain local directory name (no separators, no leading dot).
_REPO_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]*")
# An owner/name form acceptable to a fetcher.
_REPO_OWNER_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._\-]*/[A-Za-z0-9][A-Za-z0-9._\-]*"
)
# Path segments: alnum plus dot/underscore/hyphen.
_PATH_SEGMENT_RE = re.compile(r"[A-Za-z0-9._\-]+")
# Ref segments: same conservative character set (no whitespace, no shell
# metacharacters, no ``~^:+=!``); ``/`` may separate ref segments.
_REF_ALLOWED_RE = re.compile(r"[A-Za-z0-9._\-+]+")  # "+" for semver build metadata
_WS_RE = re.compile(r"\s+")

# subprocess discipline: bound every call, cap every byte we will keep.
GIT_TIMEOUT = 15.0
MAX_OUTPUT = 1 << 20  # 1 MiB


@dataclass(frozen=True)
class PinRef:
    """A pinned source citation (untrusted input; validated on resolve).

    ``kind``: ``repo_file`` | ``release_notes`` | ``docs_version``.
    ``repo``: a plain local directory name (local resolution) or an
    ``owner/name`` form (fetcher resolution).
    ``ref``: a commit sha, tag, version string, or branch name.  A missing
    ref makes the citation unpinned and ungrounded.
    ``path``: relative file path, required to mean anything for
    ``repo_file`` pins; other kinds may carry one for disambiguation.
    ``quote``: optional passage that must be found verbatim (after
    whitespace collapse) in the file at the pin.
    """

    kind: str = "repo_file"
    repo: str = ""
    ref: Optional[str] = None
    path: Optional[str] = None
    quote: Optional[str] = None

    def __post_init__(self) -> None:
        # ``kind`` is validated by :func:`resolve_pin` (it may receive
        # untrusted input and must not raise); fields must be strings.
        for name in ("kind", "repo", "ref", "path", "quote"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or None")


@dataclass
class PinResult:
    """Outcome of resolving one pin.

    ``text_sha256`` hashes the UTF-8 encoding of the resolved text (for
    local git reads, undecodable bytes are replacement characters).
    ``matched_quote`` is None when there is no quote, True/False when one
    was checked.
    """

    status: str
    flags: List[str] = field(default_factory=list)
    reason: str = ""
    text_sha256: Optional[str] = None
    matched_quote: Optional[bool] = None


def classify_ref(ref: str) -> str:
    """Classify a ref string (heuristic).

    Returns exactly one of four values:

    * ``commit``  -- 7-40 hex chars;
    * ``branch``  -- the well-known drift-prone branch names
      (case-insensitive: ``main``, ``master``, ``HEAD``, ``latest``,
      ``stable``, ``develop``, ``dev``);
    * ``tag``     -- a version shape that names a tag rather than a
      released version: ``v``-prefixed dotted versions (``v1.2.3``) and
      pre-release/build-suffixed versions (``1.2.3-rc.1``);
    * ``version`` -- a bare dotted version (``1.2.3``), and the catch-all
      for any other safe ref shape.

    Branch-ness of a non-obvious name (``stable-1``) cannot be decided
    from the string alone; :func:`resolve_pin` flags those once local git
    confirms they really are branches.
    """
    if not isinstance(ref, str) or not ref:
        raise ValueError("ref must be a non-empty string")
    if ref.lower() in _KNOWN_BRANCHES:
        return "branch"
    if _COMMIT_RE.fullmatch(ref):
        return "commit"
    if _VERSION_RE.fullmatch(ref):
        if ref[0] in "vV" or "-" in ref or "+" in ref:
            return "tag"
        return "version"
    return "version"


def _valid_ref(ref: str) -> bool:
    """Conservative ref shape: letters, digits, dot, underscore, hyphen,
    and ``/`` between segments; no leading ``-`` (option injection), no
    ``..`` or empty segments, no whitespace or any shell/meta character,
    and git's own refname rules for dots (no ``..``, no segment that starts
    with ``.`` or ends with ``.`` or ``.lock``)."""
    if not ref or ref[0] in "-+" or ".." in ref:
        return False
    for segment in ref.split("/"):
        if not _REF_ALLOWED_RE.fullmatch(segment):
            return False
        if segment[0] == "." or segment.endswith((".", ".lock")):
            return False
    return True


def _valid_path(path: str) -> bool:
    """Conservative relative path: no leading ``-``/``/``, no ``.``/``..``
    segments, no empty or out-of-charset segments, no ``\\``, no ``://``,
    no NUL bytes."""
    if not path or path[0] in "-/":
        return False
    if "\x00" in path or "\\" in path or "://" in path:
        return False
    for segment in path.split("/"):
        if segment in (".", ".."):
            return False
        if not _PATH_SEGMENT_RE.fullmatch(segment):
            return False
    return True


def _is_git_repo(directory: str) -> bool:
    """Filesystem-only git repo check (no subprocess)."""
    if not os.path.isdir(directory):
        return False
    if os.path.isdir(os.path.join(directory, ".git")) or os.path.isfile(
        os.path.join(directory, ".git")
    ):
        return True
    # Bare repository.
    return os.path.isfile(
        os.path.join(directory, "HEAD")
    ) and os.path.isdir(os.path.join(directory, "objects"))


def _is_shallow(repo_dir: str) -> bool:
    """Filesystem-only shallow-clone check (no subprocess)."""
    return any(
        os.path.isfile(os.path.join(repo_dir, *parts))
        for parts in ((".git", "shallow"), ("shallow",))
    )


def _git_env() -> dict:
    """Minimal, config-isolated environment for git child processes.

    Suppresses system and global git config (no user hooks, aliases,
    fsmonitor, or pager) and trusts every repo path for ownership checks
    (``safe.directory=*``) -- the operator has already trusted this repo
    tree by exposing it as the local repos root.
    """
    devnull = os.path.devnull
    return {
        # Executable search only (so git is found where the caller finds it);
        # it takes no part in what a lookup concludes.
        "PATH": os.environ.get("PATH", os.defpath),
        "LC_ALL": "C",
        # A refs/replace/<sha> entry must not swap the object a pin names.
        "GIT_NO_REPLACE_OBJECTS": "1",
        # A partial clone must not reach its promisor remote during a "local" lookup.
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": devnull,
        "GIT_CONFIG_SYSTEM": devnull,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "safe.directory",
        "GIT_CONFIG_VALUE_0": "*",
    }


def _run_git(args: list, cwd: str):
    """Run ``git <args>`` (argument list, never a shell) with a timeout and
    an output cap.

    Returns ``(rc, stdout, stderr)``.  ``rc`` is None when the process
    could not be started or the timeout fired -- i.e. no answer was
    obtained, which callers must report as UNRESOLVABLE, never FALSE.
    """
    try:
        proc = subprocess.run(
            list(args),
            cwd=cwd,
            env=_git_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=GIT_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None, b"", b""
    # Keep one byte past the cap so callers can tell "exactly at the cap"
    # from "truncated".
    return proc.returncode, proc.stdout[: MAX_OUTPUT + 1], proc.stderr[:MAX_OUTPUT]


def _collapsed_contains(text: str, quote: str) -> bool:
    """Quote search: verbatim, after collapsing whitespace runs to single
    spaces (and trimming the ends) in both the content and the quote."""
    return _WS_RE.sub(" ", quote).strip() in _WS_RE.sub(" ", text).strip()


def _finish_with_text(pin: PinRef, text: str, flags: List[str]) -> PinResult:
    """Build the result for content that was successfully read."""
    try:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return PinResult(UNRESOLVABLE, flags, "text is not valid Unicode; cannot verify")
    quote = pin.quote
    if quote is None or not quote.strip():
        return PinResult(TRUE, flags, "pin resolved and content read", digest, None)
    if _collapsed_contains(text, quote):
        return PinResult(
            TRUE,
            flags,
            "pin resolved; quote found after whitespace collapse",
            digest,
            True,
        )
    return PinResult(FALSE, flags, "quote not found at pin", digest, False)


def _resolve_local(
    pin: PinRef,
    ref: str,
    path: Optional[str],
    repo_dir: str,
    flags: List[str],
) -> Optional[PinResult]:
    """Resolve a pin against a local git repository.

    The ref and path have already been validated against the allow-lists,
    and the commit sha passed to ``git show`` is re-validated as hex, so no
    untrusted input reaches the child process unbounded.  ``path`` is None
    when the citation names no file (blank paths count as absent).

    Returns None when the local repo can resolve the ref but cannot answer
    the pin itself (a ``release_notes`` citation with no path: notes need a
    file to read), so the caller may consult the fetcher instead.
    """
    rc, out, _err = _run_git(
        ["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"],
        cwd=repo_dir,
    )
    if rc is None:
        return PinResult(
            UNRESOLVABLE,
            flags,
            "local git lookup could not run (git missing or timeout)",
        )
    if rc == 1:
        if _is_shallow(repo_dir):
            # A shallow clone omits history on purpose: a miss only proves this
            # clone cannot answer, not that the upstream pin is absent.
            return PinResult(UNRESOLVABLE, flags, "shallow clone does not contain the ref")
        # ``--verify --quiet`` exits 1 for "no such revision" and nothing else;
        # a fatal error (corrupt repo, unreadable objects) exits higher.
        return PinResult(
            FALSE,
            flags,
            f"{classify_ref(ref)} {ref!r} does not resolve in local repo",
        )
    if rc != 0:
        return PinResult(UNRESOLVABLE, flags, "local git could not resolve the ref")
    sha = out.decode("utf-8", "replace").strip()
    if not re.fullmatch(r"[0-9a-f]{4,64}", sha):
        return PinResult(UNRESOLVABLE, flags, "unexpected rev-parse output")

    # A ref that resolves is not necessarily pinned: a branch can hide
    # behind a tag-looking name (``stable-1``).  Ask git which ref the
    # name actually selected; only a ``refs/heads/`` hit is a branch.
    rc_b, out_b, _err_b = _run_git(
        [
            "git",
            "rev-parse",
            "--symbolic-full-name",
            "--verify",
            "--quiet",
            ref,
        ],
        cwd=repo_dir,
    )
    if rc_b is None:
        return PinResult(
            UNRESOLVABLE,
            flags,
            "local ref classification could not run (git missing or timeout)",
        )
    # Git's answer replaces the string heuristic.  An unambiguous tag is a
    # stable pin (even if it is named ``main``).  A branch or remote-tracking
    # ref can move.  A commit-shaped ref that selects no ref at all is just
    # a commit.  Anything else git cannot place (empty output: a tag and a
    # branch sharing one name) is treated as movable.
    selected = out_b.decode("utf-8", "replace").strip() if rc_b == 0 else ""
    if selected.startswith("refs/tags/"):
        if UNPINNED in flags:
            flags.remove(UNPINNED)
    elif selected.startswith(("refs/heads/", "refs/remotes/")):
        if UNPINNED not in flags:
            flags.append(UNPINNED)
    elif classify_ref(ref) != "commit" and UNPINNED not in flags:
        flags.append(UNPINNED)

    if path is None:
        if pin.kind == "release_notes" or (pin.quote is not None and pin.quote.strip()):
            # The ref exists, but with no path there is no text to read: notes
            # need a file, and a requested quote can never be confirmed from a
            # ref alone.  Hand over to the fetcher (or report no answer).
            return None
        return PinResult(
            TRUE,
            flags,
            "ref resolved in local repo; no path to read",
        )

    spec = sha + ":" + path
    rc, out, err = _run_git(["git", "cat-file", "-t", spec], cwd=repo_dir)
    if rc is None:
        return PinResult(
            UNRESOLVABLE,
            flags,
            "local git read could not run (git missing or timeout)",
        )
    if rc != 0:
        # Only git's own "no such path" answers are an authoritative no; a
        # corrupt repo or an offline partial clone is no answer at all.
        message = err.decode("utf-8", "replace").lower()
        if any(m in message for m in ("does not exist", "exists on disk, but not in", "not a valid object name")):
            return PinResult(FALSE, flags, f"path {path} does not exist at that pin")
        return PinResult(UNRESOLVABLE, flags, "local git could not read the object")
    if out.decode("utf-8", "replace").strip() != "blob":
        return PinResult(FALSE, flags, f"path {path} is not a file at that pin")
    rc, out, _err = _run_git(["git", "cat-file", "-s", spec], cwd=repo_dir)
    size = out.decode("utf-8", "replace").strip()
    if rc != 0 or not size.isdigit():
        return PinResult(UNRESOLVABLE, flags, "local git could not size the file")
    if int(size) > MAX_OUTPUT:
        # Refuse before reading: a truncated read cannot prove a quote absent.
        return PinResult(UNRESOLVABLE, flags, "file larger than the read cap; cannot verify")
    rc, out, _err = _run_git(["git", "cat-file", "blob", spec], cwd=repo_dir)
    if rc != 0:
        return PinResult(UNRESOLVABLE, flags, "local git could not read the file")
    if len(out) > MAX_OUTPUT:
        return PinResult(UNRESOLVABLE, flags, "file larger than the read cap; cannot verify")
    text = out.decode("utf-8", "replace")
    return _finish_with_text(pin, text, flags)



def resolve_pin(
    pin: PinRef,
    *,
    local_repos_root=None,
    fetcher: Optional[Callable] = None,
    enabled: bool = True,
) -> PinResult:
    """Resolve one pinned citation.

    ``local_repos_root``: directory containing local clones, addressed by
    plain repo name.  ``fetcher``: callable ``fetcher(kind, repo, ref,
    path) -> (found: bool, text: str | None)`` or ``(found, text, ref_kind)``
    with ``ref_kind`` ``"tag"`` / ``"commit"`` when the provider knows the ref
    cannot move, for anything a local repo cannot answer.  ``enabled``: provider switch; when False no lookup of
    any kind is performed.

    Order: validate inputs (any violation rejects the citation before any
    subprocess or fetcher call), then local git when the repo is a plain
    name with a local clone (local git is authoritative for that repo and
    answers every pin kind), then the fetcher, then UNRESOLVABLE.
    """
    flags: List[str] = []
    raw_ref = pin.ref if isinstance(pin.ref, str) else None
    has_ref = raw_ref is not None and bool(raw_ref.strip())
    if has_ref:
        if classify_ref(raw_ref.strip()) == "branch":
            flags.append(UNPINNED)
    else:
        flags.append(UNPINNED)

    if pin.kind not in _PIN_KINDS:
        return PinResult(
            NOT_SEARCHED,
            flags,
            f"unknown pin kind {pin.kind!r}; nothing to search",
        )
    if not enabled:
        return PinResult(
            NOT_SEARCHED,
            flags,
            "pinned-source provider disabled; no lookups performed",
        )
    if not has_ref:
        return PinResult(NOT_SEARCHED, flags, "citation has no ref (unpinned)")

    ref = raw_ref.strip()

    # --- Validate every input before any subprocess or fetcher call. ----
    if ref.startswith("-"):
        return PinResult(FALSE, flags, "ref rejected: must not start with '-'")
    if not _valid_ref(ref):
        return PinResult(FALSE, flags, "ref rejected: not a safe ref string")

    path = pin.path
    if pin.kind == "repo_file":
        if path is None or not path.strip():
            return PinResult(
                NOT_SEARCHED,
                flags,
                "repo_file citation has no path; nothing to search",
            )
        if not _valid_path(path):
            return PinResult(FALSE, flags, "path rejected: not a safe relative path")
    elif path is not None and path.strip() and not _valid_path(path):
        return PinResult(FALSE, flags, "path rejected: not a safe relative path")
    if path is not None and not path.strip():
        path = None  # a blank path names no file

    repo = pin.repo if isinstance(pin.repo, str) else ""
    repo_ok_local = bool(_REPO_NAME_RE.fullmatch(repo))
    if not repo_ok_local and not bool(_REPO_OWNER_RE.fullmatch(repo)):
        return PinResult(
            FALSE, flags, "repo rejected: not a plain name or owner/name"
        )

    # --- 1) Local git first: authoritative for a locally cloned repo. ---
    if local_repos_root is not None and repo_ok_local:
        repo_dir = os.path.join(os.fspath(local_repos_root), repo)
        if _is_git_repo(repo_dir):
            local = _resolve_local(pin, ref, path, repo_dir, flags)
            # A local "could not answer" is not an answer: the fetcher may still.
            if local is not None and not (local.status == UNRESOLVABLE and fetcher is not None):
                return local

    # --- 2) Otherwise the injected fetcher (never a first-party client).
    if fetcher is not None:
        try:
            result = fetcher(pin.kind, repo, ref, path)
        except Exception:
            return PinResult(UNRESOLVABLE, flags, "fetcher raised an exception")
        if not (isinstance(result, tuple) and len(result) in (2, 3)):
            return PinResult(
                UNRESOLVABLE, flags, "fetcher returned a malformed result"
            )
        found, text = result[0], result[1]
        ref_kind = result[2] if len(result) == 3 else None
        # A remote answer says nothing about whether the ref can move unless the
        # provider states it.  Only a commit-shaped ref, or one the provider
        # reports as a tag, is treated as stable; anything else (a branch named
        # like a version included) is flagged, found or not.
        if (UNPINNED not in flags and classify_ref(ref) != "commit"
                and ref_kind not in ("tag", "commit")):
            flags.append(UNPINNED)
        if type(found) is not bool:
            return PinResult(UNRESOLVABLE, flags, "fetcher returned a non-boolean found flag")
        if not found:
            return PinResult(FALSE, flags, "provider reports the pin was not found")
        if not isinstance(text, (str, type(None))):
            return PinResult(UNRESOLVABLE, flags, "fetcher returned non-string text")
        if text is None and pin.quote is not None and pin.quote.strip():
            return PinResult(
                UNRESOLVABLE, flags, "provider returned no text; quote cannot be verified"
            )
        if text is None:
            return PinResult(
                TRUE,
                flags,
                "provider found the pin; no text available to verify",
            )
        return _finish_with_text(pin, text, flags)

    # --- 3) No source could answer.
    return PinResult(
        UNRESOLVABLE,
        flags,
        "no local source and no fetcher available to answer",
    )


def resolve_pins(pins: list, **kwargs) -> list:
    """Resolve many pins; keyword arguments pass through to
    :func:`resolve_pin` unchanged."""
    return [resolve_pin(pin, **kwargs) for pin in pins]
