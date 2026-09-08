"""Partitioned revocations retain decryptability and converge to a safe write key."""
import hashlib
from app.core import owner_auth

import pytest

from app.db import database
from app.db import network as store
from app.network import engine, tokens, transport
from tests.core.network.harness import Worker, exchange, join


@pytest.fixture
def workers(tmp_path):
    made = []
    def make(name):
        worker = Worker(name, tmp_path)
        made.append(worker)
        return worker
    yield make
    for worker in made:
        worker.close()
    engine.reset_for_tests()
    tokens.reset_for_tests()
    transport.reset_for_tests()


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("legacy_storage", [False, True])
def test_partitioned_revocations_retain_both_keys_and_exclude_both_devices(workers, reverse, legacy_storage):
    a, b, c, d = [workers(name) for name in "abcd"]
    with a.active():
        engine.create_network("Rotation regression")
    for peer in (b, c, d):
        join(a, peer)
    for peer in (b, c, d):
        exchange(a, peer)
        exchange(peer, a)
    for origin, revoked, label in ((a, c, "a"), (b, d, "b")):
        with origin.active():
            engine.revoke_device(revoked.device_id)
            database.set_user_preference("admin", owner_auth.preference_identity(), "partition-" + label, label)
            engine.scan_all()
    if legacy_storage:
        # The previous release stored only numeric epoch -> wrapped-key entries.
        # Preserve these keys when a concurrent alternate first arrives.
        for worker in (a, b):
            with worker.active():
                wrapped = store.get_state()["keyring"]
                store.update_state(keyring={label: value for label, value in wrapped.items() if label.isdigit()})
                engine.load_context()
    first, second = (b, a) if reverse else (a, b)
    results = [exchange(first, second), exchange(second, first)]
    for _ in range(4):
        results += [exchange(a, b), exchange(b, a)]
    assert all(result.rejected == 0 for result in results), [result.reasons for result in results]
    assert a.context.network_key == b.context.network_key
    assert a.context.key_version >= 3
    for worker in (a, b):
        with worker.active():
            assert database.get_user_preference("admin", owner_auth.preference_identity(), "partition-a")["value"] == "a"
            assert database.get_user_preference("admin", owner_auth.preference_identity(), "partition-b")["value"] == "b"
            restored = engine.load_context()
            assert len(restored.key_candidates[2]) == 2
            digest = hashlib.sha256(restored.network_key).hexdigest()
            assert not ({c.device_id, d.device_id} & restored.key_recipients[digest])
    # A post-merge enrollment must carry historical candidates, not just the
    # selected write key, so delayed encrypted records remain readable.
    e = workers("e")
    join(a, e)
    with e.active():
        assert len(engine.context().key_candidates[2]) == 2
        assert engine.context().network_key == a.context.network_key
