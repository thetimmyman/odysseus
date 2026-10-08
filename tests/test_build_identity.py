"""The app reports baked identity; verification refuses inferred or ambiguous source."""
import ast
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from src import build_identity as identity

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40
NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)


def fields():
    return {'git_sha': SHA, 'branch': 'main', 'built_at': '2026-10-06T00:00:00Z'}


@pytest.fixture
def artifact(tmp_path, monkeypatch):
    path = tmp_path / 'build.json'
    monkeypatch.setattr(identity, 'BUILD_IDENTITY_FILE', str(path))
    return path


def test_baked_build_and_route_ignore_runtime_environment_and_git(artifact, monkeypatch):
    value = fields()
    assert identity.main(['write', '--git-sha', value['git_sha'], '--branch', value['branch'], '--built-at', value['built_at']]) == 0
    monkeypatch.setenv('ODYSSEUS_BUILD_GIT_SHA', 'f' * 40)
    monkeypatch.setenv('ODYSSEUS_BUILD_BRANCH', 'dev')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('identity attempted runtime Git'))
    result = identity.version_payload()
    assert result == {'version': identity.APP_VERSION, 'status': 'pinned', **value}
    assert artifact.stat().st_mode & 0o777 == 0o444
    # Execute the real minimal route body without importing app startup side effects.
    route = next(n for n in ast.parse((ROOT / 'app.py').read_text()).body
                 if isinstance(n, ast.AsyncFunctionDef) and n.name == 'get_version')
    route.decorator_list = []
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[route], type_ignores=[])), str(ROOT / 'app.py'), 'exec'), namespace)
    assert asyncio.run(namespace['get_version']()) == result


@pytest.mark.parametrize('value', [None, 'not json', [], {'format': 1},
    {'format': 1, **fields(), 'git_sha': SHA[:7]},
    {'format': 1, **fields(), 'built_at': '2026-10-06T00:00:00'},
    {'format': 1, **fields(), 'branch': None}])
def test_missing_or_invalid_artifact_is_unknown(artifact, value):
    if value is not None:
        artifact.write_text(value if isinstance(value, str) else json.dumps(value))
    result = identity.version_payload()
    assert result['status'] == 'unknown' and result['git_sha'] is None


def test_default_development_build_is_explicitly_unknown(artifact):
    assert identity.main(['write']) == 0
    assert identity.read_identity()['status'] == 'unknown'


def test_partial_explicit_build_identity_fails_without_artifact(artifact, capsys):
    assert identity.main(['write', '--git-sha', SHA]) == 1
    assert not artifact.exists()
    assert 'requires a branch' in capsys.readouterr().err


def test_verified_application_image_and_main_source_agree():
    value = {'status': 'pinned', **fields()}
    assert identity.verify(value, SHA, SHA, now=NOW)['status'] == 'verified'


@pytest.mark.parametrize('change', ['expected', 'image', 'branch', 'status', 'future', 'short'])
def test_identity_mismatch_cannot_report_success(change):
    value = {'status': 'pinned', **fields()}; expected = image = SHA
    if change == 'expected': expected = 'b' * 40
    if change == 'image': image = 'c' * 40
    if change == 'branch': value['branch'] = 'dev'
    if change == 'status': value['status'] = 'unknown'
    if change == 'future': value['built_at'] = '2026-10-08T00:00:00Z'
    if change == 'short': value['git_sha'] = SHA[:7]
    with pytest.raises(ValueError): identity.verify(value, expected, image, now=NOW)


def test_verify_cli_nonzero_on_wrong_source_and_invalid_json():
    args = [sys.executable, '-m', 'src.build_identity', 'verify', '--expect-sha', 'b' * 40, '--image-revision', SHA]
    value = {'status': 'pinned', **fields()}
    p = subprocess.run(args, input=json.dumps(value), capture_output=True, text=True, cwd=ROOT)
    assert p.returncode == 1 and not p.stdout and 'do not agree' in p.stderr
    p = subprocess.run(args, input='bad-json', capture_output=True, text=True, cwd=ROOT)
    assert p.returncode == 1 and not p.stdout


def test_invalid_deploy_candidate_refuses_before_external_actions(tmp_path):
    p = subprocess.run(['bash', str(ROOT / 'deploy-odysseus.sh'), 'abcdef0'],
                       env={**os.environ, 'ODYSSEUS_REPO_DIR': str(tmp_path)}, capture_output=True, text=True)
    assert p.returncode == 1 and 'full 40-character' in p.stderr
    assert not list(tmp_path.iterdir())
