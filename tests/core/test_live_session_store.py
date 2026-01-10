"""
Comprehensive tests for live_session_store.py

Tests cover:
- Streaming state management
- Thinking vs text message handling
- Tool use and result tracking
- Update ID tracking for polling
- Cross-device sync scenarios
- Message deduplication
- Race conditions and edge cases
- State recovery on reconnect
"""

import pytest
import asyncio
from datetime import datetime
from typing import Dict, Any, List

from app.core.live_session_store import (
    LiveSessionStore,
    LiveSession,
    LiveMessage,
    live_session_store,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def store():
    """Create a fresh LiveSessionStore for each test."""
    return LiveSessionStore()


@pytest.fixture
async def streaming_session(store):
    """Create a store with an active streaming session."""
    await store.start_streaming("session-1", initial_messages=[])
    return store


# =============================================================================
# Basic Session Management Tests
# =============================================================================

class TestSessionManagement:
    """Test basic session lifecycle."""

    @pytest.mark.asyncio
    async def test_start_streaming_creates_session(self, store):
        """Should create new session on start_streaming."""
        await store.start_streaming("session-1", initial_messages=[])

        assert store.is_streaming("session-1")
        session = store.get_session("session-1")
        assert session is not None
        assert session.session_id == "session-1"

    @pytest.mark.asyncio
    async def test_start_streaming_with_history(self, store):
        """Should initialize with history messages."""
        history = [
            {"id": "msg-1", "role": "user", "content": "Hello"},
            {"id": "msg-2", "role": "assistant", "content": "Hi!", "type": "text"}
        ]

        await store.start_streaming("session-1", initial_messages=history)

        session = store.get_session("session-1")
        assert len(session.messages) == 2
        assert session.messages[0].content == "Hello"
        assert session.messages[1].content == "Hi!"

    @pytest.mark.asyncio
    async def test_end_streaming_marks_complete(self, streaming_session):
        """Should mark session as not streaming on end."""
        await streaming_session.end_streaming("session-1")

        assert not streaming_session.is_streaming("session-1")
        # Session should still exist for polling
        session = streaming_session.get_session("session-1")
        assert session is not None

    @pytest.mark.asyncio
    async def test_end_streaming_on_nonexistent_session(self, store):
        """Should handle end_streaming for non-existent session."""
        # Should not raise
        result = await store.end_streaming("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_cleanup_removes_session(self, streaming_session):
        """Should remove session on cleanup."""
        await streaming_session.end_streaming("session-1")
        await streaming_session.cleanup_ended_session("session-1")

        session = streaming_session.get_session("session-1")
        assert session is None

    def test_is_streaming_false_for_nonexistent(self, store):
        """Should return False for non-existent session."""
        assert not store.is_streaming("nonexistent")


# =============================================================================
# Text Streaming Tests
# =============================================================================

class TestTextStreaming:
    """Test text message streaming."""

    @pytest.mark.asyncio
    async def test_append_text_creates_message(self, streaming_session):
        """Should create text message on first append."""
        await streaming_session.append_text("session-1", "Hello ")

        session = streaming_session.get_session("session-1")
        assert len(session.messages) == 1
        assert session.messages[0].type == "text"
        assert session.messages[0].content == "Hello "
        assert session.messages[0].streaming is True

    @pytest.mark.asyncio
    async def test_append_text_accumulates(self, streaming_session):
        """Should accumulate text in existing message."""
        await streaming_session.append_text("session-1", "Hello ")
        await streaming_session.append_text("session-1", "World!")

        session = streaming_session.get_session("session-1")
        assert len(session.messages) == 1
        assert session.messages[0].content == "Hello World!"

    @pytest.mark.asyncio
    async def test_append_text_on_nonexistent_session(self, store):
        """Should handle append on non-existent session."""
        # Should not raise
        await store.append_text("nonexistent", "text")


# =============================================================================
# Thinking Streaming Tests
# =============================================================================

class TestThinkingStreaming:
    """Test thinking message streaming."""

    @pytest.mark.asyncio
    async def test_append_thinking_creates_message(self, streaming_session):
        """Should create thinking message on first append."""
        await streaming_session.append_thinking("session-1", "Let me think...")

        session = streaming_session.get_session("session-1")
        assert len(session.messages) == 1
        assert session.messages[0].type == "thinking"
        assert session.messages[0].content == "Let me think..."
        assert session.messages[0].streaming is True

    @pytest.mark.asyncio
    async def test_append_thinking_accumulates(self, streaming_session):
        """Should accumulate thinking in existing message."""
        await streaming_session.append_thinking("session-1", "First ")
        await streaming_session.append_thinking("session-1", "thought")

        session = streaming_session.get_session("session-1")
        assert len(session.messages) == 1
        assert session.messages[0].content == "First thought"

    @pytest.mark.asyncio
    async def test_finalize_thinking_marks_non_streaming(self, streaming_session):
        """Should mark thinking as not streaming on finalize."""
        await streaming_session.append_thinking("session-1", "Thinking...")
        await streaming_session.finalize_thinking("session-1")

        session = streaming_session.get_session("session-1")
        assert session.messages[0].streaming is False

    @pytest.mark.asyncio
    async def test_thinking_to_text_transition(self, streaming_session):
        """Should handle transition from thinking to text correctly."""
        # Add thinking
        await streaming_session.append_thinking("session-1", "Analyzing...")
        await streaming_session.finalize_thinking("session-1")

        # Add text
        await streaming_session.append_text("session-1", "Here is my answer")

        session = streaming_session.get_session("session-1")
        assert len(session.messages) == 2
        assert session.messages[0].type == "thinking"
        assert session.messages[0].streaming is False
        assert session.messages[1].type == "text"
        assert session.messages[1].streaming is True


# =============================================================================
# Thinking and Text Mixing Prevention Tests (CRITICAL)
# =============================================================================

class TestThinkingTextSeparation:
    """Test that thinking and text messages are properly separated."""

    @pytest.mark.asyncio
    async def test_thinking_and_text_stay_separate(self, streaming_session):
        """CRITICAL: Thinking and text should be in separate messages."""
        # Simulate the stream: thinking block → text block
        await streaming_session.append_thinking("session-1", "I need to analyze this")
        await streaming_session.finalize_thinking("session-1")
        await streaming_session.append_text("session-1", "Based on my analysis")

        session = streaming_session.get_session("session-1")

        # Verify separation
        assert len(session.messages) == 2
        thinking_msg = session.messages[0]
        text_msg = session.messages[1]

        assert thinking_msg.type == "thinking"
        assert thinking_msg.content == "I need to analyze this"
        assert text_msg.type == "text"
        assert text_msg.content == "Based on my analysis"

    @pytest.mark.asyncio
    async def test_rapid_block_transitions(self, streaming_session):
        """Should handle rapid thinking → text → thinking → text transitions."""
        # Block 1: Thinking
        await streaming_session.append_thinking("session-1", "T1")
        await streaming_session.finalize_thinking("session-1")

        # Block 2: Text - will be finalized by tool_use
        await streaming_session.append_text("session-1", "X1")

        # Block 3: Tool use (automatically finalizes text)
        await streaming_session.add_tool_use("session-1", "tool-1", "Read", {})

        # Block 4: More text after tool
        await streaming_session.add_tool_result("session-1", "tool-1", "result", False)
        await streaming_session.append_text("session-1", "X2")

        session = streaming_session.get_session("session-1")
        # Should have: thinking, text, tool_use, text
        assert len(session.messages) >= 3

        # Find messages by type
        thinking_msgs = [m for m in session.messages if m.type == "thinking"]
        text_msgs = [m for m in session.messages if m.type == "text"]
        tool_msgs = [m for m in session.messages if m.type == "tool_use"]

        assert len(thinking_msgs) >= 1
        assert thinking_msgs[0].content == "T1"
        assert len(text_msgs) >= 1
        assert len(tool_msgs) == 1

    @pytest.mark.asyncio
    async def test_concurrent_append_operations(self, streaming_session):
        """Should handle concurrent appends without mixing."""
        # Simulate interleaved operations (as might happen with race conditions)
        await streaming_session.append_thinking("session-1", "T")
        await streaming_session.append_thinking("session-1", "1")
        await streaming_session.finalize_thinking("session-1")
        await streaming_session.append_text("session-1", "X")
        await streaming_session.append_text("session-1", "1")

        session = streaming_session.get_session("session-1")

        # Each type should only contain its own content
        thinking_msg = next((m for m in session.messages if m.type == "thinking"), None)
        text_msg = next((m for m in session.messages if m.type == "text"), None)

        assert thinking_msg is not None
        assert text_msg is not None
        assert thinking_msg.content == "T1"
        assert text_msg.content == "X1"


# =============================================================================
# Tool Use and Result Tests
# =============================================================================

class TestToolHandling:
    """Test tool use and result message handling."""

    @pytest.mark.asyncio
    async def test_add_tool_use_creates_message(self, streaming_session):
        """Should create tool_use message."""
        await streaming_session.add_tool_use(
            "session-1",
            tool_id="tool-123",
            name="Read",
            tool_input={"file_path": "/test.py"}
        )

        session = streaming_session.get_session("session-1")
        assert len(session.messages) == 1
        msg = session.messages[0]
        assert msg.type == "tool_use"
        assert msg.tool_id == "tool-123"
        assert msg.tool_name == "Read"
        assert msg.tool_input == {"file_path": "/test.py"}
        assert msg.tool_status == "running"

    @pytest.mark.asyncio
    async def test_add_tool_result_merges_with_tool_use(self, streaming_session):
        """Should merge tool result into matching tool_use."""
        await streaming_session.add_tool_use(
            "session-1",
            tool_id="tool-123",
            name="Read",
            tool_input={"file_path": "/test.py"}
        )
        await streaming_session.add_tool_result(
            "session-1",
            tool_id="tool-123",
            result="file contents here",
            is_error=False
        )

        session = streaming_session.get_session("session-1")
        assert len(session.messages) == 1  # Should merge, not create new
        msg = session.messages[0]
        assert msg.tool_result == "file contents here"
        assert msg.tool_status == "complete"
        assert msg.streaming is False

    @pytest.mark.asyncio
    async def test_tool_result_error_status(self, streaming_session):
        """Should mark tool as error when is_error=True."""
        await streaming_session.add_tool_use(
            "session-1",
            tool_id="tool-123",
            name="Bash",
            tool_input={"command": "invalid"}
        )
        await streaming_session.add_tool_result(
            "session-1",
            tool_id="tool-123",
            result="Command failed",
            is_error=True
        )

        session = streaming_session.get_session("session-1")
        msg = session.messages[0]
        assert msg.tool_status == "error"

    @pytest.mark.asyncio
    async def test_update_tool_input_streaming(self, streaming_session):
        """Should append to partial tool input during streaming."""
        await streaming_session.add_tool_use(
            "session-1",
            tool_id="tool-123",
            name="Write",
            tool_input={}
        )
        await streaming_session.update_tool_input_streaming("session-1", "tool-123", '{"file')
        await streaming_session.update_tool_input_streaming("session-1", "tool-123", '": "/test.py"}')

        session = streaming_session.get_session("session-1")
        msg = session.messages[0]
        assert msg.content == '{"file": "/test.py"}'


# =============================================================================
# Update ID Tracking Tests (for polling)
# =============================================================================

class TestUpdateIdTracking:
    """Test update ID tracking for efficient polling."""

    @pytest.mark.asyncio
    async def test_update_id_increments_on_text_append(self, streaming_session):
        """Update ID should increment on text changes."""
        session = streaming_session.get_session("session-1")
        initial_id = session.last_update_id

        await streaming_session.append_text("session-1", "text")

        session = streaming_session.get_session("session-1")
        assert session.last_update_id > initial_id

    @pytest.mark.asyncio
    async def test_update_id_increments_on_thinking_append(self, streaming_session):
        """Update ID should increment on thinking changes."""
        session = streaming_session.get_session("session-1")
        initial_id = session.last_update_id

        await streaming_session.append_thinking("session-1", "thinking")

        session = streaming_session.get_session("session-1")
        assert session.last_update_id > initial_id

    @pytest.mark.asyncio
    async def test_update_id_increments_on_tool_use(self, streaming_session):
        """Update ID should increment on tool use."""
        session = streaming_session.get_session("session-1")
        initial_id = session.last_update_id

        await streaming_session.add_tool_use(
            "session-1",
            tool_id="tool-1",
            name="Read",
            tool_input={}
        )

        session = streaming_session.get_session("session-1")
        assert session.last_update_id > initial_id

    @pytest.mark.asyncio
    async def test_get_session_since_returns_state(self, streaming_session):
        """Should return state when update_id changed."""
        await streaming_session.append_text("session-1", "test")
        session = streaming_session.get_session("session-1")
        current_id = session.last_update_id

        # Make a change
        await streaming_session.append_text("session-1", " more")

        state = streaming_session.get_session_since("session-1", current_id)
        assert state is not None
        assert state["last_update_id"] > current_id

    @pytest.mark.asyncio
    async def test_get_session_since_returns_none(self, streaming_session):
        """Should return None when no changes since update_id."""
        await streaming_session.append_text("session-1", "test")
        session = streaming_session.get_session("session-1")
        current_id = session.last_update_id

        # No changes
        state = streaming_session.get_session_since("session-1", current_id)
        assert state is None


# =============================================================================
# State Serialization Tests (for API responses)
# =============================================================================

class TestStateSerialization:
    """Test state serialization for API/WebSocket responses."""

    @pytest.mark.asyncio
    async def test_get_session_dict_returns_all_messages(self, streaming_session):
        """Should return all messages in order."""
        await streaming_session.append_thinking("session-1", "Thinking...")
        await streaming_session.finalize_thinking("session-1")
        await streaming_session.append_text("session-1", "Response")
        await streaming_session.add_tool_use(
            "session-1",
            tool_id="tool-1",
            name="Read",
            tool_input={"file": "test.py"}
        )

        state = streaming_session.get_session_dict("session-1")

        assert state is not None
        assert len(state["messages"]) == 3
        assert state["is_streaming"] is True
        assert "last_update_id" in state

    @pytest.mark.asyncio
    async def test_get_session_dict_includes_usage(self, streaming_session):
        """Should include token usage in state."""
        await streaming_session.update_usage(
            "session-1",
            input_tokens=100,
            output_tokens=50,
            cache_creation_tokens=25,
            cache_read_tokens=10
        )

        state = streaming_session.get_session_dict("session-1")

        assert state["usage"]["input_tokens"] == 100
        assert state["usage"]["output_tokens"] == 50
        assert state["usage"]["cache_creation_tokens"] == 25
        assert state["usage"]["cache_read_tokens"] == 10

    @pytest.mark.asyncio
    async def test_message_serialization(self, streaming_session):
        """Should serialize messages correctly."""
        await streaming_session.append_text("session-1", "Hello")
        await streaming_session.add_tool_use(
            "session-1",
            tool_id="tool-1",
            name="Bash",
            tool_input={"command": "ls"}
        )

        state = streaming_session.get_session_dict("session-1")
        messages = state["messages"]

        text_msg = messages[0]
        assert text_msg["type"] == "text"
        assert text_msg["role"] == "assistant"
        assert text_msg["content"] == "Hello"
        assert text_msg["streaming"] is False  # Finalized by tool_use

        tool_msg = messages[1]
        assert tool_msg["type"] == "tool_use"
        assert tool_msg["toolName"] == "Bash"
        assert tool_msg["toolId"] == "tool-1"
        assert tool_msg["toolStatus"] == "running"


# =============================================================================
# Cross-Device Sync Scenario Tests
# =============================================================================

class TestCrossDeviceSync:
    """Test scenarios for cross-device synchronization."""

    @pytest.mark.asyncio
    async def test_late_join_gets_current_state(self, streaming_session):
        """Late-joining device should get complete current state."""
        # Device 1 has been streaming for a while
        await streaming_session.append_thinking("session-1", "Deep thoughts...")
        await streaming_session.finalize_thinking("session-1")
        await streaming_session.append_text("session-1", "Partial response so far")
        await streaming_session.add_tool_use(
            "session-1",
            tool_id="tool-1",
            name="Read",
            tool_input={"file": "test.py"}
        )

        # Device 2 joins late - gets full state
        state = streaming_session.get_session_dict("session-1")

        assert state["is_streaming"] is True
        assert len(state["messages"]) == 3
        # Verify all content is present for late joiner
        assert state["messages"][0]["content"] == "Deep thoughts..."
        assert state["messages"][1]["content"] == "Partial response so far"
        assert state["messages"][2]["toolName"] == "Read"

    @pytest.mark.asyncio
    async def test_state_consistency_during_streaming(self, streaming_session):
        """State should be consistent regardless of when queried."""
        # Simulate streaming with intermittent state queries
        await streaming_session.append_text("session-1", "Part 1")
        state1 = streaming_session.get_session_dict("session-1")

        await streaming_session.append_text("session-1", " Part 2")
        state2 = streaming_session.get_session_dict("session-1")

        await streaming_session.append_text("session-1", " Part 3")
        state3 = streaming_session.get_session_dict("session-1")

        # Each state should be self-consistent
        assert state1["messages"][0]["content"] == "Part 1"
        assert state2["messages"][0]["content"] == "Part 1 Part 2"
        assert state3["messages"][0]["content"] == "Part 1 Part 2 Part 3"

    @pytest.mark.asyncio
    async def test_multiple_sessions_isolated(self, store):
        """Changes to one session should not affect another."""
        await store.start_streaming("session-1", initial_messages=[])
        await store.start_streaming("session-2", initial_messages=[])

        await store.append_text("session-1", "Session 1 content")
        await store.append_text("session-2", "Session 2 content")

        state1 = store.get_session_dict("session-1")
        state2 = store.get_session_dict("session-2")

        assert state1["messages"][0]["content"] == "Session 1 content"
        assert state2["messages"][0]["content"] == "Session 2 content"


# =============================================================================
# Token Usage Tracking Tests
# =============================================================================

class TestTokenUsageTracking:
    """Test token usage tracking during streaming."""

    @pytest.mark.asyncio
    async def test_update_usage(self, streaming_session):
        """Should update usage correctly."""
        await streaming_session.update_usage(
            "session-1",
            input_tokens=500,
            output_tokens=100,
            cache_creation_tokens=200,
            cache_read_tokens=50
        )

        session = streaming_session.get_session("session-1")
        assert session.input_tokens == 500
        assert session.output_tokens == 100
        assert session.cache_creation_tokens == 200
        assert session.cache_read_tokens == 50

    @pytest.mark.asyncio
    async def test_usage_updates(self, streaming_session):
        """Usage should update to latest values."""
        await streaming_session.update_usage("session-1", output_tokens=50)
        await streaming_session.update_usage("session-1", output_tokens=100)

        session = streaming_session.get_session("session-1")
        # Should be the latest value
        assert session.output_tokens == 100

    @pytest.mark.asyncio
    async def test_usage_in_state(self, streaming_session):
        """Usage should be included in state response."""
        await streaming_session.update_usage(
            "session-1",
            input_tokens=1000,
            output_tokens=500
        )

        state = streaming_session.get_session_dict("session-1")
        assert state["usage"]["input_tokens"] == 1000
        assert state["usage"]["output_tokens"] == 500


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================

class TestEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_empty_content_append(self, streaming_session):
        """Should handle empty string appends gracefully."""
        await streaming_session.append_text("session-1", "")

        session = streaming_session.get_session("session-1")
        # May create a message or not - depends on implementation
        # Key is it shouldn't raise an error

    def test_get_session_dict_on_nonexistent_session(self, store):
        """Should return None for non-existent session."""
        state = store.get_session_dict("nonexistent")
        assert state is None

    @pytest.mark.asyncio
    async def test_finalize_without_streaming_message(self, streaming_session):
        """Should handle finalize when no streaming message exists."""
        # Should not raise
        await streaming_session.finalize_thinking("session-1")

    @pytest.mark.asyncio
    async def test_double_start_streaming(self, store):
        """Starting streaming twice should reset or be idempotent."""
        await store.start_streaming("session-1", initial_messages=[{"id": "1", "content": "old"}])
        await store.append_text("session-1", "streaming text")

        # Start again - should handle gracefully
        await store.start_streaming("session-1", initial_messages=[])

        session = store.get_session("session-1")
        # Depending on implementation, may reset or preserve
        assert session is not None

    @pytest.mark.asyncio
    async def test_operations_after_end_streaming(self, streaming_session):
        """Operations after end_streaming should be handled gracefully."""
        await streaming_session.end_streaming("session-1")

        # These should not raise but may be no-ops (session still exists)
        await streaming_session.append_text("session-1", "late text")

    @pytest.mark.asyncio
    async def test_special_characters_in_content(self, streaming_session):
        """Should handle special characters in content."""
        content = "Test with\nnewlines\tand\ttabs"
        await streaming_session.append_text("session-1", content)

        state = streaming_session.get_session_dict("session-1")
        assert state["messages"][0]["content"] == content

    @pytest.mark.asyncio
    async def test_very_long_content(self, streaming_session):
        """Should handle very long content."""
        long_content = "x" * 100000  # 100KB
        await streaming_session.append_text("session-1", long_content)

        state = streaming_session.get_session_dict("session-1")
        assert len(state["messages"][0]["content"]) == 100000

    @pytest.mark.asyncio
    async def test_unicode_content(self, streaming_session):
        """Should handle unicode content correctly."""
        unicode_content = "Hello 你好 🚀 αβγ"
        await streaming_session.append_text("session-1", unicode_content)

        state = streaming_session.get_session_dict("session-1")
        assert state["messages"][0]["content"] == unicode_content


# =============================================================================
# System Message Tests
# =============================================================================

class TestSystemMessages:
    """Test system message handling."""

    @pytest.mark.asyncio
    async def test_add_system_message(self, streaming_session):
        """Should add system message correctly."""
        await streaming_session.add_system_message(
            "session-1",
            content="Context loaded",
            subtype="context_output",
            data={"files_loaded": 5}
        )

        session = streaming_session.get_session("session-1")
        assert len(session.messages) == 1
        msg = session.messages[0]
        assert msg.type == "system"
        assert msg.role == "system"
        assert msg.content == "Context loaded"
        assert msg.system_subtype == "context_output"
        assert msg.system_data == {"files_loaded": 5}


# =============================================================================
# Subagent Message Tests
# =============================================================================

class TestSubagentMessages:
    """Test subagent message handling."""

    @pytest.mark.asyncio
    async def test_add_subagent_start(self, streaming_session):
        """Should add subagent start message."""
        await streaming_session.add_subagent_start(
            "session-1",
            agent_id="agent-1",
            agent_type="Explore",
            description="Searching codebase",
            prompt="Find files",
            tool_id="tool-task-1"
        )

        session = streaming_session.get_session("session-1")
        msg = session.messages[0]
        assert msg.type == "subagent"
        assert msg.agent_id == "agent-1"
        assert msg.agent_type == "Explore"
        assert msg.agent_status == "running"

    @pytest.mark.asyncio
    async def test_append_subagent_text(self, streaming_session):
        """Should append text to subagent's output."""
        await streaming_session.add_subagent_start(
            "session-1",
            agent_id="agent-1",
            agent_type="Explore",
            description="Search",
            prompt="Find files",
            tool_id="tool-1"
        )
        await streaming_session.append_subagent_text(
            "session-1",
            agent_id="agent-1",
            content="Found files"
        )

        session = streaming_session.get_session("session-1")
        msg = session.messages[0]
        assert len(msg.agent_children) == 1
        assert msg.agent_children[0].content == "Found files"

    @pytest.mark.asyncio
    async def test_complete_subagent(self, streaming_session):
        """Should mark subagent as completed."""
        await streaming_session.add_subagent_start(
            "session-1",
            agent_id="agent-1",
            agent_type="Explore",
            description="Search",
            prompt="Find",
            tool_id="tool-1"
        )
        await streaming_session.complete_subagent(
            "session-1",
            agent_id="agent-1",
            result="Found 5 files",
            is_error=False
        )

        session = streaming_session.get_session("session-1")
        msg = session.messages[0]
        assert msg.agent_status == "completed"
        assert msg.content == "Found 5 files"
        assert msg.streaming is False


# =============================================================================
# Integration-Style Tests
# =============================================================================

class TestIntegrationScenarios:
    """Integration-style tests for realistic scenarios."""

    @pytest.mark.asyncio
    async def test_complete_query_lifecycle(self, store):
        """Test a complete query from start to finish."""
        session_id = "integration-session"

        # Start streaming
        await store.start_streaming(session_id, initial_messages=[
            {"id": "1", "role": "user", "content": "Analyze this code"}
        ])

        # Update usage from stream_start
        await store.update_usage(
            session_id,
            input_tokens=500,
            cache_read_tokens=100
        )

        # Thinking block
        await store.append_thinking(session_id, "I need to analyze the code structure...")
        await store.finalize_thinking(session_id)

        # Text response
        await store.append_text(session_id, "Based on my analysis, ")
        await store.append_text(session_id, "the code looks good. ")
        await store.append_text(session_id, "However, I notice a potential issue.")

        # Tool use
        await store.add_tool_use(
            session_id,
            tool_id="tool-read-1",
            name="Read",
            tool_input={"file_path": "/src/main.py"}
        )
        await store.add_tool_result(
            session_id,
            tool_id="tool-read-1",
            result="def main():\n    pass",
            is_error=False
        )

        # More text after tool
        await store.append_text(session_id, "Looking at the main function, I see...")

        # Update output tokens
        await store.update_usage(session_id, output_tokens=150)

        # End streaming
        await store.end_streaming(session_id)

        # Verify final state
        state = store.get_session_dict(session_id)
        assert state is not None
        assert not state["is_streaming"]
        # user + thinking + text1 + tool + text2
        assert len(state["messages"]) >= 4
        assert state["usage"]["output_tokens"] == 150

    @pytest.mark.asyncio
    async def test_device_switch_during_streaming(self, store):
        """Simulate device switch during active streaming."""
        session_id = "switch-session"

        # Device 1 starts query
        await store.start_streaming(session_id, initial_messages=[])
        await store.append_thinking(session_id, "Thinking deeply...")
        await store.append_thinking(session_id, " about the problem")

        # Device 2 joins (gets state)
        state_for_device2 = store.get_session_dict(session_id)
        assert state_for_device2["is_streaming"] is True
        assert state_for_device2["messages"][0]["content"] == "Thinking deeply... about the problem"

        # Streaming continues
        await store.finalize_thinking(session_id)
        await store.append_text(session_id, "Here's my answer")

        # Device 2 polls for updates
        state_update = store.get_session_since(
            session_id,
            state_for_device2["last_update_id"]
        )
        assert state_update is not None
        assert len(state_update["messages"]) == 2

    @pytest.mark.asyncio
    async def test_refresh_during_streaming(self, store):
        """Simulate browser refresh during streaming."""
        session_id = "refresh-session"

        # Initial streaming
        await store.start_streaming(session_id, initial_messages=[
            {"id": "1", "role": "user", "content": "Hello"}
        ])
        await store.append_text(session_id, "Response in progress...")

        # User refreshes page - backend still has state
        # Simulated by getting state from store
        state_after_refresh = store.get_session_dict(session_id)

        assert state_after_refresh["is_streaming"] is True
        assert len(state_after_refresh["messages"]) == 2  # user + streaming text
        assert state_after_refresh["messages"][1]["content"] == "Response in progress..."
        assert state_after_refresh["messages"][1]["streaming"] is True
