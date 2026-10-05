"""Measured profile activation and refusal controls, with synthetic identities."""
import dataclasses
import datetime
import json

import pytest

from src import dispatch_boundary as db, dispatch_routing as dr, local_target_routing as ltr
from src.local_targets import (CapabilityEvidence, ContextProfile, HostBaseline,
                              ModelIdentity, RuntimeIdentity, LocalTargetSpec,
                              make_target_capability_receipt, registered_targets,
                              INVALIDATED_SAFE_CONTEXT_UNMEASURED)
from src.target_capability_store import TargetCapabilityStore, CapabilityStoreError


def measured(**changes):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    fields = dict(host_id='local-framework', profile_id='derived', observed_at=now,
                  schema_version=2, qualification_disposition='ADOPT_ROLE_SPECIFIC',
                  qualification_ref='synthetic-measurement', roles=('inference',),
                  health='healthy', health_checked_at=now,
                  runtime=RuntimeIdentity(runtime_kind='synthetic', provider='synthetic',
                                          version='1.0', image_digest='a'*64,
                                          endpoint_type='http', endpoint_url='http://127.0.0.1:8731/v1',
                                          backend='hip'),
                  model=ModelIdentity(model_id='synthetic-model', digest='b'*64,
                                      quantization='synthetic', auxiliary_artifacts=('mtp:'+'c'*64,)),
                  context=ContextProfile(configured_context=262144, configured_served_context=262144,
                                         safe_working_context=32768, semantic_verified_context=32768, safe_context_source='synthetic-needle',
                                         options={'parser':'synthetic-parser'}),
                  host=HostBaseline(host_id='local-framework', kernel='synthetic-kernel'),
                  capabilities=CapabilityEvidence(measured=('text_generation','single_tool_call',
                                                            'native_tools','context_integrity')))
    fields.update(changes)
    return make_target_capability_receipt(**fields)


def spec(r):
    return LocalTargetSpec(target_id=r.host_id, label='synthetic', ssh_host='',
                           endpoint=r.runtime.endpoint_url, model=r.model.model_id,
                           roles=('inference',), qualification_ref=r.qualification_ref)


def inputs(tmp_path, r, **kwargs):
    store=TargetCapabilityStore(str(tmp_path));store.append(r)
    return ltr.persisted_routing_inputs(store, specs=[spec(r)], **kwargs)


def dispatch(view, *, exactness=dr.EXACTNESS_APPROXIMATE, context=32768):
    req=dr.RoutingRequest(domain='general_swe',role=dr.ROLE_APPROXIMATE_IMPLEMENTER,
                          exactness=exactness,minimum_context_tokens=context,local_only=True)
    return db.resolve_from_estate(db.TargetEstate(profiles=view.profiles),req,
                                 capability_store=view.capability_store)


def test_role_specific_positive_and_stronger_reference_context_controls(tmp_path):
    r=measured();view=inputs(tmp_path,r)
    bound=dispatch(view)
    assert bound.decision.selected_profile.profile_id==r.profile_id
    assert r.receipt_hash in bound.decision.capability_receipt_refs
    for kwargs in [dict(exactness=dr.EXACTNESS_EXACT),dict(context=32769)]:
        with pytest.raises(dr.RoutingRefused):dispatch(view,**kwargs)
    with pytest.raises(dr.RoutingRefused):
        req=dr.RoutingRequest(domain='general_swe',role=dr.ROLE_APPROXIMATE_IMPLEMENTER,
                              exactness=dr.EXACTNESS_APPROXIMATE,
                              capabilities=(dr.CAP_PARALLEL_TOOL_CALLS,))
        db.resolve_from_estate(db.TargetEstate(profiles=view.profiles),req,
                              capability_store=view.capability_store)


@pytest.mark.parametrize('disposition',['REJECTED','UNQUALIFIED','QUALIFIED_EXPERIMENTAL'])
def test_non_adopted_dispositions_are_stored_but_never_routable(tmp_path,disposition):
    r=measured(qualification_disposition=disposition)
    view=inputs(tmp_path,r)
    assert not view.profiles
    assert disposition.lower() in view.skipped[0]['reason']
    assert view.capability_store.entries()[0].receipt_hash==r.receipt_hash


def test_qualified_reference_requires_separate_measured_semantic_evidence(tmp_path):
    base=measured();r=measured(qualification_disposition='QUALIFIED',
                             capabilities=dataclasses.replace(base.capabilities,
                             measured=base.capabilities.measured+('exact_reference_semantics',)))
    assert dispatch(inputs(tmp_path,r),exactness=dr.EXACTNESS_EXACT).decision.selected_profile
    assert measured(qualification_disposition='QUALIFIED').routing_exactness()==dr.EXACTNESS_APPROXIMATE


@pytest.mark.parametrize('field',['kernel','firmware','rocm','libhsakmt','boot_cmdline_digest'])
def test_material_host_drift_creates_a_new_profile_and_cannot_reuse_qualification(field):
    one=measured();two=measured(host=dataclasses.replace(one.host,**{field:'changed'}))
    assert one.profile_id!=two.profile_id
    assert one.qualification_state(current_identity_digest=two.identity_digest())=='material_identity_changed'


@pytest.mark.parametrize('field',['auxiliary_artifacts','quantization','digest'])
def test_model_and_auxiliary_drift_are_new_profile_identities(field):
    one=measured();value=('mtp:'+'d'*64,) if field=='auxiliary_artifacts' else 'changed'
    two=measured(model=dataclasses.replace(one.model,**{field:value}))
    assert one.profile_id!=two.profile_id
    assert one.receipt_hash!=two.receipt_hash


def test_host_activation_is_explicit_and_cannot_choose_newest_ledger_observation(tmp_path):
    s=TargetCapabilityStore(str(tmp_path));a=measured();s.append(a)
    b=measured(runtime=dataclasses.replace(a.runtime,version='2.0'));s.append(b)
    assert s.current_for_host(a.host_id).profile_id==a.profile_id
    with pytest.raises(CapabilityStoreError,match='changed before'):
        s.activate_profile(a.host_id,b.profile_id,expected_profile_id='',current_identity_digest=b.identity_digest())
    s.activate_profile(a.host_id,b.profile_id,expected_profile_id=a.profile_id,current_identity_digest=b.identity_digest())
    assert s.current_for_host(a.host_id).profile_id==b.profile_id
    assert len(s.entries())==2


@pytest.mark.parametrize('filename',['current.json','active.json'])
def test_missing_authority_fails_closed_instead_of_rebuilding_from_history(tmp_path,filename):
    s=TargetCapabilityStore(str(tmp_path));a=measured();s.append(a);(tmp_path/filename).unlink()
    with pytest.raises(CapabilityStoreError):s.current_for_host(a.host_id)


def test_corrupt_authority_and_cross_profile_redirect_are_detected(tmp_path):
    s=TargetCapabilityStore(str(tmp_path));a=measured();s.append(a)
    b=measured(runtime=dataclasses.replace(a.runtime,version='2.0'));s.append(b)
    p=tmp_path/'current.json';index=json.loads(p.read_text());index[a.profile_id]['receipt_hash']=b.receipt_hash
    p.write_text(json.dumps(index));assert not s.verify()['ok']
    with pytest.raises(CapabilityStoreError,match='another profile'):s.current(a.profile_id)


def test_heartbeat_preserves_qualification_clock_and_invalidates_drift(tmp_path):
    s=TargetCapabilityStore(str(tmp_path));a=measured();s.append(a)
    now=datetime.datetime.now(datetime.timezone.utc).isoformat()
    fresh=s.refresh_observation(a.profile_id,current_identity_digest=a.identity_digest(),health='healthy',checked_at=now)
    assert (fresh.observed_at,fresh.ttl_s,fresh.profile_id)==(a.observed_at,a.ttl_s,a.profile_id)
    stale=s.refresh_observation(a.profile_id,current_identity_digest='different',health='healthy',checked_at=now)
    assert stale.qualification_state()=='material_identity_changed'
    assert len(s.entries())==3


def test_heartbeat_cannot_revive_expired_semantic_qualification(tmp_path):
    s=TargetCapabilityStore(str(tmp_path));a=measured(observed_at='2020-01-01T00:00:00+00:00');s.append(a)
    fresh=s.refresh_observation(a.profile_id,current_identity_digest=a.identity_digest(),health='healthy',
                                checked_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    assert fresh.observed_at==a.observed_at
    assert fresh.qualification_state()=='qualification_expired'


def test_current_identity_is_required_if_a_live_inventory_is_supplied(tmp_path):
    r=measured();view=inputs(tmp_path,r,current_identity_digests={})
    assert not view.profiles and view.skipped[0]['reason']=='live_identity_missing'
    view=ltr.persisted_routing_inputs(view.capability_store,specs=[spec(r)],current_identity_digests={r.host_id:'different'})
    assert not view.profiles and 'material_identity_changed' in view.skipped[0]['reason']


def test_registry_override_replaces_retired_endpoint_and_never_adds_msr1_inference(tmp_path,monkeypatch):
    r=measured();p=tmp_path/'registry.json';p.write_text(json.dumps([dataclasses.asdict(spec(r))]))
    monkeypatch.setenv('PS632_PROFILE_REGISTRY',str(p))
    found=next(x for x in registered_targets() if x.target_id==r.host_id)
    assert found.endpoint==r.runtime.endpoint_url
    row=dataclasses.asdict(spec(r));row['target_id']='local-msr1';p.write_text(json.dumps([row]))
    with pytest.raises(ValueError,match='MS-R1'):registered_targets()


def test_schema2_round_trip_and_historical_schema1_hash_preservation():
    two=measured();assert make_target_capability_receipt(**two.to_dict()).receipt_hash==two.receipt_hash
    one=measured(schema_version=1,qualification_disposition='')
    assert 'qualification_disposition' not in one.to_dict()
    assert make_target_capability_receipt(**one.to_dict()).receipt_hash==one.receipt_hash


def test_declared_context_cannot_cover_a_stronger_unmeasured_safe_claim():
    a=measured()
    b=measured(context=dataclasses.replace(a.context,safe_working_context=131072))
    assert b.qualification_state()==INVALIDATED_SAFE_CONTEXT_UNMEASURED
    assert measured(model=dataclasses.replace(a.model,quantization="unknown")).qualification_state()=="model_quantization_unidentified"


def test_fallback_retains_originating_context_requirement(tmp_path):
    a=measured()
    insufficient=measured(context=dataclasses.replace(a.context,safe_working_context=16384,
                                                      semantic_verified_context=16384))
    fallback=measured(host_id='local-rtx4500',host=HostBaseline(host_id='local-rtx4500',kernel='synthetic'))
    store=TargetCapabilityStore(str(tmp_path));store.append(insufficient);store.append(fallback)
    view=ltr.persisted_routing_inputs(store,specs=[spec(insufficient),spec(fallback)])
    request=dr.RoutingRequest(domain='general_swe',role=dr.ROLE_APPROXIMATE_IMPLEMENTER,
                             exactness=dr.EXACTNESS_APPROXIMATE,minimum_context_tokens=32768,
                             preferred_profile_ids=(insufficient.profile_id,),local_only=True)
    bound=db.resolve_from_estate(db.TargetEstate(profiles=view.profiles),request,capability_store=store)
    assert bound.decision.selected_profile.profile_id==fallback.profile_id
    assert fallback.receipt_hash in bound.decision.capability_receipt_refs
    assert all(not c.eligible for c in bound.decision.candidates if c.profile_id==insufficient.profile_id)
    with pytest.raises(dr.RoutingRefused):
        db.resolve_from_estate(db.TargetEstate(profiles=view.profiles),
                              dataclasses.replace(request,minimum_context_tokens=32769),capability_store=store)


def test_projection_hash_covers_canonical_source_and_measured_context(tmp_path):
    a=measured();view=inputs(tmp_path,a);projected=view.receipts[0].to_dict()
    assert db._receipt_hash_of(projected)==projected['receipt_hash']
    for key,value in [('source_receipt_hash','different'),('safe_working_context',131072)]:
        changed={**projected,key:value}
        assert db._receipt_hash_of(changed)!=projected['receipt_hash']


def test_operator_cli_import_audit_refresh_and_tamper_refusal(tmp_path):
    import subprocess
    import sys
    from pathlib import Path
    cli=Path(__file__).resolve().parents[1]/'scripts/odysseus-capability'
    a=measured();p=tmp_path/'receipt.json';p.write_text(json.dumps(a.to_dict()))
    command=[sys.executable,str(cli),'--store',str(tmp_path/'store')]
    imported=subprocess.run(command+['import',str(p)],capture_output=True,text=True)
    assert imported.returncode==0,imported.stderr
    report=subprocess.run(command+['verify'],capture_output=True,text=True)
    assert report.returncode==0 and json.loads(report.stdout)['ok']
    observed=tmp_path/'observed.json';observed.write_text(json.dumps(dict(profile_id=a.profile_id,
                   current_material_identity=a.material_identity(),health='healthy',
                   checked_at=datetime.datetime.now(datetime.timezone.utc).isoformat())))
    refreshed=subprocess.run(command+['refresh',str(observed)],capture_output=True,text=True)
    assert refreshed.returncode==0,refreshed.stderr
    assert json.loads(refreshed.stdout)['observed_at']==a.observed_at
    raw=a.to_dict();raw['context']['safe_working_context']=131072;p.write_text(json.dumps(raw))
    bad=subprocess.run(command+['import',str(p)],capture_output=True,text=True)
    assert bad.returncode==2 and 'hash does not cover' in bad.stderr


def test_existing_stronger_role_cannot_be_weakened_by_approximate_intent(tmp_path):
    a=measured();view=inputs(tmp_path,a)
    request=dr.RoutingRequest(domain='general_swe',role=dr.ROLE_IMPLEMENTER,
                             exactness=dr.EXACTNESS_APPROXIMATE,local_only=True)
    assert dr.CAP_EXACT_REFERENCE_SEMANTICS in request.required_capabilities()
    with pytest.raises(dr.RoutingRefused):
        db.resolve_from_estate(db.TargetEstate(profiles=view.profiles),request,
                              capability_store=view.capability_store)
    # Even the new bounded role retains explicitly requested stronger proof.
    request=dataclasses.replace(request,role=dr.ROLE_APPROXIMATE_IMPLEMENTER,
                               capabilities=(dr.CAP_EXACT_REFERENCE_SEMANTICS,))
    with pytest.raises(dr.RoutingRefused):
        db.resolve_from_estate(db.TargetEstate(profiles=view.profiles),request,
                              capability_store=view.capability_store)
