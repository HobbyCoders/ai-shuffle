"""Shared-database cluster migration: export an install's sessions, import into its own database."""
import json

from app.db import database
from app.db import session_events as events_db
from app.network import engine
from scripts import network_migrate
from tests.core.network.harness import converge, join



def test_export_import_moves_an_installs_sessions(workers, tmp_path):
    shared, fresh = workers("shared"), workers("fresh")
    # The shared database holds sessions from two installs: "desk" (the source) and "lab".
    with shared.active():
        database.create_session(session_id="s-desk", profile_id="default", project_id=None, title="Desk work")
        database.create_session(session_id="s-lab", profile_id="default", project_id=None, title="Lab work")
        shared.sql("UPDATE sessions SET instance_id = 'desk-1111', owner_device_id = NULL WHERE id = 's-desk'")
        shared.sql("UPDATE sessions SET instance_id = 'lab-2222', owner_device_id = NULL WHERE id = 's-lab'")
        events_db.append_events("s-lab", [{"type": "message.user", "payload": {"type": "message.user", "content": "lab hello"}}])
        database.add_session_message(session_id="s-lab", role="user", content="lab hello")
        database.create_tag(tag_id="t1", name="lab", color="#0f0")
        database.add_session_tag("s-lab", "t1")
        bundle = network_migrate.export_bundle("lab-2222")
        assert bundle["session_ids"] == ["s-lab"]
        assert {r["id"] for r in bundle["tables"]["sessions"]} == {"s-lab"}
        assert len(bundle["tables"]["session_events"]) == 1
        assert bundle["tables"]["session_messages"][0]["content"] == "lab hello"
        # JSON round trip like the CLI does.
        bundle = json.loads(json.dumps(bundle))
    # The lab install gets a fresh database, joins the network, then imports.
    with shared.active():
        engine.create_network("Home", device_name="Desk", legacy_instance_id="desk-1111")
    join(shared, fresh, name="Lab", legacy_instance_id="lab-2222")
    with shared.active():
        # The source never published the lab install's session as its own...
        assert database.get_session("s-lab")["owner_device_id"] == fresh.device_id
        assert database.get_session("s-desk")["owner_device_id"] == shared.device_id
    with fresh.active():
        # ...but handed the catalog row (and its events) over at join time.
        mirror = database.get_session("s-lab")
        assert mirror is not None and mirror["owner_device_id"] == fresh.device_id
        assert database.get_session("s-desk")["owner_device_id"] == shared.device_id
        assert network_migrate.import_bundle(bundle, dry_run=True)["session_messages"] == 1
        assert database.get_session_messages("s-lab") == []
        counts = network_migrate.import_bundle(bundle)
        assert counts["sessions"] == 0 and counts["session_messages"] == 1
        assert database.get_session_messages("s-lab")[0]["content"] == "lab hello"
        row = database.get_session("s-lab")
        assert row["owner_device_id"] == fresh.device_id and row["title"] == "Lab work"
        assert events_db.get_max_seq("s-lab") == 1
        # Importing again changes nothing.
        assert network_migrate.import_bundle(bundle)["sessions"] == 0
        # The new owner publishes its sessions; the source learns the handover.
        engine.scan_all()
    converge([shared, fresh])
    with shared.active():
        assert database.get_session("s-lab")["owner_device_id"] == fresh.device_id
        assert database.get_session("s-desk")["owner_device_id"] == shared.device_id
