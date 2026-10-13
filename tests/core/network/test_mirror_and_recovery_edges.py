"""Mirror hooks, recovery-key creation and small blob paths at their edges."""
import json

import pytest

from app.db import database
from app.db import network as store
from app.db.base import get_db
from app.network import blobs, catalog, engine, membership, recovery


def test_recovery_key_needs_a_network_and_a_current_key(workers):
    a = workers("a")
    with a.active():
        with pytest.raises(recovery.RecoveryError, match="not joined"):
            recovery.create()
        assert recovery.status() == {"configured": False, "stale": False, "created_at": None, "key_version": None}
        engine.create_network("Home", device_name="A")
        # A cached context behind the state row is reloaded under the lock.
        engine.context().key_version -= 1
        made = recovery.create()
        assert made["key_version"] == store.get_state()["key_version"]
        ctx = engine.context()
        ctx.keyring.pop(ctx.key_version)
        with pytest.raises(recovery.RecoveryError, match="unavailable"):
            recovery.create()
        with pytest.raises(recovery.RecoveryError, match="stale"):
            recovery.unwrap(made["recovery_key"], {"stale": True})
        with pytest.raises(recovery.RecoveryError, match="does not belong"):
            recovery.unwrap(made["recovery_key"], {"network_id": "net_other", "check": "x", "wrapped": {}})
        with pytest.raises(recovery.RecoveryError, match="does not open"):
            recovery.unwrap(made["recovery_key"], {"network_id": ctx.network_id})


def test_mirrored_session_drops_references_this_worker_cannot_see(workers):
    a = workers("a")
    with a.active():
        database.create_session(session_id="parent", profile_id="default", title="p")
        database.create_session(session_id="s1", profile_id="default", title="t")
        a.sql("INSERT INTO api_users (id, name, api_key_hash, is_active) VALUES ('u1', 'U', 'h', 1)")
        a.sql("UPDATE sessions SET api_user_id = 'u1' WHERE id = 's1'")
        with get_db() as conn:
            cursor = conn.cursor()
            kept = catalog.prepare_session_mirror(cursor, {
                "id": "s1", "project_id": "nowhere", "profile_id": "ghost", "api_user_id": None,
                "parent_session_id": "parent"})
            assert kept["project_id"] is None and kept["profile_id"] is None
            assert kept["api_user_id"] == "u1" and kept["parent_session_id"] == "parent"
            dropped = catalog.prepare_session_mirror(cursor, {
                "id": "new", "api_user_id": "u-gone", "parent_session_id": "gone", "profile_id": "default"})
            assert dropped["api_user_id"] is None and dropped["parent_session_id"] is None
            assert dropped["profile_id"] == "default"


def test_event_counter_follows_a_restarted_owner_stream(workers):
    a = workers("a")
    with a.active():
        database.create_session(session_id="s1", profile_id="default", title="t")
        for seq, kind, payload in ((1, "message.user", {"content": "hello"}),
                                   (2, "message.assistant", {"content": "hi there", "kind": "text"}),
                                   (3, "message.assistant", {"content": "thinking", "kind": "thinking"}),
                                   (4, "tool.call", {"content": ""})):
            a.sql("INSERT INTO session_events (id, session_id, seq, event_type, payload) VALUES (?, 's1', ?, ?, ?)",
                  (f"e{seq}", seq, kind, json.dumps(payload)))
        assert catalog.transcript_text("s1") == "User: hello\n\nAssistant: hi there"
        with get_db() as conn:
            cursor = conn.cursor()
            catalog.after_event_mirrored(cursor, {"session_id": "s1", "seq": 0})
            catalog.after_event_mirrored(cursor, {"session_id": "s1", "seq": 9})
            assert store.catalog_cursor(cursor, "s1") == 9
            # The owner cleared the transcript and restarted at seq 2.
            catalog.after_event_mirrored(cursor, {"session_id": "s1", "seq": 2})
            assert store.catalog_cursor(cursor, "s1") == 4
        assert a.sql("SELECT next_seq FROM session_event_counters WHERE session_id='s1'")[0]["next_seq"] == 4


def test_hydrate_publishes_only_owned_sessions_of_an_enrolled_worker(workers):
    a = workers("a")
    with a.active():
        assert catalog.hydrate("s1") == 0
        engine.create_network("Home", device_name="A")
        database.create_session(session_id="s1", profile_id="default", title="t")
        a.sql("UPDATE sessions SET owner_device_id = 'dev_elsewhere', instance_id = 'elsewhere' WHERE id = 's1'")
        assert catalog.hydrate("s1") == 0


async def test_blob_paths_without_a_network_or_with_local_bytes(workers):
    a = workers("a")
    with a.active():
        assert await blobs.fetch_one("0" * 64) is False
        assert await blobs.fetch_missing() == 0
        engine.create_network("Home", device_name="A")
        assert await blobs.fetch_missing() == 0
        database.create_note(note_id="n1", title="Note", content="body")
        database.create_note_attachment(attachment_id="att1", note_id="n1", filename="x.txt", file_path="/tmp/x.txt",
                                        content_type="text/plain", size_bytes=5, data=b"hello")
        digest = blobs.sha256(b"hello")
        blobs.note_missing(digest, 5, "dev_peer")
        assert store.get_blob(digest)["present"] is True
        blobs.register_present(digest, 5)
        assert blobs.read_local_range(digest, 1, 3) == (b"ell", 5)
        assert blobs.read_local_range("f" * 64, 0, 3) is None
        # Nobody online holds a missing blob: nothing to fetch, nothing raised.
        blobs.note_missing("e" * 64, 3, "dev_peer")
        assert await blobs.fetch_missing() == 0
        assert await blobs.fetch_one("e" * 64) is False


def test_record_signature_of_the_wrong_type_does_not_verify():
    assert membership._verify({"signature": 5, "type": "x"}, b"\0" * 32) is False
