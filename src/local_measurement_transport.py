"""Finite SSH supervision for the existing app-owned measurement capture.

No constructor qualifies execution. Routing, admission, account checks and the
normal broker remain in the app. The one-shot helper is not a command server.
"""
from __future__ import annotations

import math
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import socket
import sys
import stat
import selectors
import threading
import subprocess
import time
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

from src.constants import (CANONICAL_TMOS_PACKET_IDS, CANONICAL_INSPECTOR_WORK_ROOT,
    CANONICAL_TMOS_TRANSPORT_CLIENT_DIRNAME, CANONICAL_TMOS_TRANSPORT_GUARD_FILENAME,
    CANONICAL_TMOS_TRANSPORT_EXECUTOR_DIRNAME, CANONICAL_TMOS_MANIFEST_FILE, LOCAL_MEASUREMENT_COMMAND,
    CANONICAL_TMOS_CAPTURE_INTENT_FILENAME, CANONICAL_TMOS_CAPTURE_BIRTH_FILENAME,
    CANONICAL_TMOS_CAPTURE_DESCENDANT_FILENAME, CANONICAL_TMOS_CAPTURE_COMPLETE_FILENAME,
    CANONICAL_TMOS_CAPTURE_PROJECTION_FILENAME,
    CANONICAL_TMOS_CAPTURE_CONTROL_PREFIX)
from src.canonical_pilot_adapter import collect_bytes
from src.local_measurement_owner import MeasurementUnresolved
from src.local_measurement_process import decode_json, _stream, systemd_command


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_UNIT = re.compile(r"odysseus-measure-[0-9a-f]{32}\.service\Z")
_KINDS = {"proof-normal", "proof-descendant", "identity", "provider", "broker", "workspace"}
EXECUTOR_SOURCE_FILES = (
    "scripts/odysseus-measure-once", "src/local_measurement_transport.py",
    "src/local_measurement_process.py", "src/local_measurement_owner.py",
    "src/local_measurement_config.py", "src/canonical_pilot_adapter.py",
    "src/canonical_inspector_workspace.py", "src/inspector_errors.py", "src/constants.py",
    "src/local_target_qualification.py", "src/local_targets.py", "src/endpoint_identity.py",
    "src/execution_package.py", "src/evidence_contract.py", "src/work_packet.py",
    "src/source_snapshot.py", "src/routing_workdir.py", "src/routing_patch.py",
    "src/routing_context.py", "src/routing_redaction.py", "src/routing_policy.py",
    "src/routing_budget.py", "src/config_store.py", "src/dispatch_routing.py", "src/routing_sandbox.py",
)


def _absolute(value):
    if (type(value) is not str or len(value) > 2048 or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
            or ".." in Path(value).parts or str(Path(value)) != value):
        raise ValueError("unsupported installed path")
    return value


def _endpoint(value):
    if (type(value) is not str or len(value) > 2048 or value != value.strip()
            or any(ord(character) <= 32 or ord(character) == 127 for character in value)):
        raise ValueError("unsupported endpoint")
    parsed = urlsplit(value)
    if (parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError("unsupported endpoint")
    parsed.port
    return value


def _encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _directory(path, *, create=False):
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise MeasurementUnresolved("owned capture directory unavailable")
    return path


def _record_once(directory, name, value):
    """Operational ownership artifact, separate from canonical routing evidence."""
    _directory(directory)
    raw = _encoded(value)
    if len(raw) > 65536:
        raise MeasurementUnresolved("owned capture record exceeds budget")
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


async def _frame(reader):
    data = bytearray()
    while len(data) < 4096:
        value = await reader.read(1)
        if not value:
            break
        data.extend(value)
        if value == b"\n":
            return decode_json(bytes(data))
    raise MeasurementUnresolved("executor birth acknowledgement unavailable")


class SshTransport:
    """Explicit private configuration for one run; construction starts no I/O."""

    def __init__(self, manifest, *, run_id, packet_id, deadline):
        try:
            self.manifest = decode_json(json.dumps(manifest, allow_nan=False).encode())
            config = self.manifest["runtime"]["execution_transport"]
            if (self.manifest["schema"] != "canonical-tmos-owner-v1"
                    or packet_id not in CANONICAL_TMOS_PACKET_IDS
                    or packet_id not in self.manifest["packages"]
                    or str(uuid.UUID(run_id)) != run_id
                    or type(deadline) not in (int, float) or not math.isfinite(deadline)
                    or not 0 < deadline - time.monotonic() <= 180
                    or type(config) is not dict or set(config) != {"kind", "host", "python", "command",
                        "source_pins", "executor", "broker_url", "provider_endpoints", "ssh_config_projection"}
                    or config["kind"] != "ssh" or type(config["host"]) is not str
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", config["host"])
                    or Path(_absolute(config["command"])).name != "odysseus-measure-once"
                    or type(config["source_pins"]) is not dict
                    or set(config["source_pins"]) != set(EXECUTOR_SOURCE_FILES)
                    or any(type(v) is not str or not _DIGEST.fullmatch(v) for v in config["source_pins"].values())
                    or type(config["provider_endpoints"]) is not list or not config["provider_endpoints"]
                    or len(config["provider_endpoints"]) > 8
                    or config["ssh_config_projection"] is not None and type(config["ssh_config_projection"]) is not dict):
                raise ValueError("unsupported executor descriptor")
            _absolute(config["python"])
            _endpoint(config["broker_url"])
            for endpoint in config["provider_endpoints"]:
                _endpoint(endpoint)
            identity = config["executor"]
            if (type(identity) is not dict or set(identity) != {"hostname", "uid", "gid", "groups", "boot_id", "python_sha256"}
                    or type(identity["hostname"]) is not str or not identity["hostname"]
                    or any(type(identity[k]) is not int or identity[k] < 0 for k in ("uid", "gid"))
                    or type(identity["groups"]) is not list or len(identity["groups"]) > 64
                    or any(type(g) is not int or g < 0 for g in identity["groups"])
                    or identity["groups"] != sorted(set(identity["groups"]))
                    or str(uuid.UUID(identity["boot_id"])) != identity["boot_id"]
                    or type(identity["python_sha256"]) is not str or not _DIGEST.fullmatch(identity["python_sha256"])):
                raise ValueError("unsupported executor identity")
            self.config = config
            self.run_id, self.packet_id, self.deadline = run_id, packet_id, deadline
            self._journal = None
            self._guard = False
            self._configuration_path = None
            self._configuration_lock = threading.RLock()
            self._configuration_state = 'NOT_CONFIGURED' if config['ssh_config_projection'] is None else 'UNAVAILABLE'
            from src.canonical_inspector_workspace import manifest_digest
            self.manifest_digest = manifest_digest(self.manifest)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise MeasurementUnresolved("SSH executor configuration unavailable") from None

    @property
    def argv(self):
        remote = shlex.join([self.config["python"], "-I", self.config["command"],
                             "--owned-executor", "--owner-manifest-sha256", self.manifest_digest])
        if self.config["ssh_config_projection"] is not None and self._configuration_path is None:
            raise MeasurementUnresolved("SSH configuration has not been observed")
        config = ("-F", self._configuration_path or "/dev/null")
        return ("/usr/bin/ssh", "-T", *config, "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectionAttempts=1", "-o", "ConnectTimeout=2", "-o", "ControlMaster=no",
                "-o", "ControlPath=none", "-o", "ClearAllForwardings=yes", "-o", "ForwardAgent=no",
                "-o", "ForwardX11=no", "-o", "PermitLocalCommand=no", "-o", "ProxyCommand=none",
                "-o", "ProxyJump=none", "-o", "RequestTTY=no", "-o", "LogLevel=ERROR",
                "-o", "ForkAfterAuthentication=no", "-o", "ControlPersist=no", "-o", "PKCS11Provider=none",
                "--", self.config["host"], remote)

    def check_configuration(self):
        with self._configuration_lock:
            return self._check_configuration()

    def _check_configuration(self):
        if self.config['ssh_config_projection'] is None:
            return True
        if self._configuration_state in ('CLEANUP_UNRESOLVED', 'POSTCHECK_REFUSED'):
            raise MeasurementUnresolved('SSH configuration observation previously refused')
        from src.local_measurement_config import ReadOnlyConfigProjection, ConfigProjectionRefused
        projection = None
        primary = None
        try:
            projection = ReadOnlyConfigProjection(self.config['ssh_config_projection'])
            self._configuration_path = projection.native_client_config()
            self._configuration_state = 'READY'
            projection.verify()
        except BaseException as failure:
            primary = failure
            self._configuration_state = 'POSTCHECK_REFUSED'
            if isinstance(failure, ConfigProjectionRefused) and failure.cleanup_state != 'REMOVED':
                self._configuration_state = 'CLEANUP_UNRESOLVED'
        try:
            removed = projection is None or projection.close() is True
        except BaseException as failure:
            primary = primary or failure
            removed = False
        if not removed:
            self._configuration_state = 'CLEANUP_UNRESOLVED'
            raise ConfigProjectionRefused(primary or MeasurementUnresolved('configuration cleanup unresolved'),
                cleanup_state='UNKNOWN', retained_directory=projection._temporary.name if projection._temporary else None)
        if primary is not None:
            raise primary
        self._configuration_state = 'REMOVED'
        return True

    def _ownership(self, unit):
        return {"schema": "owned-measurement-transport-v1", "run_id": self.run_id,
                "packet_id": self.packet_id, "owner_manifest_digest": self.manifest_digest,
                "capture_nonce": unit.removeprefix("odysseus-measure-").removesuffix(".service"), "unit": unit}

    def _prepare_journal(self):
        if self._journal is None:
            root = _directory(Path(CANONICAL_INSPECTOR_WORK_ROOT))
            _record_once(root, CANONICAL_TMOS_TRANSPORT_GUARD_FILENAME,
                         {"run_id": self.run_id, "owner_manifest_digest": self.manifest_digest, "state": "UNRESOLVED"})
            self._guard = True
            run = _directory(root / self.run_id, create=True)
            self._journal = _directory(run / CANONICAL_TMOS_TRANSPORT_CLIENT_DIRNAME, create=True)

    def finalize_ownership(self):
        from src.canonical_inspector_workspace import _private_bytes
        if not self._guard or self._journal is None:
            raise MeasurementUnresolved("capture ownership is unresolved")
        root = _directory(Path(CANONICAL_INSPECTOR_WORK_ROOT))
        path = root / CANONICAL_TMOS_TRANSPORT_GUARD_FILENAME
        expected = {"run_id": self.run_id, "owner_manifest_digest": self.manifest_digest, "state": "UNRESOLVED"}
        if _encoded(decode_json(_private_bytes(path, 4096))) != _encoded(expected):
            raise MeasurementUnresolved("capture ownership changed")
        _record_once(self._journal, CANONICAL_TMOS_CAPTURE_COMPLETE_FILENAME, {"run_id": self.run_id, "state": "OBSERVED_STOPPED"})
        path.unlink()
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        self._guard = False
        return True

    def owns_guard(self):
        from src.canonical_inspector_workspace import _private_bytes
        if not self._guard:
            return False
        path = Path(CANONICAL_INSPECTOR_WORK_ROOT) / CANONICAL_TMOS_TRANSPORT_GUARD_FILENAME
        expected = {"run_id": self.run_id, "owner_manifest_digest": self.manifest_digest, "state": "UNRESOLVED"}
        return _encoded(decode_json(_private_bytes(path, 4096))) == _encoded(expected)

    def _validate_birth(self, birth, unit):
        expected = self._ownership(unit)
        if (type(birth) is not dict or set(birth) != {"schema", "unit", "capture_nonce", "run_id", "packet_id",
                "owner_manifest_digest", "invocation_id", "cgroup", "pid", "start_ticks", "executor"}
                or birth["schema"] != "owned-measurement-birth-v1"
                or any(birth[k] != expected[k] for k in expected if k != "schema")
                or _encoded(birth["executor"]) != _encoded(self.config["executor"])
                or type(birth["invocation_id"]) is not str or not re.fullmatch(r"[0-9a-f]{32}", birth["invocation_id"])
                or type(birth["cgroup"]) is not str or not birth["cgroup"].startswith("/")
                or ".." in Path(birth["cgroup"]).parts or Path(birth["cgroup"]).name != unit
                or any(type(birth[k]) is not int or birth[k] <= 0 for k in ("pid", "start_ticks"))):
            raise MeasurementUnresolved("executor birth mismatch")
        return birth

    def _payload(self, kind, command, raw):
        if kind in ("proof-normal", "proof-descendant"):
            return None
        if kind == "identity":
            matches = [k for k, v in self.manifest["runtime"]["identity_commands"].items() if v == command]
            if len(matches) != 1:
                raise MeasurementUnresolved("identity command is not admitted")
            return {"target_id": matches[0]}
        return decode_json(raw)

    def validate_payload(self, value):
        kind, payload = value["kind"], value["payload"]
        valid = False
        if kind in ("proof-normal", "proof-descendant"):
            valid = payload is None
        elif kind == "identity":
            valid = (type(payload) is dict and set(payload) == {"target_id"}
                     and type(payload["target_id"]) is str
                     and payload["target_id"] in self.manifest["runtime"]["identity_commands"])
        elif kind == "provider":
            valid = (type(payload) is dict and set(payload) == {"url", "payload"}
                     and type(payload["payload"]) is dict
                     and payload["url"] in [endpoint.rstrip("/") + "/api/chat" for endpoint in self.config["provider_endpoints"]])
        elif kind == "broker":
            valid = (type(payload) is dict and set(payload) == {"path", "body"}
                     and payload["path"] in ("/acquire", "/heartbeat", "/release") and type(payload["body"]) is dict
                     and len(_encoded(payload)) <= 16384 and value["seconds"] <= 8)
        elif kind == "workspace":
            from src.local_measurement_process import checked_workspace_operation
            operation = checked_workspace_operation(payload)
            valid = (operation["run_id"] == self.run_id and operation["packet_id"] == self.packet_id
                     and operation["manifest_digest"] == self.manifest_digest)
        if not valid:
            raise MeasurementUnresolved("capture payload is not admitted")

    async def start(self, owner, command, seconds, lease_valid, raw, *, kind):
        with owner._evidence_lock:
            if owner._closed or lease_valid() is not True:
                raise MeasurementUnresolved("owned capture is closed")
            owner._pending_starts += 1
        unit = "odysseus-measure-" + uuid.uuid4().hex + ".service"
        row = {"unit": unit, "kind": kind, "transport": "ssh", "local_stop_proven": False,
               "remote_stop_proven": False, "launcher_exit_code": None}
        boundary = None
        try:
            value = {**self._ownership(unit), "operation": "launch", "seconds": min(seconds, self.deadline-time.monotonic()-8),
                     "kind": kind, "payload": self._payload(kind, command, raw)}
            checked_envelope(value)
            self.validate_payload(value)
            if len(_encoded(value)) > 1048576:
                raise MeasurementUnresolved('owned executor envelope exceeds budget')
            self.check_configuration()
            self._prepare_journal()
            directory = _directory(self._journal / unit, create=True)
            _record_once(directory, CANONICAL_TMOS_CAPTURE_INTENT_FILENAME, _intent(value))
            with owner._evidence_lock:
                owner._captures.append(row)
            owner._record(row)
            process = await asyncio.create_subprocess_exec(*self.argv, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
            boundary = _SshBoundary(self, owner, process, unit, directory, lease_valid, row)
            with owner._evidence_lock:
                owner._boundaries.append(boundary)
            process.stdin.write(_encoded(value))
            await asyncio.wait_for(process.stdin.drain(), min(3, value["seconds"]))
            process.stdin.close()
            birth = await asyncio.wait_for(_frame(process.stdout), min(3, value["seconds"]))
            boundary.birth = self._validate_birth(birth, unit)
            _record_once(directory, CANONICAL_TMOS_CAPTURE_BIRTH_FILENAME, birth)
            self.check_configuration()
            return boundary
        except BaseException:
            if boundary is not None and boundary.process.returncode is None:
                boundary.process.kill()
                await asyncio.wait_for(boundary.process.wait(), 0.2)
            if boundary is not None:
                owner._record(row, launcher_exit_code=boundary.process.returncode,
                              local_stop_proven=boundary.process.returncode is not None)
            owner._closed = True
            raise
        finally:
            with owner._evidence_lock:
                owner._pending_starts -= 1

    async def control(self, boundary, operation, *, seconds=3, **extra):
        if boundary.birth is None:
            raise MeasurementUnresolved("executor birth is unresolved")
        seconds = min(seconds, self.deadline - time.monotonic())
        value = {**self._ownership(boundary.unit), "operation": operation, "seconds": seconds,
                 "birth": boundary.birth, **extra}
        checked_envelope(value)
        self.check_configuration()
        token = uuid.uuid4().hex
        _record_once(boundary.directory, CANONICAL_TMOS_CAPTURE_CONTROL_PREFIX + token + "-intent.json", {"operation": operation})
        process = await asyncio.create_subprocess_exec(*self.argv, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
        try:
            async with asyncio.timeout(seconds):
                process.stdin.write(_encoded(value))
                await process.stdin.drain()
                process.stdin.close()
                stdout, _, code = await asyncio.gather(
                    collect_bytes(_stream(process.stdout), limit=16384, seconds=seconds),
                    collect_bytes(_stream(process.stderr), limit=16384, seconds=seconds), process.wait())
            response = decode_json(stdout)
            if (code != 0 or type(response) is not dict
                    or _encoded(response.get("birth")) != _encoded(boundary.birth)):
                raise MeasurementUnresolved("executor control acknowledgement unavailable")
            self.check_configuration()
            return response
        finally:
            if process.returncode is None:
                process.kill()
                await asyncio.wait_for(process.wait(), 0.2)
            _record_once(boundary.directory, CANONICAL_TMOS_CAPTURE_CONTROL_PREFIX + token + "-closed.json", {"launcher_exit_code": process.returncode})


class _SshBoundary:
    def __init__(self, driver, owner, process, unit, directory, lease_valid, evidence):
        self.driver, self.owner, self.process = driver, owner, process
        self.unit, self.directory, self.evidence = unit, directory, evidence
        self.lease_valid = lambda: not owner._closed and lease_valid()
        self.stdout, self.stderr = _stream(process.stdout), _stream(process.stderr)
        self.birth = None

    @property
    def qualified(self):
        return self.owner.qualified

    async def wait(self):
        return await self.process.wait()

    async def _state(self):
        result = await self.driver.control(self, "state")
        if (set(result) != {"birth", "LoadState", "ActiveState", "SubState", "ControlGroup", "ExitType", "KillMode", "populated", "configuration_cleanup"}
                or result["ControlGroup"] != self.birth["cgroup"]
                or (result["LoadState"] == 'loaded' and (result["ExitType"] != 'cgroup' or result["KillMode"] != 'control-group'))
                or (result["LoadState"] == 'not-found' and (result["ExitType"] is not None or result["KillMode"] is not None))
                or result["LoadState"] not in ("loaded", "not-found")
                or result["ActiveState"] not in ("active", "inactive", "failed", "activating", "deactivating", "reloading")
                or result['configuration_cleanup'] not in ('NOT_CONFIGURED', 'UNKNOWN', 'REMOVED')
                or (result["populated"] is not None and (type(result["populated"]) is not int or result["populated"] not in (0, 1)))):
            raise MeasurementUnresolved("executor unit state unavailable")
        return result

    async def owns_descendant(self, pid):
        result = await self.driver.control(self, "membership", pid=pid)
        return set(result) == {"birth", "pid", "member"} and result["pid"] == pid and result["member"] is True

    async def terminate_all(self):
        self.owner._record(self.evidence, termination_requested=True)
        # Let the fixed executor wrapper observe its native cgroup ending and
        # remove bound configuration copies before closing its SSH client.
        await self.driver.control(self, "kill", seconds=2.4)
        if self.process.returncode is None:
            try:
                await asyncio.wait_for(self.process.wait(), 0.5)
            except TimeoutError:
                self.process.kill()
                await asyncio.wait_for(self.process.wait(), 0.2)

    async def confirm_stopped(self):
        if self.process.returncode is None:
            return False
        deadline = min(time.monotonic() + 5, self.driver.deadline)
        for _ in range(3):
            state = await asyncio.wait_for(self._state(), max(0.001, deadline-time.monotonic()))
            if ((state["LoadState"] == "not-found" or state["ActiveState"] in ("inactive", "failed"))
                    and state["populated"] in (0, None)
                    and state['configuration_cleanup'] in ('NOT_CONFIGURED', 'REMOVED')):
                self.owner._record(self.evidence, local_stop_proven=True, remote_stop_proven=True,
                                   launcher_exit_code=self.process.returncode)
                return True
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)
        return False


def checked_envelope(value):
    """Only fixed operations and exact ownership identities cross SSH stdin."""
    try:
        common = {"schema", "operation", "run_id", "packet_id", "owner_manifest_digest",
                  "capture_nonce", "unit", "seconds"}
        action = value["operation"]
        extra = {"launch": {"kind", "payload"}, "state": {"birth"},
                 "kill": {"birth"}, "membership": {"birth", "pid"}}[action]
        if (type(value) is not dict or set(value) != common | extra
                or value["schema"] != "owned-measurement-transport-v1"
                or str(uuid.UUID(value["run_id"])) != value["run_id"]
                or value["packet_id"] not in CANONICAL_TMOS_PACKET_IDS
                or type(value["owner_manifest_digest"]) is not str
                or _DIGEST.fullmatch(value["owner_manifest_digest"]) is None
                or type(value["capture_nonce"]) is not str
                or re.fullmatch(r"[0-9a-f]{32}", value["capture_nonce"]) is None
                or value["unit"] != "odysseus-measure-" + value["capture_nonce"] + ".service"
                or type(value["seconds"]) not in (int, float)
                or not math.isfinite(value["seconds"]) or not 0 < value["seconds"] <= 180):
            raise ValueError("invalid ownership envelope")
        if action == "launch" and value["kind"] not in _KINDS:
            raise ValueError("unknown capture kind")
        if action == "membership" and (type(value["pid"]) is not int or value["pid"] <= 0):
            raise ValueError("unknown descendant")
        return value
    except (KeyError, ValueError, TypeError, AttributeError):
        raise MeasurementUnresolved("owned executor envelope unavailable") from None


def _intent(value):
    return {**{k: v for k, v in value.items() if k != "payload"},
            "payload_sha256": hashlib.sha256(_encoded(value["payload"])).hexdigest()}


def executor_cli(argv, *, worker=None):
    """Private fixed helper mode; callers cannot choose a file or command."""
    if (len(argv) != 3 or argv[0] not in ("--owned-executor", "--executor-unit")
            or argv[1] != "--owner-manifest-sha256" or not _DIGEST.fullmatch(argv[2])):
        raise MeasurementUnresolved("owned executor invocation unavailable")
    raw = _read_input()
    value = checked_envelope(decode_json(raw))
    from src.canonical_inspector_workspace import _manifest_file, manifest_digest
    manifest = _manifest_file(CANONICAL_TMOS_MANIFEST_FILE)
    if manifest_digest(manifest) != argv[2] or value["owner_manifest_digest"] != argv[2]:
        raise MeasurementUnresolved("owned executor manifest mismatch")
    transport = SshTransport(manifest, run_id=value["run_id"], packet_id=value["packet_id"],
                             deadline=time.monotonic() + value["seconds"])
    verify_installed_sources(transport.config)
    if argv[0] == "--executor-unit":
        return executor_unit(value, transport, worker=worker)
    if value["operation"] != "launch":
        print(json.dumps(executor_control(value, transport), allow_nan=False))
        return 0
    root = _directory(Path(CANONICAL_INSPECTOR_WORK_ROOT))
    run = _directory(root / value["run_id"], create=True)
    captures = _directory(run / CANONICAL_TMOS_TRANSPORT_EXECUTOR_DIRNAME, create=True)
    directory = _directory(captures / value["unit"], create=True)
    transport.validate_payload(value)
    _record_once(directory, CANONICAL_TMOS_CAPTURE_INTENT_FILENAME, _intent(value))
    command = [sys.executable, "-I", LOCAL_MEASUREMENT_COMMAND, "--executor-unit",
               "--owner-manifest-sha256", argv[2]]
    description = "odysseus-owned:" + value["capture_nonce"] + ":" + value["owner_manifest_digest"]
    projection = None
    primary = None
    try:
        bindings = ()
        config = transport.manifest['runtime'].get('ssh_config_projection')
        if config is not None and value['kind'] in ('identity', 'proof-normal', 'proof-descendant'):
            from src.local_measurement_config import ReadOnlyConfigProjection
            projection = ReadOnlyConfigProjection(config)
            bindings = projection.bindings()
            # This private record comes only from the installed projection,
            # never from SSH stdin or a caller-selected path/command.
            _record_once(directory, CANONICAL_TMOS_CAPTURE_PROJECTION_FILENAME,
                         {'proof': decode_json(projection.proof_command()[-1].encode())})
        launched = subprocess.run(systemd_command(value["unit"], value["seconds"], command,
            bindings=bindings, description=description), input=_encoded(value),
            timeout=value["seconds"] + 3, check=False, env=worker_environment())
        return launched.returncode
    except BaseException as failure:
        primary = failure
        raise
    finally:
        if projection is not None:
            try:
                from src.canonical_inspector_workspace import _private_bytes
                birth = decode_json(_private_bytes(directory / CANONICAL_TMOS_CAPTURE_BIRTH_FILENAME, 16384))
                state = executor_control({**{key: item for key, item in value.items() if key not in ('kind', 'payload')},
                    'operation': 'state', 'birth': birth}, transport)
                stopped = (state['LoadState'] == 'not-found' or state['ActiveState'] in ('inactive', 'failed')) and state['populated'] in (0, None)
                if not stopped:
                    raise MeasurementUnresolved('executor configuration reader termination unknown')
                projection.verify()
                if projection.close() is not True:
                    raise MeasurementUnresolved('executor configuration cleanup unknown')
                _record_once(directory, CANONICAL_TMOS_CAPTURE_COMPLETE_FILENAME, {'configuration_cleanup': 'REMOVED'})
            except BaseException as cleanup:
                from src.local_measurement_config import ConfigProjectionRefused
                retained = projection.retain_for_reconciliation()
                raise ConfigProjectionRefused(primary or cleanup, cleanup_state='UNKNOWN', retained_directory=retained) from None


def _executor_directory(value):
    root = _directory(Path(CANONICAL_INSPECTOR_WORK_ROOT))
    return _directory(_directory(_directory(root / value["run_id"]) / CANONICAL_TMOS_TRANSPORT_EXECUTOR_DIRNAME) / value["unit"])


def _unit_start_ticks(pid):
    # The command may contain spaces/parentheses; fields after the final ')'
    # begin at kernel stat field3, so field22 is index19 there.
    text = _kernel_text("/proc/" + str(pid) + "/stat")
    return int(text.rsplit(")", 1)[1].split()[19])


def executor_unit(value, transport, *, worker):
    from src.canonical_inspector_workspace import _private_bytes
    if value["operation"] != "launch":
        raise MeasurementUnresolved("unit operation unavailable")
    directory = _executor_directory(value)
    intent = decode_json(_private_bytes(directory / CANONICAL_TMOS_CAPTURE_INTENT_FILENAME, 65536))
    transport.validate_payload(value)
    if _encoded(intent) != _encoded(_intent(value)):
        raise MeasurementUnresolved("unit intent mismatch")
    invocation = os.environ.get("INVOCATION_ID", "")
    groups = _kernel_text("/proc/self/cgroup").splitlines()
    cgroups = [line[3:] for line in groups if line.startswith("0::")]
    observed = executor_identity()
    if (not re.fullmatch(r"[0-9a-f]{32}", invocation) or len(cgroups) != 1
            or Path(cgroups[0]).name != value["unit"] or ".." in Path(cgroups[0]).parts
            or _encoded(observed) != _encoded(transport.config["executor"])):
        raise MeasurementUnresolved("unit ownership mismatch")
    birth = {**transport._ownership(value["unit"]), "schema": "owned-measurement-birth-v1",
        "invocation_id": invocation, "cgroup": cgroups[0], "pid": os.getpid(),
        "start_ticks": _unit_start_ticks(os.getpid()), "executor": observed}
    transport._validate_birth(birth, value["unit"])
    _record_once(directory, CANONICAL_TMOS_CAPTURE_BIRTH_FILENAME, birth)
    print(json.dumps(birth, allow_nan=False), flush=True)
    if value["kind"] == "proof-normal":
        if transport.manifest['runtime'].get('ssh_config_projection') is not None:
            from src.local_measurement_config import _PROJECTION_PROOF
            proof = decode_json(_private_bytes(directory / CANONICAL_TMOS_CAPTURE_PROJECTION_FILENAME, 65536))
            if type(proof) is not dict or set(proof) != {'proof'}:
                raise MeasurementUnresolved('executor configuration proof unavailable')
            os.execvpe(sys.executable, [sys.executable, '-I', '-c', _PROJECTION_PROOF,
                                      json.dumps(proof['proof'], allow_nan=False)], worker_environment())
        print("owned-output", flush=True)
        print("owned-error", file=sys.stderr, flush=True)
        return 0
    if value["kind"] == "proof-descendant":
        child = subprocess.Popen([sys.executable, "-I", "-c", "import time; time.sleep(60)"],
            start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _record_once(directory, CANONICAL_TMOS_CAPTURE_DESCENDANT_FILENAME, {"pid": child.pid, "start_ticks": _unit_start_ticks(child.pid)})
        print(child.pid, flush=True)
        return 0
    if value["kind"] == "identity":
        command = transport.manifest["runtime"]["identity_commands"][value["payload"]["target_id"]]
        if (type(command) is not list or not command or len(command) > 64
                or any(type(part) is not str or not part or "\0" in part or len(part) > 2048 for part in command)):
            raise MeasurementUnresolved("identity command unavailable")
        verify_identity_sources(transport.manifest['runtime'], command)
        os.execvpe(command[0], command, worker_environment())
    if worker is None:
        raise MeasurementUnresolved("unit worker unavailable")
    runtime = transport.manifest["runtime"]
    worker(SimpleNamespace(worker_workspace=value["kind"] == "workspace", worker_http=value["kind"] == "provider",
        tmos_manifest=Path(CANONICAL_TMOS_MANIFEST_FILE), lease_client=runtime.get("lease_client"),
        lease_client_sha256=runtime.get("lease_client_sha256"), broker_url=transport.config["broker_url"]),
        input_bytes=_encoded(value["payload"]))
    return 0


async def _native_control(*args):
    # Reuse the normal bounded native manager interface. It owns/reaps its
    # process and caps both streams before decoding.
    from src.local_measurement_process import _control
    return await _control(*args)


def executor_control(value, transport):
    from src.canonical_inspector_workspace import _private_bytes
    from src.local_measurement_process import CGROUP_ROOT
    directory = _executor_directory(value)
    birth = decode_json(_private_bytes(directory / CANONICAL_TMOS_CAPTURE_BIRTH_FILENAME, 16384))
    transport._validate_birth(birth, value["unit"])
    if _encoded(birth) != _encoded(value["birth"]):
        raise MeasurementUnresolved("control birth mismatch")
    group = (CGROUP_ROOT / birth["cgroup"].lstrip("/")).resolve()
    if not group.is_relative_to(CGROUP_ROOT) or group.name != value["unit"]:
        raise MeasurementUnresolved("control cgroup mismatch")
    code, raw, _ = asyncio.run(_native_control("show", "--no-pager",
        "--property=LoadState,ActiveState,SubState,ControlGroup,ExitType,KillMode,InvocationID,Description", "--", value["unit"]))
    state = dict(line.split("=", 1) for line in raw.decode().splitlines() if "=" in line)
    missing = state.get("LoadState") == "not-found"
    if not missing and (code != 0 or state.get("InvocationID") != birth["invocation_id"]
            or state.get("Description") != "odysseus-owned:" + value["capture_nonce"] + ":" + value["owner_manifest_digest"]
            or state.get("ControlGroup") != birth["cgroup"] or state.get("ExitType") != "cgroup"
            or state.get("KillMode") != "control-group"):
        raise MeasurementUnresolved("native unit ownership unavailable")
    if value["operation"] == "kill" and not missing:
        killed, _, _ = asyncio.run(_native_control("kill", "--signal=SIGKILL", "--kill-whom=all", "--", value["unit"]))
        if killed != 0:
            raise MeasurementUnresolved("native termination acknowledgement unavailable")
    if value["operation"] == "membership":
        child = decode_json(_private_bytes(directory / CANONICAL_TMOS_CAPTURE_DESCENDANT_FILENAME, 4096))
        member = (not missing and child["pid"] == value["pid"]
            and child["start_ticks"] == _unit_start_ticks(value["pid"])
            and "0::" + birth["cgroup"] in _kernel_text("/proc/" + str(value["pid"]) + "/cgroup").splitlines())
        return {"birth": birth, "pid": value["pid"], "member": member}
    try:
        events = dict(line.split() for line in _kernel_text(group / "cgroup.events").splitlines())
        if events.get("populated") not in ("0", "1"):
            raise MeasurementUnresolved("native cgroup evidence unavailable")
        populated = int(events["populated"])
    except FileNotFoundError:
        if group.exists():
            raise MeasurementUnresolved("native cgroup evidence unavailable") from None
        populated = None
    intent = decode_json(_private_bytes(directory / CANONICAL_TMOS_CAPTURE_INTENT_FILENAME, 65536))
    needs_projection = (transport.manifest['runtime'].get('ssh_config_projection') is not None
                        and intent['kind'] in ('identity', 'proof-normal', 'proof-descendant'))
    cleanup = 'NOT_CONFIGURED'
    if needs_projection:
        cleanup = 'UNKNOWN'
        try:
            complete = decode_json(_private_bytes(directory / CANONICAL_TMOS_CAPTURE_COMPLETE_FILENAME, 4096))
            if complete == {'configuration_cleanup': 'REMOVED'}:
                cleanup = 'REMOVED'
        except FileNotFoundError:
            pass
    return {"birth": birth, "LoadState": "not-found" if missing else state["LoadState"],
        "ActiveState": "inactive" if missing else state["ActiveState"],
        "SubState": "dead" if missing else state["SubState"], "ControlGroup": birth["cgroup"],
        "ExitType": None if missing else state["ExitType"],
        "KillMode": None if missing else state["KillMode"], "populated": populated,
        "configuration_cleanup": cleanup}


def _read_input():
    """Bound before buffering and independently of the SSH client's lifetime."""
    deadline = time.monotonic() + 5
    raw = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(sys.stdin.buffer, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise MeasurementUnresolved('owned executor input deadline exceeded')
            chunk = os.read(sys.stdin.buffer.fileno(), min(65536, 1048577 - len(raw)))
            if not chunk:
                return bytes(raw)
            raw.extend(chunk)
            if len(raw) > 1048576:
                raise MeasurementUnresolved('owned executor input exceeds budget')


def worker_environment():
    # Native user bus/data location are explicit existing interfaces. Python,
    # proxy, Git and Docker overrides do not cross into measured commands.
    preserved = ('HOME', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS', 'ODYSSEUS_DATA_DIR', 'INVOCATION_ID')
    return {**{name: os.environ[name] for name in preserved if name in os.environ},
            'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}


def verify_identity_sources(runtime, command):
    """Pin the installed collector's executable and declared source closure."""
    pins = runtime.get('identity_source_pins')
    if (type(pins) is not dict or not pins or len(pins) > 16
            or command[0] not in pins):
        raise MeasurementUnresolved('identity collector source pins unavailable')
    for name, expected in pins.items():
        _absolute(name)
        if type(expected) is not str or not _DIGEST.fullmatch(expected):
            raise MeasurementUnresolved('identity collector source pin unavailable')
        if hashlib.sha256(_source_bytes(Path(name), 67108864)).hexdigest() != expected:
            raise MeasurementUnresolved('identity collector source changed')
    # Every absolute script/config argument is part of the declared closure.
    if any(part.startswith('/') and part not in pins for part in command):
        raise MeasurementUnresolved('identity collector closure incomplete')


def _source_bytes(path, limit):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid())
                or info.st_mode & 0o022 or info.st_size > limit):
            raise MeasurementUnresolved("executor source unavailable")
        raw = stream.read(limit + 1)
        stamp = lambda value: (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_gid,
                               value.st_nlink, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        if (len(raw) != info.st_size or stamp(info) != stamp(os.fstat(stream.fileno()))
                or stamp(info) != stamp(Path(path).lstat())):
            raise MeasurementUnresolved("executor source changed")
        return raw


def _kernel_text(path, limit=4096):
    with open(path, "rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise MeasurementUnresolved("executor kernel evidence exceeds budget")
    return raw.decode().strip()


def executor_identity():
    return {"hostname": socket.gethostname(), "uid": os.getuid(), "gid": os.getgid(),
        "groups": sorted(set(os.getgroups())), "boot_id": _kernel_text("/proc/sys/kernel/random/boot_id"),
        "python_sha256": hashlib.sha256(_source_bytes(Path(sys.executable).resolve(), 67108864)).hexdigest()}


def verify_installed_sources(config):
    command = Path(LOCAL_MEASUREMENT_COMMAND).resolve()
    if (Path(config["command"]).resolve() != command
            or Path(config["python"]).resolve() != Path(sys.executable).resolve()):
        raise MeasurementUnresolved("executor installation mismatch")
    root = command.parent.parent
    for name, expected in config["source_pins"].items():
        if hashlib.sha256(_source_bytes(root / name, 1048576)).hexdigest() != expected:
            raise MeasurementUnresolved("executor source mismatch")
    observed = executor_identity()
    # Supplementary groups inside the native unit are observed again at birth.
    # The SSH launcher is a separate vantage and is not substituted for it.
    if any(observed[k] != config["executor"][k] for k in observed if k != "groups"):
        raise MeasurementUnresolved("executor identity mismatch")
