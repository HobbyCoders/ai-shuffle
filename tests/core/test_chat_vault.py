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
    chat_vault._invalidate_catalog()
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


def test_empty_snapshot_catalog_is_bounded_and_save_invalidates(vault, monkeypatch):
    owner = ("local", "worker", None)
    calls = []
    original = chat_vault.database.get_db
    def counted():
        calls.append(True)
        return original()
    monkeypatch.setattr(chat_vault.database, "get_db", counted)
    for _ in range(10):
        assert chat_vault.catalog(owner, cache_empty=True) == []
    assert len(calls) == 1
    connection_id = chat_vault.save(owner, spec())
    assert chat_vault.catalog(owner, cache_empty=True)[0]["id"] == connection_id
    chat_vault.delete(owner, connection_id)
    assert chat_vault.catalog(owner, cache_empty=True) == []
    assert len(calls) == 4
    for i in range(200):
        chat_vault.catalog(("local", "worker", str(i)), cache_empty=True)
    assert len(chat_vault._empty_catalog) <= 128


def test_empty_catalog_expires_and_never_caches_decrypted_entries(vault, monkeypatch):
    owner = ("local", "worker", None)
    clock = [100.0]
    monkeypatch.setattr(chat_vault.time, "monotonic", lambda: clock[0])
    assert chat_vault.catalog(owner, cache_empty=True) == []
    # Simulate an external database writer; TTL bounds recovery without treating
    # an empty read as an authorization decision.
    connection_id = "a" * 32
    binding = chat_vault.key(owner, connection_id)
    vault[binding] = "corrupt"
    clock[0] += chat_vault._EMPTY_CATALOG_TTL
    with pytest.raises(AccessDenied):
        chat_vault.catalog(owner, cache_empty=True)
    vault.clear()
    connection_id = chat_vault.save(owner, spec())
    assert chat_vault.catalog(owner, cache_empty=True)
    monkeypatch.setattr(encryption, "_encryption_key", None)
    assert chat_vault.catalog(owner, cache_empty=True) == []
    with pytest.raises(AccessDenied):
        chat_vault.load(owner, connection_id)


def test_empty_query_started_before_save_cannot_republish_stale_absence(vault, monkeypatch):
    owner = ("local", "worker", None)
    from concurrent.futures import ThreadPoolExecutor
    import threading
    queried = threading.Event()
    finish = threading.Event()
    original = chat_vault.database.get_db
    @contextmanager
    def delayed():
        with original() as conn:
            cursor = conn.cursor()
            fetch = cursor.fetchall
            def fetchall():
                rows = fetch()
                if threading.current_thread().name.startswith("stale-reader"):
                    queried.set()
                    assert finish.wait(5)
                return rows
            cursor.fetchall = fetchall
            yield SimpleNamespace(cursor=lambda: cursor)
    monkeypatch.setattr(chat_vault.database, "get_db", delayed)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stale-reader") as executor:
        pending = executor.submit(chat_vault.catalog, owner, cache_empty=True)
        assert queried.wait(5)
        connection_id = chat_vault.save(owner, spec())
        finish.set()
        assert pending.result() == []
    assert chat_vault.catalog(owner, cache_empty=True)[0]["id"] == connection_id


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


def test_service_credentials_are_encrypted_and_never_catalogued(vault):
    owner = ("local", "worker", None)
    service = GrantInput(kind="service", label="Inventory API", url="https://api.example.com/v1",
                         auth_header="X-API-Key", auth_scheme="", secret="vault-only-api-key", writable=True)
    connection_id = chat_vault.save(owner, service)
    ciphertext = next(iter(vault.values()))
    assert all(value not in ciphertext for value in ("vault-only-api-key", "api.example.com", "X-API-Key"))
    loaded = chat_vault.load(owner, connection_id)
    assert loaded.secret.get_secret_value() == "vault-only-api-key"
    assert (loaded.url, loaded.auth_header, loaded.auth_scheme, loaded.writable) == (
        "https://api.example.com/v1", "X-API-Key", "", True)
    catalogue = chat_vault.catalog(owner)
    assert catalogue == [{"id": connection_id, "kind": "service", "label": "Inventory API",
                          "target": "https://api.example.com/v1", "fingerprint": "", "writable": True}]
    assert "vault-only-api-key" not in json.dumps(catalogue)
    machine = chat_vault.save(owner, spec())
    assert {item["kind"] for item in chat_vault.catalog(owner)} == {"machine", "service"}
    assert chat_vault.load(owner, machine).kind == "machine"


def test_folder_grants_are_never_saved(vault):
    with pytest.raises(AccessDenied):
        chat_vault.save(("local", "worker", None), GrantInput(kind="folder", label="Docs", path="/srv/docs"))
    assert not vault
