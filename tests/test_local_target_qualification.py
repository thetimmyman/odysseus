"""Synthetic provider controls; no live model requests."""
import copy
import dataclasses
from datetime import datetime, timedelta, timezone
import hashlib
import json
import io
from email.message import Message
import re
import pytest


@pytest.mark.parametrize('failure', ['short_body', 'long_body', 'duplicate_length', 'ambiguous_framing',
                                   'oversize', 'duplicate_json', 'nonfinite'])
def test_provider_transport_refuses_incomplete_or_ambiguous_response(tmp_path, monkeypatch, failure):
    spec, old = profile()
    calls = []

    class Reply(io.BytesIO):
        def __init__(self, body, headers):
            super().__init__(body)
            self.headers = headers

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            body = json.dumps(good_response('ollama', json.loads(request.data))).encode()
            headers = Message()
            declared = len(body)
            if failure == 'short_body': declared += 1
            if failure == 'long_body': declared -= 1
            if failure == 'oversize': declared = 1048577
            if failure == 'duplicate_json': body = b'{"done":false,"done":true}'
            if failure == 'nonfinite': body = b'{"value":NaN}'
            if failure in ('duplicate_json', 'nonfinite'): declared = len(body)
            headers['Content-Length'] = str(declared)
            if failure == 'duplicate_length': headers['Content-Length'] = str(declared)
            if failure == 'ambiguous_framing': headers['Transfer-Encoding'] = 'chunked'
            return Reply(body, headers)

    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    with pytest.raises(QualificationError):
        qualify_existing_profile(spec, old, identity(old), lambda:identity(old), tmp_path, now=NOW)
    assert len(calls) == 1
    assert json.loads((tmp_path/'qualification.json').read_text())['status'] == 'FAILED'

from src.local_target_qualification import QualificationError, qualify_existing_profile
from src.local_targets import (CapabilityEvidence, CapabilityLimits, ContextProfile,
    HostBaseline, LocalTargetSpec, ModelIdentity, RuntimeIdentity, make_target_capability_receipt)

NOW = datetime(2026, 10, 8, 22, tzinfo=timezone.utc)


def profile(provider='ollama', **changes):
    options = ({'num_ctx': 262144, 'num_predict': 512, 'temperature': 0, 'think': False,
        'template_sha256': 'd'*64, 'runtime_environment_sha256': 'e'*64}
        if provider == 'ollama' else {'temperature': 0, 'max_tokens': 512,
        'enable_thinking': False, 'reasoning_effort': 'none', 'template_sha256': 'd'*64})
    fields = dict(host_id='synthetic-worker', profile_id='derived',
        observed_at=(NOW-timedelta(days=2)).isoformat(), schema_version=2,
        qualification_disposition='ADOPT_ROLE_SPECIFIC', qualification_ref='existing-anchor',
        roles=('inference',), health='healthy', health_checked_at=(NOW-timedelta(days=2)).isoformat(),
        ttl_s=86400, health_ttl_s=300,
        runtime=RuntimeIdentity(runtime_kind=provider, provider=provider, version='synthetic',
            image_digest='a'*64, endpoint_type='http', endpoint_url='http://synthetic.invalid'+(
                '/v1' if provider=='halogen-flash' else '')),
        model=ModelIdentity(model_id='synthetic-model', alias='synthetic-model',
            digest='b'*64, quantization='synthetic'),
        context=ContextProfile(configured_context=262144, safe_working_context=4096,
            semantic_verified_context=4096, safe_context_source='old-measurement', options=options),
        host=HostBaseline(host_id='synthetic-worker', kernel='synthetic-kernel'),
        capabilities=CapabilityEvidence(measured=('native_tools', 'readonly_analysis')),
        limits=CapabilityLimits(max_concurrency=1))
    fields.update(changes)
    receipt=make_target_capability_receipt(**fields)
    return LocalTargetSpec(target_id=receipt.host_id,label='synthetic',ssh_host='',
        endpoint=receipt.runtime.endpoint_url,model=receipt.model.model_id,runtime_kind=provider,
        roles=('inference',),qualification_ref=receipt.qualification_ref), receipt


def identity(receipt, **changes):
    value={'profile_id':receipt.profile_id,'checked_at':NOW.isoformat(),
           'current_material_identity':receipt.material_identity(), 'binding': {'container_id':'synthetic'}}
    value.update(changes)
    return value


def good_response(provider, request):
    prompt=request['messages'][0]['content']
    values=[re.search(rf'^{name}=(.+)$',prompt,re.MULTILINE).group(1) for name in ('FIRST','MIDDLE','LAST')]
    function={'name':'write_file','arguments':{'path':'qualification.txt','search':'baseline-placeholder',
                                             'replacement':':'.join(values)}}
    message={'content':None,'tool_calls':[{'function':function}]}
    if provider=='ollama':
        return {'model':request['model'],'done':True,'done_reason':'stop','message':message,
                'prompt_eval_count':5000,'eval_count':60}
    function['arguments']=json.dumps(function['arguments'])
    return {'model':request['model'],'choices':[{'message':message,'finish_reason':'tool_calls'}],
            'usage':{'prompt_tokens':5000,'completion_tokens':60}}


def new_profile_inputs():
    spec, baseline = profile(host=HostBaseline(host_id='synthetic-worker',
        kernel='current-kernel', boot_cmdline_digest='b'*64))
    descriptor = make_target_capability_receipt(**{**baseline.to_dict(),
        'qualification_disposition':'UNQUALIFIED', 'qualification_ref':'',
        'health':'unknown', 'health_checked_at':'', 'supersedes':'',
        'context':ContextProfile(configured_context=262144, options={
            **baseline.context.options, 'temperature':0.0, 'seed':7, 'top_p':0.9}),
        'capabilities':CapabilityEvidence(declared=('tools',))})
    before = identity(descriptor)
    before['binding']['endpoint_bound'] = True
    return dataclasses.replace(spec, qualification_ref=''), descriptor, before


def test_new_unqualified_profile_gets_only_its_own_measured_4k_anchor(tmp_path):
    from src.local_target_qualification import measure_new_profile
    spec, descriptor, before = new_profile_inputs()
    original = copy.deepcopy(descriptor.to_dict())
    calls = []

    def transport(url, request):
        calls.append(copy.deepcopy(request))
        assert url == 'http://synthetic.invalid/api/chat'
        assert request['think'] is False
        assert request['options'] == {'num_ctx':262144, 'num_predict':512,
                                     'temperature':0.0, 'seed':7, 'top_p':0.9}
        assert type(request['options']['temperature']) is float
        return good_response('ollama', request)

    candidate, report = measure_new_profile(spec, descriptor, before,
        lambda:copy.deepcopy(before), tmp_path, transport=transport, now=NOW)
    assert len(calls) == 2 and report['status'] == 'PASS'
    assert report['mode'] == 'NEW_PROFILE'
    assert candidate.material_identity() == descriptor.material_identity()
    assert candidate.profile_id == descriptor.profile_id
    assert candidate.supersedes == ''
    assert candidate.qualification_ref == candidate.context.safe_context_source
    assert candidate.qualification_ref.startswith('sha256:')
    assert candidate.context.safe_working_context == 4096
    assert candidate.context.semantic_verified_context == 4096
    assert candidate.qualification_state(now=NOW) == 'valid'
    assert candidate.capabilities.declared == ('tools',)
    assert descriptor.to_dict() == original
    assert descriptor.qualification_state(now=NOW) == 'disposition_unqualified'


@pytest.mark.parametrize('claim', ['safe_floor', 'semantic_floor', 'engine_floor', 'source',
    'measured', 'detected', 'tool_calls', 'streaming', 'health', 'health_at', 'load'])
def test_new_descriptor_cannot_carry_inherited_measurement_claims(tmp_path, claim):
    from src.local_target_qualification import measure_new_profile
    spec, descriptor, _ = new_profile_inputs()
    changes = {}
    if claim in ('safe_floor', 'semantic_floor', 'engine_floor', 'source'):
        field = {'safe_floor':'safe_working_context', 'semantic_floor':'semantic_verified_context',
                 'engine_floor':'engine_demonstrated_context', 'source':'safe_context_source'}[claim]
        changes['context'] = dataclasses.replace(descriptor.context,
            **{field:'historical-source' if claim == 'source' else 32768})
    elif claim in ('measured', 'detected', 'tool_calls', 'streaming'):
        field, value = {'measured':('measured', ('native_tools',)),
            'detected':('detected', ('native_tools',)), 'tool_calls':('tool_calls_observed', 2),
            'streaming':('streaming_observed', True)}[claim]
        changes['capabilities'] = dataclasses.replace(descriptor.capabilities, **{field:value})
    elif claim == 'load': changes['limits'] = dataclasses.replace(descriptor.limits, ttft_s=1.0)
    elif claim == 'health': changes['health'] = 'healthy'
    else: changes['health_checked_at'] = NOW.isoformat()
    descriptor = make_target_capability_receipt(**{**descriptor.to_dict(), **changes})
    before = identity(descriptor)
    before['binding']['endpoint_bound'] = True
    with pytest.raises(QualificationError):
        measure_new_profile(spec, descriptor, before, lambda:pytest.fail('postcheck'),
            tmp_path, transport=lambda *a:pytest.fail('transport'), now=NOW)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('field,value', [
    ('temperature', False), ('temperature', 0.1), ('temperature', '0'),
    ('temperature', float('nan')), ('temperature', float('inf')),
    ('num_ctx', 262144.0), ('num_predict', 512.0), ('num_predict', True),
    ('think', 'off'), ('think', 0), ('think', True), ('seed', True),
    ('top_p', True), ('top_p', -0.1), ('top_p', 1.1), ('top_p', float('inf')),
    ('template_sha256', 'not-a-digest'), ('unknown_sha256', 'f'*64),
    ('unknown_option', 1),
])
def test_new_request_contract_refuses_unsupported_options_before_transport(tmp_path, field, value):
    from src.local_target_qualification import measure_new_profile
    spec, descriptor, _ = new_profile_inputs()
    # Deliberately unsealed nonfinite input must also refuse through the public
    # interface rather than leaking a serialization error or reaching transport.
    context = dataclasses.replace(descriptor.context, options={**descriptor.context.options, field:value})
    descriptor = dataclasses.replace(descriptor, context=context)
    if not isinstance(value, float) or value == value and abs(value) != float('inf'):
        descriptor = make_target_capability_receipt(**descriptor.to_dict())
    before = identity(descriptor)
    before['binding']['endpoint_bound'] = True
    with pytest.raises(QualificationError):
        measure_new_profile(spec, descriptor, before, lambda:pytest.fail('postcheck'),
            tmp_path, transport=lambda *a:pytest.fail('transport'), now=NOW)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('missing', ['runtime_build', 'model_digest', 'quantization',
    'kernel', 'boot', 'host_id', 'typed_spec_concurrency', 'typed_descriptor_concurrency'])
def test_new_profile_requires_identified_material_and_exact_resource_types(tmp_path, missing):
    from src.local_target_qualification import measure_new_profile
    spec, descriptor, _ = new_profile_inputs()
    changes = {}
    if missing == 'runtime_build':
        changes['runtime'] = dataclasses.replace(descriptor.runtime, image_digest='', commit='')
    elif missing == 'model_digest': changes['model'] = dataclasses.replace(descriptor.model, digest='')
    elif missing == 'quantization':
        changes['model'] = dataclasses.replace(descriptor.model, quantization='unknown')
    elif missing in ('kernel', 'boot', 'host_id'):
        field = {'kernel':'kernel', 'boot':'boot_cmdline_digest', 'host_id':'host_id'}[missing]
        changes['host'] = dataclasses.replace(descriptor.host, **{field:''})
    elif missing == 'typed_spec_concurrency': spec = dataclasses.replace(spec, max_concurrency=True)
    else: changes['limits'] = dataclasses.replace(descriptor.limits, max_concurrency=True)
    descriptor = make_target_capability_receipt(**{**descriptor.to_dict(), **changes})
    before = identity(descriptor)
    before['binding']['endpoint_bound'] = True
    with pytest.raises(QualificationError):
        measure_new_profile(spec, descriptor, before, lambda:pytest.fail('postcheck'),
            tmp_path, transport=lambda *a:pytest.fail('transport'), now=NOW)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('failure', ['qualified', 'anchor', 'supersedes', 'invalidated',
    'future_descriptor', 'spec_anchor', 'role', 'stale', 'future', 'unbound',
    'foreign_profile', 'material_drift', 'boolean_option_drift'])
def test_new_profile_refuses_authority_or_identity_mismatch_before_transport(tmp_path, failure):
    from src.local_target_qualification import measure_new_profile
    spec, descriptor, before = new_profile_inputs()
    changes = {}
    if failure == 'qualified': changes['qualification_disposition'] = 'ADOPT_ROLE_SPECIFIC'
    elif failure == 'anchor': changes['qualification_ref'] = 'historical-anchor'
    elif failure == 'supersedes': changes['supersedes'] = 'a'*64
    elif failure == 'invalidated': changes['invalidation_reason'] = 'material_identity_drift'
    elif failure == 'future_descriptor': changes['observed_at'] = (NOW+timedelta(seconds=1)).isoformat()
    elif failure == 'spec_anchor': spec = dataclasses.replace(spec, qualification_ref='historical-anchor')
    elif failure == 'role': spec = dataclasses.replace(spec, roles=('inference', 'other'))
    if changes: descriptor = make_target_capability_receipt(**{**descriptor.to_dict(), **changes})
    if failure == 'stale': before['checked_at'] = (NOW-timedelta(seconds=301)).isoformat()
    elif failure == 'future': before['checked_at'] = (NOW+timedelta(seconds=1)).isoformat()
    elif failure == 'unbound': before['binding']['endpoint_bound'] = False
    elif failure == 'foreign_profile': before['profile_id'] = 'other-profile'
    elif failure == 'material_drift': before['current_material_identity']['model']['digest'] = 'f'*64
    elif failure == 'boolean_option_drift':
        before['current_material_identity']['context']['options']['temperature'] = False
    with pytest.raises(QualificationError):
        measure_new_profile(spec, descriptor, before, lambda:pytest.fail('postcheck'),
            tmp_path, transport=lambda *a:pytest.fail('transport'), now=NOW)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('failure', ['request', 'trial', 'post_unavailable', 'drift',
    'stale', 'future', 'older', 'serving_process'])
def test_new_profile_failed_trial_or_postcheck_never_returns_a_candidate(tmp_path, failure):
    from src.local_target_qualification import measure_new_profile
    spec, descriptor, before = new_profile_inputs()
    post = copy.deepcopy(before)
    if failure == 'drift': post['current_material_identity']['runtime']['version'] = 'other'
    elif failure == 'stale': post['checked_at'] = (NOW-timedelta(seconds=301)).isoformat()
    elif failure == 'future': post['checked_at'] = (NOW+timedelta(seconds=1)).isoformat()
    elif failure == 'older': post['checked_at'] = (NOW-timedelta(seconds=1)).isoformat()
    elif failure == 'serving_process': post['binding']['container_id'] = 'other-process'
    calls = []

    def transport(url, request):
        calls.append(request)
        if failure == 'request': raise OSError('outcome unknown')
        response = good_response('ollama', request)
        if failure == 'trial': response['done'] = False
        return response

    def capture_post():
        if failure == 'post_unavailable': raise OSError('source unavailable')
        return post

    with pytest.raises(QualificationError):
        measure_new_profile(spec, descriptor, before, capture_post, tmp_path,
            transport=transport, now=NOW)
    assert len(calls) == (1 if failure in ('request', 'trial') else 2)
    assert json.loads((tmp_path/'qualification.json').read_text())['status'] == 'FAILED'
    assert not (tmp_path/'receipt.json').exists()
    assert descriptor.qualification_state(now=NOW) == 'disposition_unqualified'


@pytest.mark.parametrize('endpoint', ['http://user:synthetic@synthetic.invalid',
                                    'ftp://synthetic.invalid', ' http://synthetic.invalid'])
def test_new_profile_refuses_credential_or_unsupported_endpoint_shapes(tmp_path, endpoint):
    from src.local_target_qualification import measure_new_profile
    spec, descriptor, _ = new_profile_inputs()
    spec = dataclasses.replace(spec, endpoint=endpoint)
    descriptor = make_target_capability_receipt(**{**descriptor.to_dict(),
        'runtime':dataclasses.replace(descriptor.runtime, endpoint_url=endpoint)})
    before = identity(descriptor)
    before['binding']['endpoint_bound'] = True
    with pytest.raises(QualificationError):
        measure_new_profile(spec, descriptor, before, lambda:pytest.fail('postcheck'),
            tmp_path, transport=lambda *a:pytest.fail('transport'), now=NOW)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('provider',['ollama','halogen-flash'])
def test_measured_renewal_keeps_profile_anchor_and_bounded_scope(tmp_path,provider):
    spec,old=profile(provider)
    calls=[]
    def transport(url,request):
        calls.append((url,copy.deepcopy(request)))
        return good_response(provider,request)
    renewed,report=qualify_existing_profile(spec,old,identity(old),lambda:identity(old),tmp_path,
                                           transport=transport,now=NOW)
    assert len(calls)==2 and report['status']=='PASS'
    assert renewed.material_identity()==old.material_identity()
    assert renewed.profile_id==old.profile_id and renewed.qualification_ref==old.qualification_ref
    assert renewed.context.safe_working_context==renewed.context.semantic_verified_context==4096
    assert renewed.qualification_state(now=NOW)=='valid'
    assert renewed.observed_at==NOW.isoformat() and renewed.supersedes==old.receipt_hash
    assert renewed.ttl_s==86400 and renewed.health_ttl_s==300
    assert renewed.routing_exactness()=='approximate'
    assert renewed.capabilities.streaming_observed is None
    assert renewed.capabilities.cancellation_observed is None
    raw=(tmp_path/'qualification.json').read_bytes()
    assert renewed.context.safe_context_source=='sha256:'+hashlib.sha256(raw).hexdigest()
    assert json.loads(raw)==report and len(report['producer_sha256'])==64


@pytest.mark.parametrize('provider',['ollama','halogen-flash'])
def test_larger_approximate_profile_renews_only_the_measured_4k_floor(tmp_path,provider):
    spec,old=profile(context=ContextProfile(configured_context=262144,
        safe_working_context=32768, semantic_verified_context=32768,
        safe_context_source='previous-32k-measurement', options=profile(provider)[1].context.options),
        provider=provider)
    calls=[]
    def transport(url,request):
        calls.append(request)
        return good_response(provider,request)
    renewed,report=qualify_existing_profile(spec,old,identity(old),lambda:identity(old),tmp_path,
                                           transport=transport,now=NOW)
    assert len(calls)==2 and report['status']=='PASS' and report['safe_context']==4096
    assert renewed.context.safe_working_context==renewed.context.semantic_verified_context==4096
    assert renewed.material_identity()==old.material_identity() and renewed.profile_id==old.profile_id
    assert renewed.routing_exactness()=='approximate'
    assert renewed.supersedes==old.receipt_hash and renewed.qualification_ref==old.qualification_ref
    assert renewed.qualification_state(now=NOW)=='valid'
    assert old.context.safe_working_context==32768
    assert old.qualification_state(now=NOW)=='qualification_expired'


@pytest.mark.parametrize('failure',['wrong_arguments','multiple_calls','incomplete','short_context',
                                   'missing_tokens','wrong_model','prose','network'])
def test_failed_trial_never_issues_renewal(tmp_path,failure):
    spec,old=profile()
    calls=[]
    def transport(url,request):
        calls.append(request)
        if failure=='network': raise OSError('synthetic failure')
        result=good_response('ollama',request)
        if failure=='wrong_arguments': result['message']['tool_calls'][0]['function']['arguments']['replacement']='wrong'
        elif failure=='multiple_calls': result['message']['tool_calls']*=2
        elif failure=='incomplete': result['done_reason']='length'
        elif failure=='short_context': result['prompt_eval_count']=4095
        elif failure=='missing_tokens': result.pop('eval_count')
        elif failure=='wrong_model': result['model']='other'
        elif failure=='prose': result['message']['content']='unexpected prose'
        return result
    with pytest.raises(QualificationError):
        qualify_existing_profile(spec,old,identity(old),lambda:pytest.fail('postcheck after failure'),
                                 tmp_path,transport=transport,now=NOW)
    assert len(calls)==1
    assert json.loads((tmp_path/'qualification.json').read_text())['status']=='FAILED'
    assert old.qualification_state(now=NOW)=='qualification_expired'


@pytest.mark.parametrize('failure',['stale','future','drift','wrong_profile'])
def test_invalid_precheck_refuses_before_inference(tmp_path,failure):
    spec,old=profile(); envelope=identity(old)
    if failure=='stale': envelope['checked_at']=(NOW-timedelta(seconds=301)).isoformat()
    elif failure=='future': envelope['checked_at']=(NOW+timedelta(seconds=1)).isoformat()
    elif failure=='wrong_profile': envelope['profile_id']='other'
    else: envelope['current_material_identity']['host']['kernel']='changed'
    with pytest.raises(QualificationError):
        qualify_existing_profile(spec,old,envelope,lambda:pytest.fail('postcheck'),tmp_path,
                                 transport=lambda *a:pytest.fail('inference'),now=NOW)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('failure',['drift','stale','older','container_changed'])
def test_invalid_postcheck_retains_failed_evidence(tmp_path,failure):
    spec,old=profile(); post=identity(old)
    if failure=='drift': post['current_material_identity']['runtime']['image_digest']='c'*64
    elif failure=='stale': post['checked_at']=(NOW-timedelta(seconds=301)).isoformat()
    elif failure=='older': post['checked_at']=(NOW-timedelta(seconds=1)).isoformat()
    else: post['binding']['container_id']='replacement'
    with pytest.raises(QualificationError):
        qualify_existing_profile(spec,old,identity(old),lambda:post,tmp_path,
            transport=lambda url,request:good_response('ollama',request),now=NOW)
    report=json.loads((tmp_path/'qualification.json').read_text())
    assert report['status']=='FAILED' and len(report['trials'])==2


def test_stronger_historical_profile_is_not_renewed_by_short_probes(tmp_path):
    spec,old=profile(capabilities=CapabilityEvidence(measured=('exact_reference_semantics',)))
    with pytest.raises(QualificationError):
        qualify_existing_profile(spec,old,identity(old),lambda:identity(old),tmp_path,
            transport=lambda *a:pytest.fail('inference'),now=NOW)


def test_existing_evidence_never_overwritten(tmp_path):
    spec,old=profile(); path=tmp_path/'qualification.json'; path.write_text('retained evidence')
    with pytest.raises(QualificationError):
        qualify_existing_profile(spec,old,identity(old),lambda:identity(old),tmp_path,
            transport=lambda *a:pytest.fail('inference'),now=NOW)
    assert path.read_text()=='retained evidence'


@pytest.mark.parametrize('previous_floor',[4096,32768])
def test_expired_heartbeat_can_only_be_revived_by_new_measurement(tmp_path,previous_floor):
    from src.target_capability_store import TargetCapabilityStore
    spec,old=profile()
    old=make_target_capability_receipt(**{**old.to_dict(), 'context':dataclasses.replace(
        old.context, safe_working_context=previous_floor, semantic_verified_context=previous_floor)})
    store=TargetCapabilityStore(str(tmp_path/'store'))
    store.append(old)
    refreshed=store.refresh_observation(old.profile_id,current_identity_digest=old.identity_digest(),
        health='healthy',checked_at=datetime.now(timezone.utc).isoformat())
    assert refreshed.invalidation_reason=='qualification_expired'
    fresh=lambda:identity(refreshed,checked_at=datetime.now(timezone.utc).isoformat())
    renewed,_=qualify_existing_profile(spec,refreshed,fresh(),fresh,
        tmp_path/'evidence',transport=lambda url,request:good_response('ollama',request))
    assert renewed.invalidation_reason=='' and renewed.qualification_state()=='valid'
    # Store application has its own wall-clock observation check; exercise using
    # a current independent observation rather than the fixed measurement clock.
    real_now=datetime.now(timezone.utc)
    applied=store.renew_profile(renewed,expected_profile_id=old.profile_id,
        expected_receipt_hash=refreshed.receipt_hash,current_identity_digest=renewed.identity_digest(),
        identity_checked_at=real_now.isoformat())
    assert applied.receipt_hash==renewed.receipt_hash
    assert store.current_for_host(old.host_id).profile_id==old.profile_id
    assert applied.context.safe_working_context==4096
    assert next(r for r in store.entries() if r.receipt_hash==old.receipt_hash).to_dict()==old.to_dict()


@pytest.mark.parametrize('safe,semantic',[(2048,32768),(32768,2048)])
def test_unmeasured_4k_floor_cannot_be_created_by_renewal(tmp_path,safe,semantic):
    _,previous=profile()
    spec,old=profile(context=dataclasses.replace(previous.context,
        safe_working_context=safe, semantic_verified_context=semantic))
    with pytest.raises(QualificationError):
        qualify_existing_profile(spec,old,identity(old),lambda:pytest.fail('postcheck'),tmp_path,
            transport=lambda *a:pytest.fail('inference'),now=NOW)


@pytest.mark.parametrize('floor',[float('nan'),float('inf'),32768.5])
def test_noninteger_historical_floor_refuses_before_measurement(tmp_path,floor):
    _,previous=profile()
    spec,old=profile(context=dataclasses.replace(previous.context,
        safe_working_context=floor, semantic_verified_context=floor))
    with pytest.raises(QualificationError):
        qualify_existing_profile(spec,old,identity(old),lambda:pytest.fail('postcheck'),tmp_path,
            transport=lambda *a:pytest.fail('inference'),now=NOW)


def changed_host_observation(receipt, **changes):
    value=identity(receipt)
    value['current_material_identity']['host'].update(kernel='new-kernel',
        boot_cmdline_digest='c'*64)
    value['binding']['endpoint_bound']=True
    value.update(changes)
    return value


@pytest.mark.parametrize('provider',['ollama','halogen-flash'])
def test_changed_host_is_measured_as_a_new_4k_profile_with_its_own_anchor(tmp_path,provider):
    from src.local_target_qualification import measure_changed_host_profile
    spec,previous=profile(provider,host=HostBaseline(host_id='synthetic-worker',
        kernel='old-kernel',boot_cmdline_digest='b'*64))
    original=previous.to_dict()
    observation=changed_host_observation(previous)
    candidate,report=measure_changed_host_profile(spec,previous,observation,
        lambda:copy.deepcopy(observation),tmp_path,
        transport=lambda url,request:good_response(provider,request),now=NOW)
    assert candidate.profile_id!=previous.profile_id
    assert candidate.host.kernel=='new-kernel' and candidate.host.boot_cmdline_digest=='c'*64
    assert candidate.context.safe_working_context==candidate.context.semantic_verified_context==4096
    assert candidate.routing_exactness()=='approximate' and candidate.qualification_state(now=NOW)=='valid'
    assert candidate.supersedes==''
    assert candidate.qualification_ref=='sha256:'+hashlib.sha256((tmp_path/'qualification.json').read_bytes()).hexdigest()
    assert candidate.context.safe_context_source==candidate.qualification_ref
    assert report['mode']=='CHANGED_HOST_NEW_PROFILE' and len(report['trials'])==2
    assert report['previous_receipt_hash']==previous.receipt_hash
    assert report['profile_id']==candidate.profile_id
    assert previous.to_dict()==original and previous.qualification_state(now=NOW)=='qualification_expired'


def test_changed_host_cannot_hide_boolean_request_options_as_equal_numbers(tmp_path):
    from src.local_target_qualification import measure_changed_host_profile
    _,base=profile()
    spec,previous=profile(host=HostBaseline(host_id='synthetic-worker',
        kernel='old-kernel',boot_cmdline_digest='b'*64),context=dataclasses.replace(
            base.context,options={**base.context.options,'seed':1}))
    observation=changed_host_observation(previous)
    observation['current_material_identity']['context']['options']['seed']=True
    with pytest.raises(QualificationError):
        measure_changed_host_profile(spec,previous,observation,lambda:pytest.fail('postcheck'),
            tmp_path,transport=lambda *a:pytest.fail('inference'),now=NOW)


@pytest.mark.parametrize('failure',['unchanged','model','runtime','endpoint','gpu','request_options',
    'foreign_profile','future','stale','unbound','invalid_boot','extra_material','unqualified_prior'])
def test_changed_host_refuses_other_drift_before_measurement(tmp_path,failure):
    from src.local_target_qualification import measure_changed_host_profile
    spec,previous=profile(host=HostBaseline(host_id='synthetic-worker',
        kernel='old-kernel',boot_cmdline_digest='b'*64))
    if failure=='unqualified_prior':
        previous=make_target_capability_receipt(**{**previous.to_dict(),
            'qualification_disposition':'UNQUALIFIED'})
    observation=changed_host_observation(previous)
    material=observation['current_material_identity']
    if failure=='unchanged': observation['current_material_identity']=previous.material_identity()
    elif failure=='model': material['model']['digest']='f'*64
    elif failure=='runtime': material['runtime']['version']='new-version'
    elif failure=='endpoint': material['runtime']['endpoint_url']='http://other.invalid'
    elif failure=='gpu': material['host']['gpu']='new-gpu'
    elif failure=='request_options': material['context']['options']['temperature']=1
    elif failure=='foreign_profile': observation['profile_id']='foreign'
    elif failure=='future': observation['checked_at']=(NOW+timedelta(seconds=1)).isoformat()
    elif failure=='stale': observation['checked_at']=(NOW-timedelta(seconds=301)).isoformat()
    elif failure=='unbound': observation['binding']['endpoint_bound']=False
    elif failure=='invalid_boot': material['host']['boot_cmdline_digest']='unknown'
    elif failure=='extra_material': material['unobserved']='not-a-host-change'
    with pytest.raises(QualificationError):
        measure_changed_host_profile(spec,previous,observation,lambda:pytest.fail('postcheck'),
            tmp_path,transport=lambda *a:pytest.fail('inference'),now=NOW)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('failure',['trial','changed_kernel','changed_container','stale','older','model'])
def test_failed_changed_host_measurement_never_issues_qualification(tmp_path,failure):
    from src.local_target_qualification import measure_changed_host_profile
    spec,previous=profile(host=HostBaseline(host_id='synthetic-worker',
        kernel='old-kernel',boot_cmdline_digest='b'*64))
    pre=changed_host_observation(previous)
    post=copy.deepcopy(pre)
    if failure=='changed_kernel': post['current_material_identity']['host']['kernel']='third-kernel'
    elif failure=='changed_container': post['binding']['container_id']='other-process'
    elif failure=='stale': post['checked_at']=(NOW-timedelta(seconds=301)).isoformat()
    elif failure=='older': post['checked_at']=(NOW-timedelta(seconds=1)).isoformat()
    elif failure=='model': post['current_material_identity']['model']['digest']='f'*64
    calls=[]
    def transport(url,request):
        calls.append(request)
        result=good_response('ollama',request)
        if failure=='trial': result['done']=False
        return result
    with pytest.raises(QualificationError):
        measure_changed_host_profile(spec,previous,pre,lambda:post,tmp_path,
            transport=transport,now=NOW)
    report=json.loads((tmp_path/'qualification.json').read_text())
    assert report['status']=='FAILED' and len(calls)==(1 if failure=='trial' else 2)
    assert previous.qualification_state(now=NOW)=='qualification_expired'


def test_material_drift_invalidation_cannot_be_cleared_by_short_probe(tmp_path):
    spec,old=profile(invalidation_reason='material_identity_drift')
    with pytest.raises(QualificationError):
        qualify_existing_profile(spec,old,identity(old),lambda:identity(old),tmp_path,
            transport=lambda *a:pytest.fail('inference after invalidation'),now=NOW)
