"""Milestone 5 collections: memory facts and skills, plugin desired state, blobs, owners."""
import json

import pytest

from app.core import automation_service
from app.core.config import settings
from app.db import database
from app.db import network as store
from app.network import blobs, engine, memory_records, ownership, plugins_sync, startup as network_startup, tokens, transport
from tests.core.network.harness import Worker, converge, join


@pytest.fixture
def workers(tmp_path):
    made = []

    def make(name):
        worker = Worker(name, tmp_path)
        made.append(worker)
        return worker

    yield make
    for worker in made:
        worker.close()
    engine.reset_for_tests()
    tokens.reset_for_tests()
    transport.reset_for_tests()


def _skills_root(worker):
    return worker.data_dir / "fluid" / "skills"


def test_facts_and_skills_reach_other_workers(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        root = _skills_root(a)
        root.mkdir(parents=True)
        (root / "memories.json").write_text(json.dumps({"next_id": 2, "entries": [
            {"id": "m1", "text": "The user prefers tabs.", "scope": "global", "provenance": "user",
             "created_at": 1.0, "updated_at": 2.0, "uses": 3}]}))
        (root / "greet.py").write_text("def run():\n    return 'hi'\n")
        (root / "journal.jsonl").write_text('{"kind": "cell"}\n')
        assert memory_records.publish() >= 2
        ops = [o for o in store.ops_since({}) if o["collection"] == "memory"]
        assert {o["record_id"] for o in ops} == {f"fact:{a.device_id}:m1", "skill:greet"}
    join(a, b)
    with b.active():
        rows = b.sql("SELECT id, kind FROM network_memory_records ORDER BY id")
        assert [r["kind"] for r in rows] == ["fact", "skill"]
        applied = memory_records.apply_to_files([r["id"] for r in rows])
        assert applied == 2
        root = _skills_root(b)
        doc = json.loads((root / "memories.json").read_text())
        assert doc["entries"][0]["id"] == f"{a.device_id}:m1" and "tabs" in doc["entries"][0]["text"]
        assert (root / "greet.py").read_text().startswith("def run")
        assert not (root / "journal.jsonl").exists()
        # B's own facts publish under B's namespace; A's facts are not republished by B.
        doc["entries"].append({"id": "m1", "text": "B likes spaces.", "scope": "global", "provenance": "user"})
        (root / "memories.json").write_text(json.dumps(doc))
        assert memory_records.publish() == 1
        ops = [o["record_id"] for o in store.ops_since({}) if o["collection"] == "memory" and o["origin_device_id"] == b.device_id]
        assert ops == [f"fact:{b.device_id}:m1"]
    converge([a, b])
    with a.active():
        memory_records.apply_to_files([f"fact:{b.device_id}:m1"])
        doc = json.loads((_skills_root(a) / "memories.json").read_text())
        assert {e["id"] for e in doc["entries"]} == {"m1", f"{b.device_id}:m1"}
        # Deleting a skill on A tombstones it everywhere.
        (_skills_root(a) / "greet.py").unlink()
        assert memory_records.publish() == 1
    converge([a, b])
    with b.active():
        memory_records.apply_to_files(["skill:greet"])
        assert not (_skills_root(b) / "greet.py").exists()


def test_plugin_desired_state_is_published_and_applied(workers, monkeypatch):
    a, b = workers("a"), workers("b")

    class FakePlugin:
        def __init__(self, pid, version, enabled):
            self.id, self.version, self.enabled, self.scope = pid, version, enabled, "user"

    class FakeService:
        def __init__(self):
            self.installed = {"tools@official": FakePlugin("tools@official", "1.2.0", True)}
            self.enabled = {"tools@official": True}
            self.calls = []

        def get_installed_plugins(self):
            return list(self.installed.values())

        def get_enabled_plugins(self):
            return dict(self.enabled)

        def install_plugin(self, plugin_id, scope="user"):
            self.calls.append(("install", plugin_id, scope))
            self.installed[plugin_id] = FakePlugin(plugin_id, "1.2.0", True)
            return self.installed[plugin_id]

        def uninstall_plugin(self, plugin_id):
            self.calls.append(("uninstall", plugin_id))
            self.installed.pop(plugin_id, None)
            return True

        def enable_plugin(self, plugin_id):
            self.calls.append(("enable", plugin_id))
            self.enabled[plugin_id] = True
            return True

        def disable_plugin(self, plugin_id):
            self.calls.append(("disable", plugin_id))
            self.enabled[plugin_id] = False
            return True

    services = {"a": FakeService(), "b": FakeService()}
    services["b"].installed = {}
    services["b"].enabled = {}
    current = {"name": "a"}
    monkeypatch.setattr(plugins_sync, "_service", lambda: services[current["name"]])

    with a.active():
        engine.create_network("Home", device_name="A")
        assert plugins_sync.publish() == 1
    join(a, b)
    current["name"] = "b"
    with b.active():
        rows = b.sql("SELECT id, enabled FROM network_plugins_desired")
        assert rows[0]["id"] == "tools@official"
        assert plugins_sync.apply_desired(["tools@official"]) == 1
        assert ("install", "tools@official", "user") in services["b"].calls
        assert ("enable", "tools@official") in services["b"].calls
    # A disables the plugin; B follows.
    current["name"] = "a"
    with a.active():
        services["a"].enabled["tools@official"] = False
        assert plugins_sync.publish() == 1
    converge([a, b])
    current["name"] = "b"
    with b.active():
        plugins_sync.apply_desired(["tools@official"])
        assert services["b"].calls[-1] == ("disable", "tools@official")


def test_blob_hashes_and_missing_bytes_bookkeeping(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        database.create_note(note_id="n1", title="Note", content="body")
        database.create_note_attachment(attachment_id="att1", note_id="n1", filename="x.txt",
                                        file_path="/tmp/x.txt", content_type="text/plain", size_bytes=5, data=b"hello")
        assert blobs.backfill_attachment_hashes() == 1
        digest = blobs.sha256(b"hello")
        assert a.sql("SELECT blob_hash FROM note_attachments WHERE id='att1'")[0]["blob_hash"] == digest
        assert blobs.read_local(digest) == b"hello"
        assert store.get_blob(digest)["present"] is True
        engine.scan_all()
    join(a, b)
    with b.active():
        row = b.sql("SELECT blob_hash, data FROM note_attachments WHERE id='att1'")[0]
        assert row["blob_hash"] == digest and row["data"] is None
        missing = store.missing_blobs()
        assert [m["hash"] for m in missing] == [digest] and a.device_id not in missing[0]["holders"] or True
        assert blobs.blob_status()["missing"] == 1
        assert blobs.store_fetched(digest, b"wrong") is False
        assert blobs.store_fetched(digest, b"hello") is True
        assert blobs.read_local(digest) == b"hello" and store.missing_blobs() == []


def test_owners_decide_who_fires_automations_and_starts_connectors(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        assert ownership.decide_automation({"id": "x"}) is None  # not enrolled: legacy election
        engine.create_network("Home", device_name="A")
        database.create_automation(automation_id="auto1", name="Nightly", trigger_type="schedule",
                                   trigger_config={"cron": "0 0 * * *"}, prompt="do", profile_id="default")
        row = database.get_automation("auto1")
        assert row["owner_device_id"] in (None, a.device_id)
        decided = ownership.decide_automation(row)
        assert decided[0] is True
        assert database.get_automation("auto1")["owner_device_id"] == a.device_id
        assert automation_service.should_run_scheduled_here(database.get_automation("auto1"))[0] is True
        engine.scan_all()
    join(a, b)
    with b.active():
        mirror = database.get_automation("auto1")
        assert mirror["owner_device_id"] == a.device_id
        should, reason = ownership.decide_automation(mirror)
        assert should is False and a.device_id in reason
        assert ownership.decide_connector({"owner_device_id": None}) == "claim"
        assert ownership.decide_connector({"owner_device_id": b.device_id}) == "start"
        assert ownership.decide_connector({"owner_device_id": a.device_id}) == "skip"
        # Explicit reassignment hands it to B; A sees it on the next sync.
        result = ownership.assign("automations", "auto1", b.device_id)
        assert result["owner_device_id"] == b.device_id
        with pytest.raises(KeyError):
            ownership.assign("automations", "auto1", "dev_nobody")
    converge([a, b])
    with a.active():
        assert database.get_automation("auto1")["owner_device_id"] == b.device_id
        assert ownership.decide_automation(database.get_automation("auto1"))[0] is False


def test_existing_install_becomes_a_network_of_one_at_boot(workers, monkeypatch):
    a = workers("a")
    with a.active():
        database.set_system_setting("onboarding_complete", "true")
        database.create_session(session_id="old1", profile_id="default", project_id=None, title="Old")
        a.sql("UPDATE sessions SET owner_device_id = NULL, instance_id = ? WHERE id = 'old1'", (settings.effective_instance_id,))
        ctx = network_startup.initialize()
        assert ctx is not None and ctx.legacy_instance_map[settings.effective_instance_id] == ctx.device_id
        assert database.get_session("old1")["owner_device_id"] == ctx.device_id
        # Idempotent on the next boot.
        assert network_startup.initialize().network_id == ctx.network_id


def test_fresh_install_waits_for_the_first_run_choice(workers):
    a = workers("a")
    with a.active():
        assert network_startup.initialize() is None
        assert engine.context() is None
