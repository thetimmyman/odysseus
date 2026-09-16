"""PS-638 — SourceSnapshotIdentity: HEAD is not a source identity.

Every test here uses a REAL temporary git repository rather than a mock, because
the property under test is exactly the one a mock would assume away: that the
identity changes when the tree changes, and does not change when it does not.

The load-bearing test is
``test_changing_relevant_source_after_sealing_changes_identity``: PS-638 requires
that changing relevant source state after sealing invalidates source-bound
evidence. If that test passes vacuously the whole envelope is decorative.
"""
import subprocess

import pytest

from src.source_snapshot import (
    SourceSnapshotError,
    parse_porcelain,
    snapshot_digest_is_valid,
    source_snapshot_from_dict,
    take_source_snapshot,
)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True)


@pytest.fixture()
def repo(tmp_path):
    """A real repo with one commit: src.py and tests/t.py tracked."""
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Tester")
    (root / "src.py").write_text("x = 1\n")
    (root / "tests" / "t.py").write_text("def test_x():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _snapshot(repo, **kwargs):
    return take_source_snapshot(str(repo), **kwargs)


# ------------------------------------------------------------------ parsing ---
def test_parse_porcelain_splits_each_disposition():
    raw = b" M src/a.py\x00?? new.py\x00MM both.py\x00A  staged.py\x00"
    staged, unstaged, untracked = parse_porcelain(raw)
    assert staged == ["both.py", "staged.py"]
    assert unstaged == ["both.py", "src/a.py"]
    assert untracked == ["new.py"]


def test_parse_porcelain_handles_an_empty_status():
    assert parse_porcelain(b"") == ([], [], [])


# --------------------------------------------------------- a clean worktree ---
def test_clean_worktree_is_clean_and_complete(repo):
    snap = _snapshot(repo)
    assert snap.is_clean is True
    assert snap.is_complete is True
    assert snap.disposition() == "clean"
    assert snap.head_sha
    assert snap.snapshot_digest


def test_base_sha_is_recorded_separately_from_head(repo):
    head = _snapshot(repo).head_sha
    snap = _snapshot(repo, base_sha=head)
    assert snap.base_sha == head
    assert snap.head_sha == head


def test_identity_is_deterministic_across_calls(repo):
    first = _snapshot(repo, base_sha="HEAD")
    second = _snapshot(repo, base_sha="HEAD")
    assert first.snapshot_digest == second.snapshot_digest


# ------------------------------------------------------- dirty dispositions ---
def test_untracked_file_changes_the_identity(repo):
    before = _snapshot(repo)
    (repo / "scratch.py").write_text("generated = True\n")
    after = _snapshot(repo)

    assert after.untracked_paths == ("scratch.py",)
    assert after.is_clean is False
    assert after.disposition() == "dirty"
    assert after.snapshot_digest != before.snapshot_digest
    # A clean HEAD is unchanged, which is precisely why HEAD cannot be the identity.
    assert after.head_sha == before.head_sha


def test_unstaged_edit_changes_the_identity(repo):
    before = _snapshot(repo)
    (repo / "src.py").write_text("x = 2\n")
    after = _snapshot(repo)

    assert after.unstaged_paths == ("src.py",)
    assert after.is_clean is False
    assert after.tracked_diff_digest != before.tracked_diff_digest
    assert after.snapshot_digest != before.snapshot_digest


def test_staged_edit_is_reported_as_staged(repo):
    (repo / "src.py").write_text("x = 3\n")
    _git(repo, "add", "src.py")
    snap = _snapshot(repo)

    assert snap.staged_paths == ("src.py",)
    assert snap.unstaged_paths == ()
    assert snap.is_clean is False


def test_a_path_with_a_space_is_one_path(repo):
    (repo / "two words.py").write_text("y = 1\n")
    snap = _snapshot(repo)

    assert snap.untracked_paths == ("two words.py",)
    assert snap.snapshot_digest


# -------------------------------------------------------- relevant digests ---
def test_relevant_path_digests_are_recorded_per_path(repo):
    snap = _snapshot(repo, relevant_paths=["src.py", "absent.py"])
    assert len(snap.relevant_digest("src.py")) == 64
    assert snap.relevant_digest("absent.py") == "<absent>"


def test_irrelevant_changes_do_not_move_a_relevant_digest(repo):
    sealed = _snapshot(repo, base_sha="HEAD", relevant_paths=["src.py"])
    (repo / "unrelated.py").write_text("noise\n")
    now = _snapshot(repo, base_sha="HEAD", relevant_paths=["src.py"])

    assert now.relevant_digest("src.py") == sealed.relevant_digest("src.py")
    assert now.snapshot_digest != sealed.snapshot_digest


def test_changing_relevant_source_after_sealing_changes_identity(repo):
    """PS-638: sealed evidence must not survive a change to what it measured."""
    sealed = _snapshot(repo, base_sha="HEAD", relevant_paths=["src.py"])
    (repo / "src.py").write_text("x = 999\n")
    now = _snapshot(repo, base_sha="HEAD", relevant_paths=["src.py"])

    assert now.snapshot_digest != sealed.snapshot_digest
    assert now.relevant_digest("src.py") != sealed.relevant_digest("src.py")
    assert not sealed.same_source(now)


def test_truncation_makes_the_snapshot_incomplete_not_clean(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Tester")
    (root / "big.py").write_text("x" * 500)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "init")

    # The tree is genuinely clean; only the byte cap stops the identity being
    # complete. "we could not hash all of it" must not read as "all of it was clean".
    snap = take_source_snapshot(str(root), relevant_paths=["big.py"],
                                max_hash_bytes=8)
    assert snap.truncated_paths == ("big.py",)
    assert snap.is_clean is True
    assert snap.is_complete is False
    assert snap.disposition() == "clean-but-incomplete"


# ------------------------------------------------------------ serialization ---
def test_digest_roundtrip_and_tamper_detection(repo):
    snap = _snapshot(repo, base_sha="HEAD")
    payload = snap.to_dict()

    assert snapshot_digest_is_valid(payload) is True
    assert source_snapshot_from_dict(payload).snapshot_digest == snap.snapshot_digest

    tampered = dict(payload, head_sha="0" * 40)
    assert snapshot_digest_is_valid(tampered) is False


def test_unknown_field_is_refused_not_ignored(repo):
    payload = dict(_snapshot(repo).to_dict(), surprise=True)
    with pytest.raises(SourceSnapshotError):
        source_snapshot_from_dict(payload)


def test_missing_digest_is_invalid(repo):
    payload = dict(_snapshot(repo).to_dict())
    payload.pop("snapshot_digest")
    assert snapshot_digest_is_valid(payload) is False


# ----------------------------------------------------------------- refusals ---
def test_a_non_repository_is_refused(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(SourceSnapshotError):
        take_source_snapshot(str(plain))


def test_a_missing_path_is_refused(tmp_path):
    with pytest.raises(SourceSnapshotError):
        take_source_snapshot(str(tmp_path / "nope"))


def test_empty_worktree_argument_is_refused():
    with pytest.raises(SourceSnapshotError):
        take_source_snapshot("")
