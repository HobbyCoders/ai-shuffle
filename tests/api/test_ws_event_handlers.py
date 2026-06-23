"""
Unit tests for app/api/ws_event_handlers.py

Tests the dispatch table, individual event handlers, and the
EventContext / MutableStreamState dataclasses.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.api.ws_event_handlers import (
    EventContext,
    MutableStreamState,
    EVENT_HANDLERS,
    dispatch_event,
    handle_stream_start,
    handle_stream_delta,
    handle_chunk,
    handle_tool_use,
    handle_tool_result,
    handle_system,
    handle_subagent_start,
    handle_subagent_chunk,
    handle_subagent_tool_use,
    handle_subagent_tool_result,
    handle_subagent_done,
    handle_stream_block_start,
    handle_stream_block_stop,
    handle_stream_message_delta,
    handle_done,
    handle_task_notification,
)
from app.core.streaming_context import StreamingContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_ctx(session_id="sess-1", message_id="msg-1", user_id="admin",
             project_id="proj-1", worktree_id=None):
    """Build an EventContext with a real StreamingContext."""
    streaming_ctx = StreamingContext(
        session_id=session_id,
        device_id="dev-1",
        message_id=message_id,
        user_id=user_id,
    )
    return EventContext(
        ctx=streaming_ctx,
        session_id=session_id,
        message_id=message_id,
        user_id=user_id,
        project_id=project_id,
        worktree_id=worktree_id,
    )


def make_state(**overrides):
    """Build a MutableStreamState with optional overrides."""
    return MutableStreamState(**overrides)


# ---------------------------------------------------------------------------
# Dataclass construction
# ---------------------------------------------------------------------------

class TestDataclasses:
    def test_event_context_fields(self):
        ectx = make_ctx()
        assert ectx.session_id == "sess-1"
        assert ectx.message_id == "msg-1"
        assert ectx.user_id == "admin"
        assert ectx.project_id == "proj-1"
        assert ectx.worktree_id is None

    def test_mutable_stream_state_defaults(self):
        state = MutableStreamState()
        assert state.stream_started is False
        assert state.current_tool_id is None
        assert state.current_tool_name is None
        assert state.connection_lost_logged is False
        assert state.final_metadata == {}
        assert isinstance(state.query_start_time, float)


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

class TestDispatchTable:
    def test_all_expected_event_types_registered(self):
        expected = {
            "stream_start", "stream_delta", "chunk", "tool_use",
            "tool_result", "system", "subagent_start", "subagent_chunk",
            "subagent_tool_use", "subagent_tool_result", "subagent_done",
            "task_progress", "task_notification",
            "stream_block_start", "stream_block_stop", "stream_message_delta",
            "done", "interrupted",
        }
        assert set(EVENT_HANDLERS.keys()) == expected

    def test_done_and_interrupted_share_handler(self):
        assert EVENT_HANDLERS["done"] is EVENT_HANDLERS["interrupted"]
        assert EVENT_HANDLERS["done"] is handle_done

    @pytest.mark.asyncio
    async def test_dispatch_calls_registered_handler(self):
        """dispatch_event should look up the handler and call it."""
        ectx = make_ctx()
        state = make_state()

        with patch("app.api.ws_event_handlers.handle_chunk", new_callable=AsyncMock) as mock_handler:
            # Temporarily replace the handler in the dict
            original = EVENT_HANDLERS["chunk"]
            EVENT_HANDLERS["chunk"] = mock_handler
            try:
                await dispatch_event({"type": "chunk", "content": "hello"}, ectx, state)
                mock_handler.assert_awaited_once_with(
                    {"type": "chunk", "content": "hello"}, ectx, state
                )
            finally:
                EVENT_HANDLERS["chunk"] = original

    @pytest.mark.asyncio
    async def test_dispatch_unknown_event_is_noop(self):
        """Unknown event types should be silently ignored."""
        ectx = make_ctx()
        state = make_state()
        # Should not raise
        await dispatch_event({"type": "plugin_message", "data": {}}, ectx, state)

    @pytest.mark.asyncio
    async def test_dispatch_no_type_is_noop(self):
        """Events without a type key should be silently ignored."""
        ectx = make_ctx()
        state = make_state()
        await dispatch_event({"content": "data"}, ectx, state)


# ---------------------------------------------------------------------------
# handle_stream_start
# ---------------------------------------------------------------------------

class TestHandleStreamStart:
    @pytest.mark.asyncio
    async def test_first_stream_start_sets_stream_started(self):
        ectx = make_ctx()
        state = make_state()
        event = {"type": "stream_start", "message": {"usage": {"input_tokens": 100, "output_tokens": 50}}}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.update_usage = AsyncMock()
            mock_sync.broadcast_stream_start = AsyncMock()

            await handle_stream_start(event, ectx, state)

            assert state.stream_started is True
            mock_sync.broadcast_stream_start.assert_awaited_once()
            mock_store.update_usage.assert_awaited_once_with(
                "sess-1",
                input_tokens=100,
                output_tokens=50,
                cache_creation_tokens=0,
                cache_read_tokens=0,
            )

    @pytest.mark.asyncio
    async def test_subsequent_stream_start_broadcasts_usage_update(self):
        ectx = make_ctx()
        state = make_state(stream_started=True)
        usage = {"input_tokens": 200, "output_tokens": 100}
        event = {"type": "stream_start", "message": {"usage": usage}}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.update_usage = AsyncMock()
            mock_sync.broadcast_usage_update = AsyncMock()

            await handle_stream_start(event, ectx, state)

            # Should NOT call broadcast_stream_start again
            mock_sync.broadcast_stream_start = AsyncMock()
            mock_sync.broadcast_stream_start.assert_not_awaited()
            # Should broadcast usage update
            mock_sync.broadcast_usage_update.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_first_stream_start_no_usage(self):
        """stream_start without usage data should still broadcast start."""
        ectx = make_ctx()
        state = make_state()
        event = {"type": "stream_start", "message": {}}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.update_usage = AsyncMock()
            mock_sync.broadcast_stream_start = AsyncMock()

            await handle_stream_start(event, ectx, state)

            assert state.stream_started is True
            mock_sync.broadcast_stream_start.assert_awaited_once()
            mock_store.update_usage.assert_not_awaited()


# ---------------------------------------------------------------------------
# handle_stream_delta
# ---------------------------------------------------------------------------

class TestHandleStreamDelta:
    @pytest.mark.asyncio
    async def test_text_delta_appends_text(self):
        ectx = make_ctx()
        state = make_state(stream_started=True)
        event = {"type": "stream_delta", "delta_type": "text", "content": "hello", "index": 0}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.finalize_thinking = AsyncMock()
            mock_store.append_text = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_stream_delta(event, ectx, state)

            mock_store.finalize_thinking.assert_awaited_once_with("sess-1")
            mock_store.append_text.assert_awaited_once_with("sess-1", "hello")
            mock_sync.broadcast_stream_chunk.assert_awaited_once()
            call_kwargs = mock_sync.broadcast_stream_chunk.call_args[1]
            assert call_kwargs["chunk_type"] == "text"

    @pytest.mark.asyncio
    async def test_thinking_delta_appends_thinking(self):
        ectx = make_ctx()
        state = make_state(stream_started=True)
        event = {"type": "stream_delta", "delta_type": "thinking", "content": "hmm", "index": 0}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.append_thinking = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_stream_delta(event, ectx, state)

            mock_store.append_thinking.assert_awaited_once_with("sess-1", "hmm")
            call_kwargs = mock_sync.broadcast_stream_chunk.call_args[1]
            assert call_kwargs["chunk_type"] == "thinking"

    @pytest.mark.asyncio
    async def test_tool_input_delta_updates_tool_input(self):
        ectx = make_ctx()
        state = make_state(stream_started=True, current_tool_id="tool-1")
        event = {"type": "stream_delta", "delta_type": "tool_input", "content": "{", "index": 0}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.update_tool_input_streaming = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_stream_delta(event, ectx, state)

            mock_store.update_tool_input_streaming.assert_awaited_once_with("sess-1", "tool-1", "{")
            call_kwargs = mock_sync.broadcast_stream_chunk.call_args[1]
            assert call_kwargs["chunk_type"] == "tool_input"

    @pytest.mark.asyncio
    async def test_stream_delta_triggers_first_start(self):
        """stream_delta before stream_start should trigger broadcast_stream_start."""
        ectx = make_ctx()
        state = make_state(stream_started=False)
        event = {"type": "stream_delta", "delta_type": "text", "content": "hi", "index": 0}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.finalize_thinking = AsyncMock()
            mock_store.append_text = AsyncMock()
            mock_sync.broadcast_stream_start = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_stream_delta(event, ectx, state)

            assert state.stream_started is True
            mock_sync.broadcast_stream_start.assert_awaited_once()


# ---------------------------------------------------------------------------
# handle_tool_use / handle_tool_result
# ---------------------------------------------------------------------------

class TestToolHandlers:
    @pytest.mark.asyncio
    async def test_tool_use_updates_state(self):
        ectx = make_ctx()
        state = make_state(stream_started=True)
        event = {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "ls"}}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.add_tool_use = AsyncMock()
            mock_sync.broadcast_stream_start = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_tool_use(event, ectx, state)

            assert state.current_tool_id == "tool-1"
            assert state.current_tool_name == "Bash"
            mock_store.add_tool_use.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_tool_result_clears_state(self):
        ectx = make_ctx()
        state = make_state(stream_started=True, current_tool_id="tool-1", current_tool_name="Bash")
        event = {"type": "tool_result", "tool_use_id": "tool-1", "output": "file.txt", "is_error": False}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.add_tool_result = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_tool_result(event, ectx, state)

            assert state.current_tool_id is None
            assert state.current_tool_name is None
            # Verify tool_name was captured before clearing
            chunk_data = mock_sync.broadcast_stream_chunk.call_args[1]["chunk_data"]
            assert chunk_data["tool_name"] == "Bash"

    @pytest.mark.asyncio
    async def test_tool_result_forwards_screenshot_to_store(self):
        """A screenshot on the event is passed to the store but NOT duplicated
        into the broadcast chunk (it's delivered via the /live poll)."""
        ectx = make_ctx()
        state = make_state(stream_started=True, current_tool_id="tool-1",
                           current_tool_name="mcp__claude-in-chrome__computer")
        data_uri = "data:image/png;base64,shotdata"
        event = {
            "type": "tool_result",
            "tool_use_id": "tool-1",
            "output": "screenshot taken",
            "is_error": False,
            "screenshot": data_uri,
        }

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.add_tool_result = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_tool_result(event, ectx, state)

            assert mock_store.add_tool_result.call_args[1]["screenshot"] == data_uri
            chunk_data = mock_sync.broadcast_stream_chunk.call_args[1]["chunk_data"]
            assert "screenshot" not in chunk_data

    @pytest.mark.asyncio
    async def test_tool_result_without_screenshot_passes_none(self):
        """Non-image tool results forward screenshot=None to the store."""
        ectx = make_ctx()
        state = make_state(stream_started=True, current_tool_id="tool-1", current_tool_name="Read")
        event = {"type": "tool_result", "tool_use_id": "tool-1", "output": "x", "is_error": False}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.add_tool_result = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_tool_result(event, ectx, state)

            assert mock_store.add_tool_result.call_args[1]["screenshot"] is None


# ---------------------------------------------------------------------------
# handle_stream_block_start
# ---------------------------------------------------------------------------

class TestStreamBlockStart:
    @pytest.mark.asyncio
    async def test_tool_use_block_creates_early_tool(self):
        ectx = make_ctx()
        state = make_state(stream_started=True)
        event = {
            "type": "stream_block_start",
            "block_type": "tool_use",
            "content_block": {"id": "tool-2", "name": "Read"},
            "index": 0,
        }

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.add_early_tool_use = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_stream_block_start(event, ectx, state)

            assert state.current_tool_id == "tool-2"
            mock_store.add_early_tool_use.assert_awaited_once_with(
                session_id="sess-1", tool_id="tool-2", name="Read"
            )

    @pytest.mark.asyncio
    async def test_text_block_does_not_create_tool(self):
        ectx = make_ctx()
        state = make_state(stream_started=True)
        event = {
            "type": "stream_block_start",
            "block_type": "text",
            "content_block": {},
            "index": 0,
        }

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.add_early_tool_use = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_stream_block_start(event, ectx, state)

            assert state.current_tool_id is None
            mock_store.add_early_tool_use.assert_not_awaited()


# ---------------------------------------------------------------------------
# handle_done
# ---------------------------------------------------------------------------

class TestHandleDone:
    """Tests for handle_done which handles both 'done' and 'interrupted' events.

    handle_done does lazy imports of _delayed_session_cleanup (from websocket)
    and pre_seed_parse_cache/invalidate_parse_cache (from jsonl_parser),
    so we patch those at their origin modules.
    """

    @pytest.mark.asyncio
    async def test_done_sets_final_metadata(self):
        ectx = make_ctx()
        state = make_state(stream_started=True)
        metadata = {"total_cost_usd": 0.01, "tokens_in": 100, "tokens_out": 50}
        event = {"type": "done", "metadata": metadata}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync, \
             patch("app.api.ws_event_handlers.database") as mock_db, \
             patch("app.api.websocket._delayed_session_cleanup", return_value=asyncio.sleep(0)), \
             patch("app.api.ws_event_handlers.asyncio.create_task"), \
             patch("app.core.jsonl_parser.pre_seed_parse_cache"), \
             patch("app.core.jsonl_parser.invalidate_parse_cache"):
            mock_store.end_streaming = AsyncMock()
            mock_store.get_session_dict = MagicMock(return_value=None)
            mock_db.get_session = MagicMock(return_value={"sdk_session_id": "sdk-1"})
            mock_sync.broadcast_stream_end = AsyncMock()

            await handle_done(event, ectx, state)

            assert state.final_metadata == metadata
            mock_store.end_streaming.assert_awaited_once_with(
                session_id="sess-1", metadata=metadata, interrupted=False
            )
            mock_sync.broadcast_stream_end.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_interrupted_sets_interrupted_flag(self):
        ectx = make_ctx()
        state = make_state(stream_started=True)
        event = {"type": "interrupted", "metadata": {}}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync, \
             patch("app.api.ws_event_handlers.database") as mock_db, \
             patch("app.api.websocket._delayed_session_cleanup", return_value=asyncio.sleep(0)), \
             patch("app.api.ws_event_handlers.asyncio.create_task"):
            mock_store.end_streaming = AsyncMock()
            mock_store.get_session_dict = MagicMock(return_value=None)
            mock_db.get_session = MagicMock(return_value=None)
            mock_sync.broadcast_stream_end = AsyncMock()

            await handle_done(event, ectx, state)

            mock_store.end_streaming.assert_awaited_once_with(
                session_id="sess-1", metadata={}, interrupted=True
            )

    @pytest.mark.asyncio
    async def test_done_no_broadcast_when_not_started(self):
        """If stream never started, done should not broadcast stream_end."""
        ectx = make_ctx()
        state = make_state(stream_started=False)
        event = {"type": "done", "metadata": {}}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync, \
             patch("app.api.ws_event_handlers.database") as mock_db, \
             patch("app.api.websocket._delayed_session_cleanup", return_value=asyncio.sleep(0)), \
             patch("app.api.ws_event_handlers.asyncio.create_task"):
            mock_store.end_streaming = AsyncMock()
            mock_store.get_session_dict = MagicMock(return_value=None)
            mock_db.get_session = MagicMock(return_value=None)
            mock_sync.broadcast_stream_end = AsyncMock()

            await handle_done(event, ectx, state)

            mock_sync.broadcast_stream_end.assert_not_awaited()


# ---------------------------------------------------------------------------
# handle_chunk (legacy)
# ---------------------------------------------------------------------------

class TestHandleChunk:
    @pytest.mark.asyncio
    async def test_chunk_appends_text(self):
        ectx = make_ctx()
        state = make_state(stream_started=True)
        event = {"type": "chunk", "content": "world"}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.append_text = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_chunk(event, ectx, state)

            mock_store.append_text.assert_awaited_once_with("sess-1", "world")


# ---------------------------------------------------------------------------
# handle_system
# ---------------------------------------------------------------------------

class TestHandleSystem:
    @pytest.mark.asyncio
    async def test_system_adds_message(self):
        ectx = make_ctx()
        state = make_state()
        event = {"type": "system", "subtype": "info", "data": {"key": "val"}}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.add_system_message = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_system(event, ectx, state)

            mock_store.add_system_message.assert_awaited_once_with(
                session_id="sess-1", content="", subtype="info", data={"key": "val"}
            )


# ---------------------------------------------------------------------------
# Thinking: content channel vs. token-estimate routing
# ---------------------------------------------------------------------------

class TestThinkingHandling:
    """Distinguishes the two thinking signals the newer CLI emits:

    1. Thinking *content* — `stream_delta` deltas with delta_type="thinking",
       captured into a thinking block (this is the real "is there thinking?"
       evidence: when the model reasons, this channel carries the text).
    2. Thinking *token estimate* — `system`/subtype="thinking_tokens" progress
       messages. These must update the active thinking block (like task_progress
       updates its subagent), NOT render as standalone system-message cards.
    """

    @pytest.mark.asyncio
    async def test_thinking_delta_appends_content_and_broadcasts(self):
        """Proof that thinking content flows: a thinking delta is appended to
        the thinking block and forwarded as a 'thinking' chunk (never a card)."""
        ectx = make_ctx()
        state = make_state(stream_started=True)
        event = {
            "type": "stream_delta",
            "delta_type": "thinking",
            "content": "Let me reason about this...",
            "index": 0,
            "session_id": "sess-1",
        }

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.append_thinking = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_stream_delta(event, ectx, state)

            mock_store.append_thinking.assert_awaited_once_with(
                "sess-1", "Let me reason about this..."
            )
            _, kwargs = mock_sync.broadcast_stream_chunk.await_args
            assert kwargs["chunk_type"] == "thinking"

    @pytest.mark.asyncio
    async def test_thinking_tokens_routes_to_block_not_system_message(self):
        """Spec for Option B: a thinking_tokens system message must update the
        active thinking block's token estimate and must NOT create a system
        card. Mirrors the task_progress -> subagent routing in handle_system."""
        ectx = make_ctx()
        state = make_state(stream_started=True)
        event = {
            "type": "system",
            "subtype": "thinking_tokens",
            "data": {"estimated_tokens": 174, "estimated_tokens_delta": 24},
        }

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.update_thinking_tokens = AsyncMock(return_value=True)
            mock_store.add_system_message = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_system(event, ectx, state)

            mock_store.update_thinking_tokens.assert_awaited_once_with(
                session_id="sess-1", estimated_tokens=174
            )
            mock_store.add_system_message.assert_not_awaited()


# ---------------------------------------------------------------------------
# Subagent handlers
# ---------------------------------------------------------------------------

class TestSubagentHandlers:
    @pytest.mark.asyncio
    async def test_subagent_start_converts_tool(self):
        ectx = make_ctx()
        state = make_state()
        event = {
            "type": "subagent_start",
            "agent_id": "agent-1",
            "agent_type": "code",
            "description": "test",
            "prompt": "do stuff",
            "tool_id": "tool-1",
        }

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.convert_tool_to_subagent = AsyncMock(return_value=True)
            mock_store.add_subagent_start = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_subagent_start(event, ectx, state)

            mock_store.convert_tool_to_subagent.assert_awaited_once()
            mock_store.add_subagent_start.assert_not_awaited()  # conversion succeeded

    @pytest.mark.asyncio
    async def test_subagent_start_falls_back_to_add(self):
        ectx = make_ctx()
        state = make_state()
        event = {
            "type": "subagent_start",
            "agent_id": "agent-1",
            "agent_type": "code",
            "description": "test",
            "prompt": "do stuff",
            "tool_id": None,
        }

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.convert_tool_to_subagent = AsyncMock(return_value=False)
            mock_store.add_subagent_start = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_subagent_start(event, ectx, state)

            mock_store.add_subagent_start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_subagent_chunk_appends_text(self):
        ectx = make_ctx()
        state = make_state()
        event = {"type": "subagent_chunk", "agent_id": "agent-1", "content": "thinking..."}

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.append_subagent_text = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_subagent_chunk(event, ectx, state)

            mock_store.append_subagent_text.assert_awaited_once_with(
                session_id="sess-1", agent_id="agent-1", content="thinking..."
            )

    @pytest.mark.asyncio
    async def test_subagent_tool_use(self):
        ectx = make_ctx()
        state = make_state()
        event = {
            "type": "subagent_tool_use",
            "agent_id": "agent-1",
            "id": "tool-2",
            "name": "Read",
            "input": {"path": "/tmp"},
        }

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.add_subagent_tool_use = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_subagent_tool_use(event, ectx, state)

            mock_store.add_subagent_tool_use.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_subagent_tool_result(self):
        ectx = make_ctx()
        state = make_state()
        event = {
            "type": "subagent_tool_result",
            "agent_id": "agent-1",
            "tool_use_id": "tool-2",
            "output": "contents",
            "is_error": False,
        }

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.add_subagent_tool_result = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_subagent_tool_result(event, ectx, state)

            mock_store.add_subagent_tool_result.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_subagent_done(self):
        ectx = make_ctx()
        state = make_state()
        event = {
            "type": "subagent_done",
            "agent_id": "agent-1",
            "result": "success",
            "is_error": False,
        }

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store, \
             patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_store.complete_subagent = AsyncMock()
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_subagent_done(event, ectx, state)

            mock_store.complete_subagent.assert_awaited_once()


# ---------------------------------------------------------------------------
# Stream block / message delta
# ---------------------------------------------------------------------------

class TestStreamBlockAndDelta:
    @pytest.mark.asyncio
    async def test_stream_block_stop(self):
        ectx = make_ctx()
        state = make_state()
        event = {"type": "stream_block_stop", "index": 0}

        with patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_stream_block_stop(event, ectx, state)

            chunk_data = mock_sync.broadcast_stream_chunk.call_args[1]["chunk_data"]
            assert chunk_data["index"] == 0

    @pytest.mark.asyncio
    async def test_stream_message_delta(self):
        ectx = make_ctx()
        state = make_state()
        event = {
            "type": "stream_message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 50},
        }

        with patch("app.api.ws_event_handlers.sync_engine") as mock_sync:
            mock_sync.broadcast_stream_chunk = AsyncMock()

            await handle_stream_message_delta(event, ectx, state)

            chunk_data = mock_sync.broadcast_stream_chunk.call_args[1]["chunk_data"]
            assert chunk_data["delta"] == {"stop_reason": "end_turn"}
            assert chunk_data["usage"] == {"output_tokens": 50}


# ---------------------------------------------------------------------------
# handle_task_notification (background-agent unpark)
# ---------------------------------------------------------------------------

class TestHandleTaskNotification:
    @pytest.mark.asyncio
    async def test_marks_subagent_complete_by_tool_use_id(self):
        """A terminal task_notification marks the matching subagent node complete
        (by tool_use_id) and applies usage — it must NOT add an empty system card."""
        ectx = make_ctx()
        state = make_state()
        event = {
            "type": "task_notification",
            "task_id": "task-1",
            "status": "completed",
            "summary": "did the thing",
            "tool_use_id": "tu-1",
            "usage": {"total_tokens": 10, "tool_uses": 2, "duration_ms": 100},
        }

        with patch("app.api.ws_event_handlers.live_session_store") as mock_store:
            mock_store.complete_subagent_by_tool_id = AsyncMock(return_value=True)
            mock_store.add_system_message = AsyncMock()

            await handle_task_notification(event, ectx, state)

            mock_store.complete_subagent_by_tool_id.assert_awaited_once()
            call_kwargs = mock_store.complete_subagent_by_tool_id.call_args[1]
            assert call_kwargs["session_id"] == "sess-1"
            assert call_kwargs["tool_use_id"] == "tu-1"
            assert call_kwargs["is_error"] is False
            assert call_kwargs["usage"] == {"total_tokens": 10, "tool_uses": 2, "duration_ms": 100}
            # The Bug-2 artifact must never appear.
            mock_store.add_system_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_and_stopped_mark_is_error(self):
        ectx = make_ctx()
        state = make_state()
        for status in ("failed", "stopped"):
            event = {"type": "task_notification", "task_id": "t", "status": status, "tool_use_id": "tu-x"}
            with patch("app.api.ws_event_handlers.live_session_store") as mock_store:
                mock_store.complete_subagent_by_tool_id = AsyncMock(return_value=True)
                mock_store.add_system_message = AsyncMock()
                await handle_task_notification(event, ectx, state)
                assert mock_store.complete_subagent_by_tool_id.call_args[1]["is_error"] is True
                mock_store.add_system_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_tool_use_id_is_noop(self):
        ectx = make_ctx()
        state = make_state()
        event = {"type": "task_notification", "task_id": "t", "status": "completed"}
        with patch("app.api.ws_event_handlers.live_session_store") as mock_store:
            mock_store.complete_subagent_by_tool_id = AsyncMock()
            mock_store.add_system_message = AsyncMock()
            await handle_task_notification(event, ectx, state)
            mock_store.complete_subagent_by_tool_id.assert_not_called()
            mock_store.add_system_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_registered_in_dispatch_table(self):
        assert EVENT_HANDLERS.get("task_notification") is handle_task_notification
