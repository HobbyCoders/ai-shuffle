"""Wake-turn streaming state, Half 1 (lifecycle-notification batch, item 2).

A CLI-owned wake turn (a backgrounded Task/Agent coming to rest while the
parent is parked makes the CLI start its OWN follow-up turn) is forwarded by
the between-turns drain pump into the live store — but the LiveSession's
``is_streaming`` stayed ``False`` for the whole wake turn, so ``/live`` kept
reporting an idle session while content streamed in: the composer never locked,
pollers never upshifted, and the "thinking" affordances never showed.

These tests drive the REAL drain pump (``_drain_notifications_impl`` via the
handler) against the REAL ``live_session_store`` — no fixture-planted state —
and pin the new behavior:

1. ``is_streaming`` flips True when the pump forwards the wake turn's first
   event (RED before the fix: it never flips).
2. It flips back False at the wake turn's ResultMessage finalize.
3. A pump cancelled mid-wake clears it (leak guard — a stranded True freezes
   the composer forever, since nothing else ever clears it).
4. The ``/live`` response body reports the wake-turn streaming state.

Also pins the ``set_streaming`` store primitive itself: it must mutate the
EXISTING LiveSession (never rebuild/wipe the buffer the way ``start_streaming``
does) and be a no-op for unknown sessions.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.chat_ws_handler as cwh
import app.api.ws_cleanup as ws_cleanup
from app.core.live_session_store import live_session_store
from app.core.task_registry import task_registry
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
)


SESSION = "sess-wake"


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
    """Pooled SDK client stand-in (mirrors tests/api/test_chat_ws_drain_pump).

    ``delay_between`` inserts a real await between scripted messages so a test
    can observe the pump's TRANSIENT state mid-wake (e.g. is_streaming=True
    between the first forwarded event and the ResultMessage finalize).
    """

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
    _saved_window = cwh._UNPARK_BATCH_WINDOW_SECONDS
    cwh._UNPARK_BATCH_WINDOW_SECONDS = 0.05
    yield
    cwh._UNPARK_BATCH_WINDOW_SECONDS = _saved_window
    ws_cleanup._drain_pumps.clear()
    task_registry.reset_all()
    await live_session_store.cleanup_ended_session(SESSION, force=True)


async def _seed_parked_live_session():
    """A REAL parked LiveSession: a previous turn streamed and ended."""
    await live_session_store.start_streaming(SESSION, initial_messages=[])
    await live_session_store.end_streaming(SESSION, metadata={})
    assert live_session_store.is_streaming(SESSION) is False


# ---------------------------------------------------------------------------
# Store primitive
# ---------------------------------------------------------------------------

async def test_set_streaming_mutates_existing_session_without_wiping_buffer():
    await live_session_store.start_streaming(SESSION, initial_messages=[])
    await live_session_store.add_user_message(SESSION, "hello", "m-1")
    await live_session_store.end_streaming(SESSION, metadata={})
    before = live_session_store.get_session_dict(SESSION)
    assert before is not None
    msg_count = len(before["messages"])
    update_before = before["last_update_id"]

    await live_session_store.set_streaming(SESSION, True)

    after = live_session_store.get_session_dict(SESSION)
    assert after["is_streaming"] is True
    # Buffer untouched — set_streaming must NOT rebuild the session the way
    # start_streaming does.
    assert len(after["messages"]) == msg_count
    # Pollers must wake: the update counter bumped.
    assert after["last_update_id"] > update_before

    await live_session_store.set_streaming(SESSION, False)
    assert live_session_store.is_streaming(SESSION) is False


async def test_set_streaming_noop_for_unknown_session():
    # Must not create a session, must not raise.
    await live_session_store.set_streaming("no-such-session", True)
    assert live_session_store.has_session("no-such-session") is False


# ---------------------------------------------------------------------------
# Real pump: wake turn flips is_streaming True, ResultMessage flips it back
# ---------------------------------------------------------------------------

async def test_wake_turn_first_forward_sets_streaming_true():
    await _seed_parked_live_session()
    client = _FakeClient([_assistant_text()], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        handler._start_drain_pump(SESSION, "claude-code", None, None)
        # Let the pump forward the wake turn's first event.
        for _ in range(10):
            await asyncio.sleep(0.01)
            if live_session_store.is_streaming(SESSION):
                break
        assert live_session_store.is_streaming(SESSION) is True, (
            "wake-turn content forwarded but the live session still reports "
            "is_streaming=False (composer/pollers see an idle session)"
        )
        await ws_cleanup.stop_drain_pump(SESSION)


async def test_wake_turn_result_message_clears_streaming():
    await _seed_parked_live_session()
    # A delay between the forwarded text and the ResultMessage lets the test
    # observe the TRANSIENT mid-wake True before the finalize flips it back —
    # otherwise this would pass vacuously (False the whole time).
    client = _FakeClient([_assistant_text(), _result()], hang=True,
                         delay_between=0.08)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        handler._start_drain_pump(SESSION, "claude-code", None, None)
        saw_streaming = False
        for _ in range(40):
            await asyncio.sleep(0.005)
            if live_session_store.is_streaming(SESSION):
                saw_streaming = True
                break
        assert saw_streaming, "wake turn never flipped is_streaming True"
        # …and the ResultMessage finalize flips it back False.
        for _ in range(40):
            await asyncio.sleep(0.01)
            if not live_session_store.is_streaming(SESSION):
                break
        assert live_session_store.is_streaming(SESSION) is False
        # The wake turn's content DID land (proves the flag cycled True→False
        # around a real forwarded turn, not that nothing happened).
        data = live_session_store.get_session_dict(SESSION)
        assert any(m["content"] == "wake answer" for m in data["messages"])
        await ws_cleanup.stop_drain_pump(SESSION)


async def test_cancelled_mid_wake_clears_streaming():
    """stop_drain_pump (a new turn starting / shutdown) mid-wake must clear the
    flag — a stranded True freezes the composer with no turn to ever end it."""
    await _seed_parked_live_session()
    client = _FakeClient([_assistant_text()], hang=True)
    state = _fake_state(client)
    handler = _make_handler()

    with _patch_pool(state):
        handler._start_drain_pump(SESSION, "claude-code", None, None)
        for _ in range(10):
            await asyncio.sleep(0.01)
            if live_session_store.is_streaming(SESSION):
                break
        assert live_session_store.is_streaming(SESSION) is True
        await ws_cleanup.stop_drain_pump(SESSION)

    assert live_session_store.is_streaming(SESSION) is False


# ---------------------------------------------------------------------------
# /live surfaces the wake-turn streaming state
# ---------------------------------------------------------------------------

async def test_live_body_reports_wake_turn_streaming():
    from app.api.sessions import get_live_session

    await _seed_parked_live_session()
    await live_session_store.set_streaming(SESSION, True)

    session_row = {"id": SESSION, "title": "Wake"}
    with patch("app.api.sessions.require_session",
               new=AsyncMock(return_value=session_row)), \
         patch("app.api.sessions.enrich_session_worktree_meta",
               new=MagicMock(return_value={})), \
         patch("app.core.cloud_relay.is_remote_session",
               new=MagicMock(return_value=False)):
        result = await get_live_session(
            request=MagicMock(), session_id=SESSION,
            since_update_id=1, token="tok",
        )

    assert isinstance(result, dict)
    assert result["is_streaming"] is True
