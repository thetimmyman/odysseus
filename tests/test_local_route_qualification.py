"""Explicit endpoint remeasurement with synthetic native evidence only."""
import copy
from dataclasses import replace
import hashlib
import json
from datetime import timedelta

import pytest

from src.local_target_qualification import (QualificationError, measure_changed_route_profile,
                                            measure_changed_host_profile, qualify_existing_profile)
from src.local_targets import HostBaseline
from tests.test_local_target_qualification import NOW, profile, identity, good_response


def inputs():
    old_spec, old = profile(host=HostBaseline(host_id='synthetic-worker', kernel='old',
                                             boot_cmdline_digest='a'*64))
    current_spec = replace(old_spec, endpoint='http://route.invalid')
    observation = identity(old)
    observation['current_material_identity']['host']['kernel'] = 'new'
    observation['current_material_identity']['runtime']['endpoint_url'] = current_spec.endpoint
    observation['binding']['endpoint_bound'] = True
    return old_spec, current_spec, old, observation


def test_observed_hostname_is_measured_as_new_profile_without_rewriting_history(tmp_path):
    old_spec, current_spec, old, observation = inputs()
    original = copy.deepcopy(old.to_dict())
    calls = []

    def send(url, payload):
        calls.append(url)
        return good_response('ollama', payload)

    candidate, report = measure_changed_route_profile(old_spec, current_spec, old,
        observation, lambda:copy.deepcopy(observation), tmp_path, transport=send, now=NOW)
    assert calls == ['http://route.invalid/api/chat']*2
    assert old.to_dict() == original
    assert candidate.profile_id != old.profile_id and candidate.supersedes == ''
    assert candidate.runtime.endpoint_url == 'http://route.invalid'
    assert candidate.context.safe_working_context == candidate.context.semantic_verified_context == 4096
    assert candidate.routing_exactness() == 'approximate'
    assert candidate.qualification_ref == candidate.context.safe_context_source
    assert candidate.qualification_ref == 'sha256:'+hashlib.sha256((tmp_path/'qualification.json').read_bytes()).hexdigest()
    assert report['mode'] == 'CHANGED_ROUTE_NEW_PROFILE'
    assert json.loads((tmp_path/'qualification.json').read_bytes()) == report


@pytest.mark.parametrize('url', ['http://synthetic.invalid', 'http://SYNTHETIC.invalid/',
    'https://route.invalid', 'http://route.invalid:81', 'http://route.invalid/other',
    'http://route.invalid?q=1', 'http://route.invalid#fragment',
    'http://synthetic-user:synthetic-password@route.invalid', ' http://route.invalid'])
def test_only_a_credential_free_hostname_delta_is_supported(tmp_path, url):
    old_spec, current_spec, old, observation = inputs()
    current_spec = replace(current_spec, endpoint=url)
    observation['current_material_identity']['runtime']['endpoint_url'] = url
    calls = []
    with pytest.raises(QualificationError):
        measure_changed_route_profile(old_spec, current_spec, old, observation, lambda:observation,
            tmp_path, transport=lambda *args:calls.append(args), now=NOW)
    assert calls == []


@pytest.mark.parametrize('change', [{'target_id':'other'}, {'label':'other'}, {'ssh_host':'other'},
    {'model':'other'}, {'runtime_kind':'other'}, {'privacy_class':'other'}, {'network_class':'other'},
    {'max_concurrency':2}, {'max_concurrency':True}, {'max_concurrency':1.0},
    {'roles':('inference','other')}, {'qualification_ref':'other'}])
def test_all_other_specification_fields_remain_canonically_identical(tmp_path, change):
    old_spec, current_spec, old, observation = inputs()
    calls = []
    with pytest.raises(QualificationError):
        measure_changed_route_profile(old_spec, replace(current_spec, **change), old, observation,
            lambda:observation, tmp_path, transport=lambda *args:calls.append(args), now=NOW)
    assert calls == []


@pytest.mark.parametrize('change', ['runtime', 'model', 'gpu', 'options', 'extra', 'unbound',
                                  'foreign', 'stale', 'future', 'wrong_url', 'bad_old_spec'])
def test_new_route_never_carries_unobserved_or_changed_material(tmp_path, change):
    old_spec, current_spec, old, observation = inputs()
    material = observation['current_material_identity']
    if change == 'runtime': material['runtime']['version'] = 'other'
    if change == 'model': material['model']['digest'] = 'f'*64
    if change == 'gpu': material['host']['gpu'] = 'other'
    if change == 'options': material['context']['options']['temperature'] = False
    if change == 'extra': material['unexpected'] = True
    if change == 'unbound': observation['binding']['endpoint_bound'] = False
    if change == 'foreign': observation['profile_id'] = 'other'
    if change == 'stale': observation['checked_at'] = (NOW-timedelta(seconds=301)).isoformat()
    if change == 'future': observation['checked_at'] = (NOW+timedelta(seconds=1)).isoformat()
    if change == 'wrong_url': material['runtime']['endpoint_url'] = old_spec.endpoint
    if change == 'bad_old_spec': old_spec = replace(old_spec, qualification_ref='other')
    calls = []
    with pytest.raises(QualificationError):
        measure_changed_route_profile(old_spec, current_spec, old, observation, lambda:observation,
            tmp_path, transport=lambda *args:calls.append(args), now=NOW)
    assert calls == []


@pytest.mark.parametrize('measure', [qualify_existing_profile, measure_changed_host_profile])
def test_old_modes_still_refuse_observed_endpoint_drift(tmp_path, measure):
    old_spec, _, old, observation = inputs()
    calls = []
    with pytest.raises(QualificationError):
        measure(old_spec, old, observation, lambda:observation, tmp_path,
                transport=lambda *args:calls.append(args), now=NOW)
    assert calls == []


@pytest.mark.parametrize('change', ['url', 'container', 'options', 'older', 'unavailable'])
def test_post_identity_uncertainty_never_issues_a_new_route_candidate(tmp_path, change):
    old_spec, current_spec, old, observation = inputs()
    post = copy.deepcopy(observation)
    if change == 'url': post['current_material_identity']['runtime']['endpoint_url'] = 'http://other.invalid'
    if change == 'container': post['binding']['container_id'] = 'other'
    if change == 'options': post['current_material_identity']['context']['options']['temperature'] = False
    if change == 'older': post['checked_at'] = (NOW-timedelta(seconds=1)).isoformat()
    def capture():
        if change == 'unavailable': raise OSError('source unavailable')
        return post
    with pytest.raises(QualificationError):
        measure_changed_route_profile(old_spec, current_spec, old, observation, capture, tmp_path,
            transport=lambda url,payload:good_response('ollama',payload), now=NOW)
    report = json.loads((tmp_path/'qualification.json').read_text())
    assert report['status'] == 'FAILED' and len(report['trials']) == 2
    assert not (tmp_path/'receipt.json').exists()


def test_new_route_requires_an_explicit_owned_transport_before_native_calls(tmp_path, monkeypatch):
    old_spec, current_spec, old, observation = inputs()
    calls = []
    monkeypatch.setattr('urllib.request.build_opener', lambda *args:calls.append('unsafe default'))
    with pytest.raises(QualificationError, match='owned transport'):
        measure_changed_route_profile(old_spec, current_spec, old, observation, lambda:observation,
                                      tmp_path, transport=None, now=NOW)
    assert calls == []


@pytest.mark.parametrize('failure', ['incomplete', 'model', 'tools', 'tokens'])
def test_failed_first_route_trial_stops_before_second_trial_and_post_capture(tmp_path, failure):
    old_spec, current_spec, old, observation = inputs()
    calls = []
    def send(url, payload):
        calls.append('request')
        result = good_response('ollama', payload)
        if failure == 'incomplete': result['done'] = False
        if failure == 'model': result['model'] = 'other'
        if failure == 'tools': result['message']['tool_calls'] = []
        if failure == 'tokens': result['prompt_eval_count'] = 4095
        return result
    with pytest.raises(QualificationError):
        measure_changed_route_profile(old_spec, current_spec, old, observation,
            lambda:calls.append('post'), tmp_path, transport=send, now=NOW)
    assert calls == ['request']
    assert json.loads((tmp_path/'qualification.json').read_text())['status'] == 'FAILED'


def test_existing_evidence_is_not_overwritten_and_candidate_does_not_move_active_authority(tmp_path):
    from src.target_capability_store import TargetCapabilityStore
    old_spec, current_spec, old, observation = inputs()
    folder = tmp_path/'measurement'
    candidate, _ = measure_changed_route_profile(old_spec, current_spec, old, observation,
        lambda:observation, folder, transport=lambda url,payload:good_response('ollama',payload), now=NOW)
    original = (folder/'qualification.json').read_bytes()
    calls = []
    with pytest.raises(QualificationError):
        measure_changed_route_profile(old_spec, current_spec, old, observation, lambda:observation,
            folder, transport=lambda *args:calls.append('request'), now=NOW)
    assert calls == [] and (folder/'qualification.json').read_bytes() == original
    store = TargetCapabilityStore(str(tmp_path/'synthetic-store'))
    store.append(old)
    store.append(candidate)
    assert store.current_for_host(old.host_id).receipt_hash == old.receipt_hash
    assert store.current(candidate.profile_id).receipt_hash == candidate.receipt_hash
    assert store.verify()['ok'] is True
