"""Live run settings through owner routing, adapter and native request producer."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from fluid.engine.native import NativeLoop
from fluid.events.ops import UserTurn
from fluid.providers.base import ProviderCaps, StreamChunk
from fluid.providers.openai_responses.serialize import build_request_body
from fluid.store.base import MemoryStore
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolRegistry

from app.api import sessions_permissions as api
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions
from app.core.session_pool import SessionState
from app.db import database as db
from tests.core.helpers import make_profile


@pytest.fixture
def live(monkeypatch):
    adapter = HarnessAdapter()
    adapter._provider_kind = "openai_responses"
    adapter._options = SessionOptions(model="gpt-5.4", effort="high", fast=False)
    adapter._engine = SimpleNamespace(effort="high", request_extra={"keep": 1}, ctx=SimpleNamespace(extra={}))
    adapter._sdk_options = SimpleNamespace(effort="high", aih_fast=False)
    state = SessionState(adapter=adapter, is_connected=True, is_streaming=True,
                         connect_options=adapter._sdk_options)
    runtime = SimpleNamespace(turn={"overrides": {"effort": "high", "fast": False}})
    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: SimpleNamespace(peek=lambda sid: state))
    monkeypatch.setattr("app.core.session_runtime.get_live_runtime", lambda sid: runtime)
    monkeypatch.setattr(api.control, "_require_owned", lambda sid: {"id": sid, "model": "gpt-5.4", "effort": "high"})
    save = MagicMock()
    monkeypatch.setattr(db, "set_session_run_binding", save)
    return state, runtime, save


@pytest.mark.asyncio
async def test_live_sparse_picks_update_engine_binding_and_active_turn(live):
    state, runtime, save = live
    for changes, expected in [({"effort": "low"}, {"effort": "low", "fast": False}),
                              ({"fast": True}, {"effort": "low", "fast": True}),
                              ({"fast": False}, {"effort": "low", "fast": False}),
                              ({"effort": None}, {"effort": None, "fast": False})]:
        assert await api.apply_run_settings("s", api.RunSettingsUpdate(**changes)) == expected
        assert state.adapter._engine.effort == expected["effort"]
        assert state.connect_options.effort == expected["effort"]
        assert state.connect_options.aih_fast == expected["fast"]
        assert runtime.turn["overrides"] == expected
        assert state.is_streaming
    assert state.adapter._engine.request_extra == {"keep": 1}
    save.assert_called_with("s", effort=None, fast=False, clear_effort=True)


@pytest.mark.asyncio
async def test_failed_save_rolls_back_applied_engine_settings(live):
    state, runtime, save = live
    save.side_effect = RuntimeError("db offline")
    with pytest.raises(RuntimeError, match="db offline"):
        await api.apply_run_settings("s", api.RunSettingsUpdate(effort="low", fast=True))
    assert state.adapter._options.effort == "high"
    assert state.adapter._engine.request_extra == {"keep": 1}
    assert runtime.turn["overrides"]["effort"] == "high"


@pytest.mark.asyncio
async def test_connecting_refuses_false_acknowledgement(live):
    state, _, save = live
    state.is_connected = False
    with pytest.raises(ValueError, match="still connecting"):
        await api.apply_run_settings("s", api.RunSettingsUpdate(fast=True))
    save.assert_not_called()


@pytest.mark.asyncio
async def test_delegated_refusal_and_unsupported_provider_retain_options(live):
    state, _, save = live
    adapter = state.adapter
    adapter._is_delegated = True
    adapter._engine.set_run_settings = AsyncMock(side_effect=RuntimeError("CLI refused"))
    with pytest.raises(RuntimeError, match="CLI refused"):
        await api.apply_run_settings("s", api.RunSettingsUpdate(effort="low"))
    assert adapter._options.effort == "high"
    save.assert_not_called()
    adapter._is_delegated = False
    adapter._provider_kind = "openrouter"
    with pytest.raises(ValueError, match="not supported"):
        await api.apply_run_settings("s", api.RunSettingsUpdate(fast=True))
    save.assert_not_called()


@pytest.mark.asyncio
async def test_connection_replacement_does_not_mark_new_engine_as_applied(live):
    state, _, save = live
    adapter = state.adapter
    old_engine = adapter._engine
    adapter._is_delegated = True
    async def replace(**kwargs):
        adapter._engine = SimpleNamespace(effort="high", request_extra={})
    old_engine.set_run_settings = replace
    with pytest.raises(RuntimeError, match="connection changed"):
        await api.apply_run_settings("s", api.RunSettingsUpdate(effort="low"))
    assert adapter._engine.effort == "high"
    assert adapter._options.effort == "high"
    save.assert_not_called()


@pytest.mark.parametrize("body", [{"effort": "bogus"}, {"fast": "true"}, {"model": "different"}])
def test_invalid_settings_rejected(body):
    with pytest.raises(ValidationError):
        api.RunSettingsUpdate(**body)


@pytest.mark.asyncio
async def test_http_validation_and_remote_owner_routing(live, monkeypatch):
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1/sessions")
    app.dependency_overrides[api.require_auth] = lambda: "owner"
    monkeypatch.setattr(api, "require_session", AsyncMock(return_value={"id": "s"}))
    monkeypatch.setattr(api.control, "owner_of", lambda row: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = "/api/v1/sessions/s/run-settings"
        assert (await client.put(url, json={"effort": "bogus"})).status_code == 422
        assert (await client.put(url, json={})).status_code == 409
        assert (await client.put(url, json={"fast": None})).status_code == 409
        assert (await client.put(url, json={"fast": True})).json() == {"effort": "high", "fast": True}
    monkeypatch.setattr(api.control, "owner_of", lambda row: "remote")
    remote = AsyncMock(return_value={"effort": "low", "fast": False})
    monkeypatch.setattr(api.control, "call", remote)
    await api.update_run_settings(None, "s", api.RunSettingsUpdate(effort="low"))
    remote.assert_awaited_once_with("remote", "run-settings", {"session_id": "s", "effort": "low"})
    remote.side_effect = api.control.RunnerUnreachable("offline")
    with pytest.raises(HTTPException) as exc:
        await api.update_run_settings(None, "s", api.RunSettingsUpdate(fast=True))
    assert exc.value.status_code == 503
    assert api.control.known_action("run-settings")


@pytest.mark.asyncio
async def test_idle_session_and_explicit_default_persist_without_spawning(mock_db, monkeypatch):
    make_profile("agent", {"effort": "max"})
    db.create_session("idle-knobs", profile_id="agent", model="opus", effort="high", fast=True)
    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: SimpleNamespace(peek=lambda sid: None))
    monkeypatch.setattr("app.core.session_runtime.get_live_runtime", lambda sid: None)
    await api.apply_run_settings("idle-knobs", api.RunSettingsUpdate(effort=None, fast=False))
    row = db.get_session("idle-knobs")
    assert row["effort"] is None
    assert not row["fast"]
    from app.core.query_options import build_options_from_profile
    next_options, _ = build_options_from_profile(profile={"id": "agent", "config": {"effort": "max"}}, session_id="idle-knobs")
    assert next_options.effort is None
    # The default turn-persistence contract still does not erase an effort.
    db.set_session_run_binding("idle-knobs", effort="high")
    db.set_session_run_binding("idle-knobs", fast=True)
    assert db.get_session("idle-knobs")["effort"] == "high"
    assert await api.apply_run_settings("idle-knobs", api.RunSettingsUpdate(fast=True)) == {"effort": "high", "fast": True}


@pytest.mark.asyncio
async def test_real_native_turn_uses_changed_parameters_on_next_call():
    entered, proceed = asyncio.Event(), asyncio.Event()
    class Provider:
        name = "openai_responses"
        caps = ProviderCaps()
        def __init__(self): self.requests = []
        async def stream(self, request):
            self.requests.append(request)
            first = len(self.requests) == 1
            if first:
                entered.set()
                await proceed.wait()
            yield StreamChunk("message_start", {"usage": {"input_tokens": 10}})
            if first:
                yield StreamChunk("block_start", {"index": 0, "block_type": "tool_use", "tool_name": "mcp__test__continue", "tool_use_id": "t1"})
                yield StreamChunk("tool_args_delta", {"index": 0, "partial_json": "{}"})
            else:
                yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
                yield StreamChunk("text_delta", {"index": 0, "text": "done"})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"usage": {"output_tokens": 1}, "stop_reason": "tool_use" if first else "end_turn"})
            yield StreamChunk("message_stop", {})
    @tool("continue", "Continue work", {"type": "object", "properties": {}}, risk="safe")
    async def continue_work(args):
        return {"content": [{"type": "text", "text": "ok"}]}
    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("test", [continue_work]))
    provider = Provider()
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="gpt-5.4", effort="high", registry=registry)
    adapter = HarnessAdapter()
    adapter._engine = loop
    adapter._options = SessionOptions(model="gpt-5.4", effort="high", fast=False)
    adapter._provider_kind = "openai_responses"
    try:
        await loop.submit(UserTurn(content="work"))
        await asyncio.wait_for(entered.wait(), 5)
        await adapter.set_run_settings(effort="low", fast=True)
        proceed.set()
        await asyncio.wait_for(loop.wait_turn(), 10)
        assert len(provider.requests) == 2
        before, after = [build_request_body(r) for r in provider.requests]
        assert before["reasoning"]["effort"] == "high"
        assert "service_tier" not in before
        assert after["reasoning"]["effort"] == "low"
        assert after["service_tier"] == "priority"
        await adapter.set_run_settings(effort=None, fast=False)
        current = build_request_body(loop._build_request([]))
        assert "service_tier" not in current
        assert current.get("reasoning", {}).get("effort") is None
    finally:
        proceed.set()
        await loop.aclose()
