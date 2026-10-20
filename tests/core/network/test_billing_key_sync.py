"""Billing access follows authenticated sync, not a copied installation ID."""
import asyncio
from contextlib import contextmanager
from dataclasses import replace
from unittest.mock import patch

import httpx
import pytest

from app.core import api_key_balances as balances, encryption
from app.core.config import settings
from app.core.provider_usage import ProviderUsageMonitor
from app.db import database as db, provider_balances
from app.db import network as store
from app.network import engine, policy
from tests.core.network.harness import Worker, exchange, join

PROVIDER = "billing-provider"
SECRETS = ("balance_key", "openai_admin_key")
BILLING = (*SECRETS, "openai_billing_org_id")


@pytest.fixture(autouse=True)
def separate_installations(monkeypatch):
    original = Worker.active
    keys = {}

    @contextmanager
    def active(worker):
        key = keys.setdefault(worker.name, encryption.Fernet.generate_key())
        with patch.object(settings, "instance_id", worker.name), \
                patch.object(encryption, "_encryption_key", key), original(worker):
            yield worker

    monkeypatch.setattr(Worker, "active", active)


def scan():
    return engine.scan_all(include_catalog=False, collections=["model_providers"])


def row():
    return db.get_model_provider(PROVIDER, include_token=True)


def bind(key="billing-test"):
    assert provider_balances.bind(row(), settings.effective_instance_id,
                                  encryption.encrypt_value(key) if key else None)


def pair(workers, kind="xai", configured=True):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Billing tests")
        db.create_model_provider(PROVIDER, "API", kind, "https://api.x.ai/v1" if kind == "xai"
                                 else "https://api.openai.com/v1",
                                 auth_token=encryption.encrypt_value("inference-test"))
        if configured:
            bind()
        scan()
    join(a, b)  # snapshot path, not just incremental operations
    return a, b


def test_billing_policy():
    coll = policy.collection("model_providers")
    assert set(BILLING) <= set(coll.fields)
    assert set(SECRETS) <= set(coll.encrypted)
    assert not set(BILLING) & set(coll.local_fields)
    assert {"balance_snapshot", "balance_instance_id", "openai_billing_instance_id",
            "codex_refresh_lease_owner", "codex_refresh_lease_expires_at"} <= set(coll.local_fields)


@pytest.mark.parametrize("kind", ["xai", "openai"])
def test_network_sync_balance_poll_rotate_and_clear(workers, monkeypatch, kind):
    a, b = pair(workers, kind)
    calls = []

    async def get(url, **kwargs):
        calls.append((url, kwargs["headers"]["Authorization"]))
        body = ({"team_id": "team-test"} if url == balances.XAI_KEY_URL else
                {"total": {"val": "-4200"}} if url.endswith("/prepaid/balance") else
                {} if url.endswith("/preview") else
                {"total_available": 42, "total_granted": 50, "total_used": 8})
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(balances, "usage_get", get)
    with a.active():
        local_ciphertext = row()["balance_key"]
    with b.active():
        assert encryption.is_encrypted(row()["balance_key"])
        assert row()["balance_key"] != local_ciphertext  # different at-rest data keys
        assert row()["balance_instance_id"] == "b"
        assert balances.status(row())["configured"]
        assert row()["balance_snapshot"] is None  # each worker fetches its own reading
        asyncio.run(balances.poll({"id": PROVIDER}))
        assert balances.status(row())["credits"]["balance_remaining"] == 42
        old = row()
    assert any(auth == "Bearer billing-test" for _, auth in calls)
    if kind == "xai":
        assert (balances.XAI_KEY_URL, "Bearer inference-test") in calls
    with a.active():
        # Isolate the producing path for an inference rotation that arrives via
        # sync without the local update helper's intentional balance clear.
        with db.get_db() as conn:
            conn.cursor().execute("UPDATE model_providers SET auth_token = %s WHERE id = %s",
                                  (encryption.encrypt_value("rotated-inference"), PROVIDER))
        scan()
    exchange(a, b)
    with b.active():
        assert balances.status(row())["configured"]
        assert balances.status(row())["credits"] is None
        assert not provider_balances.snapshot(old, {"credits": {"balance_remaining": 999}})
        asyncio.run(balances.poll({"id": PROVIDER}))
        assert balances.status(row())["credits"]["balance_remaining"] == 42
    with a.active():
        bind("rotated-billing")
        scan()
    exchange(a, b)
    with b.active():
        assert balances.status(row())["credits"] is None
        asyncio.run(balances.poll({"id": PROVIDER}))
        assert calls[-1][1] == "Bearer rotated-billing"
    with a.active():
        bind(None)
        scan()
    exchange(a, b)
    with b.active():
        assert row()["balance_key"] is None
        assert not balances.status(row())["configured"]
        calls.clear()
        asyncio.run(balances.poll({"id": PROVIDER}))
        assert not calls


def test_network_sync_openai_admin_poll_and_clear(workers, monkeypatch):
    a, b = pair(workers, "openai", configured=False)
    with a.active():
        assert db.set_openai_billing_binding(PROVIDER, "org-test", "a",
                                            encryption.encrypt_value("admin-test"))
        scan()
    exchange(a, b)
    calls = []

    async def get(url, **kwargs):
        calls.append((url, kwargs["headers"]))
        result = {"amount": {"value": "3.5", "currency": "usd"}} if url.endswith("/costs") else {
            "input_tokens": 10, "output_tokens": 5, "num_model_requests": 1}
        return httpx.Response(200, json={"data": [{"results": [result]}]},
                              request=httpx.Request("GET", url))

    monkeypatch.setattr("app.core.openai_admin_usage.usage_get", get)
    with b.active():
        assert row()["openai_billing_instance_id"] == "b"
        assert asyncio.run(ProviderUsageMonitor()._poll_openai_provider(row()))
        assert db.get_all_provider_usage_snapshots()[0]["credits"]["spend_month"] == 3.5
    assert len(calls) == 2
    assert all(headers == {"Authorization": "Bearer admin-test", "OpenAI-Organization": "org-test"}
               for _, headers in calls)
    with a.active():
        db.set_openai_billing_binding(PROVIDER, "org-new", "a", encryption.encrypt_value("admin-new"))
        scan()
    exchange(a, b)
    with b.active():
        assert not db.get_all_provider_usage_snapshots()
        assert asyncio.run(ProviderUsageMonitor()._poll_openai_provider(row()))
    with a.active():
        db.clear_openai_billing_binding(PROVIDER, "a")
        scan()
    exchange(a, b)
    with b.active():
        assert row()["openai_admin_key"] is None
        assert row()["openai_billing_org_id"] is None
        assert not db.get_all_provider_usage_snapshots()
        assert not asyncio.run(ProviderUsageMonitor()._poll_openai_provider(row()))


def test_mixed_version_allowlists_and_upgrade_do_not_wipe_keys(workers, monkeypatch):
    a, b = pair(workers, "openai", configured=False)
    modern = policy.collection("model_providers")
    legacy = replace(modern, fields=tuple(f for f in modern.fields if f not in BILLING),
                     encrypted=tuple(f for f in modern.encrypted if f not in SECRETS),
                     local_fields=(*modern.local_fields, *BILLING))
    with a.active():
        bind()
        db.set_openai_billing_binding(PROVIDER, "org-test", "a", encryption.encrypt_value("admin-test"))
        scan()
        wire = engine.serialize_row(modern, row())
        assert set(SECRETS) <= set(wire["_enc"])
        assert "balance_snapshot" not in wire
        # Only sealed envelopes, never plaintext billing credentials, reach
        # the transport. Old receivers can forward these without materializing.
        ops, _, _ = store.exchange_batch({})
        assert all("billing-test" not in str(op) and "admin-test" not in str(op) for op in ops)
    with monkeypatch.context() as m:
        m.setitem(policy.COLLECTIONS, "model_providers", legacy)
        exchange(a, b)
        with b.active():
            assert all(row()[field] is None for field in BILLING)
            db.update_model_provider(PROVIDER, name="Legacy edit")
            scan()
    exchange(b, a)
    with a.active():
        assert encryption.decrypt_value(row()["balance_key"]) == "billing-test"
        assert encryption.decrypt_value(row()["openai_admin_key"]) == "admin-test"
        assert row()["openai_billing_org_id"] == "org-test"
    # Upgrade the legacy peer before it has received a modern record: missing
    # local keys must not suddenly serialize as a clear on its first scan.
    with b.active():
        outgoing = engine.serialize_row(modern, row())
        assert not set(BILLING) & outgoing.keys()
        scan()
    exchange(b, a)
    with a.active():
        assert encryption.decrypt_value(row()["balance_key"]) == "billing-test"
        # A modern edit carries the keys back to the upgraded worker.
        db.update_model_provider(PROVIDER, name="Modern edit")
        scan()
    exchange(a, b)
    with b.active():
        assert encryption.decrypt_value(row()["balance_key"]) == "billing-test"
        assert row()["balance_instance_id"] == "b"


def test_local_inference_rotation_propagates_clear(workers):
    a, b = pair(workers)
    with b.active():
        old = row()
        assert provider_balances.snapshot(old, {"credits": {"balance_remaining": 999}})
    with a.active():
        db.update_model_provider(PROVIDER, auth_token=encryption.encrypt_value("rotated-inference"))
        assert row()["balance_key"] is None
        scan()
    exchange(a, b)
    with b.active():
        assert not balances.status(row())["configured"]
        assert balances.status(row())["credits"] is None
        assert not provider_balances.snapshot(old, {"credits": {"balance_remaining": 999}})


def test_backup_binding_is_not_used_or_published(workers):
    a, _ = pair(workers, "openai")
    with a.active():
        db.set_openai_billing_binding(PROVIDER, "org-test", "a", encryption.encrypt_value("admin-test"))
        restored = {**row(), "balance_instance_id": "another-installation",
                    "openai_billing_instance_id": "another-installation"}
        assert not balances.status(restored)["configured"]
        outgoing = engine.serialize_row(policy.collection("model_providers"), restored)
        assert not set(BILLING) & outgoing.keys()
        assert not set(SECRETS) & set(outgoing.get("_enc", []))
