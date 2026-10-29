"""Retired sender keys never sync; attention receipts still follow ownership."""
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from app.core import encryption
from app.core.config import settings
from app.db import database as db
from app.db.base import get_db
from app.network import catalog, engine, policy
from app.services import attention_dispatcher as attention
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


def test_sender_key_excluded_even_from_an_older_peer(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    key = "fcm_service_account"
    assert not policy.is_shared_setting_key(key)
    assert "value" in policy.collection("system_settings").encrypted
    # Billing and other encrypted settings keep their existing policy.
    assert policy.is_shared_setting_key("provider_billing_key:test")
    with a.active():
        engine.create_network("Push tests")
        db.set_system_setting(key, encryption.encrypt_value("retired-test-sender"))
        scan()
        with get_db() as conn:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) AS n FROM sync_records WHERE collection='system_settings' AND record_id=%s", (key,))
            assert c.fetchone()["n"] == 0
        # Simulate a pre-relay worker which still considers the key shareable.
        current = policy.is_shared_setting_key
        with monkeypatch.context() as older:
            older.setattr(policy, "is_shared_setting_key", lambda k: k == key or current(k))
            scan()
    join(a, b)
    with b.active():
        assert db.get_system_setting(key) is None


def test_startup_deletes_retired_key_on_every_boot(mock_db, monkeypatch):
    from app.core import startup
    from app.db import base
    db.set_system_setting("fcm_service_account", "retired-ciphertext")
    for name in ("propagate_child_priority", "_reject_legacy_sqlite", "ensure_directories", "init_database"):
        monkeypatch.setattr(startup, name, lambda: None)
    monkeypatch.setattr(base, "init_pool", lambda **kwargs: None)

    class AfterCleanup(Exception):
        pass

    def stop_after_cleanup():
        raise AfterCleanup

    monkeypatch.setattr(startup, "run_migrations", stop_after_cleanup)
    for _ in range(2):
        with pytest.raises(AfterCleanup):
            startup.init_core()
        assert db.get_system_setting("fcm_service_account") is None


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
