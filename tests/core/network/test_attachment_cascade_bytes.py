"""A peer's winning note deletion keeps the attachment bytes it cascades away (#1693).

``note_attachments.data`` never travels. When B attached a file to a shared
note that A then deleted (A's delete winning by clock), applying the delete
on B cascaded the attachment row and with it the only copy of the bytes.
B now keeps each such attachment as a conflict that holds its bytes:
readable by hash (and servable to peers) while the conflict is kept, gone
once it is dismissed or expires.
"""
from datetime import datetime, timedelta, timezone

from app.db import database, network as store
from app.network import blobs, engine, peer_files
from tests.core.network.harness import converge, exchange, join

PAYLOAD = b"bytes only B ever held"
DIGEST = blobs.sha256(PAYLOAD)


def _shared_note(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        database.create_note(note_id="n1", title="Note", content="body")
        engine.scan_all()
    join(a, b)
    converge([a, b])
    return a, b


def _attach(worker, attachment_id="att1", data=PAYLOAD):
    with worker.active():
        database.create_note_attachment(attachment_id=attachment_id, note_id="n1", filename="x.txt",
                                        file_path="/tmp/x.txt", content_type="text/plain",
                                        size_bytes=len(data), data=data)
        engine.scan_all()


def _delete_note_winning(worker):
    """Several edits first, so this worker's deletion outranks the other's attachment."""
    with worker.active():
        for i in range(4):
            database.update_note("n1", title=f"edit {i}")
            engine.scan_all()
        database.delete_note("n1")
        engine.scan_all()


def _attachment_conflicts(worker):
    with worker.active():
        return [c for c in store.list_conflicts() if c["collection"] == "note_attachments"]


def test_winning_peer_note_delete_keeps_the_cascaded_attachment_bytes(workers):
    a, b = _shared_note(workers)
    _attach(b)  # published: B signed the attachment record before the delete arrived
    _delete_note_winning(a)
    converge([a, b])
    with b.active():
        engine.scan_all()  # B retracts the cascaded attachment, as its scanner would
    converge([a, b])

    with a.active():
        assert database.get_note("n1") is None
        assert [c["record_id"] for c in _attachment_conflicts(a)] == ["att1"]
    with b.active():
        assert database.get_note("n1") is None
        assert b.sql("SELECT id FROM note_attachments") == []
        # Readable by hash on B, and B still serves it to a peer that asks.
        assert blobs.read_local(DIGEST) == PAYLOAD
        assert blobs.read_local_range(DIGEST, 6, 4) == (PAYLOAD[6:10], len(PAYLOAD))
        assert peer_files.part(DIGEST, 0, a.device_id)["size"] == len(PAYLOAD)
        # The loser is listed on B too, naming the hash, never carrying the bytes.
        [conflict] = _attachment_conflicts(b)
        assert conflict["record_id"] == "att1" and conflict["losing_device_id"] == b.device_id
        assert conflict["winning_device_id"] == a.device_id
        assert conflict["losing_record"]["blob_hash"] == DIGEST
        assert "kept_blob" not in conflict and "kept_blob_hash" not in conflict

        # Dismissing the conflict drops the bytes with it.
        assert store.delete_conflict(conflict["id"])
        assert blobs.read_local(DIGEST) is None


def test_kept_bytes_go_when_the_conflict_expires(workers):
    a, b = _shared_note(workers)
    _attach(b)
    _delete_note_winning(a)
    converge([a, b])
    with b.active():
        assert blobs.read_local(DIGEST) == PAYLOAD
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        b.sql("UPDATE sync_conflicts SET expires_at = ? WHERE kept_blob_hash = ?", (past, DIGEST))
        assert blobs.read_local(DIGEST) is None  # expired: no longer served, even before the sweep
        assert store.expire_conflicts() >= 1
        assert b.sql("SELECT id FROM sync_conflicts WHERE kept_blob_hash = ?", (DIGEST,)) == []


def test_same_delete_applied_again_keeps_one_copy(workers):
    a, b = _shared_note(workers)
    _attach(b)
    _delete_note_winning(a)
    converge([a, b])
    with a.active():
        snapshot = engine.snapshot_chunk("notes", 0)
    with b.active():
        # The same tombstone again, through the state-copy path.
        engine.apply_snapshot_chunk(snapshot)
        assert len(b.sql("SELECT id FROM sync_conflicts WHERE kept_blob_hash = ?", (DIGEST,))) == 1
        assert blobs.read_local(DIGEST) == PAYLOAD


def test_snapshot_delivered_note_delete_keeps_the_bytes(workers):
    a, b = _shared_note(workers)
    _attach(b)
    _delete_note_winning(a)
    with a.active():
        snapshot = engine.snapshot_chunk("notes", 0)
    with b.active():
        engine.apply_snapshot_chunk(snapshot)
        assert database.get_note("n1") is None
        assert [c["record_id"] for c in _attachment_conflicts(b)] == ["att1"]
        assert blobs.read_local(DIGEST) == PAYLOAD


def test_unpublished_and_pre_network_attachments_are_kept_too(workers):
    a, b = _shared_note(workers)
    old = b"attachment from before the network"
    with b.active():
        # Never scanned (no sync record), and an old row with no blob_hash.
        database.create_note_attachment(attachment_id="legacy", note_id="n1", filename="old.txt",
                                        file_path="/tmp/old.txt", size_bytes=len(old), data=old)
        b.sql("UPDATE note_attachments SET blob_hash = NULL WHERE id = 'legacy'")
    _delete_note_winning(a)
    exchange(a, b)
    with b.active():
        [conflict] = _attachment_conflicts(b)
        assert conflict["record_id"] == "legacy" and conflict["losing_clock"] == 0
        assert blobs.read_local(blobs.sha256(old)) == old


def test_an_attachment_the_deleting_device_published_goes_without_a_conflict(workers):
    a, b = _shared_note(workers)
    _attach(a)
    converge([a, b])
    with b.active():
        assert blobs.store_fetched(DIGEST, PAYLOAD)  # B fetched the bytes from A
    _delete_note_winning(a)
    converge([a, b])
    with b.active():
        assert b.sql("SELECT id FROM note_attachments") == []
        assert _attachment_conflicts(b) == []
        assert blobs.read_local(DIGEST) is None


def test_local_note_delete_is_unchanged(workers):
    a, b = _shared_note(workers)
    _attach(b)
    with b.active():
        database.delete_note("n1")
        engine.scan_all()
        assert b.sql("SELECT id FROM note_attachments") == []
        assert b.sql("SELECT id FROM sync_conflicts") == []
        assert blobs.read_local(DIGEST) is None
