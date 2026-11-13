"""A restored worker's writes outrank what it signed before the restore (#1506).

A database restore puts back an older Lamport clock along with older rows.
Versions this worker signed after the backup still live on its peers, so its
next write, a tombstone included, must be ordered after them. A snapshot must
not turn a live record whose row is missing into a deletion nobody signed.
"""
import httpx
import pytest

from app.db import database
from app.db import network as store
from app.db.base import get_db
from app.network import engine, transport
from tests.core.network.harness import converge, owner_pref, set_owner_pref
from tests.core.network.test_ownership_revocation import _network, _session
from tests.core.network.test_transport import ServedBy

SID = "kept-chat"


def _record(worker, collection, record_id):
    with worker.active(), get_db() as conn:
        return store.get_record(conn.cursor(), collection, record_id)


def _clock(worker) -> int:
    with worker.active():
        return int(store.get_state()["clock"])


def _restore(worker, image):
    """Put back a database image taken earlier (the identity file is not in it)."""
    worker.raw.deserialize(image)
    with worker.active():
        engine.load_context()


@pytest.mark.asyncio
async def test_restored_worker_write_outranks_its_own_lost_versions(workers):
    a, b = _network(workers, "a", "b")
    set_owner_pref(b, "theme", "before-backup")
    converge([a, b])
    backup = b.raw.serialize()
    # B keeps writing after the backup; A holds the newest of those versions.
    for n in range(5):
        set_owner_pref(b, "theme", f"lost-{n}")
    converge([a, b])
    assert owner_pref(a, "theme") == "lost-4"
    with a.active(), get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT clock FROM sync_records WHERE collection = 'user_preferences' "
                       "AND origin_device_id = %s ORDER BY clock DESC LIMIT 1", (b.device_id,))
        newest = int(cursor.fetchone()["clock"])

    _restore(b, backup)
    assert owner_pref(b, "theme") == "before-backup" and _clock(b) < newest
    served = ServedBy(a, "https://holder.example.com")
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            link = transport.PeerLink(a.device_id, [served.base_url])
            await link.sync_once(client, engine.context(), catch_up=True)
            assert engine.context().identity.incarnation == 2
    # Nothing B signs from here on is ordered before what it signed then.
    assert _clock(b) >= newest

    set_owner_pref(b, "theme", "after-restore")
    converge([a, b])
    assert owner_pref(a, "theme") == "after-restore"


def test_own_op_returned_in_a_batch_moves_the_clock_past_it(workers, monkeypatch):
    """The other proof path: a peer relays one of our lost ops back to us.

    The bulk chunk path observes every clock it sees; the one-by-one path it
    falls back to does not observe our own ops, so the proof carries it.
    """
    monkeypatch.setattr(engine, "_apply_chunk", lambda chunk, ctx, result: False)
    a, b = _network(workers, "a", "b")
    set_owner_pref(b, "theme", "before-backup")
    converge([a, b])
    backup = b.raw.serialize()
    for n in range(3):
        set_owner_pref(b, "theme", f"lost-{n}")
    converge([a, b])
    with a.active(), get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT envelope FROM sync_ops WHERE origin_device_id = %s ORDER BY seq",
                       (b.device_id,))
        lost = [store._load(row["envelope"], {}) for row in cursor.fetchall()]
    _restore(b, backup)
    with b.active():
        with get_db() as conn:
            held = store.own_watermark(conn.cursor(), b.device_id, 1)
        ahead = [op for op in lost if op["origin_seq"] > held]
        assert ahead and _clock(b) < max(op["clock"] for op in ahead)
        result = engine.apply_batch(ahead)
        assert result.own_ahead == max(op["origin_seq"] for op in ahead)
        assert engine.context().identity.incarnation == 2
    assert _clock(b) >= max(op["clock"] for op in ahead)


def _items(worker, collection="sessions"):
    with worker.active():
        ctx = engine.context()
        chunk, _ = engine.snapshot_page(collection, ctx=ctx)
        return {item["record_id"]: item for item in engine.open_snapshot_chunk(chunk, ctx=ctx)["items"]}


def test_snapshot_does_not_invent_a_deletion_for_a_missing_row(workers):
    a, b = _network(workers, "a", "b")
    _session(b, SID, title="v")
    _session(b, "gone", title="g")
    converge([a, b])
    with b.active():
        database.delete_session("gone")
        engine.scan_all()
    converge([a, b])
    # A keeps B's live version but has lost the row without a tombstone (as
    # a refused hand-off deletion leaves it, #1308).
    a.sql("DELETE FROM sessions WHERE id = ?", (SID,))
    live = _record(a, "sessions", SID)
    assert live and not live["deleted"]

    items = _items(a)
    assert SID not in items  # nobody signed a deletion at B's live version
    assert items["gone"]["deleted"] and items["gone"]["record"] is None  # a real tombstone still travels
