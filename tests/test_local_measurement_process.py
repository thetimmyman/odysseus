"""Source controls only: no broker, systemd unit, SSH or provider calls."""
import hashlib
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
