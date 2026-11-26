"""A synced connector delete takes the connector lock before the row (#1399 review).

Two workers on real PostgreSQL (a disposable cluster, never the app
database). A's stream edits connector C and then deletes it. On B a second
connection holds C's journal lock and then updates C's row, as
``import_watch`` does through ``watch_journal.restore``. If the apply writes
C's row before it asks for the lock, PostgreSQL finds a deadlock and aborts
one of the two. A peer copy can write C's row without touching C itself:
deleting C's API key on an earlier page sets ``connectors.api_user_id`` to
NULL in the same transaction.

The restore is a local write that lands while A's versions are being applied.
It gets no exemption (#1605): nothing replaces it unsigned. The apply signs
it, at once when it is there before the first check and after one waiting
pass otherwise, and the two workers then order it against A's versions like
any two concurrent edits.
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from app.db import database, watch_journal
from app.db import network as store
from app.db import watches as watch_store
from app.network import engine, envelope, peer_snapshot
from tests.core.network.harness import converge, exchange, join
from tests.core.network.test_pending_apply_budget import pg_workers, private_cluster  # noqa: F401
from tests.core.network.test_unscanned_local_edit import _own_ops, _second_connection, _settle, _stream_rounds
from tests.db.schema_lifecycle_helpers import pg_connect

BOT = "bot-1399-lock-order"
KEY = "key-1399-lock-order"


def _failures(caplog):
    """Exceptions logged during the race. SessionSearch logs its expected
    fallback on a PostgreSQL without pg_trgm with a traceback; that is not a
    deadlock."""
    return [record.exc_info[1] for record in caplog.records
            if record.exc_info and record.name != "app.db.session_recall"]


def _pair(pg_workers, **connector):
    """Two converged workers that both hold connector C."""
    a, b = pg_workers("worker-a"), pg_workers("worker-b")
    with a.active():
        engine.create_network("Home", device_name="Worker A")
    join(a, b)
    with a.active():
        if connector:
            database.create_api_user(KEY, "connector:" + BOT, "hash")
        database.create_connector(BOT, "telegram", "Bot", {}, [], is_active=False, **connector)
        engine.scan_all()
    _settle(a, b)
    return a, b


def _race_watch_import(private_cluster, worker, apply):
    """Run ``apply`` while another connection to ``worker``'s database holds
    C's lock and then updates C's row."""
    held, released = threading.Event(), threading.Event()
    importer_pid = []

    def importer():
        # import_watch's order: the connector lock, then restore's row update.
        with closing(_second_connection(private_cluster, worker)) as conn:
            c = conn.cursor()
            importer_pid.append(conn.get_backend_pid())
            watch_store.lock_connector(c, BOT)
            held.set()
            assert released.wait(15), "the sync apply never queued behind the connector lock"
            watch_journal.restore(c, {"source": {"connector_id": BOT, "channel_id": "123"}},
                                  {"entries": [], "heads": []})
            conn.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        imported = pool.submit(importer)
        assert held.wait(10)
        applied = pool.submit(apply)
        try:
            deadline = time.monotonic() + 10
            with closing(pg_connect(private_cluster)) as observer:
                observer.autocommit = True
                while True:
                    c = observer.cursor()
                    c.execute("SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND NOT granted "
                              "AND pid <> %s", (importer_pid[0],))
                    if c.fetchone():
                        break
                    if applied.done():
                        applied.result()  # its own error, if it failed before the lock
                    assert time.monotonic() < deadline, "the sync apply never asked for the connector lock"
                    time.sleep(0.01)
        finally:
            released.set()
        imported.result(timeout=30)
        return applied.result(timeout=30)


def _connector(worker):
    with worker.active():
        row = database.get_connector(BOT)
        return None if row is None else (row["name"], bool(row["is_active"]), row["api_user_id"])


def _ordered_like_two_edits(a, b):
    """B signed its restored row once. The higher version wins on both
    workers, the other one is a recorded conflict on both, and no later round
    signs anything. Returns whether B's row won."""
    mine, theirs = _own_ops(b, "connectors")[-1], _own_ops(a, "connectors")[-1]
    assert (mine["record_id"], mine["op"]) == (BOT, engine.OP_UPSERT)
    local_wins = envelope.compare(mine["clock"], b.device_id, theirs["clock"], a.device_id) > 0
    _settle(a, b)
    expected = ("Bot", True, None) if local_wins else None
    assert [_connector(a), _connector(b)] == [expected, expected]
    loser = (a if local_wins else b).device_id
    for worker in (a, b):
        with worker.active():
            assert loser in [c["losing_device_id"] for c in store.list_conflicts()
                             if (c["collection"], c["record_id"]) == ("connectors", BOT)]
    if not local_wins:
        assert b.sql("SELECT fence FROM watch_connector_journals WHERE connector_id = ?", (BOT,))[0]["fence"] == "deleted"
    _stream_rounds(a, b)
    return local_wins


def _ops(sender, receiver):
    """What ``receiver`` lacks from ``sender``, as signed wire envelopes."""
    with receiver.active():
        marks = store.local_watermarks()
    with sender.active():
        ops, _, needs_snapshot = store.exchange_batch(marks, exclude_origin=receiver.device_id)
    assert not needs_snapshot
    return ops


@pytest.mark.parametrize("delivery", ["chunk", "pending"])
def test_chunk_editing_then_deleting_a_connector_waits_for_its_lock_first(
        pg_workers, private_cluster, caplog, delivery):
    a, b = _pair(pg_workers)
    with a.active():
        database.update_connector(BOT, name="Renamed bot")
        assert engine.scan_collection("connectors") == 1
        assert database.delete_connector(BOT)
        assert engine.scan_collection("connectors") == 1
    chunk = _ops(a, b)
    assert [(op["collection"], op["record_id"], op["op"]) for op in chunk] == [
        ("connectors", BOT, engine.OP_UPSERT), ("connectors", BOT, engine.OP_DELETE)]
    origin, incarnation = chunk[0]["origin_device_id"], chunk[0]["origin_incarnation"]
    with b.active():
        ctx = engine.context()
        if delivery == "pending":
            with database.get_db() as conn:
                store.stash_pending_batch(conn.cursor(), chunk)

    caplog.set_level(logging.DEBUG, logger=engine.logger.name)
    result = engine.ApplyResult()

    def apply():
        with b.active():
            if delivery == "chunk":
                return engine._apply_chunk(chunk, ctx, result)
            # The same ops buffered earlier, drained by the pending path.
            assert engine._apply_pending_chunk(origin, incarnation, ctx, result)
            return True

    signed = len(_own_ops(b, "connectors"))
    committed = _race_watch_import(private_cluster, b, apply)

    # A deadlock victim inside the chunk is rolled back to its savepoint and
    # left for a retry; one outside it would fail the watch import above.
    failures = _failures(caplog)
    assert committed and not failures, failures
    # The restore landed after the first check: this pass replaces nothing.
    assert result.retry == 1 and result.applied == result.rejected == result.lost == 0, result.reasons
    assert engine.LOCAL_CHANGE_WAITS in result.reasons[0]
    assert _connector(b) == ("Bot", True, None) and len(_own_ops(b, "connectors")) == signed

    again = exchange(a, b)  # the next pass signs the restore first
    with b.active():
        engine.resume_pending()
    assert again.retry == again.rejected == 0, again.reasons
    assert len(_own_ops(b, "connectors")) == signed + 1
    _ordered_like_two_edits(a, b)


def test_peer_copy_deleting_a_key_then_its_connector_takes_the_connector_lock_first(
        pg_workers, private_cluster, tmp_path):
    """One copy transaction deletes API key U on an earlier page and connector C on a later one.

    The foreign key's ON DELETE SET NULL writes C's row during U's delete, so
    the copy must hold C's lock before its first page. The restore commits
    while the copy waits for that lock, so the copy's first check of C finds
    it and signs it: nothing waits here.
    """
    a, b = _pair(pg_workers, api_user_id=KEY)
    # Worker A deletes the key and then its connector, as connector_service does.
    with a.active():
        assert database.delete_api_user(KEY)
        assert database.delete_connector(BOT)
        engine.scan_all()
        manifest = peer_snapshot.capture(b.device_id)
        for index in range(len(manifest["pages"])):
            (tmp_path / f"{index}.json").write_bytes(
                peer_snapshot.page(b.device_id, manifest["transfer"], index))
    collections = [page["collection"] for page in manifest["pages"]]
    assert collections.index("api_users") < collections.index("connectors")
    signed = len(_own_ops(b, "connectors"))

    def apply():
        with b.active():
            return peer_snapshot.apply(manifest, tmp_path, a.device_id)

    # Before the copy took C's lock up front: psycopg2.errors.DeadlockDetected.
    assert _race_watch_import(private_cluster, b, apply) > 0
    with b.active():
        assert database.get_api_user(KEY) is None
    assert len(_own_ops(b, "connectors")) == signed + 1
    _ordered_like_two_edits(a, b)
    with a.active():
        assert database.get_api_user(KEY) is None


@pytest.mark.parametrize("then", ["delete", "rename"])
def test_chunk_deleting_a_key_then_its_connector_signs_nothing_here(pg_workers, then):
    """No local write at all: A deletes C's key and then deletes or renames C.
    The key's tombstone clears C's reference on B; that is the apply's own
    write, so A's next version of C applies in the same pass."""
    a, b = _pair(pg_workers, api_user_id=KEY)
    with a.active():
        assert database.delete_api_user(KEY)
        if then == "delete":
            assert database.delete_connector(BOT)
        else:
            database.update_connector(BOT, name="Renamed bot")
        engine.scan_all()
    signed = len(_own_ops(b))
    result = exchange(a, b)
    assert result.retry == result.lost == result.rejected == 0, result.reasons
    assert len(_own_ops(b)) == signed
    expected = None if then == "delete" else ("Renamed bot", False, None)
    assert [_connector(a), _connector(b)] == [expected, expected]
    converge([a, b])
    _stream_rounds(a, b)
    assert [_connector(a), _connector(b)] == [expected, expected]


@pytest.mark.parametrize("delivery", ["chunk", "pending"])
def test_chunk_editing_a_connector_then_deleting_its_key_and_it_takes_the_lock_first(
        pg_workers, private_cluster, caplog, delivery):
    """A's stream edits C, deletes C's key and then deletes C. The key's
    deletion clears C's reference after a direct edit of C in the same chunk,
    so the apply locks C's row to settle that clear (``_BatchClears``). It
    must hold C's journal lock first (#1399): import_watch holds that lock and
    then writes C's row."""
    a, b = _pair(pg_workers, api_user_id=KEY)
    with a.active():
        database.update_connector(BOT, name="Renamed bot")
        assert engine.scan_collection("connectors") == 1
        assert database.delete_api_user(KEY)
        assert engine.scan_collection("api_users") == 1
        assert database.delete_connector(BOT)
        assert engine.scan_collection("connectors") == 1
    chunk = _ops(a, b)
    assert [(op["collection"], op["record_id"], op["op"]) for op in chunk] == [
        ("connectors", BOT, engine.OP_UPSERT), ("api_users", KEY, engine.OP_DELETE),
        ("connectors", BOT, engine.OP_DELETE)]
    origin, incarnation = chunk[0]["origin_device_id"], chunk[0]["origin_incarnation"]
    with b.active():
        ctx = engine.context()
        if delivery == "pending":
            with database.get_db() as conn:
                store.stash_pending_batch(conn.cursor(), chunk)

    caplog.set_level(logging.DEBUG, logger=engine.logger.name)
    result = engine.ApplyResult()

    def apply():
        with b.active():
            if delivery == "chunk":
                return engine._apply_chunk(chunk, ctx, result)
            assert engine._apply_pending_chunk(origin, incarnation, ctx, result)
            return True

    # Before the clear took C's lock first: psycopg2.errors.DeadlockDetected.
    committed = _race_watch_import(private_cluster, b, apply)
    failures = _failures(caplog)
    assert committed and not failures, failures
    # The restore is signed before the chunk replaces anything; A's later
    # versions are then ordered against it like any concurrent edit.
    assert result.rejected == 0, result.reasons
    with b.active():
        engine.resume_pending()
    _settle(a, b)
    assert _connector(a) == _connector(b)
    for worker in (a, b):
        with worker.active():
            assert [c for c in store.list_conflicts() if (c["collection"], c["record_id"]) == ("connectors", BOT)]
    for worker in (a, b):
        with worker.active():
            assert database.get_api_user(KEY) is None
