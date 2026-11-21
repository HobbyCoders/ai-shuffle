"""Saved Vault sign-ins follow the owner to every worker in the network."""
import json
from contextlib import contextmanager
from unittest.mock import Mock, patch

import pytest

from app.core import encryption, vault_store
from app.core.config import settings
from app.core.vault_access import GrantInput
from app.core.vault_files import AccessDenied
from app.db import database, network as store
from app.network import engine, startup
from tests.core.network.harness import Worker, exchange, join

OWNER = (None, "worker", None)
PASSWORD, TOKEN = "vault-only-password", "vault-only-api-key"


@pytest.fixture(autouse=True)
def separate_installations(monkeypatch):
    """Each worker has its own instance id and its own data key, as real installs do."""
    original = Worker.active
    keys = {}

    @contextmanager
    def active(worker):
        key = keys.setdefault(worker.name, encryption.Fernet.generate_key())
        with patch.object(settings, "instance_id", worker.name), \
                patch.object(encryption, "_encryption_key", key), original(worker):
            yield worker

    monkeypatch.setattr(Worker, "active", active)
    vault_store._invalidate_catalog()


def machine(**changes):
    return GrantInput(**{"kind": "machine", "label": "Home server", "host": "192.168.1.20", "username": "operator",
                         "fingerprint": "SHA256:" + "a" * 43, "password": PASSWORD, **changes})


def scan():
    return engine.scan_all(include_catalog=False, collections=["system_settings"])


def stored(worker):
    return {row["key"]: row["value"] for row in worker.sql("SELECT key, value FROM system_settings")
            if row["key"].startswith((vault_store.SHARED_PREFIX, vault_store.LOCAL_PREFIX))}


def pair(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Vault tests")
        scan()
    join(a, b)
    return a, b


def test_saved_sign_ins_reach_a_joining_and_an_enrolled_worker_under_its_own_key(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Vault tests")
        first = vault_store.save(OWNER, machine())
        scan()
    join(a, b)  # a worker that joins later copies the current state
    with a.active():
        second = vault_store.save(OWNER, GrantInput(kind="service", label="Inventory API", url="https://api.example.com/v1",
                                                    auth_header="X-API-Key", auth_scheme="", secret=TOKEN))
        scan()
        # Only sealed envelopes reach the transport.
        ops, _, _ = store.exchange_batch({})
        assert ops and all(secret not in str(ops) for secret in (PASSWORD, TOKEN, "192.168.1.20", "api.example.com"))
    exchange(a, b)  # a worker already in the network gets the change itself
    with b.active():
        listed = vault_store.catalog(OWNER)
        assert {item["id"]: item["label"] for item in listed} == {first: "Home server", second: "Inventory API"}
        assert PASSWORD not in json.dumps(listed) and TOKEN not in json.dumps(listed)
        assert vault_store.load(OWNER, first).password.get_secret_value() == PASSWORD
        assert vault_store.load(OWNER, second).secret.get_secret_value() == TOKEN
        # Another principal on the receiving worker still sees nothing.
        assert vault_store.catalog((None, "worker", "api-user")) == []
    at_a, at_b = stored(a), stored(b)
    assert set(at_a) == set(at_b) == {vault_store.key(OWNER, first), vault_store.key(OWNER, second)}
    for key in at_a:
        # Each worker keeps the entry under its own data key, never the sender's ciphertext.
        assert at_a[key].startswith("enc:") and at_b[key].startswith("enc:") and at_a[key] != at_b[key]
        assert all(secret not in at_b[key] for secret in (PASSWORD, TOKEN))
        with b.active(), pytest.raises(AccessDenied):
            vault_store._open(key, at_a[key])


def test_edits_and_removals_reach_the_other_workers_in_both_directions(workers):
    a, b = pair(workers)
    with a.active():
        saved = vault_store.save(OWNER, machine())
        scan()
    exchange(a, b)
    with b.active():
        vault_store.update(OWNER, saved, lambda current: machine(label="Renamed", port=2222, password="replacement-only"))
        scan()
    exchange(b, a)
    with a.active():
        changed = vault_store.load(OWNER, saved)
        assert (changed.label, changed.port, changed.password.get_secret_value()) == ("Renamed", 2222, "replacement-only")
        # The host key the first connection verified travels like any other edit.
        unpinned = vault_store.save(OWNER, machine(label="Unpinned", fingerprint=""))
        assert vault_store.pin_fingerprint(OWNER, unpinned, vault_store.load(OWNER, unpinned), "SHA256:" + "b" * 43)
        vault_store.delete(OWNER, saved)
        scan()
    exchange(a, b)
    with b.active():
        assert [(item["id"], item["fingerprint"]) for item in vault_store.catalog(OWNER)] == [
            (unpinned, "SHA256:" + "b" * 43)]
        with pytest.raises(AccessDenied):
            vault_store.load(OWNER, saved)
    assert set(stored(a)) == set(stored(b)) == {vault_store.key(OWNER, unpinned)}


def test_entries_each_worker_kept_stay_local_until_moved_then_all_show_everywhere(workers):
    a, b = pair(workers)
    scope = vault_store.prefix(OWNER)[len(vault_store.SHARED_PREFIX):]
    kept = {worker: f"{vault_store.LOCAL_PREFIX}{worker.name}:{scope}{letter * 32}" for worker, letter in ((a, "a"), (b, "b"))}
    for worker in (a, b):
        with worker.active():
            # Written as the version before this one wrote them.
            vault_store._store(kept[worker], machine())
            scan()
    exchange(a, b)
    exchange(b, a)
    assert set(stored(a)) == {kept[a]} and set(stored(b)) == {kept[b]}
    for worker in (a, b):
        with worker.active():
            assert vault_store.catalog(OWNER) == []
            assert vault_store.adopt_local_entries() == 1
            scan()
    exchange(a, b)
    exchange(b, a)
    for worker in (a, b):
        assert set(stored(worker)) == {vault_store.key(OWNER, "a" * 32), vault_store.key(OWNER, "b" * 32)}
        with worker.active():
            # Saved once on each worker: both stay, under the same name, until
            # the owner removes one. Neither is dropped or merged for them.
            assert [item["label"] for item in vault_store.catalog(OWNER)] == ["Home server", "Home server"]
            assert vault_store.load(OWNER, "b" * 32).password.get_secret_value() == PASSWORD
            assert vault_store.adopt_local_entries() == 0
    with a.active():
        vault_store.delete(OWNER, "b" * 32)
        scan()
    exchange(a, b)
    with b.active():
        assert [item["id"] for item in vault_store.catalog(OWNER)] == ["a" * 32]


def test_arriving_entries_clear_cached_absence_and_refresh_open_vault_lists(workers, monkeypatch):
    from app.core import vault_access
    from app.core.global_events import global_events
    a, b = pair(workers)
    monkeypatch.setattr(engine, "_listeners", [startup._on_applied])
    emit, grants = Mock(), Mock(wraps=vault_access.saved_changed_elsewhere)
    monkeypatch.setattr(global_events, "resources_changed", emit)
    monkeypatch.setattr(vault_access, "saved_changed_elsewhere", grants)
    # No app loop here; the broker's side is covered in tests/core/test_vault_access.py.
    monkeypatch.setattr(global_events, "_loop", None, raising=False)
    with a.active():
        saved = vault_store.save(OWNER, machine())
        scan()
    with b.active():
        # A chat snapshot remembers for five minutes that this owner has no saved sign-ins.
        assert vault_store.catalog(OWNER, cache_empty=True) == []
    exchange(a, b)
    grants.assert_called_once_with([vault_store.key(OWNER, saved)])
    for kind in ("admin", "api_user", "account", "paired"):
        emit.assert_any_call("vault", user_type=kind)
    with b.active():
        assert [item["id"] for item in vault_store.catalog(OWNER, cache_empty=True)] == [saved]
    # A removal is announced the same way.
    grants.reset_mock()
    with a.active():
        vault_store.delete(OWNER, saved)
        scan()
    exchange(a, b)
    grants.assert_called_once_with([vault_store.key(OWNER, saved)])
    # Other shared settings do not touch the Vault.
    grants.reset_mock()
    emit.reset_mock()
    with a.active():
        database.set_system_setting("tts_model", "synced-voice")
        scan()
    exchange(a, b)
    grants.assert_not_called()
    assert not [call for call in emit.call_args_list if call.args[0] == "vault"]


def test_a_full_copy_from_a_peer_reports_changed_and_removed_sign_ins_by_key(workers, tmp_path):
    """A worker that was away catches up by a whole copy, not an exchange: the
    Vault must still hear which sign-ins changed, to end the grants built on them."""
    from app.network import peer_snapshot
    a, b = pair(workers)
    with a.active():
        removed, changed = vault_store.save(OWNER, machine()), vault_store.save(OWNER, machine(label="Backup"))
        scan()
    exchange(a, b)
    with a.active():
        vault_store.delete(OWNER, removed)
        vault_store.update(OWNER, changed, lambda current: machine(label="Backup", password="replacement-only"))
        scan()
        manifest = peer_snapshot.capture(b.device_id)
        for index in range(len(manifest["pages"])):
            (tmp_path / f"{index}.json").write_bytes(peer_snapshot.page(b.device_id, manifest["transfer"], index))
    committed = []

    def notified(touched):
        assert not b.raw.in_transaction
        committed.extend(touched)
    engine.on_applied(notified)
    with b.active():
        peer_snapshot.apply(manifest, tmp_path, a.device_id)
        assert [item["id"] for item in vault_store.catalog(OWNER)] == [changed]
        assert vault_store.load(OWNER, changed).password.get_secret_value() == "replacement-only"
    # By key, which is what startup._on_applied passes to the Vault (previous test).
    assert ("system_settings", vault_store.key(OWNER, removed), "delete") in committed
    assert ("system_settings", vault_store.key(OWNER, changed), "upsert") in committed


@pytest.mark.parametrize("arrives", ["a removal", "a new password"])
@pytest.mark.parametrize("write", ["an edit", "a host key pin"])
def test_a_write_here_never_goes_over_what_another_worker_changed_meanwhile(workers, monkeypatch, write, arrives):
    """Sync applies another worker's change without the store's lock, so it can
    land after an edit or a pin read the entry and before it writes."""
    a, b = pair(workers)
    with a.active():
        saved = vault_store.save(OWNER, machine(fingerprint=""))
        scan()
    exchange(a, b)
    with a.active():
        if arrives == "a removal":
            vault_store.delete(OWNER, saved)
        else:
            vault_store.update(OWNER, saved, lambda current: machine(fingerprint="", password="replacement-only"))
        scan()

    def held():
        try:
            return vault_store.load(OWNER, saved).password.get_secret_value()
        except AccessDenied:
            return None

    stands = None if arrives == "a removal" else "replacement-only"
    with b.active():
        connecting = vault_store.load(OWNER, saved)
        if write == "an edit":
            def renamed(current):
                exchange(a, b)  # lands between the edit's read and its write
                return current.model_copy(update={"label": "Renamed here"})
            with pytest.raises(AccessDenied, match="changed or removed on another worker"):
                vault_store.update(OWNER, saved, renamed)
        else:
            same_connection = vault_store.same_connection

            def compared(current, expected):
                exchange(a, b)  # lands between the pin's read and its write
                return same_connection(current, expected)
            monkeypatch.setattr(vault_store, "same_connection", compared)
            assert not vault_store.pin_fingerprint(OWNER, saved, connecting, "SHA256:" + "b" * 43)
        assert held() == stands
        scan()
    exchange(b, a)
    with a.active():
        # Nothing stale was published back: the other worker's change stands on both.
        assert held() == stands
