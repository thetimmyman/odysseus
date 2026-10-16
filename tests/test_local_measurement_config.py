"""Synthetic config sources only; no systemd, SSH, sockets or host configuration."""
import hashlib
import os
from pathlib import Path
import stat
import subprocess

import pytest

from src.local_measurement_config import ReadOnlyConfigProjection
from src.local_measurement_owner import MeasurementUnresolved


def manifest_for(root, files, roots):
    entries = []
    for path in files:
        meta = path.stat()
        entries.append({'path': str(path), 'resolved_path': str(path.resolve()),
                        'link': os.readlink(path) if path.is_symlink() else None,
                        'uid': meta.st_uid, 'mode': stat.S_IMODE(meta.st_mode),
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return {'schema': 'PINNED_SSH_CONFIG_V1', 'home': str(root),
            'roots': [{'path': str(path), 'include_base': str(path.parent)} for path in roots],
            'files': entries}


def test_exact_copies_have_private_ownership_and_bind_at_original_paths(tmp_path):
    config = tmp_path / 'config'
    config.write_text('Host synthetic\n HostName synthetic.invalid\n')
    config.chmod(0o600)
    original = config.read_bytes(), config.stat()
    with ReadOnlyConfigProjection(manifest_for(tmp_path, [config], [config])) as projection:
        bindings = projection.bindings()
        source, destination = next(pair for pair in bindings if pair[1] == str(config))
        assert destination == str(config)
        copy = Path(source)
        assert copy.read_bytes() == original[0]
        assert copy.stat().st_uid == os.getuid()
        assert stat.S_IMODE(copy.stat().st_mode) == 0o600
        assert stat.S_IMODE(copy.parent.stat().st_mode) == 0o700
        assert copy.parent != tmp_path
        assert (str(copy.parent), str(copy.parent)) in bindings
        projection.verify()
    assert not copy.exists()
    assert (config.read_bytes(), config.stat()) == original


def test_static_include_symlink_keeps_logical_link_and_binds_its_exact_target(tmp_path):
    config = tmp_path / 'config'
    config.write_text('Include extra.conf\nHost synthetic\n HostName synthetic.invalid\n')
    target = tmp_path / 'package.conf'
    target.write_text('Host other\n User synthetic\n')
    include = tmp_path / 'extra.conf'
    include.symlink_to('package.conf')
    with ReadOnlyConfigProjection(manifest_for(tmp_path, [config, include], [config])) as projection:
        file_bindings = [(source, destination) for source, destination in projection.bindings()
                         if Path(source).is_file()]
        assert {destination for _, destination in file_bindings} == {str(config), str(target)}
        assert os.readlink(include) == 'package.conf'
        assert all(Path(source).read_bytes() in (config.read_bytes(), target.read_bytes())
                   for source, _ in file_bindings)


def test_missing_or_extra_include_sources_are_refused(tmp_path):
    config = tmp_path / 'config'
    config.write_text('Include extra.conf\n')
    extra = tmp_path / 'extra.conf'
    extra.write_text('Host synthetic\n')
    with pytest.raises(MeasurementUnresolved):
        ReadOnlyConfigProjection(manifest_for(tmp_path, [config], [config]))
    config.write_text('Host synthetic\n')
    with pytest.raises(MeasurementUnresolved):
        ReadOnlyConfigProjection(manifest_for(tmp_path, [config, extra], [config]))


@pytest.mark.parametrize('change', ['bytes', 'mode', 'missing', 'symlink'])
def test_source_changes_after_preparation_refuse_instead_of_refreshing_pins(tmp_path, change):
    config = tmp_path / 'config'
    config.write_text('Host synthetic\n')
    config.chmod(0o600)
    with ReadOnlyConfigProjection(manifest_for(tmp_path, [config], [config])) as projection:
        if change == 'bytes':
            config.write_text('Host changed\n')
        elif change == 'mode':
            config.chmod(0o644)
        elif change == 'missing':
            config.unlink()
        else:
            other = tmp_path / 'other'
            other.write_text(config.read_text())
            other.chmod(0o600)
            config.unlink()
            config.symlink_to(other)
        with pytest.raises(MeasurementUnresolved):
            projection.verify()


@pytest.mark.parametrize('change', ['bytes', 'mode', 'missing', 'symlink'])
def test_private_copy_changes_are_refused(tmp_path, change):
    config = tmp_path / 'config'
    config.write_text('Host synthetic\n')
    with ReadOnlyConfigProjection(manifest_for(tmp_path, [config], [config])) as projection:
        copy = Path(next(source for source, dest in projection.bindings() if dest == str(config)))
        if change == 'bytes':
            copy.write_text('Host changed\n')
        elif change == 'mode':
            copy.chmod(0o644)
        elif change == 'missing':
            copy.unlink()
        else:
            copy.unlink()
            copy.symlink_to(config)
        with pytest.raises(MeasurementUnresolved):
            projection.verify()


def test_new_glob_match_refuses_without_inventing_an_updated_closure(tmp_path):
    config = tmp_path / 'config'
    config.write_text('Include extra*.conf\n')
    extra = tmp_path / 'extra.conf'
    extra.write_text('Host synthetic\n')
    with ReadOnlyConfigProjection(manifest_for(tmp_path, [config, extra], [config])) as projection:
        (tmp_path / 'extra-new.conf').write_text('Host changed\n')
        with pytest.raises(MeasurementUnresolved):
            projection.bindings()


@pytest.mark.parametrize('include', ['%h.conf', '$HOME/config', '~other/config', '../other', '*/config'])
def test_unsupported_dynamic_include_never_becomes_a_projection(tmp_path, include):
    config = tmp_path / 'config'
    config.write_text('Include ' + include + '\n')
    with pytest.raises(MeasurementUnresolved):
        ReadOnlyConfigProjection(manifest_for(tmp_path, [config], [config]))


def test_wrong_ownership_pin_and_world_writable_source_are_refused(tmp_path):
    config = tmp_path / 'config'
    config.write_text('Host synthetic\n')
    manifest = manifest_for(tmp_path, [config], [config])
    manifest['files'][0]['uid'] = os.getuid() + 1
    with pytest.raises(MeasurementUnresolved):
        ReadOnlyConfigProjection(manifest)
    config.chmod(0o666)
    with pytest.raises(MeasurementUnresolved):
        ReadOnlyConfigProjection(manifest_for(tmp_path, [config], [config]))


def test_copies_on_the_callers_writable_filesystem_cannot_grant_operational_proof(tmp_path):
    config = tmp_path / 'config'
    config.write_text('Host synthetic\n')
    with ReadOnlyConfigProjection(manifest_for(tmp_path, [config], [config])) as projection:
        result = subprocess.run(projection.proof_command(), capture_output=True, timeout=3)
        assert result.returncode == 2
        assert result.stdout == b''
        assert result.stderr == b'configuration projection proof refused\n'
