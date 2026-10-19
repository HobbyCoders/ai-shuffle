"""Lifecycle API producers converge for owned sessions and foreign catalog mirrors."""
from unittest.mock import AsyncMock

import pytest
from fastapi import BackgroundTasks, HTTPException, Request

from app.api import sessions as api
from app.db import database, network as store
from app.network import control, engine
from tests.core.network.harness import converge, join
from tests.core.network.test_control import _new_session
from tests.core.network.test_session_active_control import connect_owner


@pytest.mark.parametrize("owner_index", [0, 1])
@pytest.mark.parametrize("from_mirror", [False, True])
@pytest.mark.parametrize("batch", [False, True])
async def test_lifecycle_converges(workers, monkeypatch, owner_index, from_mirror, batch):
    pair = [workers("a"), workers("b")]
    owner, mirror = pair[owner_index], pair[1 - owner_index]
    actor = mirror if from_mirror else owner
    with pair[0].active():
        engine.create_network("Lifecycle")
    join(*pair)
    served = connect_owner(monkeypatch, owner)
    monkeypatch.setattr(api, "_cleanup_session_artifacts", AsyncMock())
    _new_session(owner, "lifecycle", "Lifecycle")
    converge(pair)
    with actor.active():
        store.touch_member(owner.device_id, endpoints=[served.base_url])
    request = Request({"type": "http", "headers": []})
    for active, archived in ((False, None), (True, None), (False, True), (True, False)):
        with actor.active():
            await api.set_session_active(
                request, "lifecycle", api.SessionActiveUpdate(is_active=active, archived=archived), token="admin",
            )
        for worker in pair:
            with worker.active():
                engine.scan_all()
        converge(pair)
        for worker in pair:
            with worker.active():
                row = database.get_session("lifecycle")
                assert bool(row["is_active"]) is active
                assert (row["status"] == "archived") is bool(archived)
                assert row["owner_device_id"] == owner.device_id
    with actor.active():
        if batch:
            result = await api.batch_delete_sessions(
                request, api.BatchDeleteRequest(session_ids=["lifecycle"]), BackgroundTasks(), token="admin",
            )
            assert result["deleted_count"] == 1, result
        else:
            await api.delete_session(request, "lifecycle", BackgroundTasks(), token="admin")
    for worker in pair:
        with worker.active():
            engine.scan_all()
    converge(pair)
    for worker in pair:
        with worker.active():
            assert database.get_session("lifecycle") is None
            record = worker.sql("SELECT * FROM sync_records WHERE collection='sessions' AND record_id='lifecycle'")[0]
            assert record["deleted"]
            assert record["origin_device_id"] == owner.device_id


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("failure", ["offline", "old_owner", "unconfirmed"])
async def test_failed_remote_delete_keeps_mirror(workers, monkeypatch, batch, failure):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Lifecycle")
    join(a, b)
    _new_session(a, "kept", "Kept")
    converge([a, b])
    call = AsyncMock(return_value={})
    if failure == "offline":
        call.side_effect = control.RunnerUnreachable("offline")
    elif failure == "old_owner":
        call.side_effect = control.ControlRefused("Unknown control action")
    monkeypatch.setattr(control, "call", call)
    cleanup = AsyncMock()
    monkeypatch.setattr(api, "_cleanup_session_artifacts", cleanup)
    with b.active():
        request = Request({"type": "http", "headers": []})
        tasks = BackgroundTasks()
        if batch:
            result = await api.batch_delete_sessions(
                request, api.BatchDeleteRequest(session_ids=["kept"]), tasks, token="admin")
            assert result["deleted_count"] == 0 and result["errors"]
        else:
            with pytest.raises(HTTPException) as caught:
                await api.delete_session(request, "kept", tasks, token="admin")
            assert caught.value.status_code == (503 if failure == "offline" else 409)
        assert database.get_session("kept")
        assert not tasks.tasks
    with a.active():
        assert database.get_session("kept")
    cleanup.assert_not_called()


async def test_peer_delete_refuses_foreign_session_and_is_idempotent(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Lifecycle")
    join(a, b)
    _new_session(a, "owned", "Owned")
    converge([a, b])
    cleanup = AsyncMock()
    monkeypatch.setattr(api, "_cleanup_session_artifacts", cleanup)
    with b.active():
        with pytest.raises(PermissionError, match="does not own"):
            await api.handle_delete({"session_id": "owned"}, a.device_id)
        assert database.get_session("owned")
    with a.active():
        response = await api.handle_delete({"session_id": "owned"}, b.device_id)
        assert response.status_code == 200
        await response.background()
        await api.handle_delete({"session_id": "owned"}, b.device_id)
        assert database.get_session("owned") is None
    cleanup.assert_awaited_once()
