"""Deterministic source identity for evidence.

A SHA alone cannot prove a clean execution tree: uncommitted or untracked
inputs are different evidence. So a snapshot records, deterministically:

  * the repo, its base SHA, its current head SHA, branch/worktree identity;
  * the STAGED / UNSTAGED / UNTRACKED disposition as explicit path lists;
  * a normalized digest of the tracked diff against the recorded base;
  * a digest over the untracked content that is actually part of the tree;
  * per-path digests for the files execution depends on (write scope, verifier
    paths, fixtures), because "the package hash changed" is less useful than
    "this fixture changed";
  * one ``snapshot_digest`` over all of the above.

Git runs with a fixed environment and explicit flags so user config cannot
change the identity; paths are NUL-split, never whitespace-split.

Content over a byte cap is recorded in ``truncated_paths`` and makes
``is_complete`` False, which the validator treats as ambiguity, not clean.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Iterable, List, Sequence, Tuple

SOURCE_SNAPSHOT_SCHEMA_VERSION = 1

#: Per-file cap for untracked content hashing.
DEFAULT_MAX_HASH_BYTES = 8 * 1024 * 1024

#: A larger tracked diff makes the snapshot incomplete, not a prefix hash.
DEFAULT_MAX_DIFF_BYTES = 32 * 1024 * 1024

_GIT_TIMEOUT_S = 60


class SourceSnapshotError(ValueError):
    """Raised when a source identity cannot be established deterministically."""


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(payload: object) -> bytes:
    """Deterministic JSON bytes for hashing: sorted keys, no incidental space."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")


def _git_env() -> dict:
    """Fixed git env: no user/system config can change the digest, and no prompt can hang."""
    env = dict(os.environ)
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_PAGER": "cat",
    })
    return env


def _git(worktree: str, args: Sequence[str], *, text: bool = True):
    cmd = ["git", "-C", worktree, "-c", "core.quotepath=false"] + list(args)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=_GIT_TIMEOUT_S,
                              env=_git_env())
    except FileNotFoundError as exc:  # no git on PATH
        raise SourceSnapshotError(f"git is not available: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SourceSnapshotError(
            f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_S}s") from exc
    if text:
        return (proc.returncode,
                (proc.stdout or b"").decode("utf-8", "replace"),
                (proc.stderr or b"").decode("utf-8", "replace"))
    return proc.returncode, proc.stdout or b"", proc.stderr or b""


def _porcelain(worktree: str) -> bytes:
    """``git status --porcelain -z -uall`` bytes; ``-z`` keeps quoted and unquoted names distinct."""
    rc, out, err = _git(worktree, ["status", "--porcelain=v1", "-z",
                                   "--untracked-files=all", "--no-renames"],
                        text=False)
    if rc != 0:
        raise SourceSnapshotError(
            f"git status failed in {worktree}: {err.strip()[:200]}")
    return out


def parse_porcelain(raw: bytes) -> Tuple[List[str], List[str], List[str]]:
    """Split porcelain ``-z`` output into sorted (staged, unstaged, untracked) paths.

    ``--no-renames`` guarantees one path per record.
    """
    staged: set = set()
    unstaged: set = set()
    untracked: set = set()
    for record in raw.split(b"\x00"):
        if not record:
            continue
        head = record[:2].decode("utf-8", "replace")
        path = record[3:].decode("utf-8", "replace")
        if not path:
            continue
        if head == "??":
            untracked.add(path)
            continue
        index_status, worktree_status = head[0], head[1]
        if index_status not in (" ", "?"):
            staged.add(path)
        if worktree_status not in (" ", "?"):
            unstaged.add(path)
    return sorted(staged), sorted(unstaged), sorted(untracked)


def _tracked_diff_digest(worktree: str, base_ref: str, *,
                         max_bytes: int) -> Tuple[str, bool]:
    """(digest, complete) of ``git diff <base_ref>``; incomplete when over the byte cap."""
    rc, out, err = _git(worktree, ["diff", "--no-ext-diff", "--no-color",
                                   "--binary", base_ref, "--"], text=False)
    if rc != 0:
        # Empty repo or missing base ref: represented, not raised, and not clean.
        note = err.strip()[:120].encode()
        return _sha256_hex(b"<no-diff-base:" + note + b">"), True
    complete = len(out) <= max_bytes
    return _sha256_hex(out[:max_bytes]), complete


def _hash_paths(worktree: str, paths: Iterable[str], *, max_bytes: int
                ) -> Tuple[Tuple[Tuple[str, str], ...], Tuple[str, ...]]:
    """Per-path sha256; missing paths record ``<absent>`` so creation is visible evidence."""
    digests: List[Tuple[str, str]] = []
    truncated: List[str] = []
    for rel in sorted(set(paths)):
        full = os.path.join(worktree, rel)
        if not os.path.isfile(full):
            digests.append((rel, "<absent>"))
            continue
        try:
            with open(full, "rb") as handle:
                data = handle.read(max_bytes + 1)
        except OSError as exc:
            raise SourceSnapshotError(
                f"cannot read {rel!r} for hashing: {exc}") from exc
        if len(data) > max_bytes:
            truncated.append(rel)
            data = data[:max_bytes]
        digests.append((rel, _sha256_hex(data)))
    return tuple(digests), tuple(truncated)


@dataclass(frozen=True)
class SourceSnapshotIdentity:
    """What the run actually read; ``snapshot_digest`` covers every field, so tampering shows."""

    repo_root: str
    head_sha: str
    base_sha: str = ""
    branch: str = ""
    repo_identity: str = ""
    worktree: str = ""
    detached: bool = False
    staged_paths: Tuple[str, ...] = ()
    unstaged_paths: Tuple[str, ...] = ()
    untracked_paths: Tuple[str, ...] = ()
    tracked_diff_digest: str = ""
    untracked_digest: str = ""
    relevant_digests: Tuple[Tuple[str, str], ...] = ()
    truncated_paths: Tuple[str, ...] = ()
    diff_truncated: bool = False
    schema_version: int = SOURCE_SNAPSHOT_SCHEMA_VERSION
    snapshot_digest: str = ""

    @property
    def is_clean(self) -> bool:
        """True only when NOTHING is staged, unstaged or untracked."""
        return not (self.staged_paths or self.unstaged_paths
                    or self.untracked_paths)

    @property
    def is_complete(self) -> bool:
        """False when a byte cap cut the identity short: ambiguity, not clean."""
        return not self.truncated_paths and not self.diff_truncated

    def disposition(self) -> str:
        """"clean" / "dirty" / "clean-but-incomplete", so partial hashing never reads as clean."""
        if not self.is_clean:
            return "dirty"
        return "clean" if self.is_complete else "clean-but-incomplete"

    def relevant_digest(self, path: str) -> str:
        """Digest recorded for one path, or ``""`` when it was not recorded."""
        for rel, digest in self.relevant_digests:
            if rel == path:
                return digest
        return ""

    def core(self) -> dict:
        """Every field identity covers — i.e. all of them but the digest."""
        return {
            "schema_version": self.schema_version,
            "repo_root": self.repo_root,
            "repo_identity": self.repo_identity,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "branch": self.branch,
            "worktree": self.worktree,
            "detached": self.detached,
            "staged_paths": list(self.staged_paths),
            "unstaged_paths": list(self.unstaged_paths),
            "untracked_paths": list(self.untracked_paths),
            "tracked_diff_digest": self.tracked_diff_digest,
            "untracked_digest": self.untracked_digest,
            "relevant_digests": [list(p) for p in self.relevant_digests],
            "truncated_paths": list(self.truncated_paths),
            "diff_truncated": self.diff_truncated,
        }

    def to_dict(self) -> dict:
        payload = self.core()
        payload["snapshot_digest"] = self.snapshot_digest
        return payload

    def same_source(self, other: "SourceSnapshotIdentity") -> bool:
        """Identity comparison by digest, never by path or by head alone."""
        return bool(self.snapshot_digest) and \
            self.snapshot_digest == getattr(other, "snapshot_digest", "")


def _finalize(core: dict) -> SourceSnapshotIdentity:
    """Build the frozen identity and seal its digest over ``core``."""
    fields = dict(core)
    fields["staged_paths"] = tuple(fields.get("staged_paths") or ())
    fields["unstaged_paths"] = tuple(fields.get("unstaged_paths") or ())
    fields["untracked_paths"] = tuple(fields.get("untracked_paths") or ())
    fields["relevant_digests"] = tuple(
        tuple(p) for p in (fields.get("relevant_digests") or ()))
    fields["truncated_paths"] = tuple(fields.get("truncated_paths") or ())
    fields["snapshot_digest"] = ""
    digest = _sha256_hex(_canonical(SourceSnapshotIdentity(**fields).core()))
    return SourceSnapshotIdentity(**{**fields, "snapshot_digest": digest})


def take_source_snapshot(worktree: str, *, base_sha: str = "",
                         relevant_paths: Sequence[str] = (),
                         max_hash_bytes: int = DEFAULT_MAX_HASH_BYTES,
                         max_diff_bytes: int = DEFAULT_MAX_DIFF_BYTES
                         ) -> SourceSnapshotIdentity:
    """Measure the source identity of ``worktree`` deterministically.

    ``base_sha`` is normally the run's declared base, not HEAD. ``relevant_paths``
    are hashed individually so validation can say which input moved.
    """
    if not worktree:
        raise SourceSnapshotError("worktree path must be non-empty")
    worktree = os.path.abspath(worktree)
    if not os.path.isdir(worktree):
        raise SourceSnapshotError(f"worktree does not exist: {worktree}")

    rc, out, err = _git(worktree, ["rev-parse", "--show-toplevel"])
    if rc != 0:
        raise SourceSnapshotError(
            f"not a git worktree: {worktree} ({err.strip()[:160]})")
    repo_root = out.strip() or worktree

    rc, out, _ = _git(worktree, ["rev-parse", "HEAD"])
    head_sha = out.strip() if rc == 0 else ""

    rc, out, _ = _git(worktree, ["rev-parse", "--abbrev-ref", "HEAD"])
    branch = out.strip() if rc == 0 else ""
    detached = branch == "HEAD"

    rc, out, _ = _git(worktree, ["config", "--get", "remote.origin.url"])
    repo_identity = out.strip() if rc == 0 and out.strip() else \
        os.path.basename(repo_root.rstrip("/"))

    staged, unstaged, untracked = parse_porcelain(_porcelain(worktree))

    base_ref = base_sha or head_sha
    diff_digest, diff_complete = _tracked_diff_digest(
        worktree, base_ref, max_bytes=max_diff_bytes) if base_ref else ("", True)

    untracked_pairs, untracked_truncated = _hash_paths(
        worktree, untracked, max_bytes=max_hash_bytes)
    untracked_digest = _sha256_hex(_canonical([list(p) for p in untracked_pairs]))

    relevant_pairs, relevant_truncated = _hash_paths(
        worktree, relevant_paths, max_bytes=max_hash_bytes)

    truncated = tuple(sorted(set(untracked_truncated) | set(relevant_truncated)))

    return _finalize({
        "schema_version": SOURCE_SNAPSHOT_SCHEMA_VERSION,
        "repo_root": repo_root,
        "repo_identity": repo_identity,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "branch": branch,
        "worktree": worktree,
        "detached": detached,
        "staged_paths": staged,
        "unstaged_paths": unstaged,
        "untracked_paths": untracked,
        "tracked_diff_digest": diff_digest,
        "untracked_digest": untracked_digest,
        "relevant_digests": list(relevant_pairs),
        "truncated_paths": list(truncated),
        "diff_truncated": not diff_complete,
    })


def source_snapshot_from_dict(payload: dict) -> SourceSnapshotIdentity:
    """Rebuild an identity and re-seal its digest; this alone proves nothing.

    Use :func:`snapshot_digest_is_valid` to detect an edited digest.
    """
    if not isinstance(payload, dict):
        raise SourceSnapshotError(
            f"source snapshot must be a mapping, got {type(payload).__name__}")
    unknown = set(payload) - set(SourceSnapshotIdentity.__dataclass_fields__)
    if unknown:
        raise SourceSnapshotError(
            f"unknown source snapshot field(s): {sorted(unknown)}")
    return _finalize({k: v for k, v in payload.items()
                      if k != "snapshot_digest"})


def snapshot_digest_is_valid(payload: dict) -> bool:
    """True when a serialized snapshot's digest matches its own fields."""
    if not isinstance(payload, dict) or not payload.get("snapshot_digest"):
        return False
    try:
        return source_snapshot_from_dict(payload).snapshot_digest == \
            payload["snapshot_digest"]
    except SourceSnapshotError:
        return False
