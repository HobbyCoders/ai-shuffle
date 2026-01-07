"""
Unit tests for sync engine module.

Tests cover:
- SyncEvent creation and serialization
- DeviceConnection management and event sending
- StreamingBuffer chunk handling for all types
- SyncEngine device registration/unregistration
- Event broadcasting with device exclusion
- Streaming lifecycle (start, chunk, end)
- Session state management
- Stale connection cleanup
- Concurrent operations and error handling
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from typing import Dict, Any

from app.core.sync_engine import (
    SyncEvent,
    DeviceConnection,
    StreamingBuffer,
    SyncEngine,
    sync_engine,
)


# =============================================================================
# SyncEvent Tests
# =============================================================================

class TestSyncEvent:
    """Test SyncEvent dataclass."""

    def test_create_event_with_required_fields(self):
        """Should create event with required fields."""
        event = SyncEvent(
            event_type="message_added",
            session_id="session-123",
            data={"message": "hello"}
        )

        assert event.event_type == "message_added"
        assert event.session_id == "session-123"
        assert event.data == {"message": "hello"}
        assert event.source_device_id is None
        assert isinstance(event.timestamp, datetime)

    def test_create_event_with_all_fields(self):
        """Should create event with all fields."""
        ts = datetime(2024, 1, 15, 12, 0, 0)
        event = SyncEvent(
            event_type="stream_chunk",
            session_id="session-456",
            data={"content": "chunk data"},
            timestamp=ts,
            source_device_id="device-abc"
        )

        assert event.event_type == "stream_chunk"
        assert event.session_id == "session-456"
        assert event.data == {"content": "chunk data"}
        assert event.timestamp == ts
        assert event.source_device_id == "device-abc"

    def test_to_dict_serialization(self):
        """Should serialize event to dictionary."""
        ts = datetime(2024, 1, 15, 12, 30, 45)
        event = SyncEvent(
            event_type="session_updated",
            session_id="session-789",
            data={"title": "New Title"},
            timestamp=ts,
            source_device_id="device-xyz"
        )

        result = event.to_dict()

        assert result["event_type"] == "session_updated"
        assert result["session_id"] == "session-789"
        assert result["data"] == {"title": "New Title"}
        assert result["timestamp"] == "2024-01-15T12:30:45"
        assert result["source_device_id"] == "device-xyz"

    def test_to_dict_without_source_device(self):
        """Should serialize event without source device."""
        event = SyncEvent(
            event_type="stream_start",
            session_id="session-001",
            data={"message_id": "msg-1"}
        )

        result = event.to_dict()

        assert result["source_device_id"] is None


# =============================================================================
# DeviceConnection Tests
# =============================================================================

class TestDeviceConnection:
    """Test DeviceConnection dataclass."""

    def test_create_connection(self):
        """Should create connection with all fields."""
        mock_ws = MagicMock()
        conn = DeviceConnection(
            device_id="device-123",
            session_id="session-456",
            websocket=mock_ws
        )

        assert conn.device_id == "device-123"
        assert conn.session_id == "session-456"
        assert conn.websocket == mock_ws
        assert isinstance(conn.connected_at, datetime)
        assert isinstance(conn.last_activity, datetime)

    @pytest.mark.asyncio
    async def test_send_event_success(self):
        """Should send event and update last_activity."""
        mock_ws = AsyncMock()
        conn = DeviceConnection(
            device_id="device-123",
            session_id="session-456",
            websocket=mock_ws
        )

        old_activity = conn.last_activity
        await asyncio.sleep(0.01)  # Ensure time difference

        event = SyncEvent(
            event_type="message_added",
            session_id="session-456",
            data={"content": "test"}
        )

        result = await conn.send_event(event)

        assert result is True
        assert conn.last_activity > old_activity
        mock_ws.send_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_send_event_failure(self):
        """Should return False on send failure."""
        mock_ws = AsyncMock()
        mock_ws.send_json.side_effect = Exception("Connection closed")

        conn = DeviceConnection(
            device_id="device-123",
            session_id="session-456",
            websocket=mock_ws
        )

        event = SyncEvent(
            event_type="message_added",
            session_id="session-456",
            data={"content": "test"}
        )

        result = await conn.send_event(event)

        assert result is False


# =============================================================================
# StreamingBuffer Tests
# =============================================================================

class TestStreamingBuffer:
    """Test StreamingBuffer dataclass."""

    def test_create_buffer(self):
        """Should create buffer with empty messages."""
        buffer = StreamingBuffer(session_id="session-123")

        assert buffer.session_id == "session-123"
        assert buffer.messages == []
        assert isinstance(buffer.started_at, datetime)

    def test_add_text_chunk_creates_new_message(self):
        """Should create new text message for first chunk."""
        buffer = StreamingBuffer(session_id="session-123")

        buffer.add_chunk(chunk_type="text", content="Hello ")

        messages = buffer.get_messages()
        assert len(messages) == 1
        assert messages[0]["type"] == "text"
        assert messages[0]["role"] == "assistant"
        assert messages[0]["content"] == "Hello "
        assert messages[0]["streaming"] is True

    def test_add_text_chunk_appends_to_existing(self):
        """Should append content to existing streaming text message."""
        buffer = StreamingBuffer(session_id="session-123")

        buffer.add_chunk(chunk_type="text", content="Hello ")
        buffer.add_chunk(chunk_type="text", content="World!")

        messages = buffer.get_messages()
        assert len(messages) == 1
        assert messages[0]["content"] == "Hello World!"

    def test_add_tool_use_chunk(self):
        """Should add tool use message."""
        buffer = StreamingBuffer(session_id="session-123")

        buffer.add_chunk(
            chunk_type="tool_use",
            content="Running search",
            tool_name="web_search",
            tool_id="tool-001",
            tool_input={"query": "test"}
        )

        messages = buffer.get_messages()
        assert len(messages) == 1
        assert messages[0]["type"] == "tool_use"
        assert messages[0]["tool_name"] == "web_search"
        assert messages[0]["tool_id"] == "tool-001"
        assert messages[0]["tool_input"] == {"query": "test"}
        assert messages[0]["streaming"] is True

    def test_add_tool_use_marks_text_complete(self):
        """Should mark previous text messages as not streaming."""
        buffer = StreamingBuffer(session_id="session-123")

        buffer.add_chunk(chunk_type="text", content="I'll search for that...")
        buffer.add_chunk(
            chunk_type="tool_use",
            content="Searching",
            tool_name="search",
            tool_id="tool-002"
        )

        messages = buffer.get_messages()
        assert len(messages) == 2
        assert messages[0]["type"] == "text"
        assert messages[0]["streaming"] is False
        assert messages[1]["type"] == "tool_use"
        assert messages[1]["streaming"] is True

    def test_add_tool_result_merges_into_tool_use(self):
        """Should merge tool result into matching tool_use message."""
        buffer = StreamingBuffer(session_id="session-123")

        buffer.add_chunk(
            chunk_type="tool_use",
            content="Running",
            tool_name="calculator",
            tool_id="tool-003"
        )
        buffer.add_chunk(
            chunk_type="tool_result",
            content="Result: 42",
            tool_id="tool-003"
        )

        messages = buffer.get_messages()
        assert len(messages) == 1
        assert messages[0]["type"] == "tool_use"
        assert messages[0]["tool_result"] == "Result: 42"
        assert messages[0]["tool_status"] == "complete"
        assert messages[0]["streaming"] is False

    def test_add_tool_result_creates_separate_if_no_match(self):
        """Should create separate tool_result if no matching tool_use found."""
        buffer = StreamingBuffer(session_id="session-123")

        buffer.add_chunk(
            chunk_type="tool_result",
            content="Orphan result",
            tool_id="nonexistent-tool"
        )

        messages = buffer.get_messages()
        assert len(messages) == 1
        assert messages[0]["type"] == "tool_result"
        assert messages[0]["content"] == "Orphan result"
        assert messages[0]["streaming"] is False

    def test_add_system_chunk(self):
        """Should add system message with subtype and data."""
        buffer = StreamingBuffer(session_id="session-123")

        buffer.add_chunk(
            chunk_type="system",
            content="Context loaded",
            subtype="context_output",
            data={"files": ["test.py"]}
        )

        messages = buffer.get_messages()
        assert len(messages) == 1
        assert messages[0]["type"] == "system"
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "Context loaded"
        assert messages[0]["subtype"] == "context_output"
        assert messages[0]["data"] == {"files": ["test.py"]}
        assert messages[0]["streaming"] is False

    def test_get_messages_returns_copy(self):
        """Should return a copy of messages list."""
        buffer = StreamingBuffer(session_id="session-123")
        buffer.add_chunk(chunk_type="text", content="Test")

        messages1 = buffer.get_messages()
        messages2 = buffer.get_messages()

        assert messages1 is not messages2
        assert messages1 == messages2

    def test_clear_buffer(self):
        """Should clear all messages."""
        buffer = StreamingBuffer(session_id="session-123")
        buffer.add_chunk(chunk_type="text", content="Test")
        buffer.add_chunk(chunk_type="system", content="Info")

        buffer.clear()

        assert buffer.get_messages() == []


# =============================================================================
# SyncEngine Tests
# =============================================================================

class TestSyncEngineInit:
    """Test SyncEngine initialization."""

    def test_init_creates_empty_state(self):
        """Should initialize with empty state."""
        engine = SyncEngine()

        assert engine._connections == {}
        assert engine._streaming_sessions == set()
        assert engine._streaming_buffers == {}

    def test_global_instance_exists(self):
        """Should have a global sync_engine instance."""
        assert sync_engine is not None
        assert isinstance(sync_engine, SyncEngine)


class TestSyncEngineDeviceRegistration:
    """Test device registration and unregistration."""

    @pytest.mark.asyncio
    async def test_register_device(self):
        """Should register device to watch session."""
        engine = SyncEngine()
        mock_ws = AsyncMock()

        conn = await engine.register_device(
            device_id="device-123",
            session_id="session-456",
            websocket=mock_ws
        )

        assert conn.device_id == "device-123"
        assert conn.session_id == "session-456"
        assert conn.websocket == mock_ws
        assert "session-456" in engine._connections
        assert "device-123" in engine._connections["session-456"]

    @pytest.mark.asyncio
    async def test_register_device_closes_existing_connection(self):
        """Should close existing connection when registering same device."""
        engine = SyncEngine()
        old_ws = AsyncMock()
        new_ws = AsyncMock()

        await engine.register_device("device-123", "session-456", old_ws)
        await engine.register_device("device-123", "session-456", new_ws)

        old_ws.close.assert_called_once()
        conn = engine._connections["session-456"]["device-123"]
        assert conn.websocket == new_ws

    @pytest.mark.asyncio
    async def test_register_device_handles_close_exception(self):
        """Should handle exception when closing old websocket."""
        engine = SyncEngine()
        old_ws = AsyncMock()
        old_ws.close.side_effect = Exception("Already closed")
        new_ws = AsyncMock()

        await engine.register_device("device-123", "session-456", old_ws)
        # Should not raise even if close fails
        conn = await engine.register_device("device-123", "session-456", new_ws)

        assert conn.websocket == new_ws

    @pytest.mark.asyncio
    async def test_register_multiple_devices_same_session(self):
        """Should allow multiple devices to watch same session."""
        engine = SyncEngine()

        await engine.register_device("device-1", "session-1", AsyncMock())
        await engine.register_device("device-2", "session-1", AsyncMock())
        await engine.register_device("device-3", "session-1", AsyncMock())

        assert len(engine._connections["session-1"]) == 3

    @pytest.mark.asyncio
    async def test_unregister_device(self):
        """Should unregister device from session."""
        engine = SyncEngine()
        mock_ws = AsyncMock()

        await engine.register_device("device-123", "session-456", mock_ws)
        await engine.unregister_device("device-123", "session-456")

        assert "device-123" not in engine._connections.get("session-456", {})

    @pytest.mark.asyncio
    async def test_unregister_device_cleans_empty_session(self):
        """Should remove session entry when last device unregisters."""
        engine = SyncEngine()

        await engine.register_device("device-123", "session-456", AsyncMock())
        await engine.unregister_device("device-123", "session-456")

        assert "session-456" not in engine._connections

    @pytest.mark.asyncio
    async def test_unregister_device_with_websocket_check(self):
        """Should only unregister if websocket matches."""
        engine = SyncEngine()
        ws1 = AsyncMock()
        ws2 = AsyncMock()

        await engine.register_device("device-123", "session-456", ws1)
        # Register new connection
        await engine.register_device("device-123", "session-456", ws2)

        # Try to unregister old websocket - should be skipped
        await engine.unregister_device("device-123", "session-456", websocket=ws1)

        # Device should still be connected with ws2
        assert "device-123" in engine._connections["session-456"]
        assert engine._connections["session-456"]["device-123"].websocket == ws2

    @pytest.mark.asyncio
    async def test_unregister_nonexistent_device(self):
        """Should handle unregistering nonexistent device gracefully."""
        engine = SyncEngine()

        # Should not raise
        await engine.unregister_device("device-999", "session-999")

    @pytest.mark.asyncio
    async def test_unregister_from_nonexistent_session(self):
        """Should handle unregistering from nonexistent session gracefully."""
        engine = SyncEngine()
        await engine.register_device("device-123", "session-456", AsyncMock())

        # Should not raise
        await engine.unregister_device("device-123", "session-nonexistent")


class TestSyncEngineDeviceQueries:
    """Test device query methods."""

    @pytest.mark.asyncio
    async def test_get_connected_devices(self):
        """Should return list of connected device IDs."""
        engine = SyncEngine()

        await engine.register_device("device-1", "session-1", AsyncMock())
        await engine.register_device("device-2", "session-1", AsyncMock())

        devices = engine.get_connected_devices("session-1")

        assert set(devices) == {"device-1", "device-2"}

    def test_get_connected_devices_empty_session(self):
        """Should return empty list for session with no devices."""
        engine = SyncEngine()

        devices = engine.get_connected_devices("nonexistent-session")

        assert devices == []

    @pytest.mark.asyncio
    async def test_get_device_count(self):
        """Should return correct device count."""
        engine = SyncEngine()

        await engine.register_device("device-1", "session-1", AsyncMock())
        await engine.register_device("device-2", "session-1", AsyncMock())

        count = engine.get_device_count("session-1")

        assert count == 2

    def test_get_device_count_empty_session(self):
        """Should return 0 for session with no devices."""
        engine = SyncEngine()

        count = engine.get_device_count("nonexistent-session")

        assert count == 0


class TestSyncEngineStreaming:
    """Test streaming state management."""

    def test_is_session_streaming_false_initially(self):
        """Should return False for non-streaming session."""
        engine = SyncEngine()

        assert engine.is_session_streaming("session-123") is False

    @pytest.mark.asyncio
    async def test_stream_start_sets_streaming_state(self):
        """Should mark session as streaming on stream_start."""
        engine = SyncEngine()
        await engine.register_device("device-1", "session-123", AsyncMock())

        await engine.broadcast_stream_start(
            session_id="session-123",
            message_id="msg-001"
        )

        assert engine.is_session_streaming("session-123") is True

    @pytest.mark.asyncio
    async def test_stream_start_creates_buffer(self):
        """Should create streaming buffer on stream_start."""
        engine = SyncEngine()
        await engine.register_device("device-1", "session-123", AsyncMock())

        await engine.broadcast_stream_start(
            session_id="session-123",
            message_id="msg-001"
        )

        assert "session-123" in engine._streaming_buffers

    @pytest.mark.asyncio
    async def test_stream_start_idempotent(self):
        """Should not reset buffer on repeated stream_start calls."""
        engine = SyncEngine()
        await engine.register_device("device-1", "session-123", AsyncMock())

        await engine.broadcast_stream_start("session-123", "msg-001")
        original_buffer = engine._streaming_buffers["session-123"]
        original_buffer.add_chunk("text", "First chunk")

        await engine.broadcast_stream_start("session-123", "msg-001")

        # Buffer should be the same instance with content preserved
        assert engine._streaming_buffers["session-123"] is original_buffer
        assert len(original_buffer.get_messages()) == 1

    @pytest.mark.asyncio
    async def test_stream_start_with_usage(self):
        """Should include usage data in stream_start event."""
        engine = SyncEngine()
        mock_ws = AsyncMock()
        await engine.register_device("device-1", "session-123", mock_ws)

        await engine.broadcast_stream_start(
            session_id="session-123",
            message_id="msg-001",
            usage={"input_tokens": 100, "cache_read_input_tokens": 50}
        )

        call_args = mock_ws.send_json.call_args[0][0]
        assert call_args["data"]["usage"] == {"input_tokens": 100, "cache_read_input_tokens": 50}

    @pytest.mark.asyncio
    async def test_stream_end_clears_streaming_state(self):
        """Should clear streaming state on stream_end."""
        engine = SyncEngine()
        await engine.register_device("device-1", "session-123", AsyncMock())

        await engine.broadcast_stream_start("session-123", "msg-001")
        await engine.broadcast_stream_end("session-123", "msg-001")

        assert engine.is_session_streaming("session-123") is False

    @pytest.mark.asyncio
    async def test_stream_end_clears_buffer(self):
        """Should clear streaming buffer on stream_end."""
        engine = SyncEngine()
        await engine.register_device("device-1", "session-123", AsyncMock())

        await engine.broadcast_stream_start("session-123", "msg-001")
        await engine.broadcast_stream_chunk(
            "session-123", "msg-001", "text", {"content": "Hello"}
        )
        await engine.broadcast_stream_end("session-123", "msg-001")

        assert engine.get_streaming_buffer("session-123") is None

    @pytest.mark.asyncio
    async def test_stream_end_with_metadata(self):
        """Should include metadata in stream_end event."""
        engine = SyncEngine()
        mock_ws = AsyncMock()
        await engine.register_device("device-1", "session-123", mock_ws)

        await engine.broadcast_stream_start("session-123", "msg-001")
        await engine.broadcast_stream_end(
            session_id="session-123",
            message_id="msg-001",
            metadata={"tokens": 500},
            interrupted=True
        )

        # Get last call (stream_end)
        call_args = mock_ws.send_json.call_args_list[-1][0][0]
        assert call_args["event_type"] == "stream_end"
        assert call_args["data"]["metadata"] == {"tokens": 500}
        assert call_args["data"]["interrupted"] is True

    @pytest.mark.asyncio
    async def test_stream_chunk_adds_to_buffer(self):
        """Should add chunk to streaming buffer."""
        engine = SyncEngine()
        await engine.register_device("device-1", "session-123", AsyncMock())

        await engine.broadcast_stream_start("session-123", "msg-001")
        await engine.broadcast_stream_chunk(
            session_id="session-123",
            message_id="msg-001",
            chunk_type="text",
            chunk_data={"content": "Hello World"}
        )

        buffer = engine.get_streaming_buffer("session-123")
        assert buffer is not None
        assert len(buffer) == 1
        assert buffer[0]["content"] == "Hello World"

    @pytest.mark.asyncio
    async def test_stream_chunk_broadcasts_event(self):
        """Should broadcast chunk event to devices."""
        engine = SyncEngine()
        mock_ws = AsyncMock()
        await engine.register_device("device-1", "session-123", mock_ws)

        await engine.broadcast_stream_start("session-123", "msg-001")
        await engine.broadcast_stream_chunk(
            session_id="session-123",
            message_id="msg-001",
            chunk_type="tool_use",
            chunk_data={
                "content": "Running",
                "tool_name": "search",
                "tool_id": "t-001"
            }
        )

        # Find the stream_chunk call
        chunk_call = None
        for call in mock_ws.send_json.call_args_list:
            if call[0][0].get("event_type") == "stream_chunk":
                chunk_call = call[0][0]
                break

        assert chunk_call is not None
        assert chunk_call["data"]["chunk_type"] == "tool_use"
        assert chunk_call["data"]["tool_name"] == "search"

    def test_get_streaming_buffer_returns_none_when_not_streaming(self):
        """Should return None for non-streaming session."""
        engine = SyncEngine()

        buffer = engine.get_streaming_buffer("nonexistent-session")

        assert buffer is None


class TestSyncEngineBroadcast:
    """Test event broadcasting."""

    @pytest.mark.asyncio
    async def test_broadcast_event_to_all_devices(self):
        """Should broadcast event to all devices in session."""
        engine = SyncEngine()
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        ws3 = AsyncMock()

        await engine.register_device("device-1", "session-1", ws1)
        await engine.register_device("device-2", "session-1", ws2)
        await engine.register_device("device-3", "session-1", ws3)

        event = SyncEvent(
            event_type="message_added",
            session_id="session-1",
            data={"message": "test"}
        )

        await engine.broadcast_event(event)

        ws1.send_json.assert_called_once()
        ws2.send_json.assert_called_once()
        ws3.send_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_event_excludes_source_device(self):
        """Should exclude source device from broadcast."""
        engine = SyncEngine()
        ws1 = AsyncMock()
        ws2 = AsyncMock()

        await engine.register_device("device-1", "session-1", ws1)
        await engine.register_device("device-2", "session-1", ws2)

        event = SyncEvent(
            event_type="message_added",
            session_id="session-1",
            data={"message": "test"}
        )

        await engine.broadcast_event(event, exclude_device_id="device-1")

        ws1.send_json.assert_not_called()
        ws2.send_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_broadcast_event_cleans_failed_connections(self):
        """Should remove devices that fail to receive events."""
        engine = SyncEngine()
        ws_good = AsyncMock()
        ws_bad = AsyncMock()
        ws_bad.send_json.side_effect = Exception("Connection lost")

        await engine.register_device("device-good", "session-1", ws_good)
        await engine.register_device("device-bad", "session-1", ws_bad)

        event = SyncEvent(
            event_type="message_added",
            session_id="session-1",
            data={"message": "test"}
        )

        await engine.broadcast_event(event)

        assert "device-good" in engine._connections["session-1"]
        assert "device-bad" not in engine._connections["session-1"]

    @pytest.mark.asyncio
    async def test_broadcast_event_no_connections(self):
        """Should handle broadcast with no connected devices."""
        engine = SyncEngine()

        event = SyncEvent(
            event_type="message_added",
            session_id="session-nonexistent",
            data={"message": "test"}
        )

        # Should not raise
        await engine.broadcast_event(event)

    @pytest.mark.asyncio
    async def test_broadcast_message_added(self):
        """Should broadcast message_added event."""
        engine = SyncEngine()
        mock_ws = AsyncMock()
        await engine.register_device("device-1", "session-1", mock_ws)

        await engine.broadcast_message_added(
            session_id="session-1",
            message={"role": "user", "content": "Hello"},
            source_device_id="device-2"
        )

        call_args = mock_ws.send_json.call_args[0][0]
        assert call_args["event_type"] == "message_added"
        assert call_args["data"]["message"]["content"] == "Hello"

    @pytest.mark.asyncio
    async def test_broadcast_session_updated(self):
        """Should broadcast session_updated event."""
        engine = SyncEngine()
        mock_ws = AsyncMock()
        await engine.register_device("device-1", "session-1", mock_ws)

        await engine.broadcast_session_updated(
            session_id="session-1",
            updates={"title": "New Title"}
        )

        call_args = mock_ws.send_json.call_args[0][0]
        assert call_args["event_type"] == "session_updated"
        assert call_args["data"]["updates"]["title"] == "New Title"

    @pytest.mark.asyncio
    async def test_broadcast_session_rewound(self):
        """Should broadcast session_rewound event to ALL devices."""
        engine = SyncEngine()
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        await engine.register_device("device-1", "session-1", ws1)
        await engine.register_device("device-2", "session-1", ws2)

        await engine.broadcast_session_rewound(
            session_id="session-1",
            target_uuid="msg-123",
            messages_removed=5,
            source_device_id="device-1"
        )

        # Both devices should receive rewind event (even source)
        ws1.send_json.assert_called_once()
        ws2.send_json.assert_called_once()

        call_args = ws1.send_json.call_args[0][0]
        assert call_args["event_type"] == "session_rewound"
        assert call_args["data"]["messages_removed"] == 5

    @pytest.mark.asyncio
    async def test_broadcast_session_opened(self):
        """Should broadcast session_opened event."""
        engine = SyncEngine()
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        await engine.register_device("device-1", "session-1", ws1)
        await engine.register_device("device-2", "session-1", ws2)

        await engine.broadcast_session_opened(
            session_id="session-1",
            device_id="device-1",
            is_new=True
        )

        # Source device should be excluded
        ws1.send_json.assert_not_called()
        ws2.send_json.assert_called_once()

        call_args = ws2.send_json.call_args[0][0]
        assert call_args["event_type"] == "session_opened"
        assert call_args["data"]["is_new"] is True

    @pytest.mark.asyncio
    async def test_broadcast_session_closed(self):
        """Should broadcast session_closed event."""
        engine = SyncEngine()
        ws1 = AsyncMock()
        ws2 = AsyncMock()
        await engine.register_device("device-1", "session-1", ws1)
        await engine.register_device("device-2", "session-1", ws2)

        await engine.broadcast_session_closed(
            session_id="session-1",
            device_id="device-1"
        )

        # Source device should be excluded
        ws1.send_json.assert_not_called()
        ws2.send_json.assert_called_once()

        call_args = ws2.send_json.call_args[0][0]
        assert call_args["event_type"] == "session_closed"


class TestSyncEngineSessionState:
    """Test session state management."""

    @pytest.mark.asyncio
    async def test_get_session_state_not_streaming(self):
        """Should return session state when not streaming."""
        engine = SyncEngine()
        await engine.register_device("device-1", "session-1", AsyncMock())
        await engine.register_device("device-2", "session-1", AsyncMock())

        state = await engine.get_session_state("session-1")

        assert state["session_id"] == "session-1"
        assert state["is_streaming"] is False
        assert state["connected_devices"] == 2
        assert "streaming_messages" not in state

    @pytest.mark.asyncio
    async def test_get_session_state_streaming(self):
        """Should include streaming buffer when streaming."""
        engine = SyncEngine()
        await engine.register_device("device-1", "session-1", AsyncMock())

        await engine.broadcast_stream_start("session-1", "msg-001")
        await engine.broadcast_stream_chunk(
            "session-1", "msg-001", "text", {"content": "Hello"}
        )

        state = await engine.get_session_state("session-1")

        assert state["is_streaming"] is True
        assert "streaming_messages" in state
        assert len(state["streaming_messages"]) == 1

    @pytest.mark.asyncio
    async def test_get_session_state_empty_session(self):
        """Should return state for non-existent session."""
        engine = SyncEngine()

        state = await engine.get_session_state("nonexistent-session")

        assert state["session_id"] == "nonexistent-session"
        assert state["is_streaming"] is False
        assert state["connected_devices"] == 0


class TestSyncEngineCleanup:
    """Test stale connection cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_stale_connections(self):
        """Should remove stale connections."""
        engine = SyncEngine()

        # Register a device
        await engine.register_device("device-stale", "session-1", AsyncMock())

        # Manually set last_activity to be old
        conn = engine._connections["session-1"]["device-stale"]
        conn.last_activity = datetime.utcnow() - timedelta(seconds=600)

        # Also register a fresh device
        await engine.register_device("device-fresh", "session-1", AsyncMock())

        await engine.cleanup_stale_connections(max_age_seconds=300)

        assert "device-stale" not in engine._connections.get("session-1", {})
        assert "device-fresh" in engine._connections["session-1"]

    @pytest.mark.asyncio
    async def test_cleanup_removes_empty_sessions(self):
        """Should remove session when all devices are stale."""
        engine = SyncEngine()

        await engine.register_device("device-stale", "session-1", AsyncMock())
        conn = engine._connections["session-1"]["device-stale"]
        conn.last_activity = datetime.utcnow() - timedelta(seconds=600)

        await engine.cleanup_stale_connections(max_age_seconds=300)

        assert "session-1" not in engine._connections

    @pytest.mark.asyncio
    async def test_cleanup_respects_max_age(self):
        """Should respect max_age_seconds parameter."""
        engine = SyncEngine()

        await engine.register_device("device-1", "session-1", AsyncMock())
        conn = engine._connections["session-1"]["device-1"]
        conn.last_activity = datetime.utcnow() - timedelta(seconds=100)

        # With max_age=200, this connection should NOT be cleaned
        await engine.cleanup_stale_connections(max_age_seconds=200)

        assert "device-1" in engine._connections["session-1"]

        # With max_age=50, it SHOULD be cleaned
        await engine.cleanup_stale_connections(max_age_seconds=50)

        assert "device-1" not in engine._connections.get("session-1", {})


class TestSyncEngineConcurrency:
    """Test concurrent operations."""

    @pytest.mark.asyncio
    async def test_concurrent_registrations(self):
        """Should handle concurrent device registrations."""
        engine = SyncEngine()

        async def register_device(device_num: int):
            await engine.register_device(
                f"device-{device_num}",
                "session-1",
                AsyncMock()
            )

        # Register 10 devices concurrently
        await asyncio.gather(*[register_device(i) for i in range(10)])

        assert engine.get_device_count("session-1") == 10

    @pytest.mark.asyncio
    async def test_concurrent_broadcasts(self):
        """Should handle concurrent broadcasts."""
        engine = SyncEngine()
        mock_ws = AsyncMock()

        await engine.register_device("device-1", "session-1", mock_ws)

        async def broadcast_message(msg_num: int):
            await engine.broadcast_message_added(
                "session-1",
                {"content": f"Message {msg_num}"}
            )

        # Broadcast 10 messages concurrently
        await asyncio.gather(*[broadcast_message(i) for i in range(10)])

        assert mock_ws.send_json.call_count == 10

    @pytest.mark.asyncio
    async def test_concurrent_register_unregister(self):
        """Should handle concurrent registration and unregistration."""
        engine = SyncEngine()

        async def register_then_unregister(device_num: int):
            ws = AsyncMock()
            await engine.register_device(f"device-{device_num}", "session-1", ws)
            await asyncio.sleep(0.001)  # Small delay
            await engine.unregister_device(f"device-{device_num}", "session-1")

        # Perform concurrent register/unregister operations
        await asyncio.gather(*[register_then_unregister(i) for i in range(10)])

        # All devices should be unregistered
        assert engine.get_device_count("session-1") == 0


class TestSyncEngineStreamingWorkflow:
    """Test complete streaming workflows."""

    @pytest.mark.asyncio
    async def test_complete_text_streaming_workflow(self):
        """Should handle complete text streaming workflow."""
        engine = SyncEngine()
        mock_ws = AsyncMock()
        await engine.register_device("device-1", "session-1", mock_ws)

        # Start streaming
        await engine.broadcast_stream_start("session-1", "msg-001")
        assert engine.is_session_streaming("session-1") is True

        # Send text chunks
        for i, chunk in enumerate(["Hello ", "World", "!"]):
            await engine.broadcast_stream_chunk(
                "session-1", "msg-001", "text",
                {"content": chunk}
            )

        # Check buffer accumulated correctly
        buffer = engine.get_streaming_buffer("session-1")
        assert buffer[0]["content"] == "Hello World!"

        # End streaming
        await engine.broadcast_stream_end("session-1", "msg-001")
        assert engine.is_session_streaming("session-1") is False
        assert engine.get_streaming_buffer("session-1") is None

    @pytest.mark.asyncio
    async def test_complete_tool_use_workflow(self):
        """Should handle complete tool use streaming workflow."""
        engine = SyncEngine()
        mock_ws = AsyncMock()
        await engine.register_device("device-1", "session-1", mock_ws)

        await engine.broadcast_stream_start("session-1", "msg-001")

        # Text before tool
        await engine.broadcast_stream_chunk(
            "session-1", "msg-001", "text",
            {"content": "Let me search for that..."}
        )

        # Tool use
        await engine.broadcast_stream_chunk(
            "session-1", "msg-001", "tool_use",
            {"content": "Searching", "tool_name": "web_search", "tool_id": "t-001"}
        )

        # Tool result
        await engine.broadcast_stream_chunk(
            "session-1", "msg-001", "tool_result",
            {"content": "Found 3 results", "tool_id": "t-001"}
        )

        buffer = engine.get_streaming_buffer("session-1")
        # Should have text message (non-streaming) and tool_use with result
        assert len(buffer) == 2
        assert buffer[0]["type"] == "text"
        assert buffer[0]["streaming"] is False
        assert buffer[1]["type"] == "tool_use"
        assert buffer[1]["tool_result"] == "Found 3 results"

        await engine.broadcast_stream_end("session-1", "msg-001")

    @pytest.mark.asyncio
    async def test_late_joining_device_gets_buffer(self):
        """Late joining device should receive streaming buffer."""
        engine = SyncEngine()
        ws1 = AsyncMock()
        ws2 = AsyncMock()

        # First device starts watching and streaming begins
        await engine.register_device("device-1", "session-1", ws1)
        await engine.broadcast_stream_start("session-1", "msg-001")
        await engine.broadcast_stream_chunk(
            "session-1", "msg-001", "text",
            {"content": "Hello from stream"}
        )

        # Second device joins late
        await engine.register_device("device-2", "session-1", ws2)

        # Get session state for late joiner
        state = await engine.get_session_state("session-1")

        assert state["is_streaming"] is True
        assert "streaming_messages" in state
        assert state["streaming_messages"][0]["content"] == "Hello from stream"


class TestSyncEngineErrorHandling:
    """Test error handling scenarios."""

    @pytest.mark.asyncio
    async def test_send_event_handles_websocket_error(self):
        """Should handle websocket send errors gracefully."""
        engine = SyncEngine()
        bad_ws = AsyncMock()
        bad_ws.send_json.side_effect = ConnectionError("Client disconnected")

        await engine.register_device("device-1", "session-1", bad_ws)

        event = SyncEvent(
            event_type="message_added",
            session_id="session-1",
            data={"content": "test"}
        )

        # Should not raise
        await engine.broadcast_event(event)

        # Device should be removed
        assert "device-1" not in engine._connections.get("session-1", {})

    @pytest.mark.asyncio
    async def test_stream_chunk_without_buffer(self):
        """Should handle stream chunk when buffer doesn't exist."""
        engine = SyncEngine()
        mock_ws = AsyncMock()
        await engine.register_device("device-1", "session-1", mock_ws)

        # Send chunk without starting stream (buffer won't exist)
        # This should not raise
        await engine.broadcast_stream_chunk(
            "session-1", "msg-001", "text",
            {"content": "Orphan chunk"}
        )

        # Event should still be broadcast
        mock_ws.send_json.assert_called_once()

    @pytest.mark.asyncio
    async def test_stream_end_without_start(self):
        """Should handle stream end without prior start."""
        engine = SyncEngine()
        mock_ws = AsyncMock()
        await engine.register_device("device-1", "session-1", mock_ws)

        # Should not raise
        await engine.broadcast_stream_end("session-1", "msg-001")

        assert engine.is_session_streaming("session-1") is False

    @pytest.mark.asyncio
    async def test_multiple_stream_ends(self):
        """Should handle multiple stream end calls gracefully."""
        engine = SyncEngine()
        mock_ws = AsyncMock()
        await engine.register_device("device-1", "session-1", mock_ws)

        await engine.broadcast_stream_start("session-1", "msg-001")
        await engine.broadcast_stream_end("session-1", "msg-001")
        await engine.broadcast_stream_end("session-1", "msg-001")
        await engine.broadcast_stream_end("session-1", "msg-001")

        # Should not raise and state should be clean
        assert engine.is_session_streaming("session-1") is False


# =============================================================================
# Integration-like Tests
# =============================================================================

class TestSyncEngineIntegration:
    """Integration-style tests for realistic scenarios."""

    @pytest.mark.asyncio
    async def test_multi_session_isolation(self):
        """Events in one session should not affect other sessions."""
        engine = SyncEngine()
        ws1 = AsyncMock()
        ws2 = AsyncMock()

        await engine.register_device("device-1", "session-1", ws1)
        await engine.register_device("device-2", "session-2", ws2)

        await engine.broadcast_message_added(
            "session-1",
            {"content": "Session 1 message"}
        )

        ws1.send_json.assert_called_once()
        ws2.send_json.assert_not_called()

    @pytest.mark.asyncio
    async def test_device_switching_sessions(self):
        """Device can switch from one session to another."""
        engine = SyncEngine()
        mock_ws = AsyncMock()

        # Start watching session 1
        await engine.register_device("device-1", "session-1", mock_ws)
        assert engine.get_device_count("session-1") == 1

        # Switch to session 2 (unregister from 1, register to 2)
        await engine.unregister_device("device-1", "session-1")
        new_ws = AsyncMock()
        await engine.register_device("device-1", "session-2", new_ws)

        assert engine.get_device_count("session-1") == 0
        assert engine.get_device_count("session-2") == 1

    @pytest.mark.asyncio
    async def test_streaming_interrupted_by_new_message(self):
        """User sending message during streaming should work."""
        engine = SyncEngine()
        ws1 = AsyncMock()
        ws2 = AsyncMock()

        await engine.register_device("device-1", "session-1", ws1)
        await engine.register_device("device-2", "session-1", ws2)

        # Start streaming
        await engine.broadcast_stream_start("session-1", "msg-001")

        # User sends a message (from device-1)
        await engine.broadcast_message_added(
            "session-1",
            {"role": "user", "content": "interrupt"},
            source_device_id="device-1"
        )

        # End streaming (interrupted)
        await engine.broadcast_stream_end(
            "session-1", "msg-001",
            interrupted=True
        )

        # Device 2 should have received: stream_start, message_added, stream_end
        assert ws2.send_json.call_count == 3
