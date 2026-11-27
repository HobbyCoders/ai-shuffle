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


async def test_offline_peer_does_not_stop_wakes(workers):
    """The only peer is off: the outbox stays held, but due chats still wake."""
    a, b = _pair(workers)
    with a.active():
        await _patch("s1", False, _soon(1))
    converge([a, b])

    def refuse(request):
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as client:
        with a.active():
            engine.hold_outbox(True)
            try:
                store.touch_member(b.device_id, endpoints=["https://mirror.example.com"])
                link = transport._links[b.device_id] = transport.PeerLink(b.device_id, ["https://mirror.example.com"])
                with pytest.raises(transport.PeerConnectFailed):
                    await link.sync_once(client, engine.context())
                assert link.last_pass[1] == transport.PASS_OFFLINE
                assert transport.caught_up_since_start()
                # The machine sleeps after that dial: it no longer counts on resume.
                transport._gate_checked_at = time.time() - transport.RESUME_GAP_SECONDS - 1
                assert not transport.caught_up_since_start()
                assert await session_snooze.wake_due_once(now=_soon(60)) == 0
                with pytest.raises(transport.PeerConnectFailed):
                    await link.sync_once(client, engine.context())
                assert transport.caught_up_since_start()
                assert await session_snooze.wake_due_once(now=_soon(60)) == 1
                assert _snooze_of(a, "s1") == (True, None)
            finally:
                engine.hold_outbox(False)


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
