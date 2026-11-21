import json
import re
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from app.core import vault_store, encryption
from app.core.vault_access import GrantInput
from app.core.vault_files import AccessDenied


@pytest.fixture
def vault(monkeypatch):
    rows = {}
    vault_store._invalidate_catalog()
    monkeypatch.setattr(encryption, "_encryption_key", Fernet.generate_key())
    monkeypatch.setattr(vault_store, "settings", SimpleNamespace(effective_instance_id="worker-one"))
    monkeypatch.setattr(vault_store.database, "get_system_setting", lambda k: rows.get(k))
    monkeypatch.setattr(vault_store.database, "set_system_setting", lambda k, v: rows.__setitem__(k, v))
    monkeypatch.setattr(vault_store.database, "delete_system_setting", lambda k: rows.pop(k, None) is not None)
    # As the table behaves: a conditional write lands only on the row that was
    # read (or, given no row, only where there is still none).
    monkeypatch.setattr(vault_store.database, "get_system_setting_snapshot",
                        lambda k: SimpleNamespace(value=rows[k]) if k in rows else None)
    def write_if_unchanged(k, v, expected):
        if rows.get(k) != (expected.value if expected else None):
            return False
        rows[k] = v
        return True
    monkeypatch.setattr(vault_store.database, "set_system_setting_if_unchanged", write_if_unchanged)
    class Cursor:
        def execute(self, sql, params):
            # LIKE as the database reads it: "%" any run, "_" any one character,
            # and the query's escape character before a literal one.
            escape, pattern, chars = "!" if "ESCAPE '!'" in sql else None, "", iter(params[0])
            for char in chars:
                pattern += (re.escape(next(chars)) if char == escape
                            else {"%": ".*", "_": "."}.get(char, re.escape(char)))
            self.matches = re.compile(pattern, re.S).fullmatch
        def fetchall(self):
            return [{"key": k, "value": v} for k, v in rows.items() if self.matches(k)]
    @contextmanager
    def db():
        yield SimpleNamespace(cursor=Cursor)
    monkeypatch.setattr(vault_store.database, "get_db", db)
    return rows


def spec():
    return GrantInput(kind="machine", label="ServerWorker", host="10.0.0.20", username="root",
                      fingerprint="SHA256:" + "a" * 43, password="vault-only-password")


def test_service_certificate_pin_is_encrypted_saved_and_reused(vault):
    owner = ("local", "worker", None)
    pin = "a" * 64
    connection = GrantInput(kind="service", label="Home API", url="https://10.0.0.5/api",
                            fingerprint=pin, secret="private-test-token")
    connection_id = vault_store.save(owner, connection)
    ciphertext = next(iter(vault.values()))
    assert pin not in ciphertext and "private-test-token" not in ciphertext
    loaded = vault_store.load(owner, connection_id)
    assert loaded.fingerprint == pin
    assert vault_store.catalog(owner)[0]["fingerprint"] == pin
    assert "private-test-token" not in json.dumps(vault_store.catalog(owner))


def test_explicit_save_encrypts_all_inputs_and_scope_binds_reuse(vault):
    owner = ("local", "worker", None)
    connection_id = vault_store.save(owner, spec())
    ciphertext = next(iter(vault.values()))
    assert ciphertext.startswith("enc:")
    assert all(secret not in ciphertext for secret in ("vault-only-password", "10.0.0.20", "ServerWorker"))
    assert vault_store.load(owner, connection_id).password.get_secret_value() == "vault-only-password"
    assert "password" not in json.dumps(vault_store.catalog(owner))
    with pytest.raises(AccessDenied):
        vault_store.load(("local", "worker", "different-api-user"), connection_id)
    assert vault_store.catalog(("local", "worker", "different-api-user")) == []
    vault_store.delete(owner, connection_id)
    assert not vault
    with pytest.raises(AccessDenied):
        vault_store.load(owner, connection_id)


def test_no_plaintext_or_locked_storage_fallback(vault, monkeypatch):
    owner = ("local", "worker", None)
    connection_id = "a" * 32
    vault[vault_store.key(owner, connection_id)] = json.dumps({"password": "cleartext"})
    with pytest.raises(AccessDenied):
        vault_store.load(owner, connection_id)
    monkeypatch.setattr(encryption, "_encryption_key", None)
    with pytest.raises(AccessDenied):
        vault_store.save(owner, spec())


def test_swapped_ciphertext_and_another_data_key_fail_closed(vault, monkeypatch):
    owner = ("local", "worker", None)
    connection_id = vault_store.save(owner, spec())
    other = "b" * 32
    vault[vault_store.key(owner, other)] = vault[vault_store.key(owner, connection_id)]
    with pytest.raises(AccessDenied):
        vault_store.load(owner, other)
    # The key names the owner, not the worker: the entry reads the same on
    # every worker that holds it under its own data key.
    monkeypatch.setattr(vault_store, "settings", SimpleNamespace(effective_instance_id="worker-two"))
    assert vault_store.load(owner, connection_id).password.get_secret_value() == "vault-only-password"
    # Ciphertext copied from a worker with another data key is not an entry here.
    monkeypatch.setattr(encryption, "_encryption_key", Fernet.generate_key())
    with pytest.raises(AccessDenied):
        vault_store.load(owner, connection_id)


def test_empty_snapshot_catalog_is_bounded_and_save_invalidates(vault, monkeypatch):
    owner = ("local", "worker", None)
    calls = []
    original = vault_store.database.get_db
    def counted():
        calls.append(True)
        return original()
    monkeypatch.setattr(vault_store.database, "get_db", counted)
    for _ in range(10):
        assert vault_store.catalog(owner, cache_empty=True) == []
    assert len(calls) == 1
    connection_id = vault_store.save(owner, spec())
    assert vault_store.catalog(owner, cache_empty=True)[0]["id"] == connection_id
    vault_store.delete(owner, connection_id)
    assert vault_store.catalog(owner, cache_empty=True) == []
    assert len(calls) == 4
    for i in range(200):
        vault_store.catalog(("local", "worker", str(i)), cache_empty=True)
    assert len(vault_store._empty_catalog) <= 128


def test_empty_catalog_expires_and_never_caches_decrypted_entries(vault, monkeypatch):
    owner = ("local", "worker", None)
    clock = [100.0]
    monkeypatch.setattr(vault_store.time, "monotonic", lambda: clock[0])
    assert vault_store.catalog(owner, cache_empty=True) == []
    # Simulate an external database writer; TTL bounds recovery without treating
    # an empty read as an authorization decision.
    connection_id = "a" * 32
    binding = vault_store.key(owner, connection_id)
    vault[binding] = "corrupt"
    clock[0] += vault_store._EMPTY_CATALOG_TTL
    with pytest.raises(AccessDenied):
        vault_store.catalog(owner, cache_empty=True)
    vault.clear()
    connection_id = vault_store.save(owner, spec())
    assert vault_store.catalog(owner, cache_empty=True)
    monkeypatch.setattr(encryption, "_encryption_key", None)
    assert vault_store.catalog(owner, cache_empty=True) == []
    with pytest.raises(AccessDenied):
        vault_store.load(owner, connection_id)


def test_empty_query_started_before_save_cannot_republish_stale_absence(vault, monkeypatch):
    owner = ("local", "worker", None)
    from concurrent.futures import ThreadPoolExecutor
    import threading
    queried = threading.Event()
    finish = threading.Event()
    original = vault_store.database.get_db
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
    monkeypatch.setattr(vault_store.database, "get_db", delayed)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="stale-reader") as executor:
        pending = executor.submit(vault_store.catalog, owner, cache_empty=True)
        assert queried.wait(5)
        connection_id = vault_store.save(owner, spec())
        finish.set()
        assert pending.result() == []
    assert vault_store.catalog(owner, cache_empty=True)[0]["id"] == connection_id


def test_entries_are_network_shared_and_rows_from_before_stay_local():
    from app.network.policy import is_shared_setting_key
    owner = ("local", "worker", None)
    assert is_shared_setting_key(vault_store.key(owner, "a" * 32))
    # So is the host key a first connection recorded for one.
    assert is_shared_setting_key(vault_store._pin_key(owner, "a" * 32))
    assert not is_shared_setting_key("chat_connection:worker:scope:abc")


def _kept_by(worker, owner, connection_id):
    """The key a worker used for an entry before entries synced."""
    return f"{vault_store.LOCAL_PREFIX}{worker}:{vault_store.key(owner, connection_id)[len(vault_store.SHARED_PREFIX):]}"


def test_entries_this_worker_kept_move_into_the_shared_vault_once(vault):
    owner, api_user = ("local", "worker", None), ("local", "worker", "api-user")
    mine, theirs, other_worker = _kept_by("worker-one", owner, "a" * 32), _kept_by("worker-one", api_user, "b" * 32), \
        _kept_by("worker-two", owner, "c" * 32)
    vault_store._store(mine, spec())
    vault_store._store(theirs, spec().model_copy(update={"label": "Backup"}))
    vault_store._store(other_worker, spec())
    # Ciphertext bound to another row, and a row that is not ciphertext at all.
    swapped, corrupt = _kept_by("worker-one", owner, "d" * 32), _kept_by("worker-one", owner, "e" * 32)
    vault[swapped], vault[corrupt] = vault[mine], "corrupt"
    assert vault_store.catalog(owner) == []

    assert vault_store.adopt_local_entries() == 2
    assert set(vault) == {vault_store.key(owner, "a" * 32), vault_store.key(api_user, "b" * 32),
                          other_worker, swapped, corrupt}
    # Re-bound to the shared key, secrets intact and each still under its own principal.
    assert vault_store.load(owner, "a" * 32).password.get_secret_value() == "vault-only-password"
    assert [item["label"] for item in vault_store.catalog(api_user)] == ["Backup"]
    with pytest.raises(AccessDenied):
        vault_store.load(owner, "b" * 32)
    before = dict(vault)
    assert vault_store.adopt_local_entries() == 0 and vault == before


def test_move_keeps_a_shared_entry_that_already_exists_and_waits_while_locked(vault, monkeypatch):
    owner = ("local", "worker", None)
    kept = _kept_by("worker-one", owner, "a" * 32)
    vault_store._store(kept, spec())
    with monkeypatch.context() as locked:
        locked.setattr(encryption, "_encryption_key", None)
        assert vault_store.adopt_local_entries() == 0 and set(vault) == {kept}
    # A crash after the shared row was written: another worker may have
    # changed that row since, so it wins and only the old row goes.
    newer = spec().model_copy(update={"label": "Renamed since"})
    vault_store._store(vault_store.key(owner, "a" * 32), newer)
    vault_store._store(kept, spec())
    assert vault_store.adopt_local_entries() == 1
    assert set(vault) == {vault_store.key(owner, "a" * 32)}
    assert vault_store.load(owner, "a" * 32).label == "Renamed since"


def test_entries_merged_from_several_workers_stay_listed_past_the_cap(vault):
    owner = ("local", "worker", None)
    for index in range(vault_store.MAX_ENTRIES + 5):
        vault_store._store(vault_store.key(owner, f"{index:032x}"), spec().model_copy(update={"label": f"Machine {index}"}))
    assert len(vault_store.catalog(owner)) == vault_store.MAX_ENTRIES + 5
    with pytest.raises(AccessDenied, match="Vault is full"):
        vault_store.save(owner, spec())
    # Removing the extra ones is how the owner gets back under it.
    for index in range(6):
        vault_store.delete(owner, f"{index:032x}")
    assert vault_store.save(owner, spec())


def test_duplicate_names_never_overwrite_credentials(vault):
    owner = ("local", "worker", None)
    first = vault_store.save(owner, spec())
    replacement = spec().model_copy(update={"label": "SERVERWORKER"})
    with pytest.raises(AccessDenied):
        vault_store.save(owner, replacement)
    assert len(vault) == 1
    assert vault_store.load(owner, first).label == "ServerWorker"


def test_concurrent_adds_cannot_pass_the_capacity_or_name_check_together(vault, monkeypatch):
    import threading
    owner = ("local", "worker", None)
    for index in range(49):
        vault_store.save(owner, spec().model_copy(update={"label": f"Machine {index}"}))
    # Without the write lock both adds read the catalogue before either writes.
    read = threading.Barrier(2, timeout=0.3)
    catalog = vault_store.catalog

    def slow_catalog(*args, **kwargs):
        found = catalog(*args, **kwargs)
        try:
            read.wait()
        except threading.BrokenBarrierError:
            pass
        return found

    monkeypatch.setattr(vault_store, "catalog", slow_catalog)
    outcomes = []

    def add(label):
        try:
            outcomes.append(vault_store.save(owner, spec().model_copy(update={"label": label})))
        except AccessDenied as exc:
            outcomes.append(exc)

    for labels in (("Last", "One too many"), ("Same name", "same NAME")):
        outcomes.clear()
        read.reset()
        threads = [threading.Thread(target=add, args=(label,)) for label in labels]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sum(isinstance(outcome, AccessDenied) for outcome in outcomes) == 1, outcomes
        if labels[0] == "Last":
            assert len(vault) == 50
            vault.pop(next(key for key in vault if vault_store.unpack(
                owner, key.rsplit(":", 1)[-1], vault[key]).label in labels))
            vault.pop(next(iter(vault)))
        else:
            assert len(vault) == 49


def test_service_credentials_are_encrypted_and_never_catalogued(vault):
    owner = ("local", "worker", None)
    service = GrantInput(kind="service", label="Inventory API", url="https://api.example.com/v1",
                         auth_header="X-API-Key", auth_scheme="", secret="vault-only-api-key", writable=True)
    connection_id = vault_store.save(owner, service)
    ciphertext = next(iter(vault.values()))
    assert all(value not in ciphertext for value in ("vault-only-api-key", "api.example.com", "X-API-Key"))
    loaded = vault_store.load(owner, connection_id)
    assert loaded.secret.get_secret_value() == "vault-only-api-key"
    assert (loaded.url, loaded.auth_header, loaded.auth_scheme, loaded.writable) == (
        "https://api.example.com/v1", "X-API-Key", "", True)
    catalogue = vault_store.catalog(owner)
    assert catalogue == [{"id": connection_id, "kind": "service", "label": "Inventory API",
                          "target": "https://api.example.com/v1", "fingerprint": "", "writable": True,
                          "auth_header": "X-API-Key", "auth_scheme": "", "username": ""}]
    assert "vault-only-api-key" not in json.dumps(catalogue)
    machine = vault_store.save(owner, spec())
    assert {item["kind"] for item in vault_store.catalog(owner)} == {"machine", "service"}
    assert vault_store.load(owner, machine).kind == "machine"


def test_folder_grants_are_never_saved(vault):
    with pytest.raises(AccessDenied):
        vault_store.save(("local", "worker", None), GrantInput(kind="folder", label="Docs", path="/srv/docs"))
    assert not vault


def test_update_rewrites_in_place_and_refuses_without_writing(vault):
    owner = ("local", "worker", None)
    first = vault_store.save(owner, spec())
    other = vault_store.save(owner, spec().model_copy(update={"label": "Backup"}))
    old, new = vault_store.update(owner, first, lambda current: current.model_copy(update={"label": "serverworker", "port": 2222}))
    assert (old.label, old.port, new.label, new.port) == ("ServerWorker", 22, "serverworker", 2222)
    loaded = vault_store.load(owner, first)
    # Same id and binding; the stored secret survives an edit that did not touch it.
    assert (loaded.label, loaded.port) == ("serverworker", 2222)
    assert loaded.password.get_secret_value() == "vault-only-password"
    assert len(vault) == 2 and "vault-only-password" not in json.dumps(vault_store.catalog(owner))
    before = dict(vault)
    with pytest.raises(AccessDenied, match="already has a sign-in with that name"):
        vault_store.update(owner, other, lambda current: current.model_copy(update={"label": "SERVERWORKER"}))
    def refuse(current):
        raise AccessDenied("refused")
    with pytest.raises(AccessDenied, match="refused"):
        vault_store.update(owner, first, refuse)
    with pytest.raises(AccessDenied, match="keeps its type"):
        vault_store.update(owner, first, lambda current: GrantInput(kind="folder", label="Docs", path="/srv"))
    with pytest.raises(AccessDenied):
        vault_store.update(owner, "c" * 32, lambda current: current)
    assert vault == before
    with pytest.raises(AccessDenied):
        vault_store.update(("local", "worker", "someone-else"), first, lambda current: current)


def test_pin_fingerprint_only_fills_a_blank_pin_on_unchanged_details(vault):
    owner = ("local", "worker", None)
    unpinned = spec().model_copy(update={"fingerprint": ""})
    connection_id = vault_store.save(owner, unpinned)
    key = "SHA256:" + "b" * 43
    # Edited after the handshake started: the key seen is not saved.
    edited = unpinned.model_copy(update={"port": 2222})
    assert not vault_store.pin_fingerprint(owner, connection_id, edited, key)
    entry = vault_store.key(owner, connection_id)
    before = vault[entry]
    assert not vault_store.pin_fingerprint(owner, connection_id, unpinned, "not a host key")
    assert vault_store.pin_fingerprint(owner, connection_id, unpinned.model_copy(update={"minutes": 5}), key)
    assert vault_store.load(owner, connection_id).fingerprint == key
    assert [item["fingerprint"] for item in vault_store.catalog(owner)] == [key]
    # An existing pin is never replaced this way.
    assert not vault_store.pin_fingerprint(owner, connection_id, vault_store.load(owner, connection_id),
                                          "SHA256:" + "c" * 43)
    assert vault_store.load(owner, connection_id).fingerprint == key
    # The entry, which holds the credentials, was not rewritten to record it:
    # the key is a row of its own, with no credentials in it.
    pin = next(k for k in vault if k != entry)
    assert vault[entry] == before and pin.startswith(vault_store.PIN_PREFIX) and len(vault) == 2
    assert "vault-only-password" not in encryption.decrypt_value(vault[pin])
    vault_store.delete(owner, connection_id)
    assert not vault_store.pin_fingerprint(owner, connection_id, unpinned, key) and not vault


def test_a_recorded_host_key_holds_for_the_version_it_was_verified_with_and_an_edit_keeps_it(vault):
    owner = ("local", "worker", None)
    connection_id = vault_store.save(owner, spec().model_copy(update={"fingerprint": ""}))
    key, entry = "SHA256:" + "b" * 43, vault_store.key(owner, connection_id)
    assert vault_store.pin_fingerprint(owner, connection_id, vault_store.load(owner, connection_id), key)
    pin = next(k for k in vault if k != entry)
    # An edit that keeps the key stores it in the entry itself, so it no longer rests on the pin row.
    old, new = vault_store.update(owner, connection_id, lambda current: current.model_copy(update={"label": "Renamed"}))
    assert (old.fingerprint, new.fingerprint) == (key, key)
    kept = vault.pop(pin)
    assert vault_store.load(owner, connection_id).fingerprint == key
    # A pin written for a version that has since been replaced (as when another
    # worker changed the password) applies to nothing.
    vault_store.update(owner, connection_id, lambda current: current.model_copy(update={"fingerprint": ""}))
    vault[pin] = kept
    assert vault_store.load(owner, connection_id).fingerprint == ""
    assert [item["fingerprint"] for item in vault_store.catalog(owner)] == [""]
    # Nor does one bound to another row, or one that cannot be read.
    other = vault_store.save(owner, spec().model_copy(update={"fingerprint": "", "label": "Other"}))
    assert vault_store.pin_fingerprint(owner, other, vault_store.load(owner, other), key)
    vault[pin] = vault[next(k for k in vault if k.startswith(vault_store.PIN_PREFIX) and k != pin)]
    assert vault_store.load(owner, connection_id).fingerprint == ""
    vault[pin] = "corrupt"
    assert vault_store.load(owner, connection_id).fingerprint == ""


def test_every_stored_version_carries_a_random_value_of_its_own(vault):
    """Worker sync keeps a hash of what it decrypted; with nothing random in an
    entry that hash would confirm a guessed password without the data key."""
    owner = ("local", "worker", None)
    connection_id = vault_store.save(owner, spec())
    entry = vault_store.key(owner, connection_id)
    first = json.loads(encryption.decrypt_value(vault[entry]))
    vault_store.update(owner, connection_id, lambda current: current)
    second = json.loads(encryption.decrypt_value(vault[entry]))
    assert first["connection"] == second["connection"]
    assert re.fullmatch("[0-9a-f]{32}", first["version"]) and re.fullmatch("[0-9a-f]{32}", second["version"])
    assert first["version"] != second["version"]


def test_the_list_holds_entries_only_whatever_else_is_named_like_one(vault):
    owner = ("local", "worker", None)
    connection_id = vault_store.save(owner, spec())
    # Another setting can be written under a name that starts like an entry's
    # (the model-parameters API appends "_params" to a caller's key).
    vault[vault_store.key(owner, connection_id) + "_params"] = "{}"
    vault[vault_store.prefix(owner) + "not-an-entry"] = "plain"
    # As long as an entry's key, and differing only where LIKE reads "_" as any character.
    vault[vault_store.key(owner, "f" * 32).replace("vault_entry:", "vaultXentry:")] = "plain"
    vault[vault_store.prefix(owner) + "G" * 32] = "plain"
    assert [item["id"] for item in vault_store.catalog(owner)] == [connection_id]
    assert vault_store.load(owner, connection_id).label == "ServerWorker"
    # Neither counts against the cap or blocks a name.
    vault_store.save(owner, spec().model_copy(update={"label": "Second"}))
    assert len(vault_store.catalog(owner)) == 2
