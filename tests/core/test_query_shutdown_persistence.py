"""A disconnect must not cancel a retired query between saving and its terminal."""
import asyncio
from contextlib import ExitStack, asynccontextmanager
import time
import sys
from unittest.mock import AsyncMock

import pytest

from app.core import credential_gate, query_engine as qe, query_shutdown, sdk_options
from app.core.provider.events import SystemMessage
from app.core.provider.session_admission import raw_engine_events
from app.db.async_database import run_sync
from tests.core.query_engine_flow_harness import engine_patches, fresh_adapter


@pytest.mark.asyncio
async def test_retired_query_delayed_persistence_survives_owner_disconnect(monkeypatch, tmp_path):
    monkeypatch.setattr(query_shutdown, "_closing_loop", None)
    started = asyncio.Event()
    saving = asyncio.Event()
    saved = asyncio.Event()
    terminal = asyncio.Event()
    published = asyncio.Event()
    records = []
    loop = asyncio.get_running_loop()

    def delayed_write():
        loop.call_soon_threadsafe(saving.set)
        time.sleep(2)  # Real executor work, longer than the default 1.5s reserve.
        (tmp_path / "persisted").write_text("SDK and usage saved")
        loop.call_soon_threadsafe(saved.set)

    def record(**kwargs):
        records.append(kwargs)
        loop.call_soon_threadsafe(terminal.set)

    async def raw_events():
        started.set()
        await asyncio.Event().wait()
        yield None

    async def messages():
        yield SystemMessage(subtype="init", data={"session_id": "sdk-delayed-save"})
        async for message in raw_engine_events(raw_events()):
            yield message

    @asynccontextmanager
    async def no_plan(*args, **kwargs):
        yield None

    # Undo fixture patches before the harness restores its own boundaries.
    # Reversing those owners leaks the harness's fake DB dispatcher to later
    # tests, so real API producers would appear to create non-existent chats.
    with ExitStack() as stack, monkeypatch.context() as patches:
        adapter = fresh_adapter()
        boundaries = engine_patches(stack, cwd=str(tmp_path), adapter=adapter)
        adapter.receive_events = messages
        patches.setattr(sdk_options, "build_session_options", lambda **kw: (boundaries.options, {}))
        patches.setattr(credential_gate, "refusal_for_options_async", AsyncMock(return_value=None))
        patches.setattr(qe, "recorded_turn", no_plan)
        patches.setattr(qe, "_record_query_message", AsyncMock())
        patched_sync = qe.run_sync

        async def dispatch(fn, *args, **kwargs):
            if fn is qe.turns_db.record_adhoc_turn:
                return await run_sync(record, **kwargs)
            return await patched_sync(fn, *args, **kwargs)

        async def persist(**kwargs):
            await run_sync(delayed_write)

        async def push(*args):
            published.set()

        patches.setattr(qe, "run_sync", dispatch)
        patches.setattr(qe, "_persist_turn_results", persist)
        patches.setattr(qe, "push_memory", push)
        owner = asyncio.create_task(qe.execute_query("save this work", "claude-code"))
        draining = None
        try:
            await asyncio.wait_for(started.wait(), 5)
            draining = asyncio.create_task(query_shutdown.settle(grace=0))
            await asyncio.wait_for(saving.wait(), 2)
            # Same cancellation the HTTP disconnect/Uvicorn request owner gets.
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
            await asyncio.wait_for(saved.wait(), 3)
            waiter = asyncio.create_task(terminal.wait())
            _, pending = await asyncio.wait({waiter}, timeout=1)
            for task in pending:
                task.cancel()
            assert len(records) == 1, "issued persistence completed, but terminal finalization was lost"
            assert records[0]["outcome"] == "aborted"
            assert records[0]["stop_reason"] == "server_shutdown"
            await asyncio.wait_for(published.wait(), 1)
        finally:
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
            if draining is not None:
                await draining


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX production stop_group")
def test_real_http_query_finishes_delayed_write_after_owner_disconnect(tmp_path):
    from tests.core.test_query_shutdown_budget import run_query_shutdown_probe

    run_query_shutdown_probe(tmp_path, "stalled", 10, "/api/v1/query", persist_delay=2)
