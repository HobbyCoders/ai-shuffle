"""A restored worker gets back the versions it signed after the backup (#1568).

Peers keep the ops a worker signed after its last backup. Once the restored
worker has moved to a new incarnation, an exchange serves it its own streams
from earlier incarnations, past what it holds, so it re-imports them. Its
current incarnation is never echoed back, and a worker that does not
advertise support is served exactly as before.
"""
from unittest.mock import patch

import httpx
import pytest

from app.db import network as store
from app.db.base import get_db
from app.network import engine, transport
from tests.core.network.harness import _ACTIVE, converge, owner_pref, set_owner_pref
from tests.core.network.test_ownership_revocation import _network
from tests.core.network.test_restored_worker_ordering import _restore
from tests.core.network.test_transport import ServedBy


def _restored(workers):
    """B wrote ``lost-0..4`` after its backup; A holds them; B is put back."""
    a, b = _network(workers, "a", "b")
    set_owner_pref(b, "theme", "before-backup")
    converge([a, b])
    backup = b.raw.serialize()
    for n in range(5):
        set_owner_pref(b, "theme", f"lost-{n}")
    converge([a, b])
    assert owner_pref(a, "theme") == "lost-4"
    _restore(b, backup)
    assert owner_pref(b, "theme") == "before-backup"
    return a, b


def _head(worker, origin, incarnation) -> int:
    with worker.active():
        return int(store.local_watermarks().get(origin, {}).get(str(incarnation), 0))


class _Served:
    """Record every batch ``server`` hands out through ``store.exchange_batch``."""

    def __init__(self, server):
        self.server = server
        self.ops = []
        self.snapshots = 0
        self._real = store.exchange_batch

    def __call__(self, *args, **kwargs):
        ops, heads, snapshot = self._real(*args, **kwargs)
        if _ACTIVE.get() is self.server:
            self.ops.extend(ops)
            self.snapshots += bool(snapshot)
        return ops, heads, snapshot

    def streams(self, origin):
        return sorted({(op["origin_incarnation"], op["origin_seq"]) for op in self.ops
                       if op["origin_device_id"] == origin})


async def _sync(dialer, server, client, base_url, **kwargs):
    with dialer.active():
        link = transport.PeerLink(server.device_id, [base_url])
        return await link.sync_once(client, engine.context(), **kwargs)


@pytest.mark.asyncio
async def test_restored_worker_pulls_back_what_it_signed_after_the_backup(workers):
    a, b = _restored(workers)
    lost_head = _head(a, b.device_id, 1)
    served_by_a = ServedBy(a, "https://a.example.com")
    async with httpx.AsyncClient(transport=served_by_a.transport()) as client:
        served = _Served(a)
        with patch.object(store, "exchange_batch", served):
            # Still on incarnation 1: its own stream is not sent back to it; the
            # pass ends by finding the rollback proof and moving to incarnation 2.
            await _sync(b, a, client, served_by_a.base_url, catch_up=True)
        with b.active():
            assert engine.context().identity.incarnation == 2
        assert served.streams(b.device_id) == []
        assert owner_pref(b, "theme") == "before-backup"

        set_owner_pref(b, "after", "incarnation-2")
        served = _Served(a)
        with patch.object(store, "exchange_batch", served):
            await _sync(b, a, client, served_by_a.base_url)
    assert owner_pref(b, "theme") == "lost-4"
    # Exactly the tail B lacked, all from incarnation 1.
    resent = served.streams(b.device_id)
    assert resent and {inc for inc, _ in resent} == {1}
    assert resent[-1] == (1, lost_head)
    with b.active(), get_db() as conn:
        assert store.own_watermark(conn.cursor(), b.device_id, 1) == lost_head
    assert owner_pref(a, "after") == "incarnation-2"

    # A holds B's incarnation 2 op now and still never serves it back to B.
    with a.active():
        assert _head(a, b.device_id, 2) >= 1
        ops = store.exchange_batch({}, exclude_origin=b.device_id, own_history_before=2)[0]
    mine = {op["origin_incarnation"] for op in ops if op["origin_device_id"] == b.device_id}
    assert mine == {1}
    # Caught up: the next exchange carries nothing of B's.
    with b.active():
        marks = store.local_watermarks()
    with a.active():
        ops, _, snapshot = store.exchange_batch(marks, exclude_origin=b.device_id, own_history_before=2)
    assert not snapshot and not [op for op in ops if op["origin_device_id"] == b.device_id]
    converge([a, b])
    assert owner_pref(a, "theme") == owner_pref(b, "theme") == "lost-4"


@pytest.mark.asyncio
async def test_peer_pushes_a_restored_worker_its_earlier_incarnation(workers):
    """The other direction: A dials B and pushes B's lost tail to it."""
    a, b = _restored(workers)
    served_by_a = ServedBy(a, "https://a.example.com")
    served_by_b = ServedBy(b, "https://b.example.com")
    async with httpx.AsyncClient(transport=served_by_a.transport()) as client:
        await _sync(b, a, client, served_by_a.base_url, catch_up=True)
    with b.active():
        assert engine.context().identity.incarnation == 2
    assert owner_pref(b, "theme") == "before-backup"
    async with httpx.AsyncClient(transport=served_by_b.transport()) as client:
        await _sync(a, b, client, served_by_b.base_url)
    assert owner_pref(b, "theme") == "lost-4"


@pytest.mark.asyncio
async def test_worker_without_support_is_served_as_before(workers, monkeypatch):
    """An older worker does not advertise support: none of its streams come back."""
    a, b = _restored(workers)
    capabilities = transport._capabilities

    def older(*args, **kwargs):
        caps = dict(capabilities(*args, **kwargs))
        caps.pop(transport.OWN_HISTORY_CAPABILITY, None)
        return caps
    served_by_a = ServedBy(a, "https://a.example.com")
    async with httpx.AsyncClient(transport=served_by_a.transport()) as client:
        with b.active():
            monkeypatch.setattr(transport, "_capabilities", older)
        await _sync(b, a, client, served_by_a.base_url, catch_up=True)
        served = _Served(a)
        with patch.object(store, "exchange_batch", served):
            await _sync(b, a, client, served_by_a.base_url)
    assert served.streams(b.device_id) == []
    assert owner_pref(b, "theme") == "before-backup"


@pytest.mark.asyncio
async def test_pruned_gap_in_the_earlier_incarnation_takes_one_state_copy(workers):
    """A hole in what A kept of B's old stream: a state copy, then ordinary exchange."""
    a, b = _restored(workers)
    with b.active(), get_db() as conn:
        held = store.own_watermark(conn.cursor(), b.device_id, 1)
    # A pruned one superseded op B lacks (not the stream's head).
    a.sql("DELETE FROM sync_ops WHERE origin_device_id = ? AND incarnation = 1 AND seq = ?",
          (b.device_id, held + 1))
    served_by_a = ServedBy(a, "https://a.example.com")
    async with httpx.AsyncClient(transport=served_by_a.transport()) as client:
        await _sync(b, a, client, served_by_a.base_url, catch_up=True)
        summary = await _sync(b, a, client, served_by_a.base_url)
        assert summary.get("snapshot_required")
        assert owner_pref(b, "theme") == "lost-4"
        # The copy's frontier covers B's old stream: no copy on the next pass.
        summary = await _sync(b, a, client, served_by_a.base_url)
    assert not summary.get("snapshot_required")
    assert _head(b, b.device_id, 1) == _head(a, b.device_id, 1)


def _detect_restore(a, b, incarnation):
    """Hand B one op it signed past its outbox (the proof any peer can relay)."""
    with b.active(), get_db() as conn:
        held = store.own_watermark(conn.cursor(), b.device_id, incarnation)
    with a.active(), get_db() as conn:
        proof = store.get_op(conn.cursor(), b.device_id, incarnation, held + 1)
    with b.active():
        assert engine.apply_batch([proof]).own_ahead
        assert engine.context().identity.incarnation == incarnation + 1


def test_repeated_restores_reimport_every_earlier_incarnation_in_any_order(workers):
    a, b = _network(workers, "a", "b")
    set_owner_pref(b, "theme", "v0")
    converge([a, b])
    first = b.raw.serialize()
    set_owner_pref(b, "theme", "lost-1")
    set_owner_pref(b, "one", "incarnation-1")
    converge([a, b])
    _restore(b, first)
    _detect_restore(a, b, 1)
    second = b.raw.serialize()
    set_owner_pref(b, "theme", "lost-2")
    set_owner_pref(b, "two", "incarnation-2")
    converge([a, b])
    _restore(b, second)
    _detect_restore(a, b, 2)
    with b.active():
        marks = store.local_watermarks()
    with a.active():
        ops, _, snapshot = store.exchange_batch(marks, exclude_origin=b.device_id, own_history_before=3)
    mine = [op for op in ops if op["origin_device_id"] == b.device_id]
    assert not snapshot and {op["origin_incarnation"] for op in mine} == {1, 2}
    # The newer stream first: the older one is still history, not a stale stream.
    with b.active():
        for incarnation in (2, 1):
            result = engine.apply_batch([op for op in mine if op["origin_incarnation"] == incarnation])
            assert not result.rejected and not result.retry and not result.pending
    assert (owner_pref(b, "theme"), owner_pref(b, "one"), owner_pref(b, "two")) == \
        ("lost-2", "incarnation-1", "incarnation-2")
    for incarnation in (1, 2):
        assert _head(b, b.device_id, incarnation) == _head(a, b.device_id, incarnation)


@pytest.mark.asyncio
async def test_push_skips_an_earlier_incarnation_with_a_hole(workers):
    """Only the restored worker's own pull can ask for a state copy: keep pushing the rest."""
    a, b = _restored(workers)
    served_by_a = ServedBy(a, "https://a.example.com")
    served_by_b = ServedBy(b, "https://b.example.com")
    async with httpx.AsyncClient(transport=served_by_a.transport()) as client:
        await _sync(b, a, client, served_by_a.base_url, catch_up=True)
    with b.active(), get_db() as conn:
        held = store.own_watermark(conn.cursor(), b.device_id, 1)
    a.sql("DELETE FROM sync_ops WHERE origin_device_id = ? AND incarnation = 1 AND seq = ?",
          (b.device_id, held + 1))
    set_owner_pref(a, "from-a", "pushed")
    async with httpx.AsyncClient(transport=served_by_b.transport()) as client:
        await _sync(a, b, client, served_by_b.base_url)
    assert owner_pref(b, "from-a") == "pushed"
    assert owner_pref(b, "theme") == "before-backup"  # left for B's own pull and state copy
