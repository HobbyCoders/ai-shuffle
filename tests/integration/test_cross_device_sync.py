"""
Integration tests for cross-device message streaming synchronization.

These tests verify the complete flow of:
- Streaming state consistency across devices
- Late-joining devices receiving correct state
- Reconnection scenarios (page refresh)
- Thinking and text block separation
- StreamingBuffer correctness

The goal is bulletproof cross-device streaming that allows users to:
- Switch between devices at will
- See streaming messages in real-time with no delays
- Not have thinking and assistant messages get mixed up on refresh
"""

import pytest
import asyncio
from datetime import datetime
from typing import Dict, Any, List

from app.core.sync_engine import SyncEngine, StreamingBuffer
from app.core.live_session_store import LiveSessionStore


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def live_store():
    """Create a fresh LiveSessionStore for each test."""
    return LiveSessionStore()


@pytest.fixture
def streaming_buffer():
    """Create a fresh StreamingBuffer for each test."""
    return StreamingBuffer(session_id="test-session")


# =============================================================================
# StreamingBuffer Tests - Critical for late-joining devices
# =============================================================================

class TestStreamingBuffer:
    """Test StreamingBuffer behavior for late-joining devices."""

    def test_text_chunks_accumulate(self, streaming_buffer):
        """Text chunks should accumulate into a single message."""
        streaming_buffer.add_chunk("text", "Hello ")
        streaming_buffer.add_chunk("text", "World!")

        messages = streaming_buffer.get_messages()
        assert len(messages) == 1
        assert messages[0]["type"] == "text"
        assert messages[0]["content"] == "Hello World!"
        assert messages[0]["streaming"] is True

    def test_thinking_not_handled_by_streaming_buffer(self, streaming_buffer):
        """StreamingBuffer does not handle 'thinking' type - it's handled by LiveSessionStore.

        Note: This is the actual behavior. Thinking content is managed by LiveSessionStore
        not StreamingBuffer. StreamingBuffer is for WebSocket event buffering for late-joiners.
        """
        # StreamingBuffer ignores 'thinking' chunks - they're not in the implementation
        streaming_buffer.add_chunk("thinking", "Analyzing...")
        streaming_buffer.add_chunk("text", "Here is my answer")

        messages = streaming_buffer.get_messages()
        # Only text message is created - thinking is ignored by StreamingBuffer
        assert len(messages) == 1
        assert messages[0]["type"] == "text"
        assert messages[0]["content"] == "Here is my answer"

    def test_tool_use_marks_text_complete(self, streaming_buffer):
        """Tool use should mark previous text as not streaming."""
        streaming_buffer.add_chunk("text", "Let me check...")
        streaming_buffer.add_chunk("tool_use", "", tool_name="Read", tool_id="tool-1", tool_input={"file": "test.py"})

        messages = streaming_buffer.get_messages()
        text_msg = next(m for m in messages if m["type"] == "text")
        assert text_msg["streaming"] is False

    def test_tool_result_merges_into_tool_use(self, streaming_buffer):
        """Tool result should be merged into matching tool_use."""
        streaming_buffer.add_chunk("tool_use", "", tool_name="Read", tool_id="tool-1", tool_input={"file": "test.py"})
        streaming_buffer.add_chunk("tool_result", "file contents", tool_id="tool-1")

        messages = streaming_buffer.get_messages()
        # Should only have one message (merged)
        tool_msgs = [m for m in messages if m["type"] == "tool_use"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_result"] == "file contents"
        assert tool_msgs[0]["tool_status"] == "complete"

    def test_system_message_preserved(self, streaming_buffer):
        """System messages should be preserved for late-joining devices."""
        streaming_buffer.add_chunk("system", "Context loaded: 5 files", subtype="context_output")

        messages = streaming_buffer.get_messages()
        assert len(messages) == 1
        assert messages[0]["type"] == "system"
        assert messages[0]["content"] == "Context loaded: 5 files"

    def test_subagent_lifecycle(self, streaming_buffer):
        """Subagent messages should maintain proper structure."""
        streaming_buffer.add_chunk(
            "subagent_start",
            "",
            agent_id="agent-1",
            agent_type="Explore",
            description="Searching",
            prompt="Find files",
            tool_id="task-1"
        )
        streaming_buffer.add_chunk("subagent_chunk", "Found files", agent_id="agent-1")
        streaming_buffer.add_chunk(
            "subagent_tool_use",
            "",
            agent_id="agent-1",
            tool_name="Read",
            tool_id="read-1",
            tool_input={"file": "test.py"}
        )
        streaming_buffer.add_chunk(
            "subagent_tool_result",
            "",
            agent_id="agent-1",
            tool_use_id="read-1",
            output="file content",
            is_error=False
        )
        streaming_buffer.add_chunk(
            "subagent_done",
            "",
            agent_id="agent-1",
            result="Search complete",
            is_error=False
        )

        messages = streaming_buffer.get_messages()
        assert len(messages) == 1
        subagent = messages[0]
        assert subagent["type"] == "subagent"
        assert subagent["agent_status"] == "completed"
        assert len(subagent["agent_children"]) == 2  # text + tool_use

    def test_clear_removes_all_messages(self, streaming_buffer):
        """Clear should remove all buffered messages."""
        streaming_buffer.add_chunk("text", "test")
        streaming_buffer.clear()

        assert len(streaming_buffer.get_messages()) == 0


# =============================================================================
# LiveSessionStore Cross-Device Scenarios
# =============================================================================

class TestLiveStoreCrossDevice:
    """Test LiveSessionStore for cross-device scenarios."""

    @pytest.mark.asyncio
    async def test_late_joiner_gets_full_state(self, live_store):
        """Device joining mid-stream should get complete state."""
        session_id = "cross-device-session"

        # Device 1 starts streaming
        await live_store.start_streaming(session_id, initial_messages=[
            {"id": "1", "role": "user", "content": "Analyze this"}
        ])

        # Streaming progresses
        await live_store.append_thinking(session_id, "Let me analyze...")
        await live_store.finalize_thinking(session_id)
        await live_store.append_text(session_id, "Based on my analysis")
        await live_store.add_tool_use(session_id, "tool-1", "Read", {"file": "test.py"})

        # Device 2 joins - gets full state
        state = live_store.get_session_dict(session_id)

        assert state is not None
        assert state["is_streaming"] is True
        assert len(state["messages"]) == 4  # user + thinking + text + tool_use

        # Verify each message
        assert state["messages"][0]["role"] == "user"
        assert state["messages"][1]["type"] == "thinking"
        assert state["messages"][2]["type"] == "text"
        assert state["messages"][3]["type"] == "tool_use"

    @pytest.mark.asyncio
    async def test_state_updates_visible_to_all_devices(self, live_store):
        """Updates should be immediately visible to all devices."""
        session_id = "update-visibility"

        await live_store.start_streaming(session_id, initial_messages=[])

        # Device 1 observes initial state
        state1_initial = live_store.get_session_dict(session_id)
        update_id = state1_initial["last_update_id"]

        # Streaming updates happen
        await live_store.append_text(session_id, "First ")
        await live_store.append_text(session_id, "Second ")
        await live_store.append_text(session_id, "Third")

        # Device 2 observes updated state
        state2 = live_store.get_session_dict(session_id)

        assert state2["messages"][0]["content"] == "First Second Third"
        assert state2["last_update_id"] > update_id

        # Device 1 polls and also sees updates
        state1_poll = live_store.get_session_since(session_id, update_id)
        assert state1_poll is not None
        assert state1_poll["messages"][0]["content"] == "First Second Third"

    @pytest.mark.asyncio
    async def test_refresh_recovers_streaming_state(self, live_store):
        """Page refresh should recover complete streaming state."""
        session_id = "refresh-session"

        # Start streaming with history
        await live_store.start_streaming(session_id, initial_messages=[
            {"id": "1", "role": "user", "content": "Hello"}
        ])

        # Streaming in progress
        await live_store.append_thinking(session_id, "Processing...")
        await live_store.finalize_thinking(session_id)
        await live_store.append_text(session_id, "Here is my response")

        # User refreshes page - simulated by getting state again
        state_after_refresh = live_store.get_session_dict(session_id)

        # Verify full state is recovered
        assert state_after_refresh["is_streaming"] is True
        assert len(state_after_refresh["messages"]) == 3

        # Verify thinking and text are separate (filter by role=assistant)
        thinking_msgs = [m for m in state_after_refresh["messages"] if m["type"] == "thinking" and m["role"] == "assistant"]
        assistant_text_msgs = [m for m in state_after_refresh["messages"] if m["type"] == "text" and m["role"] == "assistant"]
        user_msgs = [m for m in state_after_refresh["messages"] if m["role"] == "user"]

        assert len(user_msgs) == 1
        assert len(thinking_msgs) == 1
        assert len(assistant_text_msgs) == 1
        assert thinking_msgs[0]["content"] == "Processing..."
        assert assistant_text_msgs[0]["content"] == "Here is my response"

    @pytest.mark.asyncio
    async def test_thinking_text_never_mixed_during_streaming(self, live_store):
        """CRITICAL: Thinking and text must never be mixed in same message."""
        session_id = "no-mixing"

        await live_store.start_streaming(session_id, initial_messages=[])

        # Simulate complex thinking → text flow
        await live_store.append_thinking(session_id, "Step 1 ")
        await live_store.append_thinking(session_id, "Step 2 ")
        await live_store.append_thinking(session_id, "Step 3")
        await live_store.finalize_thinking(session_id)

        await live_store.append_text(session_id, "Answer part 1 ")
        await live_store.append_text(session_id, "Answer part 2")

        state = live_store.get_session_dict(session_id)

        # Should have exactly 2 messages
        assert len(state["messages"]) == 2

        thinking = state["messages"][0]
        text = state["messages"][1]

        # Verify complete separation
        assert thinking["type"] == "thinking"
        assert thinking["content"] == "Step 1 Step 2 Step 3"
        assert "Answer" not in thinking["content"]

        assert text["type"] == "text"
        assert text["content"] == "Answer part 1 Answer part 2"
        assert "Step" not in text["content"]

    @pytest.mark.asyncio
    async def test_session_end_preserves_state_for_reconnect(self, live_store):
        """Session ending should keep state available for late reconnects."""
        session_id = "end-preserve"

        await live_store.start_streaming(session_id, initial_messages=[])
        await live_store.append_text(session_id, "Complete response")
        await live_store.end_streaming(session_id)

        # Session ended but state should still be available
        state = live_store.get_session_dict(session_id)

        assert state is not None
        assert state["is_streaming"] is False
        assert state["messages"][0]["content"] == "Complete response"
        assert state["messages"][0]["streaming"] is False

    @pytest.mark.asyncio
    async def test_multiple_device_polling_efficiency(self, live_store):
        """Multiple devices polling should use update_id efficiently."""
        session_id = "polling-test"

        await live_store.start_streaming(session_id, initial_messages=[])
        await live_store.append_text(session_id, "Initial")

        state = live_store.get_session_dict(session_id)
        current_id = state["last_update_id"]

        # No changes - polling should return None (304 equivalent)
        result = live_store.get_session_since(session_id, current_id)
        assert result is None

        # Make a change
        await live_store.append_text(session_id, " Updated")

        # Now polling should return new state
        result = live_store.get_session_since(session_id, current_id)
        assert result is not None
        assert result["messages"][0]["content"] == "Initial Updated"


# =============================================================================
# Complete Flow Integration Tests
# =============================================================================

class TestCompleteFlows:
    """Test complete streaming flows."""

    @pytest.mark.asyncio
    async def test_full_query_with_tool_use(self, live_store):
        """Test complete query flow with thinking, text, and tools."""
        session_id = "full-query"

        # Start with user message
        await live_store.start_streaming(session_id, initial_messages=[
            {"id": "u1", "role": "user", "content": "Read test.py and explain it"}
        ])

        # Thinking phase
        await live_store.append_thinking(session_id, "I need to read the file first")
        await live_store.finalize_thinking(session_id)

        # Text before tool
        await live_store.append_text(session_id, "Let me read that file.")

        # Tool use
        await live_store.add_tool_use(
            session_id,
            tool_id="read-1",
            name="Read",
            tool_input={"file_path": "test.py"}
        )
        await live_store.add_tool_result(
            session_id,
            tool_id="read-1",
            result="def main(): pass",
            is_error=False
        )

        # Text after tool
        await live_store.append_text(session_id, "The file contains a simple main function.")

        # End
        await live_store.end_streaming(session_id)

        # Verify final state
        state = live_store.get_session_dict(session_id)

        assert state["is_streaming"] is False
        messages = state["messages"]

        # Should have: user, thinking, text1, tool_use, text2
        assert len(messages) >= 4

        # Verify types
        user_msgs = [m for m in messages if m["role"] == "user"]
        thinking_msgs = [m for m in messages if m["type"] == "thinking"]
        text_msgs = [m for m in messages if m["type"] == "text"]
        tool_msgs = [m for m in messages if m["type"] == "tool_use"]

        assert len(user_msgs) == 1
        assert len(thinking_msgs) == 1
        assert len(text_msgs) >= 1  # May be merged or separate
        assert len(tool_msgs) == 1

        # Verify tool has result
        assert tool_msgs[0]["toolResult"] == "def main(): pass"
        assert tool_msgs[0]["toolStatus"] == "complete"

    @pytest.mark.asyncio
    async def test_interrupted_stream_handling(self, live_store):
        """Test handling of interrupted (stopped) stream."""
        session_id = "interrupted"

        await live_store.start_streaming(session_id, initial_messages=[])
        await live_store.append_text(session_id, "Starting response...")

        # Simulate interruption
        await live_store.end_streaming(session_id, interrupted=True)

        state = live_store.get_session_dict(session_id)

        assert state["is_streaming"] is False
        # Should have [Stopped] marker
        assert "[Stopped]" in state["messages"][0]["content"]

    @pytest.mark.asyncio
    async def test_rapid_block_transitions(self, live_store):
        """Test rapid transitions between block types."""
        session_id = "rapid"

        await live_store.start_streaming(session_id, initial_messages=[])

        # Rapid transitions
        await live_store.append_thinking(session_id, "T1")
        await live_store.finalize_thinking(session_id)
        await live_store.append_text(session_id, "X1")
        await live_store.add_tool_use(session_id, "t1", "Read", {})
        await live_store.add_tool_result(session_id, "t1", "r1", False)
        await live_store.append_text(session_id, "X2")
        await live_store.add_tool_use(session_id, "t2", "Write", {})
        await live_store.add_tool_result(session_id, "t2", "r2", False)
        await live_store.append_text(session_id, "X3")
        await live_store.append_thinking(session_id, "T2")  # Thinking after text
        await live_store.finalize_thinking(session_id)
        await live_store.append_text(session_id, "X4")

        state = live_store.get_session_dict(session_id)

        # Verify all content captured correctly
        thinking_msgs = [m for m in state["messages"] if m["type"] == "thinking"]
        text_msgs = [m for m in state["messages"] if m["type"] == "text"]
        tool_msgs = [m for m in state["messages"] if m["type"] == "tool_use"]

        assert len(thinking_msgs) == 2
        assert len(tool_msgs) == 2

        # Thinking content should be separate
        thinking_content = " ".join(m["content"] for m in thinking_msgs)
        assert "T1" in thinking_content
        assert "T2" in thinking_content
        assert "X" not in thinking_content


# =============================================================================
# Stress Tests
# =============================================================================

class TestStress:
    """Stress tests for streaming system."""

    @pytest.mark.asyncio
    async def test_many_concurrent_updates(self, live_store):
        """Handle many rapid updates."""
        session_id = "stress"

        await live_store.start_streaming(session_id, initial_messages=[])

        # 100 rapid updates
        for i in range(100):
            await live_store.append_text(session_id, f"chunk{i} ")

        state = live_store.get_session_dict(session_id)

        # All content should be captured
        content = state["messages"][0]["content"]
        for i in range(100):
            assert f"chunk{i}" in content

    @pytest.mark.asyncio
    async def test_large_content_handling(self, live_store):
        """Handle large content blocks."""
        session_id = "large"

        await live_store.start_streaming(session_id, initial_messages=[])

        # Large thinking block
        large_thinking = "x" * 50000  # 50KB
        await live_store.append_thinking(session_id, large_thinking)
        await live_store.finalize_thinking(session_id)

        # Large text block
        large_text = "y" * 50000
        await live_store.append_text(session_id, large_text)

        state = live_store.get_session_dict(session_id)

        assert len(state["messages"]) == 2
        assert len(state["messages"][0]["content"]) == 50000
        assert len(state["messages"][1]["content"]) == 50000

    @pytest.mark.asyncio
    async def test_many_tool_uses(self, live_store):
        """Handle many sequential tool uses."""
        session_id = "many-tools"

        await live_store.start_streaming(session_id, initial_messages=[])

        for i in range(20):
            await live_store.add_tool_use(session_id, f"tool-{i}", "Read", {"file": f"file{i}.py"})
            await live_store.add_tool_result(session_id, f"tool-{i}", f"result{i}", False)

        state = live_store.get_session_dict(session_id)

        tool_msgs = [m for m in state["messages"] if m["type"] == "tool_use"]
        assert len(tool_msgs) == 20

        # Each should have result
        for i, msg in enumerate(tool_msgs):
            assert msg["toolResult"] == f"result{i}"
