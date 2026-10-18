"""A CalDAV write-back is bounded as a whole, and cannot pile up on the executor.

``writeback_event`` is awaited inside the event routes and runs on a thread of the
loop's default executor, which every other ``asyncio.to_thread`` caller shares. One
write-back makes several requests, so capping each request at the pull budget (5
minutes) still let a server that trickles bytes hold a thread for a budget per
request, and nothing stopped one more write-back from starting while the last was
still stuck. The write-back now has one deadline for the whole of it, one run per
owner and a small limit across owners, and the limits stay taken for as long as the
worker thread lives, not just for as long as the route waits. Everything is local:
a socket that answers a byte at a time stands in for the stuck server, and a worker
that waits on an event stands in for a write-back that does not end.
"""
import asyncio
import socket
import threading
import time
from datetime import datetime

import pytest

from src import caldav_sync
from src import caldav_writeback as wb


# ── the budget ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, "default"),
        ("", "default"),
        ("12", 12.0),
        (" 2.5 ", 2.5),
        ("0", "default"),
        ("-3", "default"),
        ("soon", "default"),
        ("inf", "default"),
        ("nan", "default"),
    ],
)
def test_the_writeback_budget_is_a_finite_positive_number(raw, expected):
    env = {} if raw is None else {wb.ENV_WRITEBACK_TIMEOUT: raw}
    value = wb.writeback_timeout_seconds(env)
    assert value == (wb.DEFAULT_WRITEBACK_TIMEOUT_SECONDS if expected == "default" else expected)


def test_the_default_budget_is_tens_of_seconds_and_inside_the_request_timeout():
    # A route answers with the result only if the write-back is over before the
    # HTTP request timeout (REQUEST_HARD_TIMEOUT, 45s by default) cuts it off.
    assert 10 <= wb.DEFAULT_WRITEBACK_TIMEOUT_SECONDS <= 40
    assert wb.DEFAULT_WRITEBACK_TIMEOUT_SECONDS < caldav_sync.DEFAULT_SYNC_TIMEOUT_SECONDS


def test_the_budget_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "17")
    assert wb.writeback_timeout_seconds() == 17


# ── a trickling server cannot stretch the write-back ──────────────────────────

class _Trickler:
    """Answers every request a byte at a time and counts them."""

    def __init__(self, interval=0.05):
        self.interval = interval
        self.requests = 0
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(5)
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}/dav/"
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
            conn.sendall(
                b"HTTP/1.1 207 Multi-Status\r\nContent-Type: application/xml\r\n"
                b"Content-Length: 1000000\r\n\r\n"
            )
            for _ in range(2000):
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
def trickler():
    made = []

    def make(**kwargs):
        made.append(_Trickler(**kwargs))
        return made[-1]

    yield make
    for server in made:
        server.close()


def _event():
    return {"uid": "u1", "summary": "x", "dtstart": datetime(2026, 1, 1, 9), "dtend": datetime(2026, 1, 1, 10)}


@pytest.mark.parametrize("delete", [False, True], ids=["create", "delete"])
def test_a_write_back_against_a_trickling_server_ends_at_its_deadline(monkeypatch, trickler, delete):
    """The reported scenario. Every request there gets what is left of the one
    deadline, so the write-back takes the deadline in all and sends no request
    once it is spent; before, each of its requests got a whole pull budget (here
    10s) and the write-back took one budget per request."""
    pytest.importorskip("caldav")
    monkeypatch.setenv("CALDAV_REQUEST_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "1")
    server = trickler()
    cal_id = wb._stable_cal_id(server.url, owner="o", account_id="a")

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        wb._writeback_blocking(cal_id, _event(), delete, server.url, "u", "p", "o", "a")
    elapsed = time.monotonic() - started

    assert 0.8 <= elapsed < 4, f"took {elapsed:.1f}s against a 1s write-back budget"
    assert server.requests == 1, "no request may follow the one that spent the deadline"


def test_the_deadline_that_the_caller_passes_is_the_one_used(monkeypatch, trickler):
    pytest.importorskip("caldav")
    monkeypatch.setenv("CALDAV_REQUEST_TIMEOUT_SECONDS", "5")
    monkeypatch.setenv("CALDAV_SYNC_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "30")
    server = trickler()
    cal_id = wb._stable_cal_id(server.url, owner="o", account_id="a")

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        wb._writeback_blocking(
            cal_id, _event(), False, server.url, "u", "p", "o", "a",
            deadline=caldav_sync._clock() + 1,
        )

    assert time.monotonic() - started < 4


def test_a_write_back_whose_deadline_has_passed_sends_nothing(trickler):
    pytest.importorskip("caldav")
    server = trickler()
    cal_id = wb._stable_cal_id(server.url, owner="o", account_id="a")

    with pytest.raises(TimeoutError):
        wb._writeback_blocking(
            cal_id, _event(), False, server.url, "u", "p", "o", "a",
            deadline=caldav_sync._clock() - 1,
        )

    time.sleep(0.2)
    assert server.requests == 0


class _TimedOutCalendar:
    url = "https://dav.invalid/cal/"

    def event_by_uid(self, uid):
        raise TimeoutError("a CalDAV request did not finish within its time budget")

    def save_event(self, ical):  # pragma: no cover - must not be reached
        raise AssertionError("an event that could not be looked up must not be created again")


@pytest.mark.parametrize("delete", [False, True], ids=["save", "delete"])
def test_a_lookup_that_runs_out_of_time_is_not_taken_for_a_missing_event(delete):
    """It used to read as "no such event": a delete reported the event already gone
    and a save created a second copy of it."""
    calendar = _TimedOutCalendar()
    cal_id = wb._stable_cal_id(str(calendar.url))
    with pytest.raises(TimeoutError):
        wb.push_event([calendar], cal_id, _event(), delete=delete)


def test_discovery_that_runs_out_of_time_does_not_fall_back_to_the_url():
    pytest.importorskip("caldav")

    class Client:
        url = "https://dav.invalid/dav/"

        def principal(self):
            raise TimeoutError("out of time")

        def calendar(self, url):  # pragma: no cover - must not be reached
            raise AssertionError("no time is left to try the url as a calendar")

    with pytest.raises(TimeoutError):
        wb._discover_calendars(Client())


def test_discovery_that_fails_otherwise_still_falls_back_to_the_url():
    pytest.importorskip("caldav")

    class Client:
        url = "https://dav.invalid/dav/"

        def principal(self):
            raise RuntimeError("no principal here")

        def calendar(self, url):
            return ("calendar", url)

    assert wb._discover_calendars(Client()) == [("calendar", "https://dav.invalid/dav/")]


# ── the gate: one per owner, a few in all, held while the thread lives ────────

@pytest.fixture
def account(monkeypatch):
    """One CalDAV account for every owner, with the network side faked."""
    import src.secret_storage as secrets

    monkeypatch.setattr(caldav_sync, "_load_caldav_accounts", lambda owner: [
        {"id": "acct", "url": "https://dav.invalid/", "username": "u", "password": "p"},
    ])
    monkeypatch.setattr(caldav_sync, "validate_caldav_url", lambda url: url)
    monkeypatch.setattr(secrets, "decrypt", lambda value: value)


class _Worker:
    """Stands in for ``_writeback_blocking``: counts who is inside, waits to be let go."""

    def __init__(self, monkeypatch):
        self.lock = threading.Lock()
        self.inside = []
        self.peak = 0
        self.peak_by_owner = {}
        self.calls = []
        self.deadlines = []
        self.release = threading.Event()
        self.entered = threading.Semaphore(0)
        monkeypatch.setattr(wb, "_writeback_blocking", self)

    def __call__(self, local_cal_id, ev, delete, url, username, password,
                 owner="", account_id="", deadline=None):
        with self.lock:
            self.calls.append(owner)
            self.deadlines.append(deadline)
            self.inside.append(owner)
            self.peak = max(self.peak, len(self.inside))
            same = self.inside.count(owner)
            self.peak_by_owner[owner] = max(self.peak_by_owner.get(owner, 0), same)
        self.entered.release()
        try:
            self.release.wait(20)
            return {"ok": True}
        finally:
            with self.lock:
                self.inside.remove(owner)

    def wait_inside(self, count=1, seconds=5):
        for _ in range(count):
            assert self.entered.acquire(timeout=seconds), "the worker never started"


async def _ask(owner, **kwargs):
    return await wb.writeback_event(owner, "caldav", "caldav-1", {"uid": f"u-{owner}"}, **kwargs)


async def test_one_write_back_runs_per_owner_at_a_time(monkeypatch, account):
    worker = _Worker(monkeypatch)
    monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "10")
    try:
        first = asyncio.create_task(_ask("alice"))
        await asyncio.to_thread(worker.wait_inside)
        second = asyncio.create_task(_ask("alice"))
        await asyncio.sleep(0.3)
        assert worker.calls == ["alice"], "a second write-back for the owner must wait its turn"
        worker.release.set()
        assert await first == {"ok": True}
        assert await second == {"ok": True}
    finally:
        worker.release.set()
    assert worker.calls == ["alice", "alice"]
    assert worker.peak_by_owner == {"alice": 1}


async def test_an_owner_who_is_not_served_in_time_is_told_so_and_not_run(monkeypatch, account):
    worker = _Worker(monkeypatch)
    monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "0.4")
    try:
        first = asyncio.create_task(_ask("alice"))
        await asyncio.to_thread(worker.wait_inside)
        started = time.monotonic()
        waiting = await _ask("alice")
        assert time.monotonic() - started < 2
        assert waiting["ok"] is False and "still running" in waiting["error"]
        assert worker.calls == ["alice"]
        worker.release.set()
        await first
    finally:
        worker.release.set()


async def test_writebacks_across_owners_are_limited_and_do_not_wait_on_each_other(monkeypatch, account):
    """Different owners run side by side up to the limit, never more: that is what
    keeps the shared executor from being used up by write-backs."""
    worker = _Worker(monkeypatch)
    monkeypatch.setattr(wb, "MAX_CONCURRENT_WRITEBACKS", 2)
    monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "10")
    try:
        running = [asyncio.create_task(_ask(name)) for name in ("a", "b")]
        await asyncio.to_thread(worker.wait_inside, 2)
        monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "0.3")  # the two above keep their long deadline
        refused = [await _ask(name) for name in ("c", "d")]
        for result in refused:
            assert result["ok"] is False and "too many" in result["error"]
        assert worker.peak == 2
        assert sorted(worker.calls) == ["a", "b"], "the refused write-backs must never have run"

        worker.release.set()
        for task in running:
            assert await task == {"ok": True}
        # The refusal gave back what it had taken: the owner it turned away can go on.
        monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "5")
        assert await _ask("c") == {"ok": True}
    finally:
        worker.release.set()
    assert worker.peak == 2


async def test_the_caller_has_its_answer_at_the_deadline_while_the_thread_goes_on(monkeypatch, account):
    worker = _Worker(monkeypatch)
    monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "0.4")
    try:
        started = time.monotonic()
        result = await _ask("alice")
        elapsed = time.monotonic() - started
        assert result["ok"] is False and "timed out" in result["error"]
        assert 0.3 <= elapsed < 3
        assert worker.inside == ["alice"], "the thread cannot be taken back; it is still there"

        # ...so it still counts: the owner's next write-back is not started beside it.
        again = await _ask("alice")
        assert again["ok"] is False and "still running" in again["error"]
        assert worker.calls == ["alice"]

        worker.release.set()
        for _ in range(100):
            if not worker.inside:
                break
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.1)  # the worker task ends and gives the turns back
        monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "5")
        assert await _ask("alice") == {"ok": True}
    finally:
        worker.release.set()


async def test_a_cancelled_route_leaves_the_thread_counted(monkeypatch, account):
    """The HTTP request timeout cancels the route; the thread it started cannot be
    cancelled, so it must keep the owner's turn until it ends."""
    worker = _Worker(monkeypatch)
    monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "10")
    try:
        route = asyncio.create_task(_ask("alice"))
        await asyncio.to_thread(worker.wait_inside)
        route.cancel()
        with pytest.raises(asyncio.CancelledError):
            await route
        assert worker.inside == ["alice"]

        monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "0.3")
        retry = await _ask("alice")
        assert retry["ok"] is False and "still running" in retry["error"]
        assert worker.calls == ["alice"]

        worker.release.set()
        await asyncio.sleep(0.3)
        monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "5")
        assert await _ask("alice") == {"ok": True}
    finally:
        worker.release.set()


async def test_a_waiter_that_is_cancelled_takes_nothing_with_it(monkeypatch, account):
    worker = _Worker(monkeypatch)
    monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "10")
    try:
        first = asyncio.create_task(_ask("alice"))
        await asyncio.to_thread(worker.wait_inside)
        waiter = asyncio.create_task(_ask("alice"))
        await asyncio.sleep(0.2)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

        worker.release.set()
        assert await first == {"ok": True}
        assert await _ask("alice") == {"ok": True}, "the cancelled waiter must not have kept the turn"
    finally:
        worker.release.set()
    assert worker.calls == ["alice", "alice"]


async def test_the_worker_receives_the_deadline_set_when_the_write_back_was_asked_for(monkeypatch, account):
    worker = _Worker(monkeypatch)
    worker.release.set()
    monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "20")

    before = caldav_sync._clock()
    assert await _ask("alice") == {"ok": True}
    after = caldav_sync._clock()

    (deadline,) = worker.deadlines
    assert before + 20 <= deadline <= after + 20


async def test_a_write_back_that_waited_in_the_pool_past_its_deadline_is_not_started(monkeypatch, account):
    """With the pool busy a work item can sit unstarted. When it finally gets a
    thread its time is up and it must not open a connection; and abandoning the
    wait must not cancel it, or the turns it holds would never be given back."""
    worker = _Worker(monkeypatch)
    worker.release.set()
    pool_free = asyncio.Event()
    real_to_thread = asyncio.to_thread

    async def late_to_thread(func, /, *args, **kwargs):
        await pool_free.wait()
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", late_to_thread)
    monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "0.3")

    result = await _ask("alice")
    assert result["ok"] is False and "timed out" in result["error"]

    again = await _ask("alice")
    assert again["ok"] is False and "still running" in again["error"]

    pool_free.set()
    await asyncio.sleep(0.3)
    assert worker.calls == [], "its time was up before it got a thread, so it must not have run"

    monkeypatch.setattr(asyncio, "to_thread", real_to_thread)
    monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "5")
    assert await _ask("alice") == {"ok": True}


async def test_a_write_back_that_raises_is_a_result_and_gives_the_turns_back(monkeypatch, account):
    calls = []

    def boom(*args, **kwargs):
        calls.append(args[0])
        raise RuntimeError("server exploded")

    monkeypatch.setattr(wb, "_writeback_blocking", boom)
    monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "5")

    first = await _ask("alice")
    second = await _ask("alice")

    assert first == second == {"ok": False, "error": "server exploded"}
    assert len(calls) == 2


async def test_a_write_back_that_runs_out_of_time_inside_is_reported_as_timed_out(monkeypatch, account):
    def out_of_time(*args, **kwargs):
        raise TimeoutError("a CalDAV request did not finish within the 1s left of its time budget")

    monkeypatch.setattr(wb, "_writeback_blocking", out_of_time)
    monkeypatch.setenv(wb.ENV_WRITEBACK_TIMEOUT, "5")

    result = await _ask("alice")

    assert result == {"ok": False, "error": "CalDAV write-back timed out"}
    assert await _ask("alice") == result, "the owner's turn was given back"


async def test_a_calendar_that_is_not_caldav_never_reaches_the_gate(monkeypatch, account):
    worker = _Worker(monkeypatch)
    assert await wb.writeback_event("alice", "local", "loc-1", {"uid": "u"}) == {"skipped": "not a caldav calendar"}
    assert worker.calls == []
