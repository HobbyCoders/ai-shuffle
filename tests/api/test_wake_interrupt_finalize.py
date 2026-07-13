"""Wake-turn interrupt finalize, Half 2 (lifecycle batch, item 6a).

Stopping a CLI-owned wake turn (the follow-up turn the CLI starts on its own
after a backgrounded task rests, forwarded by the drain pump) used to finalize
as a NORMAL turn end — and, because a wake turn has no ``run_query`` finally to
consume them, the ``_stopped_sessions`` latch and the ``unpark_pending`` latch
LEAKED, silently suppressing every later queue kick for the session.

V2 (Phase-1 deletion): the live store and ``broadcast_stream_end`` are deleted;
the wake finalize's observable contract is now durable/registry-level, pinned
here by driving the REAL pump reader with real SDK messages:

1. A wake ResultMessage landing with the session in ``_stopped_sessions``
   finalizes via ``persist_turn_end_state`` AND consumes both leaked latches
   (``_stopped_sessions`` entry + ``unpark_pending``).
2. An error-shaped wake ResultMessage counts as interrupted too: any app
   unparks HELD in the pump's batch window are DROPPED (a stopped/errored wake
   episode must not auto-start a follow-up turn), and the latches clear.
3. A CLEAN wake finalize still delivers a held unpark (the complement proving
   the drop in (2) is interrupt-specific).
4. ``handle_stop`` during a CLI wake: ``interrupt_session`` is called, there
   is no foreground ``_active_chat_sessions`` task to cancel (correctly
   skipped, nothing raises), and a plain Stop with the preserve-background
   patch active does NOT call ``stop_all_background_tasks``.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.chat_ws_handler as cwh
import app.api.ws_cleanup as ws_cleanup
from app.api.ws_cleanup import _stopped_sessions
from app.core.task_registry import task_registry
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TaskNotificationMessage,
    TextBlock,
)


SESSION = "sess-wake-stop"


def _assistant_text(text="wake answer"):
    return AssistantMessage(content=[TextBlock(text)], model="claude")


def _result(*, is_error=False, subtype="success"):
    return ResultMessage(
        subtype=subtype,
        duration_ms=5,
        duration_api_ms=5,
        is_error=is_error,
        num_turns=1,
        session_id=SESSION,
    )


def _notif(task_id, *, status="completed", uuid="u-1", tool_use_id="tu-1",
           summary="done"):
    """A real SDK TaskNotificationMessage (so isinstance checks match)."""
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status=status,
        uuid=uuid,
        tool_use_id=tool_use_id,
        output_file="",
        summary=summary,
        session_id=SESSION,
    )


class _FakeClient:
    def __init__(self, messages, *, hang=False, delay_between=0.0):
        self._messages = list(messages)
        self._hang = hang
        self._delay = delay_between

    async def receive_messages(self):
        for i, msg in enumerate(self._messages):
            if i and self._delay:
                await asyncio.sleep(self._delay)
            yield msg
        if self._hang:
            await asyncio.Event().wait()


def _fake_state(client, *, is_streaming=False, alive=True):
    state = MagicMock()
    state.is_streaming = is_streaming
    state.stale_turn_undrained = False
    state.client = client
    adapter = MagicMock()
    adapter.is_alive = MagicMock(return_value=alive)
    state.adapter = adapter
    return state


def _make_handler():
    handler = cwh.ChatWebSocketHandler(
        websocket=MagicMock(),
        device_id="dev-1",
        user_id="admin",
        api_user_id=None,
    )
    handler.send_json = AsyncMock(return_value=True)
    handler._deliver_unpark = AsyncMock(return_value=True)
    return handler


def _patch_pool(state):
    pool = MagicMock()
    pool.peek = MagicMock(return_value=state)
    return patch("app.core.session_pool.get_session_pool", return_value=pool)


def _patch_recorder():
    """Keep the pump's recorder hand-off out of the DB for these tests."""
    return patch(
        "app.core.turn_recorder.record_drained_event", new=AsyncMock()
    )


@pytest.fixture(autouse=True)
def _isolate():
    task_registry.reset_all()
    ws_cleanup._drain_pumps.clear()
    _stopped_sessions.discard(SESSION)
    _saved_window = cwh._UNPARK_BATCH_WINDOW_SECONDS
    cwh._UNPARK_BATCH_WINDOW_SECONDS = 0.05
    yield
    cwh._UNPARK_BATCH_WINDOW_SECONDS = _saved_window
    ws_cleanup._drain_pumps.clear()
    _stopped_sessions.discard(SESSION)
    task_registry.reset_all()


def _seed_held_shell_unpark():
    """A shell-kind background task whose terminal notification the pump HOLDS
    in its batch window (app-owned unpark class — agents are cli-owned)."""
    task_registry.start_task(SESSION, "task-1", "shell", "long build")
    task_registry.mark_sdk_background_task(SESSION, "task-1")
    task_registry.mark_background_launch(SESSION, "tu-1")


async def test_stop_latched_wake_finalizes_and_clears_latches():
    # Simulate handle_stop landing mid-wake: latch BEFORE the ResultMessage.
    _stopped_sessions.add(SESSION)

    client = _FakeClient([_assistant_text(), _result()])
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state), _patch_recorder(), \
         patch.object(cwh, "persist_turn_end_state", new=AsyncMock()) as finalize:
        await handler._drain_notifications(SESSION, "claude-code", None, None)

    # (a) the wake still ran the durable turn-end finalize.
    finalize.assert_awaited_once_with(SESSION)
    # (b) leaked latches consumed by the INTERRUPTED branch: a stranded
    # _stopped_sessions entry would suppress every later queue kick (a wake has
    # no run_query finally to consume it); a stranded unpark_pending strands
    # the park-poller. The latch consumption is what proves the finalize took
    # the interrupted path — a clean finalize never touches _stopped_sessions.
    assert SESSION not in _stopped_sessions
    assert task_registry.is_unpark_pending(SESSION) is False


async def test_error_result_wake_drops_held_unparks():
    """An error-shaped wake ResultMessage counts as interrupted: app unparks
    HELD in the batch window are dropped (the episode must not auto-start a
    follow-up turn), and the poller latch clears."""
    _seed_held_shell_unpark()

    client = _FakeClient([
        _notif("task-1"),  # held: batch window (0.05s) can't expire between
        _assistant_text(),  # synchronous yields, so it is still pending at the
        _result(is_error=True, subtype="error_during_execution"),  # finalize
    ])
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state), _patch_recorder(), \
         patch.object(cwh, "persist_turn_end_state", new=AsyncMock()) as finalize:
        await handler._drain_notifications(SESSION, "claude-code", None, None)

    # The held unpark was DROPPED, not delivered after the errored wake.
    handler._deliver_unpark.assert_not_awaited()
    # Latches consumed; the turn-end finalize still ran (pending was cleared
    # before the finalize gate, so the durable parked state is re-written).
    assert task_registry.is_unpark_pending(SESSION) is False
    finalize.assert_awaited_once_with(SESSION)


async def test_clean_wake_finalize_still_delivers_held_unpark():
    """Complement of the drop above: a CLEAN wake finalize keeps the held
    unpark alive and it is delivered — proving the drop is interrupt-specific,
    not a general behavior of wakes with pending work."""
    _seed_held_shell_unpark()

    client = _FakeClient([_notif("task-1"), _assistant_text(), _result()])
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state), _patch_recorder(), \
         patch.object(cwh, "persist_turn_end_state", new=AsyncMock()):
        await handler._drain_notifications(SESSION, "claude-code", None, None)

    handler._deliver_unpark.assert_awaited_once()
    assert SESSION not in _stopped_sessions


async def test_handle_stop_during_wake_interrupts_without_foreground_cancel():
    """handle_stop's existing path holds for a CLI wake: interrupt_session is
    invoked, no _active_chat_sessions entry exists to cancel (skipped without
    error), and a plain Stop with the preserve patch active never calls
    stop_all_background_tasks (#504 gating untouched)."""
    from app.api.websocket import _active_chat_sessions

    assert SESSION not in _active_chat_sessions  # no foreground turn — a wake

    handler = _make_handler()
    handler.current_session_id = SESSION

    interrupt = AsyncMock(return_value=True)
    stop_all = AsyncMock(return_value=[])
    with patch("app.core.query_engine.interrupt_session", interrupt), \
         patch("app.core.query_engine.stop_all_background_tasks", stop_all), \
         patch("app.core.cli_interrupt_patch.is_patch_active", return_value=True):
        # Plain Stop (no stop_background opt-in) — preserve stays in force.
        await handler.handle_stop({"session_id": SESSION})

    interrupt.assert_awaited_once_with(SESSION)
    stop_all.assert_not_awaited()
    # The stop latch is armed for the pump finalize to consume.
    assert SESSION in _stopped_sessions
