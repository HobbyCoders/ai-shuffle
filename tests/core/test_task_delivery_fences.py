"""Delivery fences must survive awaits, not just the initial wake snapshot."""
from types import SimpleNamespace
from typing import Any, Iterator
from unittest.mock import AsyncMock, patch

import pytest

from app.core import session_runtime as sr, turn_scheduler as ts
from app.core.task_registry import TaskRegistry
from app.db import turns
from tests.core.test_session_runtime import _runtime_env, _clean_module_state  # noqa: F401


@pytest.mark.parametrize("restart_at_start,legacy,held", [
    (True, False, False), (True, True, False), (False, True, False),
    (False, False, False), (False, False, True),
])
async def test_queued_delivery_rechecks_frames_and_actual_prompt(
    mock_db, monkeypatch, restart_at_start, legacy, held,
):
    sid = "delivery-fence"
    reg = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", reg)
    reg.start_task(sid, "A", "shell", "first")
    reg.mark_monitor_shell_task(sid, "A")
    reg.complete_task(sid, "A", "completed")
    frames = [{"task_id": "A", "status": "completed", "summary": "STALE_A_FINISHED",
               "task_incarnation": reg.task_incarnation(sid, "A")}]
    if legacy:
        reg.start_task(sid, "B", "shell", "legacy")
        reg.mark_monitor_shell_task(sid, "B")
        reg.complete_task(sid, "B", "completed")
        frames.append({"task_id": "B", "status": "completed", "summary": "CURRENT_B_FINISHED"})
    payload = {"pending_frames": frames, "task_ids": [f["task_id"] for f in frames]}
    if held:
        payload["held_peer_messages"] = "UNRELATED_HELD_MESSAGE"
    turn = turns.enqueue_turn(sid, "unpark", prompt=ts._build_task_notification_prompt(frames), payload=payload)
    calls = []

    def restart():
        reg.start_task(sid, "A", "shell", "second")

    async def pump(**kwargs):
        calls.append(kwargs)
        yield {"type": "done"}

    with _runtime_env(pump=pump) as env:
        with patch("app.core.task_registry.task_registry", reg):
            if restart_at_start:
                env["recorder"].start.side_effect = lambda *a, **kw: restart()
            else:
                restart()
            await sr.SessionRuntime(turn).execute()
    assert not reg.get_task(sid, "A").get("notification_delivered")
    assert "A" not in reg._terminal_notified.get(sid, set())
    recorded = [frame for call in env["recorder"].record_task_terminal.await_args_list
                for frame in call.args[0]]
    assert all(f["task_id"] != "A" for f in recorded)
    if legacy or held:
        assert len(calls) == 1
        prompt = calls[0]["prompt"]
        assert "STALE_A_FINISHED" not in prompt
        assert ("CURRENT_B_FINISHED" in prompt) is legacy
        assert ("UNRELATED_HELD_MESSAGE" in prompt) is held
        assert all(f["task_id"] != "A" for f in
                   calls[0]["overrides"].get("background_task_completions", []))
    else:
        assert calls == [], "a stale-only wake must not reach the model"


@pytest.mark.parametrize("delivery", ["queued", "steered"])
async def test_card_fence_survives_recorder_lock_wait(mock_db, monkeypatch, delivery):
    import asyncio
    from tests.core.test_turn_recorder import _mk_session, _recorder, _rows, SID

    _mk_session()
    reg = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", reg)
    reg.start_task(SID, "A", "shell", "first")
    reg.complete_task(SID, "A", "completed")
    frame = {"task_id": "A", "status": "completed", "summary": "old",
             "task_incarnation": reg.task_incarnation(SID, "A")}
    recorder = await _recorder()
    entered = asyncio.Event()
    original_record = recorder.record_task_terminal

    async def record(frames):
        entered.set()
        return await original_record(frames)

    monkeypatch.setattr(recorder, "record_task_terminal", record)
    turn = turns.enqueue_turn(SID, "unpark", prompt="old", payload={"pending_frames": [frame]})
    runtime = sr.SessionRuntime(turn)
    # Let start run, but hold the terminal append's recorder lock.
    monkeypatch.setattr(recorder, "start", AsyncMock())
    await recorder._lock.acquire()
    if delivery == "steered":
        task = asyncio.create_task(runtime._on_task_notification_delivered(
            recorder, {"frames": [frame], "task_ids": ["A"]},
        ))
    else:
        async def pump(**kwargs):
            pytest.fail("stale-only wake should not run the model")
            yield
        monkeypatch.setattr("app.core.query_engine._stream_to_websocket_impl", pump)
        task = asyncio.create_task(runtime._run_pump(recorder))
    await entered.wait()
    reg.start_task(SID, "A", "shell", "second")
    recorder._lock.release()
    await task
    assert not [r for r in _rows() if r["event_type"] == "task.updated"]
    assert "A" not in reg._terminal_notified.get(SID, set())
    assert reg.get_task(SID, "A")["status"] == "running"
    await recorder.close()


@pytest.mark.parametrize("delivery", ["claimed", "drained"])
async def test_card_fence_survives_database_admission(
    mock_db: Any, monkeypatch: pytest.MonkeyPatch, delivery: str,
) -> None:
    """The old terminal has passed the recorder guard before DB admission stalls."""
    import asyncio
    import threading
    from contextlib import contextmanager

    from app.core import turn_recorder as tr
    from app.db import session_events as se
    from tests.core.test_turn_recorder import _mk_session, _recorder, _rows, SID

    _mk_session()
    reg = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", reg)
    reg.start_task(SID, "A", "shell", "first")
    await tr.record_task_start_frame(SID, "A", description="first")
    token = reg.task_incarnation(SID, "A")
    assert se.get_latest_task_updates(SID)[0]["task_incarnation"] == token
    reg.complete_task(SID, "A", "completed")
    recorder = await _recorder()
    entered, release = threading.Event(), threading.Event()
    original_event_db = se.get_event_db

    @contextmanager
    def blocked_admission() -> Iterator[Any]:
        # Block only the old terminal, inside append_events but before it gets
        # a connection. The restarted task's running write must proceed first.
        if not entered.is_set():
            entered.set()
            assert release.wait(10), "test did not release database admission"
        with original_event_db() as conn:
            yield conn

    monkeypatch.setattr(se, "get_event_db", blocked_admission)
    if delivery == "claimed":
        pending = recorder.record_task_terminal([{
            "task_id": "A", "status": "completed", "summary": "old terminal",
            "task_incarnation": token,
        }])
    else:
        pending = tr.record_task_settle_frame(SID, "A", "completed", "old terminal")
    terminal = asyncio.create_task(pending)
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        reg.start_task(SID, "A", "shell", "second")
        assert not reg.matches_task_incarnation(SID, "A", token)
        await tr.record_task_start_frame(SID, "A", description="second")
        current = se.get_latest_task_updates(SID)[0]
        assert current["state"] == "running"
        assert current["task_incarnation"] == reg.task_incarnation(SID, "A")
    finally:
        release.set()
        await asyncio.wait_for(terminal, 5)
        await recorder.close()

    # The real durable projection AND raw replay/live input stay running.
    assert se.get_latest_task_updates(SID)[0] == current
    updates = [r["payload"] for r in _rows() if r["event_type"] == "task.updated"]
    assert [r["state"] for r in updates] == ["running", "running"]
    assert reg.get_task(SID, "A")["status"] == "running"


@pytest.mark.parametrize("system_frame", [True, False])
async def test_translation_preserves_receipt_incarnation(mock_db, monkeypatch, system_frame):
    from app.core import turn_recorder as tr
    from app.db import session_events as se
    from tests.core.test_turn_recorder import _mk_session, SID

    _mk_session()
    reg = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", reg)
    reg.start_task(SID, "A", "shell", "first")
    token = reg.task_incarnation(SID, "A")
    reg.complete_task(SID, "A", "completed")
    reg.start_task(SID, "A", "shell", "second")
    await tr.record_task_start_frame(SID, "A", description="second")
    data = {"task_id": "A", "status": "completed", "task_incarnation": token}
    frame = ({"type": "system", "subtype": "task_updated", "data": data}
             if system_frame else {"type": "task_notification", **data})
    # Do not stamp the new registry token over a delayed, already-tagged frame.
    assert tr._translate_shared(frame, SID)[0]["payload"]["task_incarnation"] == token
    await tr.record_drained_event(SID, frame)
    assert se.get_latest_task_updates(SID)[0]["state"] == "running"


async def test_adapter_engine_fence_rechecks_after_session_await(mock_db, monkeypatch):
    from app.core.provider.harness_adapter import HarnessAdapter
    from fluid.engine.background import BackgroundLifecycleMixin
    from fluid.tools.registry import BackgroundTaskReceipt

    reg = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", reg)
    reg.start_task("s", "A", "shell", "first")
    reg.complete_task("s", "A", "completed")
    token = reg.task_incarnation("s", "A")

    class Engine(BackgroundLifecycleMixin):
        async def submit(self, op):
            await self._finish_background_task(op)

        async def _ensure_session(self):
            # The host restarts AFTER the adapter's pre-submit check.
            reg.start_task("s", "A", "shell", "second")
            return SimpleNamespace(session_id="s")

    engine = Engine()
    engine.ctx = SimpleNamespace(extra={})
    engine._init_background_lifecycle()
    engine._background_rehydrated = True
    engine._background_pending["A"] = BackgroundTaskReceipt(task_id="A")
    engine._emit = AsyncMock()
    engine._active_turn_id = None
    adapter = HarnessAdapter()
    adapter._engine = engine
    adapter._options = SimpleNamespace(extra={"app_session_id": "s"})
    await adapter.prepare_background_completions([
        {"task_id": "A", "status": "completed", "task_incarnation": token},
    ])
    assert "A" in engine._background_pending
    assert "A" not in engine._background_terminal
    engine._emit.assert_not_awaited()
