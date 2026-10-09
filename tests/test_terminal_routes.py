"""Owned Terminal cwd, resource guards and fork/exec admission without models."""

import asyncio
import errno
import inspect
import json
import os
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocket, WebSocketDisconnect


@pytest.fixture(scope="module")
def native_auth(tmp_path_factory):
    from core.auth import AuthManager

    manager = AuthManager(str(tmp_path_factory.mktemp("terminal-auth") / "auth.json"))
    tokens = {}
    for name, admin in (("admin", True), ("otheradmin", True), ("regular", False)):
        password = "synthetic-terminal-test-password"
        assert manager.create_user(name, password, is_admin=admin)
        tokens[name] = manager.create_session(name, password)
        assert tokens[name]
    return manager, tokens


@pytest.fixture
def terminal_api(monkeypatch, tmp_path, native_auth):
    import core.database as database
    import core.models as models
    import core.session_manager as managers
    import routes.terminal_routes as terminals

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    database.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(managers, "SessionLocal", factory)
    manager = managers.SessionManager()
    monkeypatch.setattr(models, "_session_manager", manager)
    manager.create_session("owned", "Owned", "", "synthetic", owner="admin")
    manager.create_session("foreign", "Foreign", "", "synthetic", owner="otheradmin")
    monkeypatch.setenv("ALLOWED_ORIGINS", "http://localhost")
    app = FastAPI()
    app.state.auth_manager, tokens = native_auth
    router = terminals.setup_terminal_routes()
    app.include_router(router)
    calls = []

    def refuse_spawn(cwd, cols, rows):
        calls.append(cwd)
        # The executor is outside ownership-validation endpoint tests. An
        # attempted spawn remains visible and fails the no-admission assertion.
        raise RuntimeError("synthetic executor admission")

    monkeypatch.setattr(terminals, "_spawn_pty", refuse_spawn)
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client, app=app, manager=manager, module=terminals,
            tokens=tokens, calls=calls, project=tmp_path, endpoint=router.routes[0].endpoint,
        )
    engine.dispose()


def _headers(api, user="admin", origin="http://localhost"):
    return {"origin": origin, "cookie": f"odysseus_session={api.tokens[user]}"}


def _refusal(api, sid="owned", **headers):
    with api.client.websocket_connect(f"/ws/terminal?session_id={sid}", headers=_headers(api, **headers)) as ws:
        message = ws.receive_json()
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
    assert closed.value.code == 1011
    assert message["type"] == "error"
    assert api.app.state.terminal_ptys == {}
    return message["msg"]


@pytest.mark.parametrize("sid", ["missing", "foreign"])
def test_missing_or_foreign_session_never_admits_executor(terminal_api, sid):
    api = terminal_api
    assert _refusal(api, sid) == "Conversation is unavailable. Open a conversation you own, then reconnect."
    assert api.calls == []


@pytest.mark.parametrize("kind", ["deleted", "file", "sensitive", "malformed"])
def test_invalid_saved_project_never_falls_back_or_changes_record(terminal_api, kind):
    api = terminal_api
    root = api.project / "saved-project"
    if kind == "deleted":
        root.mkdir()
        root.rmdir()
    elif kind == "file":
        root.write_text("fixture")
    elif kind == "sensitive":
        root = api.project / ".ssh"
        root.mkdir()
    else:
        root = "invalid\x00project"
    saved = str(root)
    api.manager.set_session_project_root("owned", saved)
    assert _refusal(api) == (
        "Selected project folder is unavailable. Choose another folder or clear it, then reconnect."
    )
    assert api.calls == []
    assert api.manager.get_session("owned").project_root == saved


def test_owned_symlink_is_canonical_and_reconnect_can_recover(terminal_api):
    api = terminal_api
    gone = api.project / "gone"
    api.manager.set_session_project_root("owned", str(gone))
    assert "Selected project folder is unavailable" in _refusal(api)
    valid = api.project / "valid"
    valid.mkdir()
    link = api.project / "alias"
    link.symlink_to(valid, target_is_directory=True)
    api.manager.set_session_project_root("owned", str(link))
    _refusal(api)
    assert api.calls == [str(valid)]
    assert api.manager.get_session("owned").project_root == str(link)


@pytest.mark.parametrize("sid", ["", "owned"])
def test_default_is_allowed_for_no_session_or_genuinely_unset_root(terminal_api, sid):
    api = terminal_api
    _refusal(api, sid)
    expected = next(p for p in ("/app/work", os.path.expanduser("~"), "/") if os.path.isdir(p))
    assert api.calls == [expected]
    assert api.manager.get_session("owned").project_root is None


def test_session_storage_unavailable_does_not_fall_back(terminal_api, monkeypatch):
    import core.models as models

    api = terminal_api
    monkeypatch.setattr(models, "_session_manager", None)
    assert _refusal(api) == "Conversation storage is unavailable. Try again before reconnecting."
    assert api.calls == []


def test_unconfigured_auth_matches_only_genuine_ownerless_session(terminal_api):
    api = terminal_api
    api.app.state.auth_manager = None
    api.manager.create_session("anonymous", "Local", "", "synthetic", owner=None)
    _refusal(api, "anonymous")
    assert len(api.calls) == 1
    api.calls.clear()
    assert "Conversation is unavailable" in _refusal(api, "owned")
    assert api.calls == []


@pytest.mark.parametrize("credentials", ["missing", "invalid", "regular", "bearer"])
def test_real_cookie_and_admin_checks_refuse_before_accept(terminal_api, credentials):
    api = terminal_api
    headers = {"origin": "http://localhost"}
    if credentials == "regular":
        headers.update(_headers(api, user="regular"))
    elif credentials == "invalid":
        headers["cookie"] = "odysseus_session=not-a-session"
    elif credentials == "bearer":
        headers["authorization"] = "Bearer synthetic-not-a-cookie"
    with pytest.raises(WebSocketDisconnect) as closed:
        with api.client.websocket_connect("/ws/terminal?session_id=owned", headers=headers):
            pytest.fail("unauthorized handshake accepted")
    assert closed.value.code == 1008
    assert api.calls == []


def test_origin_gate_remains_load_bearing_with_real_admin_cookie(terminal_api):
    api = terminal_api
    with pytest.raises(WebSocketDisconnect) as closed:
        with api.client.websocket_connect(
            "/ws/terminal?session_id=owned", headers=_headers(api, origin="https://untrusted.invalid"),
        ):
            pytest.fail("foreign origin accepted")
    assert closed.value.code == 1008
    assert api.calls == []


def test_exact_owner_is_required_even_for_another_real_admin(terminal_api):
    api = terminal_api
    assert "Conversation is unavailable" in _refusal(api, user="otheradmin")
    assert api.calls == []


async def _invoke(function, *args):
    """Also permits running behavior controls against the original sync source."""
    result = function(*args)
    return await result if inspect.isawaitable(result) else result


def _observe_fork(monkeypatch, module):
    original = module.pty.fork
    children = []

    def fork():
        pid, fd = original()
        if pid:
            children.append((pid, fd))
        return pid, fd

    monkeypatch.setattr(module.pty, "fork", fork)
    return children


def _assert_child_cleaned(children):
    assert len(children) == 1
    pid, fd = children[0]
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    with pytest.raises(OSError) as closed:
        os.fstat(fd)
    assert closed.value.errno == errno.EBADF


@pytest.mark.parametrize("kind", ["deleted_after_resolution", "file", "malformed"])
def test_child_cwd_failure_is_reported_before_exec_and_reaped(monkeypatch, tmp_path, kind):
    import routes.terminal_routes as module

    root = tmp_path / "project"
    root.mkdir()
    resolved = os.path.realpath(root)
    if kind == "deleted_after_resolution":
        root.rmdir()
    elif kind == "file":
        root.rmdir()
        root.write_text("fixture")
    else:
        resolved = "bad\x00project"
    children = _observe_fork(monkeypatch, module)
    read_fd, write_fd = os.pipe()
    original_exec = os.execvpe

    def observe_exec(*args):
        os.write(write_fd, b"executed")
        original_exec(*args)

    monkeypatch.setattr(module.os, "execvpe", observe_exec)

    async def check():
        try:
            with pytest.raises(module.TerminalStartupError, match="Selected project folder is unavailable"):
                await _invoke(module._spawn_pty, resolved, 80, 24)
            _assert_child_cleaned(children)
        finally:
            # Negative controls against the old code must also clean their PTY.
            for pid, fd in children:
                await _invoke(module._reap, pid, fd)

    try:
        asyncio.run(check())
        os.close(write_fd)
        write_fd = None
        assert os.read(read_fd, 64) == b""
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def test_exec_failure_is_explicit_and_cleans_child(monkeypatch, tmp_path):
    import routes.terminal_routes as module

    shell = tmp_path / "nonexecutable-shell"
    shell.write_text("not executable")
    monkeypatch.setenv("SHELL", str(shell))
    # Isolate exec refusal from unrelated same-UID desktop task consumption.
    monkeypatch.setattr(module, "_has_scoped_process_limit", lambda: True)
    children = _observe_fork(monkeypatch, module)

    async def check():
        with pytest.raises(module.TerminalStartupError, match="Terminal shell could not start"):
            await module._spawn_pty(str(tmp_path), 80, 24)
        _assert_child_cleaned(children)

    asyncio.run(check())


@pytest.mark.parametrize("scoped", [False, True])
def test_real_exec_admission_preserves_cwd_argv_environment_and_limits(monkeypatch, tmp_path, scoped):
    import resource
    import routes.terminal_routes as module

    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setattr(module, "_has_scoped_process_limit", lambda: scoped)
    if not scoped:
        # This case measures real applied limits and exec admission. The
        # separate capacity controls exercise the probe, independently of
        # unrelated host workloads sharing this test process's real UID.
        monkeypatch.setattr(module, "_probe_command_capacity", lambda: None)
    inherited_nproc = resource.getrlimit(resource.RLIMIT_NPROC)
    inherited_as = resource.getrlimit(resource.RLIMIT_AS)

    def lowered(bounds, ceiling):
        return [ceiling if value == resource.RLIM_INFINITY else min(value, ceiling) for value in bounds]

    children = _observe_fork(monkeypatch, module)
    read_fd, write_fd = os.pipe()
    original_exec = os.execvpe
    original_pipe = os.pipe
    startup_pipes = []

    def observe_pipe():
        pair = original_pipe()
        startup_pipes.append(pair)
        return pair

    def observe_exec(shell, argv, env):
        proof = {
            "cwd": os.getcwd(), "shell": shell, "argv": argv, "env_keys": sorted(env),
            "nproc": resource.getrlimit(resource.RLIMIT_NPROC),
            "as": resource.getrlimit(resource.RLIMIT_AS), "core": resource.getrlimit(resource.RLIMIT_CORE),
            "cloexec": not os.get_inheritable(startup_pipes[0][1]),
        }
        os.write(write_fd, json.dumps(proof).encode())
        original_exec(shell, argv, env)

    monkeypatch.setattr(module.os, "execvpe", observe_exec)
    monkeypatch.setattr(module.os, "pipe", observe_pipe)

    async def check():
        pid, fd = await module._spawn_pty(str(tmp_path), 80, 24)
        try:
            assert children == [(pid, fd)]
            assert os.get_blocking(fd) is False
        finally:
            await module._reap(pid, fd)
        _assert_child_cleaned(children)

    try:
        asyncio.run(check())
        proof = json.loads(os.read(read_fd, 4096))
        assert proof == {
            "cwd": str(tmp_path), "shell": "/bin/sh", "argv": ["-sh"],
            "env_keys": ["HOME", "LANG", "LC_ALL", "PATH", "PS1", "TERM", "USER"],
            "nproc": list(inherited_nproc) if scoped else lowered(inherited_nproc, 512),
            "as": lowered(inherited_as, 32 * 1024 ** 3), "core": [0, 0], "cloexec": True,
        }
    finally:
        os.close(read_fd)
        os.close(write_fd)


@pytest.mark.parametrize("cancel", [False, True])
def test_startup_deadline_and_cancellation_are_async_and_clean(monkeypatch, tmp_path, cancel):
    import routes.terminal_routes as module

    children = _observe_fork(monkeypatch, module)
    original_chdir = os.chdir

    def delayed_chdir(cwd):
        original_chdir(cwd)
        time.sleep(10)

    monkeypatch.setattr(module.os, "chdir", delayed_chdir)
    monkeypatch.setattr(module, "STARTUP_TIMEOUT_S", 0.05)

    async def check():
        ticks = []

        async def ticker():
            while True:
                ticks.append(1)
                await asyncio.sleep(0.005)

        ticking = asyncio.create_task(ticker())
        task = asyncio.create_task(module._spawn_pty(str(tmp_path), 80, 24))
        try:
            if cancel:
                await asyncio.sleep(0.025)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(module.TerminalStartupError, match="startup timed out"):
                    await task
            assert len(ticks) >= 3
            _assert_child_cleaned(children)
        finally:
            ticking.cancel()
            await asyncio.gather(ticking, return_exceptions=True)

    started = time.monotonic()
    asyncio.run(check())
    assert time.monotonic() - started < 2


def test_fork_failure_closes_both_startup_pipe_descriptors(monkeypatch, tmp_path):
    import routes.terminal_routes as module

    original_pipe = os.pipe
    descriptors = []

    def pipe():
        pair = original_pipe()
        descriptors.extend(pair)
        return pair

    def broken_fork():
        raise OSError("synthetic fork failure")

    async def check():
        with monkeypatch.context() as patch:
            patch.setattr(module.os, "pipe", pipe)
            patch.setattr(module.pty, "fork", broken_fork)
            with pytest.raises(OSError, match="synthetic fork failure"):
                await module._spawn_pty(str(tmp_path), 80, 24)
        assert len(descriptors) == 2
        for fd in descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)

    asyncio.run(check())


@pytest.mark.parametrize("state", ["already_reaped", "exited", "foreign_group", "own_group"])
def test_cleanup_signals_only_unreaped_owned_child_group(monkeypatch, state):
    import routes.terminal_routes as module

    pid = 9876543
    read_fd, write_fd = os.pipe()
    signals = []
    waits = iter([(0, 0), (pid, 0)])
    original_close = os.close

    def close(fd):
        if state in ("foreign_group", "own_group"):
            assert signals, "closing the PTY before signalling can orphan descendants"
        original_close(fd)

    def waitpid(*args):
        if state == "already_reaped":
            raise ChildProcessError
        if state == "exited":
            return pid, 0
        return next(waits)

    async def check():
        with monkeypatch.context() as patch:
            patch.setattr(module.os, "waitpid", waitpid)
            patch.setattr(module.os, "getpgid", lambda _: pid if state == "own_group" else pid - 1)
            patch.setattr(module.os, "kill", lambda *args: signals.append(("pid", *args)))
            patch.setattr(module.os, "killpg", lambda *args: signals.append(("group", *args)))
            patch.setattr(module.os, "close", close)
            await module._reap(pid, read_fd)

    try:
        asyncio.run(check())
        with pytest.raises(OSError):
            os.fstat(read_fd)
        expected = [] if state in ("already_reaped", "exited") else [
            ("group" if state == "own_group" else "pid", pid, module.signal.SIGKILL),
        ]
        assert signals == expected
    finally:
        os.close(write_fd)


def test_cleanup_terminates_live_owned_group_before_master_close():
    import routes.terminal_routes as module

    info_read, info_write = os.pipe()
    hold_read, hold_write = os.pipe()
    master, peer = os.pipe()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(info_read)
            os.close(hold_write)
            os.close(master)
            os.close(peer)
            os.setsid()
            descendant = os.fork()
            if descendant:
                os.write(info_write, f"{descendant}\n".encode())
            while True:
                os.read(hold_read, 1)
        finally:
            os._exit(0)
    os.close(info_write)
    os.close(hold_read)
    os.set_blocking(info_read, False)

    async def read_info():
        async with asyncio.timeout(2):
            while True:
                try:
                    return os.read(info_read, 64)
                except BlockingIOError:
                    await asyncio.sleep(0.005)

    async def check():
        try:
            descendant = int(await read_info())
            assert os.getpgid(pid) == pid
            assert os.getpgid(descendant) == pid
            await module._reap(pid, master)
            # Both processes hold the info writer while blocked. EOF proves
            # group cleanup closed the descendant's descriptor as well.
            assert await read_info() == b""
            with pytest.raises(ChildProcessError):
                os.waitpid(pid, os.WNOHANG)
        finally:
            await module._reap(pid, master)

    try:
        asyncio.run(check())
    finally:
        os.close(info_read)
        os.close(hold_write)
        os.close(peer)


def test_pending_starts_reserve_cap_even_when_accept_yields_and_cancel_cleans(terminal_api, monkeypatch):
    api = terminal_api
    module = api.module
    endpoint = api.endpoint

    async def check():
        accepted = 0
        all_accepted = asyncio.Event()
        never = asyncio.Event()
        starts = []
        frames = []

        async def pending(cwd, cols, rows):
            starts.append(cwd)
            await never.wait()

        monkeypatch.setattr(module, "_spawn_pty", pending)

        def connection():
            connected = False

            async def receive():
                nonlocal connected
                if not connected:
                    connected = True
                    return {"type": "websocket.connect"}
                await never.wait()

            async def send(message):
                nonlocal accepted
                frames.append(message)
                if message["type"] == "websocket.accept":
                    accepted += 1
                    if accepted == module.MAX_CONCURRENT_PTYS + 1:
                        all_accepted.set()
                    await all_accepted.wait()

            scope = {
                "type": "websocket", "path": "/ws/terminal", "query_string": b"session_id=owned",
                "headers": [(k.encode(), v.encode()) for k, v in _headers(api).items()],
                "app": api.app, "client": ("127.0.0.1", 12345), "scheme": "ws", "server": ("localhost", 80),
            }
            return WebSocket(scope, receive, send)

        tasks = [asyncio.create_task(endpoint(connection())) for _ in range(module.MAX_CONCURRENT_PTYS + 1)]
        try:
            async with asyncio.timeout(2):
                while len(starts) < module.MAX_CONCURRENT_PTYS or not any(
                    frame["type"] == "websocket.close" for frame in frames
                ):
                    await asyncio.sleep(0)
            assert len(starts) == module.MAX_CONCURRENT_PTYS
            assert len(api.app.state.terminal_ptys) == module.MAX_CONCURRENT_PTYS
            assert [f["code"] for f in frames if f["type"] == "websocket.close"] == [1013]
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        assert api.app.state.terminal_ptys == {}

    asyncio.run(check())


@pytest.mark.parametrize("ceiling", ["1\n", "512\n"])
def test_only_bounded_private_readonly_cgroup_v2_root_qualifies(monkeypatch, tmp_path, ceiling):
    import routes.terminal_routes as module

    observations = {
        "CGROUP_MEMBERSHIP_PATH": "0::/\n",
        "CGROUP_MOUNTS_PATH": "11 10 0:1 / /sys/fs/cgroup ro,nosuid,nodev,noexec - cgroup2 cgroup rw\n",
        "CGROUP_PIDS_PATH": ceiling,
    }
    for name, content in observations.items():
        path = tmp_path / name
        path.write_text(content)
        monkeypatch.setattr(module, name, path)
    assert module._has_scoped_process_limit() is True


@pytest.mark.parametrize(
    "kind,content",
    [
        ("CGROUP_MEMBERSHIP_PATH", "0::/user.slice\n"),
        ("CGROUP_MEMBERSHIP_PATH", "0::/../other\n"),
        ("CGROUP_MEMBERSHIP_PATH", "5:pids:/\n"),
        ("CGROUP_MEMBERSHIP_PATH", "0::/\n5:pids:/\n"),
        ("CGROUP_MEMBERSHIP_PATH", ""),
        ("CGROUP_MOUNTS_PATH", "11 10 0:1 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"),
        ("CGROUP_MOUNTS_PATH", "11 10 0:1 / /sys/fs/cgroup ro,rw - cgroup2 cgroup rw\n"),
        ("CGROUP_MOUNTS_PATH", "11 10 0:1 /subtree /sys/fs/cgroup ro - cgroup2 cgroup rw\n"),
        ("CGROUP_MOUNTS_PATH", "11 10 0:1 / /elsewhere ro - cgroup2 cgroup rw\n"),
        ("CGROUP_MOUNTS_PATH", "11 10 0:1 / /sys/fs/cgroup ro - cgroup cgroup rw\n"),
        ("CGROUP_MOUNTS_PATH", "11 10 0:1 / /sys/fs/cgroup ro - tmpfs cgroup rw\n"),
        ("CGROUP_MOUNTS_PATH", "11 10 0:1 / /sys/fs/cgroup ro cgroup2 cgroup rw\n"),
        ("CGROUP_MOUNTS_PATH", "11 10 0:1 / /sys/fs/cgroup ro - cgroup2\n"),
        ("CGROUP_MOUNTS_PATH", "11 10 0:1 / /sys/fs/cgroup ro - cgroup2 cgroup rw\n" * 2),
        ("CGROUP_MOUNTS_PATH", ""),
        ("CGROUP_PIDS_PATH", "max\n"),
        ("CGROUP_PIDS_PATH", "513\n"),
        ("CGROUP_PIDS_PATH", "1024\n"),
        ("CGROUP_PIDS_PATH", "0\n"),
        ("CGROUP_PIDS_PATH", "-1\n"),
        ("CGROUP_PIDS_PATH", "5 12\n"),
        ("CGROUP_PIDS_PATH", "invalid\n"),
        ("CGROUP_PIDS_PATH", "\u0665\u0661\u0662\n"),
        ("CGROUP_PIDS_PATH", ""),
    ],
)
def test_unknown_or_weaker_cgroup_observations_never_qualify(monkeypatch, tmp_path, kind, content):
    import routes.terminal_routes as module

    observations = {
        "CGROUP_MEMBERSHIP_PATH": "0::/\n",
        "CGROUP_MOUNTS_PATH": "11 10 0:1 / /sys/fs/cgroup ro - cgroup2 cgroup rw\n",
        "CGROUP_PIDS_PATH": "512\n",
    }
    observations[kind] = content
    for name, value in observations.items():
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setattr(module, name, path)
    assert module._has_scoped_process_limit() is False


@pytest.mark.parametrize("kind", ["CGROUP_MEMBERSHIP_PATH", "CGROUP_MOUNTS_PATH", "CGROUP_PIDS_PATH"])
def test_unreadable_cgroup_observation_never_qualifies(monkeypatch, tmp_path, kind):
    import routes.terminal_routes as module

    observations = {
        "CGROUP_MEMBERSHIP_PATH": "0::/\n",
        "CGROUP_MOUNTS_PATH": "11 10 0:1 / /sys/fs/cgroup ro - cgroup2 cgroup rw\n",
        "CGROUP_PIDS_PATH": "512\n",
    }
    for name, value in observations.items():
        path = tmp_path / name
        path.write_text(value)
        monkeypatch.setattr(module, name, path)
        if name == kind:
            path.unlink()
    assert module._has_scoped_process_limit() is False


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize(
    "nproc,address,core",
    [
        ((-1, -1), (-1, -1), (-1, -1)),
        ((256, 4096), (16 * 1024 ** 3, 64 * 1024 ** 3), (4096, 8192)),
        ((128, 256), (8 * 1024 ** 3, 16 * 1024 ** 3), (0, 0)),
        ((512, 512), (32 * 1024 ** 3, 32 * 1024 ** 3), (0, 0)),
    ],
)
def test_child_limits_preserve_stricter_inherited_bounds(monkeypatch, scoped, nproc, address, core):
    import routes.terminal_routes as module

    inherited = {"nproc": nproc, "as": address, "core": core}
    applied = {}
    fake_resource = SimpleNamespace(
        RLIMIT_NPROC="nproc", RLIMIT_AS="as", RLIMIT_CORE="core", RLIM_INFINITY=-1,
        getrlimit=inherited.__getitem__, setrlimit=applied.__setitem__,
    )
    monkeypatch.setitem(sys.modules, "resource", fake_resource)
    monkeypatch.setattr(module, "_has_scoped_process_limit", lambda: scoped)
    module._apply_child_resource_limits()

    def lowered(bounds, ceiling):
        return tuple(ceiling if value == -1 else min(value, ceiling) for value in bounds)

    expected = {"as": lowered(address, 32 * 1024 ** 3), "core": (0, 0)}
    if not scoped:
        expected["nproc"] = lowered(nproc, 512)
    assert applied == expected
    assert inherited == {"nproc": nproc, "as": address, "core": core}


@pytest.mark.parametrize("guard", ["RLIMIT_NPROC", "RLIMIT_AS", "RLIMIT_CORE"])
@pytest.mark.parametrize("operation", ["getrlimit", "setrlimit"])
def test_child_guard_failure_refuses_before_exec_and_cleans(monkeypatch, tmp_path, guard, operation):
    import resource
    import routes.terminal_routes as module

    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("HOME", str(tmp_path))
    if hasattr(module, "_has_scoped_process_limit"):
        monkeypatch.setattr(module, "_has_scoped_process_limit", lambda: False)
    children = _observe_fork(monkeypatch, module)
    original_operation = getattr(resource, operation)
    inherited = {kind: resource.getrlimit(kind) for kind in (resource.RLIMIT_NPROC, resource.RLIMIT_AS, resource.RLIMIT_CORE)}
    read_fd, write_fd = os.pipe()
    original_exec = os.execvpe
    original_pipe = os.pipe
    startup_descriptors = []

    def observe_pipe():
        pair = original_pipe()
        startup_descriptors.extend(pair)
        return pair

    def broken(kind, *args):
        if kind == getattr(resource, guard):
            raise OSError("synthetic resource guard failure")
        return original_operation(kind, *args)

    def observe_exec(*args):
        os.write(write_fd, b"executed")
        original_exec(*args)

    monkeypatch.setattr(module.os, "pipe", observe_pipe)
    monkeypatch.setattr(resource, operation, broken)
    monkeypatch.setattr(module.os, "execvpe", observe_exec)

    async def check():
        try:
            with pytest.raises(module.TerminalStartupError, match="Terminal resource limits could not be applied"):
                await module._spawn_pty(str(tmp_path), 80, 24)
            _assert_child_cleaned(children)
        finally:
            for pid, fd in children:
                await module._reap(pid, fd)

    try:
        asyncio.run(check())
        os.close(write_fd)
        write_fd = None
        assert os.read(read_fd, 64) == b""
        assert len(startup_descriptors) == 2
        for fd in startup_descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)
    # Resource changes in the real forked child never alter its parent.
    if operation == "getrlimit":
        monkeypatch.setattr(resource, operation, original_operation)
    assert {kind: resource.getrlimit(kind) for kind in inherited} == inherited


@pytest.mark.parametrize("error", [errno.EAGAIN, errno.ENOMEM])
def test_child_capacity_failure_refuses_before_exec_and_cleans(monkeypatch, tmp_path, error):
    import routes.terminal_routes as module

    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("HOME", str(tmp_path))
    if hasattr(module, "_has_scoped_process_limit"):
        monkeypatch.setattr(module, "_has_scoped_process_limit", lambda: False)
    children = _observe_fork(monkeypatch, module)
    read_fd, write_fd = os.pipe()
    original_exec = os.execvpe

    original_pipe = os.pipe
    startup_descriptors = []

    def observe_pipe():
        pair = original_pipe()
        startup_descriptors.extend(pair)
        return pair

    def exhausted():
        raise OSError(error, "synthetic command capacity exhaustion")

    def observe_exec(*args):
        os.write(write_fd, b"executed")
        original_exec(*args)

    monkeypatch.setattr(module.os, "fork", exhausted)
    monkeypatch.setattr(module.os, "pipe", observe_pipe)
    monkeypatch.setattr(module.os, "execvpe", observe_exec)

    async def check():
        try:
            with pytest.raises(module.TerminalStartupError, match="Terminal cannot launch commands within its resource limits"):
                await module._spawn_pty(str(tmp_path), 80, 24)
            _assert_child_cleaned(children)
        finally:
            for pid, fd in children:
                await module._reap(pid, fd)

    try:
        asyncio.run(check())
        os.close(write_fd)
        write_fd = None
        assert os.read(read_fd, 64) == b""
        assert len(startup_descriptors) == 2
        for fd in startup_descriptors:
            with pytest.raises(OSError):
                os.fstat(fd)
    finally:
        os.close(read_fd)
        if write_fd is not None:
            os.close(write_fd)


def test_command_capacity_probe_forks_once_and_reaps(monkeypatch):
    import routes.terminal_routes as module

    original_fork = os.fork
    children = []

    def observed_fork():
        pid = original_fork()
        if pid:
            children.append(pid)
        return pid

    monkeypatch.setattr(module.os, "fork", observed_fork)
    module._probe_command_capacity()
    assert len(children) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(children[0], os.WNOHANG)


@pytest.mark.parametrize("status", [1 << 8, 9])
def test_command_capacity_probe_rejects_unsuccessful_child(monkeypatch, status):
    import routes.terminal_routes as module

    monkeypatch.setattr(module.os, "fork", lambda: 9876543)
    monkeypatch.setattr(module.os, "waitpid", lambda pid, flags: (pid, status))
    with pytest.raises(OSError, match="capacity probe failed"):
        module._probe_command_capacity()


def test_command_capacity_probe_retries_interrupted_wait(monkeypatch):
    import routes.terminal_routes as module

    calls = []

    def wait(pid, flags):
        calls.append((pid, flags))
        if len(calls) == 1:
            raise InterruptedError
        return pid, 0

    monkeypatch.setattr(module.os, "fork", lambda: 9876543)
    monkeypatch.setattr(module.os, "waitpid", wait)
    module._probe_command_capacity()
    assert calls == [(9876543, 0)] * 2


def test_scoped_policy_runs_real_probe_and_external_command(monkeypatch, tmp_path):
    import routes.terminal_routes as module

    # Detection is qualified separately with observations. This real child
    # integration test isolates command behavior from the replay host's UID.
    monkeypatch.setattr(module, "_has_scoped_process_limit", lambda: True)
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("HOME", str(tmp_path))
    children = _observe_fork(monkeypatch, module)

    async def check():
        pid, fd = await module._spawn_pty(str(tmp_path), 80, 24)
        output = bytearray()
        try:
            # The input itself does not contain the joined success marker.
            os.write(fd, b"/bin/echo CAPACITY_''EXTERNAL_OK\n")
            async with asyncio.timeout(5):
                while b"CAPACITY_EXTERNAL_OK" not in output:
                    try:
                        output.extend(os.read(fd, 4096))
                    except BlockingIOError:
                        await asyncio.sleep(0.01)
            assert b"CAPACITY_EXTERNAL_OK" in output
        finally:
            await module._reap(pid, fd)
        _assert_child_cleaned(children)

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["RESOURCE_LIMITS_UNAVAILABLE", "COMMAND_CAPACITY_UNAVAILABLE"])
def test_resource_startup_refusal_reaches_owned_websocket_and_releases_slot(terminal_api, monkeypatch, failure):
    api = terminal_api
    message = getattr(api.module, failure)

    async def refuse(cwd, cols, rows):
        raise api.module.TerminalStartupError(message)

    monkeypatch.setattr(api.module, "_spawn_pty", refuse)
    assert _refusal(api) == message
    assert api.app.state.terminal_ptys == {}
