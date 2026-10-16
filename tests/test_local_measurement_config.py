"""Synthetic config sources only; no systemd, SSH, sockets or host configuration."""
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import shutil
from types import SimpleNamespace

import pytest

from src.local_measurement_config import ReadOnlyConfigProjection, ConfigProjectionRefused
import src.local_measurement_config as config_module
from src.local_measurement_owner import MeasurementUnresolved


@pytest.fixture(autouse=True)
def synthetic_ssh_home(tmp_path, monkeypatch):
    (tmp_path / '.ssh').mkdir(mode=0o700)
    monkeypatch.setattr('src.local_measurement_config.pwd.getpwuid',
                        lambda uid: SimpleNamespace(pw_dir=str(tmp_path)))


def manifest_for(root, files, roots):
    entries = []
    for path in files:
        meta = path.stat()
        entries.append({'path': str(path), 'resolved_path': str(path.resolve()),
                        'link': os.readlink(path) if path.is_symlink() else None,
                        'uid': meta.st_uid, 'mode': stat.S_IMODE(meta.st_mode),
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    roles = ['user' if path == root / '.ssh' / 'config' else 'system' for path in roots]
    return {'schema': 'PINNED_SSH_CONFIG_V1', 'home': str(root),
            'roots': roles,
            'files': entries}


def test_exact_copies_have_private_ownership_and_bind_at_original_paths(tmp_path):
    config = tmp_path / '.ssh' / 'config'
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
    config = tmp_path / '.ssh' / 'config'
    config.write_text('Include extra.conf\nHost synthetic\n HostName synthetic.invalid\n')
    target = tmp_path / '.ssh' / 'package.conf'
    target.write_text('Host other\n User synthetic\n')
    include = tmp_path / '.ssh' / 'extra.conf'
    include.symlink_to('package.conf')
    with ReadOnlyConfigProjection(manifest_for(tmp_path, [config, include], [config])) as projection:
        file_bindings = [(source, destination) for source, destination in projection.bindings()
                         if Path(source).is_file()]
        assert {destination for _, destination in file_bindings} == {str(config), str(target)}
        assert os.readlink(include) == 'package.conf'
        assert all(Path(source).read_bytes() in (config.read_bytes(), target.read_bytes())
                   for source, _ in file_bindings)


def test_missing_or_extra_include_sources_are_refused(tmp_path):
    config = tmp_path / '.ssh' / 'config'
    config.write_text('Include extra.conf\n')
    extra = tmp_path / '.ssh' / 'extra.conf'
    extra.write_text('Host synthetic\n')
    with pytest.raises(MeasurementUnresolved):
        ReadOnlyConfigProjection(manifest_for(tmp_path, [config], [config]))
    config.write_text('Host synthetic\n')
    with pytest.raises(MeasurementUnresolved):
        ReadOnlyConfigProjection(manifest_for(tmp_path, [config, extra], [config]))


@pytest.mark.parametrize('change', ['bytes', 'mode', 'missing', 'symlink'])
def test_source_changes_after_preparation_refuse_instead_of_refreshing_pins(tmp_path, change):
    config = tmp_path / '.ssh' / 'config'
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
            other = tmp_path / '.ssh' / 'other'
            other.write_text(config.read_text())
            other.chmod(0o600)
            config.unlink()
            config.symlink_to(other)
        with pytest.raises(MeasurementUnresolved):
            projection.verify()


@pytest.mark.parametrize('change', ['bytes', 'mode', 'missing', 'symlink'])
def test_private_copy_changes_are_refused(tmp_path, change):
    config = tmp_path / '.ssh' / 'config'
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
    config = tmp_path / '.ssh' / 'config'
    config.write_text('Include extra*.conf\n')
    extra = tmp_path / '.ssh' / 'extra.conf'
    extra.write_text('Host synthetic\n')
    with ReadOnlyConfigProjection(manifest_for(tmp_path, [config, extra], [config])) as projection:
        (tmp_path / '.ssh' / 'extra-new.conf').write_text('Host changed\n')
        with pytest.raises(MeasurementUnresolved):
            projection.bindings()


@pytest.mark.parametrize('include', ['%h.conf', '$HOME/config', '~other/config', '../other', '*/config'])
def test_unsupported_dynamic_include_never_becomes_a_projection(tmp_path, include):
    config = tmp_path / '.ssh' / 'config'
    config.write_text('Include ' + include + '\n')
    with pytest.raises(MeasurementUnresolved):
        ReadOnlyConfigProjection(manifest_for(tmp_path, [config], [config]))


def test_wrong_ownership_pin_and_world_writable_source_are_refused(tmp_path):
    config = tmp_path / '.ssh' / 'config'
    config.write_text('Host synthetic\n')
    manifest = manifest_for(tmp_path, [config], [config])
    manifest['files'][0]['uid'] = os.getuid() + 1
    with pytest.raises(MeasurementUnresolved):
        ReadOnlyConfigProjection(manifest)
    config.chmod(0o666)
    with pytest.raises(MeasurementUnresolved):
        ReadOnlyConfigProjection(manifest_for(tmp_path, [config], [config]))


def test_copies_on_the_callers_writable_filesystem_cannot_grant_operational_proof(tmp_path):
    config = tmp_path / '.ssh' / 'config'
    config.write_text('Host synthetic\n')
    with ReadOnlyConfigProjection(manifest_for(tmp_path, [config], [config])) as projection:
        result = subprocess.run(projection.proof_command(), capture_output=True, timeout=3)
        assert result.returncode == 2
        assert result.stdout == b''
        assert result.stderr == b'configuration projection proof refused\n'


@pytest.mark.parametrize('name', ['id_ed25519', 'known_hosts'])
def test_nonconfig_root_or_include_is_refused_before_reading_that_source(tmp_path, monkeypatch, name):
    config = tmp_path / '.ssh' / 'config'
    config.write_text('Include ' + name + '\n')
    forbidden = config.parent / name
    forbidden.write_text('SYNTHETIC_PRIVATE_VALUE\n')
    forbidden.chmod(0o600)
    manifest = manifest_for(tmp_path, [config, forbidden], [config])
    opened = []
    original_open = os.open

    def tracked_open(path, *args, **kwargs):
        opened.append(Path(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, 'open', tracked_open)
    with pytest.raises(MeasurementUnresolved):
        ReadOnlyConfigProjection(manifest)
    assert forbidden not in opened


@pytest.mark.parametrize('body', [
    '-----BEGIN OPENSSH PRIVATE KEY-----\nSYNTHETIC_PRIVATE_VALUE\n',
    'synthetic.invalid ssh-ed25519 SYNTHETIC_PUBLIC_KEY\n',
])
def test_key_or_store_content_is_not_copied_even_with_conf_extension(tmp_path, body):
    config = tmp_path / '.ssh' / 'config'
    config.write_text('Include extra.conf\n')
    extra = config.parent / 'extra.conf'
    extra.write_text(body)
    with pytest.raises(MeasurementUnresolved):
        ReadOnlyConfigProjection(manifest_for(tmp_path, [config, extra], [config]))


def test_native_system_and_user_roots_keep_separate_include_bases(tmp_path, monkeypatch):
    config = tmp_path / '.ssh' / 'config'
    config.write_text('Include extra.conf\n')
    user_extra = config.parent / 'extra.conf'
    user_extra.write_text('Host user-synthetic\n')
    system = tmp_path / 'system' / 'ssh_config'
    system.parent.mkdir()
    system.write_text('Include extra.conf\n')
    system_extra = system.parent / 'extra.conf'
    system_extra.write_text('Host system-synthetic\n')
    monkeypatch.setattr(config_module, 'SSH_SYSTEM_CONFIG_FILE', str(system))
    manifest = manifest_for(tmp_path, [config, user_extra, system, system_extra], [config, system])
    with ReadOnlyConfigProjection(manifest) as projection:
        destinations = {dest for source, dest in projection.bindings() if Path(source).is_file()}
        assert destinations == {str(config), str(user_extra), str(system), str(system_extra)}


@pytest.mark.parametrize('role', ['known_hosts', 'id_ed25519'])
def test_manifest_cannot_choose_a_credential_role_as_a_native_config_root(tmp_path, role):
    config = tmp_path / '.ssh' / 'config'
    config.write_text('Host synthetic\n')
    manifest = manifest_for(tmp_path, [config], [config])
    manifest['roots'] = [role]
    with pytest.raises(MeasurementUnresolved):
        ReadOnlyConfigProjection(manifest)


def test_failed_copy_cleanup_is_explicit_one_shot_and_preserves_primary_failure(tmp_path, monkeypatch):
    config = tmp_path / '.ssh' / 'config'
    config.write_text('Host synthetic\n')
    projection = ReadOnlyConfigProjection(manifest_for(tmp_path, [config], [config]))
    directory = Path(projection.bindings()[0][0])
    calls = []

    def unavailable(*args, **kwargs):
        calls.append(True)
        raise OSError('SYNTHETIC_PRIVATE_CLEANUP_VALUE')

    with monkeypatch.context() as patch:
        patch.setattr(shutil, 'rmtree', unavailable)
        with pytest.raises(ValueError, match='primary'):
            with projection:
                raise ValueError('primary')
        assert projection.close() is False
        assert calls == [True]
    # Explicit recovery of this synthetic test fixture, after observing failure;
    # production close() does not silently retry an uncertain removal.
    shutil.rmtree(directory)


@pytest.mark.parametrize('name', ['id_ed25519', 'known_hosts'])
def test_config_symlink_cannot_launder_a_credential_target_name(tmp_path, monkeypatch, name):
    config = tmp_path / '.ssh' / 'config'
    target = config.parent / name
    target.write_text('SYNTHETIC_PRIVATE_VALUE\n')
    target.chmod(0o600)
    config.symlink_to(target.name)
    manifest = manifest_for(tmp_path, [config], [config])
    opened = []
    original_open = os.open

    def tracked_open(path, *args, **kwargs):
        opened.append(Path(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(os, 'open', tracked_open)
    with pytest.raises(MeasurementUnresolved):
        ReadOnlyConfigProjection(manifest)
    assert target not in opened


def test_constructor_failure_retains_primary_and_private_recovery_key_when_cleanup_fails(tmp_path, monkeypatch):
    config = tmp_path / '.ssh' / 'config'
    config.write_text('Host synthetic\n')
    manifest = manifest_for(tmp_path, [config], [config])
    original_open = os.open
    primary = OSError('SYNTHETIC_PRIVATE_PREPARATION_VALUE')
    reads = []

    def refuse_post_copy_read(path, *args, **kwargs):
        if Path(path) == config:
            reads.append(True)
            if len(reads) == 2:
                raise primary
        return original_open(path, *args, **kwargs)

    def refuse_removal(*args, **kwargs):
        raise OSError('SYNTHETIC_PRIVATE_REMOVAL_VALUE')

    with monkeypatch.context() as patch:
        patch.setattr(os, 'open', refuse_post_copy_read)
        patch.setattr(shutil, 'rmtree', refuse_removal)
        with pytest.raises(ConfigProjectionRefused) as caught:
            ReadOnlyConfigProjection(manifest)
    failure = caught.value
    assert failure.primary_error is primary
    assert failure.primary_failure_class == 'OSError'
    assert failure.cleanup_state == 'UNKNOWN'
    directory = Path(failure.retained_directory)
    assert directory.parent == Path('/tmp')
    assert directory.name.startswith('odysseus-measure-config-')
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert (directory / '0').read_bytes() == config.read_bytes()
    assert 'SYNTHETIC_PRIVATE_' not in str(failure)
    shutil.rmtree(directory)  # Explicit synthetic-fixture recovery after UNKNOWN.


@pytest.mark.parametrize('line', [
    'Host ssh-synthetic secondary',
    'HostKeyAlgorithms ssh-ed25519',
    'PubkeyAcceptedAlgorithms ssh-ed25519',
])
def test_native_host_and_option_values_are_not_reclassified_as_key_records(tmp_path, line):
    config = tmp_path / '.ssh' / 'config'
    config.write_text(line + '\n')
    with ReadOnlyConfigProjection(manifest_for(tmp_path, [config], [config])) as projection:
        projected = next(Path(source) for source, dest in projection.bindings() if dest == str(config))
        assert projected.read_text() == line + '\n'
