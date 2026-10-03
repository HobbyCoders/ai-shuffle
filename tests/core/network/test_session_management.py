"""Tool producer -> shared membership / signed owner control -> remote DB."""
import json
from unittest.mock import AsyncMock

import pytest
from fluid.tools.registry import ToolCtx
from fluid.tools.builtins.session_orchestration import SPECS

from app.core import session_orchestration as orch
from app.db import database, network as store
from app.network import control, engine
from tests.core.network.harness import converge, join
from tests.core.network.test_control import _new_session
from tests.core.network.test_session_active_control import connect_owner


@pytest.fixture(autouse=True)
def install_local_principal(monkeypatch):
    # The network harness has no mobile identity tables; these are admin chats.
    monkeypatch.setattr(orch, "_principal", lambda sid: None)


async def invoke(name):
    _, handler = next((s, h) for s, h in SPECS if s.name == name)
    return await handler({"session_id": "target"}, ToolCtx(extra={
        "session_orchestration": orch.make_handler(lambda: "caller")}))


async def test_tools_settle_remote_offline_and_reconcile(workers, monkeypatch):
    a, b = workers("owner"), workers("viewer")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _new_session(a, "target", "Remote chat")
    _new_session(b, "caller", "Caller")
    converge([a, b])
    call = AsyncMock(side_effect=AssertionError("No execution-owner call for membership"))
    monkeypatch.setattr(control, "call", call)
    for name, active in (("SettleSession", False), ("UnsettleSession", True)):
        with b.active():
            store.touch_member(a.device_id, endpoints=[])
            assert [r["id"] for r in orch.list_sessions("caller", {"state": "all"})["sessions"]] == ["target"]
            before = database.get_session("target")
            result = await invoke(name)
            assert not result.is_error, result.text
            row = database.get_session("target")
            assert bool(row["is_active"]) is active
            for field in ("status", "owner_device_id", "instance_id", "placement", "updated_at"):
                assert row[field] == before[field]
            if not active:
                assert [r["id"] for r in orch.list_sessions("caller", {"state": "settled"})["sessions"]] == ["target"]
        converge([a, b])
        with a.active():
            assert bool(database.get_session("target")["is_active"]) is active
    call.assert_not_called()


async def test_tool_archives_on_remote_owner(workers, monkeypatch):
    a, b = workers("owner"), workers("viewer")
    served = connect_owner(monkeypatch, a)
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _new_session(a, "target", "Remote chat")
    _new_session(b, "caller", "Caller")
    converge([a, b])
    with b.active():
        store.touch_member(a.device_id, endpoints=[served.base_url])
        for _ in range(2):
            result = await invoke("ArchiveSession")
            assert not result.is_error, result.text
        assert database.get_session("target")["status"] == "archived"
        assert not database.get_session("target")["is_active"]
    with a.active():
        assert database.get_session("target")["status"] == "archived"
        assert not database.get_session("target")["is_active"]


@pytest.mark.parametrize("error", [control.RunnerUnreachable("Owner offline"), control.ControlRefused("Owner refused")])
async def test_tool_remote_archive_failures_are_errors_not_success(workers, monkeypatch, error):
    a, b = workers("owner"), workers("viewer")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _new_session(a, "target", "Remote chat")
    _new_session(b, "caller", "Caller")
    converge([a, b])
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=error))
    with b.active():
        result = await invoke("ArchiveSession")
        assert result.is_error, result.text
        assert database.get_session("target")["status"] != "archived"
        assert database.get_session("target")["is_active"]


async def invoke_with(name, **data):
    _, handler = next((s, h) for s, h in SPECS if s.name == name)
    return await handler(data, ToolCtx(extra={
        "session_orchestration": orch.make_handler(lambda: "caller")}))


async def test_bulk_settle_offline_owner_in_one_shared_transaction(workers, monkeypatch):
    from app.network import session_active
    a, b = workers("owner"), workers("viewer")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    for sid in ("target", "second"):
        _new_session(a, sid, sid)
    _new_session(b, "caller", "Caller")
    converge([a, b])
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=AssertionError("No owner call for membership")))
    batches = []
    real = session_active.write_shared_many
    monkeypatch.setattr(session_active, "write_shared_many", lambda ids, active: batches.append(ids) or real(ids, active))
    for name, active in (("SettleSession", False), ("UnsettleSession", True)):
        with b.active():
            store.touch_member(a.device_id, endpoints=[])
            result = await invoke_with(name, session_ids=["target", "second", "missing"])
            assert not result.is_error, result.text
            summary = json.loads(result.text)
            assert (summary["refused"], summary["failed"]) == (1, 0)
            assert summary["settled" if not active else "unsettled"] == 2
            for sid in ("target", "second"):
                assert bool(database.get_session(sid)["is_active"]) is active
        converge([a, b])
        with a.active():
            for sid in ("target", "second"):
                assert bool(database.get_session(sid)["is_active"]) is active
    assert batches == [["target", "second"], ["target", "second"]]


async def test_bulk_archive_offline_owner_fails_per_item(workers, monkeypatch):
    a, b = workers("owner"), workers("viewer")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _new_session(a, "target", "Remote chat")
    _new_session(b, "caller", "Caller")
    _new_session(b, "local", "Local chat")
    converge([a, b])
    monkeypatch.setattr(control, "call", AsyncMock(side_effect=control.RunnerUnreachable("Owner offline")))
    with b.active():
        result = await invoke_with("ArchiveSession", session_ids=["target", "local"])
        assert not result.is_error, result.text
        summary = json.loads(result.text)
        assert (summary["archived"], summary["failed"]) == (1, 1)
        [issue] = summary["issues"]
        assert (issue["session_id"], issue["outcome"]) == ("target", "failed")
        assert "can't be reached" in issue["reason"]
        assert database.get_session("local")["status"] == "archived"
        assert database.get_session("target")["status"] != "archived"
        assert database.get_session("target")["is_active"]
