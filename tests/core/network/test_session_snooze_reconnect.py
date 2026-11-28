"""A snooze reaches a worker that comes (back) online."""
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from app.core import session_snooze
from app.db import database, network as store
from app.network import engine, transport
from tests.core.network.harness import converge, exchange, join, set_owner_pref
from tests.core.network.test_control import _new_session
from tests.core.network.test_session_snooze_replication import _patch, _snooze_of
from tests.core.network.test_transport import ServedBy


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from app.core.sync_engine import sync_engine
    monkeypatch.setattr(sync_engine, "broadcast_user_session_updated", AsyncMock())


def _soon(minutes=30):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def _next_wall_tick():
    """Wait until time.time() moves on. On Windows it steps every 15.6 ms, so a
    pass and a resume noticed right after it can otherwise share one timestamp."""
    start = time.time()
    while time.time() == start:
        time.sleep(0.001)


def _pair(workers):
    a, b = workers("owner"), workers("mirror")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _new_session(a, "s1", "Snoozable")
    converge([a, b])
    return a, b


@pytest.mark.parametrize("where", ["owner", "mirror"])
async def test_snooze_made_while_outbox_held_reaches_peer(workers, where):
    a, b = _pair(workers)
    w, other = (a, b) if where == "owner" else (b, a)
    when = _soon()
    with w.active():
        engine.hold_outbox(True)
        try:
            await _patch("s1", False, when)
            assert _snooze_of(w, "s1") == (False, session_snooze.canonical(when))
        finally:
            engine.hold_outbox(False)
        engine.load_context()
        engine.scan_all()
    converge([a, b])
    for worker in (a, b):
        assert _snooze_of(worker, "s1") == (False, session_snooze.canonical(when)), worker.name


@pytest.mark.parametrize("where", ["owner", "mirror"])
async def test_held_snooze_survives_peer_ops_before_scan(workers, where):
    a, b = _pair(workers)
    w, other = (a, b) if where == "owner" else (b, a)
    with other.active():
        await _patch("s1", False)  # an older plain settle on the peer
    converge([a, b])
    when = _soon()
    with w.active():
        engine.hold_outbox(True)
        try:
            await _patch("s1", False, when)
        finally:
            engine.hold_outbox(False)
    with other.active():
        await _patch("s1", True)  # the reconnecting peer's own earlier-unseen edit
    result = exchange(other, w)
    assert not result.rejected and not result.retry
    converge([a, b])
    states = {_snooze_of(worker, "s1") for worker in (a, b)}
    assert len(states) == 1, states


@pytest.mark.parametrize("snoozer", ["owner", "mirror"])
async def test_offline_worker_catches_up_snooze(workers, snoozer):
    a, b = _pair(workers)
    w, offline = (a, b) if snoozer == "owner" else (b, a)
    when = _soon()
    with w.active():
        store.touch_member(offline.device_id, endpoints=[])
        await _patch("s1", False, when)
    converge([a, b])
    assert _snooze_of(offline, "s1") == (False, session_snooze.canonical(when))
    with w.active():
        await _patch("s1", False, _soon(60))
        later = session_snooze.canonical(database.get_session("s1")["snoozed_until"])
    converge([a, b])
    assert _snooze_of(offline, "s1") == (False, later)


@pytest.mark.parametrize("returning", ["owner", "mirror"])
@pytest.mark.parametrize("newer", ["resnooze", "settle", "wake_then_settle"])
async def test_returning_worker_does_not_replay_a_stale_wake(workers, returning, newer):
    """A worker back after hours offline woke snoozed chats from its stale copy
    at boot, before it had synced; the wake was then signed over the newer
    edits its peer made while it was away."""
    a, b = _pair(workers)
    first = _soon(1)
    with a.active():
        await _patch("s1", False, first)
    converge([a, b])
    away, stayed = (a, b) if returning == "owner" else (b, a)
    # ``away`` goes offline; on ``stayed`` the owner changes the chat after its
    # first wake time.
    with stayed.active():
        store.touch_member(away.device_id, endpoints=[])
        if newer == "resnooze":
            await _patch("s1", False, _soon(240))
        elif newer == "settle":
            await _patch("s1", False)
        else:
            await _patch("s1", True)
            await _patch("s1", False)
        expected = _snooze_of(stayed, "s1")
    # ``away`` comes back: its outbox is held until it has compared streams,
    # and its wake loop runs at once, on the copy it had before it left.
    late = _soon(60)
    with away.active():
        store.touch_member(stayed.device_id, endpoints=["https://peer.example.com"])
        engine.hold_outbox(True)
        try:
            await session_snooze.wake_due_once(now=late)
            result = exchange(stayed, away)
            assert not result.rejected
        finally:
            # The transport's order: the comparison opens the outbox, then the
            # same batch is applied again (``PeerLink._sync_once``).
            engine.hold_outbox(False)
    result = exchange(stayed, away)
    assert not result.rejected and not result.retry
    converge([a, b])
    with away.active():
        await session_snooze.wake_due_once(now=late)
    converge([a, b])
    for worker in (a, b):
        assert _snooze_of(worker, "s1") == expected, worker.name


async def test_wake_waits_for_a_full_pull(workers, monkeypatch):
    """A pass the time budget cut short does not count; a full one does."""
    a, b = _pair(workers)
    with a.active():
        await _patch("s1", False, _soon(1))
    converge([a, b])
    for index in range(3):
        set_owner_pref(a, f"backlog-{index}", index)
    with a.active():
        await _patch("s1", False, _soon(240))  # the newest op: a short pull misses it
        expected = _snooze_of(a, "s1")
    served = ServedBy(a, "https://owner.example.com")
    monkeypatch.setattr(transport, "BATCH_LIMIT", 1)
    monkeypatch.setattr(transport, "SYNC_PULL_SECONDS", 0)
    late = _soon(60)
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            store.touch_member(a.device_id, endpoints=[served.base_url])
            link = transport._links[a.device_id] = transport.PeerLink(a.device_id, [served.base_url])
            assert not transport.caught_up_since_start()  # no pass yet
            await link.sync_once(client, engine.context())
            assert link.last_pass[1] == transport.PASS_BEHIND
            assert not transport.caught_up_since_start()
            assert await session_snooze.wake_due_once(now=late) == 0
            await link.sync_once(client, engine.context(), catch_up=True)
            assert link.last_pass[1] == transport.PASS_CURRENT
            assert transport.caught_up_since_start()
            assert await session_snooze.wake_due_once(now=late) == 0  # re-snoozed: not due
            assert _snooze_of(b, "s1") == expected
            # A pass from before this machine slept does not count.
            link.last_pass = (time.time() - transport.PASS_FRESH_SECONDS[transport.PASS_CURRENT] - 1,
                              transport.PASS_CURRENT)
            assert not transport.caught_up_since_start()


async def test_failed_push_keeps_a_short_pull_behind(workers, monkeypatch):
    a, b = _pair(workers)
    with a.active():
        await _patch("s1", False, _soon(1))
    converge([a, b])
    for index in range(3):
        set_owner_pref(a, f"backlog-{index}", index)
    set_owner_pref(b, "to-push", 1)
    served = ServedBy(a, "https://owner.example.com")
    answer = served.handler
    pushes = []

    def refuse_push(request):
        if request.url.path == "/api/v1/network/peer/ops":
            pushes.append(request)
            return httpx.Response(413, json={"detail": "too large"})
        return answer(request)

    monkeypatch.setattr(transport, "BATCH_LIMIT", 1)
    monkeypatch.setattr(transport, "SYNC_PULL_SECONDS", 0)
    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse_push)) as client:
        with b.active():
            store.touch_member(a.device_id, endpoints=[served.base_url])
            link = transport._links[a.device_id] = transport.PeerLink(a.device_id, [served.base_url])
            with pytest.raises((transport.PeerError, httpx.HTTPError)):
                await link.sync_once(client, engine.context())
            assert pushes  # the pull ended short, then the push was refused
            assert link.last_pass[1] == transport.PASS_BEHIND
            assert not transport.caught_up_since_start()


async def test_connection_lost_between_batches_stays_behind(workers, monkeypatch):
    """The peer answered a full batch, then the connection dropped: not offline."""
    a, b = _pair(workers)
    with a.active():
        await _patch("s1", False, _soon(1))
    converge([a, b])
    for index in range(3):
        set_owner_pref(a, f"backlog-{index}", index)
    served = ServedBy(a, "https://owner.example.com")
    answer = served.handler
    exchanges = []

    def drop_after_first_exchange(request):
        if exchanges:
            raise httpx.ConnectError("connection refused", request=request)
        if request.url.path == "/api/v1/network/peer/exchange":
            exchanges.append(request)
        return answer(request)

    monkeypatch.setattr(transport, "BATCH_LIMIT", 1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(drop_after_first_exchange)) as client:
        with b.active():
            store.touch_member(a.device_id, endpoints=[served.base_url])
            link = transport._links[a.device_id] = transport.PeerLink(a.device_id, [served.base_url])
            with pytest.raises((transport.PeerError, httpx.HTTPError)):
                await link.sync_once(client, engine.context())
            assert len(exchanges) == 1
            assert link.last_pass[1] == transport.PASS_BEHIND
            assert not transport.caught_up_since_start()


def _refused(request):
    raise httpx.ConnectError("connection refused", request=request)


def _tunnel_worker_down(request):
    return httpx.Response(transport.TUNNEL_WORKER_DOWN_STATUS, text="error code: 1033")


@pytest.mark.parametrize("dial", [_refused, _tunnel_worker_down], ids=["refused", "tunnel-worker-down"])
@pytest.mark.parametrize("sleep", ["long", "short"])
async def test_offline_peer_does_not_stop_wakes(workers, dial, sleep):
    """The only peer is off: the outbox stays held, but due chats still wake."""
    a, b = _pair(workers)
    with a.active():
        await _patch("s1", False, _soon(1))
    converge([a, b])

    async with httpx.AsyncClient(transport=httpx.MockTransport(dial)) as client:
        with a.active():
            engine.hold_outbox(True)
            try:
                store.touch_member(b.device_id, endpoints=["https://mirror.example.com"])
                link = transport._links[b.device_id] = transport.PeerLink(b.device_id, ["https://mirror.example.com"])
                with pytest.raises(transport.PeerError):
                    await link.sync_once(client, engine.context())
                assert link.last_pass[1] == transport.PASS_OFFLINE
                assert transport.caught_up_since_start()
                # The machine sleeps after that dial: it no longer counts on resume.
                # A long sleep shows as a gap between sweeps; a short one as wall
                # time running ahead of monotonic time (which a suspend stops).
                _next_wall_tick()
                if sleep == "long":
                    transport._gate_checked_at = (time.time() - transport.RESUME_GAP_SECONDS - 1,
                                                  transport._awake_seconds())
                else:
                    transport._gate_checked_at = (time.time() - 20, transport._awake_seconds() - 1)
                assert not transport.caught_up_since_start()
                assert await session_snooze.wake_due_once(now=_soon(60)) == 0
                with pytest.raises(transport.PeerError):
                    await link.sync_once(client, engine.context())
                assert transport.caught_up_since_start()
                assert await session_snooze.wake_due_once(now=_soon(60)) == 1
                assert _snooze_of(a, "s1") == (True, None)
            finally:
                engine.hold_outbox(False)


async def test_worker_down_on_the_cached_route_tries_the_others(workers):
    """A 530 on the address a cached token uses is not proof the peer is off:
    another address it advertises may carry its newer edits."""
    a, b = _pair(workers)
    with a.active():
        await _patch("s1", False, _soon(1))
    converge([a, b])
    with a.active():
        await _patch("s1", False, _soon(240))
        expected = _snooze_of(a, "s1")
    tunnel = "https://tunnel.example.com"
    alias = ServedBy(a, "https://alias.example.com")

    def route(request):
        if str(request.url).startswith(tunnel):
            return httpx.Response(transport.TUNNEL_WORKER_DOWN_STATUS, text="error code: 1033")
        return alias.handler(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(route)) as client:
        with b.active():
            store.touch_member(a.device_id, endpoints=[tunnel, alias.base_url])
            link = transport._links[a.device_id] = transport.PeerLink(a.device_id, [tunnel, alias.base_url])
            link.base_url, link.token, link.token_expires = tunnel, "cached", time.time() + 3600
            with pytest.raises(transport.PeerError):
                await link.sync_once(client, engine.context())
            assert link.last_pass[1] == transport.PASS_BEHIND
            assert link.token is None and link.base_url is None
            assert not transport.caught_up_since_start()
            assert await session_snooze.wake_due_once(now=_soon(60)) == 0
            await link.sync_once(client, engine.context())
            assert link.base_url == alias.base_url
            assert link.last_pass[1] == transport.PASS_CURRENT
            assert _snooze_of(b, "s1") == expected


async def test_pass_spanning_a_sleep_does_not_count(workers):
    """A pass started before the machine slept is dated before the resume."""
    a, b = _pair(workers)
    served = ServedBy(a, "https://owner.example.com")
    slept = []

    def sleep_during_exchange(request):
        response = served.handler(request)
        if request.url.path == "/api/v1/network/peer/exchange" and not slept:
            # The machine slept while the answer was on its way; the sweep
            # notices on resume, before this pass records its outcome.
            slept.append(True)
            with b.active():
                _next_wall_tick()
                transport._gate_checked_at = (time.time() - 20, transport._awake_seconds() - 1)
                assert not transport.caught_up_since_start()
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(sleep_during_exchange)) as client:
        with b.active():
            store.touch_member(a.device_id, endpoints=[served.base_url])
            link = transport._links[a.device_id] = transport.PeerLink(a.device_id, [served.base_url])
            await link.sync_once(client, engine.context())
            assert slept and link.last_pass[1] == transport.PASS_CURRENT
            assert not transport.caught_up_since_start()
            _next_wall_tick()
            await link.sync_once(client, engine.context())
            assert transport.caught_up_since_start()


async def test_resume_ends_the_lan_reprobe_wait_and_backoff(workers, monkeypatch):
    """A LAN that failed before the sleep (another network) is probed again,
    and a backoff from before the sleep is dropped, so the gate reopens on the
    next pass instead of after the backoff."""
    kicks = []
    monkeypatch.setattr(transport, "_kick_from_thread", lambda: kicks.append(1))
    a, b = _pair(workers)
    with a.active():
        store.touch_member(b.device_id, endpoints=["https://mirror.example.com"])
        link = transport._links[b.device_id] = transport.PeerLink(b.device_id, ["https://mirror.example.com"])
        link.lan_endpoints = ["http://192.168.1.20:8000"]
        link._lan_failed(OSError("no route"))
        for _ in range(4):
            link.note_failure()
        assert link.lan_retry_at > time.monotonic()
        assert link.backoff_until > time.monotonic()
        transport.caught_up_since_start()
        # No sleep: the waits stand.
        assert link.lan_retry_at > time.monotonic() and link.backoff_until > time.monotonic()
        assert kicks == []
        transport._gate_checked_at = (time.time() - 20, transport._awake_seconds() - 1)
        transport.caught_up_since_start()
        assert link.lan_retry_at == 0.0
        assert link.backoff_until == 0.0 and link.failures == 0
        assert kicks == [1]


def test_superseded_incarnation_does_not_hold_the_gate():
    """An origin restored from a backup moves to a new incarnation; the old
    one is never applied again, so a peer still advertising it does not leave
    us behind forever."""
    current = {"dev-c": {"2": 7}}
    assert transport._reached(current, {"dev-c": {"1": 50, "2": 7}})
    assert not transport._reached(current, {"dev-c": {"1": 50, "2": 8}})
    # Without a newer incarnation, the old stream still has to be pulled.
    assert not transport._reached({"dev-c": {"1": 40}}, {"dev-c": {"1": 50}})
    assert not transport._reached({}, {"dev-c": {"1": 1}})
    assert transport._reached({"dev-c": {"1": 50}}, {"dev-c": {"1": 50}})


def test_awake_clock_is_monotonic_off_windows(monkeypatch):
    monkeypatch.setattr(transport, "_windows_awake", None)
    before = transport._awake_seconds()
    assert abs(before - time.monotonic()) < 1
    monkeypatch.setattr(transport, "_windows_awake", lambda: 12.5)
    assert transport._awake_seconds() == 12.5


async def test_peer_answering_with_an_error_is_not_offline(workers):
    """Only a tunnel's worker-down answer counts as offline, not any HTTP error."""
    a, b = _pair(workers)

    def bad_gateway(request):
        return httpx.Response(502, text="bad gateway")

    async with httpx.AsyncClient(transport=httpx.MockTransport(bad_gateway)) as client:
        with a.active():
            store.touch_member(b.device_id, endpoints=["https://mirror.example.com"])
            link = transport._links[b.device_id] = transport.PeerLink(b.device_id, ["https://mirror.example.com"])
            with pytest.raises(transport.PeerError):
                await link.sync_once(client, engine.context())
            assert link.last_pass[1] == transport.PASS_BEHIND
            assert not transport.caught_up_since_start()


async def test_new_worker_joining_sees_snooze(workers):
    a, b = _pair(workers)
    when = _soon()
    with b.active():
        await _patch("s1", False, when)
    converge([a, b])
    c = workers("third")
    join(a, c)
    converge([a, b, c])
    assert _snooze_of(c, "s1") == (False, session_snooze.canonical(when))


async def test_owner_catalog_update_after_snooze_keeps_it_on_mirror(workers):
    a, b = _pair(workers)
    when = _soon()
    with b.active():
        await _patch("s1", False, when)
    converge([a, b])
    with a.active():
        database.update_session("s1", title="Renamed while snoozed")
        engine.scan_all()
    converge([a, b])
    for worker in (a, b):
        assert _snooze_of(worker, "s1") == (False, session_snooze.canonical(when)), worker.name
