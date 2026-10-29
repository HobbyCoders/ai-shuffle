"""Sender keys and attention receipts follow the owner's encrypted network."""
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.core import encryption
from app.core.config import settings
from app.db import database as db
from app.db.base import get_db
from app.network import catalog, engine, policy
from app.services import push_credentials, attention_dispatcher as attention
from app.services.push_notification import NotificationResult
from tests.core.network.harness import Worker, exchange, join


@pytest.fixture(autouse=True)
def separate_local_keys(monkeypatch):
    original = Worker.active
    keys = {}
    @contextmanager
    def active(worker):
        key = keys.setdefault(worker.name, encryption.Fernet.generate_key())
        with patch.object(settings, "instance_id", worker.name), patch.object(encryption, "_encryption_key", key), original(worker):
            yield worker
    monkeypatch.setattr(Worker, "active", active)


def scan():
    engine.scan_all(include_catalog=False, collections=["system_settings"])


def test_sender_secret_synced_encrypted_rotated_and_deleted(workers):
    a, b = workers("a"), workers("b")
    assert policy.is_shared_setting_key(push_credentials.SETTING_KEY)
    assert "value" in policy.collection("system_settings").encrypted
    with a.active():
        engine.create_network("Push tests")
        encrypted = encryption.encrypt_value(json.dumps({"test": "sender-account-one"}))
        db.set_system_setting(push_credentials.SETTING_KEY, encrypted)
        scan()
    join(a, b)
    with b.active():
        stored = db.get_system_setting(push_credentials.SETTING_KEY)
        assert encryption.is_encrypted(stored) and stored != encrypted
        assert push_credentials.load_account()[1] == {"test": "sender-account-one"}
    with a.active():
        db.set_system_setting(push_credentials.SETTING_KEY, encryption.encrypt_value(json.dumps({"test": "rotated"})))
        scan()
    exchange(a, b)
    with b.active():
        assert push_credentials.load_account()[1] == {"test": "rotated"}
    with a.active():
        db.delete_system_setting(push_credentials.SETTING_KEY)
        scan()
    exchange(a, b)
    with b.active():
        assert db.get_system_setting(push_credentials.SETTING_KEY) is None


@pytest.mark.asyncio
async def test_owner_receipt_catalog_sync_mirror_and_execution_transfer_do_not_realert(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    sender = AsyncMock()
    sender.send_to_all_devices.return_value = [NotificationResult(True)]
    dispatcher = attention.AttentionDispatcher(sender)
    monkeypatch.setattr(dispatcher, "private_needs", lambda sid: set())
    with a.active():
        engine.create_network("Attention tests")
        db.create_session("push-transfer", "default", title="Plan", is_active=True)
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET owner_device_id=%s WHERE id=%s", (a.device_id, "push-transfer"))
        await dispatcher.reconcile("push-transfer", explicit_id="one-tool-use")
        catalog.publish_owned()
        command = attention.current_command("push-transfer", a.device_id)
    join(a, b)
    with b.active():
        await dispatcher.reconcile("push-transfer", explicit_id="one-tool-use")
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT push_attention_state FROM sessions WHERE id=%s", ("push-transfer",))
            state = c.fetchone()["push_attention_state"]
            assert "one-tool-use" in str(state)
            c.execute("UPDATE sessions SET owner_device_id=%s WHERE id=%s", (b.device_id, "push-transfer"))
        await dispatcher.reconcile("push-transfer", explicit_id="one-tool-use")
        assert attention.current_command("push-transfer", b.device_id)["episode"] == command["episode"]
    assert sender.send_to_all_devices.await_count == 1
