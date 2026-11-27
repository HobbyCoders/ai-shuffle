"""A snooze reaches a worker that comes (back) online."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.core import session_snooze
from app.db import database, network as store
from app.network import engine, transport
from tests.core.network.harness import converge, exchange, join
from tests.core.network.test_control import _new_session
from tests.core.network.test_session_snooze_replication import _patch, _snooze_of


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


async def test_wake_waits_for_sync_then_fires(workers):
    a, b = _pair(workers)
    with a.active():
        await _patch("s1", False, _soon(1))
    converge([a, b])
    late = _soon(60)
    with a.active():
        engine.hold_outbox(True)
        try:
            assert not transport.caught_up_since_start()
            assert await session_snooze.wake_due_once(now=late) == 0
        finally:
            engine.hold_outbox(False)
        transport._tried_since_hold.add(b.device_id)  # our pass tried the peer
        assert transport.caught_up_since_start()
        assert await session_snooze.wake_due_once(now=late) == 1
    converge([a, b])
    for worker in (a, b):
        assert _snooze_of(worker, "s1") == (True, None), worker.name


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
