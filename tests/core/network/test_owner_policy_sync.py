"""Imported owner policy travels over the real settings replication path."""
from contextlib import nullcontext

from app.core import legacy_settings_import as legacy, owner_settings
from app.core.config import settings
from app.db import database
from app.network import engine, policy
from tests.core.network.harness import exchange, join


def test_import_then_sync_then_peer_restart_preserves_owner_choice(workers, monkeypatch, tmp_path):
    monkeypatch.setattr(database, "boot_init_lock", nullcontext)
    monkeypatch.setenv("SESSION_EXPIRE_DAYS", "7")
    monkeypatch.delenv("NETWORK_PEER_TERMINALS", raising=False)
    monkeypatch.delenv("NETWORK_REQUIRE_TOKEN_BINDING", raising=False)
    a, b = workers("primary"), workers("peer")
    with a.active():
        engine.create_network("Policy test")
    join(a, b)
    with a.active():
        legacy.import_legacy_settings(env_file=str(tmp_path / "missing"))
        assert owner_settings.session_expire_days() == 7
        engine.scan_collection("system_settings")
    exchange(a, b)
    with b.active():
        assert settings.session_expire_days == 7
    with a.active():
        owner_settings.write({"session_expire_days": 14})
        engine.scan_collection("system_settings")
    exchange(a, b)
    with b.active():
        assert owner_settings.session_expire_days() == 14
        assert settings.session_expire_days == 14
        assert database.get_system_setting(legacy.MIGRATION_KEY) is None
        # A second worker has a conflicting explicit old value. The synced row
        # exists, so its local import must not undo the owner's current choice.
        monkeypatch.setenv("SESSION_EXPIRE_DAYS", "90")
        legacy.import_legacy_settings(env_file=str(tmp_path / "missing"))
        assert settings.session_expire_days == 14
        engine.scan_collection("system_settings")
    exchange(b, a)
    with a.active():
        assert settings.session_expire_days == 14
    for name in owner_settings.OwnerPolicy.model_fields:
        assert policy.is_shared_setting_key(owner_settings.PREFIX + name)
    assert not policy.is_shared_setting_key(legacy.MIGRATION_KEY)
