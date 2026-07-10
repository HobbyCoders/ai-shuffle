"""Wake-turn interrupt finalize, Half 2 (lifecycle batch, item 6a).

Stopping a CLI-owned wake turn (the follow-up turn the CLI starts on its own
after a backgrounded task rests, forwarded live by the drain pump) used to
finalize as a NORMAL turn end: the pump's finalize hardcoded
``interrupted=False`` into ``end_streaming``/``broadcast_stream_end``, and —
because a wake turn has no ``run_query`` finally to consume them — the
``_stopped_sessions`` latch and the ``unpark_pending`` latch LEAKED, silently
suppressing every later queue kick for the session.

These tests drive the REAL pump reader with real SDK messages and pin:

1. A wake ResultMessage landing with the session in ``_stopped_sessions``
   finalizes ``interrupted=True`` (live store [Stopped] marker + stream_end
   broadcast) and CLears both leaked latches.
2. An error-shaped wake ResultMessage finalizes interrupted too.
3. ``handle_stop`` during a CLI wake: ``interrupt_session`` is called, there
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
from app.core.live_session_store import live_session_store
from app.core.task_registry import task_registry
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock


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


@pytest.fixture(autouse=True)
async def _isolate():
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
    await live_session_store.cleanup_ended_session(SESSION, force=True)


async def _seed_parked_live_session():
    await live_session_store.start_streaming(SESSION, initial_messages=[])
    await live_session_store.end_streaming(SESSION, metadata={})


async def test_stop_latched_wake_finalizes_interrupted_and_clears_latches():
    await _seed_parked_live_session()
    # Simulate handle_stop landing mid-wake: latch BEFORE the ResultMessage.
    _stopped_sessions.add(SESSION)

    client = _FakeClient([_assistant_text(), _result()], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    end_bcast = AsyncMock()
    with _patch_pool(state), \
         patch.object(cwh.sync_engine, "broadcast_stream_end", end_bcast):
        handler._start_drain_pump(SESSION, "claude-code", None, None)
        for _ in range(40):
            await asyncio.sleep(0.01)
            if end_bcast.await_count:
                break
        await ws_cleanup.stop_drain_pump(SESSION)

    # (a) finalize marked the wake turn interrupted, not a clean end.
    end_bcast.assert_awaited_once()
    assert end_bcast.await_args.kwargs["interrupted"] is True
    data = live_session_store.get_session_dict(SESSION)
    wake_msgs = [m for m in data["messages"] if "wake answer" in m["content"]]
    assert wake_msgs and wake_msgs[-1]["content"].endswith("[Stopped]")
    # (b) leaked latches cleared: a stranded _stopped_sessions entry would
    # suppress every later queue kick; a stranded unpark_pending strands the
    # park-poller.
    assert SESSION not in _stopped_sessions
    assert task_registry.is_unpark_pending(SESSION) is False
    assert live_session_store.is_streaming(SESSION) is False


async def test_error_result_wake_finalizes_interrupted():
    await _seed_parked_live_session()
    client = _FakeClient(
        [_assistant_text(), _result(is_error=True, subtype="error_during_execution")],
        hang=True,
    )
    state = _fake_state(client)
    handler = _make_handler()

    end_bcast = AsyncMock()
    with _patch_pool(state), \
         patch.object(cwh.sync_engine, "broadcast_stream_end", end_bcast):
        handler._start_drain_pump(SESSION, "claude-code", None, None)
        for _ in range(40):
            await asyncio.sleep(0.01)
            if end_bcast.await_count:
                break
        await ws_cleanup.stop_drain_pump(SESSION)

    end_bcast.assert_awaited_once()
    assert end_bcast.await_args.kwargs["interrupted"] is True


async def test_clean_wake_finalize_still_not_interrupted():
    await _seed_parked_live_session()
    client = _FakeClient([_assistant_text(), _result()], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    end_bcast = AsyncMock()
    with _patch_pool(state), \
         patch.object(cwh.sync_engine, "broadcast_stream_end", end_bcast):
        handler._start_drain_pump(SESSION, "claude-code", None, None)
        for _ in range(40):
            await asyncio.sleep(0.01)
            if end_bcast.await_count:
                break
        await ws_cleanup.stop_drain_pump(SESSION)

    end_bcast.assert_awaited_once()
    assert end_bcast.await_args.kwargs["interrupted"] is False
    data = live_session_store.get_session_dict(SESSION)
    assert not any(m["content"].endswith("[Stopped]") for m in data["messages"])


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
