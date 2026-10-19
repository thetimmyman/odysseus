"""The CalDAV client must never wait forever on a server.

python-caldav's ``DAVClient`` defaults ``timeout`` to ``None`` and hands that to
the HTTP session, so an endpoint that accepts a connection and then says nothing
blocks the request for good. The sync (periodic or manual) and the write-back
both build their client in ``_build_dav_client``, which has to set a finite
timeout. That timeout bounds each socket read, not the request, so a server that
trickles its answer out a few bytes at a time is never cut off by it; the pull's
time budget therefore has to cap the request itself. Everything here is local: a
socket that listens and stays silent, or one that answers a byte at a time,
stands in for the stuck server.
"""
import asyncio
import contextvars
import math
import socket
import threading
import time
import types

import pytest

from src import caldav_sync


@pytest.mark.parametrize(
    "func, name, default",
    [
        (caldav_sync.request_timeout_seconds, "CALDAV_REQUEST_TIMEOUT_SECONDS", caldav_sync.DEFAULT_REQUEST_TIMEOUT_SECONDS),
        (caldav_sync.sync_timeout_seconds, "CALDAV_SYNC_TIMEOUT_SECONDS", caldav_sync.DEFAULT_SYNC_TIMEOUT_SECONDS),
    ],
)
@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, "default"),
        ("", "default"),
        ("12", 12.0),
        (" 2.5 ", 2.5),
        ("0.2", 0.2),
        # Anything that would mean "no limit" falls back to the default instead.
        ("0", "default"),
        ("-3", "default"),
        ("soon", "default"),
        ("inf", "default"),
        ("-inf", "default"),
        ("nan", "default"),
    ],
)
def test_timeouts_are_finite_positive_numbers(func, name, default, raw, expected):
    env = {} if raw is None else {name: raw}
    value = func(env)
    assert value == (default if expected == "default" else expected)
    assert math.isfinite(value) and value > 0


def test_defaults_are_bounded_and_ordered():
    assert 0 < caldav_sync.DEFAULT_REQUEST_TIMEOUT_SECONDS <= 120
    assert caldav_sync.DEFAULT_REQUEST_TIMEOUT_SECONDS < caldav_sync.DEFAULT_SYNC_TIMEOUT_SECONDS


def test_timeouts_read_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("CALDAV_REQUEST_TIMEOUT_SECONDS", "7")
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "90")
    assert caldav_sync.request_timeout_seconds() == 7
    assert caldav_sync.sync_timeout_seconds() == 90


def test_the_client_is_built_with_a_finite_network_timeout(monkeypatch):
    pytest.importorskip("caldav")
    monkeypatch.delenv("CALDAV_REQUEST_TIMEOUT_SECONDS", raising=False)
    client = caldav_sync._build_dav_client("https://calendar.example.com/dav", "u", "p")
    assert client.timeout == caldav_sync.DEFAULT_REQUEST_TIMEOUT_SECONDS

    monkeypatch.setenv("CALDAV_REQUEST_TIMEOUT_SECONDS", "7")
    client = caldav_sync._build_dav_client("https://calendar.example.com/dav", "u", "p")
    assert client.timeout == 7


def test_a_server_that_never_answers_is_given_up_on(monkeypatch):
    """The request raises after the timeout instead of blocking for good."""
    pytest.importorskip("caldav")
    monkeypatch.setenv("CALDAV_REQUEST_TIMEOUT_SECONDS", "0.5")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    accepted = []

    def hold():
        try:
            while True:
                conn, _ = listener.accept()
                accepted.append(conn)  # take the connection, never read or answer
        except OSError:
            pass

    threading.Thread(target=hold, daemon=True).start()
    url = f"http://127.0.0.1:{listener.getsockname()[1]}/dav"
    outcome = {}

    def call():
        client = caldav_sync._build_dav_client(url, "u", "p")
        started = time.monotonic()
        try:
            client.request(url, "PROPFIND", "")
        except Exception as exc:  # the type is the HTTP library's
            outcome["error"] = exc
        outcome["seconds"] = time.monotonic() - started

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    try:
        worker.join(15)
        assert not worker.is_alive(), "the request is still waiting on a server that never answers"
        assert "error" in outcome, "a silent server must surface as an error, not a result"
        assert outcome["seconds"] < 10
    finally:
        listener.close()
        for conn in accepted:
            conn.close()


# ── a server that trickles its answer ─────────────────────────────────────────

class _TricklingServer:
    """Accepts requests and answers each one a byte at a time.

    Every byte arrives well inside the per-read timeout, so only a cap on the
    request as a whole can end the wait. ``head_interval`` slows the response
    header as well (default: the header arrives at once, then the body drips).
    """

    def __init__(self, interval=0.05, head_interval=None, body_bytes=300, body=None):
        self.interval = interval
        self.head_interval = head_interval
        self.body_bytes = body_bytes
        self.body = body  # a complete answer, sent at once; the default never completes
        self.requests = 0
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}/dav"
        self._conns = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        try:
            while True:
                conn, _ = self.listener.accept()
                self._conns.append(conn)
                threading.Thread(target=self._answer, args=(conn,), daemon=True).start()
        except OSError:
            pass

    def _answer(self, conn):
        try:
            conn.settimeout(5)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, rest = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            while len(rest) < length:
                rest += conn.recv(65536)
            self.requests += 1
            size = len(self.body) if self.body is not None else self.body_bytes + 100
            header = (
                b"HTTP/1.1 207 Multi-Status\r\nContent-Type: application/xml\r\n"
                b"Content-Length: %d\r\n\r\n" % size
            )
            if self.head_interval is None:
                conn.sendall(header)
            else:
                for byte in header:
                    conn.sendall(bytes([byte]))
                    time.sleep(self.head_interval)
            if self.body is not None:
                conn.sendall(self.body)
                return
            for _ in range(self.body_bytes):
                conn.sendall(b" ")
                time.sleep(self.interval)
        except OSError:
            pass

    def close(self):
        self.listener.close()
        for conn in self._conns:
            try:
                conn.close()
            except OSError:
                pass


@pytest.fixture
def trickle():
    servers = []

    def make(**kwargs):
        servers.append(_TricklingServer(**kwargs))
        return servers[-1]

    yield make
    for server in servers:
        server.close()


def _request_threads():
    return [t for t in threading.enumerate() if t.name == caldav_sync._REQUEST_THREAD_NAME and t.is_alive()]


def _wait_until_none_alive(seconds=5):
    stop = time.monotonic() + seconds
    while time.monotonic() < stop and _request_threads():
        time.sleep(0.02)
    return _request_threads()


@pytest.mark.parametrize("head_interval", [None, 0.02], ids=["body-trickles", "header-trickles"])
def test_a_server_that_trickles_its_answer_is_cut_off_at_the_budget(monkeypatch, trickle, head_interval):
    """The 5s per-read timeout never fires: a byte arrives every 50ms for 15s.
    The request still ends when the pull's budget does, and leaves no thread."""
    pytest.importorskip("caldav")
    monkeypatch.setenv("CALDAV_REQUEST_TIMEOUT_SECONDS", "5")
    server = trickle(interval=0.05, head_interval=head_interval)
    client = caldav_sync._build_dav_client(server.url, "u", "p", deadline=time.monotonic() + 1)

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        client.request(server.url, "PROPFIND", "")
    elapsed = time.monotonic() - started

    assert 0.8 <= elapsed < 4, f"cut off after {elapsed:.1f}s; the budget was 1s"
    assert server.requests == 1
    assert _wait_until_none_alive() == [], "the abandoned request must not keep a thread"


def test_a_request_without_a_pull_deadline_is_capped_by_the_pull_budget(monkeypatch, trickle):
    """A client built without a deadline (the write-back always passes one; see
    test_caldav_writeback_deadline.py) caps each request by CALDAV_SYNC_TIMEOUT_SECONDS
    instead of letting it run as long as the server likes."""
    pytest.importorskip("caldav")
    monkeypatch.setenv("CALDAV_REQUEST_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "1")
    server = trickle(interval=0.05)
    client = caldav_sync._build_dav_client(server.url, "u", "p")

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        client.request(server.url, "PROPFIND", "")

    assert time.monotonic() - started < 4
    assert _wait_until_none_alive() == []


def test_a_request_that_starts_after_the_deadline_is_not_sent(trickle):
    pytest.importorskip("caldav")
    server = trickle()
    client = caldav_sync._build_dav_client(server.url, "u", "p", deadline=time.monotonic() - 1)

    with pytest.raises(TimeoutError):
        client.request(server.url, "PROPFIND", "")

    time.sleep(0.2)
    assert server.requests == 0


def test_a_prompt_server_is_not_affected_by_the_cap(trickle):
    pytest.importorskip("caldav")
    server = trickle(body=b"<?xml version='1.0'?><d:multistatus xmlns:d='DAV:'/>")
    client = caldav_sync._build_dav_client(server.url, "u", "p", deadline=time.monotonic() + 30)

    response = client.request(server.url, "PROPFIND", "")

    assert response.status == 207
    assert server.requests == 1
    assert _wait_until_none_alive() == []


# ── the owner lock is not held by a trickling server ──────────────────────────

def test_a_sync_against_a_trickling_server_ends_and_frees_the_owner(monkeypatch, trickle):
    """The reported scenario: with only per-read timeouts the worker thread (and
    with it the owner's lock) stayed in the request for as long as bytes kept
    coming. The pull's budget now ends it, so the next sync for that owner runs."""
    pytest.importorskip("caldav")
    import core.database as cdb

    monkeypatch.setenv("CALDAV_REQUEST_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "1")
    server = trickle(interval=0.05)
    # Nothing reaches the database before the budget stops the pull.
    monkeypatch.setattr(cdb, "SessionLocal", lambda: types.SimpleNamespace(close=lambda: None), raising=False)
    monkeypatch.setattr(
        caldav_sync, "_load_caldav_accounts",
        lambda owner: [{"id": "acc", "label": "Trickle", "url": server.url, "username": "u", "password": "p"}],
    )
    monkeypatch.setattr(caldav_sync, "validate_caldav_url", lambda url: url)

    async def run():
        started = time.monotonic()
        result = await asyncio.wait_for(caldav_sync.sync_caldav("tricklefan"), timeout=15)
        return result, time.monotonic() - started, caldav_sync._owner_lock("tricklefan").locked()

    result, elapsed, still_locked = asyncio.run(run())

    assert elapsed < 8, f"the pull took {elapsed:.1f}s against a 1s budget"
    assert not still_locked, "the owner's lock must be released once the pull has ended"
    assert any("time budget" in error for error in result["errors"]), result["errors"]
    assert _wait_until_none_alive() == []


# ── the capped call itself ────────────────────────────────────────────────────

def test_call_within_returns_the_result_and_runs_in_the_callers_context():
    var = contextvars.ContextVar("who", default="nobody")
    var.set("me")
    assert caldav_sync._call_within(5, 1, lambda a, b=0: (a + b, var.get()), 2, b=3) == (5, "me")


def test_call_within_raises_what_the_call_raised():
    class Boom(Exception):
        pass

    def fail():
        raise Boom("nope")

    with pytest.raises(Boom, match="nope"):
        caldav_sync._call_within(5, 1, fail)


def test_call_within_stops_a_call_that_outlives_its_time():
    ran = {"stopped": False}

    def forever():
        try:
            while True:
                time.sleep(0.01)
        except BaseException:
            ran["stopped"] = True
            raise

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        caldav_sync._call_within(0.3, 2, forever)

    assert time.monotonic() - started < 1.5
    assert _wait_until_none_alive() == []
    assert ran["stopped"], "the thread must be told to stop, not left running"


def test_call_within_does_not_start_a_call_without_time():
    called = []
    with pytest.raises(TimeoutError):
        caldav_sync._call_within(0, 1, lambda: called.append(1))
    with pytest.raises(TimeoutError):
        caldav_sync._call_within(-3, 1, lambda: called.append(1))
    assert called == []


def test_a_thread_stuck_past_its_grace_stays_counted_and_caps_new_requests(monkeypatch):
    """A request blocked in a call that cannot be interrupted (DNS) outlives the
    grace; it must stay tracked, and once too many are stuck no new one starts."""
    release = threading.Event()
    monkeypatch.setattr(caldav_sync, "_stop_thread", lambda thread: False)
    monkeypatch.setattr(caldav_sync, "MAX_OVERDUE_REQUESTS", 2)
    monkeypatch.setattr(caldav_sync, "_overdue_threads", set())
    try:
        for _ in range(2):
            with pytest.raises(TimeoutError):
                caldav_sync._call_within(0.05, 0.01, release.wait)
        assert caldav_sync._overdue_count() == 2

        called = []
        with pytest.raises(TimeoutError, match="still stuck"):
            caldav_sync._call_within(5, 1, lambda: called.append(1))
        assert called == []

        release.set()
        deadline = time.monotonic() + 3
        while caldav_sync._overdue_count() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert caldav_sync._overdue_count() == 0
        assert caldav_sync._call_within(5, 1, lambda: "ok") == "ok"
    finally:
        release.set()
