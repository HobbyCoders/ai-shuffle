"""A note written by an API key stays that key's note on every device.

``notes.api_user_id`` decides who may read a note (the owner reaches all, a key
only its own). Without the column in the sync record a synced copy of a key's
note landed with NULL there, i.e. owner-only, and the key lost it elsewhere.
"""

from app.db import database
from app.network import engine, policy
from tests.core.network.harness import exchange, join


def test_notes_sync_record_carries_the_key():
    assert "api_user_id" in policy.collection("notes").fields


def test_key_note_keeps_its_key_on_other_devices(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        database.create_note(note_id="n-key", title="Key note", content="k", api_user_id="u-key")
        database.create_note(note_id="n-owner", title="Owner note", content="o")
        engine.scan_all()
    join(a, b)
    with b.active():
        rows = {r["id"]: r["api_user_id"] for r in b.sql("SELECT id, api_user_id FROM notes")}
        assert rows == {"n-key": "u-key", "n-owner": None}
        assert [n["id"] for n in database.get_all_notes(api_user_id="u-key")] == ["n-key"]

    # A later edit on A travels with the key as well.
    with a.active():
        database.update_note("n-key", content="k2")
        engine.scan_all()
    exchange(a, b)
    with b.active():
        assert b.sql("SELECT content, api_user_id FROM notes WHERE id='n-key'") == [
            {"content": "k2", "api_user_id": "u-key"}
        ]
