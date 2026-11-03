"""A Stop belongs to one SSE turn, not every turn on its pooled adapter."""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.query_engine as qe
from app.core import query_session_lifecycle as lifecycle
from app.core.provider.events import AssistantMessage, ResultMessage, TextBlock
from tests.core.engine_harness import collect, make_state, stream_response
from tests.core.query_engine_flow_harness import RecordingPool, engine_patches, scripted_events

SID = "sse-interrupt-lifecycle"


async def _turn():
    return await collect(qe.stream_query("hello", "claude-code", session_id=SID))


@pytest.mark.parametrize("hard_cancel", [False, True])
async def test_stop_then_completed_followup_then_stop_again(tmp_path, hard_cancel):
    state = make_state(interrupt=AsyncMock())
    pool = RecordingPool(state)

    async def stop():
        assert await lifecycle.interrupt_session(SID)
        if hard_cancel:
            raise asyncio.CancelledError()

    answer = AssistantMessage(content=[TextBlock(text="answer")], model="m")
    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        stack.enter_context(patch.object(lifecycle, "get_session_pool", return_value=pool))
        for stopping in (True, False, True):
            state.adapter.receive_events = (
                scripted_events([answer, stop]) if stopping
                else stream_response([answer, ResultMessage(subtype="success")])
            )
            events = await _turn()
            assert events[-1]["type"] == ("interrupted" if stopping else "done")
            assert b.persist.await_args.kwargs["interrupted"] is stopping
            adhoc = b.run_sync_named("record_adhoc_turn")[-1][2]
            assert adhoc["outcome"] == ("aborted" if stopping else "completed")
            assert adhoc["stop_reason"] == ("interrupted" if stopping else None)
            assert pool.state is state
        assert state.adapter.interrupt.await_count == 2
        b.make_adapter.assert_not_called()


@pytest.mark.parametrize("phase", ["options", "model", "send"])
async def test_stop_while_pooled_turn_is_starting_is_not_cleared(tmp_path, phase):
    """Do not reset at the later streaming marker: setup has already awaited."""
    state = make_state(interrupt=AsyncMock())
    state.adapter.receive_events = stream_response([ResultMessage(subtype="success")])
    pool = RecordingPool(state)

    async def stop(*args, **kwargs):
        assert await lifecycle.interrupt_session(SID)

    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        stack.enter_context(patch.object(lifecycle, "get_session_pool", return_value=pool))
        if phase == "options":
            stack.enter_context(patch("app.core.query_options.ensure_pending_checkout", side_effect=stop))
        elif phase == "model":
            state.connect_options = MagicMock(model="old", permission_mode="default", aih_provider_id=None)
            b.options.model = "new"
            state.adapter.set_model = AsyncMock(side_effect=stop)
        else:
            state.adapter.send_query = AsyncMock(side_effect=stop)
        events = await _turn()
    assert events[-1]["type"] == "interrupted"
    assert b.persist.await_args.kwargs["interrupted"] is True
