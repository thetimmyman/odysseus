"""One bounded changed-host measurement; no store application or routing policy.

The normal broker's lease ID is the authority. Loss of its acknowledgement is
sticky, and abandonment deliberately does not release an uncertain request.
TTL expiry is neither remote cancellation nor proof of available capacity.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
import time

from src.endpoint_identity import canonical_endpoint_identity
from src.constants import (LOCAL_QUALIFICATION_BEFORE_FILENAME, LOCAL_QUALIFICATION_AFTER_FILENAME,
                           LOCAL_QUALIFICATION_FAILURE_FILENAME, LOCAL_QUALIFICATION_RECEIPT_FILENAME,
                           LOCAL_MEASUREMENT_OWNERSHIP_FILENAME)
from src.local_target_qualification import measure_changed_host_profile, measure_changed_route_profile


class MeasurementUnresolved(ValueError):
    """No receipt may be accepted; the owner must reconcile retained evidence."""


class BrokerLease:
    """Own one normal P1 RTX lease, using the existing broker wire contract.

    post must be a bounded transport, including a total deadline and byte cap.
    It is injected so tests and the fixed process adapter cross the same seam.
    """

    def __init__(self, post, *, endpoint, holder, clock=time.monotonic,
                 wall_clock=time.time):
        if not re.fullmatch(r'[a-zA-Z0-9-]{1,80}', holder):
            raise MeasurementUnresolved('invalid lease holder')
        self._post, self._endpoint, self._holder = post, endpoint, holder
        self._clock, self._wall_clock = clock, wall_clock
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._state = 'NEW'
        self._id = None
        self._expires = 0
        self._events = []
        self._release_ack = False

    def _fail(self):
        self._state = 'UNRESOLVED'
        self._stop.set()
        raise MeasurementUnresolved('lease unavailable; owner reconciliation required') from None

    def acquire(self):
        with self._lock:
            if self._state != 'NEW':
                self._fail()
            self._state = 'ACQUIRING'
            arrived, deadline = self._wall_clock(), self._clock() + 30
            try:
                while self._clock() < deadline:
                    sent = self._clock()
                    status, body = self._post('/acquire', {
                        'priority': 1, 'pool': 'rtx4500', 'holder': self._holder,
                        'ttl': 600, 'arrived': arrived, 'wait': min(2, deadline - sent)})
                    if status == 202 and body == {'lease': None, 'retry': True, 'arrived': arrived}:
                        continue
                    if type(body) is dict and isinstance(body.get('lease'), str):
                        self._id = body['lease']  # Retain even a mismatched grant for reconciliation.
                    if (status != 200 or type(body) is not dict
                            or set(body) != {'lease', 'pool', 'url', 'ttl'}
                            or not isinstance(body['lease'], str)
                            or not re.fullmatch(r'[0-9a-f]{12}', body['lease'])
                            or body['pool'] != 'rtx4500'
                            or type(body['ttl']) not in (int, float) or body['ttl'] != 600
                            or canonical_endpoint_identity(body['url'], 'http')
                            != canonical_endpoint_identity(self._endpoint, 'http')):
                        self._fail()
                    self._id, self._expires, self._state = body['lease'], sent + 590, 'HELD'
                    self._events.append({'action':'acquire_ack', 'at':self._wall_clock(),
                                         'lease':self._id, 'pool':body['pool'], 'ttl':body['ttl']})
                    if not self.valid():
                        self._fail()
                    self._thread = threading.Thread(target=self._keep, daemon=True)
                    self._thread.start()
                    return
            except Exception:
                self._fail()
            self._fail()

    def valid(self):
        # Never wait on a network lock from collect_process's lease watcher.
        if self._state != 'HELD' or self._clock() >= self._expires:
            if self._state == 'HELD':
                self._state = 'UNRESOLVED'
                self._stop.set()
            return False
        return True

    def check(self):
        with self._lock:
            if not self.valid():
                self._fail()
            sent = self._clock()
            try:
                status, body = self._post('/heartbeat', {'lease': self._id})
                if (status != 200 or type(body) is not dict or set(body) != {'ok'}
                        or body['ok'] is not True or not self.valid()):
                    self._fail()
            except Exception:
                self._fail()
            self._expires = sent + 590
            self._events.append({'action':'heartbeat_ack', 'at':self._wall_clock(), 'lease':self._id})

    def _keep(self):
        while not self._stop.wait(30):
            try:
                self.check()
            except MeasurementUnresolved:
                return

    def _stop_keeper(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                self._fail()

    def release_completed(self):
        """Called only after both native completions and stable post-identity."""
        self._stop_keeper()
        with self._lock:
            self.check()
            try:
                status, body = self._post('/release', {'lease': self._id})
                if (status != 200 or type(body) is not dict or set(body) != {'ok'}
                        or body['ok'] is not True or not self.valid()):
                    self._fail()
            except Exception:
                self._fail()
            self._state = 'RELEASED'
            self._release_ack = True
            self._events.append({'action':'release_ack', 'at':self._wall_clock(), 'lease':self._id})

    def abandon(self):
        """Stop local renewal, retaining uncertainty; NEVER call /release here."""
        self._state = 'UNRESOLVED'
        self._stop_keeper()

    @property
    def endpoint_identity(self):
        return canonical_endpoint_identity(self._endpoint, 'http')

    def evidence(self):
        return {'state': self._state, 'lease': self._id, 'holder': self._holder,
                'pool': 'rtx4500', 'remote_stop_proven': False,
                'capacity_available_proven': False, 'release_acknowledged':self._release_ack,
                'events':list(self._events)}


def _write(path, value):
    with path.open('xb') as handle:
        handle.write((json.dumps(value, allow_nan=False, sort_keys=True, indent=2)+'\n').encode())


def _process_evidence(processes):
    # A missing diagnostic source is unavailable, never invented stop proof.
    try:
        return processes.evidence()
    except Exception:
        return None


def _configuration_evidence(processes):
    try:
        value = processes.configuration_evidence()
        return value if value in {'NOT_CONFIGURED', 'READY', 'REMOVED',
            'POSTCHECK_REFUSED', 'CLEANUP_UNRESOLVED'} else 'UNAVAILABLE'
    except Exception:
        return 'UNAVAILABLE'


def measure_once(spec, previous, identity_command, output_dir, *, lease, processes,
                 current_spec=None, now=None):
    """Run the existing two trials once; publish only after explicit release ACK.

    processes supplies bounded, qualified owned-client captures. Successful
    captures establish local exit only. The existing qualifier separately
    validates native done/model/tokens/tool semantics and stable post-identity.
    Any failure retains private evidence and requires owner reconciliation.
    """
    folder = Path(output_dir)
    folder.mkdir(mode=0o700, parents=True, exist_ok=False)
    execution_spec = current_spec if current_spec is not None else spec
    stage = 'binding'
    try:
        if (getattr(processes, 'qualified', False) is not True
                or execution_spec.runtime_kind != 'ollama' or execution_spec.transport != 'http'
                or execution_spec.max_concurrency != 1
                or lease.endpoint_identity != canonical_endpoint_identity(execution_spec.endpoint, 'http')
                or type(identity_command) is not list or not identity_command
                or any(type(arg) is not str or not arg for arg in identity_command)):
            raise MeasurementUnresolved('measurement binding unavailable')
        stage = 'acquire'
        lease.acquire()

        def capture(filename):
            nonlocal stage
            stage = filename
            lease.check()
            observation = processes.identity(identity_command, lease_valid=lease.valid)
            _write(folder / filename, observation)
            return observation

        def send(url, payload):
            nonlocal stage
            stage = 'provider_request'
            if url != execution_spec.endpoint.rstrip('/') + '/api/chat':
                raise MeasurementUnresolved('measurement endpoint mismatch')
            lease.check()  # Fresh authoritative ACK immediately before EACH request.
            return processes.request(url, payload, lease_valid=lease.valid)

        before = capture(LOCAL_QUALIFICATION_BEFORE_FILENAME)
        after = lambda: capture(LOCAL_QUALIFICATION_AFTER_FILENAME)
        if current_spec is None:
            candidate, _ = measure_changed_host_profile(spec, previous, before, after,
                                                        folder, transport=send, now=now)
        else:
            candidate, _ = measure_changed_route_profile(spec, current_spec, previous, before,
                                                         after, folder, transport=send, now=now)
        # Private capture resources must be finalized BEFORE release and the
        # last atomic publication. Cleanup cannot invalidate a published receipt.
        stage = 'capture_cleanup'
        finish = getattr(processes, 'finish_capture', None)
        if finish is not None and finish() is not True:
            raise MeasurementUnresolved('capture cleanup unresolved')
        # No finally-release: all native completion and post-identity checks
        # above must return successfully before the explicit acknowledged end.
        stage = 'release_acknowledgement'
        lease.release_completed()
        _write(folder / LOCAL_MEASUREMENT_OWNERSHIP_FILENAME,
               {**lease.evidence(), 'processes':_process_evidence(processes),
                'configuration_cleanup':_configuration_evidence(processes)})
        stage = 'receipt_publication'
        pending = folder / ('.' + LOCAL_QUALIFICATION_RECEIPT_FILENAME + '.pending')
        _write(pending, candidate.to_dict())
        os.link(pending, folder / LOCAL_QUALIFICATION_RECEIPT_FILENAME)
        return candidate
    except BaseException as exc:
        cleanup = {'state':'KEEPER_STOPPED'}
        try:
            lease.abandon()
        except BaseException as cleanup_error:
            cleanup = {'state':'UNKNOWN', 'failure_class':type(cleanup_error).__name__}
        _write(folder / LOCAL_QUALIFICATION_FAILURE_FILENAME, {
            'status': 'UNRESOLVED', 'failure_class': type(exc).__name__,
            'failed_stage': stage,
            'lease': lease.evidence(), 'receipt_accepted': False,
            'cleanup':cleanup, 'processes':_process_evidence(processes),
            'configuration_cleanup':_configuration_evidence(processes),
            'action': 'owner reconciliation required; no retry, import or activation'})
        if not isinstance(exc, Exception):
            raise
        raise MeasurementUnresolved('measurement unresolved; no receipt accepted') from None
