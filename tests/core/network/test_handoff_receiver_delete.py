"""A chat deleted on the worker that just received it is deleted everywhere (#1308).

After a hand-off the receiver owns the chat, but the chat's sync record still
names the previous owner as its origin: the source's commit published the
ownership flip, and the receiver publishes the row itself only on its next
scan. The owner-only scan retracts only records this worker originated, so a
delete on the receiver before that scan published no tombstone, and the
previous owner kept the chat for good.

When the source's commit record reaches the receiver only after that delete,
it must not outrank the tombstone and bring the chat back on both workers:
the receiver observes the commit's clock from the commit reply, or from the
source's finalize-wakes when every reply was lost.

Whatever the route, the owner's delete writes a durable fact in its own
transaction: incoming sync never recreates that chat, and its tombstone is
published, over whichever worker signed the chat's record, once it can be
(outbox held, no local record yet, ownership taken by abandoning a transfer).
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
        return store.session_deletion(conn.cursor(), sid)


def _assert_deleted_everywhere(a, b):
    converge([a, b])
    for worker in (a, b):
        with worker.active():
            assert database.get_session(SID) is None, worker.name
        assert _record(worker)["deleted"], worker.name
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
        catalog.publish_owned()
    _assert_deleted_everywhere(a, b)


@pytest.mark.asyncio
async def test_receiver_delete_after_finalize_recovers_a_lost_commit_reply(pair, monkeypatch):  # noqa: F811
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


def test_a_chat_the_network_never_knew_gets_no_tombstone(pair):  # noqa: F811
    _, b, _, _ = pair
    with b.active():
        database.create_session("local-only", "default", title="Local")
        assert _record(b, "local-only") is None
        assert database.delete_session("local-only")
        catalog.publish_owned()
    assert _record(b, "local-only") is None
    assert not _fact(b, "local-only")["pending"]  # nothing to publish unless a record arrives


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
            assert _fact(b)["pending"]
        finally:
            engine.hold_outbox(False)
        catalog.publish_owned()
    assert not _fact(b)["pending"]
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
    assert _record(b) is None and not _fact(b)["pending"]
    _assert_deleted_everywhere(a, b)


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


def test_a_pending_deletion_outlives_retention_and_a_published_one_does_not(pair):  # noqa: F811
    _, b, _, _ = pair
    with b.active(), get_db() as conn:
        cur = conn.cursor()
        network_id = store.get_state(conn)["network_id"]
        for sid, pending in (("old-published", False), ("old-pending", True), ("recent", False)):
            store.remember_session_deletion(cur, sid, pending=pending, network_id=network_id)
        cur.execute("UPDATE network_session_deletions SET deleted_at = %s WHERE session_id <> 'recent'",
                    ("2000-01-01T00:00:00+00:00",))
        assert store.prune_session_deletions(cur) == 1
        cur.execute("SELECT session_id FROM network_session_deletions ORDER BY session_id")
        assert [row["session_id"] for row in cur.fetchall()] == ["old-pending", "recent"]


@pytest.mark.asyncio
async def test_a_fact_of_a_network_this_worker_left_never_refuses_its_new_network(pair, monkeypatch):  # noqa: F811
    """Review S8: a delete that raced leaving network X wrote X's fact; in network Y it counts for nothing."""
    a, b, _, _ = pair
    await _move_to_receiver(a, b, monkeypatch)
    exchange(a, b)
    with b.active(), get_db() as conn:
        # What the delete in flight while B left X committed: X's fact, no row.
        cur = conn.cursor()
        store.remember_session_deletion(cur, SID, pending=True, network_id="a-network-this-worker-left")
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
    assert not _fact(b)["pending"]


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
        assert _fact(b)["pending"], "the copy committed, and with it the tombstone it owes"
        monkeypatch.setattr(engine, "_refused_session_ids", set(), raising=False)  # a restart forgets it
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
    monkeypatch.setattr(engine, "_refused_session_ids", set(), raising=False)
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
        store.remember_session_deletion(cur, SID, pending=False, network_id=store.get_state(conn)["network_id"])
        assert store.session_deletion(cur, SID) is not None
    with b.active():
        assert database.get_session(SID) is not None  # the row is here
        assert engine.publish_session_deletions() == 0
    with b.active(), get_db() as conn:
        assert store.session_deletion(conn.cursor(), SID) is None


@pytest.mark.asyncio
async def test_a_lost_refusal_still_owes_the_tombstone_when_another_peer_moves_the_frontier(
        pair, tmp_path, monkeypatch):  # noqa: F811
    """Review S9: the refusing copy rolled back and B restarted; C's snapshot then says 'deleted at A's version'."""
    from tests.core.network.harness import Worker, join

    a, b, _, _ = pair
    c = Worker("c", tmp_path)
    join(a, c)
    exchange(a, b)  # B learns C is a member
    await _delete_unsynced_on_receiver(a, b, monkeypatch)  # a fact with nothing to publish yet
    exchange(a, c)
    a_version = _record(a)
    assert not a_version["deleted"]
    with c.active(), get_db() as conn:
        # C purges its mirror: the chat is a tombstone at A's version there.
        cur = conn.cursor()
        cur.execute("DELETE FROM sessions WHERE id=%s", (SID,))
        cur.execute("UPDATE sync_records SET deleted=TRUE, content_hash='' "
                    "WHERE collection='sessions' AND record_id=%s", (SID,))
    # The refusing copy from A rolled back, and B restarted: no pending fact, no hint.
    monkeypatch.setattr(engine, "_refused_session_ids", set(), raising=False)
    assert not _fact(b)["pending"]
    with c.active():
        manifest = engine.snapshot_manifest()
    _copy_state(b, _snapshot(c, "sessions"))
    with b.active():
        engine.adopt_watermarks(manifest["watermarks"])  # B acknowledges A's stream through C
        catalog.publish_owned()
    converge([a, b, c])
    with a.active():
        assert database.get_session(SID) is None, "A kept the chat its owner deleted"
    record = _record(a)
    assert record["deleted"] and record["origin_device_id"] == b.device_id
    assert record["clock"] > a_version["clock"], "an equal-version tombstone does not replace a live record"
    c.close()
