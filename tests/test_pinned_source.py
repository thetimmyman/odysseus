"""Tests for src.pinned_source (local git resolution, fetcher fallback,
input validation, status discipline).

All git repos are built in temp directories via subprocess; no network is
used.  Pin text (file contents, quotes, ref/path/repo names) is untrusted
data and is treated as such.
"""

import hashlib
import os
import re
import subprocess

import pytest

from src import pinned_source
from src.pinned_source import (
    FALSE,
    NOT_SEARCHED,
    TRUE,
    UNPINNED,
    UNRESOLVABLE,
    PinRef,
    classify_ref,
    resolve_pin,
    resolve_pins,
)

FILE_TEXT = (
    "Odysseus keeps its pinned sources grounded.\n\n"
    "Every citation resolves against a commit that cannot drift.\n\n"
    "Unpinned names such as branches are resolved, then flagged, and the\n"
    "content is hashed and compared, never executed."
)
QUOTE = "citation resolves against a commit that cannot drift"
SHA256_FILE = hashlib.sha256(FILE_TEXT.encode("utf-8")).hexdigest()


def _make_repo(tmp_path, name="myrepo"):
    """Build a tiny local repo with two commits, a tag, and extra branches.

    Layout: ``v1.0.0`` points at the first commit (adds read.txt); the
    second commit (HEAD of ``main``) adds docs/version.txt.  ``master``,
    ``latest``, ``stable``, ``stable-1`` and ``develop`` exist as branch
    names: the first two exercise branch-class refs, ``stable-1`` is a
    real branch with a tag-like shape.
    """
    repo = tmp_path / name

    def git(*args):
        subprocess.run(
            ["git", *[str(a) for a in args]],
            cwd=str(repo),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(tmp_path),
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_SYSTEM": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_AUTHOR_NAME": "Test",
                "GIT_AUTHOR_EMAIL": "test@example.invalid",
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.invalid",
            },
        )

    subprocess.run(
        ["git", "init", "-q", str(repo)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    (repo / "read.txt").write_text(FILE_TEXT, encoding="utf-8")
    git("add", "read.txt")
    git("commit", "-qm", "first")
    git("tag", "v1.0.0")
    git("branch", "-m", "main")
    (repo / "docs").mkdir()
    (repo / "docs" / "version.txt").write_text(
        "Version 2.0.0: pinned source notes.\n", encoding="utf-8"
    )
    git("add", "docs/version.txt")
    git("commit", "-qm", "second")
    for branch in ("master", "latest", "stable", "stable-1", "develop"):
        git("branch", branch)
    return repo


@pytest.fixture
def git_repo(tmp_path):
    return _make_repo(tmp_path, "myrepo")


@pytest.fixture
def root(git_repo, tmp_path):
    return tmp_path  # local_repos_root: the clone sits at <root>/myrepo


def first_commit_sha(repo):
    return subprocess.run(
        ["git", "rev-parse", "v1.0.0^{commit}"],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        check=True,
    ).stdout.decode().strip()


def _repo_file(**kw):
    kw.setdefault("kind", "repo_file")
    kw.setdefault("repo", "myrepo")
    return PinRef(**kw)


# --- classify_ref ----------------------------------------------------------


@pytest.mark.parametrize(
    "ref,kind",
    [
        ("abc1234", "commit"),
        ("d41d8cd98f00b204e9800998ecf8427e", "commit"),
        ("ABCDEF0123456789", "commit"),
        ("aaaaaaa", "commit"),
        ("v2.1.0", "tag"),
        ("2.1.0", "version"),
        ("1.2.3-rc.1", "tag"),
        ("v10.20.30", "tag"),
        ("main", "branch"),
        ("master", "branch"),
        ("HEAD", "branch"),
        ("head", "branch"),
        ("latest", "branch"),
        ("stable", "branch"),
        ("develop", "branch"),
        ("dev", "branch"),
        ("Main", "branch"),
    ],
)
def test_classify_ref(ref, kind):
    assert classify_ref(ref) == kind


def test_classify_ref_ambiguous_and_odd_shapes():
    # 40 (sha1) and 64 (sha256) hex chars are commits; 65 is not a commit shape.
    assert classify_ref("a" * 40) == "commit"
    assert classify_ref("a" * 64) == "commit"
    assert classify_ref("a" * 65) == "version"
    assert classify_ref("2.0") == "version"
    assert classify_ref("release-notes") == "version"


# --- local resolution: the controls ----------------------------------------


def test_real_commit_path_quote_true(git_repo, root):
    # Probe the fixture directly so this test is a known hit, not a guess.
    sha = first_commit_sha(git_repo)
    assert re.fullmatch(r"[0-9a-f]{40}", sha)
    result = resolve_pin(
        _repo_file(ref=sha, path="read.txt", quote=QUOTE),
        local_repos_root=root,
    )
    assert result.status == TRUE
    assert result.matched_quote is True
    assert result.text_sha256 == SHA256_FILE
    assert UNPINNED not in result.flags


def test_wrong_tag_false(root):
    result = resolve_pin(
        _repo_file(ref="v9.9.9", path="read.txt"), local_repos_root=root
    )
    assert result.status == FALSE
    assert "tag" in result.reason


def test_fabricated_path_false_at_real_commit(git_repo, root):
    sha = first_commit_sha(git_repo)
    result = resolve_pin(
        _repo_file(ref=sha, path="no/such/file.md"), local_repos_root=root
    )
    assert result.status == FALSE
    assert "path" in result.reason


def test_real_file_fabricated_quote_false(root):
    result = resolve_pin(
        _repo_file(ref="v1.0.0", path="read.txt", quote="text that was never written"),
        local_repos_root=root,
    )
    assert result.status == FALSE
    assert result.matched_quote is False
    # The sha is still reported, because the content was actually read.
    assert result.text_sha256 == SHA256_FILE


def test_quote_matches_after_whitespace_collapse(root):
    # Leading/trailing whitespace and odd spacing in the quote must not
    # matter; the verbatim text (with normal spacing) does matter.
    for quote in ("  " + QUOTE, "\t" + QUOTE + "\n", "cannot    drift"):
        result = resolve_pin(
            _repo_file(ref="v1.0.0", path="read.txt", quote=quote),
            local_repos_root=root,
        )
        assert result.status == TRUE, quote
        assert result.matched_quote is True, quote


def test_main_citation_true_with_unpinned_flag(root):
    result = resolve_pin(
        _repo_file(ref="main", path="read.txt"), local_repos_root=root
    )
    assert result.status == TRUE
    assert UNPINNED in result.flags


@pytest.mark.parametrize("ref", ["stable", "develop", "latest", "master", "HEAD"])
def test_other_branch_citations_unpinned(root, ref):
    result = resolve_pin(
        _repo_file(ref=ref, path="read.txt"), local_repos_root=root
    )
    assert result.status == TRUE
    assert UNPINNED in result.flags


def test_release_notes_kind_via_local_git(root):
    result = resolve_pin(
        PinRef(
            kind="release_notes",
            repo="myrepo",
            ref="main",
            path="docs/version.txt",
            quote="pinned source notes",
        ),
        local_repos_root=root,
    )
    assert result.status == TRUE
    assert UNPINNED in result.flags


def test_branch_tag_shape_ref_still_unpinned(root):
    # "stable-1" is a real branch shaped like a tag: it resolves, it is
    # still unpinned (it drifts), but nothing may pretend to be unresolvable.
    git_repo = root / "myrepo"
    out = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "stable-1^{commit}"],
        cwd=str(git_repo),
        stdout=subprocess.PIPE,
    )
    assert out.returncode == 0
    result = resolve_pin(
        _repo_file(ref="stable-1", path="read.txt"), local_repos_root=root
    )
    assert result.status == TRUE
    assert UNPINNED in result.flags


def test_docs_version_without_path_resolves_ref_locally(root):
    # A docs_version pin names the version; with a local clone the ref
    # itself is the thing to confirm, and no file content is claimed.
    result = resolve_pin(
        PinRef(kind="docs_version", repo="myrepo", ref="v1.0.0"),
        local_repos_root=root,
    )
    assert result.status == TRUE
    assert result.text_sha256 is None
    assert UNPINNED not in result.flags


def test_release_notes_without_path_is_not_answered_locally(root):
    # The ref exists locally, but release notes cannot be read without a
    # path: with no fetcher that is an unanswered lookup, never TRUE.
    result = resolve_pin(
        PinRef(kind="release_notes", repo="myrepo", ref="v1.0.0"),
        local_repos_root=root,
    )
    assert result.status == UNRESOLVABLE

    calls = []

    def fake(kind, repo, ref, path):
        calls.append((kind, repo, ref, path))
        return (True, "notes at the pin")

    result = resolve_pin(
        PinRef(kind="release_notes", repo="myrepo", ref="v1.0.0"),
        local_repos_root=root,
        fetcher=fake,
    )
    assert result.status == TRUE
    assert calls == [("release_notes", "myrepo", "v1.0.0", None)]


# --- UNRESOLVABLE -----------------------------------------------------------


def test_local_repo_missing_and_no_fetcher(git_repo, tmp_path):
    # A valid plain repo name that has no clone: nobody can answer.
    result = resolve_pin(
        PinRef(kind="repo_file", repo="gone", ref="v1.0.0", path="read.txt"),
        local_repos_root=tmp_path,
    )
    assert result.status == UNRESOLVABLE
    assert result.reason


def test_no_root_no_fetcher(git_repo):
    result = resolve_pin(_repo_file(ref="v1.0.0", path="read.txt"))
    assert result.status == UNRESOLVABLE


def test_fetcher_raising_unresolvable(tmp_path):
    def boom(kind, repo, ref, path):
        raise RuntimeError("network down")

    result = resolve_pin(
        PinRef(
            kind="release_notes",
            repo="some/owner-repo",
            ref="v1.2.3",
            path="notes.md",
        ),
        local_repos_root=tmp_path,
        fetcher=boom,
    )
    assert result.status == UNRESOLVABLE
    assert "fetcher" in result.reason


def test_fetcher_returns_not_found(tmp_path):
    calls = []

    def missing(kind, repo, ref, path):
        calls.append((kind, repo, ref, path))
        return (False, None)

    result = resolve_pin(
        PinRef(kind="docs_version", repo="a/b", ref="1.2.3"),
        fetcher=missing,
    )
    assert calls == [("docs_version", "a/b", "1.2.3", None)]
    assert result.status == FALSE


def test_local_git_authoritative_over_fetcher(git_repo, root):
    calls = []

    def lying_fetcher(kind, repo, ref, path):
        calls.append(kind)
        return (False, None)

    sha = first_commit_sha(git_repo)
    result = resolve_pin(
        _repo_file(ref=sha, path="read.txt"),
        local_repos_root=root,
        fetcher=lying_fetcher,
    )
    assert result.status == TRUE
    assert calls == []  # the local clone answered; fetcher never consulted


# --- fetcher path: found + text ---------------------------------------------


def test_fetcher_found_true_and_sha(tmp_path):
    calls = []

    def fake(kind, repo, ref, path):
        calls.append((kind, repo, ref, path))
        return (True, FILE_TEXT)

    result = resolve_pin(
        PinRef(kind="docs_version", repo="owner/name", ref="2.5.0", quote=QUOTE),
        fetcher=fake,
    )
    assert calls == [("docs_version", "owner/name", "2.5.0", None)]
    assert result.status == TRUE
    assert result.matched_quote is True
    assert result.text_sha256 == SHA256_FILE


def test_fetcher_found_but_quote_missing(tmp_path):
    def fake(kind, repo, ref, path):
        return (True, "nothing like the quote in here")

    result = resolve_pin(
        PinRef(
            kind="release_notes", repo="a/b", ref="v1.0.0", quote="never said that"
        ),
        fetcher=fake,
    )
    assert result.status == FALSE
    assert result.matched_quote is False
    assert result.text_sha256 == hashlib.sha256(
        "nothing like the quote in here".encode("utf-8")
    ).hexdigest()


def test_fetcher_found_no_text(tmp_path):
    def fake(kind, repo, ref, path):
        return (True, None)

    result = resolve_pin(
        PinRef(kind="release_notes", repo="a/b", ref="v1.0.0"), fetcher=fake
    )
    assert result.status == TRUE
    assert result.matched_quote is None
    assert result.text_sha256 is None


def test_fetcher_found_without_text_cannot_verify_a_quote():
    def fake(kind, repo, ref, path):
        return (True, None)

    result = resolve_pin(
        PinRef(kind="release_notes", repo="a/b", ref="v1.0.0", quote="shipped"),
        fetcher=fake,
    )
    assert result.status == UNRESOLVABLE
    assert result.matched_quote is None


def test_oversized_file_is_unresolvable_not_false(tmp_path, monkeypatch):
    root = tmp_path / "repos"
    root.mkdir()
    repo = _make_repo(root)
    monkeypatch.setattr(pinned_source, "MAX_OUTPUT", 8)
    result = resolve_pin(
        PinRef(kind="repo_file", repo=repo.name, ref="main", path="read.txt",
               quote="a passage that would sit past the cap"),
        local_repos_root=root,
    )
    assert result.status == UNRESOLVABLE


# --- disabled flag: NOT_SEARCHED, fetcher never called -----------------------


def test_disabled_not_searched_and_fetcher_never_called(tmp_path):
    called = []

    def spy(kind, repo, ref, path):
        called.append(1)
        return (True, FILE_TEXT)

    result = resolve_pin(
        _repo_file(ref="v1.0.0", path="read.txt", quote=QUOTE),
        local_repos_root=tmp_path,  # root without the clone: git is not tried
        fetcher=spy,
        enabled=False,
    )
    assert result.status == NOT_SEARCHED
    assert result.reason
    assert called == []  # a disabled provider must not even reach the fetcher


def test_disabled_short_circuits_even_with_clone(git_repo, root):
    called = []

    def spy(kind, repo, ref, path):
        called.append(1)
        return (True, "x")

    # The clone really exists here, so this proves the switch beats a
    # perfectly resolvable local lookup.
    result = resolve_pin(
        _repo_file(ref="v1.0.0", path="read.txt"),
        local_repos_root=root,
        fetcher=spy,
        enabled=False,
    )
    assert result.status == NOT_SEARCHED
    assert called == []


def test_disabled_malformed_pin_stays_not_searched(tmp_path):
    result = resolve_pin(
        PinRef(kind="repo_file", repo="../x", ref="--output=/tmp/x", path="a.txt"),
        local_repos_root=tmp_path,
        enabled=False,
    )
    assert result.status == NOT_SEARCHED


# --- hostile inputs: rejected before any subprocess --------------------------


def test_hostile_inputs_rejected_without_git(git_repo, root, monkeypatch):
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        raise AssertionError("git must not be spawned for rejected inputs")

    monkeypatch.setattr(pinned_source.subprocess, "run", refuse)

    pins = {
        "ref option injection": _repo_file(ref="--output=/tmp/x", path="read.txt"),
        "path traversal": _repo_file(ref="v1.0.0", path="../../etc/passwd"),
        "path with dotdot segment": _repo_file(ref="v1.0.0", path="docs/../read.txt"),
        "path with backslash": _repo_file(ref="v1.0.0", path="a\\b.txt"),
        "path with shell char": _repo_file(ref="v1.0.0", path="a; b.txt"),
        "repo traversal": PinRef(
            kind="repo_file", repo="../x", ref="v1.0.0", path="read.txt"
        ),
        "ref with whitespace": _repo_file(ref="v1.0.0 +1", path="read.txt"),
        "ref with tilde": _repo_file(ref="HEAD~1", path="read.txt"),
        "ref with shell char": _repo_file(ref="v1.0.0; rm -rf /", path="read.txt"),
    }
    for label, pin in pins.items():
        result = resolve_pin(pin, local_repos_root=root)
        assert result.status in (FALSE, UNRESOLVABLE), (label, result)
        assert result.reason, (label, result)
    assert calls == []  # not a single subprocess call was made


def test_hostile_repo_owner_and_root_name_rejected(monkeypatch):
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        raise AssertionError("no subprocess for rejected inputs")

    monkeypatch.setattr(pinned_source.subprocess, "run", refuse)

    pins = [
        PinRef(kind="release_notes", repo="a/../b", ref="v1.0.0"),
        PinRef(kind="docs_version", repo="..", ref="1.2.3"),
        PinRef(kind="docs_version", repo="own er/name", ref="1.2.3"),
        PinRef(kind="repo_file", repo="", ref="v1.0.0", path="read.txt"),
    ]
    for pin in pins:
        result = resolve_pin(pin)
        assert result.status in (FALSE, UNRESOLVABLE), pin
        assert result.reason
    assert calls == []


def test_hostile_ref_on_fetcher_path_rejected_before_fetch(tmp_path, monkeypatch):
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        raise AssertionError("no subprocess for rejected refs")

    monkeypatch.setattr(pinned_source.subprocess, "run", refuse)

    def spy(kind, repo, ref, path):
        calls.append(("fetcher", kind, repo, ref, path))
        return (True, "x")

    result = resolve_pin(
        PinRef(kind="release_notes", repo="owner/name", ref="HEAD~1", path="n.md"),
        fetcher=spy,
    )
    assert result.status == FALSE
    assert calls == []  # refused before the fetcher, and without any git


# --- ungrounded: UNPINNED + NOT_SEARCHED -------------------------------------


def test_missing_ref_unpinned_not_searched(git_repo, root):
    result = resolve_pin(
        PinRef(kind="repo_file", repo="myrepo"), local_repos_root=root
    )
    assert result.status == NOT_SEARCHED
    assert UNPINNED in result.flags


def test_blank_ref_unpinned_not_searched(git_repo, root):
    result = resolve_pin(
        PinRef(kind="repo_file", repo="myrepo", ref="   "), local_repos_root=root
    )
    assert result.status == NOT_SEARCHED
    assert UNPINNED in result.flags


def test_missing_ref_does_not_spawn_git(root, monkeypatch):
    calls = []

    def refuse(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))

    monkeypatch.setattr(pinned_source.subprocess, "run", refuse)
    result = resolve_pin(
        PinRef(kind="repo_file", repo="myrepo"), local_repos_root=root
    )
    assert result.status == NOT_SEARCHED
    assert calls == []


def test_repo_file_missing_path_not_searched_with_reason(git_repo, root):
    result = resolve_pin(
        PinRef(kind="repo_file", repo="myrepo", ref="v1.0.0", path=None),
        local_repos_root=root,
    )
    assert result.status == NOT_SEARCHED
    assert "path" in result.reason


# --- misc --------------------------------------------------------------------


def test_resolve_pins_wrapper(git_repo, root):
    sha = first_commit_sha(git_repo)
    results = resolve_pins(
        [
            PinRef(
                kind="repo_file",
                repo="myrepo",
                ref=sha,
                path="read.txt",
                quote=QUOTE,
            ),
            PinRef(kind="repo_file", repo="myrepo", ref="v9.9.9", path="read.txt"),
            PinRef(kind="repo_file", repo="myrepo"),
        ],
        local_repos_root=root,
    )
    assert [r.status for r in results] == [TRUE, FALSE, NOT_SEARCHED]
    assert UNPINNED in results[2].flags


def test_quote_matches_verbatim_even_when_spaced(root):
    # Extra internal spacing in the quote still matches single-spaced text.
    result = resolve_pin(
        _repo_file(ref="v1.0.0", path="read.txt", quote="cannot   drift"),
        local_repos_root=root,
    )
    assert result.status == TRUE
    assert result.matched_quote is True


def test_odd_but_safe_ref_shapes_go_to_git_and_answer_false(git_repo, root):
    # 41 hex chars and release-* branch names are inside the safe charset:
    # they must be passed to git (not falsely rejected) and answered
    # authoritatively FALSE (the ref simply does not exist here).
    for ref in ("A" * 41, "release-2.0"):
        result = resolve_pin(
            _repo_file(ref=ref, path="read.txt"), local_repos_root=root
        )
        assert result.status == FALSE, (ref, result)


def test_pin_ref_field_type_validation():
    # Spec: a bad ``kind`` is untrusted input handled as a rejected
    # PinResult (see test_unknown_kind_rejected), not a constructor
    # exception; field *types* are still validated eagerly.
    with pytest.raises(ValueError):
        PinRef(kind="repo_file", repo=123)
    with pytest.raises(ValueError):
        PinRef(kind="repo_file", repo="myrepo", ref=1.0)


def test_unknown_kind_rejected(tmp_path):
    result = resolve_pin(
        PinRef(kind="bogus_kind", repo="myrepo", ref="v1.0.0", path="read.txt"),
        local_repos_root=tmp_path,
    )
    assert result.status == NOT_SEARCHED
    assert "kind" in result.reason


def test_module_hygiene():
    text = open(
        os.path.join(os.path.dirname(__file__), "..", "src", "pinned_source.py")
    ).read()
    assert "shell=True" not in text
    assert "urllib" not in text and "http.client" not in text and "socket" not in text
    assert "import os" in text  # only for path handling and subprocess env


def test_no_env_reads_outside_fixture(tmp_path):
    # Behavior must not depend on the ambient environment: identical results
    # when irrelevant env chatter is present or absent.
    git_repo = _make_repo(tmp_path, "myrepo")
    assert (git_repo / ".git").exists()
    result = resolve_pin(
        _repo_file(ref="v1.0.0", path="read.txt", quote=QUOTE),
        local_repos_root=tmp_path,
    )
    assert result.status == TRUE


# --- review hardening: object kinds, replace refs, remote refs, formats ------

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
}


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), check=True, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={"PATH": os.environ.get("PATH", ""), **_GIT_ENV},
    ).stdout.decode().strip()


def test_a_directory_path_is_not_a_file(git_repo, root):
    result = resolve_pin(_repo_file(ref="main", path="docs"), local_repos_root=root)
    assert result.status == FALSE
    assert "not a file" in result.reason


def test_replace_refs_cannot_swap_the_pinned_commit(git_repo, root):
    first = first_commit_sha(git_repo)
    second = _git(git_repo, "rev-parse", "main")
    # Make the first commit "look like" the second one to any git that honours it.
    _git(git_repo, "replace", "-f", first, second)
    result = resolve_pin(
        _repo_file(ref=first, path="docs/version.txt"), local_repos_root=root
    )
    assert result.status == FALSE  # docs/version.txt does not exist in the real first commit


def test_remote_tracking_branch_is_flagged_unpinned(git_repo, root):
    _git(git_repo, "update-ref", "refs/remotes/origin/main", "main")
    result = resolve_pin(
        _repo_file(ref="origin/main", path="read.txt"), local_repos_root=root
    )
    assert result.status == TRUE
    assert UNPINNED in result.flags


def test_git_read_failure_is_unresolvable_not_false(git_repo, root, monkeypatch):
    real = pinned_source._run_git

    def flaky(args, cwd):
        if "cat-file" in args and "-t" in args:
            return 128, b"", b"fatal: unable to read object (corrupt)"
        return real(args, cwd)

    monkeypatch.setattr(pinned_source, "_run_git", flaky)
    result = resolve_pin(_repo_file(ref="main", path="read.txt"), local_repos_root=root)
    assert result.status == UNRESOLVABLE


def test_oversized_blob_is_refused_before_it_is_read(git_repo, root, monkeypatch):
    monkeypatch.setattr(pinned_source, "MAX_OUTPUT", 8)
    calls = []
    real = pinned_source._run_git

    def spy(args, cwd):
        calls.append(list(args))
        return real(args, cwd)

    monkeypatch.setattr(pinned_source, "_run_git", spy)
    result = resolve_pin(_repo_file(ref="main", path="read.txt", quote=QUOTE),
                         local_repos_root=root)
    assert result.status == UNRESOLVABLE
    assert not any(a[:3] == ["git", "cat-file", "blob"] for a in calls)


def test_sha256_object_format_repository(tmp_path):
    repo = tmp_path / "sha2"
    subprocess.run(["git", "init", "-q", "--object-format=sha256", str(repo)], check=True)
    (repo / "f.txt").write_text("alpha beta gamma\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "c")
    full = _git(repo, "rev-parse", "HEAD")
    assert len(full) == 64
    result = resolve_pin(
        PinRef(kind="repo_file", repo="sha2", ref=full, path="f.txt", quote="beta"),
        local_repos_root=tmp_path,
    )
    assert result.status == TRUE
    assert UNPINNED not in result.flags


def test_semver_build_metadata_ref_is_accepted():
    seen = []

    def fake(kind, repo, ref, path):
        seen.append(ref)
        return (True, "notes")

    result = resolve_pin(PinRef(kind="release_notes", repo="a/b", ref="v1.2.3+build.1"),
                         fetcher=fake)
    assert result.status == TRUE and seen == ["v1.2.3+build.1"]


def test_leading_plus_ref_is_rejected():
    result = resolve_pin(PinRef(kind="release_notes", repo="a/b", ref="+v1"),
                         fetcher=lambda *a: (True, "x"))
    assert result.status == FALSE


@pytest.mark.parametrize("found", [None, 0, 1, "false", "yes"])
def test_non_boolean_found_flag_is_unresolvable(found):
    result = resolve_pin(PinRef(kind="release_notes", repo="a/b", ref="v1.0.0"),
                         fetcher=lambda *a: (found, "text"))
    assert result.status == UNRESOLVABLE


def test_unencodable_provider_text_is_unresolvable_not_an_exception():
    result = resolve_pin(PinRef(kind="release_notes", repo="a/b", ref="v1.0.0",
                                quote="x"),
                         fetcher=lambda *a: (True, "bad \udc80 text"))
    assert result.status == UNRESOLVABLE


def test_name_shared_by_tag_and_branch_is_flagged_unpinned(git_repo, root):
    _git(git_repo, "branch", "shared")
    _git(git_repo, "tag", "shared", "main")
    result = resolve_pin(_repo_file(ref="shared", path="read.txt"), local_repos_root=root)
    assert result.status == TRUE
    assert UNPINNED in result.flags


def test_a_plain_tag_is_not_flagged(git_repo, root):
    result = resolve_pin(_repo_file(ref="v1.0.0", path="read.txt"), local_repos_root=root)
    assert result.status == TRUE
    assert UNPINNED not in result.flags


def test_fatal_rev_parse_is_unresolvable_and_lets_the_fetcher_answer(git_repo, root, monkeypatch):
    real = pinned_source._run_git

    def broken(args, cwd):
        if "rev-parse" in args:
            return 128, b"", b"fatal: bad object"
        return real(args, cwd)

    monkeypatch.setattr(pinned_source, "_run_git", broken)
    result = resolve_pin(_repo_file(ref="main", path="read.txt"), local_repos_root=root)
    assert result.status == UNRESOLVABLE


def test_git_environment_blocks_lazy_fetch_and_replace_objects():
    env = pinned_source._git_env()
    assert env["GIT_NO_LAZY_FETCH"] == "1" and env["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_local_failure_falls_through_to_the_fetcher(git_repo, root, monkeypatch):
    monkeypatch.setattr(pinned_source, "_run_git", lambda args, cwd: (128, b"", b"fatal"))
    result = resolve_pin(
        _repo_file(ref="main", path="read.txt", quote="beta"),
        local_repos_root=root,
        fetcher=lambda kind, repo, ref, path: (True, "alpha beta"),
    )
    assert result.status == TRUE and result.matched_quote is True


def test_docs_version_quote_without_a_path_is_not_confirmed_locally(git_repo, root):
    pin = PinRef(kind="docs_version", repo="myrepo", ref="v1.0.0", quote="made up claim")
    result = resolve_pin(pin, local_repos_root=root)
    assert result.status == UNRESOLVABLE
    assert result.matched_quote is None


def test_remote_refs_are_unpinned_unless_commit_shaped_or_reported_as_a_tag():
    def plain(kind, repo, ref, path):
        return (True, "text")

    def reports_tag(kind, repo, ref, path):
        return (True, "text", "tag")

    def pin(ref):
        return PinRef(kind="repo_file", repo="a/b", ref=ref, path="x.md")

    assert UNPINNED in resolve_pin(pin("feature/release"), fetcher=plain).flags
    assert UNPINNED in resolve_pin(pin("v1.2.3"), fetcher=plain).flags  # may be a branch
    assert UNPINNED not in resolve_pin(pin("v1.2.3"), fetcher=reports_tag).flags
    assert UNPINNED not in resolve_pin(pin("abc1234"), fetcher=plain).flags


def test_shallow_clone_miss_is_unresolvable_and_falls_to_the_fetcher(git_repo, root):
    (git_repo / ".git" / "shallow").write_text("", encoding="utf-8")
    pin = _repo_file(ref="deadbeefcafe", path="read.txt")
    assert resolve_pin(pin, local_repos_root=root).status == UNRESOLVABLE
    answered = resolve_pin(pin, local_repos_root=root,
                           fetcher=lambda kind, repo, ref, path: (True, "alpha"))
    assert answered.status == TRUE


def test_hex_named_branch_is_flagged_unpinned(git_repo, root):
    _git(git_repo, "branch", "abcdef0", "main")
    result = resolve_pin(_repo_file(ref="abcdef0", path="read.txt"), local_repos_root=root)
    assert result.status == TRUE and UNPINNED in result.flags


def test_a_real_commit_sha_stays_pinned(git_repo, root):
    result = resolve_pin(_repo_file(ref=first_commit_sha(git_repo), path="read.txt"),
                         local_repos_root=root)
    assert result.status == TRUE and UNPINNED not in result.flags


def test_a_tag_named_like_a_branch_is_cleared_of_the_heuristic_flag(tmp_path):
    repo = tmp_path / "tagmain"
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    (repo / "f.txt").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "c")
    _git(repo, "tag", "stable")
    result = resolve_pin(
        PinRef(kind="repo_file", repo="tagmain", ref="stable", path="f.txt"),
        local_repos_root=tmp_path,
    )
    assert result.status == TRUE and UNPINNED not in result.flags


def test_git_found_through_the_callers_path(git_repo, root, monkeypatch):
    seen = {}
    real = subprocess.run

    def spy(args, **kw):
        seen["path"] = kw["env"].get("PATH")
        return real(args, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setenv("PATH", os.environ["PATH"])
    resolve_pin(_repo_file(ref="main", path="read.txt"), local_repos_root=root)
    assert seen["path"] == os.environ["PATH"]


def test_remote_not_found_for_a_branch_shaped_ref_is_still_unpinned():
    result = resolve_pin(
        PinRef(kind="repo_file", repo="a/b", ref="feature/release", path="x.md"),
        fetcher=lambda kind, repo, ref, path: (False, None),
    )
    assert result.status == FALSE and UNPINNED in result.flags


@pytest.mark.parametrize("ref", ["a..b", "v1.", "a.lock", "feat/x.lock", ".hidden", "a/.b", "x/"])
def test_malformed_git_refnames_are_rejected_before_any_lookup(ref):
    calls = []

    def fetcher(kind, repo, ref, path):
        calls.append(ref)
        return (True, "text")

    result = resolve_pin(PinRef(kind="release_notes", repo="a/b", ref=ref), fetcher=fetcher)
    assert result.status == FALSE and not calls
