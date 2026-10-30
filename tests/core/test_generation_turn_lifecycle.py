"""Generation context belongs to the active pooled turn, never an idle client."""

import asyncio
from collections import OrderedDict
from contextlib import ExitStack
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import generation_resolver as gr
from app.core import query_background as qb
from app.core import query_engine as qe
from app.core import session_pool as sp
from app.core.query_options import _generation_context
from tests.core.engine_harness import collect
from tests.core.query_engine_flow_harness import RecordingPool, engine_patches
from tests.core.test_query_background_recording import _bg_patches
from tests.core.test_query_engine_turn_lifecycle import _pooled_state, _result

SID = "generation-context-lifecycle"
TURN = gr.RunContext(session_id=SID, unattended=True, owner=True, turn_source="automation")


@pytest.fixture(autouse=True)
def clean_contexts(monkeypatch):
    monkeypatch.setattr(gr, "_TURN_CONTEXTS", OrderedDict())


@pytest.mark.parametrize("bad", [None, gr.RunContext(), replace(TURN, session_id="other-session")])
def test_an_invalid_binding_clears_only_its_own_session(bad):
    client = SimpleNamespace(generation_context=None)
    gr.bind_turn_context(SimpleNamespace(aih_generation_context=TURN), client=client)
    other = replace(TURN, session_id="other-session")
    gr.bind_turn_context(SimpleNamespace(aih_generation_context=other))
    gr.bind_turn_context(SimpleNamespace(aih_generation_context=bad, aih_session_id=SID), client=client)
    assert gr.turn_context(SID) is None
    assert gr.turn_context("other-session") is other


def test_an_old_clients_invalid_bind_cannot_clear_another_clients_turn():
    old_client = SimpleNamespace(generation_context=TURN)
    newer = replace(TURN, turn_source="wakeup")
    gr.bind_turn_context(SimpleNamespace(aih_generation_context=newer))
    gr.bind_turn_context(SimpleNamespace(aih_generation_context=None, aih_session_id=SID), client=old_client)
    assert gr.turn_context(SID) is newer


def test_cleanup_after_an_invalid_bind_cannot_clear_a_newer_turn():
    bound = gr.bind_turn_context(SimpleNamespace(aih_generation_context=None, aih_session_id=SID))
    assert bound is None
    newer = replace(TURN, turn_source="wakeup")
    gr.bind_turn_context(SimpleNamespace(aih_generation_context=newer))
    gr.clear_turn_context(SID, expected=bound)
    assert gr.turn_context(SID) is newer


@pytest.mark.parametrize("method", ["remove", "remove_dead", "remove_broken", "_do_evict", "evict_dead"])
async def test_removing_an_old_client_cannot_clear_another_clients_turn(monkeypatch, method):
    pool = sp.SessionPool()
    old_state = _pooled_state()
    old_state.generation_context = TURN
    pool._pool[SID] = old_state
    newer = replace(TURN, turn_source="wakeup")
    gr.bind_turn_context(SimpleNamespace(aih_generation_context=newer))
    monkeypatch.setattr(sp, "is_subprocess_alive", lambda state: False)
    monkeypatch.setattr(sp, "subprocess_pid", lambda state: None)
    monkeypatch.setattr(sp, "snapshot_descendants", lambda pid: [])
    monkeypatch.setattr(sp, "reap_closed_subprocess", AsyncMock())
    monkeypatch.setattr(sp, "record_terminated_subprocess", lambda pid: None)
    if method == "_do_evict":
        await pool._do_evict(SID, old_state)
    elif method == "evict_dead":
        await pool.evict_dead()
    else:
        await getattr(pool, method)(SID)
    if pool._background_teardowns:
        await asyncio.gather(*pool._background_teardowns)
    assert gr.turn_context(SID) is newer


@pytest.mark.parametrize("method", ["remove_dead", "remove_broken"])
async def test_old_turn_removal_does_not_remove_a_reused_client(method):
    pool = sp.SessionPool()
    state = _pooled_state()
    newer = replace(TURN, turn_source="wakeup")
    state.generation_context = newer
    pool._pool[SID] = state
    gr.bind_turn_context(SimpleNamespace(aih_generation_context=newer))
    assert await getattr(pool, method)(SID, expected_context=TURN) is None
    assert pool.peek(SID) is state
    assert gr.turn_context(SID) is newer


def test_late_cleanup_does_not_clear_a_newer_turn():
    gr.bind_turn_context(SimpleNamespace(aih_generation_context=TURN))
    newer = replace(TURN, turn_source="wakeup")
    gr.bind_turn_context(SimpleNamespace(aih_generation_context=newer))
    gr.clear_turn_context(SID, expected=TURN)
    assert gr.turn_context(SID) is newer
    gr.clear_turn_context(SID, expected=newer)
    assert gr.turn_context(SID) is None


@pytest.mark.parametrize("lane", ["websocket", "rest", "background"])
async def test_old_runner_error_cleanup_preserves_a_reused_clients_new_turn(tmp_path, lane):
    state = _pooled_state([_result()])
    pool = RecordingPool(state)
    options = MagicMock(cwd=str(tmp_path), aih_generation_context=TURN)
    newer = replace(TURN, turn_source="wakeup")

    async def send(*args, **kwargs):
        assert state.generation_context is TURN
        gr.bind_turn_context(SimpleNamespace(aih_generation_context=newer), client=state)
        raise RuntimeError("old turn's delayed provider error")

    state.adapter.send_query.side_effect = send
    with ExitStack() as stack:
        if lane == "background":
            _bg_patches(stack, pool)
            stack.enter_context(patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})))
            await qb._run_background_query_impl(SID, "hi", {"id": "claude-code"}, None, {}, None, None, None)
        else:
            bounds = engine_patches(stack, pool=pool, cwd=str(tmp_path), session_row={"id": SID})
            bounds.options.aih_generation_context = TURN
            if lane == "rest":
                stack.enter_context(patch("app.core.sdk_options.build_session_options", return_value=(bounds.options, {})))
                await collect(qe.stream_query("hi", "claude-code", session_id=SID))
            else:
                await collect(qe._stream_to_websocket_impl("hi", SID, "claude-code"))
    state.adapter.send_query.assert_awaited_once()
    assert gr.turn_context(SID) is newer
    assert pool.peek(SID) is state
    assert pool.removed == []


@pytest.mark.parametrize("lane", ["websocket", "rest", "background"])
@pytest.mark.parametrize("outcome", ["completed", "error", "cancelled", "setup_error"])
async def test_every_turn_exit_releases_the_context(tmp_path, lane, outcome):
    state = _pooled_state([_result()])
    pool = RecordingPool(state)
    options = MagicMock(cwd=str(tmp_path), aih_generation_context=TURN)

    async def send(*args, **kwargs):
        assert gr.turn_context(SID) is TURN
        assert state.generation_context is TURN  # the real bind records client ownership
        if outcome == "error":
            raise RuntimeError("fixture provider error")
        if outcome == "cancelled":
            raise asyncio.CancelledError()

    state.adapter.send_query.side_effect = send
    with ExitStack() as stack:
        if lane == "background":
            _bg_patches(stack, pool)
            stack.enter_context(patch("app.core.sdk_options.build_options_from_profile", return_value=(options, {})))
        else:
            bounds = engine_patches(stack, pool=pool, cwd=str(tmp_path), session_row={"id": SID})
            bounds.options.aih_generation_context = TURN
            if lane == "rest":
                stack.enter_context(patch("app.core.sdk_options.build_session_options", return_value=(bounds.options, {})))
        if outcome == "setup_error":
            module = qb if lane == "background" else qe
            stack.enter_context(patch.object(module, "capture_usage_identity", new=AsyncMock(
                side_effect=RuntimeError("fixture setup error"),
            )))

        async def run():
            if lane == "background":
                await qb._run_background_query_impl(SID, "hi", {"id": "claude-code"}, None, {}, None, None, None)
            elif lane == "rest":
                await collect(qe.stream_query("hi", "claude-code", session_id=SID))
            else:
                await collect(qe._stream_to_websocket_impl("hi", SID, "claude-code"))

        if outcome == "setup_error":
            with pytest.raises(RuntimeError, match="fixture setup error"):
                await run()
        else:
            await run()
            state.adapter.send_query.assert_awaited_once()
    assert gr.turn_context(SID) is None
    assert not state.is_streaming
    assert state.generation_context is TURN


@pytest.mark.parametrize("method", ["remove", "remove_dead", "remove_broken", "_do_evict", "evict_dead"])
async def test_every_pool_removal_releases_the_context(monkeypatch, method):
    pool = sp.SessionPool()
    state = _pooled_state()
    state.adapter.disconnect = AsyncMock()
    pool._pool[SID] = state
    gr.bind_turn_context(SimpleNamespace(aih_generation_context=TURN))
    state.generation_context = TURN
    monkeypatch.setattr(sp, "is_subprocess_alive", lambda state: False)
    monkeypatch.setattr(sp, "subprocess_pid", lambda state: None)
    monkeypatch.setattr(sp, "snapshot_descendants", lambda pid: [])
    async def reaped(*args):
        assert gr.turn_context(SID) is None

    monkeypatch.setattr(sp, "reap_closed_subprocess", AsyncMock(side_effect=reaped))
    monkeypatch.setattr(sp, "record_terminated_subprocess", lambda pid: None)
    if method == "_do_evict":
        await pool._do_evict(SID, state)
    elif method == "evict_dead":
        assert await pool.evict_dead() == [SID]
    else:
        await getattr(pool, method)(SID)
    if pool._background_teardowns:
        await asyncio.gather(*pool._background_teardowns)
    assert gr.turn_context(SID) is None
    assert SID not in pool._pool


@pytest.mark.parametrize("source", ["user", "automation"])
@pytest.mark.parametrize("surface", ["mcp", "native", "fal", "search"])
async def test_an_idle_tool_server_cannot_use_its_connect_context(monkeypatch, source, surface):
    # Construct the same context as production options, including the saved-
    # choice loader. Clearing the active registry must not re-read that loader
    # or fall back to an interactive/automation connect-time snapshot.
    from app.core import query_options
    from app.services import generation_sources
    from app.services.generation_sources import Sources as ProductionSources
    from app.mcp.ai_tools_sdk import _create_unified_tools
    from tests.core.test_generation_resolver import FakeBackend, candidate
    import json

    pick = candidate()
    loader = MagicMock(return_value={"remembered": {"image": pick.choice()}, "last": {}})
    monkeypatch.setattr(query_options, "_generation_choices", loader)
    context = _generation_context({}, {"id": SID}, None, session_id=SID,
                                  unattended=source != "user", turn_source=source)
    backend = FakeBackend([pick])
    backend.catalog = AsyncMock(side_effect=AssertionError("Idle generation must not read a catalog"))
    backend.fal_key = AsyncMock(side_effect=AssertionError("Idle generation must not read credentials"))
    if surface == "native":
        from fluid.tools.registry import ToolCtx
        from app.core.provider import native_generation_aliases as aliases

        # A lazy module import must not retain another surface's temporary
        # Sources fake after monkeypatch restores the original dependency.
        assert aliases.Sources is ProductionSources
        monkeypatch.setattr(aliases, "Sources", lambda *args, **kw: backend)
        _, handler = aliases.make_alias("image", context)
    else:
        monkeypatch.setattr(generation_sources, "Sources", lambda *args, **kw: backend)
        name = {"mcp": "generateImage", "fal": "runFalModel", "search": "searchModels"}[surface]
        tool = next(t for t in _create_unified_tools(None, context) if t.name == name)
    gr.bind_turn_context(SimpleNamespace(aih_generation_context=context))
    gr.clear_turn_context(SID, expected=context)
    loader.reset_mock()
    if surface == "native":
        output = json.loads((await handler({"prompt": "a tree"}, ToolCtx())).text)
    else:
        args = ({"endpoint_id": "fal-ai/test", "input": {"prompt": "a tree"}} if surface == "fal"
                else {} if surface == "search" else {"prompt": "a tree"})
        response = await tool.handler(args)
        output = json.loads(response["content"][0]["text"])
    assert output["status"] == "skipped"
    assert output["reason"] == "no_active_turn"
    assert "no active turn" in output["message"].lower()
    assert backend.calls == []
    backend.catalog.assert_not_awaited()
    backend.fal_key.assert_not_awaited()
    loader.assert_not_called()
