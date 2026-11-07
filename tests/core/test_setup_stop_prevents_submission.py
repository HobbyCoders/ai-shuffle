"""A Stop during host setup must stop model work, not just relabel its result."""

import asyncio
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.core.query_engine as qe
from app.core import query_session_lifecycle as lifecycle
from app.core.provider import session_admission
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions
from app.core.session_pool import SessionState
from fluid.engine.delegated import DelegatedLoop
from fluid.engine.native import NativeLoop
from fluid.providers.base import ProviderCaps, StreamChunk
from fluid.store.base import MemoryStore
from tests.core.engine_harness import collect
from tests.core.query_engine_flow_harness import RecordingPool, engine_patches
from tests.core.test_sse_interrupt_lifecycle import _background_patches, _background

SID = "sse-interrupt-lifecycle"
FAKE_CLI = Path(__file__).resolve().parents[2] / "fluid/tests/fixtures/fake_claude.py"


class RecordingProvider:
    name = "fake"
    caps = ProviderCaps()

    def __init__(self):
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        yield StreamChunk("message_start", {})
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": "Should not run after Stop"})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {"stop_reason": "end_turn"})


@pytest.mark.parametrize("leg", ["native", "delegated"])
@pytest.mark.parametrize("phase", ["options", "model", "adapter"])
@pytest.mark.parametrize("background", [False, True])
async def test_setup_stop_prevents_model_work_and_next_turn_recovers(tmp_path, monkeypatch, leg, phase, background):
    provider = RecordingProvider()
    if leg == "native":
        engine = NativeLoop(provider=provider, store=MemoryStore(), model="old")
    else:
        engine = DelegatedLoop(
            store=MemoryStore(), model="old", claude_cmd=[sys.executable, str(FAKE_CLI)],
            env={"FAKE_CLAUDE_SCENARIO": "f"},
        )
    adapter = HarnessAdapter()
    adapter._engine = engine
    adapter._connected = True
    adapter._is_delegated = leg == "delegated"
    adapter._options = SessionOptions(model="old", extra={"app_session_id": SID})
    monkeypatch.setattr(adapter, "_turn_context_note", lambda *args: None)
    monkeypatch.setattr(adapter, "_refresh_task_roster", AsyncMock())
    monkeypatch.setattr(adapter, "_memory_held", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_native_scratch_held", AsyncMock(return_value=None))
    # Observe the producing boundary, not just the host's interrupted label.
    frames = []
    if leg == "delegated":
        write = engine._write_frame

        async def record_frame(frame):
            frames.append(frame)
            await write(frame)
            if phase == "model" and frame.get("request", {}).get("subtype") == "set_model":
                # Stop an actual unanswered control switch, not a stub setter.
                await barrier()

        monkeypatch.setattr(engine, "_write_frame", record_frame)

    state = SessionState(adapter=adapter, is_connected=True)
    pool = RecordingPool(state)
    entered, release = asyncio.Event(), asyncio.Event()

    async def barrier(*args, **kwargs):
        entered.set()
        await release.wait()

    async def turn():
        if background:
            await _background()
            result = []
        else:
            result = await collect(qe.stream_query("hello", "claude-code", session_id=SID))
        if engine._turn_task is not None:
            await engine._turn_task
        return result

    with ExitStack() as stack:
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool, leg=leg)
        _background_patches(stack, b, pool)
        stack.enter_context(patch.object(lifecycle, "get_session_pool", return_value=pool))
        # Warm a connected pooled adapter first, as in the reported direct API flow.
        await turn()
        assert provider.requests if leg == "native" else any(f.get("type") == "user" for f in frames)
        provider.requests.clear()
        frames.clear()
        if phase == "options":
            blocker = stack.enter_context(patch("app.core.query_options.ensure_pending_checkout", side_effect=barrier))
        elif phase == "model":
            state.connect_options = MagicMock(model="old", permission_mode="default", aih_provider_id=None)
            b.options.model = "new"
            original = adapter.set_model

            async def switch(model):
                await barrier()
                await original(model)

            blocker = stack.enter_context(patch.object(
                adapter, "set_model", side_effect=switch if leg == "native" else original,
            ))
        else:
            blocker = stack.enter_context(patch.object(adapter, "_refresh_task_roster", side_effect=barrier))
        running = asyncio.create_task(turn())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            assert await lifecycle.interrupt_session(SID)
        finally:
            release.set()
        try:
            events = await asyncio.wait_for(running, 10)
            assert provider.requests == []
            assert not any(f.get("type") == "user" for f in frames)
            assert b.persist.await_args.kwargs["interrupted"] is True
            row = b.run_sync_named("record_adhoc_turn")[-1][2]
            assert (row["outcome"], row["stop_reason"]) == ("aborted", "interrupted")
            if not background:
                assert events[-1]["type"] == "interrupted"
            assert state.interrupt_requested is False
            assert state.is_streaming is False
            if leg == "delegated":
                assert engine._stop_carryover_until is None
            # Remove the one-time barrier, then immediately submit a clean turn.
            blocker.side_effect = None
            if phase == "model":
                blocker.side_effect = original
            await turn()
            assert b.persist.await_args.kwargs["interrupted"] is False
            assert provider.requests if leg == "native" else any(f.get("type") == "user" for f in frames)
        finally:
            await adapter.disconnect()


async def test_stop_before_pool_entry_is_owned_by_admission():
    pool = RecordingPool()
    with patch.object(session_admission, "get_session_pool", return_value=pool), patch.object(
        lifecycle, "get_session_pool", return_value=pool,
    ):
        lease = await session_admission.acquire(SID)
        try:
            assert await lifecycle.interrupt_session(SID)
            assert lease.stop_requested is True
            assert session_admission.stop_requested(SID)
        finally:
            session_admission.close(lease)
        clean = await session_admission.acquire(SID)
        try:
            assert not session_admission.stop_requested(SID)
        finally:
            session_admission.close(clean)
