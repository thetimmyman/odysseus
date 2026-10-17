"""One-shot ownership controls; every broker/provider here is synthetic."""
import copy
import json
from datetime import timedelta
from dataclasses import replace

import pytest

from src.local_measurement_owner import BrokerLease, MeasurementUnresolved
from tests.test_local_target_qualification import NOW, profile, identity, good_response, new_profile_inputs
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

    def evidence(self):
        return [{'unit':'owned-synthetic.service', 'kind':'provider', 'local_stop_proven':False}]


def test_fresh_descriptor_publishes_only_after_post_identity_cleanup_and_normal_release(tmp_path):
    from src.local_measurement_owner import measure_once
    spec, descriptor, before = new_profile_inputs()

    class Captures(Processes):
        cleanup_done = False

        def finish_capture(self):
            self.cleanup_done = True
            self.calls.append('finish')
            return True

        def configuration_evidence(self):
            return 'REMOVED' if self.cleanup_done else 'READY'

    processes = Captures(before)

    class ReleaseAfterCleanup(Broker):
        def __call__(self, path, body):
            if path == '/release':
                assert processes.calls == ['identity', 'request', 'request', 'identity', 'finish']
                assert processes.cleanup_done
                assert not (tmp_path/'run'/'receipt.json').exists()
            return super().__call__(path, body)

    broker = ReleaseAfterCleanup()
    broker.responses['/acquire'] = 200, {'lease':'a'*12, 'pool':'rtx4500',
                                       'url':spec.endpoint, 'ttl':600}
    lease = BrokerLease(broker, endpoint=spec.endpoint, holder='synthetic')
    candidate = measure_once(spec, descriptor, ['collector'], tmp_path/'run',
        new_profile=True, lease=lease, processes=processes, now=NOW)
    receipt = json.loads((tmp_path/'run'/'receipt.json').read_text())
    ownership = json.loads((tmp_path/'run'/'ownership.json').read_text())
    assert receipt == candidate.to_dict()
    assert ownership['configuration_cleanup'] == 'REMOVED'
    assert ownership['release_acknowledged'] is True
    assert candidate.qualification_ref == candidate.context.safe_context_source
    assert candidate.supersedes == ''
    assert candidate.context.safe_working_context == 4096
    assert descriptor.qualification_state(now=NOW) == 'disposition_unqualified'


@pytest.mark.parametrize('failure', ['cleanup_absent', 'cleanup_false', 'cleanup_raise',
    'request', 'post', 'release', 'heartbeat', 'unqualified', 'combined_route', 'nonboolean_mode'])
def test_fresh_profile_uncertainty_never_publishes_or_releases_in_finally(tmp_path, failure):
    from src.local_measurement_owner import measure_once
    spec, descriptor, before = new_profile_inputs()

    class Captures(Processes):
        def finish_capture(self):
            if failure == 'cleanup_raise': raise OSError('private-cleanup-value')
            return failure != 'cleanup_false'

    processes, broker = Captures(before), Broker()
    processes.failure = failure
    if failure == 'cleanup_absent': processes.finish_capture = None
    if failure == 'unqualified': processes.qualified = False
    broker.responses['/acquire'] = 200, {'lease':'a'*12, 'pool':'rtx4500',
                                       'url':spec.endpoint, 'ttl':600}
    if failure == 'release': broker.responses['/release'] = OSError('lost ACK')
    if failure == 'heartbeat': broker.responses['/heartbeat'] = 404, {'error':'expired'}
    lease = BrokerLease(broker, endpoint=spec.endpoint, holder='synthetic')
    with pytest.raises(MeasurementUnresolved):
        measure_once(spec, descriptor, ['collector'], tmp_path/'run',
            new_profile=1 if failure == 'nonboolean_mode' else True,
            current_spec=spec if failure == 'combined_route' else None,
            lease=lease, processes=processes, now=NOW)
    assert not (tmp_path/'run'/'receipt.json').exists()
    record = json.loads((tmp_path/'run'/'failure.json').read_text())
    assert record['status'] == 'UNRESOLVED'
    assert record['receipt_accepted'] is False
    assert record['lease']['release_acknowledged'] is False
    assert 'private-cleanup-value' not in json.dumps(record)
    if failure != 'release': assert '/release' not in [path for path,_ in broker.calls]
    if failure == 'request': assert processes.calls.count('request') == 1
    if failure in ('cleanup_absent', 'unqualified', 'combined_route', 'nonboolean_mode'):
        assert broker.calls == [] and processes.calls == []


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


def test_failed_private_capture_cleanup_prevents_release_and_receipt_publication(tmp_path):
    from src.local_measurement_owner import measure_once
    spec, old, before = inputs()

    class CleanupFailure(Processes):
        def finish_capture(self):
            raise OSError('SYNTHETIC_PRIVATE_CLEANUP_VALUE')

        def configuration_evidence(self):
            return 'CLEANUP_UNRESOLVED'

    processes, broker = CleanupFailure(before), Broker()
    broker.responses['/acquire'] = 200, {'lease': 'a' * 12, 'pool': 'rtx4500',
                                       'url': spec.endpoint, 'ttl': 600}
    lease = BrokerLease(broker, endpoint=spec.endpoint, holder='synthetic')
    output = tmp_path / 'run'
    with pytest.raises(MeasurementUnresolved):
        measure_once(spec, old, ['collector'], output, lease=lease, processes=processes, now=NOW)
    assert not (output / 'receipt.json').exists()
    assert '/release' not in [path for path, _ in broker.calls]
    failure = json.loads((output / 'failure.json').read_text())
    assert failure['failed_stage'] == 'capture_cleanup'
    assert failure['configuration_cleanup'] == 'CLEANUP_UNRESOLVED'
    assert 'SYNTHETIC_PRIVATE_CLEANUP_VALUE' not in json.dumps(failure)


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
    assert failure_record['processes'] == [{'unit':'owned-synthetic.service',
                                          'kind':'provider', 'local_stop_proven':False}]
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


def test_cleanup_failure_does_not_hide_first_failure_or_lose_reconciliation(tmp_path):
    from src.local_measurement_owner import measure_once
    spec, old, before = inputs()
    processes, broker = Processes(before), Broker()
    processes.failure = 'request'
    broker.responses['/acquire'] = (200, {'lease':'a'*12, 'pool':'rtx4500',
                                        'url':spec.endpoint, 'ttl':600})

    class CleanupFailure(BrokerLease):
        def abandon(self):
            super().abandon()
            raise OSError('cleanup unavailable')

    lease = CleanupFailure(broker, endpoint=spec.endpoint, holder='synthetic')
    output = tmp_path/'run'
    with pytest.raises(MeasurementUnresolved):
        measure_once(spec, old, ['collector'], output, lease=lease, processes=processes, now=NOW)
    evidence = json.loads((output/'failure.json').read_text())
    assert evidence['failure_class'] == 'QualificationError'
    assert evidence['failed_stage'] == 'provider_request'
    assert evidence['cleanup'] == {'state':'UNKNOWN', 'failure_class':'OSError'}
    assert evidence['processes'][0]['unit'] == 'owned-synthetic.service'
    assert not (output/'receipt.json').exists()
    assert '/release' not in [p for p, _ in broker.calls]


def test_explicit_current_spec_binds_the_normal_lease_and_both_requests(tmp_path):
    from src.local_measurement_owner import measure_once
    old_spec, old, before = inputs()
    current_spec = replace(old_spec, endpoint='http://route.invalid')
    before['current_material_identity']['runtime']['endpoint_url'] = current_spec.endpoint
    processes, broker = Processes(before), Broker()
    broker.responses['/acquire'] = 200, {'lease':'a'*12, 'pool':'rtx4500',
                                       'url':current_spec.endpoint, 'ttl':600}
    lease = BrokerLease(broker, endpoint=current_spec.endpoint, holder='synthetic')
    candidate = measure_once(old_spec, old, ['collector'], tmp_path/'run',
        current_spec=current_spec, lease=lease, processes=processes, now=NOW)
    assert candidate.runtime.endpoint_url == 'http://route.invalid'
    assert processes.calls == ['identity','request','request','identity']
    assert [p for p,_ in broker.calls][-1] == '/release'
    report = json.loads((tmp_path/'run'/'qualification.json').read_text())
    assert report['mode'] == 'CHANGED_ROUTE_NEW_PROFILE'


@pytest.mark.parametrize('failure', ['old_lease_target', 'post', 'request', 'release', 'changed_role'])
def test_route_owner_preserves_closed_failure_and_never_accepts_a_candidate(tmp_path, failure):
    from src.local_measurement_owner import measure_once
    old_spec, old, before = inputs()
    current_spec = replace(old_spec, endpoint='http://route.invalid')
    before['current_material_identity']['runtime']['endpoint_url'] = current_spec.endpoint
    processes, broker = Processes(before), Broker()
    processes.failure = failure
    broker.responses['/acquire'] = 200, {'lease':'a'*12, 'pool':'rtx4500',
                                       'url':current_spec.endpoint, 'ttl':600}
    if failure == 'release': broker.responses['/release'] = OSError('ack lost')
    if failure == 'changed_role': current_spec = replace(current_spec, roles=('inference','other'))
    endpoint = old_spec.endpoint if failure == 'old_lease_target' else current_spec.endpoint
    lease = BrokerLease(broker, endpoint=endpoint, holder='synthetic')
    folder = tmp_path/'run'
    with pytest.raises(MeasurementUnresolved):
        measure_once(old_spec, old, ['collector'], folder, current_spec=current_spec,
                     lease=lease, processes=processes, now=NOW)
    assert not (folder/'receipt.json').exists()
    assert json.loads((folder/'failure.json').read_text())['status'] == 'UNRESOLVED'
    if failure != 'release':
        assert '/release' not in [p for p,_ in broker.calls]
    if failure in ('old_lease_target','changed_role'):
        assert processes.calls.count('request') == 0
