"""A chat deleted on the worker that just received it is deleted everywhere (#1308).

After a hand-off the receiver owns the chat, but the chat's sync record still
names the previous owner as its origin: the source's commit published the
ownership flip, and the receiver publishes the row itself only on its next
scan. The owner-only scan retracts only records this worker originated, so a
delete on the receiver before that scan published no tombstone, and the
previous owner kept the chat for good.

The owner's delete writes a durable fact in its own transaction. Incoming
sync never recreates that chat: a live version that arrives is refused but
kept as the chat's sync record, and the tombstone is owed while that record
is live (fact + no row + live record). Signing the tombstone, or a peer's
winning tombstone, ends it; a newer live version (the source's commit record
arriving after the delete) owes it again, over a higher clock.
"""
import time

import pytest
from fastapi import BackgroundTasks

from app.api import sessions as sessions_api
from app.core import session_handoff
from app.db import database, session_moves, wakeups
from app.db import network as store
from app.db import sessions as sessions_db
from app.db.base import get_db
from app.network import catalog, control, engine, handoff, transport
from tests.core.network.harness import converge, exchange
from tests.core.network.test_handoff import pair, repositories  # noqa: F401 - fixtures

SID = "handoff-test"


def _record(worker, sid=SID):
    with worker.active(), get_db() as conn:
        return store.get_record(conn.cursor(), "sessions", sid)


def _prepare(a, b):
    for worker in (a, b):
        with worker.active(), get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET project_v2_id=NULL,branch=NULL WHERE id=%s", (SID,))
    with a.active(), get_db() as conn:
        # The source published far more than the receiver has seen: its
        # commit record's clock is ahead of anything the receiver signs.
        store.observe_clock(conn.cursor(), 10_000)


def _route(a, b, monkeypatch, lost_commit_replies=False):
    async def call(device, action, body, **kwargs):
        if device == b.device_id:  # the source's finalize-wakes
            with b.active():
                return await control.handle(action, body, from_device=a.device_id)
        with a.active():
            result = await control.handle(action, body, from_device=b.device_id)
        if lost_commit_replies and body.get("phase") == "commit":
            raise TimeoutError("the commit applied, its reply was lost")
        return result

    monkeypatch.setattr(control, "call", call)


async def _move_to_receiver(a, b, monkeypatch):
    _prepare(a, b)
    _route(a, b, monkeypatch)
    with b.active():
        await handoff.receive(database.get_session(SID))
        assert database.get_session(SID)["owner_device_id"] == b.device_id


async def _delete_on_receiver(b):
    with b.active():
        await sessions_api._delete_session(database.get_session(SID), BackgroundTasks())
        assert database.get_session(SID) is None
        catalog.publish_owned()


def _fact(worker, sid=SID):
    with worker.active(), get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM network_session_deletions WHERE session_id=%s", (sid,))
        return cur.fetchone()


def _owed(worker, sid=SID):
    with worker.active(), get_db() as conn:
        return store.owed_session_tombstones(conn.cursor(), [sid]) == [sid]


def _settle(workers):
    """Sync and run the owner scan (the scanner, every few seconds) until quiet."""
    for _ in range(3):
        converge(workers)
        for worker in workers:
            with worker.active():
                catalog.publish_owned()
    converge(workers)


def _assert_deleted_everywhere(a, b):
    _settle([a, b])
    for worker in (a, b):
        with worker.active():
            assert database.get_session(SID) is None, worker.name
            assert engine.publish_session_deletions() == 0, "published once, not on every scan"
        assert _record(worker)["deleted"], worker.name
        assert not _owed(worker)
    assert _fact(a) is None  # the previous owner only applied the tombstone


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_arrived", [True, False])
async def test_receiver_delete_right_after_handoff_reaches_the_source(pair, monkeypatch, commit_arrived):  # noqa: F811
    a, b, _, _ = pair
    await _move_to_receiver(a, b, monkeypatch)
    if commit_arrived:
        # The source's commit record reaches the receiver before its own scan.
        exchange(a, b)
        assert _record(b)["origin_device_id"] == a.device_id

    await _delete_on_receiver(b)
    _assert_deleted_everywhere(a, b)


@pytest.mark.asyncio
async def test_commit_record_arriving_right_after_the_delete_commits_cannot_undo_it(pair, monkeypatch):  # noqa: F811
    a, b, _, _ = pair
    await _move_to_receiver(a, b, monkeypatch)
    rows = sessions_db._delete_session_rows

    def deliver_commit_after_delete(*args):
        deleted = rows(*args)
        exchange(a, b)  # the source's delayed commit record lands now
        return deleted

    monkeypatch.setattr(sessions_db, "_delete_session_rows", deliver_commit_after_delete)
    with b.active():
        await sessions_api._delete_session(database.get_session(SID), BackgroundTasks())
        assert database.get_session(SID) is None
    # The refused commit record became B's record, so the delete's own
    # publication outranked it rather than the version B held before.
    commit = _record(a)
    assert not commit["deleted"] and commit["origin_device_id"] == a.device_id
    tombstone = _record(b)
    assert tombstone["deleted"] and tombstone["origin_device_id"] == b.device_id
    assert tombstone["clock"] > commit["clock"]
    _assert_deleted_everywhere(a, b)


@pytest.mark.asyncio
async def test_receiver_delete_after_finalize_recovers_a_lost_commit_reply(pair, monkeypatch):  # noqa: F811
    """B owns the chat through finalize-wakes; A's commit record, far ahead of B's clock, arrives later."""
    a, b, _, _ = pair
    _prepare(a, b)
    with a.active():
        # A queued move with a pending wake: the source finalizes its wakes.
        session_moves.save(SID, target_id=b.device_id, label="B", note="", requested_by="owner",
                           requested_at=time.time(), expires_at=time.time() + 600)
        wakeups.create_wakeup(id="wake-moved", session_id=SID, prompt="later",
                              profile_id="default", fire_at=time.time() + 3600)
    _route(a, b, monkeypatch, lost_commit_replies=True)
    with b.active():
        with pytest.raises(TimeoutError):
            await handoff.receive(database.get_session(SID))
        assert database.get_session(SID)["owner_device_id"] == a.device_id
    with a.active():
        assert database.get_session(SID)["owner_device_id"] == b.device_id
        # The source's delivery finalizes the receiver's ownership.
        await session_handoff._finalize_moved_wakes(SID, b.device_id, release=False)
    with b.active():
        assert database.get_session(SID)["owner_device_id"] == b.device_id
    await _delete_on_receiver(b)
    _assert_deleted_everywhere(a, b)


@pytest.mark.asyncio
async def test_receiver_delete_of_a_chat_moved_from_an_unsynced_mirror(pair, monkeypatch):  # noqa: F811
    a, b, _, _ = pair
    with b.active(), get_db() as conn:
        # The receiver's local mirror of a chat it created on the source,
        # before the source's first sync of it arrived: no sync record.
        conn.cursor().execute("DELETE FROM sync_records WHERE collection='sessions' AND record_id=%s", (SID,))
    assert _record(b) is None
    await _move_to_receiver(a, b, monkeypatch)
    assert _record(b) is None
    await _delete_on_receiver(b)
    _assert_deleted_everywhere(a, b)


def test_a_chat_the_network_never_knew_publishes_nothing(pair):  # noqa: F811
    """No live version known here: nothing is owed; the fact still refuses one that arrives."""
    _, b, _, _ = pair
    with b.active():
        database.create_session("local-only", "default", title="Local")
        assert _record(b, "local-only") is None
        assert database.delete_session("local-only")
        assert _fact(b, "local-only") is not None
        assert engine.publish_session_deletions() == 0
    assert _record(b, "local-only") is None


def test_both_owners_deleting_settle_on_one_tombstone(pair):  # noqa: F811
    """Review S12: a transfer abandoned while the source was offline leaves both owner; both delete."""
    a, b, _, _ = pair
    exchange(a, b)
    with b.active(), get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET owner_device_id=%s WHERE id=%s", (b.device_id, SID))
    for worker in (a, b):
        with worker.active():
            assert database.delete_session(SID)
            assert _fact(worker) is not None
    _settle([a, b])
    # The winning tombstone is each worker's record: neither owes anything,
    # so neither republishes over the other on the next scans.
    version = lambda w: (_record(w)["clock"], _record(w)["origin_device_id"])  # noqa: E731
    assert version(a) == version(b)
    for worker in (a, b):
        assert _record(worker)["deleted"] and not _owed(worker)
        with worker.active():
            assert engine.publish_session_deletions() == 0
            assert database.get_session(SID) is None


@pytest.mark.asyncio
async def test_receiver_delete_while_the_outbox_is_held_is_published_later(pair, monkeypatch):  # noqa: F811
    a, b, _, _ = pair
    await _move_to_receiver(a, b, monkeypatch)
    exchange(a, b)  # B holds A's record of the chat
    with b.active():
        engine.hold_outbox(True)  # startup: nothing is signed until streams are compared
        try:
            await _delete_on_receiver(b)
            assert _record(b)["origin_device_id"] == a.device_id and not _record(b)["deleted"]
            assert _owed(b)
        finally:
            engine.hold_outbox(False)
        catalog.publish_owned()
    assert not _owed(b)
    _assert_deleted_everywhere(a, b)


@pytest.mark.asyncio
async def test_receiver_delete_after_abandoning_a_transfer_outlives_the_delayed_commit(pair, monkeypatch):  # noqa: F811
    a, b, _, _ = pair
    _prepare(a, b)
    with a.active():
        session_moves.save(SID, target_id=b.device_id, label="B", note="", requested_by="owner",
                           requested_at=time.time(), expires_at=time.time() + 600)
        wakeups.create_wakeup(id="wake-moved", session_id=SID, prompt="later",
                              profile_id="default", fire_at=time.time() + 3600)
    _route(a, b, monkeypatch, lost_commit_replies=True)
    with b.active():
        with pytest.raises(TimeoutError):
            await handoff.receive(database.get_session(SID))
        # The source stays offline past the threshold: the owner abandons.
        monkeypatch.setattr(transport, "online_devices", list)
        monkeypatch.setattr(handoff, "ABANDON_AFTER_S", 0)
        token = next(row["token"] for row in handoff.transfer_recovery_status()["pending"])
        handoff.abandon_transfer(token)
        assert database.get_session(SID)["owner_device_id"] == b.device_id
    await _delete_on_receiver(b)
    # A comes back: its commit record (a higher clock than B's tombstone)
    # reaches B, which refuses it and signs a tombstone that outranks it.
    _assert_deleted_everywhere(a, b)


@pytest.mark.asyncio
async def test_receiver_delete_of_an_unsynced_mirror_moved_then_unpinned(pair, monkeypatch):  # noqa: F811
    a, b, _, _ = pair
    with b.active(), get_db() as conn:
        conn.cursor().execute("DELETE FROM sync_records WHERE collection='sessions' AND record_id=%s", (SID,))
    await _move_to_receiver(a, b, monkeypatch)
    with b.active():
        sessions_db.unpin_session(SID, "")  # placement (and its hand-off receipt) is gone
        assert database.get_session(SID)["placement"] in (None, {}, "null")
    await _delete_on_receiver(b)
    assert _record(b) is None and not _owed(b)  # nothing known here to outrank yet
    # A's record of the chat reaches B, is refused and owes the tombstone.
    _assert_deleted_everywhere(a, b)
    assert _record(b)["origin_device_id"] == b.device_id


@pytest.mark.asyncio
async def test_a_worker_that_does_not_own_the_chat_never_writes_the_fact(pair, monkeypatch):  # noqa: F811
    a, b, _, _ = pair
    await _move_to_receiver(a, b, monkeypatch)
    exchange(a, b)
    with a.active():
        # The previous owner deletes its local mirror (a project or batch
        # delete reaches mirrors through the same function).
        assert database.delete_session(SID)
    assert _fact(a) is None
    exchange(a, b)
    with b.active():
        catalog.publish_owned()
    exchange(a, b)
    assert _fact(b) is None


def test_an_owed_deletion_outlives_retention_and_a_settled_one_does_not(pair):  # noqa: F811
    _, b, _, _ = pair
    with b.active(), get_db() as conn:
        cur = conn.cursor()
        network_id = store.get_state(conn)["network_id"]
        for sid in ("old-settled", "old-owed", "recent"):
            store.remember_session_deletion(cur, sid, network_id=network_id)
        store.put_record(cur, "sessions", "old-owed", clock=5, origin_device_id="dev_other", content_hash="",
                         deleted=False)
        cur.execute("UPDATE network_session_deletions SET deleted_at = %s WHERE session_id <> 'recent'",
                    ("2000-01-01T00:00:00+00:00",))
        assert store.prune_session_deletions(cur) == 1
        cur.execute("SELECT session_id FROM network_session_deletions ORDER BY session_id")
        assert [row["session_id"] for row in cur.fetchall()] == ["old-owed", "recent"]


@pytest.mark.asyncio
async def test_a_fact_of_a_network_this_worker_left_never_refuses_its_new_network(pair, monkeypatch):  # noqa: F811
    """Review S8: a delete that raced leaving network X wrote X's fact; in network Y it counts for nothing."""
    a, b, _, _ = pair
    await _move_to_receiver(a, b, monkeypatch)
    exchange(a, b)
    with b.active(), get_db() as conn:
        # What the delete in flight while B left X committed: X's fact, no row.
        cur = conn.cursor()
        store.remember_session_deletion(cur, SID, network_id="a-network-this-worker-left")
        cur.execute("DELETE FROM sessions WHERE id=%s", (SID,))
        cur.execute("DELETE FROM sync_records WHERE collection='sessions' AND record_id=%s", (SID,))
    with a.active(), get_db() as conn:
        # In the new network A holds and owns the same chat id; its record reaches B.
        conn.cursor().execute("UPDATE sessions SET owner_device_id=%s WHERE id=%s", (a.device_id, SID))
    with a.active():
        catalog.publish_owned()
    exchange(a, b)
    with b.active():
        assert database.get_session(SID) is not None, "another network's fact refused the chat"
        assert engine.publish_session_deletions() == 0
        catalog.publish_owned()
    exchange(a, b)
    with a.active():
        assert database.get_session(SID) is not None
    assert not _record(a)["deleted"] and not _record(b)["deleted"]
    with b.active(), get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT session_id FROM network_session_deletions")
        assert cur.fetchall() == [], "another network's fact must be pruned"


def _snapshot(worker, *collections):
    with worker.active():
        return [engine.snapshot_chunk(name, 0) for name in collections]


def _copy_state(worker, chunks):
    """Apply peer pages in one transaction, the way the state copy does (peer_snapshot)."""
    with worker.active():
        ctx = engine.require_context()
        with get_db() as conn:
            store.lock_state(conn.cursor())
            for chunk in chunks:
                engine.apply_snapshot_chunk(chunk, ctx=ctx, _conn=conn)


async def _delete_unsynced_on_receiver(a, b, monkeypatch):
    """B owns the chat, holds no record of it, and deletes it: a fact with nothing to publish yet."""
    await _move_to_receiver(a, b, monkeypatch)
    with b.active(), get_db() as conn:
        conn.cursor().execute("DELETE FROM sync_records WHERE collection='sessions' AND record_id=%s", (SID,))
    with b.active():
        assert database.delete_session(SID)
    assert _fact(b) is not None and not _owed(b)


@pytest.mark.asyncio
async def test_a_state_copy_carrying_a_deleted_chat_and_its_tag_completes_and_owes_the_tombstone(pair, monkeypatch):  # noqa: F811
    """Review S7: the refused chat's tag would fail its session foreign key and roll the whole copy back."""
    a, b, _, _ = pair
    with a.active():
        database.create_tag("copy-tag", "Tag")
        assert database.add_session_tag(SID, "copy-tag")
        engine.scan_all()
    exchange(a, b)
    await _delete_unsynced_on_receiver(a, b, monkeypatch)
    chunks = _snapshot(a, "tags", "sessions", "session_tags")
    assert any(item["record_id"].startswith(SID + "|") for item in
               engine.open_snapshot_chunk(chunks[2], ctx=a_ctx(a))["items"])
    _copy_state(b, chunks)
    with b.active():
        assert database.get_session(SID) is None
        assert _rows("SELECT * FROM session_tags WHERE session_id=%s") == []
    # The copy committed, and with it A's live version as B's record: owed,
    # from durable state alone (nothing in memory survives a restart).
    assert _owed(b) and _record(b)["origin_device_id"] == a.device_id
    with b.active():
        assert engine.publish_session_deletions() == 1
    converge([a, b])
    _assert_deleted_everywhere(a, b)


@pytest.mark.asyncio
async def test_a_snapshot_after_restart_stores_no_events_of_a_deleted_chat(pair, monkeypatch):  # noqa: F811
    """Review S7: with no session record and no in-memory marks, events were taken as 'parent not arrived'."""
    from app.db import session_events, session_locks

    a, b, _, _ = pair
    with a.active():
        session_events.append_events(SID, [{"type": "user_message", "payload": {"text": "hi"}}])
        engine.scan_all()
    await _delete_unsynced_on_receiver(a, b, monkeypatch)
    chunks = _snapshot(a, "sessions", "session_events")
    assert engine.open_snapshot_chunk(chunks[1], ctx=a_ctx(a))["items"], "the snapshot carries the events"
    with session_locks._deleted_lock:
        session_locks._DELETED.clear()  # a restart
    _copy_state(b, chunks[1:])  # the events page alone first: the parent is refused, not late
    _copy_state(b, chunks)
    with b.active():
        assert database.get_session(SID) is None
        assert _rows("SELECT seq FROM session_events WHERE session_id=%s") == []
        assert engine.publish_session_deletions() == 1
    converge([a, b])
    _assert_deleted_everywhere(a, b)


def _rows(query):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(query, (SID,))
        return cur.fetchall()


def a_ctx(worker):
    with worker.active():
        return engine.require_context()


def test_the_owner_scan_drops_the_fact_of_a_chat_recreated_here(pair):  # noqa: F811
    """Review (c): a row with the fact's id means the chat came back (whole-database recovery)."""
    _, b, _, _ = pair
    with b.active(), get_db() as conn:
        cur = conn.cursor()
        store.remember_session_deletion(cur, SID, network_id=store.get_state(conn)["network_id"])
    assert _fact(b) is not None
    with b.active():
        assert database.get_session(SID) is not None  # the row is here
        assert engine.publish_session_deletions() == 0
    assert _fact(b) is None
