"""A conflict never keeps an encrypted column in plaintext, and only the owner lists conflicts."""
import hashlib
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import network as network_api
from app.core import encryption
from app.db import database
from app.db import network as store
from app.network import engine
from tests.core.network.harness import converge, join

SECRETS = ("sk-ant-A-SECRET-111", "sk-ant-B-1", "sk-ant-B-SECRET-222")


def test_losing_remote_secret_is_redacted_and_conflicts_are_owner_only(workers, monkeypatch):
    monkeypatch.setattr(encryption, "_encryption_key", encryption.Fernet.generate_key())
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        database.set_system_setting("anthropic_api_key", encryption.encrypt_value("sk-ant-ORIGINAL"))
        engine.scan_all()
    join(a, b)
    converge([a, b])
    # Partitioned edits of the same encrypted setting; B's clock ends ahead,
    # so A's write loses on B (the remote-loser path) and on A (local loser).
    with a.active():
        database.set_system_setting("anthropic_api_key", encryption.encrypt_value(SECRETS[0]))
        engine.scan_all()
    with b.active():
        database.set_system_setting("anthropic_api_key", encryption.encrypt_value(SECRETS[1]))
        engine.scan_all()
        database.set_system_setting("anthropic_api_key", encryption.encrypt_value(SECRETS[2]))
        engine.scan_all()
    converge([a, b])
    stored = []
    for w in (a, b):
        with w.active():
            stored += [row["losing_record"] for row in w.sql("SELECT losing_record FROM sync_conflicts")]
    assert stored, "the partitioned edits must leave a conflict"
    at_rest = json.dumps(stored, default=str)
    assert not any(secret in at_rest for secret in SECRETS), at_rest
    assert "<secret>" in at_rest

    with b.active():
        database.create_api_user("key-1", "Restricted key", hashlib.sha256(b"sk-user-key").hexdigest(),
                                 profile_ids=["default"])
        app = FastAPI()
        app.include_router(network_api.router)
        client = TestClient(app, client=("203.0.113.9", 4000))
        resp = client.get("/api/v1/network/conflicts", headers={"Authorization": "Bearer sk-user-key"})
        assert resp.status_code == 403


def test_startup_scrub_redacts_conflicts_stored_by_older_builds(workers):
    a = workers("a")
    with a.active():
        engine.create_network("Home")
        with store.get_db() as conn:
            store.add_conflict(conn.cursor(), collection="system_settings", record_id="anthropic_api_key",
                               losing_clock=1, losing_device_id="x", winning_clock=2, winning_device_id=a.device_id,
                               losing_record={"key": "anthropic_api_key", "value": "sk-ant-OLD", "_enc": ["value"]})
            store.add_conflict(conn.cursor(), collection="tags", record_id="t", losing_clock=1, losing_device_id="x",
                               losing_record={"name": "old"}, winning_clock=2, winning_device_id=a.device_id)
        assert engine.redact_stored_conflicts() == 1
        assert engine.redact_stored_conflicts() == 0
        records = sorted((c["record_id"], c["losing_record"]) for c in store.list_conflicts())
        assert records == [("anthropic_api_key", {"key": "anthropic_api_key", "value": "<secret>"}),
                           ("t", {"name": "old"})]
