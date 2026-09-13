import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.core import chat_vault, encryption
from app.core.chat_access import GrantInput
from app.core.chat_files import AccessDenied


@pytest.fixture
def vault(monkeypatch):
    rows = {}
    monkeypatch.setattr(encryption, "_encryption_key", Fernet.generate_key())
    monkeypatch.setattr(chat_vault, "settings", SimpleNamespace(effective_instance_id="worker-one"))
    monkeypatch.setattr(chat_vault.database, "get_system_setting", lambda k: rows.get(k))
    monkeypatch.setattr(chat_vault.database, "set_system_setting", lambda k, v: rows.__setitem__(k, v))
    monkeypatch.setattr(chat_vault.database, "delete_system_setting", lambda k: rows.pop(k, None) is not None)
    class Cursor:
        def execute(self, sql, params):
            self.prefix = params[0][:-1]
        def fetchall(self):
            return [{"key": k, "value": v} for k, v in rows.items() if k.startswith(self.prefix)]
    @contextmanager
    def db():
        yield SimpleNamespace(cursor=Cursor)
    monkeypatch.setattr(chat_vault.database, "get_db", db)
    return rows


def spec():
    return GrantInput(kind="machine", label="Unraid", host="10.0.0.200", username="root",
                      fingerprint="SHA256:" + "a" * 43, password="vault-only-password")


def test_explicit_save_encrypts_all_inputs_and_scope_binds_reuse(vault):
    owner = ("local", "worker", None)
    connection_id = chat_vault.save(owner, spec())
    ciphertext = next(iter(vault.values()))
    assert ciphertext.startswith("enc:")
    assert all(secret not in ciphertext for secret in ("vault-only-password", "10.0.0.200", "Unraid"))
    assert chat_vault.load(owner, connection_id).password.get_secret_value() == "vault-only-password"
    assert "password" not in json.dumps(chat_vault.catalog(owner))
    with pytest.raises(AccessDenied):
        chat_vault.load(("local", "worker", "different-api-user"), connection_id)
    assert chat_vault.catalog(("local", "worker", "different-api-user")) == []
    chat_vault.delete(owner, connection_id)
    assert not vault
    with pytest.raises(AccessDenied):
        chat_vault.load(owner, connection_id)


def test_no_plaintext_or_locked_storage_fallback(vault, monkeypatch):
    owner = ("local", "worker", None)
    connection_id = "a" * 32
    vault[chat_vault.key(owner, connection_id)] = json.dumps({"password": "cleartext"})
    with pytest.raises(AccessDenied):
        chat_vault.load(owner, connection_id)
    monkeypatch.setattr(encryption, "_encryption_key", None)
    with pytest.raises(AccessDenied):
        chat_vault.save(owner, spec())


def test_swapped_ciphertext_and_other_worker_fail_closed(vault, monkeypatch):
    owner = ("local", "worker", None)
    connection_id = chat_vault.save(owner, spec())
    other = "b" * 32
    vault[chat_vault.key(owner, other)] = vault[chat_vault.key(owner, connection_id)]
    with pytest.raises(AccessDenied):
        chat_vault.load(owner, other)
    monkeypatch.setattr(chat_vault, "settings", SimpleNamespace(effective_instance_id="worker-two"))
    with pytest.raises(AccessDenied):
        chat_vault.load(owner, connection_id)


def test_connections_are_never_network_shared():
    from app.network.policy import is_shared_setting_key
    assert not is_shared_setting_key("chat_connection:worker:scope:abc")


def test_duplicate_names_never_overwrite_credentials(vault):
    owner = ("local", "worker", None)
    first = chat_vault.save(owner, spec())
    replacement = spec().model_copy(update={"label": "UNRAID"})
    with pytest.raises(AccessDenied):
        chat_vault.save(owner, replacement)
    assert len(vault) == 1
    assert chat_vault.load(owner, first).label == "Unraid"
