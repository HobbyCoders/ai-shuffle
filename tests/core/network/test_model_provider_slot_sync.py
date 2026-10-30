"""Real network row/selection merge, including concurrent offline switches."""
import pytest

from app.core import encryption
from app.db import database as db
from app.db.model_provider_slots import SLOT_SETTING_PREFIX
from app.network import engine, policy
from tests.core.network.harness import converge, exchange, join


def scan():
    return engine.scan_all(include_catalog=False, collections=["system_settings", "model_providers"])


def enabled():
    return [r["id"] for r in db.get_enabled_model_providers()]


def pair(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Saved accounts")
        for id in ("one", "two"):
            db.create_model_provider(id, id, "openai", "https://api.openai.com/v1", enabled=(id == "one"))
        scan()
    join(a, b)
    converge([a, b])
    return a, b


def test_peer_activation_selects_only_one_account(workers):
    a, b = pair(workers)
    with b.active():
        db.set_model_provider_enabled("two", True)
        scan()
    result = exchange(b, a)
    assert result.rejected == result.retry == 0, result.reasons
    with a.active():
        assert enabled() == ["two"]
        assert db.resolve_model_provider("one")["id"] == "two"


@pytest.mark.parametrize("reverse", [False, True])
def test_concurrent_offline_switches_converge_independent_of_delivery(workers, reverse):
    a, b = pair(workers)
    for worker, id in ((a, "one"), (b, "two")):
        with worker.active():
            db.set_model_provider_enabled(id, True)
            scan()
    order = [b, a] if reverse else [a, b]
    converge(order, rounds=4)
    for worker in order:
        with worker.active():
            scan()  # include derived enabled projections
    converge(order, rounds=4)
    selections = []
    for worker in order:
        with worker.active():
            selections.append(enabled())
            assert len(selections[-1]) == 1
    assert selections[0] == selections[1]


def test_selection_arriving_before_row_never_uses_wrong_account(workers):
    a, _ = pair(workers)
    with a.active(), db.get_db() as conn:
        cursor = conn.cursor()
        engine.materialize(cursor, policy.collection("system_settings"), SLOT_SETTING_PREFIX + "openai:key",
                           {"key": SLOT_SETTING_PREFIX + "openai:key", "value": "later"})
        assert not enabled()
        engine.materialize(cursor, policy.collection("model_providers"), "later",
                           {"id": "later", "name": "Later", "provider_type": "openai",
                            "base_url": "https://api.openai.com/v1", "enabled": True})
    with a.active():
        assert enabled() == ["later"]


def test_stale_or_legacy_enabled_row_cannot_override_shared_selection(workers):
    a, _ = pair(workers)
    with a.active(), db.get_db() as conn:
        engine.materialize(conn.cursor(), policy.collection("model_providers"), "two",
                           {"id": "two", "name": "Stale", "provider_type": "openai",
                            "base_url": "https://api.openai.com/v1", "enabled": True})
    with a.active():
        assert enabled() == ["one"]


def test_explicit_slot_off_syncs_without_auto_enabling_a_sibling(workers):
    a, b = pair(workers)
    with b.active():
        db.set_model_provider_enabled("one", False)
        scan()
    exchange(b, a)
    for worker in (a, b):
        with worker.active():
            assert not enabled()
