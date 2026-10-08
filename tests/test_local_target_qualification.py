"""Synthetic provider controls; no live model requests."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import pytest

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


def test_expired_heartbeat_can_only_be_revived_by_new_measurement(tmp_path):
    from src.target_capability_store import TargetCapabilityStore
    spec,old=profile()
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


def test_material_drift_invalidation_cannot_be_cleared_by_short_probe(tmp_path):
    spec,old=profile(invalidation_reason='material_identity_drift')
    with pytest.raises(QualificationError):
        qualify_existing_profile(spec,old,identity(old),lambda:identity(old),tmp_path,
            transport=lambda *a:pytest.fail('inference after invalidation'),now=NOW)
