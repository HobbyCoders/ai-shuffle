"""Worker display names converge independently of membership certificates."""
import pytest

from app.db import network as store
from app.network import engine, hub, runners
from tests.core.network.harness import converge, exchange, join
from tests.core.network.test_api import workers, make_client


def names(worker, device_id):
    with worker.active():
        return (
            store.get_member(device_id)["name"],
            engine.context().member(device_id).name,
            next(r["device_name"] for r in runners.directory()["runners"] if r["id"] == device_id),
        )


@pytest.mark.parametrize("rename_on_owner", [True, False])
def test_rename_reaches_owner_peers_and_new_joiner(workers, make_client, monkeypatch, rename_on_owner):
    a, b, c = workers("unraid"), workers("gaming-pc"), workers("new-worker")
    monkeypatch.setattr(hub, "kick", lambda: None)
    with a.active():
        engine.create_network("Home", device_name="62b69c81fbdf")
    join(a, b, name="DesktopWorker")
    editor = a if rename_on_owner else b
    with editor.active():
        response = make_client().patch(f"/api/v1/network/members/{a.device_id}", json={"name": "Unraid"})
        assert response.status_code == 200
        assert response.json()["name"] == "Unraid"
    converge([a, b])
    for worker in (a, b):
        assert names(worker, a.device_id) == ("Unraid",) * 3
    # Heartbeats and scanners must not restore the certificate's old hostname.
    for worker in (a, b):
        with worker.active():
            engine.publish_presence(version="test")
            engine.scan_all()
    converge([a, b])
    join(b, c)
    for worker in (a, b, c):
        assert names(worker, a.device_id) == ("Unraid",) * 3


def test_existing_local_name_is_published_without_renaming_again(workers):
    a, b = workers("unraid"), workers("gaming-pc")
    with a.active():
        engine.create_network("Home", device_name="62b69c81fbdf")
    join(a, b)
    with a.active():
        # The old rename path wrote only this local row.
        with store.get_db() as conn:
            conn.cursor().execute("UPDATE network_members SET name = %s WHERE device_id = %s", ("Unraid", a.device_id))
        engine.load_context()
        engine.publish_presence(version="test")
    result = exchange(a, b)
    assert result.rejected == 0
    assert names(b, a.device_id) == ("Unraid",) * 3


def test_concurrent_renames_converge_and_refresh_connected_views(workers, make_client, monkeypatch):
    from unittest.mock import Mock
    from app.core.global_events import global_events
    from app.network import startup

    a, b = workers("unraid"), workers("gaming-pc")
    with a.active():
        engine.create_network("Home", device_name="Container")
    join(a, b)
    emit = Mock()
    monkeypatch.setattr(global_events, "resources_changed", emit)
    monkeypatch.setattr(engine, "_listeners", [startup._on_applied])
    for worker, name in ((a, "Unraid"), (b, "Server")):
        with worker.active():
            response = make_client().patch(f"/api/v1/network/members/{a.device_id}", json={"name": name})
            assert response.status_code == 200
    converge([a, b])
    assert names(a, a.device_id) == names(b, a.device_id)
    for kind in ("admin", "api_user", "account", "paired"):
        emit.assert_any_call("network", user_type=kind)
    # Reloading an older certificate must not undo the synchronized name.
    with b.active():
        member = store.get_member(a.device_id)
        store.upsert_member(device_id=a.device_id, name="Container", kind=member["kind"],
                            signing_public=member["signing_public"], encryption_public=member["encryption_public"],
                            generation=member["generation"])
        engine.load_context()
    assert names(a, a.device_id) == names(b, a.device_id)


def test_member_name_schema_is_idempotent_and_cleared_on_leave(workers):
    worker = workers("unraid")
    with worker.active():
        engine.create_network("Home", device_name="Unraid")
        store.rename_member(worker.device_id, "Server")
        with store.get_db() as conn:
            store.create_schema(conn.cursor())
            store.create_schema(conn.cursor())
        assert store.get_member(worker.device_id)["name"] == "Server"
        engine.leave_network()
        with store.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM network_member_names")
            assert cursor.fetchall() == []
