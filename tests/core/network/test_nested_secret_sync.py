"""Nested secrets cross independently keyed workers, including legacy repair."""
import asyncio
import json
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.api import connector_oauth, connector_plaid, network as network_api
from app.core import automation_service, connector_service, encryption
from app.db import database as db, network as store
from app.network import engine, policy
from tests.core.network.harness import Worker, exchange, exchange_with_gap, join as bootstrap

COLLECTIONS = ("connectors", "automations", "system_settings")
SECRET = "nested-test-credential"


@pytest.fixture(autouse=True)
def data_keys(monkeypatch):
    original = Worker.active
    keys = {}

    @contextmanager
    def active(worker):
        key = keys.setdefault(worker.name, encryption.Fernet.generate_key())
        with patch.object(encryption, "_encryption_key", key), original(worker):
            yield worker

    monkeypatch.setattr(Worker, "active", active)


def contact(sender, receiver):
    """Drive the real contact writer separately from signed presence delivery."""
    with sender.active():
        body = network_api.ExchangeRequest(watermarks=store.local_watermarks(),
            capabilities=engine.presence_of(sender.device_id)["capabilities"])
    with receiver.active():
        response = asyncio.run(network_api.peer_exchange(body, device_id=sender.device_id))
    with sender.active():
        store.touch_member(receiver.device_id, capabilities=response["capabilities"])


def join(a, b):
    # A newly admitted member is unknown until its own presence reaches peers.
    bootstrap(a, b)
    for worker in (a, b):
        with worker.active():
            engine.publish_presence()
    contact(b, a)
    exchange(a, b)
    exchange(b, a)
    exchange(a, b)
    with a.active():
        scan()  # the enlarged upgraded quorum republishes without a user edit
    exchange(a, b)


def produce():
    engine.publish_presence()
    db.create_connector("connector", "gmail", "Mail",
                        connector_service.encrypt_config({"client_secret": SECRET,
                            "refresh_token": SECRET, "label": "enc:literal"}, "gmail"), [])
    automation = asyncio.run(automation_service.create_automation(
        "Webhook", "webhook", {"secret": SECRET}, "default",
        triggers=[{"type": "webhook", "config": {"secret": SECRET}},
                  {"type": "webhook", "config": {"secret": SECRET + "-second"}},
                  {"type": "interval", "config": {"seconds": 60, "label": "enc:literal"}}]))
    request = SimpleNamespace(base_url="https://preview.test/")
    asyncio.run(connector_oauth.configure(connector_oauth.GoogleApp(
        client_id="test-client", client_secret=SECRET,
        redirect_uri="https://preview.test" + connector_oauth.CALLBACK), request))
    asyncio.run(connector_plaid.configure(connector_plaid.PlaidApp(
        client_id="test-client", client_secret=SECRET,
        redirect_uri="https://preview.test/plaid-link"), request))
    return automation["id"]


def scan():
    return sum(engine.scan_collection(name) for name in COLLECTIONS)


def check_secrets(automation_id):
    connector = db.get_connector("connector")
    assert connector["config"]["label"] == "enc:literal"
    for key in ("client_secret", "refresh_token"):
        assert encryption.is_encrypted(connector["config"][key])
        assert encryption.decrypt_value(connector["config"][key]) == SECRET
    automation = db.get_automation(automation_id)
    assert encryption.decrypt_value(automation["trigger_config"]["secret"]) == SECRET
    assert encryption.decrypt_value(automation["triggers"][0]["config"]["secret"]) == SECRET
    assert encryption.decrypt_value(automation["triggers"][1]["config"]["secret"]) == SECRET + "-second"
    assert automation["triggers"][2]["config"]["label"] == "enc:literal"
    for key in (connector_oauth.SETTING, connector_plaid.SETTING):
        assert encryption.decrypt_value(json.loads(db.get_system_setting(key))["client_secret"]) == SECRET
    return connector["config"]["client_secret"]


def legacy_collections(monkeypatch):
    for name in COLLECTIONS:
        coll = policy.collection(name)
        json_fields = tuple(f for f in coll.json_fields if not (name == "automations" and f == "triggers"))
        monkeypatch.setitem(policy.COLLECTIONS, name, replace(coll, map_secrets=None, json_fields=json_fields))


def test_produced_secrets_bootstrap_and_incremental_rekey(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Nested secrets")
        aid = produce()
        source_cipher = check_secrets(aid)
        assert scan() >= 4
    join(a, b)
    with b.active():
        assert check_secrets(aid) != source_cipher
        assert scan() == 0  # random local encryption must not cause echo writes
    with a.active():
        db.update_connector("connector", name="Edited on A")
        assert scan() == 1
    exchange(a, b)
    with b.active():
        check_secrets(aid)
        assert db.get_connector("connector")["name"] == "Edited on A"
        assert scan() == 0


@pytest.mark.parametrize("delivery", ["incremental", "snapshot"])
def test_rescan_repairs_previously_synced_unreadable_leaves(workers, monkeypatch, delivery):
    a, b = workers("a"), workers("b")
    with monkeypatch.context() as old:
        legacy_collections(old)
        with a.active():
            engine.create_network("Legacy repair")
            aid = produce()
            assert scan() >= 4
        join(a, b)
    with b.active():
        with pytest.raises(ValueError):
            encryption.decrypt_value(db.get_connector("connector")["config"]["client_secret"])
        assert scan() == 0  # unreadable mirrors cannot beat the good source
    with a.active():
        assert scan() == 4  # changed plaintext hashes republish without a user edit
        chunks = [engine.snapshot_chunk(name, 0) for name in COLLECTIONS]
    if delivery == "incremental":
        exchange(a, b)
    else:
        # Conflict capture must tolerate an unreadable local loser too.
        b.sql("UPDATE sync_records SET origin_device_id=? WHERE collection='connectors'", (b.device_id,))
        with b.active():
            for chunk in chunks:
                engine.apply_snapshot_chunk(chunk)
    with b.active():
        check_secrets(aid)
        assert scan() == 0


@pytest.mark.parametrize("delivery", ["ordered", "gap"])
def test_legacy_peer_cannot_overwrite_good_secrets_or_block_repair(workers, monkeypatch, delivery):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Mixed versions")
        aid = produce()
        scan()
    join(a, b)
    with b.active():
        before = check_secrets(aid)
    if delivery == "gap":
        with a.active():
            db.set_system_setting("gap_buffer_probe", "held-prefix")
            engine.scan_collection("system_settings")
    with monkeypatch.context() as old:
        legacy_collections(old)
        with a.active():
            db.update_connector("connector", name="Legacy edit")
            scan()
    deliver = exchange_with_gap if delivery == "gap" else exchange
    deliver(a, b)
    with b.active():
        assert check_secrets(aid) == before
        assert db.get_connector("connector")["name"] == "Mail"
    with a.active():
        scan()
    exchange(a, b)
    with b.active():
        check_secrets(aid)
        assert db.get_connector("connector")["name"] == "Legacy edit"


def test_nested_secrets_are_redacted_and_locked_storage_defers(workers, monkeypatch):
    a = workers("a")
    with a.active():
        engine.create_network("Redaction")
        produce()
        coll = policy.collection("connectors")
        row = a.sql("SELECT * FROM connectors WHERE id='connector'")[0]
        plain = engine.serialize_row(coll, row)
        assert plain["config"]["client_secret"] == SECRET
        assert engine.redact_secrets(plain)["config"] == engine.SECRET_PLACEHOLDER
        monkeypatch.setattr(encryption, "_encryption_key", None)
        with pytest.raises(engine.TransientApplyError):
            engine.serialize_row(coll, row)
        with pytest.raises(engine.TransientApplyError):
            engine.materialize(a.shim.cursor(), coll, "connector", plain)
        assert a.sql("SELECT * FROM connectors WHERE id='connector'")[0] == row


def test_local_only_setting_is_filtered_before_nested_handling():
    assert engine.serialize_row(policy.collection("system_settings"), {
        "key": "plugin_oauth:test", "value": json.dumps({"client_secret": "enc:unreadable"})}) is None
