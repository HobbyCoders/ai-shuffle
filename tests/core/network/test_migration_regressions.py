"""Upgrade regressions using real domain writes and independent worker databases."""
import json

import pytest

from app.core import artifact_store
from app.db import artifacts, connectors, database
from app.db import network as store
from app.network import blobs, engine, tokens, transport
from scripts import network_migrate
from tests.core.network.harness import Worker, join


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


def test_migration_preserves_queued_prompts_connector_routes_and_upload_bytes(workers):
    source, target = workers("source"), workers("target")
    with source.active():
        database.create_session(session_id="s-lab", profile_id="default", title="Queued work")
        database.create_session(session_id="s-other", profile_id="default", title="Other worker")
        source.sql("UPDATE sessions SET instance_id = 'lab', sdk_session_id = 'sdk-resume-lab' WHERE id = 's-lab'")
        source.sql("UPDATE sessions SET instance_id = 'other' WHERE id = 's-other'")
        connectors.create_connector("connector", "telegram", "Telegram", {}, [], runner_id="lab")
        source.sql("INSERT INTO queued_messages (id,session_id,content) VALUES ('q','s-lab','do not lose this')")
        source.sql("INSERT INTO queued_messages (id,session_id,content) VALUES ('other-q','s-other','not ours')")
        source.sql("INSERT INTO connector_sessions (id,connector_id,platform_chat_id,session_id) VALUES ('route','connector','chat-42','s-lab')")
        source.sql("INSERT INTO upload_blobs (id,session_id,source_path,filename,size,data) VALUES ('upload','s-lab','/old/a.txt','a.txt',3,?)", (b"abc",))
        bundle = json.loads(json.dumps(network_migrate.export_bundle("lab")))
        assert len(bundle["tables"].get("queued_messages", [])) == 1
        assert len(bundle["tables"].get("connector_sessions", [])) == 1
        assert len(bundle["tables"].get("upload_blobs", [])) == 1
        engine.create_network("Home", legacy_instance_id="source")
    join(source, target, legacy_instance_id="lab")
    with target.active():
        # The actual join path produces a catalog mirror without local runner
        # fields. Import must hydrate it, not discard the whole existing row.
        assert database.get_session("s-lab")["sdk_session_id"] is None
        original = json.dumps(bundle, sort_keys=True)
        network_migrate.import_bundle(bundle)
        assert database.get_session("s-lab")["sdk_session_id"] == "sdk-resume-lab"
        assert target.sql("SELECT content FROM queued_messages") == [{"content": "do not lose this"}]
        assert target.sql("SELECT platform_chat_id,session_id FROM connector_sessions") == [{"platform_chat_id": "chat-42", "session_id": "s-lab"}]
        assert bytes(target.sql("SELECT data FROM upload_blobs")[0]["data"]) == b"abc"
        assert json.dumps(bundle, sort_keys=True) == original
        assert network_migrate.import_bundle(bundle)["queued_messages"] == 0


def test_failed_artifact_write_remains_missing_and_can_retry(workers, monkeypatch, tmp_path):
    worker = workers("artifact")
    monkeypatch.setattr(artifact_store, "ARTIFACTS_DIR", tmp_path / "artifacts")
    payload = b"artifact bytes"
    digest = blobs.sha256(payload)
    with worker.active():
        artifacts.upsert_artifact(digest, len(payload), "text/plain", "result.txt", None)
        blobs.note_missing(digest, len(payload), "remote-holder")
        real_store = artifact_store.store_bytes
        with monkeypatch.context() as failure:
            def disk_full(*args, **kwargs):
                raise OSError("simulated disk full")
            failure.setattr(artifact_store, "store_bytes", disk_full)
            assert blobs.store_fetched(digest, payload) is False
        assert store.get_blob(digest)["present"] is False
        assert [row["hash"] for row in store.missing_blobs()] == [digest]
        assert artifact_store.store_bytes is real_store
        assert blobs.store_fetched(digest, payload) is True
        assert artifact_store.artifact_path(digest).read_bytes() == payload
        assert store.missing_blobs() == []


def test_unreferenced_blob_is_not_marked_present(workers):
    worker = workers("unreferenced")
    with worker.active():
        payload = b"metadata has not arrived yet"
        digest = blobs.sha256(payload)
        blobs.note_missing(digest, len(payload), "peer")
        assert blobs.store_fetched(digest, payload) is False
        assert store.get_blob(digest)["present"] is False


def test_shared_database_does_not_auto_enroll_one_identity(workers):
    from app.network import startup

    worker = workers("shared")
    with worker.active():
        database.create_session(session_id="legacy", profile_id="default")
        worker.sql("INSERT INTO installs (id,device_name) VALUES ('old-a','A'), ('old-b','B')")
        assert startup.initialize() is None
        assert store.get_state() is None
        assert database.get_session("legacy")["owner_device_id"] is None


def test_network_database_rejects_a_different_device_identity(workers, monkeypatch):
    from app.network import identity, startup

    source, other = workers("source-owner"), workers("wrong-owner")
    with source.active():
        engine.create_network("Private database")
        original = store.get_state()
        with monkeypatch.context() as wrong_identity:
            wrong_identity.setattr(identity, "load", lambda **kwargs: other.identity)
            with pytest.raises(RuntimeError, match="private database"):
                startup.initialize()
        assert store.get_state() == original


def test_import_dry_run_validates_constraints_and_rolls_back(workers):
    source, target = workers("dry-source"), workers("dry-target")
    with source.active():
        database.create_session(session_id="dry-session", profile_id="default")
        source.sql("UPDATE sessions SET instance_id='dry' WHERE id='dry-session'")
        source.sql("INSERT INTO queued_messages (id,session_id,content) VALUES ('dry-q','dry-session','Queued')")
        bundle = network_migrate.export_bundle("dry")
    with target.active():
        assert network_migrate.import_bundle(bundle, dry_run=True)["queued_messages"] == 1
        assert database.get_session("dry-session") is None
        assert target.sql("SELECT * FROM queued_messages") == []
        bundle["tables"]["connector_sessions"] = [{
            "id": "missing-connector", "connector_id": "not-imported",
            "platform_chat_id": "chat", "session_id": "dry-session",
        }]
        with pytest.raises(Exception, match="FOREIGN KEY"):
            network_migrate.import_bundle(bundle, dry_run=True)
        assert database.get_session("dry-session") is None


def test_export_refuses_undrained_turns(workers):
    worker = workers("busy")
    with worker.active():
        database.create_session(session_id="busy-session", profile_id="default")
        worker.sql("UPDATE sessions SET instance_id='busy' WHERE id='busy-session'")
        worker.sql("INSERT INTO turns (id,session_id,state,source,prompt) VALUES ('pending','busy-session','queued','user','Still queued')")
        with pytest.raises(RuntimeError, match="Drain"):
            network_migrate.export_bundle("busy")
