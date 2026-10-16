"""Source controls only: no broker, systemd unit, SSH or provider calls."""
import hashlib
import importlib.machinery
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from src.local_measurement_process import SystemdProcesses
from src.local_measurement_owner import MeasurementUnresolved


def test_no_process_or_request_is_admitted_before_operational_boundary_proof():
    processes = SystemdProcesses(worker_command=['unused'], broker_worker=['unused'])
    assert processes.qualified is False
    with pytest.raises(MeasurementUnresolved):
        processes.identity(['unused'], lease_valid=lambda: True)
    with pytest.raises(MeasurementUnresolved):
        processes.request('http://synthetic.invalid', {}, lease_valid=lambda: True)
    with pytest.raises(MeasurementUnresolved):
        processes.post('/acquire', {})


def test_identity_preserves_containment_and_adds_only_checked_readonly_projection(monkeypatch):
    """The actual command interface, intercepted before any process can start."""
    class Projection:
        checks = 0

        def bindings(self):
            self.checks += 1
            return (('/synthetic/private', '/synthetic/private'),
                    ('/synthetic/private/0', '/synthetic/config'))

        def verify(self):
            self.checks += 1

        def proof_command(self):
            return ['synthetic-proof']

    projection = Projection()
    processes = SystemdProcesses(worker_command=['unused'], broker_worker=['unused'],
                                  config_projection=projection)
    observed = []

    async def refuse_start(*args, **kwargs):
        observed.append(args)
        raise OSError('synthetic unavailable manager')

    monkeypatch.setattr('asyncio.create_subprocess_exec', refuse_start)
    # A refused local proof must include the same projection as an identity
    # capture, and must not grant a qualified adapter.
    with pytest.raises(MeasurementUnresolved):
        processes.qualify_no_provider()
    assert len(observed) == 1
    argv = observed[0]
    for required in ('ProtectControlGroups=yes', 'NoNewPrivileges=yes',
                     'KillMode=control-group', 'ExitType=cgroup', 'MemoryMax=1G',
                     'MemorySwapMax=0', 'TasksMax=32', 'CPUQuota=100%'):
        assert '--property=' + required in argv
    assert '--property=BindReadOnlyPaths=/synthetic/private:/synthetic/private:norbind' in argv
    assert '--property=BindReadOnlyPaths=/synthetic/private/0:/synthetic/config:norbind' in argv
    assert projection.checks > 0
    assert processes.qualified is False


@pytest.mark.parametrize('body,declared,accepted', [
    (b'{"ok":true}', 11, True),
    (b'{"ok":true}', 12, False),
    (b'{"ok":true}', 10, False),
    (b'{"ok":false,"ok":true}', 22, False),
    (b'{"ok":NaN}', 10, False),
    (b' '*16385, 16385, False),
])
def test_worker_bounds_normal_client_json_before_decoding(tmp_path, body, declared, accepted):
    # A normal-client interface fixture; no socket, service or live client.
    client = tmp_path/'normal-client'
    client.write_text('import json\nfrom email.message import Message\n'
        'class Response:\n'
        '    headers=Message()\n'
        f'    headers["Content-Length"]={str(declared)!r}\n'
        f'    def read(self, size): return {body!r}[:size]\n'
        'def _post(path, body, timeout): return 200,json.load(Response())\n')
    command = [sys.executable, '-I', str(Path('scripts/odysseus-measure-once').resolve()),
        '--worker-broker', '--lease-client', str(client), '--lease-client-sha256',
        hashlib.sha256(client.read_bytes()).hexdigest()]
    result = subprocess.run(command, input=b'{"path":"/heartbeat","body":{"lease":"synthetic"}}',
                            capture_output=True, timeout=5)
    if accepted:
        assert result.returncode == 0
        assert json.loads(result.stdout) == {'status':200, 'body':{'ok':True}}
    else:
        assert result.returncode == 2
        assert result.stdout == b''
        assert b'no receipt applied' in result.stderr


def test_invalid_worker_source_refuses_before_any_unit(tmp_path):
    command = [sys.executable, '-I', str(Path('scripts/odysseus-measure-once').resolve()),
        '--worker-broker', '--lease-client', str(tmp_path/'absent'), '--lease-client-sha256', 'a'*64]
    result = subprocess.run(command, input=b'{"path":"/heartbeat","body":{}}',
                            capture_output=True, timeout=5)
    assert result.returncode == 2 and result.stdout == b''


def test_unit_reconciliation_key_is_flushed_before_start_even_when_start_is_unknown(monkeypatch, capsys):
    processes = SystemdProcesses(worker_command=['unused'], broker_worker=['unused'])
    observed = []

    async def unavailable(*args, **kwargs):
        diagnostic = json.loads(capsys.readouterr().err)['measurement_process']
        assert diagnostic['kind'] == 'proof-normal'
        assert diagnostic['local_stop_proven'] is False
        assert diagnostic['remote_stop_proven'] is False
        assert set(diagnostic) == {'unit', 'kind', 'local_stop_proven',
                                   'remote_stop_proven', 'launcher_exit_code'}
        assert diagnostic['unit'].startswith('odysseus-measure-')
        observed.append(diagnostic)
        raise OSError('manager outcome unavailable')

    monkeypatch.setattr('asyncio.create_subprocess_exec', unavailable)
    with pytest.raises(MeasurementUnresolved):
        processes.qualify_no_provider()
    assert processes.evidence() == observed
    assert processes.qualified is False


@pytest.mark.parametrize('args', [['--changed-route'], ['--current-spec','unused'],
                                 ['--qualify-boundary','--changed-route','--current-spec','unused']])
def test_route_cli_requires_explicit_paired_mode_before_any_execution(args):
    result = subprocess.run([sys.executable, '-I', str(Path('scripts/odysseus-measure-once').resolve()),
                             *args], capture_output=True, timeout=5)
    assert result.returncode == 2 and result.stdout == b''
    assert b'route mode' in result.stderr


@pytest.mark.parametrize('args', [
    ['--qualify-boundary', '--ssh-config-manifest', 'unused'],
    ['--qualify-boundary', '--ssh-config-manifest-sha256', 'a' * 64],
    ['--worker-http', '--ssh-config-manifest', 'unused', '--ssh-config-manifest-sha256', 'a' * 64],
])
def test_projection_cli_requires_pinned_pair_and_controller_context(args):
    result = subprocess.run([sys.executable, '-I', str(Path('scripts/odysseus-measure-once').resolve()),
                             *args], capture_output=True, timeout=5)
    assert result.returncode == 2 and result.stdout == b''
    assert b'config projection' in result.stderr


def test_primary_identity_refusal_survives_a_secondary_configuration_postcheck():
    class Projection:
        def verify(self):
            raise MeasurementUnresolved('secondary postcheck')

    processes = SystemdProcesses(worker_command=[], broker_worker=[], config_projection=Projection())
    with pytest.raises(MeasurementUnresolved, match='not qualified'):
        processes.identity(['never-started'], lease_valid=lambda: True)
    assert processes.configuration_evidence() == 'POSTCHECK_REFUSED'


def test_cli_cleanup_failure_retains_primary_refusal_and_never_exposes_exception_text(tmp_path, monkeypatch, capsys):
    path = Path('scripts/odysseus-measure-once').resolve()
    loader = importlib.machinery.SourceFileLoader('projection_failure_cli', str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)

    class Projection:
        def __init__(self, unused):
            pass

        def close(self):
            raise OSError('SYNTHETIC_PRIVATE_CLEANUP_VALUE')

    class Processes:
        def __init__(self, **unused):
            pass

        def qualify_no_provider(self):
            raise MeasurementUnresolved('primary refusal')

    monkeypatch.setattr(module, 'ReadOnlyConfigProjection', Projection)
    monkeypatch.setattr(module, 'SystemdProcesses', Processes)
    manifest = tmp_path / 'manifest.json'
    manifest.write_text('{}')
    manifest.chmod(0o600)
    result = module.main(['--qualify-boundary', '--ssh-config-manifest', str(manifest),
                         '--ssh-config-manifest-sha256', hashlib.sha256(manifest.read_bytes()).hexdigest()])
    assert result == 2
    captured = capsys.readouterr()
    assert 'SYNTHETIC_PRIVATE_CLEANUP_VALUE' not in captured.err + captured.out
    assert 'configuration cleanup UNKNOWN' in captured.err
