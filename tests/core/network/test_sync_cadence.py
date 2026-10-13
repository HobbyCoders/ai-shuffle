"""Connected peers settle to the idle exchange cadence (#1285).

Quiet peers back off from ``SYNC_IDLE_SECONDS`` to ``SYNC_IDLE_MAX_SECONDS``;
a local write still reaches the peer at once.

Both workers run their real ``sync_loop`` and ``scan_loop`` concurrently on a
virtual clock. Each worker gets its own copy of the transport module, so the
loop globals (wake events, links, contact times) stay separate; every database
call runs under that worker's context. Worker A is served to B (and B to A)
in-process through the real network router.
"""
import asyncio
import heapq
import importlib.util
import itertools
import time
from types import SimpleNamespace

import httpx
import pytest

from app.api import network as network_api
from app.db import database, session_events
from app.db import network as store
from app.network import blobs, engine, tokens, transport
from tests.core.network.harness import Worker, converge, join
from tests.core.network.test_transport import ServedBy

WARMUP = 20.0


class VirtualClock:
    """Timers resolve in order once every task is blocked; no wall-clock sleeps."""

    def __init__(self):
        self.now = 0.0
        self._timers = []
        self._order = itertools.count()

    def monotonic(self):
        return self.now

    def _timer(self, delay):
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._timers, (self.now + delay, next(self._order), future))
        return future

    async def sleep(self, seconds):
        await self._timer(seconds)

    async def wait_for(self, awaitable, *, timeout):
        task = asyncio.ensure_future(awaitable)
        timer = self._timer(timeout)
        done, _ = await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            timer.cancel()
            return task.result()
        task.cancel()
        raise asyncio.TimeoutError

    async def run_until(self, until):
        while True:
            for _ in range(300):
                await asyncio.sleep(0)
            while self._timers and self._timers[0][2].cancelled():
                heapq.heappop(self._timers)
            if not self._timers or self._timers[0][0] > until:
                self.now = until
                return
            self.now, _, future = heapq.heappop(self._timers)
            future.set_result(None)


class Node:
    """One worker with its own transport loop state."""

    def __init__(self, worker, clock):
        spec = importlib.util.spec_from_file_location(
            f"app.network._transport_{worker.name.replace('-', '_')}", transport.__file__)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.worker, self.clock = worker, clock
        self.sent = []  # (virtual time, path) of requests this node made


class _Engine:
    """``engine`` as seen by one node's loop code: context reads use its database."""

    def __init__(self, node):
        self._node = node

    def __getattr__(self, name):
        value = getattr(engine, name)
        if name in ("context", "require_context"):
            def scoped(*args, **kwargs):
                with self._node.worker.active():
                    return value(*args, **kwargs)
            return scoped
        return value


class _Served(ServedBy):
    """Answer as ``node`` and record the request against the calling node."""

    def __init__(self, node, caller, base_url):
        super().__init__(node.worker, base_url)
        self.node, self.caller = node, caller

    def handler(self, request):
        saved = network_api.transport
        network_api.transport = self.node.module
        try:
            response = super().handler(request)
        finally:
            network_api.transport = saved
        self.caller.sent.append((self.node.clock.now, self.requests[-1][1]))
        return response


def _wire(node, served, monkeypatch):
    worker, clock = node.worker, node.clock

    async def to_thread(fn, *args, **kwargs):
        with worker.active():
            return fn(*args, **kwargs)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(node.module, "httpx", SimpleNamespace(
        AsyncClient=lambda **kw: real_client(transport=served.transport(), **kw), HTTPError=httpx.HTTPError))
    monkeypatch.setattr(node.module, "engine", _Engine(node))
    monkeypatch.setattr(node.module, "time", SimpleNamespace(monotonic=clock.monotonic, time=time.time))
    monkeypatch.setattr(node.module, "asyncio", SimpleNamespace(
        Event=asyncio.Event, Lock=asyncio.Lock, CancelledError=asyncio.CancelledError,
        TimeoutError=asyncio.TimeoutError, to_thread=to_thread, sleep=clock.sleep, wait_for=clock.wait_for))


@pytest.fixture
def peers(tmp_path, monkeypatch):
    # The per-process challenge limiter is shared by every in-process peer test;
    # keep these authentications out of the window later tests see.
    monkeypatch.setattr(network_api, "_challenge_limiter", type(network_api._challenge_limiter)(
        limit=network_api._challenge_limiter.limit, window_seconds=network_api._challenge_limiter.window_seconds))
    a, b = Worker("cadence-a", tmp_path), Worker("cadence-b", tmp_path)
    with a.active():
        engine.create_network("Cadence")
    join(a, b)
    for worker, prefix in ((a, "a"), (b, "b")):
        with worker.active():
            for i in range(3):
                database.create_session(session_id=f"{prefix}-{i}", profile_id="default", project_id=None,
                                        title=f"{prefix} {i}")
                session_events.append_events(f"{prefix}-{i}", [
                    {"type": "message.user", "payload": {"type": "message.user", "content": f"hello {n}"}}
                    for n in range(4)])
            engine.scan_all()
    converge([a, b], rounds=4)

    clock = VirtualClock()
    na, nb = Node(a, clock), Node(b, clock)
    served_a = _Served(na, nb, "http://cadence-a.local:8000")
    served_b = _Served(nb, na, "http://cadence-b.local:8000")
    _wire(na, served_b, monkeypatch)
    _wire(nb, served_a, monkeypatch)
    nodes = {a.device_id: na, b.device_id: nb}
    urls = {a.device_id: served_a.base_url, b.device_id: served_b.base_url}
    with a.active():
        store.touch_member(b.device_id, endpoints=[served_b.base_url])
    with b.active():
        store.touch_member(a.device_id, endpoints=[served_a.base_url])

    def kick():
        # Route a wake to the loop of whichever worker's code is running.
        node = nodes.get(engine._context.device_id) if engine._context else None
        if node is not None:
            node.module._kick_from_thread()

    async def no_missing_blobs(limit=4):
        return 0  # nothing references a blob here; the real fetcher reads the database off-loop

    monkeypatch.setattr(engine, "kick", kick)
    monkeypatch.setattr(engine, "set_kick", lambda callback: None)
    monkeypatch.setattr(engine, "_own_endpoints",
                        lambda: [urls[engine._context.device_id]] if engine._context else [])
    monkeypatch.setattr(blobs, "fetch_missing", no_missing_blobs)

    async def run(seconds, during=None):
        for node in (na, nb):
            node.module._loop = asyncio.get_running_loop()
        tasks = [asyncio.create_task(loop())
                 for node in (na, nb) for loop in (node.module.sync_loop, node.module.scan_loop)]
        driver = asyncio.create_task(during()) if during is not None else None
        try:
            await clock.run_until(seconds)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if driver is not None:
            assert driver.done()
            driver.result()  # surface assertion failures from inside the run

    yield SimpleNamespace(a=a, b=b, na=na, nb=nb, clock=clock, run=run)
    a.close()
    b.close()
    engine.reset_for_tests()
    tokens.reset_for_tests()
    transport.reset_for_tests()


def _times(node, suffix, since=WARMUP):
    return [t for t, path in node.sent if path.endswith(suffix) and t >= since]


def _gaps(times):
    return {round(later - earlier, 6) for earlier, later in itertools.pairwise(times)}


async def test_quiet_connected_peers_back_off_to_the_idle_ceiling(peers):
    await peers.run(900)
    for node in (peers.na, peers.nb):
        exchanges = _times(node, "/peer/exchange", since=0)
        gaps = [round(later - earlier, 6) for earlier, later in itertools.pairwise(exchanges)]
        # Each quiet pass doubles the wait, up to the ceiling.
        assert gaps[:2] == [transport.SYNC_IDLE_SECONDS, 2 * transport.SYNC_IDLE_SECONDS]
        assert set(gaps[2:]) == {transport.SYNC_IDLE_MAX_SECONDS}
        assert exchanges[-1] >= 900 - transport.SYNC_IDLE_MAX_SECONDS
        # 15 quiet minutes: 32 exchanges where a fixed 10 s wait made 91
        # (per day per peer: 2,880 instead of 8,640).
        assert len(exchanges) == 32
        assert _times(node, "/peer/ops", since=0) == []
        # Connected peers still count as online between two idle passes.
        assert transport.SYNC_IDLE_MAX_SECONDS < transport.ONLINE_WINDOW_SECONDS / 2


async def test_remote_writes_arrive_promptly_without_echo_exchanges(peers):
    writes = []

    async def write_on_a():
        for n in range(6):
            await peers.clock.sleep(23.0)
            with peers.a.active():
                database.update_session("a-0", title=f"edit {n}")
            writes.append(peers.clock.now)
            peers.na.module.request_scan(["sessions"])  # what the post-commit hook does
            # A real change still reaches the idle peer within a scan interval.
            await peers.clock.sleep(transport.SCAN_INTERVAL_SECONDS + 0.5)
            with peers.b.active():
                assert database.get_session("a-0")["title"] == f"edit {n}"

    await peers.run(160, during=write_on_a)
    assert len(writes) == 6
    # The writer pushes each change once, in one exchange, with no follow-up pass.
    pushes = _times(peers.na, "/peer/ops", since=0)
    assert len(pushes) == 6
    for written, pushed in zip(writes, pushes):
        assert 0 <= pushed - written <= transport.SCAN_INTERVAL_SECONDS
        extra = [t for t in _times(peers.na, "/peer/exchange", since=0)
                 if pushed < t < pushed + transport.SYNC_IDLE_SECONDS - 0.5]
        assert extra == []
    # The writer was idle when each write came (its wait had grown past the
    # gap between writes), yet it pushed within a scan interval: a write wakes it.
    # The receiver keeps backing off: pushed ops do not trigger a pass back.
    assert _gaps(_times(peers.nb, "/peer/exchange")) == {transport.SYNC_IDLE_MAX_SECONDS}
    assert _times(peers.nb, "/peer/ops", since=0) == []


def test_pushed_ops_wake_the_loop_only_to_forward_to_another_peer(monkeypatch):
    kicks = []
    monkeypatch.setattr(engine, "kick", lambda: kicks.append(True))
    transport.reset_for_tests()
    try:
        transport._links["sender"] = transport.PeerLink("sender", ["http://sender.local:8000"])
        transport.forward_pushed("sender")
        assert kicks == []
        transport._links["other"] = transport.PeerLink("other", ["http://other.local:8000"])
        transport.forward_pushed("sender")
        assert kicks == [True]
    finally:
        transport.reset_for_tests()


@pytest.mark.parametrize("summary, expected", [
    ({"received": 0, "sent": 3}, transport.SYNC_IDLE_SECONDS),
    ({"received": 0, "sent": 3, "unsettled": True}, transport.SYNC_ACTIVE_SECONDS),
    ({"received": 2, "sent": 0}, transport.SYNC_ACTIVE_SECONDS),
])
async def test_follow_up_pass_only_when_work_remains(monkeypatch, summary, expected):
    timeouts = []

    async def sync_pass(client):
        return {"peer": dict(summary)}

    async def wait_for(awaitable, *, timeout):
        awaitable.close()
        timeouts.append(timeout)
        raise asyncio.CancelledError

    async def no_missing_blobs(limit=4):
        return 0

    monkeypatch.setattr(transport, "sync_pass", sync_pass)
    monkeypatch.setattr(blobs, "fetch_missing", no_missing_blobs)
    monkeypatch.setattr(transport, "asyncio", SimpleNamespace(
        Event=asyncio.Event, CancelledError=asyncio.CancelledError, TimeoutError=asyncio.TimeoutError,
        wait_for=wait_for))
    with pytest.raises(asyncio.CancelledError):
        await transport.sync_loop()
    engine.set_kick(None)
    assert timeouts == [expected]


async def test_offline_peer_backoff_is_jittered_capped_and_reset(monkeypatch):
    """A day against an offline peer: ~130 attempts where the 60 s cap made ~1,400."""
    from app.network import hub

    clock = SimpleNamespace(now=0.0)
    draws = iter(itertools.cycle([0.0, 1.0, 0.5]))
    refreshes = []
    ctx = SimpleNamespace(bootstrap_state=engine.BOOTSTRAP_READY, generation=1)

    class OfflineLink(transport.PeerLink):
        online = False
        attempts = 0

        async def sync_once(self, client, ctx):
            self.attempts += 1
            if not self.online:
                raise transport.PeerConnectFailed("connect timeout")
            self.reset_backoff()
            return {"received": 0, "sent": 0}

    async def nothing(*args, **kwargs):
        return None

    monkeypatch.setattr(transport, "time", SimpleNamespace(monotonic=lambda: clock.now, time=time.time))
    monkeypatch.setattr(transport, "random", SimpleNamespace(random=lambda: next(draws)))
    monkeypatch.setattr(transport.engine, "context", lambda: ctx)
    monkeypatch.setattr(transport, "_refresh_links", nothing)
    monkeypatch.setattr(transport, "_announce_own_endpoints_if_changed", nothing)
    monkeypatch.setattr(transport.store, "upsert_peer", lambda *a, **k: None)
    monkeypatch.setattr(transport, "note_contact", lambda device_id: None)
    monkeypatch.setattr(hub, "request_directory_refresh", lambda: refreshes.append(clock.now))
    transport.reset_for_tests()
    link = transport._links["offline"] = OfflineLink("offline", ["https://offline.example.com"])
    try:
        # Equal jitter: the wait lies between half and all of the exponential step.
        waits = []
        for failures in range(1, 12):
            low, high = transport.peer_backoff_seconds(failures), transport.peer_backoff_seconds(failures)
            waits.append((low, high))
            next(draws)  # keep the 0, 1, 0.5 cycle aligned
        assert waits[0] == (2.0, 4.0)
        assert waits[-1] == (450.0, 900.0)
        assert all(high <= transport.PEER_BACKOFF_MAX_SECONDS for _, high in waits)

        # The sync loop passes at most every SYNC_IDLE_MAX_SECONDS while idle.
        while clock.now < 86400:
            await transport.sync_pass(None)
            clock.now += transport.SYNC_IDLE_MAX_SECONDS
        assert 100 <= link.attempts <= 160
        assert refreshes == [0.0]  # one directory refresh per failure streak

        # A changed address ends the wait at once.
        assert link.backoff_until > clock.now
        link.update_endpoints(["https://moved.example.com"])
        assert link.failures == 0 and link.backoff_until == 0.0
        before = link.attempts
        await transport.sync_pass(None)
        assert link.attempts == before + 1 and link.failures == 1 and len(refreshes) == 2
        # So does a membership change.
        ctx.generation = 2
        await transport.sync_pass(None)
        assert link.attempts == before + 2
        # And a success.
        link.online = True
        clock.now = link.backoff_until
        await transport.sync_pass(None)
        assert link.failures == 0 and link.backoff_until == 0.0
        await transport.sync_pass(None)
        assert link.attempts == before + 4
    finally:
        transport.reset_for_tests()
