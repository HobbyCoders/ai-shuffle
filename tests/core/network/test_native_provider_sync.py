"""Encrypted native-key rows and slots use the existing replication path."""
import pytest

from app.core import encryption
from app.core.provider.harness_adapter import _provider_row_kind
from app.core.environment import ProviderConfigError
from app.db import database as db
from app.db.model_provider_slots import model_provider_slot
from app.network import engine, policy
from tests.core.network.harness import converge, exchange, join


def scan():
    return engine.scan_all(include_catalog=False, collections=["system_settings", "model_providers"])


@pytest.mark.parametrize("provider", ["kimi", "minimax", "opencode"])
def test_keys_sync_and_old_peer_missing_optional_fields_is_safe(workers, monkeypatch, provider):
    monkeypatch.setattr(encryption, "_encryption_key", encryption.Fernet.generate_key())
    a, b = workers("new"), workers("peer")
    with a.active():
        engine.create_network("Native providers")
        for id, mode in (("plan", "plan_key"), ("api", "api_key")):
            db.create_model_provider(id, id, provider, "https://example.com", auth_mode=mode,
                                     auth_token=encryption.encrypt_value("test-synced-key"))
        scan()
    join(a, b)
    converge([a, b])
    with b.active():
        for id, mode in (("plan", "plan_key"), ("api", "api_key")):
            row = db.get_model_provider(id, include_token=True)
            assert row["auth_mode"] == mode and row["enabled"]
            assert row["auth_token"] != "test-synced-key"
            assert encryption.decrypt_value(row["auth_token"]) == "test-synced-key"
            assert _provider_row_kind(row["provider_type"], mode, row.get("api_dialect")) == "anthropic"
        # Older shape: no dialect/defaults/capability fields. The receiving
        # worker does not infer a Claude route from the missing metadata.
        with db.get_db() as conn:
            engine.materialize(conn.cursor(), policy.collection("model_providers"), "legacy",
                               {"id": "legacy", "name": "Older shape", "provider_type": provider,
                                "auth_mode": "plan_key", "base_url": "https://example.com", "enabled": True})
        row = db.get_model_provider("legacy")
        assert model_provider_slot(row) == f"{provider}:subscription"
        assert _provider_row_kind(row["provider_type"], row["auth_mode"], row.get("api_dialect")) == "anthropic"
        db.set_model_provider_enabled("legacy", True)
        scan()
    exchange(b, a)
    with a.active():
        assert {r["id"] for r in db.get_enabled_model_providers()} == {"legacy", "api"}


def test_unknown_future_provider_fails_closed_instead_of_claude_sniffing():
    with pytest.raises(ProviderConfigError, match="update the worker"):
        _provider_row_kind("future-native", "plan_key", None)


def test_presence_advertises_native_messages_capability(workers):
    from app.core import native_providers
    from app.db import network as store

    worker = workers("Current worker")
    with worker.active():
        engine.create_network("Native providers")
        engine.publish_presence()
        own = store.get_member(engine.context().device_id)
        assert own["capabilities"][native_providers.CAPABILITY] == 1
        assert native_providers.workers_requiring_update() == []
