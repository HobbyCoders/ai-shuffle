"""Runtime reader exits at a yielded frame still drain and save the real pump."""

import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

import app.core.query_engine as qe
from app.core import session_runtime
from app.core.provider import session_admission as admission
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions
from fluid.engine.native import NativeLoop
from fluid.providers.base import ProviderCaps, StreamChunk
from fluid.store.base import MemoryStore
from app.core.session_pool import SessionState
from app.db import turns
from tests.core.query_engine_flow_harness import RecordingPool, engine_patches
from tests.core.test_session_runtime import _runtime_env

SID = "runtime-early-reader-exit"


@pytest.mark.parametrize("exit_at_frame", ["cancelled", "recording_failed"])
async def test_yielded_pump_drains_engine_and_persistence_before_release(mock_db, tmp_path, monkeypatch, exit_at_frame):
    yielded, engine_release = asyncio.Event(), asyncio.Event()
    engine_drained, saving, saved = asyncio.Event(), asyncio.Event(), asyncio.Event()
    save_release = asyncio.Event()

    class HeldProvider:
        name = "fake"
        caps = ProviderCaps()

        async def stream(self, request):
            yield StreamChunk("message_start", {})
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            yield StreamChunk("text_delta", {"index": 0, "text": "first frame"})
            await engine_release.wait()
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"stop_reason": "end_turn"})
            engine_drained.set()

    engine = NativeLoop(provider=HeldProvider(), store=MemoryStore(), model="m")
    adapter = HarnessAdapter()
    adapter._engine = engine
    adapter._connected = True
    adapter._can_use_tool = AsyncMock()
    adapter._options = SessionOptions(
        model="m", include_partial_messages=True, extra={"app_session_id": SID},
    )
    monkeypatch.setattr(adapter, "_turn_context_note", lambda *args: None)
    for name in ("_refresh_task_roster", "_memory_held", "_native_scratch_held"):
        monkeypatch.setattr(adapter, name, AsyncMock(return_value=None))
    pool = RecordingPool(SessionState(adapter=adapter, is_connected=True))
    real_pump = qe._stream_to_websocket_impl
    turn = turns.enqueue_turn(SID, "user", prompt="hello")
    rt = session_runtime.SessionRuntime(turn)

    seen = []

    async def record(event):
        seen.append(event)
        if event.get("type") != "stream_delta":
            return
        yielded.set()
        if exit_at_frame == "recording_failed":
            rt._on_recorder_recording_failed()
            return
        await asyncio.Event().wait()

    async def persist(*args, **kwargs):
        assert engine_drained.is_set(), seen
        saving.set()
        await save_release.wait()
        saved.set()

    with ExitStack() as stack:
        env = stack.enter_context(_runtime_env(pump=real_pump))
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        b.options.include_partial_messages = True
        b.persist.side_effect = persist
        env["recorder"].on_event.side_effect = record
        stack.enter_context(patch("app.core.query_session_lifecycle.interrupt_session", new=AsyncMock()))
        runtime = asyncio.create_task(rt.execute())
        try:
            await asyncio.wait_for(yielded.wait(), 5)
            assert engine._turn_task is not None and not engine._turn_task.done()
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
            try:
                await asyncio.wait_for(runtime, 5)
            finally:
                await adapter.disconnect()
            assert yielded.is_set(), seen
        assert engine_drained.is_set()
        assert saved.is_set()
        b.persist.assert_awaited_once()
        assert SID not in admission._holders


@pytest.mark.parametrize("reader", ["runtime", "direct"])
async def test_abandoned_never_releasing_engine_is_fenced_and_evicted(mock_db, tmp_path, monkeypatch, reader):
    from app.core.provider.fluid_pg_store import PgFluidStore
    from app.core.session_pool import SessionPool
    from fluid.messages.content import Message, Text
    from fluid.store.base import SessionNode

    yielded, provider_cancelled = asyncio.Event(), asyncio.Event()

    class WedgedProvider:
        name = "fake"
        caps = ProviderCaps()

        async def stream(self, request):
            try:
                yield StreamChunk("message_start", {})
                yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
                yield StreamChunk("text_delta", {"index": 0, "text": "first frame"})
                # There is deliberately NO release handle. Only forced cancellation
                # can stop this producing path; it never finishes voluntarily.
                await asyncio.Event().wait()
            finally:
                provider_cancelled.set()

    store = PgFluidStore()
    engine = NativeLoop(provider=WedgedProvider(), store=store, model="m")
    adapter = HarnessAdapter()
    adapter._engine, adapter._store = engine, store
    adapter._connected = True
    adapter._can_use_tool = AsyncMock()
    adapter._options = SessionOptions(model="m", include_partial_messages=True, extra={"app_session_id": SID})
    monkeypatch.setattr(adapter, "_turn_context_note", lambda *args: None)
    for name in ("_refresh_task_roster", "_memory_held", "_native_scratch_held"):
        monkeypatch.setattr(adapter, name, AsyncMock(return_value=None))
    monkeypatch.setattr(admission, "_ABANDONED_DRAIN_SECONDS", 0.02, raising=False)
    pool = SessionPool()
    state = SessionState(adapter=adapter, is_connected=True)
    pool.put(SID, state)
    real_pump = qe._stream_to_websocket_impl
    turn = turns.enqueue_turn(SID, "user", prompt="hello")
    rt = session_runtime.SessionRuntime(turn)

    async def record(event):
        if event.get("type") == "stream_delta":
            yielded.set()
            await asyncio.Event().wait()

    with ExitStack() as stack:
        env = stack.enter_context(_runtime_env(pump=real_pump))
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        b.options.include_partial_messages = True
        env["recorder"].on_event.side_effect = record
        stack.enter_context(patch("app.core.query_session_lifecycle.interrupt_session", new=AsyncMock()))
        if reader == "runtime":
            closing = asyncio.create_task(rt.execute())
            await asyncio.wait_for(yielded.wait(), 5)
            rt._pump_task.cancel()
            await asyncio.sleep(0)
            # A watchdog cancels this same pump again, not the drain's engine.
            rt._pump_task.cancel()
        else:
            events = real_pump(prompt="hello", session_id=SID, profile_id="claude-code")
            while (await asyncio.wait_for(anext(events), 5)).get("type") != "stream_delta":
                pass
            origin = admission._holders[SID]
            closing = asyncio.create_task(events.aclose())
        try:
            assert engine._turn_task is not None and not engine._turn_task.done()
            finished, _ = await asyncio.wait({closing}, timeout=0.5)
            assert closing in finished, "an abandoned engine pinned admission after the drain deadline"
            await closing
            if reader == "direct":
                await asyncio.wait_for(origin.exited.wait(), 0.5)
            assert SID not in admission._holders
            assert pool.peek(SID) is None, "the next request could reuse the wedged engine"
            await asyncio.wait_for(provider_cancelled.wait(), 5)
            # The real PG store's conditional writes reject an old engine even
            # if a stubborn producer later attempts one more append.
            fluid_id = engine._session.session_id
            assert store.lost_ownership()
            await store.append(fluid_id, SessionNode(
                node_id="late-old-write", parent_id=None,
                message=Message(role="assistant", content=[Text(text="must not survive")]),
            ))
            assert all(node.node_id != "late-old-write" for node in await PgFluidStore().load_path(fluid_id))
            b.persist.assert_not_awaited()
            if reader == "runtime":
                assert turns.get_turn(turn["id"])["state"] == "aborted"
            successor = await admission.acquire(SID)
            admission.close(successor)
        finally:
            # Cleanup force-cancels, never voluntarily releases, the provider.
            await adapter.disconnect_transport()
            await asyncio.wait_for(closing, 5)
            if pool._background_teardowns:
                await asyncio.wait_for(asyncio.gather(*pool._background_teardowns), 5)


@pytest.mark.parametrize("reader", ["runtime", "direct", "cancelled_finisher"])
async def test_engine_deadline_does_not_release_issued_threaded_session_update(mock_db, tmp_path, monkeypatch, reader):
    import threading

    from app.core.query_persistence import _persist_turn_results
    from app.core.session_pool import SessionPool
    from app.db import database

    yielded, engine_release = asyncio.Event(), asyncio.Event()
    saving, save_release, saved = threading.Event(), threading.Event(), threading.Event()

    class CompletingProvider:
        name = "fake"
        caps = ProviderCaps()

        async def stream(self, request):
            yield StreamChunk("message_start", {})
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            yield StreamChunk("text_delta", {"index": 0, "text": "first frame"})
            await engine_release.wait()
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"input_tokens": 1, "stop_reason": "end_turn"})

    database.create_session(SID, "default")
    update_session = database.update_session

    def delayed_update(*args, **kwargs):
        if kwargs.get("last_input_tokens") == 1:
            saving.set()
            assert save_release.wait(5), "test did not release the threaded update"
            try:
                return update_session(*args, **kwargs)
            finally:
                saved.set()
        return update_session(*args, **kwargs)

    monkeypatch.setattr(database, "update_session", delayed_update)
    monkeypatch.setattr(admission, "_ABANDONED_DRAIN_SECONDS", 0.02)
    engine = NativeLoop(provider=CompletingProvider(), store=MemoryStore(), model="m")
    adapter = HarnessAdapter()
    adapter._engine, adapter._connected = engine, True
    adapter._can_use_tool = AsyncMock()
    adapter._options = SessionOptions(model="m", include_partial_messages=True, extra={"app_session_id": SID})
    monkeypatch.setattr(adapter, "_turn_context_note", lambda *args: None)
    for name in ("_refresh_task_roster", "_memory_held", "_native_scratch_held"):
        monkeypatch.setattr(adapter, name, AsyncMock(return_value=None))
    pool = SessionPool()
    pool.put(SID, SessionState(adapter=adapter, is_connected=True))
    real_pump = qe._stream_to_websocket_impl
    turn = turns.enqueue_turn(SID, "user", prompt="hello")
    rt = session_runtime.SessionRuntime(turn)

    async def record(event):
        if event.get("type") == "stream_delta":
            yielded.set()
            await asyncio.Event().wait()

    with ExitStack() as stack:
        env = stack.enter_context(_runtime_env(pump=real_pump))
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        b.options.include_partial_messages = True
        # Restore the REAL persistence helper and its REAL run_sync/to_thread
        # dispatch. Only the sync DB boundary has a controlled scheduling gate.
        stack.enter_context(patch.object(qe, "_persist_turn_results", new=_persist_turn_results))
        env["recorder"].on_event.side_effect = record
        stack.enter_context(patch("app.core.query_session_lifecycle.interrupt_session", new=AsyncMock()))
        direct = None
        if reader == "runtime":
            closing = asyncio.create_task(rt.execute())
            await asyncio.wait_for(yielded.wait(), 5)
            rt._pump_task.cancel()
        else:
            events = real_pump(prompt="hello", session_id=SID, profile_id="claude-code")
            while (await asyncio.wait_for(anext(events), 5)).get("type") != "stream_delta":
                pass
            closing = asyncio.create_task(events.aclose())
        origin = admission._holders[SID]
        engine_release.set()
        try:
            assert await asyncio.to_thread(saving.wait, 5)
            await asyncio.wait_for(engine._turn_task, 5)
            assert not engine.turn_running, "this is slow saving, not a wedged engine"
            if reader == "runtime" and rt._pump_task is not None:
                rt._pump_task.cancel()  # watchdog cancellation cannot detach the write
            elif reader == "cancelled_finisher":
                finishing = [
                    task for task in admission._unattended
                    if task.get_coro().__name__ == "_finish_turn"
                ]
                assert len(finishing) == 1
                finishing[0].cancel()  # shutdown cannot detach the write either
                await asyncio.sleep(0)
                finishing[0].cancel()
            # The real DB update lasts much longer than the engine-drain budget.
            try:
                await asyncio.wait_for(origin.exited.wait(), 0.15)
            except asyncio.TimeoutError:
                pass
            try:
                direct = await admission.acquire(SID)
            except admission.SessionBusyError:
                admitted_while_saving = False
            else:
                admitted_while_saving = True
                update_session(SID, last_input_tokens=999)
            assert not saved.is_set()
            save_release.set()
            await asyncio.wait_for(closing, 5)
            await asyncio.wait_for(origin.exited.wait(), 5)
            assert await asyncio.to_thread(saved.wait, 5)
            if direct is None:
                direct = await admission.acquire(SID)
                update_session(SID, last_input_tokens=999)
            current = database.get_session(SID)["last_input_tokens"]
            assert current == 999, "A's uncancellable thread overwrote B's 999 with " + str(current)
            assert not admitted_while_saving, "B started while A still owned an issued DB mutation"
            assert not origin.abandoned, "a healthy finished engine was retired for slow saving"
        finally:
            save_release.set()
            await asyncio.wait_for(closing, 5)
            if direct is not None:
                admission.close(direct)
            await adapter.disconnect_transport()


@pytest.mark.parametrize("reader", ["runtime", "direct"])
async def test_slow_real_away_receipt_settles_before_successor(mock_db, tmp_path, monkeypatch, reader):
    import threading

    from app.core import away_mode
    from app.core.session_pool import SessionPool
    from app.db import database

    receipt_started, release_receipt, receipt_done = threading.Event(), threading.Event(), threading.Event()
    yielded = asyncio.Event()
    writes = []

    class CompletingProvider:
        name = "fake"
        caps = ProviderCaps()

        async def stream(self, request):
            yield StreamChunk("message_start", {})
            yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
            yield StreamChunk("text_delta", {"index": 0, "text": "finished normally"})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"stop_reason": "end_turn"})

    database.create_session(SID, "default")
    real_receipt = away_mode.record_context_delivery

    def delayed_receipt(*args):
        receipt_started.set()
        assert release_receipt.wait(5), "test did not release the Away receipt thread"
        try:
            real_receipt(*args)
            writes.append("A receipt")
        finally:
            receipt_done.set()

    monkeypatch.setattr(away_mode, "record_context_delivery", delayed_receipt)
    monkeypatch.setattr(admission, "_ABANDONED_DRAIN_SECONDS", 0.02)
    engine = NativeLoop(provider=CompletingProvider(), store=MemoryStore(), model="m")
    adapter = HarnessAdapter()
    adapter._engine, adapter._connected = engine, True
    adapter._can_use_tool = AsyncMock()
    adapter._options = SessionOptions(model="m", include_partial_messages=True, extra={"app_session_id": SID})
    # Drive send_query's real pending-note/TurnStarted producer, not a seeded
    # receive_events field. Model-info is yielded before the first Away receipt.
    monkeypatch.setattr(adapter, "_turn_context_note", lambda *args: away_mode.CONTEXT_NOTE)
    for name in ("_refresh_task_roster", "_memory_held", "_native_scratch_held"):
        monkeypatch.setattr(adapter, name, AsyncMock(return_value=None))
    pool = SessionPool()
    pool.put(SID, SessionState(adapter=adapter, is_connected=True))
    real_pump = qe._stream_to_websocket_impl
    turn = turns.enqueue_turn(SID, "user", prompt="hello")
    rt = session_runtime.SessionRuntime(turn)

    async def record(event):
        if event.get("subtype") == "model_info":
            yielded.set()
            await asyncio.Event().wait()

    direct = None
    with ExitStack() as stack:
        env = stack.enter_context(_runtime_env(pump=real_pump))
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        b.options.include_partial_messages = True
        env["recorder"].on_event.side_effect = record
        stack.enter_context(patch("app.core.query_session_lifecycle.interrupt_session", new=AsyncMock()))
        if reader == "runtime":
            closing = asyncio.create_task(rt.execute())
            await asyncio.wait_for(yielded.wait(), 5)
            rt._pump_task.cancel()
        else:
            events = real_pump(prompt="hello", session_id=SID, profile_id="claude-code")
            while (await asyncio.wait_for(anext(events), 5)).get("subtype") != "model_info":
                pass
            closing = asyncio.create_task(events.aclose())
        origin = admission._holders[SID]
        try:
            assert await asyncio.to_thread(receipt_started.wait, 5)
            await asyncio.wait_for(engine._turn_task, 5)
            assert not engine.turn_running
            try:
                await asyncio.wait_for(origin.exited.wait(), 0.15)
            except asyncio.TimeoutError:
                pass
            try:
                direct = await admission.acquire(SID)
            except admission.SessionBusyError:
                pass
            else:
                database.update_session(SID, last_input_tokens=999)
                writes.append("B successor mutation")
            release_receipt.set()
            assert await asyncio.to_thread(receipt_done.wait, 5)
            await asyncio.wait_for(closing, 5)
            await asyncio.wait_for(origin.exited.wait(), 5)
            if direct is None:
                direct = await admission.acquire(SID)
                database.update_session(SID, last_input_tokens=999)
                writes.append("B successor mutation")
            assert writes == ["A receipt", "B successor mutation"]
            assert not origin.abandoned, "a slow adapter app write retired a healthy engine"
            b.persist.assert_awaited_once()
        finally:
            release_receipt.set()
            await asyncio.wait_for(closing, 5)
            if direct is not None:
                admission.close(direct)
            await adapter.disconnect_transport()


@pytest.mark.parametrize("reader", ["runtime", "direct"])
async def test_permission_insert_during_engine_retirement_settles_before_successor(
    mock_db, tmp_path, monkeypatch, reader,
):
    import threading

    from app.core.permission_handler import PermissionHandler
    from app.core.session_pool import SessionPool
    from app.db import database, interactions
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.builtins import bash
    from fluid.tools.registry import ToolRegistry

    insert_started, release_insert, insert_done = threading.Event(), threading.Event(), threading.Event()
    yielded, retiring = asyncio.Event(), asyncio.Event()
    writes = []

    class AskingProvider:
        name = "fake"
        caps = ProviderCaps()

        async def stream(self, request):
            yield StreamChunk("message_start", {})
            yield StreamChunk("block_start", {
                "index": 0, "block_type": "tool_use", "tool_name": "Bash", "tool_use_id": "slow-permission",
            })
            yield StreamChunk("tool_args_delta", {"index": 0, "partial_json": '{"command":"not executed"}'})
            yield StreamChunk("block_stop", {"index": 0})
            yield StreamChunk("usage", {"stop_reason": "tool_use"})

    async def must_not_execute(*args):
        raise AssertionError("the permission must never be granted")

    registry = ToolRegistry()
    registry.register_builtin(bash.SPEC, must_not_execute)
    engine = NativeLoop(
        provider=AskingProvider(), store=MemoryStore(), model="m", registry=registry,
        permissions=PermissionEngine("always_ask"),
    )
    database.create_session(SID, "default")
    real_insert = interactions.create_interaction

    def delayed_insert(*args, **kwargs):
        insert_started.set()
        assert release_insert.wait(5), "test did not release the permission insert thread"
        try:
            row = real_insert(*args, **kwargs)
            writes.append("A permission insert")
            return row
        finally:
            insert_done.set()

    monkeypatch.setattr(interactions, "create_interaction", delayed_insert)
    monkeypatch.setattr(admission, "_ABANDONED_DRAIN_SECONDS", 0.1)
    handler = PermissionHandler()

    async def can_use_tool(name, tool_input, context):
        return await handler.request_permission(
            request_id="slow-permission", session_id=SID, profile_id="default",
            tool_name=name, tool_input=tool_input, tool_use_id=context.tool_use_id,
            broadcast_func=AsyncMock(),
        )

    adapter = HarnessAdapter()
    adapter._engine, adapter._connected = engine, True
    adapter._can_use_tool = can_use_tool
    adapter._options = SessionOptions(model="m", include_partial_messages=True, extra={"app_session_id": SID})
    monkeypatch.setattr(adapter, "_turn_context_note", lambda *args: None)
    for name in ("_refresh_task_roster", "_memory_held", "_native_scratch_held"):
        monkeypatch.setattr(adapter, name, AsyncMock(return_value=None))
    pool = SessionPool()
    pool.put(SID, SessionState(adapter=adapter, is_connected=True))
    real_pump = qe._stream_to_websocket_impl
    turn = turns.enqueue_turn(SID, "user", prompt="hello")
    rt = session_runtime.SessionRuntime(turn)

    async def record(event):
        if event.get("subtype") == "model_info":
            yielded.set()
            await asyncio.Event().wait()

    direct = None
    with ExitStack() as stack:
        env = stack.enter_context(_runtime_env(pump=real_pump))
        b = engine_patches(stack, cwd=str(tmp_path), session_row={"id": SID}, pool=pool)
        b.options.include_partial_messages = True
        b.options.permission_mode = "default"
        env["recorder"].on_event.side_effect = record
        stack.enter_context(patch("app.core.query_session_lifecycle.interrupt_session", new=AsyncMock()))
        if reader == "runtime":
            closing = asyncio.create_task(rt.execute())
            await asyncio.wait_for(yielded.wait(), 5)
        else:
            events = real_pump(prompt="hello", session_id=SID, profile_id="claude-code")
            while (await asyncio.wait_for(anext(events), 5)).get("subtype") != "model_info":
                pass
        origin = admission._holders[SID]
        real_retire = origin.abandon

        async def retire():
            assert insert_started.is_set(), "the real permission bridge must have issued its insert"
            await real_retire()
            retiring.set()

        origin.abandon = retire
        if reader == "runtime":
            rt._pump_task.cancel()
        else:
            closing = asyncio.create_task(events.aclose())
        try:
            assert await asyncio.to_thread(insert_started.wait, 5)
            await asyncio.wait_for(retiring.wait(), 5)
            # Let the release owner run; the write remains held by the barrier.
            try:
                await asyncio.wait_for(origin.exited.wait(), 0.05)
            except asyncio.TimeoutError:
                pass
            try:
                direct = await admission.acquire(SID)
            except admission.SessionBusyError:
                pass
            else:
                database.update_session(SID, last_input_tokens=999)
                writes.append("B successor mutation")
            release_insert.set()
            assert await asyncio.to_thread(insert_done.wait, 5)
            await asyncio.wait_for(closing, 5)
            await asyncio.wait_for(origin.exited.wait(), 5)
            if direct is None:
                direct = await admission.acquire(SID)
                database.update_session(SID, last_input_tokens=999)
                writes.append("B successor mutation")
            assert writes == ["A permission insert", "B successor mutation"]
            assert origin.abandoned
            b.persist.assert_not_awaited()
        finally:
            release_insert.set()
            await asyncio.wait_for(closing, 5)
            if direct is not None:
                admission.close(direct)
            await adapter.disconnect_transport()
