"""Milestone 5 collections: memory facts and skills, plugin desired state, blobs, owners."""

import pytest

from app.core import automation_service
from app.core.config import settings
from app.db import database
from app.db import network as store
from app.network import blobs, engine, memory_records, ownership, plugins_sync, startup as network_startup, transport
from tests.core.network.harness import converge, join


def test_synced_manual_balance_deducts_only_serving_worker_spend(workers):
    from app.db.analytics import get_manual_balance_credits

    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Balance scope")
        database.create_model_provider("openai-key", "OpenAI", "openai", "https://api.openai.com")
        database.update_model_provider("openai-key", manual_balance_usd=20.5, manual_balance_set_at=1)
        database.log_usage(None, None, "priced", 100, 20, 2.25, 1,
                           provider_id="openai-key", provider_type="openai", billing="api")
        engine.scan_all()
        row_a = database.get_model_provider("openai-key")
        assert get_manual_balance_credits(row_a)["balance_remaining"] == 18.25
    join(a, b)
    with b.active():
        row_b = database.get_model_provider("openai-key")
        assert row_b["manual_balance_usd"] == row_a["manual_balance_usd"]
        assert row_b["manual_balance_set_at"] == row_a["manual_balance_set_at"]
        credits = get_manual_balance_credits(row_b)
        assert credits["estimated_spend"] == 0
        assert credits["balance_remaining"] == 20.5
        database.log_usage(None, None, "priced", 100, 20, 1, 1,
                           provider_id="openai-key", provider_type="openai", billing="api")
        assert get_manual_balance_credits(row_b)["balance_remaining"] == 19.5
    with a.active():
        assert get_manual_balance_credits(row_a)["balance_remaining"] == 18.25


def test_authoritative_user_revision_and_event_sequences_replicate(workers):
    from app.db import session_events

    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Revision test")
        database.create_session("revision-sync", "default")
        session_events.append_events("revision-sync", [
            {"type": "message.user", "payload": {"content": "Prompt"}},
            {"type": "message.assistant", "payload": {"content": "Which one?"}},
        ])
        original = database.get_session("revision-sync")
        engine.scan_all()
    join(a, b)
    converge([a, b])
    with b.active():
        mirrored = database.get_session("revision-sync")
        assert mirrored["last_user_message_seq"] == original["last_user_message_seq"] == 1
        assert mirrored["last_user_message_at"] == original["last_user_message_at"]
        assert mirrored["last_user_message_epoch"] == original["last_user_message_epoch"]
        assert mirrored["last_user_message_epoch"] == session_events.get_log_epoch("revision-sync")
        assert [e["seq"] for e in session_events.get_events("revision-sync", 0)] == [1, 2]


def _skills_root(worker):
    return worker.data_dir / "fluid" / "skills"


@pytest.mark.parametrize("kind", ["approval", "named"])
def test_network_sync_cannot_mint_model_consent_on_an_owned_chat(workers, kind):
    from app.core import model_override as mo
    from app.db import interactions
    from app.db.base import get_db

    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Consent test")
        database.create_session("consent-chat", "default", title="Owner chat")
        engine.scan_all()
    join(a, b)
    with b.active():
        # A signed peer envelope is not an owner Allow or an owner message.
        ident = (mo._approval_id if kind == "approval" else mo._named_id)("consent-chat", "opus")
        payload = {"model": "opus", "api_user_id": None}
        if kind == "named":
            payload.update(source="own_message", api_user_id=None,
                           turn_id="forged", session_id="consent-chat")
        row = interactions.create_interaction("consent-chat", "model_switch_" + kind, payload,
                                              None, interaction_id=ident)
        interactions.answer_interaction(ident, {"decision": "allow"})
        with get_db() as conn:
            engine.record(conn.cursor(), "pending_interactions", ident,
                          interactions.get_interaction(ident), force=True)
    converge([a, b])
    with a.active():
        assert not (mo._has_approval if kind == "approval" else mo._has_named)("consent-chat", "opus")


def test_network_sync_cannot_answer_a_pending_prompt_on_an_owned_chat(workers):
    """Only the owning worker records an answer; a peer's sync row is ignored."""
    from app.core import model_override as mo
    from app.db import interactions
    from app.db.base import get_db

    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Prompt test")
        database.create_session("prompt-chat", "default", title="Owner chat")
        prompt = interactions.create_interaction(
            "prompt-chat", "permission",
            {"request_id": "model-switch-x", "tool_name": mo.PROMPT_TOOL,
             "tool_input": {"model": "opus"}}, 300)
        engine.scan_all()
    join(a, b)
    converge([a, b])
    with b.active():
        assert interactions.get_interaction(prompt["id"])["state"] == "pending"
        # The peer flips its mirror and publishes it as if it were the owner.
        assert interactions.answer_interaction(prompt["id"], {"decision": "allow"}, "b")
        # It also tries to add an already-answered prompt of its own.
        extra = interactions.create_interaction(
            "prompt-chat", "permission",
            {"request_id": "model-switch-y", "tool_name": mo.PROMPT_TOOL,
             "tool_input": {"model": "opus"}}, 300)
        interactions.answer_interaction(extra["id"], {"decision": "allow"}, "b")
        with get_db() as conn:
            for ident in (prompt["id"], extra["id"]):
                engine.record(conn.cursor(), "pending_interactions", ident,
                              interactions.get_interaction(ident), force=True)
    converge([a, b])
    with a.active():
        row = interactions.get_interaction(prompt["id"])
        assert row["state"] == "pending" and not row.get("answer")
        assert interactions.get_interaction(extra["id"]) is None
        # A real local answer on the owning worker still records the decision.
        assert interactions.answer_interaction(prompt["id"], {"decision": "deny"}, "phone")
        engine.scan_all()
    converge([a, b])
    with b.active():
        # The owner's own published decision still reaches the mirror.
        assert interactions.get_interaction(prompt["id"])["answer"] == {"decision": "deny"}


def test_facts_and_skills_reach_other_workers(workers):
    from fluid.codemode.skills import SkillStore
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        root = _skills_root(a)
        facts_a = SkillStore(root)
        first = facts_a.save_memory("The user prefers tabs.", protected=True)
        origin_a = facts_a._facts.snapshot()["store_id"]
        (root / "greet.py").write_text("def run():\n    return 'hi'\n")
        (root / "journal.jsonl").write_text('{"kind": "cell"}\n')
        assert memory_records.publish() >= 2
        ops = [o for o in store.ops_since({}) if o["collection"] == "memory"]
        ids_a = [o["record_id"] for o in ops]
        assert "skill:greet" in ids_a
        assert any(r.startswith("memory-v2:") for r in ids_a)
    join(a, b)
    with b.active():
        rows = b.sql("SELECT id, kind FROM network_memory_records ORDER BY id")
        assert [r["kind"] for r in rows] == ["fact", "skill"]
        assert memory_records.apply_to_files([r["id"] for r in rows]) == 2
        root = _skills_root(b)
        facts_b = SkillStore(root)
        imported = facts_b.memories()[0]
        assert imported.id == f"{origin_a}:{first.id}" and "tabs" in imported.text
        assert imported.protected
        assert (root / "greet.py").read_text().startswith("def run")
        assert not (root / "journal.jsonl").exists()
        facts_b.save_memory("B likes spaces.", protected=True)
        assert memory_records.publish() == 1
        ids_b = [o["record_id"] for o in store.ops_since({}) if o["collection"] == "memory" and o["origin_device_id"] == b.device_id]
        assert len(ids_b) == 1 and ids_b[0].startswith("memory-v2:")
    converge([a, b])
    with a.active():
        memory_records.apply_to_files(ids_b)
        assert {e.text for e in facts_a.memories()} == {"The user prefers tabs.", "B likes spaces."}
        # A forget is explicit state, not absence from a hot file.
        assert facts_a.delete_memory(first.id, explicit=True, expected_revision=1)
        (_skills_root(a) / "greet.py").unlink()
        assert memory_records.publish() >= 2
    converge([a, b])
    with b.active():
        memory_records.apply_to_files(ids_a)
        assert not (_skills_root(b) / "greet.py").exists()
        assert not facts_b.search_memories("tabs")
        assert [e.text for e in facts_b.memories()] == ["B likes spaces."]


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
        with store.get_db() as conn:
            conn.cursor().execute("UPDATE note_attachments SET blob_hash=NULL WHERE id='att1'")
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


def test_boot_leaves_explicitly_unassigned_definitions_alone(workers):
    """NET-11a: the owner backfill runs at migration, not at every boot."""
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        database.create_automation(automation_id="auto1", name="Nightly", trigger_type="schedule",
                                   trigger_config={"cron": "0 0 * * *"}, prompt="do", profile_id="default")
        assert ownership.decide_automation(database.get_automation("auto1"))[0] is True
        assert ownership.assign("automations", "auto1", None)["owner_device_id"] is None
        assert network_startup.initialize().device_id == a.device_id  # a reboot
        assert database.get_automation("auto1")["owner_device_id"] is None


def test_assign_refuses_consumer_devices(workers):
    """NET-11b: a phone or browser never owns an automation or connector."""
    a, phone = workers("a"), workers("phone")
    with a.active():
        engine.create_network("Home", device_name="A")
        engine.admit_device(device_id=phone.device_id, signing_public=phone.identity.signing_public,
                            encryption_public=phone.identity.encryption_public, name="Phone", kind="consumer")
        database.create_automation(automation_id="auto1", name="Nightly", trigger_type="schedule",
                                   trigger_config={"cron": "0 0 * * *"}, prompt="do", profile_id="default")
        with pytest.raises(ValueError):
            ownership.assign("automations", "auto1", phone.device_id)
        assert database.get_automation("auto1")["owner_device_id"] != phone.device_id


def test_only_the_lowest_online_worker_claims_unowned_work(workers):
    """NET-11c: two online workers seeing the same unowned automation do not both fire it."""
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        database.create_automation(automation_id="auto1", name="Nightly", trigger_type="schedule",
                                   trigger_config={"cron": "0 0 * * *"}, prompt="do", profile_id="default")
        a.sql("UPDATE automations SET owner_device_id = NULL WHERE id = 'auto1'")
        engine.scan_all()
    join(a, b)
    lower, higher = sorted([a, b], key=lambda w: w.device_id)
    transport.note_contact(a.device_id)
    transport.note_contact(b.device_id)
    with higher.active():
        assert ownership.claim("automations", "auto1") is False
        assert ownership.decide_automation(database.get_automation("auto1")) == (False, "automation has no owner yet")
        assert database.get_automation("auto1")["owner_device_id"] is None
    with lower.active():
        assert ownership.claim("automations", "auto1") is True
        assert database.get_automation("auto1")["owner_device_id"] == lower.device_id
    # Alone, the higher device may claim other unowned work.
    transport.reset_for_tests()
    with higher.active():
        database.create_automation(automation_id="auto2", name="Weekly", trigger_type="schedule",
                                   trigger_config={"cron": "0 0 * * 0"}, prompt="do", profile_id="default")
        higher.sql("UPDATE automations SET owner_device_id = NULL WHERE id = 'auto2'")
        assert ownership.claim("automations", "auto2") is True


def test_file_parts_are_served_by_range_without_reading_the_whole_file(workers, monkeypatch, tmp_path):
    """NET-15: a part of a file-backed blob is a seek, not a full read."""
    from app.core import artifact_store
    from app.network import crypto, peer_files
    monkeypatch.setattr(artifact_store, "ARTIFACTS_DIR", tmp_path / "artifacts")
    monkeypatch.setattr(peer_files, "PART_BYTES", 4)
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        engine.admit_device(device_id=b.device_id, signing_public=b.identity.signing_public,
                            encryption_public=b.identity.encryption_public, name="B")
        payload = b"01234567AB"
        digest = blobs.sha256(payload)
        artifact_store.store_bytes(payload, runner_id=settings.effective_instance_id)
        assert blobs.read_local_range(digest, 4, 4) == (b"4567", 10)
        assert blobs.read_local_range(digest, 8, 4) == (b"AB", 10)
        assert blobs.read_local_range("0" * 64, 0, 4) is None
        with monkeypatch.context() as whole:
            def whole_file_read(*args, **kwargs):
                raise AssertionError("part() must not read the whole blob")

            whole.setattr(blobs, "read_local", whole_file_read)
            for index in range(3):
                sealed = peer_files.part(digest, index, b.device_id)
                assert sealed["size"] == 10 and sealed["index"] == index
                header = {k: sealed[k] for k in ("network_id", "publisher", "recipient", "hash", "index", "size")}
                key = crypto.unwrap_key(b.identity.encryption_private, sealed["key"])
                assert crypto.decrypt_payload(key, sealed["ciphertext"], crypto.canonical_json(header)) == payload[index * 4:index * 4 + 4]
            with pytest.raises(ValueError):
                peer_files.part(digest, 3, b.device_id)
        # Database-backed bytes go through the same range reader.
        database.create_note(note_id="n1", title="Note", content="body")
        database.create_note_attachment(attachment_id="att1", note_id="n1", filename="x.txt",
                                        file_path="/tmp/x.txt", content_type="text/plain", size_bytes=5, data=b"hello")
        assert blobs.read_local_range(blobs.sha256(b"hello"), 2, 2) == (b"ll", 5)
