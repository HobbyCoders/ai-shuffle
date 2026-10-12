"""Permission-only preservation must cross the ordinary peer catch-up boundary."""
import httpx
import pytest

from app.core.builtin_subagents import seed_builtin_subagents
from app.db import database, network as store
from app.network import engine, peer_snapshot, transport
from tests.builtin_fixtures import create_unchanged_builtin
from tests.core.network.harness import join, exchange, converge
from tests.core.network.test_peer_state_preparation import capture_jobs  # noqa: F401
from tests.core.network.test_transport import ServedBy


def prepare(workers, monkeypatch, role):
    a, b = workers("upgraded-with-stale-default"), workers("permission-editor")
    with monkeypatch.context() as old:
        old.setattr(engine, "RETIRED_BUILTIN_IDS", ("Explore", "Plan"))
        with a.active():
            engine.create_network("permission preservation")
            create_unchanged_builtin(role)
            engine.scan_all()
        join(a, b)
        converge([a, b])
    with b.active():
        rule = database.add_permission_rule(role, "Bash", "*", "deny")
        engine.scan_all()
        with database.get_db() as conn:
            version = store.get_record(conn.cursor(), "agents", role)
    return a, b, rule, version


def upgrade(a, b, role, editor_first):
    for worker in ((b, a) if editor_first else (a, b)):
        with worker.active():
            with database.get_db() as conn:
                store.bump_clock(conn.cursor(), observed=500)
            seed_builtin_subagents()
            engine.scan_all()
    with a.active():
        assert database.get_profile(role) is None
    with b.active():
        assert not database.get_profile(role)["is_builtin"]


@pytest.mark.parametrize("role", ["general-purpose", "frontend-designer"])
@pytest.mark.parametrize("editor_first", [False, True])
@pytest.mark.parametrize("scenario", ["permission-only", "rule-deleted", "newer-owner-edit"])
def test_permission_only_owner_edit_reaches_peer_after_upgrade(
        workers, monkeypatch, role, editor_first, scenario):
    a, b, rule, version = prepare(workers, monkeypatch, role)
    upgrade(a, b, role, editor_first)
    if scenario == "rule-deleted":
        with b.active():
            database.delete_permission_rule(rule["id"])
            engine.scan_all()
    if scenario == "newer-owner-edit":
        with a.active():
            database.create_profile(role, "Newer owner agent", None, {})
            engine.scan_all()
            with database.get_db() as conn:
                version = store.get_record(conn.cursor(), "agents", role)
    with a.active():
        marks = store.local_watermarks()
    with b.active():
        assert store.exchange_batch(marks, exclude_origin=a.device_id)[2]
    converge([a, b], rounds=5)
    for worker, peer in ((a, b), (b, a)):
        result = exchange(peer, worker)
        assert result.retry == result.pending == result.rejected == 0, result.reasons
        with worker.active():
            row = database.get_profile(role)
            assert row is not None and not row["is_builtin"]
            if scenario == "newer-owner-edit":
                assert row["name"] == "Newer owner agent"
            assert database.get_permission_rules(role) == ([] if scenario == "rule-deleted" else [rule])
            with database.get_db() as conn:
                held = store.get_record(conn.cursor(), "agents", role)
            assert (held["clock"], held["origin_device_id"]) == (version["clock"], version["origin_device_id"])
            marks = store.local_watermarks()
        with peer.active():
            assert not store.exchange_batch(marks, exclude_origin=worker.device_id)[2]


@pytest.mark.parametrize("role", ["general-purpose", "frontend-designer"])
@pytest.mark.parametrize("editor_first", [False, True])
@pytest.mark.parametrize("cache", ["future", "disk"])
async def test_permission_preservation_via_peer_http_with_pre_upgrade_cache(
        workers, monkeypatch, role, editor_first, cache, capture_jobs):
    a, b, rule, version = prepare(workers, monkeypatch, role)
    with b.active():
        old = peer_snapshot.state(a.device_id) if cache == "future" else peer_snapshot.capture(a.device_id)
    upgrade(a, b, role, editor_first)
    with b.active():
        fresh = peer_snapshot.state(a.device_id) if cache == "future" else peer_snapshot.capture(a.device_id)
        assert fresh["watermarks"] == old["watermarks"]  # no fresh winning clock
        assert fresh["transfer"] != old["transfer"]
        assert role in fresh["preserved_builtin_ids"]
    served = ServedBy(b, "https://permission-editor.example.com")
    with a.active():
        async with httpx.AsyncClient(transport=served.transport()) as client:
            link = transport.PeerLink(b.device_id, [served.base_url])
            result = await link.sync_once(client, engine.context(), catch_up=True)
        assert result["snapshot_required"]
        assert database.get_profile(role) is not None
        assert not database.get_profile(role)["is_builtin"]
        assert database.get_permission_rules(role) == [rule]
        with database.get_db() as conn:
            held = store.get_record(conn.cursor(), "agents", role)
        assert (held["clock"], held["origin_device_id"]) == (version["clock"], version["origin_device_id"])
        marks = store.local_watermarks()
    with b.active():
        assert not store.exchange_batch(marks, exclude_origin=a.device_id)[2]
