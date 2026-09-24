import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from src import free_scout as scout

HTML = b'<table><tr><td>Space Bunny Free</td><td>Free</td><td>Free</td><td>Free</td><td>-</td><td>Unlimited</td></tr><tr><td>Space Bunny Free</td><td>space-bunny-free</td><td>https://opencode.ai/zen/go/v1/chat/completions</td><td>sdk</td></tr></table>'


def test_exact_free_price_and_route_required():
    scout.verify_terms(HTML)
    for changed in [HTML.replace(b'<td>Free</td>', b'<td>$0.01</td>', 1), HTML.replace(b'space-bunny-free', b'paid-model'), HTML.replace(b'opencode.ai/zen/go', b'opencode.ai.evil/zen/go')]:
        with pytest.raises(ValueError): scout.verify_terms(changed)


def test_go_headers_are_opt_in_and_bound_to_exact_namespace(monkeypatch):
    from src.endpoint_resolver import build_headers
    monkeypatch.delenv('ODYSSEUS_OPENCODE_SESSION', raising=False)
    assert 'x-opencode-session' not in build_headers('fixture', scout.BASE_URL)
    monkeypatch.setenv('ODYSSEUS_OPENCODE_SESSION', 'task:fixture')
    headers = build_headers('fixture', scout.BASE_URL)
    assert headers['x-opencode-session'] == 'task:fixture'
    assert headers['User-Agent'] == 'odysseus-scout/0.1'
    assert 'x-opencode-session' not in build_headers('fixture', 'https://opencode.ai.evil/zen/go/v1')
    monkeypatch.setenv('ODYSSEUS_OPENCODE_SESSION', 'bad\nheader')
    with pytest.raises(ValueError): build_headers('fixture', scout.BASE_URL)


def test_prepare_overrides_paid_permissions_and_requires_public_task(monkeypatch, tmp_path):
    root = scout.private_dir(tmp_path/'runtime'); taskfile=tmp_path/'task.json'
    task = {'id':'t', 'dataSensitivity':'public','repoPath':str(tmp_path),'routing':{'allowPaidModels':True,'maxAttempts':9}}
    taskfile.write_text(json.dumps(task))
    calls=[]; monkeypatch.setattr(scout,'refresh',lambda root, manifest: calls.append(manifest))
    scout.prepare(root, taskfile, tmp_path/'auth.json',tmp_path/'inbox')
    stored=json.loads((root/'task.json').read_text())
    assert stored['routing'] == {'allowFreeModels': True, 'allowPaidModels': False, 'allowPremiumModels': False, 'maxAttempts': 1, 'maxCostUsd': 0}
    assert calls and (root/'runtime.json').stat().st_mode & 0o077 == 0
    task['dataSensitivity']='private'; taskfile.write_text(json.dumps(task))
    with pytest.raises(ValueError,match='public'): scout.prepare(scout.private_dir(tmp_path/'other'),taskfile,tmp_path/'auth',tmp_path/'inbox')


def test_run_calls_existing_entrypoint_once_and_failure_blocks_next_run(monkeypatch, tmp_path):
    root=scout.private_dir(tmp_path/'runtime'); taskfile=tmp_path/'task.json'
    taskfile.write_text(json.dumps({'id':'t','dataSensitivity':'public','repoPath':str(tmp_path)}))
    monkeypatch.setattr(scout,'refresh',lambda *a: None)
    scout.prepare(root,taskfile,tmp_path/'auth',tmp_path/'inbox')
    calls=[]
    def run(command,**kwargs):
        calls.append(command);return SimpleNamespace(returncode=0,stdout=json.dumps({'status':'failed'}),stderr='')
    monkeypatch.setattr(scout.subprocess,'run',run)
    assert scout.main(['run','--directory',str(root)])==1
    assert len(calls)==1 and calls[0][1].endswith('/scripts/odysseus-run')
    assert calls[0][-4:]==['--max-attempts','1','--budget','0']
    with pytest.raises(ValueError,match='previous run failed'):scout.main(['run','--directory',str(root)])
    assert len(calls)==1


def test_database_claim_refuses_existing_data_and_wrong_inode(tmp_path):
    root=scout.private_dir(tmp_path/'runtime');data=scout.private_dir(root/'data')
    (data/'app.db').write_text('unrelated existing database')
    with pytest.raises(ValueError,match='existing'):scout.claim_database(root)
    assert (data/'app.db').read_text()=='unrelated existing database'
    (data/'app.db').unlink()
    db,marker=scout.claim_database(root)
    db.write_text('owned')
    scout.write_json(marker,{'database_identity':[db.stat().st_dev,db.stat().st_ino]})
    assert scout.claim_database(root)[0]==db
    scout.write_json(marker,{'database_identity':[0,0]})
    with pytest.raises(ValueError,match='ownership'):scout.claim_database(root)


def test_uncertain_execution_is_durably_one_shot(monkeypatch,tmp_path):
    root=scout.private_dir(tmp_path/'runtime');taskfile=tmp_path/'task.json'
    taskfile.write_text(json.dumps({'id':'t','dataSensitivity':'public','repoPath':str(tmp_path)}))
    monkeypatch.setattr(scout,'refresh',lambda *a:None)
    scout.prepare(root,taskfile,tmp_path/'auth',tmp_path/'inbox')
    def timeout(*args,**kwargs):raise scout.subprocess.TimeoutExpired('scout',150)
    monkeypatch.setattr(scout.subprocess,'run',timeout)
    with pytest.raises(scout.subprocess.TimeoutExpired):scout.main(['run','--directory',str(root)])
    assert (root/'attempt-started.json').exists()
    with pytest.raises(ValueError,match='already started'):scout.main(['run','--directory',str(root)])
