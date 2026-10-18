"""Source interfaces only; no live SSH, manager, Docker, broker or provider."""
import os
import asyncio
import json
from pathlib import Path
import subprocess
import sys
import time
import hashlib
import importlib.machinery
import importlib.util
import io
import socket
import builtins
import pwd
import threading
import shutil
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from tests.helpers.docker_cli import block_native_transports

from src.local_measurement_owner import MeasurementUnresolved


def transport_manifest():
    from src.local_measurement_transport import EXECUTOR_SOURCE_FILES
    return {"schema": "canonical-tmos-owner-v1", "packages": {"routing-inspector-command": {}},
            "runtime": {"identity_commands": {"synthetic-target": ["/synthetic/identity"]},
                "execution_transport": {"kind": "ssh", "host": "synthetic-executor",
                    "python": "/usr/bin/python3", "command": "/synthetic/source/scripts/odysseus-measure-once",
                    "source_pins": {path: "a" * 64 for path in EXECUTOR_SOURCE_FILES},
                    "executor": {"hostname": "synthetic-executor", "uid": 1000, "gid": 1000,
                        "groups": [1000], "boot_id": "00000000-0000-4000-8000-000000000001",
                        "python_sha256": "b" * 64},
                    "broker_url": "http://synthetic-broker.invalid:8745",
                    "provider_endpoints": ["http://synthetic-provider.invalid:11434"],
                    "ssh_config_projection": None}}}


def test_owned_helper_rejects_valid_inert_json_before_any_native_operation(tmp_path):
    data = tmp_path / "native-data"
    command = [sys.executable, "-I", "-S", "-B",
               str(Path("scripts/odysseus-measure-once").resolve()),
               "--owned-executor", "--owner-manifest-sha256", "a" * 64]
    result = subprocess.run(command, input=b"{}", capture_output=True, timeout=5,
                            env={**os.environ, "ODYSSEUS_DATA_DIR": str(data)})
    assert result.returncode == 2
    assert result.stdout == b""
    assert result.stderr == b"measurement refused: MeasurementUnresolved; no receipt applied\n"
    assert not data.exists()


def test_explicit_transport_builds_only_the_fixed_ssh_helper_without_executing(tmp_path, monkeypatch):
    from src.local_measurement_transport import SshTransport
    monkeypatch.setenv("ODYSSEUS_DATA_DIR", str(tmp_path / "unused"))
    manifest = transport_manifest()
    transport = SshTransport(manifest, run_id="00000000-0000-4000-8000-000000000002",
                             packet_id="routing-inspector-command", deadline=time.monotonic() + 180)
    argv = transport.argv
    assert argv[:2] == ("/usr/bin/ssh", "-T")
    assert argv[-2] == "synthetic-executor"
    assert argv[-1].startswith("/usr/bin/python3 -I /synthetic/source/scripts/odysseus-measure-once --owned-executor --owner-manifest-sha256 ")
    for option in ("BatchMode=yes", "StrictHostKeyChecking=yes", "ProxyCommand=none",
                   "ProxyJump=none", "ForwardAgent=no", "PermitLocalCommand=no", "ConnectTimeout=2"):
        assert option in argv
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("endpoint", ["http://synthetic\nprovider.invalid", "http://synthetic provider.invalid"])
def test_transport_refuses_ambiguous_endpoint_text_before_execution(endpoint):
    from src.local_measurement_transport import SshTransport
    manifest = transport_manifest()
    manifest["runtime"]["execution_transport"]["provider_endpoints"] = [endpoint]
    with pytest.raises(MeasurementUnresolved):
        SshTransport(manifest, run_id="00000000-0000-4000-8000-000000000002",
                     packet_id="routing-inspector-command", deadline=time.monotonic() + 180)


@pytest.mark.parametrize("conditional", [False, True], ids=["static-root", "executable-match"])
def test_native_ssh_configuration_is_pinned_and_nonexecuting(tmp_path, monkeypatch, conditional):
    from src.local_measurement_process import SystemdProcesses
    from src.local_measurement_transport import SshTransport
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    config = home / ".ssh/config"
    config.write_text('Host synthetic-executor\n  HostName synthetic.invalid\n' + ('Match exec "false"\n' if conditional else ''))
    config.chmod(0o600)
    monkeypatch.setattr(pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(home)))
    manifest = transport_manifest()
    manifest["runtime"]["execution_transport"]["ssh_config_projection"] = {
        "schema": "PINNED_SSH_CONFIG_V1", "home": str(home), "roots": ["user"],
        "files": [{"path": str(config), "resolved_path": str(config), "link": None,
                   "uid": os.getuid(), "mode": 0o600, "sha256": hashlib.sha256(config.read_bytes()).hexdigest()}]}
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    monkeypatch.setattr("src.local_measurement_transport.CANONICAL_INSPECTOR_WORK_ROOT", work)
    transport = SshTransport(manifest, run_id="00000000-0000-4000-8000-000000000002",
                             packet_id="routing-inspector-command", deadline=time.monotonic() + 180)
    peer = SyntheticExecutor(manifest["runtime"]["execution_transport"]["executor"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", peer.start)
    processes = SystemdProcesses(worker_command=["unused-http"], broker_worker=["unused-broker"], transport=transport)
    if conditional:
        with pytest.raises(MeasurementUnresolved):
            processes.qualify_no_provider()
        assert peer.calls == []
    else:
        processes.qualify_no_provider()
        assert peer.calls[0].argv[2:4] == ("-F", str(config))
        assert processes.configuration_evidence() == "REMOVED"


class SyntheticExecutor:
    """External SSH process fixture; its cgroups exist only as protocol facts."""

    def __init__(self, identity):
        self.identity, self.units, self.calls = identity, {}, []
        self.provider_output = b'{"done":true}\n'
        self.broker_output = b'{"status":200,"body":{"ok":true}}\n'
        self.state_delay_seconds = 0
        self.descendant_member = True
        self.populated_override = "derived"
        self.birth_fault = None
        self.provider_entered = None
        self.heartbeat_entered = None

    async def start(self, *argv, **options):
        assert argv[0] == "/usr/bin/ssh"
        process = SyntheticSshClient(self, argv, options)
        self.calls.append(process)
        return process

    def respond(self, process, value):
        process.envelope = value
        unit, action = value["unit"], value["operation"]
        if action == "launch":
            assert unit not in self.units
            birth = {"schema": "owned-measurement-birth-v1", "unit": unit,
                "capture_nonce": value["capture_nonce"], "run_id": value["run_id"],
                "packet_id": value["packet_id"], "owner_manifest_digest": value["owner_manifest_digest"],
                "invocation_id": "1" * 32, "cgroup": "/synthetic.slice/" + unit,
                "pid": 32001, "start_ticks": 12345, "executor": self.identity}
            descendant = value["kind"] == "proof-descendant"
            self.units[unit] = {"birth": birth, "active": descendant}
            frame = json.dumps(birth).encode() + b"\n"
            if self.birth_fault == 'wrong-run':
                frame = json.dumps({**birth, 'run_id': '00000000-0000-4000-8000-000000000003'}).encode() + b'\n'
            elif self.birth_fault == 'partial':
                frame = b'{"schema":'
            elif self.birth_fault == 'oversized':
                frame = b'x' * 5000 + b'\n'
            process.stdout.feed_data(frame)
            output = self.provider_output if value["kind"] == "provider" else b"owned-output\n"
            if value['kind'] == 'broker':
                output = self.broker_output
                if value['payload']['path'] == '/heartbeat' and self.heartbeat_entered is not None:
                    self.heartbeat_entered.set()
            process.stdout.feed_data(b"9001\n" if descendant else output)
            if not descendant:
                if value["kind"] == "proof-normal":
                    process.stderr.feed_data(b"owned-error\n")
                process.stdout.feed_eof(); process.stderr.feed_eof()
                process.done = True
            return
        row = self.units[unit]
        assert value["birth"] == row["birth"]
        if action == "kill":
            row["active"] = False
            for launched in self.calls:
                if (launched.envelope is not None and launched.envelope['operation'] == 'launch'
                        and launched.envelope['unit'] == unit and not launched.done):
                    launched.done = True
                    launched.stdout.feed_eof(); launched.stderr.feed_eof()
        if action == "membership":
            response = {"birth": row["birth"], "pid": value["pid"],
                        "member": value["pid"] == 9001 and self.descendant_member}
        else:
            response = {"birth": row["birth"], "LoadState": "loaded",
                "ActiveState": "active" if row["active"] else "inactive", "SubState": "running" if row["active"] else "dead",
                "ControlGroup": row["birth"]["cgroup"], "ExitType": "cgroup", "KillMode": "control-group",
                "configuration_cleanup": "NOT_CONFIGURED",
                "populated": (1 if row["active"] else 0) if self.populated_override == "derived" else self.populated_override}
        process.stdout.feed_data(json.dumps(response).encode())
        process.stdout.feed_eof(); process.stderr.feed_eof()
        process.done = True


class SyntheticSshClient:
    def __init__(self, peer, argv, options):
        self.peer, self.argv, self.options = peer, argv, options
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        self.returncode, self.done, self.envelope = None, False, None
        self.stdin = self
        self.input = bytearray()

    def write(self, data):
        self.input.extend(data)

    async def drain(self):
        return None

    def close(self):
        self.peer.respond(self, json.loads(self.input))

    async def wait(self):
        if not self.done:
            raise AssertionError("a detached descendant has not been stopped")
        if self.returncode is None:
            if self.envelope is not None and self.envelope.get('kind') == 'provider' and self.peer.provider_entered is not None:
                self.peer.provider_entered.set()
                assert await asyncio.to_thread(self.peer.heartbeat_entered.wait, 2)
            if self.envelope is not None and self.envelope["operation"] == "state":
                await asyncio.sleep(self.peer.state_delay_seconds)
            self.returncode = 0
        return self.returncode

    def kill(self):
        self.returncode, self.done = -9, True
        self.stdout.feed_eof(); self.stderr.feed_eof()


def test_public_capture_qualifies_only_from_current_executor_membership_and_stop(tmp_path, monkeypatch):
    from src.local_measurement_process import SystemdProcesses
    from src.local_measurement_transport import SshTransport
    work = tmp_path / "owned-work"
    work.mkdir(mode=0o700)
    monkeypatch.setattr("src.local_measurement_transport.CANONICAL_INSPECTOR_WORK_ROOT", work, raising=False)
    manifest = transport_manifest()
    transport = SshTransport(manifest, run_id="00000000-0000-4000-8000-000000000002",
                             packet_id="routing-inspector-command", deadline=time.monotonic() + 180)
    peer = SyntheticExecutor(manifest["runtime"]["execution_transport"]["executor"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", peer.start)
    original_read = Path.read_text

    def refuse_app_kernel_substitution(path, *args, **options):
        assert not str(path).startswith(("/proc/", "/sys/fs/cgroup/"))
        return original_read(path, *args, **options)

    monkeypatch.setattr(Path, "read_text", refuse_app_kernel_substitution)
    processes = SystemdProcesses(worker_command=["unused-http"], broker_worker=["unused-broker"], transport=transport)
    assert processes.qualified is False
    result = processes.qualify_no_provider()
    assert processes.qualified is True
    assert result["provider_calls"] == 0
    assert result["executor_descendant_termination"] is True
    assert result["provider_cancellation"] is False
    assert [p.envelope["kind"] for p in peer.calls if p.envelope["operation"] == "launch"] == ["proof-normal", "proof-descendant"]
    assert any(p.envelope["operation"] == "membership" for p in peer.calls)
    assert all(p.returncode is not None for p in peer.calls)
    assert all(row["local_stop_proven"] and row["remote_stop_proven"] for row in processes.evidence())


def test_uncertain_response_blocks_the_next_request_without_another_launch(tmp_path, monkeypatch):
    from src.local_measurement_process import SystemdProcesses
    from src.local_measurement_transport import SshTransport
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    monkeypatch.setattr("src.local_measurement_transport.CANONICAL_INSPECTOR_WORK_ROOT", work)
    manifest = transport_manifest()
    transport = SshTransport(manifest, run_id="00000000-0000-4000-8000-000000000002",
                             packet_id="routing-inspector-command", deadline=time.monotonic() + 180)
    peer = SyntheticExecutor(manifest["runtime"]["execution_transport"]["executor"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", peer.start)
    processes = SystemdProcesses(worker_command=["unused-http"], broker_worker=["unused-broker"], transport=transport)
    processes.qualify_no_provider()
    peer.provider_output = b'{"incomplete"'
    url = "http://synthetic-provider.invalid:11434/api/chat"
    with pytest.raises(MeasurementUnresolved):
        processes.request(url, {}, lease_valid=lambda: True)
    before = len(peer.calls)
    with pytest.raises(MeasurementUnresolved):
        processes.request(url, {}, lease_valid=lambda: True)
    assert len(peer.calls) == before


def test_remote_confirmation_uses_its_bounded_five_second_allowance(tmp_path, monkeypatch):
    from src.local_measurement_process import SystemdProcesses
    from src.local_measurement_transport import SshTransport
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    monkeypatch.setattr("src.local_measurement_transport.CANONICAL_INSPECTOR_WORK_ROOT", work)
    manifest = transport_manifest()
    transport = SshTransport(manifest, run_id="00000000-0000-4000-8000-000000000002",
                             packet_id="routing-inspector-command", deadline=time.monotonic() + 180)
    peer = SyntheticExecutor(manifest["runtime"]["execution_transport"]["executor"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", peer.start)
    processes = SystemdProcesses(worker_command=["unused-http"], broker_worker=["unused-broker"], transport=transport)
    processes.qualify_no_provider()
    peer.state_delay_seconds = 2.1
    assert processes.request("http://synthetic-provider.invalid:11434/api/chat", {}, lease_valid=lambda: True) == {"done": True}
    assert all(client.returncode is not None for client in peer.calls)


def test_failed_current_membership_proof_cannot_be_retried_or_reused(tmp_path, monkeypatch):
    from src.local_measurement_process import SystemdProcesses
    from src.local_measurement_transport import SshTransport
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    monkeypatch.setattr("src.local_measurement_transport.CANONICAL_INSPECTOR_WORK_ROOT", work)
    manifest = transport_manifest()
    transport = SshTransport(manifest, run_id="00000000-0000-4000-8000-000000000002",
                             packet_id="routing-inspector-command", deadline=time.monotonic() + 180)
    peer = SyntheticExecutor(manifest["runtime"]["execution_transport"]["executor"])
    peer.descendant_member = False
    monkeypatch.setattr(asyncio, "create_subprocess_exec", peer.start)
    processes = SystemdProcesses(worker_command=["unused-http"], broker_worker=["unused-broker"], transport=transport)
    with pytest.raises(MeasurementUnresolved):
        processes.qualify_no_provider()
    assert processes.qualified is False
    before = len(peer.calls)
    with pytest.raises(MeasurementUnresolved):
        processes.qualify_no_provider()
    assert len(peer.calls) == before


@pytest.mark.parametrize("populated", [False, 0.0])
def test_untyped_empty_cgroup_cannot_complete_a_request(tmp_path, monkeypatch, populated):
    from src.local_measurement_process import SystemdProcesses
    from src.local_measurement_transport import SshTransport
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    monkeypatch.setattr("src.local_measurement_transport.CANONICAL_INSPECTOR_WORK_ROOT", work)
    manifest = transport_manifest()
    transport = SshTransport(manifest, run_id="00000000-0000-4000-8000-000000000002",
                             packet_id="routing-inspector-command", deadline=time.monotonic() + 180)
    peer = SyntheticExecutor(manifest["runtime"]["execution_transport"]["executor"])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", peer.start)
    processes = SystemdProcesses(worker_command=["unused-http"], broker_worker=["unused-broker"], transport=transport)
    processes.qualify_no_provider()
    peer.populated_override = populated
    with pytest.raises(MeasurementUnresolved):
        processes.request("http://synthetic-provider.invalid:11434/api/chat", {}, lease_valid=lambda: True)


@pytest.mark.parametrize("scenario", ['manager-launch', 'unit-birth-and-output', 'control-clean',
    'control-foreign-invocation', 'control-foreign-description', 'control-mismatched-birth',
    'control-missing-manager', 'control-changed-source', 'projection-unproven', 'identity-current', 'identity-stale'])
def test_pinned_helper_launches_only_the_fixed_native_wrapper_after_admission(tmp_path, monkeypatch, capsys, scenario):
    class CollectorExec(BaseException):
        pass

    execute_unit = scenario not in ('manager-launch', 'projection-unproven')
    from src.canonical_inspector_workspace import manifest_digest
    from src.local_measurement_transport import EXECUTOR_SOURCE_FILES
    root = Path.cwd()
    # The manager is simulated. Its interpreter material is a source fixture,
    # not a qualification of whichever Python happens to run this test suite.
    python = tmp_path / "synthetic-python"
    python.write_bytes(b"synthetic executor material; never executed\n")
    python.chmod(0o700)
    monkeypatch.setattr(sys, "executable", str(python))
    manifest = transport_manifest()
    executed = []
    if scenario.startswith('identity-'):
        collector = tmp_path / 'collector.py'
        collector.write_text('print("synthetic identity collector")\n'); collector.chmod(0o600)
        manifest['runtime']['identity_commands']['synthetic-target'] = [sys.executable, '-I', str(collector)]
        manifest['runtime']['identity_source_pins'] = {sys.executable: hashlib.sha256(python.read_bytes()).hexdigest(),
            str(collector): hashlib.sha256(collector.read_bytes()).hexdigest() if scenario == 'identity-current' else 'e' * 64}

        def collector_exec(executable, command, env):
            executed.append(command)
            assert executable == sys.executable and command[-1] == str(collector)
            assert 'PYTHONPATH' not in env and 'DOCKER_HOST' not in env and 'HTTP_PROXY' not in env
            print('{"source":"synthetic"}')
            raise CollectorExec()

        monkeypatch.setattr(os, 'execvpe', collector_exec)
    if scenario == 'projection-unproven':
        home = tmp_path / 'home'; (home / '.ssh').mkdir(parents=True)
        config_file = home / '.ssh/config'
        config_file.write_text('Host synthetic\n  HostName synthetic.invalid\n'); config_file.chmod(0o600)
        monkeypatch.setattr(pwd, 'getpwuid', lambda _uid: SimpleNamespace(pw_dir=str(home)))
        manifest['runtime']['ssh_config_projection'] = {'schema': 'PINNED_SSH_CONFIG_V1',
            'home': str(home), 'roots': ['user'], 'files': [{'path': str(config_file),
                'resolved_path': str(config_file), 'link': None, 'uid': os.getuid(), 'mode': 0o600,
                'sha256': hashlib.sha256(config_file.read_bytes()).hexdigest()}]}
    config = manifest["runtime"]["execution_transport"]
    config["python"], config["command"] = sys.executable, str(root / "scripts/odysseus-measure-once")
    config["source_pins"] = {p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in EXECUTOR_SOURCE_FILES}
    config["executor"] = {"hostname": socket.gethostname(), "uid": os.getuid(), "gid": os.getgid(),
        "groups": sorted(set(os.getgroups())), "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "python_sha256": hashlib.sha256(Path(sys.executable).resolve().read_bytes()).hexdigest()}
    fixed = tmp_path / "tmos-owner.json"
    fixed.write_text(json.dumps(manifest))
    fixed.chmod(0o600)
    monkeypatch.setattr("src.local_measurement_transport.CANONICAL_TMOS_MANIFEST_FILE", fixed, raising=False)
    nonce = "c" * 32
    value = {"schema": "owned-measurement-transport-v1", "operation": "launch",
        "run_id": "00000000-0000-4000-8000-000000000002", "packet_id": "routing-inspector-command",
        "owner_manifest_digest": manifest_digest(manifest), "capture_nonce": nonce,
        "unit": "odysseus-measure-" + nonce + ".service", "seconds": 5,
        "kind": "proof-normal", "payload": None}
    if scenario.startswith('identity-'):
        value.update(kind='identity', payload={'target_id': 'synthetic-target'})
    work = tmp_path / "work"
    work.mkdir(mode=0o700)
    monkeypatch.setattr("src.local_measurement_transport.CANONICAL_INSPECTOR_WORK_ROOT", work)
    path = root / "scripts/odysseus-measure-once"
    loader = importlib.machinery.SourceFileLoader("synthetic_owned_executor_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    cli = importlib.util.module_from_spec(spec)
    loader.exec_module(cli)
    calls = []
    original_open = builtins.open

    def native_process(argv, **options):
        assert argv[:4] == ["systemd-run", "--user", "--pipe", "--wait"]
        assert "--unit=" + value["unit"] in argv
        assert "--property=ExitType=cgroup" in argv
        assert "--property=KillMode=control-group" in argv
        assert "--property=Description=odysseus-owned:" + nonce + ":" + manifest_digest(manifest) in argv
        assert argv[-6:] == [sys.executable, "-I", str(path), "--executor-unit", "--owner-manifest-sha256", manifest_digest(manifest)]
        assert json.loads(options["input"]) == value
        calls.append(argv)
        if execute_unit:
            monkeypatch.setenv("INVOCATION_ID", "1" * 32)

            def unit_kernel(path, *args, **kwargs):
                if str(path) == "/proc/self/cgroup":
                    return io.BytesIO(("0::/synthetic.slice/" + value["unit"] + "\n").encode())
                return original_open(path, *args, **kwargs)

            monkeypatch.setattr(builtins, "open", unit_kernel)
            set_pipe_input(monkeypatch, options["input"])
            try:
                code = cli.main(["--executor-unit", "--owner-manifest-sha256", manifest_digest(manifest)])
            except CollectorExec:
                code = 0
            return subprocess.CompletedProcess(argv, code)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", native_process)
    set_pipe_input(monkeypatch, json.dumps(value).encode())
    result = cli.main(["--owned-executor", "--owner-manifest-sha256", manifest_digest(manifest)])
    assert result == (2 if scenario in ('projection-unproven', 'identity-stale') else 0)
    assert len(calls) == 1
    captured = capsys.readouterr()
    if scenario == 'projection-unproven':
        assert captured.out == '' and 'ConfigProjectionRefused' in captured.err
        bindings = [arg for arg in calls[0] if arg.startswith('--property=BindReadOnlyPaths=')]
        assert len(bindings) == 2 and all(arg.endswith(':norbind') for arg in bindings)
        records = list(work.glob('*/transport-executor/*/projection.json'))
        assert len(records) == 1
        retained = Path(json.loads(records[0].read_text())['proof']['directory'])
        assert retained.exists() and (retained / '0').read_bytes() == config_file.read_bytes()
        shutil.rmtree(retained)  # Explicit recovery of the synthetic unknown reader.
        return
    if execute_unit:
        header, output = captured.out.split("\n", 1)
        assert json.loads(header)["unit"] == value["unit"]
        assert json.loads(header)["invocation_id"] == "1" * 32
        if scenario.startswith('identity-'):
            assert len(executed) == (1 if scenario == 'identity-current' else 0)
            assert output == ('{"source":"synthetic"}\n' if executed else '')
            assert captured.err == ('' if executed else 'measurement refused: MeasurementUnresolved; no receipt applied\n')
        else:
            assert output == "owned-output\n"
            assert captured.err == "owned-error\n"
    else:
        assert captured.out == captured.err == ""
    if not scenario.startswith('control-'):
        return
    birth = json.loads(header)
    control = {key: item for key, item in value.items() if key not in ('kind', 'payload')}
    control.update(operation='kill', birth=birth)
    manager_calls = []

    class ManagerClient:
        def __init__(self, argv):
            self.returncode = None
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            state = {'LoadState': 'loaded', 'ActiveState': 'inactive', 'SubState': 'dead',
                'ControlGroup': birth['cgroup'], 'ExitType': 'cgroup', 'KillMode': 'control-group',
                'InvocationID': birth['invocation_id'],
                'Description': 'odysseus-owned:' + nonce + ':' + manifest_digest(manifest)}
            if scenario == 'control-foreign-invocation':
                state['InvocationID'] = 'f' * 32
            if scenario == 'control-foreign-description':
                state['Description'] = 'foreign'
            if scenario == 'control-missing-manager':
                state = {'LoadState': 'not-found'}
            self.stdout.feed_data(('\n'.join(key+'='+item for key, item in state.items()) if 'show' in argv else '').encode())
            self.stdout.feed_eof(); self.stderr.feed_eof()

        async def wait(self):
            self.returncode = 0
            return 0

        def kill(self):
            self.returncode = -9

    async def native_manager(*argv, **options):
        assert argv[:2] == ('systemctl', '--user')
        assert argv[-1] == value['unit']
        manager_calls.append(argv)
        return ManagerClient(argv)

    def control_kernel(path, *args, **kwargs):
        if str(path).endswith('/' + value['unit'] + '/cgroup.events'):
            return io.BytesIO(b'populated 0\n')
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', native_manager)
    monkeypatch.setattr(builtins, 'open', control_kernel)
    if scenario == 'control-mismatched-birth':
        control['birth'] = {**birth, 'invocation_id': 'e' * 32}
    if scenario == 'control-changed-source':
        python.write_bytes(b'changed source after birth\n')
    set_pipe_input(monkeypatch, json.dumps(control).encode())
    code = cli.main(['--owned-executor', '--owner-manifest-sha256', manifest_digest(manifest)])
    reply = capsys.readouterr()
    refused = scenario in ('control-foreign-invocation', 'control-foreign-description', 'control-mismatched-birth', 'control-changed-source')
    assert code == (2 if refused else 0)
    assert not any('kill' in argv for argv in manager_calls) if refused or scenario == 'control-missing-manager' else any('kill' in argv for argv in manager_calls)
    if refused:
        assert reply.out == '' and 'measurement refused' in reply.err
    elif scenario == 'control-missing-manager':
        assert json.loads(reply.out)['ExitType'] is None
        assert json.loads(reply.out)['KillMode'] is None
    else:
        assert json.loads(reply.out)['birth'] == birth


def set_pipe_input(monkeypatch, raw):
    reader, writer = os.pipe()
    with os.fdopen(writer, 'wb') as stream:
        stream.write(raw)
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(os.fdopen(reader, 'rb')))


def test_helper_input_has_its_own_deadline_even_if_sender_never_closes():
    command = [sys.executable, '-I', '-S', '-B', str(Path('scripts/odysseus-measure-once').resolve()),
               '--owned-executor', '--owner-manifest-sha256', 'a' * 64]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        process.stdin.write(b'{')
        process.stdin.flush()
        assert process.wait(timeout=7) == 2
        assert process.stdout.read() == b''
        assert process.stderr.read() == b'measurement refused: MeasurementUnresolved; no receipt applied\n'
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=1)
        process.stdin.close(); process.stdout.close(); process.stderr.close()


def capture_fixture(tmp_path, monkeypatch):
    from src.local_measurement_process import SystemdProcesses
    from src.local_measurement_transport import SshTransport
    work = tmp_path / 'work'; work.mkdir(mode=0o700)
    monkeypatch.setattr('src.local_measurement_transport.CANONICAL_INSPECTOR_WORK_ROOT', work)
    manifest = transport_manifest()
    transport = SshTransport(manifest, run_id='00000000-0000-4000-8000-000000000002',
        packet_id='routing-inspector-command', deadline=time.monotonic()+180)
    peer = SyntheticExecutor(manifest['runtime']['execution_transport']['executor'])
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', peer.start)
    processes = SystemdProcesses(worker_command=['unused-http'], broker_worker=['unused-broker'], transport=transport)
    return processes, peer, transport


@pytest.mark.parametrize('fault', ['wrong-run', 'partial', 'oversized'])
def test_lost_or_mismatched_birth_retains_guard_and_blocks_restart(tmp_path, monkeypatch, fault):
    from src.local_measurement_process import SystemdProcesses
    from src.local_measurement_transport import SshTransport
    processes, peer, transport = capture_fixture(tmp_path, monkeypatch)
    peer.birth_fault = fault
    with pytest.raises(MeasurementUnresolved):
        processes.qualify_no_provider()
    assert len(peer.calls) == 1 and all(client.returncode is not None for client in peer.calls)
    assert processes.evidence()[0]['local_stop_proven'] is True
    assert processes.evidence()[0]['remote_stop_proven'] is False
    replacement = SshTransport(transport.manifest, run_id='00000000-0000-4000-8000-000000000003',
        packet_id='routing-inspector-command', deadline=time.monotonic()+180)
    restarted = SystemdProcesses(worker_command=['unused'], broker_worker=[], transport=replacement)
    with pytest.raises(MeasurementUnresolved):
        restarted.qualify_no_provider()
    assert len(peer.calls) == 1


def test_heartbeat_is_not_blocked_by_an_active_provider_capture(tmp_path, monkeypatch):
    processes, peer, _transport = capture_fixture(tmp_path, monkeypatch)
    processes.qualify_no_provider()
    peer.provider_entered, peer.heartbeat_entered = threading.Event(), threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        provider = pool.submit(processes.request, 'http://synthetic-provider.invalid:11434/api/chat', {}, lease_valid=lambda: True)
        assert peer.provider_entered.wait(2)
        heartbeat = pool.submit(processes.post, '/heartbeat', {'lease_id': 'synthetic'})
        assert heartbeat.result(timeout=3) == (200, {'ok': True})
        assert provider.result(timeout=3) == {'done': True}
    assert all(client.returncode is not None for client in peer.calls)


@pytest.mark.parametrize('reply', [b'{"status":true,"body":{}}',
    b'{"status":"200","body":{}}', b'{"status":200,"body":[]}', b'{"status":200}'])
def test_untrusted_broker_envelope_cannot_be_followed_by_another_action(tmp_path, monkeypatch, reply):
    processes, peer, _transport = capture_fixture(tmp_path, monkeypatch)
    processes.qualify_no_provider()
    peer.broker_output = reply
    with pytest.raises(MeasurementUnresolved):
        processes.post('/heartbeat', {'lease_id': 'synthetic'})
    calls = len(peer.calls)
    with pytest.raises(MeasurementUnresolved):
        processes.post('/release', {'lease_id': 'synthetic'})
    assert len(peer.calls) == calls


def test_full_wire_budget_refuses_before_another_ssh_launch(tmp_path, monkeypatch):
    processes, peer, _transport = capture_fixture(tmp_path, monkeypatch)
    processes.qualify_no_provider()
    calls = len(peer.calls)
    with pytest.raises(MeasurementUnresolved):
        processes.request('http://synthetic-provider.invalid:11434/api/chat', {'text': 'x' * (1048576-100)},
                          lease_valid=lambda: True)
    assert len(peer.calls) == calls


@pytest.fixture(autouse=True)
def no_native_transports(monkeypatch):
    block_native_transports(monkeypatch)
