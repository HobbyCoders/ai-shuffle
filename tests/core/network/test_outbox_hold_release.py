"""#1621: the startup outbox hold must not outlast a peer that can only reach us.

A worker holds its outbox at startup until its stream has been compared with
a peer, so a worker restored from a backup moves to a new incarnation before
it reuses sequence numbers. The comparison used to happen only on exchanges
the worker started itself. With one-way reachability (the peer reaches us,
we cannot dial it) the hold never lifted, and every settle a peer asked of
this worker's sessions was refused for as long as that lasted.
"""
import asyncio

import httpx
import pytest

from app.db import database
from app.db import network as store
from app.network import engine, session_active, transport
from tests.core.network.harness import converge, exchange, owner_pref, set_owner_pref
from tests.core.network.test_control import _new_session
from tests.core.network.test_ownership_revocation import Network, _network
from tests.core.network.test_peer_plane_errors import _pref_ops
from tests.core.network.test_session_active_control import connect_owner, settle


@pytest.fixture(autouse=True)
def _release_hold():
    yield
    engine.hold_outbox(False)


async def _dial(caller, callee_device_id, url, *, catch_up=False):
    """``caller`` runs one exchange with the peer at ``url`` (an inbound exchange for that peer)."""
    with caller.active():
        link = transport.PeerLink(callee_device_id, [url])
        async with httpx.AsyncClient() as client:
            return await link.sync_once(client, engine.context(), catch_up=catch_up)


async def test_inbound_exchange_opens_the_outbox_with_one_way_reachability(workers, monkeypatch):
    a, b = _network(workers, "a", "b")
    net = Network(monkeypatch, b=b)  # A reaches B; B cannot reach A
    with b.active():
        store.touch_member(a.device_id, endpoints=["http://nowhere.local:9"])
        engine.hold_outbox(True)  # B restarted
    set_owner_pref(b, "while-held", 1)
    assert _pref_ops(b, "while-held") == []
    with b.active():
        async with httpx.AsyncClient() as client:
            assert await transport.sync_pass(client) == {}
        assert engine.outbox_held() is True

    await _dial(a, b.device_id, net.url("b"))

    assert engine.outbox_held() is False
    with b.active():
        engine.scan_all()
    assert len(_pref_ops(b, "while-held")) == 1
    await _dial(a, b.device_id, net.url("b"))
    assert owner_pref(a, "while-held") == 1


async def test_peer_startup_catch_up_gets_writes_made_during_the_hold(workers, monkeypatch):
    """A peer's startup barrier asks us to flush before it reads: the hold
    opens first, so the writes made while it was on are in that read."""
    a, b = _network(workers, "a", "b")
    net = Network(monkeypatch, b=b)
    with b.active():
        engine.hold_outbox(True)
    set_owner_pref(b, "held-then-flushed", 3)

    await _dial(a, b.device_id, net.url("b"), catch_up=True)

    assert engine.outbox_held() is False
    assert owner_pref(a, "held-then-flushed") == 3


async def test_inbound_exchange_keeps_the_hold_for_a_worker_restored_from_backup(workers, monkeypatch):
    """The original purpose: a peer that holds more of our stream than we do
    keeps the outbox held until an exchange we start fetches the signed proof
    and moves us to a new incarnation."""
    a, b = _network(workers, "a", "b")
    set_owner_pref(b, "before-backup", 1)
    exchange(b, a)
    with b.active():
        b.sql("DELETE FROM sync_ops WHERE origin_device_id = ?", (b.device_id,))  # restored from backup
        store.touch_member(a.device_id, endpoints=["http://nowhere.local:9"])
        engine.hold_outbox(True)
    net = Network(monkeypatch, a=a, b=b)

    await _dial(a, b.device_id, net.url("b"))

    assert engine.outbox_held() is True
    set_owner_pref(b, "after-restore", 2)
    assert _pref_ops(b, "after-restore") == []
    with b.active():
        assert engine.context().identity.incarnation == 1
        # B can reach A again: its own exchange proves the rollback first.
        store.touch_member(a.device_id, endpoints=[net.url("a")])
        transport._links.clear()
        async with httpx.AsyncClient() as client:
            await transport.sync_pass(client)
        assert engine.context().identity.incarnation == 2
        assert engine.outbox_held() is False
        engine.scan_all()
        [op] = _pref_ops(b, "after-restore")
        assert op["origin_incarnation"] == 2


def test_peer_view_that_cannot_prove_safety_keeps_the_hold(workers):
    a, b = _network(workers, "a", "b")
    set_owner_pref(b, "one", 1)
    with b.active():
        held = store.own_watermark(store.get_db().__enter__().cursor(), b.device_id, 1)
        engine.hold_outbox(True)
        for view in ({"2": 1},            # a newer incarnation of ours exists
                     {"1": held + 1},     # more of our stream than we hold
                     {"x": 1},            # malformed
                     None):
            assert transport.release_hold_for_peer_view(a.device_id, view) is False
            assert engine.outbox_held() is True
        # Another peer's exchange is in flight: that comparison decides.
        other = transport.PeerLink("device-c", ["http://c.local:8000"])
        transport._links["device-c"] = other

        async def compare_elsewhere():
            async with other._sync_lock:
                assert transport.release_hold_for_peer_view(a.device_id, {"1": held}) is False
        asyncio.run(compare_elsewhere())
        assert engine.outbox_held() is True
        # Older incarnations and our own head are fine.
        assert transport.release_hold_for_peer_view(a.device_id, {"0": 99, "1": held}) is True
        assert engine.outbox_held() is False


async def test_peer_settle_during_startup_hold_is_signed_and_reaches_the_mirror(workers, monkeypatch):
    """The startup case: B settles a session A owns while A's outbox is still
    held after a restart. The request carries B's view of A's stream, so A
    compares streams, signs the change and B's mirror updates at once."""
    a, b = _network(workers, "a", "b")
    served = connect_owner(monkeypatch, a)
    _new_session(a, "startup-settle", "On owner")
    converge([a, b])
    with a.active():
        engine.hold_outbox(True)
    with b.active():
        store.touch_member(a.device_id, endpoints=[served.base_url])
        result = await settle("startup-settle", False)
        assert bool(result["is_active"]) is False
        assert bool(database.get_session("startup-settle")["is_active"]) is False
    assert engine.outbox_held() is False
    with a.active():
        assert bool(database.get_session("startup-settle")["is_active"]) is False


async def test_peer_settle_without_a_stream_view_is_still_refused_during_the_hold(workers):
    """A peer from before #1621 sends no view: nothing proves the hold safe to
    lift, so the request is refused and nothing is committed."""
    a, b = _network(workers, "a", "b")
    _new_session(a, "old-peer", "On owner")
    converge([a, b])
    with a.active():
        engine.hold_outbox(True)
        with pytest.raises(PermissionError, match="still syncing"):
            await session_active.handle({"session_id": "old-peer", "is_active": False}, b.device_id)
        # A view that claims more of A's stream than A holds is refused too.
        with pytest.raises(PermissionError, match="still syncing"):
            await session_active.handle({"session_id": "old-peer", "is_active": False,
                                         "owner_watermarks": {"1": 10 ** 6}}, b.device_id)
        assert engine.outbox_held() is True
        assert bool(database.get_session("old-peer")["is_active"]) is True


async def test_inbound_view_waits_for_peers_we_have_not_compared_with(workers, monkeypatch):
    """A restored worker with several peers: one that dials in holding none of
    our stream past our log must not open the hold before our own pass has
    compared with the peers we can reach. Here C holds more of B's stream
    than B's restored log; A shows nothing past it (offline since before the
    backup, say)."""
    a, b, c = _network(workers, "a", "b", "c")
    set_owner_pref(b, "before-backup", 1)
    converge([a, b, c])
    net = Network(monkeypatch, b=b, c=c)
    with b.active():
        b.sql("DELETE FROM sync_ops WHERE origin_device_id = ?", (b.device_id,))  # restored from backup
        store.touch_member(a.device_id, endpoints=["http://nowhere.local:9"])
        store.touch_member(c.device_id, endpoints=[net.url("c")])
        transport._links.clear()
        engine.hold_outbox(True)  # B restarted; its first pass has not reached C yet

        assert transport.release_hold_for_peer_view(a.device_id, {}) is False
        assert engine.outbox_held() is True
        set_owner_pref(b, "after-restore", 2)
        assert _pref_ops(b, "after-restore") == []

        # B's own pass reaches C, which proves the rollback.
        async with httpx.AsyncClient() as client:
            await transport.sync_pass(client)
        assert engine.context().identity.incarnation == 2
        assert engine.outbox_held() is False
        engine.scan_all()
        [op] = _pref_ops(b, "after-restore")
        assert op["origin_incarnation"] == 2


def test_inbound_view_opens_the_hold_once_other_peers_were_tried(workers, monkeypatch):
    """Peers our pass tried and could not reach do not keep the hold: the
    one-way reachability case with more than one peer."""
    a, b, c = _network(workers, "a", "b", "c")
    Network(monkeypatch, b=b)  # nobody but B answers
    with b.active():
        store.touch_member(a.device_id, endpoints=["http://a.local:8000"])
        store.touch_member(c.device_id, endpoints=["http://c.local:8000"])
        transport._links.clear()
        engine.hold_outbox(True)
        assert transport.release_hold_for_peer_view(a.device_id, {}) is False

        async def unreachable_pass():
            async with httpx.AsyncClient() as client:
                assert await transport.sync_pass(client) == {}
        asyncio.run(unreachable_pass())
        assert engine.outbox_held() is True

        assert transport.release_hold_for_peer_view(a.device_id, {}) is True
        assert engine.outbox_held() is False
