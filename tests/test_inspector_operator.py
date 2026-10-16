"""Operator outcomes through the owned retained-attempt interface."""
import hashlib
import json
import pytest
from tests.test_canonical_inspector_admission import native_store as native_store, principal, prepared, publication
from tests.test_canonical_inspector_routes import retained_payload, retained_projection
from src.canonical_inspector_service import InspectorService
from src.canonical_inspector_admission import UnknownInspection


def owned_service(native_store, tmp_path):
    store, _ = native_store
    owner = principal()
    claim = store.admit(owner, 'one', prepared())
    store.consume(claim, owner)
    handle = store.publish(claim, owner, publication())
    projection = retained_projection()
    projection['dispatch'].update(reason='preferred eligible profile', policy_ref='policy:one',
        decided_at='2026-10-16T12:00:00+00:00', selected={'target_id': 'local', 'model': 'qwen', 'runtime_kind': 'ollama'},
        envelope={'tools': ['write_file'], 'network_policy': 'offline', 'write_scope': ['answer.py']})
    projection['capacity'] = {'status': 'UNKNOWN', 'supplements': [{'provider': 'local',
        'pool_id': 'local-pool', 'account_identity': 'PRIVATE', 'facts_current': False,
        'observed_at': '2026-10-15T12:00:00+00:00', 'quotas': [], 'price': None}]}
    service = InspectorService(store, retained_root=tmp_path, projector=lambda *a, **k: projection)
    service._read = lambda *a, **k: (retained_payload(), {})
    return owner, handle, service, store


def test_owned_inspection_explains_receipt_without_exposing_account(native_store, tmp_path):
    owner, handle, service, _ = owned_service(native_store, tmp_path)
    result = service.inspect(owner, handle, lambda: owner)
    assert result['routing']['decision']['reason'] == 'preferred eligible profile'
    assert result['routing']['decision']['selected']['model'] == 'qwen'
    assert result['routing']['capacity']['supplements'][0]['facts_current'] is False
    assert 'PRIVATE' not in json.dumps(result)


def test_escalation_is_immutable_source_bound_request_and_survives_reopen(native_store, tmp_path):
    owner, handle, service, store = owned_service(native_store, tmp_path)
    before = service.inspect(owner, handle, lambda: owner)
    result = service.request_escalation(owner, handle, before['dispatch_receipt_hash'], 'need independent review', 'request-1', lambda: owner)
    assert result['status'] == 'REQUESTED'
    receipt = result['receipt']
    assert receipt['attempt_receipt_hash'] == before['attempt_receipt_hash']
    assert receipt['original_dispatch_receipt_hash'] == before['dispatch_receipt_hash']
    assert receipt['resulting_dispatch_receipt_hash'] is None
    core = {k:v for k,v in receipt.items() if k != 'receipt_hash'}
    assert receipt['receipt_hash'] == hashlib.sha256(json.dumps(core,sort_keys=True,separators=(',', ':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
    assert service.request_escalation(owner, handle, before['dispatch_receipt_hash'], 'need independent review', 'request-1', lambda: owner) == result
    assert service.inspect(owner, handle, lambda: owner)['interventions'] == [receipt]
    from sqlalchemy.orm import sessionmaker
    from src.canonical_inspector_admission import AdmissionStore
    reopened = AdmissionStore(sessionmaker(bind=native_store[1], expire_on_commit=False))
    assert reopened.interventions(owner, handle, 'public-uppercase-v1') == [receipt]
    with pytest.raises(UnknownInspection):
        service.request_escalation(principal('other'), handle, before['dispatch_receipt_hash'], 'reason', 'request-2', lambda: principal('other'))
    with pytest.raises(UnknownInspection):
        service.request_escalation(owner, handle, 'f'*64, 'reason', 'request-2', lambda: owner)
    store.revoke(owner, handle)
    with pytest.raises(UnknownInspection):
        service.request_escalation(owner, handle, before['dispatch_receipt_hash'], 'reason', 'request-3', lambda: owner)


def test_pending_escalation_does_not_refresh_or_modify_decision(native_store, tmp_path):
    owner, handle, service, _ = owned_service(native_store, tmp_path)
    before = service.inspect(owner, handle, lambda: owner)
    service.request_escalation(owner, handle, before['dispatch_receipt_hash'], 'stale capacity needs owner review', 'request-1', lambda: owner)
    after = service.inspect(owner, handle, lambda: owner)
    assert after['routing'] == before['routing']
    assert after['dispatch_receipt_hash'] == before['dispatch_receipt_hash']
    assert after['routing']['capacity']['supplements'][0]['facts_current'] is False


def test_escalation_requires_real_cookie_and_refuses_extra_authority(tmp_path, monkeypatch, native_store):
    from tests.test_canonical_inspector_routes import client_for
    owner, handle, service, _ = owned_service(native_store, tmp_path)
    _, _, client = client_for(tmp_path, monkeypatch, service)
    path = '/api/harness/inspector/attempts/' + handle + '/escalation'
    body = {'dispatch_receipt_hash':'d'*64, 'reason':'need review', 'idempotency_key':'request-1'}
    assert client.post(path,json=body).status_code == 403
    assert client.post(path,json=body,headers={'x-test-user':'other'}).status_code == 404
    assert client.post(path,json={**body,'owner':'admin'},headers={'x-test-user':'admin'}).status_code == 404
    response = client.post(path,json=body,headers={'x-test-user':'admin'})
    assert response.status_code == 200
    assert response.json()['status'] == 'REQUESTED'
    assert response.json()['receipt']['actor'] == 'admin'


def test_canonical_request_rows_cannot_be_updated_deleted_or_replaced(native_store, tmp_path):
    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError
    owner, handle, service, _ = owned_service(native_store, tmp_path)
    service.request_escalation(owner, handle, 'd'*64, 'need review', 'request-1', lambda: owner)
    engine = native_store[1]
    for command in ["UPDATE canonical_inspector_intervention SET receipt='{}'", 'DELETE FROM canonical_inspector_intervention',
                    'INSERT OR REPLACE INTO canonical_inspector_intervention SELECT * FROM canonical_inspector_intervention']:
        with pytest.raises(SQLAlchemyError), engine.begin() as connection:
            connection.execute(text(command))
