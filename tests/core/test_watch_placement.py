"""Two isolated fake worker databases exercise placement and fenced cursor hand-over."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest import MonkeyPatch

from app.core import watches, watch_placement as placement, watch_manager as manager
from app.core.config import settings
from app.db import base, database, watches as store
from app.network import control, runners, policy
from tests.core.test_watches import owner, make_watch  # noqa: F401
from tests.pg_shim import ShimConnection


@pytest.fixture
def network(
    owner: dict[str, Any], test_db: sqlite3.Connection, monkeypatch: MonkeyPatch
) -> Iterator[Any]:
    database.delete_system_setting(placement.PREFERENCE)
    remote = sqlite3.connect(":memory:", check_same_thread=False)
    remote.row_factory = sqlite3.Row
    test_db.backup(remote)
    remote.execute("DELETE FROM sessions")
    remote.commit()
    shims = {"DesktopWorker": ShimConnection(test_db), "ServerWorker": ShimConnection(remote)}
    current = ["DesktopWorker"]
    devices = {"DesktopWorker": "dev_desktop", "ServerWorker": "dev_server"}
    monkeypatch.setattr(runners, "self_device_id", lambda: devices[current[0]])
    monkeypatch.setattr(settings, "instance_id", current[0])
    test_db.execute("UPDATE sessions SET instance_id=? WHERE id=?", (current[0], owner["id"]))
    test_db.commit()

    def connection() -> Any:
        return shims[current[0]]

    monkeypatch.setattr(base, "get_connection", connection)
    monkeypatch.setattr(database, "get_connection", connection)

    @contextmanager
    def on(worker: str) -> Iterator[None]:
        previous = current[0]
        current[0] = worker
        monkeypatch.setattr(settings, "instance_id", worker)
        try:
            yield
        finally:
            current[0] = previous
            monkeypatch.setattr(settings, "instance_id", previous)

    def directory() -> dict[str, Any]:
        return {"runners": [
            dict(id=devices[name], online=True, is_self=name == current[0],
                 legacy_instance_id=name, platform=platform, capabilities={}, device_name=name)
            for name, platform in [("DesktopWorker", "windows"), ("ServerWorker", "linux")]
        ]}

    async def rpc(device_id: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        sender = devices[current[0]]
        assert action == "watches" and control.known_action(action)
        receiver = next(name for name, device in devices.items() if device == device_id)
        with on(receiver):
            return await control.handle(action, body, from_device=sender)

    monkeypatch.setattr(runners, "directory", directory)
    monkeypatch.setattr(control, "call", rpc)
    yield on
    remote.close()


def test_default_override_and_unavailable(
    owner: dict[str, Any], network: Any, monkeypatch: MonkeyPatch
) -> None:
    assert placement.resolve() == "dev_server"
    assert placement.resolve("DesktopWorker") == "DesktopWorker"
    database.set_system_setting(placement.PREFERENCE, "DesktopWorker")
    assert placement.resolve() == "DesktopWorker"
    database.set_system_setting(placement.PREFERENCE, "ServerWorker")
    listing = runners.directory()
    listing["runners"][1]["online"] = False
    monkeypatch.setattr(runners, "directory", lambda: listing)
    with pytest.raises(ValueError, match="unavailable"):
        placement.resolve()
    assert placement.resolve("DesktopWorker") == "DesktopWorker"
    assert policy.is_shared_setting_key(placement.PREFERENCE)
    assert policy.is_shared_setting_key("watch_directory:ServerWorker")


@pytest.mark.asyncio
async def test_remote_create_list_controls_and_creator_offline(
    owner: dict[str, Any], network: Any, monkeypatch: MonkeyPatch
) -> None:
    from app.core.permission_handler import permission_handler
    from app.core.agent_types import PermissionResultAllow

    approve = AsyncMock(return_value=PermissionResultAllow())
    monkeypatch.setattr(permission_handler, "request_permission", approve)
    result = await watches.create_from_conversation(owner["id"], {
        "description": "server schedule", "source": {"kind": "schedule", "interval_seconds": 60}
    })
    wid = result["watch"]["id"]
    assert "ServerWorker" in approve.call_args.kwargs["description"]
    assert store.get(wid) is None
    assert (await placement.rows())[0]["worker_id"] == "ServerWorker"
    assert (await placement.change(wid, "paused"))["status"] == "paused"
    await placement.change(wid, "active")
    # No creator row exists at all in the server's database. The executing
    # worker still detects, reports and checkpoints without contacting it.
    monkeypatch.setattr(manager, "summarize", AsyncMock(return_value=("Reminder", 0.001)))
    monkeypatch.setattr(manager, "notify", AsyncMock())
    with network("ServerWorker"):
        assert database.get_session(owner["id"]) is None
        await manager.WatchManager().run_once()
        event = store.events(wid, ("delivered",))[0]
        assert event["status"] == "delivered"
        cursor = store.get(wid)["cursor"]
        await manager.WatchManager().run_once()
        assert len(store.events(wid, ("delivered",))) == 1
        assert store.get(wid)["cursor"] == cursor
    assert (await placement.change(wid, "stopped"))["status"] == "stopped"
    assert (await placement.change(wid, "active"))["status"] == "stopped"


@pytest.mark.asyncio
async def test_move_cursor_outbox_dedupe_retry_and_return(
    owner: dict[str, Any], network: Any
) -> None:
    watch = make_watch(owner, worker_id="DesktopWorker")
    wid = watch["id"]
    assert store.checkpoint(watch, [("one", {"text": "one"})], {"next_at": "2099-01-01"})
    original_event = store.events(wid)[0]
    moved = await placement.move(wid, "ServerWorker")
    assert moved["cursor"] == {"next_at": "2099-01-01"}
    assert store.get(wid)["worker_id"] == "transfer:dev_server"
    assert not store.checkpoint(watch, [("stale", {})], {"bad": True})
    assert not store.reserve(wid, original_event["id"], 0.25)
    with network("ServerWorker"):
        assert store.events(wid)[0]["id"] == original_event["id"]
        current = store.get(wid)
        assert store.checkpoint(current, [("one", {})], current["cursor"])
        assert len(store.events(wid)) == 1
        snapshot = store.export_watch(wid, "DesktopWorker")
        assert store.export_watch(wid, "DesktopWorker")["token"] == snapshot["token"]
        row = placement.execution_row(snapshot["watch"])
    # Complete the reverse move using the same frozen snapshot twice: this is
    # also a receiver-committed / acknowledgement-lost retry.
    answer = await placement.call(
        "DesktopWorker", "import", snapshot=snapshot, source="ServerWorker"
    )
    retry = await placement.call(
        "DesktopWorker", "import", snapshot=snapshot, source="ServerWorker"
    )
    assert retry["watch"]["id"] == answer["watch"]["id"] == wid
    assert store.get(wid)["cursor"] == moved["cursor"]
    assert store.events(wid)[0]["id"] == original_event["id"]
    assert row["worker_id"] == "ServerWorker"
    bad = dict(snapshot, target="ServerWorker")
    with pytest.raises(ValueError, match="another worker"):
        await placement.call("DesktopWorker", "import", snapshot=bad, source="ServerWorker")


def test_transfer_refuses_inflight_and_preserves_pause(
    owner: dict[str, Any], network: Any
) -> None:
    watch = make_watch(owner, worker_id="DesktopWorker")
    store.append_events(watch, [("one", {})])
    eid = store.events(watch["id"])[0]["id"]
    assert store.reserve(watch["id"], eid, 0.25)
    with pytest.raises(ValueError, match="in-flight"):
        store.export_watch(watch["id"], "ServerWorker")
    assert store.get(watch["id"])["status"] == "active"
    second = make_watch(owner, worker_id="DesktopWorker")
    store.set_status(second["id"], "admin", "paused")
    snapshot = store.export_watch(second["id"], "ServerWorker")
    assert snapshot["watch"]["status"] == "paused"
    assert store.set_status(second["id"], "admin", "active")["status"] == "paused"


@pytest.mark.asyncio
async def test_catalog_sync_is_display_only(
    owner: dict[str, Any], network: Any, monkeypatch: MonkeyPatch
) -> None:
    from app.network import engine
    from types import SimpleNamespace

    watch = make_watch(owner, worker_id="DesktopWorker")
    ctx = SimpleNamespace(device_id="DesktopWorker")
    monkeypatch.setattr(engine, "cached_context", lambda: ctx)
    placement.publish()
    value = database.get_system_setting("watch_directory:DesktopWorker")
    with network("ServerWorker"):
        database.set_system_setting("watch_directory:DesktopWorker", value)
        assert placement.cached_rows()[0]["id"] == watch["id"]
        assert store.get(watch["id"]) is None
        assert not any(c.table == "watches" for c in policy.COLLECTIONS.values())

def test_owner_preference_api(owner: dict[str, Any], network: Any) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import watches as api
    from app.api.auth import require_admin

    app = FastAPI()
    app.include_router(api.router)
    with TestClient(app) as client:
        path = "/api/v1/watches/preference"
        assert client.put(path, json={"worker_id": "ServerWorker"}).status_code in (401, 403)
        app.dependency_overrides[require_admin] = lambda: {"id": "admin"}
        response = client.put(path, json={"worker_id": "ServerWorker"})
        assert response.status_code == 200
        assert client.get("/api/v1/watches").json()["preferred_worker_id"] == "ServerWorker"
        assert client.put(path, json={"worker_id": "missing"}).status_code == 409
        assert client.put(path, json={"worker_id": None}).status_code == 200
        assert client.get("/api/v1/watches").json()["preferred_worker_id"] is None


@pytest.mark.parametrize(("error", "status", "detail"), [
    (control.ControlRefused("Telegram watches cannot move"), 409, "Telegram watches cannot move"),
    (ValueError("Wait for in-flight deliveries"), 409, "Wait for in-flight deliveries"),
    (control.MaybeApplied("ServerWorker did not answer in time"), 503, "retry the same operation"),
    (control.RunnerUnreachable("offline"), 503, "retry the same operation"),
])
def test_move_api_separates_refusal_from_unknown_outcome(
    owner: dict[str, Any], network: Any, monkeypatch: MonkeyPatch,
    error: Exception, status: int, detail: str,
) -> None:
    """The panel shows a refusal's reason; an unknown outcome must not read as done."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import watches as api
    from app.api.auth import require_admin

    monkeypatch.setattr(placement, "move", AsyncMock(side_effect=error))
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_admin] = lambda: {"id": "admin"}
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.patch("/api/v1/watches/w1", json={"worker_id": "ServerWorker"})
    assert response.status_code == status
    assert detail in response.json()["detail"]


@pytest.mark.asyncio
async def test_owner_confirmed_move_keeps_scope(
    owner: dict[str, Any], network: Any, monkeypatch: MonkeyPatch
) -> None:
    from app.core.permission_handler import permission_handler
    from app.core.agent_types import PermissionResultAllow

    watch = make_watch(owner, worker_id="DesktopWorker")
    approve = AsyncMock(return_value=PermissionResultAllow())
    monkeypatch.setattr(permission_handler, "request_permission", approve)
    result = await watches.create_from_conversation(owner["id"], {
        "move_watch_id": watch["id"], "worker_id": "ServerWorker"
    })
    assert "Move this existing watch?" in approve.call_args.kwargs["description"]
    assert result["watch"]["source"] == watch["source"]
    assert result["watch"]["permissions"] == watch["permissions"]
    assert result["watch"]["id"] == watch["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_operation", [None, "import", "complete"])
async def test_move_retry_then_reverse_with_distinct_device_ids(
    owner: dict[str, Any], network: Any, monkeypatch: MonkeyPatch, lost_operation: str | None
) -> None:
    watch = make_watch(owner, worker_id="DesktopWorker")
    wid = watch["id"]
    assert store.checkpoint(watch, [("first", {})], {"next_at": "2099-01-01"})
    event = store.events(wid)[0]
    dispatch = placement.call
    lost = False

    async def call(worker_id: str, operation: str, **body: Any) -> dict[str, Any]:
        nonlocal lost
        result = await dispatch(worker_id, operation, **body)
        if operation == lost_operation and not lost:
            lost = True
            raise control.RunnerUnreachable("acknowledgement lost")
        return result

    monkeypatch.setattr(placement, "call", call)
    if lost_operation is not None:
        with pytest.raises(control.RunnerUnreachable):
            await placement.move(wid, "ServerWorker")
        # Retry from the original worker must finish its outstanding receipt.
        await placement.move(wid, "ServerWorker")
    else:
        await placement.move(wid, "ServerWorker")
    with base.get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM watch_transfers WHERE watch_id=%s", (wid,))
        assert c.fetchone()["completed"]
    with network("ServerWorker"):
        returned = await placement.move(wid, "DesktopWorker")
        assert returned["worker_id"] == "DesktopWorker"
        assert store.get(wid)["worker_id"] == "transfer:dev_desktop"
    assert store.get(wid)["worker_id"] == "DesktopWorker"
    assert store.get(wid)["cursor"] == {"next_at": "2099-01-01"}
    assert store.events(wid)[0]["id"] == event["id"]
    assert len(store.events(wid)) == 1

