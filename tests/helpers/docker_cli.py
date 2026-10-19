"""External Docker CLI fixture; never permits a real Docker process."""
import asyncio
import json
import os
import subprocess


class FakeDocker:
    """Docker CLI seam only; no daemon, images or containers are created."""
    cid = 'c' * 64
    image = 'sha256:' + 'd' * 64

    def __init__(self, calls, stdout=b'ok', stderr=b'', returncode=0):
        self.calls, self.stdout, self.stderr, self.returncode = calls, stdout, stderr, returncode
        self.row = None
        self.fault = None
        self.artifacts = None
        self.behavior = None

    def __call__(self, argv, **options):
        self.calls.append((argv, options))
        data, code = b'', 0
        truncated = [False, False]
        timed_out = False
        if argv[1:3] == ['image', 'inspect']:
            data = self.image.encode() + b'\n'
        elif argv[1] == 'create':
            labels = dict(value.split('=', 1) for index, value in enumerate(argv) if index and argv[index-1] == '--label')
            idx = argv.index(self.image)
            mount = argv[argv.index('-v') + 1]
            if self.behavior is not None:
                self.returncode, self.stdout = self.behavior(' '.join(argv[idx+1:]), mount.split(':')[0])
            self.row = {'Id': self.cid, 'Name': '/' + argv[argv.index('--name')+1], 'Image': self.image,
                'Config': {'Labels': labels, 'Cmd': argv[idx+1:], 'Entrypoint': [],
                           'User': argv[argv.index('--user')+1], 'WorkingDir': '/work'},
                'HostConfig': {'NetworkMode': 'none', 'Privileged': False, 'AutoRemove': False,
                    'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges'], 'PidsLimit': 64,
                    'Memory': 2 * 1024**3, 'NanoCpus': 10**9, 'Tmpfs': {'/tmp': ''}, 'Binds': [mount]},
                'Mounts': [{'Type': 'bind', 'Source': mount.split(':')[0], 'Destination': '/work', 'RW': True}],
                'State': {'Running': False, 'ExitCode': self.returncode}}
            data = self.cid.encode() + b'\n'
            if self.fault == 'lost-create-ack':
                data = b''
            if self.fault == 'foreign-label':
                self.row['Config']['Labels']['odysseus.sandbox.nonce'] = 'foreign'
        elif argv[1] == 'inspect':
            assert argv[-1] == self.cid
            data = json.dumps([self.row]).encode()
        elif argv[1:3] == ['start', '--attach']:
            assert argv[-1] == self.cid
            # The exact inspected CID must be durable before start.
            if self.artifacts is not None:
                created = list(self.artifacts.glob('*.ownership.created.json'))
                assert len(created) == 1 and json.loads(created[0].read_text())['container_id'] == self.cid
            truncated = [len(self.stdout) > options['limit'], len(self.stderr) > options['limit']]
            return {'code': self.returncode, 'stdout': self.stdout[:options['limit']],
                    'stderr': self.stderr[:options['limit']], 'stdout_truncated': truncated[0],
                    'stderr_truncated': truncated[1], 'timed_out': self.fault == 'timeout'}
        elif argv[1] == 'rm':
            assert argv == ['docker', 'rm', '--force', '--', self.cid]
            if self.fault == 'cleanup-failed':
                code = 1
        elif argv[1:3] == ['container', 'ls']:
            assert 'id=' + self.cid in argv
            if self.fault == 'cleanup-failed':
                data = self.cid.encode() + b'\n'
        else:
            raise AssertionError(argv)
        return {'code': code, 'stdout': data, 'stderr': b'', 'stdout_truncated': truncated[0],
                'stderr_truncated': truncated[1], 'timed_out': timed_out}



def _refuse_native_transport(args, options, positional_executable=None):
    """Conservative test backstop, not a shell or command-language parser."""
    native = ('docker', 'ssh')
    wrappers = ('sh', 'bash', 'dash', 'zsh', 'fish', 'env', 'busybox',
                'sudo', 'doas', 'su', 'nohup', 'timeout', 'nice', 'setsid')
    message = 'Source fixture escaped to native Docker or SSH'

    def program(value):
        return os.path.basename(os.fsdecode(value)).lower()

    for executable in (options.get('executable'), positional_executable):
        if executable is not None:
            if program(executable) in native:
                raise AssertionError(message)
            if program(executable) in wrappers:
                raise AssertionError(message + ': unsupported wrapper')
    if options.get('shell'):
        raise AssertionError(message + ': unsupported shell form')
    if isinstance(args, (str, bytes, os.PathLike)):
        raw = os.fsdecode(args)
        if program(raw) in native:
            raise AssertionError(message)
        # A bare executable string is supported. Shell syntax or compound
        # string forms refuse rather than acquiring a second parser.
        if any(character.isspace() or character in "\"'`;|&<>$()" for character in raw):
            raise AssertionError(message + ': unsupported compound string')
        args = [raw]
    if not isinstance(args, (list, tuple)) or not args:
        return
    if program(args[0]) in native:
        raise AssertionError(message)
    if program(args[0]) in wrappers:
        raise AssertionError(message + ': unsupported wrapper')
    first = os.fsdecode(args[0])
    if any(character.isspace() or character in "\"'`;|&<>$()" for character in first):
        raise AssertionError(message + ': unsupported compound executable')


def block_native_transports(monkeypatch):
    """Wrap run/Popen/async exec before Docker, SSH or unsupported wrappers.

    Inert controls replace every underlying spawner before calling this helper.
    Ordinary list/tuple/bare-string and args= fake calls retain their semantics.
    """
    original_run, original_popen = subprocess.run, subprocess.Popen
    original_async = asyncio.create_subprocess_exec

    def run(*args, **options):
        _refuse_native_transport(args[0] if args else options.get('args'), options,
                                 args[2] if len(args) > 2 else None)
        return original_run(*args, **options)

    def popen(*args, **options):
        _refuse_native_transport(args[0] if args else options.get('args'), options,
                                 args[2] if len(args) > 2 else None)
        return original_popen(*args, **options)

    async def asynchronous(*args, **options):
        _refuse_native_transport(args, options)
        return await original_async(*args, **options)

    monkeypatch.setattr(subprocess, 'run', run)
    monkeypatch.setattr(subprocess, 'Popen', popen)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', asynchronous)
