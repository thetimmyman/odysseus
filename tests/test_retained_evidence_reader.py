"""Confined historical bundle reads through the public storage interface."""
from copy import deepcopy
import hashlib
import json
import os

import pytest

from src import evidence_io as store
from src.evidence_package import compute_evidence_package_hash
from tests.test_execution_outcomes import make_evidence


LIMITS = dict(max_bundle_bytes=2 * 1024 * 1024, max_blob_bytes=64 * 1024,
              max_total_blob_bytes=128 * 1024, max_artifacts=32, max_read_seconds=5)


@pytest.fixture
def retained(tmp_path):
    payload, artifacts, *_ = make_evidence(tmp_path / "synthetic-repo", exit_code=1)
    root = tmp_path / "retained"
    path = store.retain_evidence_bundle(root, payload, artifacts=artifacts)
    return root, path, payload, artifacts


def read(root, digest, **limits):
    return store.read_retained_evidence_bundle(root, digest, **{**LIMITS, **limits})


def publish(root, bundle=None, *, raw=None):
    if raw is None:
        raw = json.dumps(bundle, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode()
    digest = hashlib.sha256(raw).hexdigest()
    target = root / f"{digest}.json"
    target.write_bytes(raw)
    target.chmod(0o600)
    return digest


def reseal(payload):
    payload.pop("evidence_package_hash", None)
    payload["evidence_package_hash"] = compute_evidence_package_hash(payload)
    return payload


def test_failed_history_round_trips_exactly_without_promoting_lifecycle(retained):
    root, path, payload, artifacts = retained
    before = {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in root.rglob("*")}
    loaded, contents = read(root, path.stem)
    assert loaded == payload
    assert contents == artifacts
    assert loaded["verification_receipts"][0]["exit_code"] == 1
    assert {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in root.rglob("*")} == before
    loaded["attempt_receipts"].clear()
    assert read(root, path.stem)[0] == payload


def test_no_verification_is_still_retained_history(retained):
    root, _, payload, artifacts = retained
    payload = deepcopy(payload)
    payload["verification_receipts"] = []
    reseal(payload)
    path = store.retain_evidence_bundle(root, payload, artifacts=artifacts)
    loaded, contents = read(root, path.stem)
    assert loaded == payload
    assert set(contents) == {"mem://context", "mem://output"}


@pytest.mark.parametrize("digest", ["../escape", "f" * 63, "A" * 64, "g" * 64, ""])
def test_locator_must_be_one_exact_lowercase_bundle_hash(retained, digest):
    with pytest.raises(store.EvidenceBundleReadError):
        read(retained[0], digest)


@pytest.mark.parametrize("name", LIMITS)
@pytest.mark.parametrize("value", [True, 0, -1, "10", float("inf"), float("nan")])
def test_invalid_limits_refuse_before_opening_any_path(tmp_path, name, value):
    with pytest.raises(store.EvidenceBundleReadError):
        read(tmp_path / "missing", "f" * 64, **{name: value})
    assert list(tmp_path.iterdir()) == []


def test_missing_exact_object_never_selects_another_bundle(retained):
    with pytest.raises(store.EvidenceBundleNotFoundError):
        read(retained[0], "f" * 64)


@pytest.mark.parametrize("limit", ["max_bundle_bytes", "max_blob_bytes",
                                   "max_total_blob_bytes", "max_artifacts"])
def test_each_budget_refuses_without_partial_results(retained, limit):
    with pytest.raises(store.EvidenceBundleLimitError):
        read(retained[0], retained[1].stem, **{limit: 1})


def test_exact_byte_and_object_ceilings_are_inclusive(retained):
    root, path, payload, artifacts = retained
    assert read(root, path.stem, max_bundle_bytes=path.stat().st_size,
                max_blob_bytes=max(map(len, artifacts.values())),
                max_total_blob_bytes=sum(map(len, artifacts.values())),
                max_artifacts=len(artifacts)) == (payload, artifacts)


def test_uri_aliases_count_separately_but_share_one_blob_budget_and_read(retained, monkeypatch):
    root, _, payload, artifacts = retained
    payload = deepcopy(payload)
    ref = payload["attempt_receipts"][0]["output_ref"]
    alias = {**ref, "storage_uri": "mem://alias"}
    payload["attempt_receipts"][0]["artifact_refs"] = [ref, alias]
    path = store.retain_evidence_bundle(root, reseal(payload),
                                       artifacts={**artifacts, "mem://alias": artifacts["mem://output"]})
    original = os.read
    reads = {}
    def track(fd, size):
        info = os.fstat(fd)
        reads[info.st_ino] = reads.get(info.st_ino, 0) + 1
        return original(fd, size)
    monkeypatch.setattr(os, "read", track)
    loaded, contents = read(root, path.stem, max_total_blob_bytes=sum(map(len, artifacts.values())),
                            max_artifacts=len(artifacts) + 1)
    assert loaded == payload
    assert contents["mem://alias"] is contents["mem://output"]
    inode = (root / "blobs" / ref["sha256"]).stat().st_ino
    assert reads[inode] == 2  # One data read and one EOF, irrespective of aliases.
    with pytest.raises(store.EvidenceBundleLimitError):
        read(root, path.stem, max_artifacts=len(artifacts))


def test_empty_uri_and_empty_blob_are_opaque_retained_values(retained):
    root, _, payload, artifacts = retained
    payload = deepcopy(payload)
    payload["attempt_receipts"][0]["output_ref"].update(
        storage_uri="", sha256=hashlib.sha256(b"").hexdigest(), size=0)
    path = store.retain_evidence_bundle(root, reseal(payload), artifacts={**artifacts, "": b""})
    assert read(root, path.stem)[1][""] == b""


@pytest.mark.parametrize("limit", ["max_blob_bytes", "max_total_blob_bytes", "max_artifacts"])
def test_blob_budgets_are_preflighted_before_blob_directory_open(retained, monkeypatch, limit):
    root, path, _, _ = retained
    original = os.open
    def track(name, *args, **kwargs):
        assert name != "blobs", "budget preflight must precede blob directory access"
        return original(name, *args, **kwargs)
    monkeypatch.setattr(os, "open", track)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {track})
    with pytest.raises(store.EvidenceBundleLimitError):
        read(root, path.stem, **{limit: 1})


@pytest.mark.parametrize("part", ["root", "blobs", "bundle", "blob"])
def test_symlinks_are_refused_at_each_owned_boundary(retained, tmp_path, part):
    root, path, _, _ = retained
    target = {"root": root, "blobs": root / "blobs", "bundle": path,
              "blob": next((root / "blobs").iterdir())}[part]
    moved = tmp_path / f"moved-{part}"
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=moved.is_dir())
    with pytest.raises(store.EvidenceBundleReadError):
        read(root, path.stem)


@pytest.mark.parametrize("part", ["root", "blobs", "bundle", "blob"])
def test_public_permissions_are_refused_and_never_repaired(retained, part):
    root, path, _, _ = retained
    target = {"root": root, "blobs": root / "blobs", "bundle": path,
              "blob": next((root / "blobs").iterdir())}[part]
    mode = 0o755 if target.is_dir() else 0o644
    target.chmod(mode)
    with pytest.raises(store.EvidenceBundleReadError):
        read(root, path.stem)
    assert target.stat().st_mode & 0o777 == mode


@pytest.mark.parametrize("kind", ["fifo", "directory", "hardlink"])
@pytest.mark.parametrize("part", ["bundle", "blob"])
def test_nonregular_and_multiply_linked_objects_are_refused(retained, tmp_path, kind, part):
    root, path, _, _ = retained
    target = path if part == "bundle" else next((root / "blobs").iterdir())
    if kind == "hardlink":
        os.link(target, tmp_path / "extra-link")
    else:
        target.unlink()
        if kind == "fifo":
            os.mkfifo(target, 0o600)
        else:
            target.mkdir(mode=0o700)
    with pytest.raises(store.EvidenceBundleReadError):
        read(root, path.stem)


@pytest.mark.parametrize("raw", [b'{"schema_version":1,"schema_version":1}',
                                 b'{"x":NaN}', b'{"x":Infinity}', b'[]', b'{}\n',
                                 b'{"broken":', b'\xff', b'[' * 2000])
def test_strict_bounded_json_never_returns_malformed_objects(retained, raw):
    root = retained[0]
    digest = publish(root, raw=raw)
    with pytest.raises(store.EvidenceBundleReadError):
        read(root, digest)


@pytest.mark.parametrize("mutation", ["schema", "extra", "package", "missing", "extra_uri",
                                      "digest", "size", "bool_size", "consumers", "uri"])
def test_resealed_manifest_and_package_inconsistency_is_refused(retained, mutation):
    root, path, _, _ = retained
    bundle = json.loads(path.read_bytes())
    manifest = bundle["artifacts"]
    uri = next(iter(manifest))
    if mutation == "schema":
        bundle["schema_version"] = True
    elif mutation == "extra":
        bundle["extra"] = "unexpected"
    elif mutation == "package":
        bundle["evidence_package"]["attempt_receipts"][0]["run_id"] = "changed"
    elif mutation == "missing":
        manifest.pop(uri)
    elif mutation == "extra_uri":
        manifest["file:///unselected"] = deepcopy(manifest[uri])
    elif mutation == "digest":
        manifest[uri]["sha256"] = "../outside"
    elif mutation == "size":
        manifest[uri]["size"] += 1
    elif mutation == "bool_size":
        manifest[uri]["size"] = True
    elif mutation == "consumers":
        manifest[uri]["consumers"] = []
    elif mutation == "uri":
        manifest["file:///different"] = manifest.pop(uri)
    digest = publish(root, bundle)
    with pytest.raises(store.EvidenceBundleReadError):
        read(root, digest)


def test_no_default_uri_loader_or_directory_discovery(retained, monkeypatch):
    root, path, _, _ = retained
    blob = next((root / "blobs").iterdir())
    def forbidden(*args, **kwargs):
        pytest.fail("reader attempted discovery or URI fallback")
    monkeypatch.setattr(store, "default_artifact_loader", forbidden)
    monkeypatch.setattr(os, "listdir", forbidden)
    monkeypatch.setattr(os, "scandir", forbidden)
    assert read(root, path.stem)[0] == retained[2]
    blob.unlink()
    with pytest.raises(store.EvidenceBundleNotFoundError):
        read(root, path.stem)


def test_missing_retained_file_uri_blob_never_reads_original(retained, tmp_path, monkeypatch):
    root, _, payload, artifacts = retained
    original = tmp_path / "original-output"
    original.write_bytes(artifacts["mem://output"])
    payload = deepcopy(payload)
    payload["attempt_receipts"][0]["output_ref"]["storage_uri"] = original.as_uri()
    path = store.retain_evidence_bundle(root, reseal(payload), artifacts=artifacts)
    digest = payload["attempt_receipts"][0]["output_ref"]["sha256"]
    (root / "blobs" / digest).unlink()
    def forbidden(*args, **kwargs):
        pytest.fail("original URI must never be loaded during retained reads")
    monkeypatch.setattr(store, "default_artifact_loader", forbidden)
    with pytest.raises(store.EvidenceBundleNotFoundError):
        read(root, path.stem)
    assert original.read_bytes() == artifacts["mem://output"]


def test_blob_and_outer_hash_tampering_is_refused(retained):
    root, path, _, _ = retained
    raw = path.read_bytes()
    path.write_bytes(raw + b" ")
    with pytest.raises(store.EvidenceBundleReadError):
        read(root, path.stem)
    path.write_bytes(raw)
    blob = next((root / "blobs").iterdir())
    blob.write_bytes(b"x" * blob.stat().st_size)
    with pytest.raises(store.EvidenceBundleReadError):
        read(root, path.stem)


def test_metadata_change_during_read_is_refused(retained, monkeypatch):
    root, path, _, _ = retained
    original = os.read
    changed = False
    def mutate(fd, size):
        nonlocal changed
        data = original(fd, size)
        if not changed:
            changed = True
            os.utime(path, ns=(1, 1))
        return data
    monkeypatch.setattr(os, "read", mutate)
    with pytest.raises(store.EvidenceBundleReadError):
        read(root, path.stem)


def test_elapsed_deadline_refuses_the_result(retained, monkeypatch):
    tick = 0
    def clock():
        nonlocal tick
        tick += 1
        return tick
    monkeypatch.setattr(store.time, "monotonic", clock)
    with pytest.raises(store.EvidenceBundleLimitError):
        read(retained[0], retained[1].stem, max_read_seconds=0.5)


def test_deadline_expiring_during_read_refuses_without_returning_partial_history(retained, monkeypatch):
    tick = [0]
    original = os.read
    def read_then_expire(fd, size):
        raw = original(fd, size)
        tick[0] = 10
        return raw
    monkeypatch.setattr(store.time, "monotonic", lambda: tick[0])
    monkeypatch.setattr(os, "read", read_then_expire)
    with pytest.raises(store.EvidenceBundleLimitError):
        read(retained[0], retained[1].stem)
