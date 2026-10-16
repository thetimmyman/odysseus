"""One-shot ownership controls; every broker/provider here is synthetic."""
import copy
import json
from datetime import timedelta

import pytest

from src.local_measurement_owner import BrokerLease, MeasurementUnresolved
from tests.test_local_target_qualification import NOW, profile, identity, good_response
from src.local_targets import HostBaseline


class Broker:
    def __init__(self):
        self.calls = []
        self.responses = {}

    def __call__(self, path, body):
        self.calls.append((path, body))
        if path in self.responses:
            value = self.responses[path]
            if isinstance(value, Exception):
                raise value
            return value
        if path == '/acquire':
            return 200, {'lease': 'a'*12, 'pool': 'rtx4500',
                         'url': 'http://worker.invalid:11434', 'ttl': 600}
        return 200, {'ok': True}


def test_lost_heartbeat_is_sticky_and_never_releases_an_uncertain_request():
    broker = Broker()
    lease = BrokerLease(broker, endpoint='http://worker.invalid:11434', holder='synthetic')
    lease.acquire()
    try:
        lease.check()
        broker.responses['/heartbeat'] = OSError('lost response')
        with pytest.raises(MeasurementUnresolved):
            lease.check()
        broker.responses.clear()
        assert lease.valid() is False
        with pytest.raises(MeasurementUnresolved):
            lease.check()
        with pytest.raises(MeasurementUnresolved):
            lease.release_completed()
        assert [path for path, _ in broker.calls] == ['/acquire', '/heartbeat', '/heartbeat']
    finally:
        lease.abandon()


def inputs():
    spec, old = profile(host=HostBaseline(host_id='synthetic-worker', kernel='old',
                                        boot_cmdline_digest='a'*64))
    before = identity(old)
    before['current_material_identity']['host']['kernel'] = 'new'
    before['binding']['endpoint_bound'] = True
    return spec, old, before


class Processes:
    qualified = True  # A synthetic adapter, not operational qualification.

    def __init__(self, before):
        self.before = before
        self.calls = []
        self.failure = None

    def identity(self, command, *, lease_valid):
        assert lease_valid()
        self.calls.append('identity')
        if self.failure == 'post' and self.calls.count('identity') == 2:
            raise OSError('post unavailable')
        return copy.deepcopy(self.before)

    def request(self, url, payload, *, lease_valid):
        assert lease_valid()
        self.calls.append('request')
        if self.failure == 'request':
            raise OSError('native outcome unknown')
        return good_response('ollama', payload)


def test_candidate_published_only_after_two_completions_post_identity_and_release(tmp_path):
    from src.local_measurement_owner import measure_once
    spec, old, before = inputs()
    processes, broker = Processes(before), Broker()
    # The lease must bind the registered endpoint, never pick its own target.
    broker.responses['/acquire'] = (200, {'lease':'a'*12, 'pool':'rtx4500',
                                        'url':spec.endpoint, 'ttl':600})
    lease = BrokerLease(broker, endpoint=spec.endpoint, holder='synthetic')
    result = measure_once(spec, old, ['trusted-collector'], tmp_path/'run',
                          lease=lease, processes=processes, now=NOW)
    assert processes.calls == ['identity', 'request', 'request', 'identity']
    assert [p for p, _ in broker.calls] == ['/acquire'] + ['/heartbeat']*5 + ['/release']
    receipt = json.loads((tmp_path/'run'/'receipt.json').read_text())
    assert receipt['receipt_hash'] == result.receipt_hash
    assert receipt['profile_id'] != old.profile_id
    assert receipt['supersedes'] == ''
    assert result.context.safe_working_context == 4096
    assert result.qualification_state(now=NOW+timedelta(seconds=1)) == 'valid'
    ownership = json.loads((tmp_path/'run'/'ownership.json').read_text())
    assert ownership['state'] == 'RELEASED' and ownership['release_acknowledged'] is True
    assert [e['action'] for e in ownership['events']] == ['acquire_ack']+['heartbeat_ack']*5+['release_ack']


@pytest.mark.parametrize('failure', ['request', 'post', 'release', 'heartbeat', 'unqualified'])
def test_uncertain_run_preserves_evidence_without_receipt_or_automatic_release(tmp_path, failure):
    from src.local_measurement_owner import measure_once
    spec, old, before = inputs()
    processes, broker = Processes(before), Broker()
    processes.failure = failure
    broker.responses['/acquire'] = (200, {'lease':'a'*12, 'pool':'rtx4500',
                                        'url':spec.endpoint, 'ttl':600})
    if failure == 'release':
        broker.responses['/release'] = OSError('release response lost')
    if failure == 'heartbeat':
        broker.responses['/heartbeat'] = (404, {'error':'expired'})
    if failure == 'unqualified':
        processes.qualified = False
    lease = BrokerLease(broker, endpoint=spec.endpoint, holder='synthetic')
    output = tmp_path/'run'
    with pytest.raises(MeasurementUnresolved):
        measure_once(spec, old, ['collector'], output, lease=lease, processes=processes, now=NOW)
    assert not (output/'receipt.json').exists()
    failure_record = json.loads((output/'failure.json').read_text())
    assert failure_record['status'] == 'UNRESOLVED'
    assert failure_record['lease']['capacity_available_proven'] is False
    assert failure_record['lease']['remote_stop_proven'] is False
    if failure != 'release':
        assert '/release' not in [p for p, _ in broker.calls]
    if failure in ('request', 'heartbeat', 'unqualified'):
        assert processes.calls.count('request') <= 1


@pytest.mark.parametrize('field,value', [('pool','other'), ('url','http://other.invalid'),
                                      ('ttl',True), ('lease','a'*32)])
def test_mismatched_grant_is_refused_and_retained_without_release(field, value):
    broker = Broker()
    grant = {'lease':'a'*12, 'pool':'rtx4500', 'url':'http://worker.invalid:11434', 'ttl':600}
    grant[field] = value
    broker.responses['/acquire'] = 200, grant
    lease = BrokerLease(broker, endpoint='http://worker.invalid:11434', holder='synthetic')
    with pytest.raises(MeasurementUnresolved):
        lease.acquire()
    assert lease.valid() is False
    assert lease.evidence()['lease'] == grant['lease']
    assert [p for p, _ in broker.calls] == ['/acquire']


def test_expiry_cannot_be_refreshed_and_ambiguous_acquisition_is_not_replayed():
    broker = Broker()
    clock = [0]
    lease = BrokerLease(broker, endpoint='http://worker.invalid:11434', holder='synthetic',
                        clock=lambda: clock[0])
    lease.acquire()
    try:
        clock[0] = 590
        assert lease.valid() is False
        with pytest.raises(MeasurementUnresolved):
            lease.check()
        assert len(broker.calls) == 1
    finally:
        lease.abandon()
    broker = Broker()
    broker.responses['/acquire'] = OSError('grant response unknown')
    lease = BrokerLease(broker, endpoint='http://worker.invalid:11434', holder='synthetic')
    with pytest.raises(MeasurementUnresolved):
        lease.acquire()
    assert [p for p, _ in broker.calls] == ['/acquire']
