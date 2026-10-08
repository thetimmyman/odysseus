"""Exercise native deployment failure/rollback with real Git and simulated Docker/HTTP."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
OLD = 'sha256:' + '1' * 64
NEW = 'sha256:' + '2' * 64


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True, stderr=subprocess.DEVNULL).strip()


@pytest.fixture
def installation(tmp_path):
    repo = tmp_path / 'repo'; repo.mkdir()
    git(repo, 'init', '-qb', 'main')
    (repo / '.gitignore').write_text('.env\n')
    (repo / 'Dockerfile').write_text('FROM scratch\n')
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=f@example.test', 'commit', '-qm', 'base')
    previous = git(repo, 'rev-parse', 'HEAD')
    (repo / 'application.txt').write_text('candidate')
    git(repo, 'add', '.')
    git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=f@example.test', 'commit', '-qm', 'candidate')
    sha = git(repo, 'rev-parse', 'HEAD')
    origin = tmp_path / 'origin.git'
    subprocess.run(['git', 'init', '--bare', '-q', str(origin)], check=True)
    git(repo, 'remote', 'add', 'origin', str(origin)); git(repo, 'push', '-q', 'origin', 'main')
    (repo / '.env').write_text('PASSWORD=ignored-secret-sentinel')
    config = tmp_path / 'private.json'
    config.write_text(json.dumps({'services': {'odysseus': {'image': OLD,
        'environment': {'PASSWORD': 'private-secret-sentinel'}, 'volumes': [{'target': '/app/data', 'source': '/private/data', 'type': 'bind'}]}}}))
    state = tmp_path / 'state.json'; state.write_text(json.dumps({'image': OLD, 'actions': []}))
    bindir = tmp_path / 'bin'; bindir.mkdir()
    common = f'''import io,json,os,sys,tarfile
from pathlib import Path
p=Path(os.environ['TEST_STATE']);s=json.loads(p.read_text());a=sys.argv[1:]
OLD={OLD!r};NEW={NEW!r};SHA=os.environ['TEST_SHA']
def save():p.write_text(json.dumps(s))
def version():return {{'status':'pinned','git_sha':SHA,'branch':'main','built_at':'2026-10-06T00:00:00Z','version':'1.0.0'}}
'''
    docker = common + '''
if a[0]=='inspect':
 fmt=a[-1]
 if fmt=='{{.Image}}':print(s['image'])
 elif 'config_files' in fmt:print(os.environ['TEST_CONFIG'])
 elif 'project' in fmt:print('odysseus')
 else:sys.exit(3)
elif a[:2]==['image','inspect']:
 print(NEW if a[-1]=='{{.Id}}' else SHA)
elif a[0]=='build':
 raw=sys.stdin.buffer.read();s['archive_files']=tarfile.open(fileobj=io.BytesIO(raw)).getnames();s['archive_has_secret']=b'ignored-secret-sentinel' in raw;save()
elif a[0]=='compose':
 config=Path(a[a.index('-f')+1])
 if 'config' in a:print(config.read_text())
 elif 'up' in a:
  s['image']=json.loads(config.read_text())['services']['odysseus']['image'];s['actions'].append(s['image']);save()
 else:sys.exit(3)
elif a[0] in ('run','rm'):pass
elif a[0]=='exec':
 code=a[-1]
 if '/api/ready' in code and os.environ.get('FAIL_CANARY'):sys.exit(1)
 if '/api/version' in code:print(json.dumps(version()))
else:sys.exit(3)
'''
    curl = common + '''
if '/api/ready' in a[-1]:print('{"ready":true}')
elif '/api/version' in a[-1]:
 value=version() if s['image']==NEW else {'version':'1.0.0'}
 if s['image']==NEW and os.environ.get('FAIL_LIVE_IDENTITY'):value['git_sha']='b'*40
 print(json.dumps(value))
else:sys.exit(3)
'''
    for name, code in [('docker', docker), ('curl', curl), ('sleep', 'pass')]:
        p = bindir / name; p.write_text('#!' + sys.executable + '\n' + code); p.chmod(0o755)
    env = {**os.environ, 'PATH': str(bindir) + os.pathsep + os.environ['PATH'],
           'ODYSSEUS_REPO_DIR': str(repo), 'ODYSSEUS_DEPLOY_STATE_DIR': str(tmp_path / 'releases'),
           'TEST_STATE': str(state), 'TEST_SHA': sha, 'TEST_CONFIG': str(config)}
    return repo, sha, previous, state, config, env


def deploy(installation, **extra):
    _, sha, _, _, _, env = installation
    return subprocess.run(['bash', str(ROOT / 'deploy-odysseus.sh'), sha],
                          env={**env, **extra}, capture_output=True, text=True, timeout=25)


def test_exact_archive_and_private_native_settings_survive_deploy(installation):
    repo, sha, _, state, config, env = installation
    p = deploy(installation)
    assert p.returncode == 0, p.stderr
    value = json.loads(state.read_text())
    assert value['image'] == NEW and value['actions'] == [NEW]
    assert '.env' not in value['archive_files'] and not value['archive_has_secret']
    packet = next(Path(env['ODYSSEUS_DEPLOY_STATE_DIR']).iterdir())
    active = json.loads((packet / 'activate.compose.json').read_text())
    before = json.loads(config.read_text())
    assert active['services']['odysseus']['environment'] == before['services']['odysseus']['environment']
    assert active['services']['odysseus']['volumes'] == before['services']['odysseus']['volumes']
    assert (packet / 'activate.compose.json').stat().st_mode & 0o777 == 0o600
    assert git(repo, 'rev-parse', 'HEAD') == sha
    receipt = json.loads((packet / 'receipt.json').read_text())
    assert receipt['previous_source']['status'] == 'unknown'
    assert receipt['checkout_before']['git_sha'] == sha
    assert 'Rollback:' in p.stdout
    assert 'secret-sentinel' not in p.stdout + p.stderr


def test_identity_failure_restores_previous_image_and_reports_failure(installation):
    p = deploy(installation, FAIL_LIVE_IDENTITY='1')
    value = json.loads(installation[3].read_text())
    assert p.returncode == 1 and value['image'] == OLD
    assert value['actions'] == [NEW, OLD]
    assert 'Restored previous image' in p.stdout and 'Verified main source' not in p.stdout


def test_failed_canary_never_changes_production(installation):
    p = deploy(installation, FAIL_CANARY='1')
    value = json.loads(installation[3].read_text())
    assert p.returncode == 1 and value['image'] == OLD and value['actions'] == []
    assert 'production was not changed' in p.stderr


def test_runtime_code_mount_is_refused_before_build_or_activation(installation):
    config = installation[4]
    value = json.loads(config.read_text());value['services']['odysseus']['volumes'].append({'target': '/app/src', 'source': '/mutable/code', 'type': 'bind'})
    config.write_text(json.dumps(value))
    p = deploy(installation)
    state = json.loads(installation[3].read_text())
    assert p.returncode != 0 and not state['actions'] and 'archive_files' not in state
    assert 'refusing runtime mount' in p.stderr


def test_non_main_production_authority_is_refused(installation):
    p = deploy(installation, ODYSSEUS_BRANCH='dev')
    assert p.returncode == 1 and 'authority must be main' in p.stderr
    assert json.loads(installation[3].read_text())['actions'] == []


def test_new_branch_ci_base_is_first_parent_and_invalid_base_is_loud(installation):
    repo, sha, previous, _, _, _ = installation
    script = str(ROOT / 'scripts/ci-base.sh')
    p = subprocess.run([script, '0' * 40, sha], cwd=repo, capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip() == previous
    p = subprocess.run([script, previous], cwd=repo, capture_output=True, text=True)
    assert p.returncode == 0 and p.stdout.strip() == previous
    p = subprocess.run([script, 'f' * 40], cwd=repo, capture_output=True, text=True)
    assert p.returncode != 0 and not p.stdout


def test_documented_rollback_and_reapply_restore_exact_images(installation):
    p = deploy(installation)
    assert p.returncode == 0, p.stderr
    env = installation[-1]
    packet = next(Path(env['ODYSSEUS_DEPLOY_STATE_DIR']).iterdir())
    for command, expected in [('rollback', OLD), ('reapply', NEW)]:
        result = subprocess.run(['bash', str(ROOT / 'deploy-odysseus.sh'), command, str(packet)],
                                env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert json.loads(installation[3].read_text())['image'] == expected
    assert json.loads(installation[3].read_text())['actions'] == [NEW, OLD, NEW]


def test_changed_rollback_packet_is_refused_before_service_change(installation):
    assert deploy(installation).returncode == 0
    env = installation[-1]
    packet = next(Path(env['ODYSSEUS_DEPLOY_STATE_DIR']).iterdir())
    with (packet / 'rollback.compose.json').open('a') as f:
        f.write('\n')
    p = subprocess.run(['bash', str(ROOT / 'deploy-odysseus.sh'), 'rollback', str(packet)], env=env, capture_output=True, text=True)
    assert p.returncode != 0
    assert json.loads(installation[3].read_text())['actions'] == [NEW]
