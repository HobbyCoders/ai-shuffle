"""Tool producer -> shared membership / signed owner control -> remote DB."""
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
