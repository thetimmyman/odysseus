"""Inert escape controls: even a broken guard cannot create a real process."""
import asyncio
import subprocess
from types import SimpleNamespace

import pytest

from tests.helpers.docker_cli import block_native_transports


@pytest.fixture
def inert_spawners(monkeypatch):
    calls = []

    def synchronous(*args, **options):
        calls.append(('synchronous', args, options))
        return SimpleNamespace(returncode=0)

    async def asynchronous(*args, **options):
        calls.append(('asynchronous', args, options))
        return SimpleNamespace(returncode=0)

    # Install every underlying spawner BEFORE wrapping it. This fixture never
    # captures or delegates to a native spawner, including on a failing test.
    monkeypatch.setattr(subprocess, 'run', synchronous)
    monkeypatch.setattr(subprocess, 'Popen', synchronous)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', asynchronous)
    block_native_transports(monkeypatch)
    return calls


@pytest.mark.parametrize('api', ['run', 'popen', 'async'])
@pytest.mark.parametrize('argv', [
    ['docker', 'info'], ('/usr/bin/docker', 'info'), '/usr/bin/docker',
    ['ssh', 'synthetic.invalid'], ('/usr/bin/ssh', 'synthetic.invalid'), 'ssh synthetic.invalid',
])
def test_native_transport_is_refused_before_underlying_spawn(inert_spawners, api, argv):
    with pytest.raises(AssertionError, match='native Docker or SSH'):
        if api == 'run':
            subprocess.run(argv)
        elif api == 'popen':
            subprocess.Popen(argv)
        else:
            # The async API takes variadic argv. Its single string form is
            # still recognized conservatively before an underlying spawner.
            args = argv if isinstance(argv, (list, tuple)) else (argv,)
            asyncio.run(asyncio.create_subprocess_exec(*args))
    assert inert_spawners == []


@pytest.mark.parametrize('api', ['run', 'popen', 'async'])
def test_executable_override_cannot_escape_guard(inert_spawners, api):
    with pytest.raises(AssertionError, match='native Docker or SSH'):
        if api == 'run':
            subprocess.run(['inert-name'], executable='/usr/bin/ssh')
        elif api == 'popen':
            subprocess.Popen(['inert-name'], executable='/usr/bin/docker')
        else:
            asyncio.run(asyncio.create_subprocess_exec('inert-name', executable='/usr/bin/ssh'))
    assert inert_spawners == []


@pytest.mark.parametrize('api', ['run', 'popen', 'async'])
def test_non_native_cli_still_reaches_only_the_inert_fake(inert_spawners, api):
    if api == 'run':
        result = subprocess.run(['synthetic-cli', '--status'])
    elif api == 'popen':
        result = subprocess.Popen(('synthetic-cli', '--status'))
    else:
        result = asyncio.run(asyncio.create_subprocess_exec('synthetic-cli', '--status'))
    assert result.returncode == 0 and len(inert_spawners) == 1


@pytest.mark.parametrize('api', ['run', 'popen', 'async'])
def test_bytes_executable_override_is_normalized_before_spawn(inert_spawners, api):
    with pytest.raises(AssertionError, match='native Docker or SSH'):
        if api == 'run':
            subprocess.run(['inert-name'], executable=b'/usr/bin/ssh')
        elif api == 'popen':
            subprocess.Popen(['inert-name'], executable=b'/usr/bin/docker')
        else:
            asyncio.run(asyncio.create_subprocess_exec('inert-name', executable=b'/usr/bin/ssh'))
    assert inert_spawners == []


@pytest.mark.parametrize('api', ['run', 'popen'])
def test_positional_executable_is_checked_before_spawn(inert_spawners, api):
    with pytest.raises(AssertionError, match='native Docker or SSH'):
        if api == 'run':
            subprocess.run(['inert-name'], -1, b'/usr/bin/ssh')
        else:
            subprocess.Popen(['inert-name'], -1, '/usr/bin/docker')
    assert inert_spawners == []


@pytest.mark.parametrize('api', ['run', 'popen'])
@pytest.mark.parametrize('argv', [[b'/usr/bin/docker', b'info'], ('ssh', 'synthetic.invalid'), b'/usr/bin/ssh'])
def test_keyword_args_is_checked_before_spawn(inert_spawners, api, argv):
    with pytest.raises(AssertionError, match='native Docker or SSH'):
        if api == 'run':
            subprocess.run(args=argv)
        else:
            subprocess.Popen(args=argv)
    assert inert_spawners == []


@pytest.mark.parametrize('api', ['run', 'popen', 'async'])
@pytest.mark.parametrize('argv,options', [
    ('synthetic-cli', {'shell': True}),
    (['/bin/sh', '-c', 'docker info'], {}),
    (['env', 'ssh', 'synthetic.invalid'], {}),
    (['timeout', '1', 'docker', 'info'], {}),
    ('synthetic-cli;docker', {}),
])
def test_unsupported_shell_or_wrapper_forms_refuse_without_parsing(inert_spawners, api, argv, options):
    with pytest.raises(AssertionError, match='native Docker or SSH'):
        if api == 'run':
            subprocess.run(argv, **options)
        elif api == 'popen':
            subprocess.Popen(argv, **options)
        else:
            args = argv if isinstance(argv, (list, tuple)) else (argv,)
            asyncio.run(asyncio.create_subprocess_exec(*args, **options))
    assert inert_spawners == []


@pytest.mark.parametrize('api', ['run', 'popen'])
def test_ordinary_keyword_args_preserves_the_inert_fake(inert_spawners, api):
    options = {'args': b'synthetic-cli', 'executable': b'/synthetic/ordinary-cli'}
    result = subprocess.run(**options) if api == 'run' else subprocess.Popen(**options)
    assert result.returncode == 0 and len(inert_spawners) == 1
    assert inert_spawners[0][2] == options
