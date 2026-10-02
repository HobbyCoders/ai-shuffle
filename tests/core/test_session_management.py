"""Exercise session tools through their native and delegated producers."""
import json
from unittest.mock import AsyncMock, Mock

import pytest
from fluid.tools.registry import ToolCtx
from fluid.tools.builtins.session_orchestration import SPECS

from app.core import session_orchestration as orch
from app.db import database, sessions, turns
from app.db.base import get_db


@pytest.fixture
def chats(mock_db, monkeypatch):
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    for sid in ("caller", "target"):
        sessions.create_session(sid, "agent", title=sid, is_active=True)
    return ToolCtx(extra={"session_orchestration": orch.make_handler(lambda: "caller")})


async def invoke(ctx, name, **data):
    _, handler = next((spec, handler) for spec, handler in SPECS if spec.name == name)
    return await handler(data, ctx)


async def test_native_round_trip_keeps_work_and_transcript(chats, monkeypatch):
    from app.core import turn_scheduler
    from app.core.sync_engine import sync_engine
    kick = Mock()
    monkeypatch.setattr(turn_scheduler, "kick", kick)
    broadcast = AsyncMock()
    monkeypatch.setattr(sync_engine, "broadcast_user_session_updated", broadcast)
    turn = turns.enqueue_turn("target", "user", prompt="Keep this conversation", profile_id="agent")
    assert turns.start_turn(turn["id"])
    before = turns.get_turn(turn["id"])
    for name, active, archived in (("SettleSession", False, False), ("SettleSession", False, False),
                                    ("UnsettleSession", True, False), ("UnsettleSession", True, False),
                                    ("ArchiveSession", False, True), ("ArchiveSession", False, True)):
        outcome = await invoke(chats, name, session_id="target")
        assert not outcome.is_error, outcome.text
        receipt = json.loads(outcome.text)
        row = sessions.get_session("target")
        assert bool(row["is_active"]) is active
        assert (row["status"] == "archived") is archived
        assert receipt["is_active"] is active
        assert receipt["status"] == row["status"]
        assert turns.get_turn(turn["id"]) == before
        assert broadcast.await_args.kwargs["updates"] == {"is_active": active, "status": row["status"]}
    kick.assert_not_called()
    for name in ("SettleSession", "UnsettleSession"):
        outcome = await invoke(chats, name, session_id="target")
        assert outcome.is_error and "Restore" in outcome.text
        assert sessions.get_session("target")["status"] == "archived"


async def test_settle_self_then_unsettle_self(chats):
    for name in ("SettleSession", "UnsettleSession"):
        result = await invoke(chats, name, session_id="caller")
        assert not result.is_error, result.text
    assert sessions.get_session("caller")["is_active"]


async def test_list_producer_finds_settled_and_archived(chats):
    await invoke(chats, "SettleSession", session_id="target")
    assert orch.list_sessions("caller", {})["sessions"] == []
    listing = orch.list_sessions("caller", {"state": "settled"})
    assert [r["id"] for r in listing["sessions"]] == ["target"]
    assert not listing["sessions"][0]["is_active"]
    assert not (await invoke(chats, "UnsettleSession", session_id=listing["sessions"][0]["id"])).is_error
    assert [r["id"] for r in orch.list_sessions("caller", {})["sessions"]] == ["target"]
    await invoke(chats, "ArchiveSession", session_id="target")
    assert orch.list_sessions("caller", {"state": "settled"})["sessions"] == []
    for state in ("archived", "all"):
        assert [r["id"] for r in orch.list_sessions("caller", {"state": state})["sessions"]] == ["target"]
    assert (await invoke(chats, "ListSessions", state="invalid")).is_error


@pytest.mark.parametrize("field,value", [("hidden", True), ("is_worker", True),
    ("parent_session_id", "caller"), ("mode", "voice"), ("instance_id", "foreign")])
@pytest.mark.parametrize("name", ["SettleSession", "UnsettleSession", "ArchiveSession"])
async def test_ineligible_targets_never_mutate(chats, field, value, name):
    with get_db() as conn:
        conn.cursor().execute(f"UPDATE sessions SET {field}=%s WHERE id=%s", (value, "target"))
    result = await invoke(chats, name, session_id="target")
    assert result.is_error
    assert sessions.get_session("target")["is_active"]
    assert orch.list_sessions("caller", {"state": "all"})["sessions"] == []


@pytest.mark.parametrize("name", ["SettleSession", "UnsettleSession", "ArchiveSession"])
async def test_owner_and_revocation_checks(chats, monkeypatch, name):
    monkeypatch.setattr(orch, "_principal", lambda sid: {"owner": sid})
    monkeypatch.setattr("app.core.mobile_tool_identity.validate", lambda p: None)
    result = await invoke(chats, name, session_id="target")
    assert result.is_error and "accessible" in result.text
    assert orch.list_sessions("caller", {"state": "all"})["sessions"] == []
    monkeypatch.setattr(orch, "_principal", lambda sid: {"owner": "same", "sid": sid})
    def revoked(principal):
        if principal["sid"] == "target":
            raise PermissionError("revoked")
    monkeypatch.setattr("app.core.mobile_tool_identity.validate", revoked)
    result = await invoke(chats, name, session_id="target")
    assert result.is_error and "revoked" in result.text
    assert sessions.get_session("target")["is_active"]


async def test_api_user_scope(chats):
    database.create_api_user("other", "Other", "hash")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET api_user_id=%s WHERE id=%s", ("other", "target"))
    assert (await invoke(chats, "ArchiveSession", session_id="target")).is_error
    assert orch.list_sessions("caller", {"state": "all"})["sessions"] == []


@pytest.mark.parametrize("sid", [None, "", " ", "missing", 123, "x" * 201])
async def test_invalid_ids(chats, sid):
    assert (await invoke(chats, "SettleSession", session_id=sid)).is_error


async def test_stopped_target_organization_does_not_resume_it(chats, monkeypatch):
    from app.core.task_registry import task_registry
    monkeypatch.setattr(task_registry, "is_user_stopped", lambda sid: sid == "target")
    for name in ("SettleSession", "UnsettleSession", "ArchiveSession"):
        result = await invoke(chats, name, session_id="target")
        assert not result.is_error, result.text
        assert task_registry.is_user_stopped("target")
    monkeypatch.setattr(task_registry, "is_user_stopped", lambda sid: sid == "caller")
    assert (await invoke(chats, "ArchiveSession", session_id="target")).is_error


async def test_assistant_home_is_protected_by_mutation_guard(chats, monkeypatch):
    from app.core import assistant_service as service
    monkeypatch.setattr(service, "_guard_version", 0)
    service.configure({"agent_id": "agent", "enabled": True})
    home = service.open_home()
    ctx = ToolCtx(extra={"session_orchestration": orch.make_handler(lambda: home["id"])})
    # Drive the real home producer, then try to settle/archive that caller.
    for name in ("SettleSession", "ArchiveSession"):
        result = await invoke(ctx, name, session_id=home["id"])
        assert result.is_error and "persistent" in result.text
        assert sessions.get_session(home["id"])["is_active"]


async def test_mcp_registration_executes_same_tools(chats, monkeypatch):
    from app.mcp.builtin_tools import create_builtin_tools_server, get_builtin_tool_names
    from app.core.provider.fluid_tool_bridge import BUILTINS_RISK_MAP, NATIVE_SHADOWED_BUILTINS
    import app.mcp.inproc as inproc
    captured = {}
    real_create = inproc.create_tool_server
    def capture(**kwargs):
        captured.update({item.name: item for item in kwargs["tools"]})
        return real_create(**kwargs)
    monkeypatch.setattr(inproc, "create_tool_server", capture)
    create_builtin_tools_server(session_id="caller", profile_id="agent")
    for name in ("SettleSession", "UnsettleSession", "ArchiveSession"):
        assert BUILTINS_RISK_MAP[name] == "write"
        assert name in NATIVE_SHADOWED_BUILTINS
        assert f"mcp__builtins__{name}" in get_builtin_tool_names()
        result = await captured[name].handler({"session_id": "target"})
        assert not result.get("isError"), result
    assert sessions.get_session("target")["status"] == "archived"


async def test_host_capability_required():
    for name in ("SettleSession", "UnsettleSession", "ArchiveSession"):
        spec = next(spec for spec, _ in SPECS if spec.name == name)
        assert spec.risk == "write" and spec.requires_extra == ("session_orchestration",)
        assert (await invoke(ToolCtx(), name, session_id="target")).is_error


@pytest.mark.parametrize("name", ["SettleSession", "UnsettleSession", "ArchiveSession"])
async def test_code_mode_write_permission_gate(chats, name):
    from fluid.codemode.bridge import HostBridge
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.registry import ToolRegistry
    from fluid.tools.builtins import register_builtins
    registry = ToolRegistry()
    register_builtins(registry)
    bridge = HostBridge(registry, PermissionEngine("on_write"), ctx=chats)
    assert (await bridge.on_host_call(name, {"session_id": "target"})).denied
    assert sessions.get_session("target")["is_active"]
    assert sessions.get_session("target")["status"] != "archived"
    allowed = HostBridge(registry, PermissionEngine("never_ask"), ctx=chats)
    assert not (await allowed.on_host_call(name, {"session_id": "target"})).denied
    row = sessions.get_session("target")
    assert bool(row["is_active"]) is (name == "UnsettleSession")
    assert (row["status"] == "archived") is (name == "ArchiveSession")


@pytest.mark.parametrize("field,value", [("is_worker", True), ("hidden", True),
    ("parent_session_id", "target"), ("status", "archived")])
async def test_ineligible_callers_cannot_manage_chats(chats, field, value):
    with get_db() as conn:
        conn.cursor().execute(f"UPDATE sessions SET {field}=%s WHERE id=%s", (value, "caller"))
    result = await invoke(chats, "ArchiveSession", session_id="target")
    assert result.is_error
    assert sessions.get_session("target")["is_active"]


async def test_missing_writer_result_is_not_success(chats, monkeypatch):
    monkeypatch.setattr(database, "set_session_active", lambda *args: None)
    result = await invoke(chats, "SettleSession", session_id="target")
    assert result.is_error and "Failed to update" in result.text

