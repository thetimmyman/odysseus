"""Exact read-only SSH config copies for owned identity captures.

Host ownership is checked in the controller's namespace. The native SSH parser
still reads its original paths; a unit-specific bind presents caller-owned
copies so an unprivileged user namespace never has to trust an unmapped owner.
This module grants no routing, authentication or qualification authority.
"""
from __future__ import annotations

import hashlib
import glob
import json
import os
import pwd
from pathlib import Path
import re
import shlex
import stat
import sys
import tempfile

from src.local_measurement_owner import MeasurementUnresolved
from src.constants import LOCAL_MEASUREMENT_CONFIG_TEMP_ROOT, SSH_SYSTEM_CONFIG_FILE

_FILE_BYTES = 65536
_TOTAL_BYTES = 262144
_PATH = re.compile(r'/[A-Za-z0-9_./-]+\Z')
_PROJECTION_PROOF = '''import errno, hashlib, json, os, stat, sys
try:
    value = json.loads(sys.argv[1])
    root = value['directory']
    meta = os.lstat(root)
    assert stat.S_ISDIR(meta.st_mode) and meta.st_uid == os.getuid()
    assert stat.S_IMODE(meta.st_mode) == 0o700
    assert sorted(os.listdir(root)) == sorted(value['copy_names'])
    assert os.statvfs(root).f_flag & os.ST_RDONLY
    for entry in value['files']:
        path = entry['path']
        assert (os.readlink(path) if os.path.islink(path) else None) == entry['link']
        assert os.path.realpath(path) == entry['resolved_path']
        meta = os.stat(path)
        assert stat.S_ISREG(meta.st_mode) and meta.st_uid == os.getuid()
        assert stat.S_IMODE(meta.st_mode) == 0o600 and meta.st_size <= 65536
        with open(path, 'rb') as stream:
            raw = stream.read(65537)
        assert len(raw) <= 65536 and hashlib.sha256(raw).hexdigest() == entry['sha256']
        assert os.statvfs(path).f_flag & os.ST_RDONLY
        try:
            fd = os.open(entry['resolved_path'], os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as error:
            assert error.errno == errno.EROFS
        else:
            os.close(fd)
            raise ValueError('readonly proof refused')
    print('owned-output')
    print('owned-error', file=sys.stderr)
except Exception:
    print('configuration projection proof refused', file=sys.stderr)
    raise SystemExit(2)
'''


def _path(value):
    if (type(value) is not str or len(value) > 2048 or not _PATH.fullmatch(value)
            or '..' in Path(value).parts or str(Path(value)) != value):
        raise ValueError('unsupported configuration path')
    return Path(value)


def _stamp(meta):
    return (meta.st_dev, meta.st_ino, meta.st_uid, meta.st_gid,
            meta.st_mode, meta.st_size, meta.st_mtime_ns, meta.st_ctime_ns)


def _read_original(entry):
    path = _path(entry['path'])
    resolved = _path(entry['resolved_path'])
    link_meta = path.lstat()
    if (not (stat.S_ISREG(link_meta.st_mode) or stat.S_ISLNK(link_meta.st_mode))
            or link_meta.st_uid not in (0, os.getuid())
            or (os.readlink(path) if path.is_symlink() else None) != entry['link']
            or path.resolve(strict=True) != resolved):
        raise ValueError('configuration link changed')
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        meta = os.fstat(stream.fileno())
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid not in (0, os.getuid())
                or meta.st_mode & 0o022 or meta.st_uid != entry['uid']
                or stat.S_IMODE(meta.st_mode) != entry['mode'] or meta.st_size > _FILE_BYTES):
            raise ValueError('configuration ownership or mode refused')
        raw = stream.read(_FILE_BYTES + 1)
        if (len(raw) > _FILE_BYTES or hashlib.sha256(raw).hexdigest() != entry['sha256']
                or _stamp(os.fstat(stream.fileno())) != _stamp(meta)):
            raise ValueError('configuration source changed')
    if _stamp(path.lstat()) != _stamp(link_meta) or path.resolve(strict=True) != resolved:
        raise ValueError('configuration link changed')
    return raw, (_stamp(link_meta), _stamp(meta))


class ReadOnlyConfigProjection:
    """One explicit, pinned configuration closure; construction starts no process.

    Use as a context manager. bindings() and verify() recheck original sources
    outside the unit and private copies. Returned pairs are for systemd's
    BindReadOnlyPaths only. No key, known-host or credential file is copied.
    """

    def __init__(self, manifest):
        self._temporary = None
        self._sources = []
        self._copies = []
        self._roots = []
        self._cleanup_attempted = False
        self._cleanup_complete = False
        try:
            if (type(manifest) is not dict
                    or set(manifest) != {'schema', 'home', 'roots', 'files'}
                    or manifest['schema'] != 'PINNED_SSH_CONFIG_V1'
                    or type(manifest['files']) is not list or not 1 <= len(manifest['files']) <= 4
                    or type(manifest['roots']) is not list or not 1 <= len(manifest['roots']) <= 2):
                raise ValueError('configuration manifest refused')
            self._home = str(_path(pwd.getpwuid(os.getuid()).pw_dir))
            if manifest['home'] != self._home:
                raise ValueError('configuration home is not the native SSH home')
            for role in manifest['roots']:
                if type(role) is not str or role not in ('user', 'system'):
                    raise ValueError('configuration root refused')
                root = (Path(self._home) / '.ssh' / 'config' if role == 'user'
                        else _path(SSH_SYSTEM_CONFIG_FILE))
                self._roots.append((str(root), str(root.parent)))
            if len(set(name for name, _ in self._roots)) != len(self._roots):
                raise ValueError('duplicate configuration root')
            seen = set()
            total = 0
            for value in manifest['files']:
                if (type(value) is not dict
                        or set(value) != {'path', 'resolved_path', 'link', 'uid', 'mode', 'sha256'}
                        or type(value['uid']) is not int or type(value['mode']) is not int
                        or value['link'] is not None and type(value['link']) is not str
                        or type(value['sha256']) is not str
                        or not re.fullmatch('[0-9a-f]{64}', value['sha256'])):
                    raise ValueError('configuration entry refused')
                entry = dict(value)
                roots = {path for path, _ in self._roots}
                if (entry['path'] in roots and Path(entry['resolved_path']).name not in ('config', 'ssh_config')
                        and Path(entry['resolved_path']).suffix != '.conf'):
                    raise ValueError('native configuration symlink target is not a config source')
                if (entry['path'] not in roots
                        and (Path(entry['path']).suffix != '.conf'
                             or Path(entry['resolved_path']).suffix != '.conf')):
                    raise ValueError('only declared SSH config roots and conf Includes are supported')
                if entry['path'] in seen:
                    raise ValueError('duplicate configuration path')
                seen.add(entry['path'])
                raw, stamp = _read_original(entry)
                total += len(raw)
                if total > _TOTAL_BYTES:
                    raise ValueError('configuration closure exceeds budget')
                self._sources.append((entry, raw, stamp))
            self._check_closure()
            self._temporary = tempfile.TemporaryDirectory(prefix='odysseus-measure-config-',
                dir=LOCAL_MEASUREMENT_CONFIG_TEMP_ROOT)
            destinations = set()
            for ordinal, (entry, raw, _) in enumerate(self._sources):
                if entry['resolved_path'] in destinations:
                    continue
                destinations.add(entry['resolved_path'])
                copy = Path(self._temporary.name) / str(ordinal)
                with copy.open('xb') as stream:
                    os.chmod(copy, 0o600)
                    stream.write(raw)
                self._copies.append((copy, entry['resolved_path'], raw))
            self.verify()
        except Exception:
            self.close()
            raise MeasurementUnresolved('SSH configuration projection refused') from None

    def _check_closure(self):
        # Conservative Include discovery only. Host/Match selection, option
        # precedence and the effective target remain the native SSH parser's job.
        expected = {entry['path']: raw for entry, raw, _ in self._sources}
        found = {}
        patterns = 0

        def visit(name, base, depth=0):
            nonlocal patterns
            if name in found:
                if found[name] != base:
                    raise ValueError('ambiguous Include base')
                return
            if name not in expected or depth > 8:
                raise ValueError('configuration closure incomplete')
            found[name] = base
            for line in expected[name].decode('utf-8', errors='strict').splitlines():
                parts = shlex.split(line, comments=True)
                if not parts:
                    continue
                if '=' in parts[0]:
                    parts = parts[0].split('=', 1) + parts[1:]
                if len(parts) > 1 and parts[1] == '=':
                    parts.pop(1)
                if not re.fullmatch('[A-Za-z][A-Za-z0-9]*', parts[0]):
                    raise ValueError('not an SSH configuration directive')
                if len(parts) > 1 and re.match(r'(?:ssh-|ecdsa-|sk-)', parts[1]):
                    raise ValueError('key/store records are not configuration inputs')
                if parts[0].lower() != 'include':
                    continue
                if len(parts) < 2:
                    raise ValueError('empty Include refused')
                for pattern in parts[1:]:
                    patterns += 1
                    if patterns > 32 or any(x in pattern for x in ('%', '$', '`', '\\')):
                        raise ValueError('dynamic Include refused')
                    if pattern.startswith('~/'):
                        pattern = self._home + pattern[1:]
                    elif pattern.startswith('~'):
                        raise ValueError('named-home Include refused')
                    if not os.path.isabs(pattern):
                        pattern = str(Path(base) / pattern)
                    if glob.has_magic(str(Path(pattern).parent)) or '..' in Path(pattern).parts:
                        raise ValueError('unsupported Include pattern')
                    for child in glob.iglob(pattern):
                        visit(str(_path(child)), base, depth + 1)

        for name, base in self._roots:
            visit(name, base)
        if set(found) != set(expected):
            raise ValueError('configuration closure has extra files')

    def verify(self):
        try:
            if self._temporary is None:
                raise ValueError('configuration projection closed')
            for entry, original, stamp in self._sources:
                raw, current = _read_original(entry)
                if raw != original or current != stamp:
                    raise ValueError('configuration source changed')
            self._check_closure()
            directory = Path(self._temporary.name).stat()
            if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
                raise ValueError('private projection directory changed')
            for copy, _, original in self._copies:
                raw, _ = _read_original({'path': str(copy), 'resolved_path': str(copy),
                    'link': None, 'uid': os.getuid(), 'mode': 0o600,
                    'sha256': hashlib.sha256(original).hexdigest()})
                if raw != original:
                    raise ValueError('private projection changed')
        except Exception:
            raise MeasurementUnresolved('SSH configuration projection changed or unavailable') from None

    def bindings(self):
        self.verify()
        # Also protect the backing directory at its own path. Protecting only
        # the SSH destination would leave an alternate writable source path.
        return ((self._temporary.name, self._temporary.name),) + tuple(
            (str(copy), destination) for copy, destination, _ in self._copies)

    def proof_command(self):
        """Same-unit proof; writable copies or permission denial cannot pass it."""
        self.verify()
        value = {'directory': self._temporary.name,
                 'copy_names': [copy.name for copy, _, _ in self._copies],
                 'files': [{key: entry[key] for key in ('path', 'resolved_path', 'link', 'sha256')}
                           for entry, _, _ in self._sources]}
        return [sys.executable, '-I', '-c', _PROJECTION_PROOF, json.dumps(value)]

    def close(self):
        if self._cleanup_attempted:
            return self._cleanup_complete
        self._cleanup_attempted = True
        try:
            if self._temporary is not None:
                self._temporary.cleanup()
                self._temporary = None
            self._cleanup_complete = True
        except Exception:
            self._cleanup_complete = False
        return self._cleanup_complete

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        if not self.close() and unused[0] is None:
            raise MeasurementUnresolved('configuration cleanup unresolved')
