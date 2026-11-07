"""Runtime reader exits at a yielded frame still drain and save the real pump."""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

import app.core.query_engine as qe
from app.core import session_runtime
from app.core.provider import session_admission as admission
from app.core.provider.events import AssistantMessage, ResultMessage, TextBlock
from app.core.session_pool import SessionState
from app.db import turns
from tests.core.query_engine_flow_harness import RecordingPool, engine_patches, fresh_adapter, scripted_events
from tests.core.test_session_runtime import _runtime_env

SID = "runtime-early-reader-exit"


@pytest.mark.parametrize("exit_at_frame", ["cancelled", "recording_failed"])
async def test_yielded_pump_drains_engine_and_persistence_before_release(mock_db, tmp_path, exit_at_frame):
    yielded, engine_release = asyncio.Event(), asyncio.Event()
    engine_drained, saving, saved = asyncio.Event(), asyncio.Event(), asyncio.Event()
    save_release = asyncio.Event()

    async def finish_engine():
        await engine_release.wait()
        engine_drained.set()

    adapter = fresh_adapter()
    adapter.receive_events = scripted_events([
        AssistantMessage(content=[TextBlock(text="first frame")], model="m"),
        finish_engine,
        ResultMessage(subtype="success"),
    ])
    pool = RecordingPool(SessionState(adapter=adapter, is_connected=True))
    real_pump = qe._stream_to_websocket_impl
    turn = turns.enqueue_turn(SID, "user", prompt="hello")
    rt = session_runtime.SessionRuntime(turn)

    async def record(event):
        if event.get("type") != "chunk":
            return
        yielded.set()
        if exit_at_frame == "recording_failed":
            rt._on_recorder_recording_failed()
            return
        await asyncio.Event().wait()

    async def persist(*args, **kwargs):
        assert engine_drained.is_set()
        saving.set()
        await save_release.wait()
        saved.set()

    with ExitStack() as stack:
        env = stack.enter_context(_runtime_env(pump=real_pump))
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        b.persist.side_effect = persist
        env["recorder"].on_event.side_effect = record
        stack.enter_context(patch("app.core.query_session_lifecycle.interrupt_session", new=AsyncMock()))
        runtime = asyncio.create_task(rt.execute())
        try:
            await asyncio.wait_for(yielded.wait(), 5)
            if exit_at_frame == "cancelled":
                rt._pump_task.cancel()
            engine_release.set()
            await asyncio.wait_for(saving.wait(), 5)
            assert not runtime.done()
            assert not saved.is_set()
            with patch.object(admission, "_ENDING_WAIT_SECONDS", 0.01):
                with pytest.raises(admission.SessionBusyError):
                    await admission.acquire(SID)
            # A second cancellation while draining must not detach saving.
            rt._pump_task.cancel()
            await asyncio.sleep(0)
            assert not runtime.done()
            assert SID in admission._holders
        finally:
            engine_release.set()
            save_release.set()
            await asyncio.wait_for(runtime, 5)
        assert engine_drained.is_set()
        assert saved.is_set()
        b.persist.assert_awaited_once()
        assert SID not in admission._holders
