"""Live setter and real permission-prompt producer regression coverage."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import sessions_permissions as api
from app.core.permission_handler import permission_handler
from app.core.session_pool import SessionState
from tests.core.test_query_engine_permission_flows import (
    SID, _callback, _ctx, _next_prompt, _allowed,
)


@pytest.fixture
def live(monkeypatch):
    state = SessionState(adapter=SimpleNamespace(set_permission_mode=AsyncMock(), _is_delegated=False),
                         is_connected=True, is_streaming=True,
                         connect_options=SimpleNamespace(permission_mode="default"))
    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: SimpleNamespace(peek=lambda sid: state))
    monkeypatch.setattr(api.control, "_require_owned", lambda sid: {"id": sid})
    monkeypatch.setattr("app.core.session_runtime.get_live_runtime", lambda sid: SimpleNamespace(turn={"overrides": {"permission_mode": "default"}}))
    save = MagicMock()
    monkeypatch.setattr(api.database, "set_session_run_binding", save)
    return state, save


async def test_live_ask_bypass_ask_without_interrupt_or_reconnect(live):
    state, save = live
    for mode in ("bypassPermissions", "default"):
        assert await api.apply_permission_mode(SID, mode) == {"permission_mode": mode}
        assert state.callback_refs["permission_mode"] == mode
        assert state.connect_options.permission_mode == mode
        assert state.is_streaming
        save.assert_called_with(SID, permission_mode=mode)
    assert state.adapter.set_permission_mode.await_count == 2


async def test_engine_refusal_keeps_old_mode(live):
    state, save = live
    state.adapter.set_permission_mode.side_effect = RuntimeError("refused")
    with pytest.raises(RuntimeError, match="refused"):
        await api.apply_permission_mode(SID, "bypassPermissions")
    assert state.callback_refs["permission_mode"] == "default"
    save.assert_not_called()


async def test_persistence_failure_restores_engine(live):
    state, save = live
    save.side_effect = RuntimeError("db offline")
    with pytest.raises(RuntimeError, match="db offline"):
        await api.apply_permission_mode(SID, "bypassPermissions")
    assert [c.args[0] for c in state.adapter.set_permission_mode.await_args_list] == ["bypassPermissions", "default"]
    assert state.callback_refs["permission_mode"] == "default"


async def test_connecting_session_refuses_false_success(live):
    state, save = live
    state.is_connected = False
    with pytest.raises(ValueError, match="still connecting"):
        await api.apply_permission_mode(SID, "bypassPermissions")
    save.assert_not_called()


async def test_idle_session_saves_without_spawning(live, monkeypatch):
    state, save = live
    state.is_connected = False
    monkeypatch.setattr("app.core.session_runtime.get_live_runtime", lambda sid: None)
    await api.apply_permission_mode(SID, "acceptEdits")
    save.assert_called_once_with(SID, permission_mode="acceptEdits")
    state.adapter.set_permission_mode.assert_not_awaited()


async def test_chat_ceiling_cannot_be_widened(live, monkeypatch):
    state, save = live
    monkeypatch.setattr(api.control, "_require_owned", lambda sid: {"id": sid, "mode": "chat"})
    assert await api.apply_permission_mode(SID, "bypassPermissions") == {"permission_mode": "default"}


@pytest.mark.parametrize("mode", ["plan", "bogus", "", None])
def test_invalid_modes_rejected(mode):
    with pytest.raises(ValidationError):
        api.PermissionModeUpdate(permission_mode=mode)


async def test_http_route_validates_and_applies_mode(live, monkeypatch):
    import httpx
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1/sessions")
    app.dependency_overrides[api.require_auth] = lambda: "owner"
    monkeypatch.setattr(api, "require_session", AsyncMock(return_value={"id": SID}))
    monkeypatch.setattr(api.control, "owner_of", lambda row: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f"/api/v1/sessions/{SID}/permission-mode"
        assert (await client.put(url, json={"permission_mode": "invalid"})).status_code == 422
        result = await client.put(url, json={"permission_mode": "bypassPermissions"})
        assert result.status_code == 200
        assert result.json() == {"permission_mode": "bypassPermissions"}
    assert live[0].is_streaming


async def test_remote_owner_receives_update_not_local_pool(monkeypatch):
    monkeypatch.setattr(api, "require_session", AsyncMock(return_value={"id": SID}))
    monkeypatch.setattr(api.control, "owner_of", lambda row: "remote")
    remote = AsyncMock(return_value={"permission_mode": "bypassPermissions"})
    local = AsyncMock()
    monkeypatch.setattr(api.control, "call", remote)
    monkeypatch.setattr(api, "apply_permission_mode", local)
    result = await api.update_permission_mode(None, SID, api.PermissionModeUpdate(permission_mode="bypassPermissions"))
    assert result["permission_mode"] == "bypassPermissions"
    remote.assert_awaited_once_with("remote", "permission-mode", {"session_id": SID, "permission_mode": "bypassPermissions"})
    local.assert_not_awaited()
    remote.side_effect = api.control.RunnerUnreachable("offline")
    with pytest.raises(HTTPException) as exc:
        await api.update_permission_mode(None, SID, api.PermissionModeUpdate(permission_mode="default"))
    assert exc.value.status_code == 503


async def test_access_checked_before_changing_permissions(monkeypatch):
    monkeypatch.setattr(api, "require_session", AsyncMock(side_effect=HTTPException(403)))
    apply = AsyncMock()
    monkeypatch.setattr(api, "apply_permission_mode", apply)
    with pytest.raises(HTTPException):
        await api.update_permission_mode(None, SID, api.PermissionModeUpdate(permission_mode="bypassPermissions"))
    apply.assert_not_awaited()


@pytest.mark.parametrize("tool,mode,releases", [
    ("Bash", "bypassPermissions", True),
    ("Write", "acceptEdits", True),
    ("Bash", "acceptEdits", False),
    ("mcp__computer-use__request_access", "bypassPermissions", False),
])
async def test_switch_releases_real_policy_prompts_only(tmp_path, live, tool, mode, releases):
    state, _ = live
    cb, broadcast, bounds = await _callback(tmp_path)
    # Use the refs captured by the REAL producer, not a seeded interaction.
    state.callback_refs = dict(zip(cb.__code__.co_freevars, [c.cell_contents for c in cb.__closure__]))["callback_refs"]
    with patch("app.core.query_engine.sync_engine", bounds.sync_engine), \
         patch("app.db.database.get_permission_rules", return_value=[]):
        task = asyncio.create_task(cb(tool, {"file_path": "/x"}, _ctx(delegated=False)))
        try:
            prompt = await _next_prompt(broadcast)
            await api.apply_permission_mode(SID, mode)
            if not releases:
                rows = permission_handler._v2_pending_permissions(SID)
                assert any(r["payload"]["request_id"] == prompt["request_id"] for r in rows)
                await permission_handler.respond(prompt["request_id"], SID, "deny")
            assert _allowed(await asyncio.wait_for(task, 5)) == releases
            # The original callback now reads the live mode for subsequent tools.
            if releases:
                assert _allowed(await cb("Write", {"file_path": "/y"}, _ctx(delegated=False)))
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)




async def test_mode_change_during_prompt_publication_is_rechecked(tmp_path, live):
    state, _ = live
    cb, broadcast, bounds = await _callback(tmp_path)
    state.callback_refs = dict(zip(cb.__code__.co_freevars, [c.cell_contents for c in cb.__closure__]))["callback_refs"]
    async def change_on_broadcast(message):
        if message.get("type") == "permission_request":
            # Simulate setter's sweep missing a prompt being published; the
            # post-insert recheck must still release it before waiting.
            state.callback_refs["permission_mode"] = "bypassPermissions"
    broadcast.side_effect = change_on_broadcast
    with patch("app.core.query_engine.sync_engine", bounds.sync_engine), \
         patch("app.db.database.get_permission_rules", return_value=[]):
        result = await asyncio.wait_for(cb("Bash", {"command": "echo ok"}, _ctx(delegated=False)), 5)
    assert _allowed(result)


async def test_non_policy_and_child_prompts_are_not_released(live):
    state, _ = live
    rows = [
        {"payload": {"request_id": "private", "tool_name": "ChatRunCommand"}},
        {"payload": {"request_id": "child", "tool_name": "Bash", "mode_managed": True, "provider_agent": {"worker_session_id": "child"}}},
    ]
    state.callback_refs["permission_mode"] = "bypassPermissions"
    with patch.object(permission_handler, "_v2_pending_permissions", return_value=rows), \
         patch.object(permission_handler, "respond", new_callable=AsyncMock) as respond:
        await permission_handler.resolve_mode_permissions(SID, state)
        respond.assert_not_awaited()


async def test_accept_edits_does_not_release_saved_deny(live):
    from app.core.permission_handler import PermissionDecision
    state, _ = live
    state.callback_refs["permission_mode"] = "acceptEdits"
    rows = [{"payload": {"request_id": "denied-edit", "tool_name": "Write", "mode_managed": True}}]
    with patch.object(permission_handler, "_v2_pending_permissions", return_value=rows), \
         patch.object(permission_handler, "saved_rule", return_value=SimpleNamespace(decision=PermissionDecision.DENY)), \
         patch.object(permission_handler, "respond", new_callable=AsyncMock) as respond:
        await permission_handler.resolve_mode_permissions(SID, state)
        respond.assert_not_awaited()


async def test_new_workers_inherit_changed_live_policy(live):
    from app.core.provider_agent import _caller_permission_mode
    state, _ = live
    await api.apply_permission_mode(SID, "bypassPermissions")
    with patch("app.db.turns.get_latest_turn", return_value={"overrides": {"permission_mode": "default"}}):
        assert _caller_permission_mode({"id": SID, "permission_mode": "default"}) == "bypassPermissions"


async def test_native_engine_policy_changes_in_place(live):
    from tests.core.test_permission_mode_reuse import _NativeEngine, _connected_adapter
    state, _ = live
    engine = _NativeEngine()
    state.adapter, state.connect_options = await _connected_adapter(engine, "default")
    await api.apply_permission_mode(SID, "bypassPermissions")
    assert engine.permissions.approval_policy == "never_ask"
    await api.apply_permission_mode(SID, "default")
    assert engine.permissions.approval_policy == "on_write"
