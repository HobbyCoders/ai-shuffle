"""Review reproducer: edits on an old peer must survive either upgrade order."""
import pytest

from app.core.builtin_subagents import seed_builtin_subagents
from app.db import database, network as store
from app.network import engine
from tests.builtin_fixtures import create_unchanged_builtin
from tests.core.network.harness import join, exchange, converge


@pytest.mark.parametrize("role", ["general-purpose", "frontend-designer"])
@pytest.mark.parametrize("edited_peer_first", [False, True], ids=["old-peer-edit", "upgraded-peer-edit"])
@pytest.mark.parametrize("transfer", ["incremental", "snapshot", "forwarded"])
def test_newer_edit_on_old_peer_survives_upgrade_and_converges(
        workers, monkeypatch, role, edited_peer_first, transfer):
    a, b, relay = workers("stale-peer"), workers("newest-owner-edit"), workers("relay")
    with monkeypatch.context() as old_build:
        old_build.setattr(engine, "RETIRED_BUILTIN_IDS", ("Explore", "Plan"))
        with a.active():
            engine.create_network("review fixture")
            create_unchanged_builtin(role)
            database.update_profile(role, name="Original owner edit", allow_builtin=True)
            engine.scan_all()
        join(a, b)
        join(a, relay)
        converge([a, b, relay])
    first, last = (b, a) if edited_peer_first else (a, b)
    with first.active():
        seed_builtin_subagents()
        engine.scan_all()
    with relay.active():
        seed_builtin_subagents()
    with b.active():
        database.update_profile(role, name="Newest owner edit on old peer", allow_builtin=True)
        engine.scan_all()
        with database.get_db() as conn:
            original_order = store.get_record(conn.cursor(), "agents", role)
        snapshot = engine.snapshot_chunk("agents", 0) if transfer == "snapshot" else None
    if transfer == "incremental":
        exchange(b, a)
    elif transfer == "snapshot":
        with a.active():
            engine.apply_snapshot_chunk(snapshot)
    else:
        exchange(b, relay)
        exchange(relay, a)  # verifies forwarding the original signed envelope
    with a.active():
        assert database.get_profile(role)["name"] == "Newest owner edit on old peer"
    with last.active():
        # Unrelated local activity must not turn migration upkeep into a new
        # winning agent write. Keep the writer's original ordering everywhere.
        with database.get_db() as conn:
            store.bump_clock(conn.cursor(), observed=500)
        seed_builtin_subagents()
        engine.scan_all()
    converge([a, b, relay])
    for worker in (a, b, relay):
        with worker.active():
            row = database.get_profile(role)
            assert not row["is_builtin"]
            assert row["name"] == "Newest owner edit on old peer"
            with database.get_db() as conn:
                synced = store.get_record(conn.cursor(), "agents", role)
            assert (synced["clock"], synced["origin_device_id"]) == (
                original_order["clock"], original_order["origin_device_id"])
