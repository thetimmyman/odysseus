"""Concrete owned-client capture for the one-shot local qualification command.

systemd's ExitType=cgroup and KillMode=control-group supervise LOCAL processes.
They cannot cancel remote SSH descendants or an Ollama request. An uncertain
remote outcome remains unresolved even when this local capture confirms exit.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import threading
import time
import uuid

from src.canonical_pilot_adapter import CaptureBudget, collect_bytes, collect_process
from src.local_measurement_owner import MeasurementUnresolved
from src.constants import CANONICAL_TMOS_PACKET_IDS


CGROUP_ROOT = Path('/sys/fs/cgroup')
_CONTROL_SECONDS = 0.6
_STDERR_BYTES = 65536
_JSON_BYTES = 1048576


def checked_workspace_operation(operation):
    """Finite wire shape shared by the owner capture and configured worker."""
    try:
        copied = decode_json(json.dumps(operation, allow_nan=False).encode())
        if type(copied) is not dict:
            raise ValueError('workspace operation unavailable')
        action = copied.get('operation')
        expected = {'schema', 'operation', 'packet_id', 'run_id', 'manifest_digest'}
        if action in ('snapshot', 'apply', 'verify'):
            expected.add('package')
        if action == 'apply':
            expected.add('patch')
        if action == 'verify':
            expected.update(('policy_hash', 'timeout_s'))
        run_id, digest = copied.get('run_id'), copied.get('manifest_digest')
        if (set(copied) != expected or action not in ('prepare', 'snapshot', 'apply', 'verify')
                or copied.get('schema') != 'tmos-workspace-operation-v1'
                or copied.get('packet_id') not in CANONICAL_TMOS_PACKET_IDS
                or str(uuid.UUID(run_id)) != run_id
                or type(digest) is not str or len(digest) != 64
                or any(character not in '0123456789abcdef' for character in digest)):
            raise ValueError('workspace operation unavailable')
        if action == 'verify' and (type(copied['timeout_s']) is not int or not 0 < copied['timeout_s'] <= 180):
            raise ValueError('workspace deadline unavailable')
        return copied
    except (ValueError, TypeError, KeyError, AttributeError):
        raise MeasurementUnresolved('workspace operation unavailable') from None


def decode_json(raw):
    """Reject duplicate/nonfinite JSON before trusting a worker envelope."""
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError('duplicate key')
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError('nonfinite value')

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=nonfinite)


async def _control(*args):
    process = await asyncio.create_subprocess_exec('systemctl', '--user', *args,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(_CONTROL_SECONDS):
            stdout, stderr, code = await asyncio.gather(
                collect_bytes(_stream(process.stdout), limit=16384, seconds=_CONTROL_SECONDS),
                collect_bytes(_stream(process.stderr), limit=16384, seconds=_CONTROL_SECONDS),
                process.wait())
            return code, stdout, stderr
    finally:
        if process.returncode is None:
            process.kill()
            await asyncio.wait_for(process.wait(), 1)


async def _stream(reader):
    while chunk := await reader.read(8192):
        yield chunk


class _Boundary:
    def __init__(self, owner, process, unit, lease_valid, evidence):
        self.owner, self.process, self.unit = owner, process, unit
        self.lease_valid = lease_valid
        self.stdout, self.stderr = _stream(process.stdout), _stream(process.stderr)
        self.cgroup = None
        self.evidence = evidence

    @property
    def qualified(self):
        return self.owner.qualified

    async def wait(self):
        return await self.process.wait()

    async def _state(self):
        code, raw, _ = await _control('show', '--no-pager', '--property=LoadState,ActiveState,SubState,'
            'ControlGroup,ExitType,KillMode', '--', self.unit)
        # systemctl show may return nonzero for an unloaded transient unit.
        rows = dict(line.split('=', 1) for line in raw.decode().splitlines() if '=' in line)
        if rows.get('LoadState') == 'not-found':
            return rows
        if code != 0 or rows.get('ExitType') != 'cgroup' or rows.get('KillMode') != 'control-group':
            raise MeasurementUnresolved('owned unit state unavailable')
        group = rows.get('ControlGroup', '')
        if group:
            path = (CGROUP_ROOT / group.lstrip('/')).resolve()
            if (not path.is_relative_to(CGROUP_ROOT) or path.name != self.unit
                    or (self.cgroup is not None and path != self.cgroup)):
                raise MeasurementUnresolved('owned cgroup mismatch')
            self.cgroup = path
        return rows

    async def terminate_all(self):
        self.owner._record(self.evidence, termination_requested=True)
        # First stop this launcher so no further start request can be submitted.
        if self.process.returncode is None:
            self.process.kill()
            await asyncio.wait_for(self.process.wait(), 0.2)
        await _control('kill', '--signal=SIGKILL', '--kill-whom=all', '--', self.unit)

    async def confirm_stopped(self):
        if self.process.returncode is None:
            return False
        deadline = time.monotonic() + 1.3
        state = await self._state()
        while (state.get('LoadState') != 'not-found'
               and state.get('ActiveState') not in ('inactive', 'failed')
               and time.monotonic() < deadline):
            await asyncio.sleep(0.05)
            state = await self._state()
        if state.get('LoadState') != 'not-found' and state.get('ActiveState') not in ('inactive', 'failed'):
            return False
        if self.cgroup is not None:
            try:
                values = dict(row.split() for row in (self.cgroup/'cgroup.events').read_text().splitlines())
                if values.get('populated') != '0':
                    return False
            except FileNotFoundError:
                pass  # Manager confirmation plus removed cgroup, not PID exit alone.
        self.owner._record(self.evidence, local_stop_proven=True,
                           launcher_exit_code=self.process.returncode)
        return True


class SystemdProcesses:
    """One-shot concrete collect_process adapter, gated by local operational proof.

    Construction does not start anything. qualify_no_provider must prove normal
    output and termination of a detached descendant on THIS host/run before any
    broker, identity or provider worker is admitted. Tests cannot grant this by
    supplying a stale proof file or an environment flag.
    """

    def __init__(self, *, worker_command, broker_worker, config_projection=None, workspace_worker=None):
        self._worker, self._broker_worker = worker_command, broker_worker
        self._config_projection = config_projection
        self._configuration_state = 'NOT_CONFIGURED' if config_projection is None else 'READY'
        self._qualified = False
        self._deadline = 0
        self._captures = []
        self._evidence_lock = threading.Lock()
        self._workspace_worker = workspace_worker
        self._boundaries = []
        self._closed = False
        self._pending_starts = 0

    def _record(self, row, **changes):
        with self._evidence_lock:
            row.update(changes)
            # The operational controller redirects stderr to a private bounded
            # file. Flush the unit ID BEFORE launch, so even controller death
            # leaves an exact reconciliation key. Never include argv/payloads.
            print(json.dumps({'measurement_process':dict(row)}, sort_keys=True),
                  file=sys.stderr, flush=True)

    def evidence(self):
        with self._evidence_lock:
            return [dict(row) for row in self._captures]

    def configuration_evidence(self):
        return self._configuration_state

    def finish_capture(self):
        """Complete private-copy cleanup before release/receipt publication."""
        if self._config_projection is not None:
            try:
                self._config_projection.verify()
            except Exception:
                self._configuration_state = 'POSTCHECK_REFUSED'
                raise
            try:
                complete = self._config_projection.close()
            except Exception:
                complete = False
            if complete is not True:
                self._configuration_state = 'CLEANUP_UNRESOLVED'
                raise MeasurementUnresolved('configuration cleanup unresolved') from None
            self._configuration_state = 'REMOVED'
        return True

    @property
    def qualified(self):
        return self._qualified

    async def _start(self, command, seconds, lease_valid, input_bytes=b'', *, kind):
        with self._evidence_lock:
            if self._closed or lease_valid() is not True:
                raise MeasurementUnresolved('owned capture is closed')
            self._pending_starts += 1
        unit = 'odysseus-measure-' + uuid.uuid4().hex + '.service'
        row = {'unit':unit, 'kind':kind, 'local_stop_proven':False,
               'remote_stop_proven':False, 'launcher_exit_code':None}
        with self._evidence_lock:
            self._captures.append(row)
        self._record(row)  # Durable diagnostic precedes ANY start request.
        properties = ['ExitType=cgroup', 'KillMode=control-group', 'SendSIGKILL=yes',
            'TimeoutStopSec=1', f'RuntimeMaxSec={seconds}', 'MemoryMax=1G', 'MemorySwapMax=0',
            'TasksMax=32', 'CPUQuota=100%', 'UMask=0077', 'LimitFSIZE=2097152',
            'NoNewPrivileges=yes', 'ProtectControlGroups=yes', 'RestrictSUIDSGID=yes']
        if self._config_projection is not None and kind in ('identity', 'proof-normal', 'proof-descendant'):
            properties.extend('BindReadOnlyPaths='+source+':'+destination+':norbind'
                              for source, destination in self._config_projection.bindings())
        argv = ['systemd-run', '--user', '--pipe', '--wait', '--quiet', '--service-type=exec',
                '--expand-environment=no', '--unit='+unit]
        argv.extend('--property='+value for value in properties)
        try:
            process = await asyncio.create_subprocess_exec(*argv, '--', *command,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            boundary = _Boundary(self, process, unit, lambda: not self._closed and lease_valid(), row)
            with self._evidence_lock:
                self._boundaries.append(boundary)
        finally:
            with self._evidence_lock:
                self._pending_starts -= 1
        try:
            if self._closed or lease_valid() is not True:
                raise MeasurementUnresolved('owned capture is closed')
            process.stdin.write(input_bytes)
            await asyncio.wait_for(process.stdin.drain(), 3)
            process.stdin.close()
            return boundary
        except BaseException:
            await boundary.terminate_all()
            raise

    def qualify_no_provider(self):
        """Two owned synthetic units only; no network, SSH, broker or provider."""
        if self._qualified or self._closed:
            raise MeasurementUnresolved('one-shot boundary proof already used')

        async def prove():
            normal_command = ([sys.executable, '-I', '-c',
                'import sys; print("owned-output"); print("owned-error",file=sys.stderr)']
                if self._config_projection is None else self._config_projection.proof_command())
            normal = await self._start(normal_command,
                5, lambda: True, kind='proof-normal')
            try:
                stdout, stderr, code = await asyncio.wait_for(asyncio.gather(
                    collect_bytes(normal.stdout, limit=4096, seconds=5),
                    collect_bytes(normal.stderr, limit=4096, seconds=5), normal.wait()), 5)
                if (stdout != b'owned-output\n' or stderr != b'owned-error\n' or code != 0
                        or await normal.confirm_stopped() is not True):
                    raise MeasurementUnresolved('normal owned capture unproven')
            finally:
                if normal.process.returncode is None:
                    await normal.terminate_all()
            descendant = await self._start([sys.executable, '-I', '-c',
                'import subprocess,sys; p=subprocess.Popen([sys.executable,"-I","-c",'
                '"import time; time.sleep(60)"],start_new_session=True,stdin=subprocess.DEVNULL,'
                'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); print(p.pid,flush=True)'],
                10, lambda: True, kind='proof-descendant')
            try:
                pid_raw = await asyncio.wait_for(descendant.process.stdout.readline(), 3)
                if not pid_raw.strip().isdigit():
                    raise MeasurementUnresolved('descendant proof unavailable')
                pid = int(pid_raw)
                await asyncio.sleep(0.3)
                state = await descendant._state()
                group = descendant.cgroup
                if (descendant.process.returncode is not None or state.get('ActiveState') != 'active'
                        or group is None or f'0::{str(group).removeprefix(str(CGROUP_ROOT))}'
                        not in Path(f'/proc/{pid}/cgroup').read_text().splitlines()
                        or 'populated 1' not in (group/'cgroup.events').read_text().splitlines()):
                    raise MeasurementUnresolved('whole-descendant ownership unproven')
            finally:
                await descendant.terminate_all()
            if await descendant.confirm_stopped() is not True:
                raise MeasurementUnresolved('whole-descendant termination unproven')

        try:
            asyncio.run(prove())
            if self._config_projection is not None:
                self._config_projection.verify()
        except Exception:
            raise MeasurementUnresolved('local boundary qualification failed') from None
        self._qualified = True
        self._deadline = time.monotonic() + 900
        return {'local_capture': True, 'local_descendant_termination': True,
                'remote_cancellation': False, 'provider_calls': 0, 'processes':self.evidence()}

    def _json(self, command, value, *, seconds, limit, lease_valid, kind):
        if self._closed or not self.qualified or lease_valid() is not True:
            raise MeasurementUnresolved('owned capture not qualified or lease unavailable')
        raw = json.dumps(value, allow_nan=False).encode() if value is not None else b''
        remaining = min(seconds, self._deadline - time.monotonic())
        if remaining <= 0 or len(raw) > _JSON_BYTES:
            raise MeasurementUnresolved('owned capture budget exhausted')

        async def capture():
            boundary = await self._start(command, remaining, lease_valid, raw, kind=kind)
            stdout, _, code = await collect_process(boundary, CaptureBudget(
                stdout_bytes=limit, stderr_bytes=_STDERR_BYTES, total_bytes=limit+_STDERR_BYTES,
                seconds=remaining))
            if code != 0:
                raise MeasurementUnresolved('owned worker did not complete successfully')
            return decode_json(stdout)

        try:
            return asyncio.run(capture())
        except Exception:
            raise MeasurementUnresolved('owned capture unresolved') from None

    def identity(self, command, *, lease_valid):
        try:
            result = self._json(command, None, seconds=180, limit=524288, lease_valid=lease_valid,
                                kind='identity')
        except BaseException:
            if self._config_projection is not None:
                try:
                    self._config_projection.verify()
                except Exception:
                    self._configuration_state = 'POSTCHECK_REFUSED'
            raise
        if self._config_projection is not None:
            try:
                self._config_projection.verify()
            except Exception:
                self._configuration_state = 'POSTCHECK_REFUSED'
                raise
        return result

    def request(self, url, payload, *, lease_valid):
        return self._json(self._worker, {'url':url, 'payload':payload}, seconds=140,
                          limit=_JSON_BYTES, lease_valid=lease_valid, kind='provider')

    def post(self, path, body):
        response = self._json(self._broker_worker, {'path':path, 'body':body}, seconds=8,
                              limit=16384, lease_valid=lambda: True, kind='broker')
        if type(response) is not dict or set(response) != {'status', 'body'}:
            raise MeasurementUnresolved('broker envelope unavailable')
        return response['status'], response['body']

    def workspace(self, operation, *, lease_valid):
        """Capture one fixed owner-prepared TMOS workspace operation.

        The configured worker independently checks the admitted manifest and
        package. There is no argv, shell, URL or general command input here.
        Local exit does not prove remote provider/container cancellation.
        """
        if not self._workspace_worker:
            raise MeasurementUnresolved('workspace capture unavailable')
        operation = checked_workspace_operation(operation)
        return self._json(self._workspace_worker, operation, seconds=180,
                          limit=_JSON_BYTES, lease_valid=lease_valid, kind='workspace')

    def request_stop(self):
        """Invalidate active capture guards in their owning event loops."""
        with self._evidence_lock:
            self._closed = True

    async def confirm_stopped(self):
        with self._evidence_lock:
            if self._pending_starts:
                return False
            boundaries = tuple(self._boundaries)
        for boundary in boundaries:
            if not await boundary.confirm_stopped():
                return False
        with self._evidence_lock:
            return not self._pending_starts and all(row.get('local_stop_proven') is True for row in self._captures)
