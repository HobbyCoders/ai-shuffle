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

from app.db import database
from app.db import network as store
from app.db import session_events as events_db
from app.db.base import get_db
from app.db.sessions import set_session_run_binding
from app.network import catalog, engine, transport
from tests.core.network.harness import _ACTIVE, converge, owner_pref, set_owner_pref
from tests.core.network.test_ownership_revocation import _network, _session
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
async def test_compacted_gap_stops_the_replay_without_a_state_copy(workers):
    """Only the unbroken prefix comes back; nothing past a hole is claimed received."""
    a, b = _restored(workers)
    with b.active(), get_db() as conn:
        held = store.own_watermark(conn.cursor(), b.device_id, 1)
    # A compacted one superseded op in the middle of what B lacks.
    a.sql("DELETE FROM sync_ops WHERE origin_device_id = ? AND incarnation = 1 AND seq = ?",
          (b.device_id, held + 3))
    served_by_a = ServedBy(a, "https://a.example.com")
    async with httpx.AsyncClient(transport=served_by_a.transport()) as client:
        await _sync(b, a, client, served_by_a.base_url, catch_up=True)
        for _ in range(2):
            served = _Served(a)
            with patch.object(store, "exchange_batch", served):
                summary = await _sync(b, a, client, served_by_a.base_url)
            assert not summary.get("snapshot_required") and not served.snapshots
    assert owner_pref(b, "theme") == "lost-1"
    assert _head(b, b.device_id, 1) == held + 2
    with b.active(), get_db() as conn:
        assert store.count_pending(conn.cursor(), b.device_id, 1) == 0  # nothing past the hole sits unapplied


def _detect_restore(a, b, incarnation):
    """Hand B one op it signed past its outbox (the proof any peer can relay)."""
    with b.active(), get_db() as conn:
        held = store.own_watermark(conn.cursor(), b.device_id, incarnation)
    with a.active(), get_db() as conn:
        proof = store.get_op(conn.cursor(), b.device_id, incarnation, held + 1)
    with b.active():
        assert engine.apply_batch([proof]).own_ahead
        assert engine.context().identity.incarnation == incarnation + 1


def _rename(worker, title):
    with worker.active():
        database.update_session("chat", title=title)
        engine.scan_all()


def test_repeated_restores_reimport_every_earlier_incarnation_in_any_order(workers):
    a, b = _network(workers, "a", "b")
    set_owner_pref(b, "theme", "v0")
    _session(b, "chat", title="v0", events=0)
    converge([a, b])
    first = b.raw.serialize()
    set_owner_pref(b, "theme", "lost-1")
    set_owner_pref(b, "one", "incarnation-1")
    _rename(b, "lost-1")
    converge([a, b])
    _restore(b, first)
    _detect_restore(a, b, 1)
    second = b.raw.serialize()
    set_owner_pref(b, "theme", "lost-2")
    set_owner_pref(b, "two", "incarnation-2")
    _rename(b, "lost-2")
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
    # The later incarnation is newer, for a shared setting and the chat it runs alike.
    assert (owner_pref(b, "theme"), owner_pref(b, "one"), owner_pref(b, "two"), _chat(b)[0]) == \
        ("lost-2", "incarnation-1", "incarnation-2", "lost-2")
    for incarnation in (1, 2):
        assert _head(b, b.device_id, incarnation) == _head(a, b.device_id, incarnation)


@pytest.mark.asyncio
async def test_push_stops_at_a_hole_in_the_earlier_incarnation(workers):
    """A's push sends B's old stream only up to a hole, and everything else as usual."""
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
    assert owner_pref(b, "theme") == "before-backup"
    assert _head(b, b.device_id, 1) == held


# ---------------------------------------------------------------------------
# A chat the restored worker runs (owner-ordered rows)
# ---------------------------------------------------------------------------

def _chat(worker):
    with worker.active():
        row = database.get_session("chat")
        return row["title"], row["permission_mode"], [
            e["payload"].get("content") for e in events_db.get_events("chat")]


def _turn(worker, text, *, publish=True):
    with worker.active():
        events_db.append_events("chat", [{"type": "message.user",
                                          "payload": {"type": "message.user", "content": text}}])
        if publish:
            engine.scan_all()


def _restored_chat(workers):
    """B runs a chat; after its backup it renamed it, changed its permission
    mode and took a turn. A holds all of that; B is put back."""
    a, b = _network(workers, "a", "b")
    _session(b, "chat", title="before-backup", events=1)
    converge([a, b])
    backup = b.raw.serialize()
    with b.active():
        database.update_session("chat", title="lost-title")
        set_session_run_binding("chat", permission_mode="acceptEdits")
    _turn(b, "lost turn")
    converge([a, b])
    assert _chat(a) == ("lost-title", "acceptEdits", ["hello 0 from b", "lost turn"])
    _restore(b, backup)
    return a, b


async def _pull_twice(a, b):
    served_by_a = ServedBy(a, "https://a.example.com")
    async with httpx.AsyncClient(transport=served_by_a.transport()) as client:
        await _sync(b, a, client, served_by_a.base_url, catch_up=True)
        with b.active():
            assert engine.context().identity.incarnation == 2
        yield
        for _ in range(2):
            await _sync(b, a, client, served_by_a.base_url)


@pytest.mark.asyncio
async def test_restored_worker_gets_back_its_chat_title_permission_and_turns(workers):
    a, b = _restored_chat(workers)
    async for _ in _pull_twice(a, b):
        pass
    # The permission mode is the owner's own field: its own signed record restores it.
    assert _chat(b) == ("lost-title", "acceptEdits", ["hello 0 from b", "lost turn"])
    converge([a, b])
    assert _chat(a) == _chat(b)


@pytest.mark.asyncio
async def test_writes_since_the_restore_beat_the_versions_it_gets_back(workers):
    """The restore forked the chat: what B wrote since wins, whatever the clocks say."""
    a, b = _restored_chat(workers)
    async for _ in _pull_twice(a, b):
        with b.active():
            database.update_session("chat", title="new-title")
            engine.scan_all()
        _turn(b, "new turn", publish=False)  # not even published yet
    assert _chat(b) == ("new-title", None, ["hello 0 from b", "new turn"])
    assert _head(b, b.device_id, 1) == _head(a, b.device_id, 1)  # all consumed, none left to retry
    # The versions it got back are kept as conflicts, not dropped silently.
    with b.active():
        lost = {c["record_id"] for c in store.list_conflicts() if c["losing_device_id"] == b.device_id}
    assert "chat" in lost
    with b.active():
        engine.scan_all()  # publishes the new turn, in incarnation 2
    converge([a, b])
    assert _chat(a) == _chat(b) == ("new-title", None, ["hello 0 from b", "new turn"])


def test_another_worker_cannot_replay_owner_fields_into_a_chat(workers):
    """Only B's own signed record sets them: a member's copy still cannot."""
    a, b = _network(workers, "a", "b")
    _session(b, "chat", title="t", events=0)
    converge([a, b])
    with b.active(), get_db() as conn:
        record = {"id": "chat", "owner_device_id": b.device_id, "permission_mode": "bypassPermissions"}
        prepared = catalog.prepare_session_mirror(conn.cursor(), record, origin_device_id=a.device_id,
                                                  device_id=b.device_id, own_replay=True)
    assert "permission_mode" not in prepared
