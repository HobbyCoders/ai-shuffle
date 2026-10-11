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

@pytest.mark.asyncio
async def test_context_pick_is_durable_without_a_message(mock_db, monkeypatch):
    from app.core import turn_manager
    from app.db import turns
    from app.core.query_options import build_options_from_profile
    from unittest.mock import patch

    # Create through the existing real session/options producer, then save the
    # picker change independently of a turn or browser-local preference.
    db.create_profile("role", "Role", None, {"model": "fable"})
    db.create_session(session_id="window-child", profile_id="role", model="fable", context_window=1_000_000)
    monkeypatch.setattr(api.control, "_require_owned", db.get_session)
    monkeypatch.setattr("app.core.session_pool.get_session_pool",
                        lambda: SimpleNamespace(peek=lambda _: None))
    monkeypatch.setattr("app.core.session_runtime.get_live_runtime", lambda _: None)
    result = await api.apply_run_settings("window-child", api.RunSettingsUpdate(context_window=200_000))
    assert result["context_window"] == 200_000
    assert db.get_session("window-child")["context_window"] == 200_000
    # Server-originated turns can inherit stale snapshots, including the old
    # 1M override. The acknowledged row pin must not be widened by them.
    for source in ("unpark", "agent_message", "resume", "compact_continue", "failover", "wakeup"):
        turn_id = turn_manager._submit_blocking("window-child", source,
            prompt="continue", profile_id="role",
            overrides={"model": "fable", "context_window": 1_000_000})
        saved = turns.get_turn(turn_id)["overrides"]
        assert saved["context_window"] == 200_000, source
        with patch("app.core.query_options.get_security_instructions", return_value=""):
            options, _ = build_options_from_profile(db.get_profile("role"),
                session_id="window-child", overrides=saved)
        assert options.aih_context_window == 200_000, source
        assert options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "200000"


@pytest.mark.asyncio
async def test_context_pick_does_not_hot_swap_or_interrupt_the_live_leg(live, monkeypatch):
    state, runtime, save = live
    state.adapter._options.model = "fable"
    monkeypatch.setattr(api.control, "_require_owned", lambda sid: {"id": sid, "model": "fable"})
    old_turn = dict(runtime.turn["overrides"])
    result = await api.apply_run_settings("s", api.RunSettingsUpdate(context_window=200_000))
    assert result == {"context_window": 200_000}
    save.assert_called_once_with("s", context_window=200_000)
    assert runtime.turn["overrides"] == old_turn
    assert state.is_streaming
    assert state.connect_options.effort == "high"

@pytest.mark.asyncio
async def test_raised_standard_cap_is_saved_and_reaches_compaction(mock_db, monkeypatch):
    from app.core.query_options import build_options_from_profile
    from unittest.mock import patch

    # Standard is a cap the user can raise: any value from the model's default
    # standard window up to its full window is saved and pins compaction.
    db.create_profile("cap-role", "Cap", None, {"model": "fable"})
    db.create_session(session_id="cap", profile_id="cap-role", model="fable")
    monkeypatch.setattr(api.control, "_require_owned", db.get_session)
    result = await api.apply_run_settings("cap", api.RunSettingsUpdate(context_window=450_000))
    assert result == {"context_window": 450_000}
    assert db.get_session("cap")["context_window"] == 450_000
    with patch("app.core.query_options.get_security_instructions", return_value=""):
        options, _ = build_options_from_profile(db.get_profile("cap-role"), session_id="cap", overrides={})
    assert options.aih_context_window == 450_000
    assert options.env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] == "450000"
    # Above 200K the delegated CLI needs its long-context window enabled.
    assert options.env["CLAUDE_CODE_DISABLE_1M_CONTEXT"] == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize("window", [150_000, 1_000_001, 2_000_000])
async def test_cap_outside_the_models_range_is_refused(mock_db, monkeypatch, window):
    db.create_profile("cap-role", "Cap", None, {"model": "fable"})
    db.create_session(session_id="cap-bad", profile_id="cap-role", model="fable", context_window=1_000_000)
    monkeypatch.setattr(api.control, "_require_owned", db.get_session)
    with pytest.raises(ValueError, match="does not support the selected context window"):
        await api.apply_run_settings("cap-bad", api.RunSettingsUpdate(context_window=window))
    assert db.get_session("cap-bad")["context_window"] == 1_000_000


@pytest.mark.asyncio
async def test_context_pick_on_legacy_session_uses_its_launched_model(mock_db, monkeypatch):
    db.create_profile("legacy-role", "Legacy", None, {"model": "haiku"})
    db.create_session("legacy-window", "legacy-role")
    db.update_session("legacy-window", launched_model="fable")
    monkeypatch.setattr(api.control, "_require_owned", db.get_session)
    await api.apply_run_settings("legacy-window", api.RunSettingsUpdate(context_window=200_000))
    assert db.get_session("legacy-window")["context_window"] == 200_000


@pytest.mark.asyncio
async def test_context_pick_is_announced_to_open_clients(mock_db, monkeypatch):
    """#1895: another tab, the phone or the other worker's browser learns a
    window pick without a reload, on the worker that took the request and on
    the owner that saved it. Effort/Fast changes announce nothing new."""
    from app.core.sync_engine import sync_engine

    db.create_profile("role", "Role", None, {"model": "fable"})
    db.create_session(session_id="announce", profile_id="role", model="fable", context_window=1_000_000)
    monkeypatch.setattr(api.control, "_require_owned", db.get_session)
    monkeypatch.setattr("app.core.session_pool.get_session_pool",
                        lambda: SimpleNamespace(peek=lambda _: None))
    monkeypatch.setattr("app.core.session_runtime.get_live_runtime", lambda _: None)
    monkeypatch.setattr(api, "require_session", AsyncMock(return_value={"id": "announce"}))
    monkeypatch.setattr(api.control, "owner_of", lambda row: None)
    broadcast = AsyncMock()
    monkeypatch.setattr(sync_engine, "broadcast_session_updated", broadcast)

    await api.update_run_settings(None, "announce", api.RunSettingsUpdate(context_window=200_000))
    broadcast.assert_awaited_once_with(
        session_id="announce", updates={"context_window": 200_000}, source_device_id=None)

    broadcast.reset_mock()
    await api.handle_run_settings({"session_id": "announce", "context_window": 1_000_000}, "peer")
    broadcast.assert_awaited_once_with(
        session_id="announce", updates={"context_window": 1_000_000}, source_device_id=None)

    broadcast.reset_mock()
    monkeypatch.setattr(api.control, "owner_of", lambda row: "remote")
    monkeypatch.setattr(api.control, "call", AsyncMock(return_value={"context_window": 200_000}))
    await api.update_run_settings(None, "announce", api.RunSettingsUpdate(context_window=200_000))
    broadcast.assert_awaited_once_with(
        session_id="announce", updates={"context_window": 200_000}, source_device_id=None)

    broadcast.reset_mock()
    monkeypatch.setattr(api.control, "call", AsyncMock(return_value={"effort": "low", "fast": False}))
    await api.update_run_settings(None, "announce", api.RunSettingsUpdate(effort="low"))
    broadcast.assert_not_awaited()


# --- fast mode handed down the delegation tree --------------------------------

@pytest.fixture
def tree(mock_db, monkeypatch):
    """A parent with subagents, all idle on this worker.

    ``w1``/``w2`` are Opus workers (``w2`` under ``w1``), ``w-sonnet`` runs a
    model without fast mode, ``w-pinned`` was set by the owner in its own chat
    and ``fork`` is the owner's fork of the parent, not a subagent.
    """
    make_profile("agent", {})
    db.create_session("p", profile_id="agent", model="opus")
    worker = dict(profile_id="agent", hidden=True, is_worker=True)
    db.create_session("w1", parent_session_id="p", model="opus", **worker)
    db.create_session("w2", parent_session_id="w1", model="opus", **worker)
    db.create_session("w-sonnet", parent_session_id="p", model="sonnet", **worker)
    db.create_session("w-pinned", parent_session_id="p", model="opus", fast=False, **worker)
    db.set_session_run_binding("w-pinned", fast_pinned=True)
    db.create_session("fork", profile_id="agent", parent_session_id="p", model="opus")
    monkeypatch.setattr(api.control, "_require_owned", db.get_session)
    monkeypatch.setattr(api.control, "owner_of", lambda row: None)
    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: SimpleNamespace(peek=lambda sid: None))
    monkeypatch.setattr("app.core.session_runtime.get_live_runtime", lambda sid: None)


async def _toggle(session_id, fast):
    result = await api.apply_run_settings(session_id, api.RunSettingsUpdate(fast=fast))
    await asyncio.gather(*list(api._fast_propagations))
    return result


def _fast(session_id):
    value = db.get_session(session_id)["fast"]
    return None if value is None else bool(value)


@pytest.mark.asyncio
async def test_parent_fast_toggle_reaches_its_subagents_and_reverses(tree):
    assert (await _toggle("p", True))["fast"] is True
    assert [_fast(s) for s in ("p", "w1", "w2")] == [True, True, True]
    # A model without fast mode, the owner's pin and a fork are left alone.
    assert _fast("w-sonnet") is None
    assert _fast("w-pinned") is False
    assert _fast("fork") is None
    await _toggle("p", False)
    assert [_fast(s) for s in ("p", "w1", "w2")] == [False, False, False]
    assert _fast("w-sonnet") is None and _fast("fork") is None
    # Handing it down never pins: the subagents keep following the parent.
    assert not db.get_session("w1")["fast_pinned"]


@pytest.mark.asyncio
async def test_owner_pick_in_a_subagents_chat_pins_it(tree):
    await _toggle("w1", False)
    assert db.get_session("w1")["fast_pinned"]
    await _toggle("p", True)
    assert _fast("p") is True
    assert _fast("w1") is False
    # Its own pick still reaches its subagents: w1's toggle handed False to w2,
    # and the root's later change reaches w2 only through w1, which is pinned.
    assert _fast("w2") is False
    # A non-worker chat is never pinned.
    assert not db.get_session("p")["fast_pinned"]


@pytest.mark.asyncio
async def test_running_subagent_takes_the_change_on_its_next_request(tree, monkeypatch):
    adapter = HarnessAdapter()
    adapter._is_delegated = True
    adapter._options = SessionOptions(model="opus", effort="high", fast=False)
    adapter._engine = SimpleNamespace(set_run_settings=AsyncMock())
    adapter._sdk_options = SimpleNamespace(effort="high", aih_fast=False)
    state = SessionState(adapter=adapter, is_connected=True, is_streaming=True,
                         connect_options=adapter._sdk_options)
    runtime = SimpleNamespace(turn={"overrides": {"effort": "high", "fast": False}})
    monkeypatch.setattr("app.core.session_pool.get_session_pool",
                        lambda: SimpleNamespace(peek=lambda sid: state if sid == "w1" else None))
    monkeypatch.setattr("app.core.session_runtime.get_live_runtime",
                        lambda sid: runtime if sid == "w1" else None)
    await _toggle("p", True)
    adapter._engine.set_run_settings.assert_awaited_once_with(effort="high", fast=True)
    assert adapter._options.fast is True
    assert state.connect_options.aih_fast is True
    assert runtime.turn["overrides"]["fast"] is True
    assert state.is_streaming
    assert _fast("w1") is True and _fast("w2") is True


@pytest.mark.asyncio
async def test_subagent_on_another_worker_gets_the_change_through_its_owner(tree, monkeypatch):
    monkeypatch.setattr(api.control, "owner_of", lambda row: "gaming-pc" if row.get("id") == "w1" else None)
    remote = AsyncMock(return_value={"effort": None, "fast": True})
    monkeypatch.setattr(api.control, "call", remote)
    await _toggle("p", True)
    remote.assert_awaited_once_with("gaming-pc", "run-settings",
                                    {"session_id": "w1", "fast": True, "inherited": True})


@pytest.mark.asyncio
async def test_handed_down_change_skips_a_pinned_or_unsupported_subagent(tree):
    assert await api.handle_run_settings(
        {"session_id": "w-pinned", "fast": True, "inherited": True}, "parent-worker") == {"skipped": "pinned"}
    assert await api.handle_run_settings(
        {"session_id": "w-sonnet", "fast": True, "inherited": True}, "parent-worker") == {"skipped": "unsupported"}
    assert _fast("w-pinned") is False and _fast("w-sonnet") is None
    with pytest.raises(ValueError, match="only fast mode"):
        await api.apply_run_settings("w1", api.RunSettingsUpdate(effort="low"), inherited=True)
    result = await api.handle_run_settings({"session_id": "w1", "fast": True, "inherited": True}, "parent-worker")
    await asyncio.gather(*list(api._fast_propagations))
    assert result["fast"] is True and _fast("w2") is True
    assert not db.get_session("w1")["fast_pinned"]


@pytest.mark.asyncio
async def test_a_failing_subagent_does_not_stop_the_others(tree, monkeypatch):
    real = api._apply_run_settings

    async def flaky(session_id, body, *, inherited):
        if session_id == "w1":
            raise ValueError("The session is still connecting. Try again in a moment.")
        return await real(session_id, body, inherited=inherited)

    monkeypatch.setattr(api, "_apply_run_settings", flaky)
    db.create_session("w3", parent_session_id="p", model="opus", profile_id="agent", hidden=True, is_worker=True)
    assert (await _toggle("p", True))["fast"] is True
    assert _fast("w1") is None
    assert _fast("w3") is True
