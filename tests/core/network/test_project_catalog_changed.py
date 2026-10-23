"""A project deleted on one worker leaves every peer's selectors (#434)."""

import asyncio
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.core.global_events import global_events
from app.db import database
from app.network import control, engine, project_catalog, transport
from app.db import network as store
from tests.core.network.harness import exchange
from tests.core.network.test_runner_git_operations import (
    PROJECT_ID,
    _cluster_app,
    _join_with_distinct_legacy_ids,
    _seed_project,
    _serve_peer,
)


def test_project_deleted_on_the_owner_leaves_the_peer_catalog(workers, monkeypatch, tmp_path):
    a, b = _join_with_distinct_legacy_ids(workers, monkeypatch)
    project = _seed_project(b, tmp_path)
    with b.active():
        engine.scan_all()
    exchange(b, a)
    _serve_peer(monkeypatch, b)
    with a.active():
        store.touch_member(b.device_id, endpoints=["http://peer.local:8000"])
        transport.note_contact(b.device_id)
        with TestClient(_cluster_app()) as client:
            rows = client.get("/api/v1/cluster/projects").json()["projects"]
            assert [row["project_v2_id"] for row in rows] == [project["pv2"]["id"]]

            with b.active():
                database.delete_project(PROJECT_ID)

            assert client.get("/api/v1/cluster/projects").json()["projects"] == []


def test_announce_reaches_each_online_peer_and_survives_failures(monkeypatch):
    class Ctx:
        device_id = "me"

    monkeypatch.setattr(engine, "context", lambda: Ctx())
    monkeypatch.setattr(transport, "online_devices", lambda: {"me": 0.1, "peer-1": 1.0, "peer-2": 2.0})
    call = AsyncMock(side_effect=[{"ok": True}, control.RunnerUnreachable("old worker")])
    monkeypatch.setattr(control, "call", call)

    asyncio.run(project_catalog.announce())

    assert sorted(c.args[:2] for c in call.await_args_list) == [
        ("peer-1", project_catalog.ACTION),
        ("peer-2", project_catalog.ACTION),
    ]


def test_announce_is_a_no_op_without_a_network(monkeypatch):
    monkeypatch.setattr(engine, "context", lambda: None)
    call = AsyncMock()
    monkeypatch.setattr(control, "call", call)
    asyncio.run(project_catalog.announce())
    call.assert_not_awaited()


def test_peer_handler_refreshes_every_browser_kind(monkeypatch):
    seen = []
    monkeypatch.setattr(
        global_events, "resources_changed",
        lambda *resources, user_type="admin", user_id=None: seen.append((resources, user_type)),
    )
    assert control.known_action(project_catalog.ACTION)

    assert asyncio.run(control.handle(project_catalog.ACTION, {}, from_device="peer")) == {"ok": True}

    assert seen == [(("projects",), kind) for kind in ("admin", "api_user", "account", "paired")]
