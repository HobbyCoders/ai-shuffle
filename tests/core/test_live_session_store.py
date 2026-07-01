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

from app.core.live_session_store import (
    LiveSessionStore,
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
    async def test_start_streaming_preserves_history_uuid(self, store):
        """Seeded history keeps its JSONL uuid so the UI can rewind by uuid
        during a streaming turn (not just on reload)."""
        history = [
            {"id": "msg-1", "uuid": "jsonl-uuid-1", "role": "user", "content": "Hello"},
            {"id": "msg-2", "role": "user", "content": "No uuid here"},
        ]

        await store.start_streaming("session-1", initial_messages=history)

        session = store.get_session("session-1")
        assert session.messages[0].uuid == "jsonl-uuid-1"
        # to_dict surfaces uuid when present, omits it otherwise.
        assert session.messages[0].to_dict()["uuid"] == "jsonl-uuid-1"
        assert "uuid" not in session.messages[1].to_dict()

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


class TestCleanupGuardsBackgroundWork:
    """cleanup_ended_session must NOT wipe a session parked on background work.

    A session parked on a backgrounded sub-agent or ``local_bash`` shell is not
    streaming and has no team members, so the 30s-delayed cleanup would delete it
    and clear ``task_registry`` mid-run — leaving ``TaskList`` empty while the
    task is still on the taskbar, and dropping the unpark follow-up's events on a
    missing session. These tests pin that guard.
    """

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        from app.core.task_registry import task_registry
        task_registry.reset_all()
        yield
        task_registry.reset_all()

    @pytest.mark.asyncio
    async def test_skips_cleanup_while_background_agent_running(self, streaming_session):
        """A running background sub-agent keeps the parked session alive."""
        from app.core.task_registry import task_registry

        await streaming_session.end_streaming("session-1")  # parked, not streaming
        task_registry.start_task("session-1", "task-1", "agent", "bg agent")

        removed = await streaming_session.cleanup_ended_session("session-1")

        assert removed is False
        assert streaming_session.get_session("session-1") is not None

    @pytest.mark.asyncio
    async def test_skips_cleanup_while_sdk_background_shell_running(self, streaming_session):
        """A running SDK-backgrounded local_bash shell keeps the session alive."""
        from app.core.task_registry import task_registry

        await streaming_session.end_streaming("session-1")
        task_registry.start_task("session-1", "task-1", "shell", "bg shell")
        task_registry.mark_sdk_background_task("session-1", "task-1")

        removed = await streaming_session.cleanup_ended_session("session-1")

        assert removed is False
        assert streaming_session.get_session("session-1") is not None

    @pytest.mark.asyncio
    async def test_cleans_up_app_managed_monitor_shell(self, streaming_session):
        """An app-managed Monitor shell (NOT SDK-background) must NOT pin the
        session — cleanup proceeds normally."""
        from app.core.task_registry import task_registry

        await streaming_session.end_streaming("session-1")
        # Monitor shells register via start_task only (no mark_sdk_background_task).
        task_registry.start_task("session-1", "mon-1", "shell", "monitor watch")

        removed = await streaming_session.cleanup_ended_session("session-1")

        assert removed is True
        assert streaming_session.get_session("session-1") is None

    @pytest.mark.asyncio
    async def test_cleans_up_after_background_task_completes(self, streaming_session):
        """Once the background task reaches a terminal status the guard releases
        and a later cleanup removes the session (deferral, not a permanent block)."""
        from app.core.task_registry import task_registry

        await streaming_session.end_streaming("session-1")
        task_registry.start_task("session-1", "task-1", "agent", "bg agent")
        task_registry.mark_sdk_background_task("session-1", "task-1")

        # Still running → skipped.
        assert await streaming_session.cleanup_ended_session("session-1") is False
        assert streaming_session.get_session("session-1") is not None

        # Completes → next cleanup proceeds.
        task_registry.complete_task("session-1", "task-1", "completed")
        assert await streaming_session.cleanup_ended_session("session-1") is True
        assert streaming_session.get_session("session-1") is None

    @pytest.mark.asyncio
    async def test_streaming_session_with_background_work_still_skipped(self, streaming_session):
        """The pre-existing is_streaming guard still wins (no behaviour change for
        an actively streaming session)."""
        from app.core.task_registry import task_registry

        # Session is streaming AND has background work.
        task_registry.start_task("session-1", "task-1", "agent", "bg agent")

        removed = await streaming_session.cleanup_ended_session("session-1")

        assert removed is False
        assert streaming_session.get_session("session-1") is not None


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
    async def test_text_before_thinking_reorders_correctly(self, streaming_session):
        """CRITICAL: When SDK emits text before thinking, thinking should still appear first.

        The SDK sometimes emits text blocks (e.g., '\\n\\n') before thinking blocks.
        When append_thinking creates a new message, it must be inserted before any
        trailing streaming text messages to maintain correct visual ordering.
        """
        # Simulate SDK's out-of-order emission: text arrives before thinking
        await streaming_session.append_text("session-1", "\n\n")
        await streaming_session.append_thinking("session-1", "Let me analyze this...")

        session = streaming_session.get_session("session-1")
        assert len(session.messages) == 2
        # Thinking must come BEFORE text in the array
        assert session.messages[0].type == "thinking"
        assert session.messages[0].content == "Let me analyze this..."
        assert session.messages[1].type == "text"
        assert session.messages[1].content == "\n\n"

    @pytest.mark.asyncio
    async def test_text_before_thinking_with_real_content(self, streaming_session):
        """When text with real content arrives before thinking, thinking should still come first."""
        await streaming_session.append_text("session-1", "Some early text")
        await streaming_session.append_thinking("session-1", "Thinking...")

        session = streaming_session.get_session("session-1")
        assert len(session.messages) == 2
        assert session.messages[0].type == "thinking"
        assert session.messages[1].type == "text"

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
    async def test_add_tool_result_stores_and_serializes_screenshot(self, streaming_session):
        """A screenshot data URI must be stored on the tool_use message and
        surfaced as ``toolScreenshot`` in the serialized payload."""
        data_uri = "data:image/png;base64,iVBORscreenshot"
        await streaming_session.add_tool_use(
            "session-1",
            tool_id="tool-shot",
            name="mcp__claude-in-chrome__computer",
            tool_input={"action": "screenshot"},
        )
        await streaming_session.add_tool_result(
            "session-1",
            tool_id="tool-shot",
            result="took a screenshot",
            is_error=False,
            screenshot=data_uri,
        )

        session = streaming_session.get_session("session-1")
        msg = session.messages[0]
        assert msg.tool_screenshot == data_uri
        assert msg.to_dict()["toolScreenshot"] == data_uri

    @pytest.mark.asyncio
    async def test_add_tool_result_without_screenshot_omits_field(self, streaming_session):
        """Tools with no image result must not carry a toolScreenshot key."""
        await streaming_session.add_tool_use(
            "session-1", tool_id="tool-x", name="Read", tool_input={}
        )
        await streaming_session.add_tool_result(
            "session-1", tool_id="tool-x", result="contents", is_error=False
        )

        session = streaming_session.get_session("session-1")
        msg = session.messages[0]
        assert msg.tool_screenshot is None
        assert "toolScreenshot" not in msg.to_dict()

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
    async def test_background_bash_stays_running_via_input_flag(self, streaming_session):
        """A backgrounded Bash (run_in_background=True) must stay 'running' when
        its immediate acknowledgement result arrives — not flip to 'complete'."""
        await streaming_session.add_tool_use(
            "session-1",
            tool_id="tool-bg",
            name="Bash",
            tool_input={"command": "sleep 100", "run_in_background": True},
        )
        await streaming_session.add_tool_result(
            "session-1",
            tool_id="tool-bg",
            result="Command running in the background with ID: bash_1",
            is_error=False,
        )

        session = streaming_session.get_session("session-1")
        msg = session.messages[0]
        # Result is surfaced, but status stays running (still executing).
        assert msg.tool_result == "Command running in the background with ID: bash_1"
        assert msg.tool_status == "running"
        assert msg.streaming is False

    @pytest.mark.asyncio
    async def test_background_bash_stays_running_via_ack_text(self, streaming_session):
        """Even without the input flag, the standard CLI 'running in the
        background' acknowledgement keeps the tool 'running'."""
        await streaming_session.add_tool_use(
            "session-1",
            tool_id="tool-bg2",
            name="Bash",
            tool_input={"command": "npm run dev"},
        )
        await streaming_session.add_tool_result(
            "session-1",
            tool_id="tool-bg2",
            result="Command running in the background with ID: bash_7",
            is_error=False,
        )

        session = streaming_session.get_session("session-1")
        assert session.messages[0].tool_status == "running"

    @pytest.mark.asyncio
    async def test_background_bash_error_still_errors(self, streaming_session):
        """A backgrounded Bash that fails must still report 'error', not run."""
        await streaming_session.add_tool_use(
            "session-1",
            tool_id="tool-bg3",
            name="Bash",
            tool_input={"command": "bad", "run_in_background": True},
        )
        await streaming_session.add_tool_result(
            "session-1",
            tool_id="tool-bg3",
            result="boom",
            is_error=True,
        )

        session = streaming_session.get_session("session-1")
        assert session.messages[0].tool_status == "error"

    @pytest.mark.asyncio
    async def test_foreground_bash_with_background_text_completes(self, streaming_session):
        """A normal (foreground) Bash whose real output merely mentions the word
        'background' must still complete — guard against over-eager sniffing."""
        await streaming_session.add_tool_use(
            "session-1",
            tool_id="tool-fg",
            name="Bash",
            tool_input={"command": "echo hi"},
        )
        await streaming_session.add_tool_result(
            "session-1",
            tool_id="tool-fg",
            result="hi",
            is_error=False,
        )

        session = streaming_session.get_session("session-1")
        assert session.messages[0].tool_status == "complete"

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


# =============================================================================
# Message ID Uniqueness Tests
# =============================================================================

class TestMessageIdUniqueness:
    """Verify that auto-generated message IDs never collide, even under rapid creation."""

    @pytest.mark.asyncio
    async def test_rapid_text_messages_have_unique_ids(self, streaming_session):
        """Two text messages created back-to-back must have different IDs."""
        # Create first text message
        await streaming_session.append_text("session-1", "chunk1")
        # Finalize it so append_text creates a new message
        session = streaming_session.get_session("session-1")
        for msg in session.messages:
            if msg.streaming and msg.type == "text":
                msg.streaming = False
        # Create second text message
        await streaming_session.append_text("session-1", "chunk2")

        text_msgs = [m for m in session.messages if m.type == "text"]
        assert len(text_msgs) == 2
        assert text_msgs[0].id != text_msgs[1].id, f"ID collision: {text_msgs[0].id}"

    @pytest.mark.asyncio
    async def test_rapid_thinking_messages_have_unique_ids(self, streaming_session):
        """Two thinking messages created back-to-back must have different IDs."""
        await streaming_session.append_thinking("session-1", "thought1")
        session = streaming_session.get_session("session-1")
        for msg in session.messages:
            if msg.streaming and msg.type == "thinking":
                msg.streaming = False
        await streaming_session.append_thinking("session-1", "thought2")

        thinking_msgs = [m for m in session.messages if m.type == "thinking"]
        assert len(thinking_msgs) == 2
        assert thinking_msgs[0].id != thinking_msgs[1].id, f"ID collision: {thinking_msgs[0].id}"

    @pytest.mark.asyncio
    async def test_rapid_system_messages_have_unique_ids(self, streaming_session):
        """Two system messages created back-to-back must have different IDs."""
        await streaming_session.add_system_message("session-1", "msg1", subtype="info")
        await streaming_session.add_system_message("session-1", "msg2", subtype="info")

        session = streaming_session.get_session("session-1")
        system_msgs = [m for m in session.messages if m.type == "system"]
        assert len(system_msgs) == 2
        assert system_msgs[0].id != system_msgs[1].id, f"ID collision: {system_msgs[0].id}"

    @pytest.mark.asyncio
    async def test_ids_are_uuid_format(self, streaming_session):
        """Auto-generated IDs should use UUID hex format, not timestamps."""
        await streaming_session.append_text("session-1", "test")
        session = streaming_session.get_session("session-1")
        text_msg = [m for m in session.messages if m.type == "text"][0]
        # Should be text-<12 hex chars>, not text-<float timestamp>
        assert text_msg.id.startswith("text-")
        hex_part = text_msg.id.split("-", 1)[1]
        assert len(hex_part) == 12
        # Verify it's hex (would raise ValueError if not)
        int(hex_part, 16)


# =============================================================================
# Discard Empty Interrupted User Turn Tests
# =============================================================================

class TestDiscardEmptyUserTurn:
    """Tests for discard_empty_user_turn — used when a user presses Stop before
    any assistant output is produced. The orphan user message (which never gets
    durably saved) is removed from the live store and its text returned so the
    frontend can restore it to the input box."""

    @pytest.mark.asyncio
    async def test_discards_orphan_user_message(self, store):
        """Stop before any output: the lone user message is removed and returned."""
        await store.start_streaming("session-1", initial_messages=[])
        await store.add_user_message("session-1", "draft me a poem", "user-1")
        # End streaming as interrupted (no assistant output was produced).
        await store.end_streaming("session-1", metadata={}, interrupted=True)

        discarded = await store.discard_empty_user_turn("session-1")

        assert discarded == "draft me a poem"
        session = store.get_session("session-1")
        assert all(m.role != "user" for m in session.messages)
        assert session.needs_full_refresh is True

    @pytest.mark.asyncio
    async def test_keeps_turn_with_assistant_output(self, store):
        """A turn that produced assistant text must NOT be discarded."""
        await store.start_streaming("session-1", initial_messages=[])
        await store.add_user_message("session-1", "hello", "user-1")
        await store.start_assistant_message("session-1", "asst-1")
        await store.append_text("session-1", "Hi there")
        await store.end_streaming("session-1", metadata={}, interrupted=True)

        discarded = await store.discard_empty_user_turn("session-1")

        assert discarded is None
        session = store.get_session("session-1")
        assert any(m.role == "user" and m.content == "hello" for m in session.messages)

    @pytest.mark.asyncio
    async def test_preserves_prior_history(self, store):
        """Only THIS turn's user message is removed; prior history is untouched."""
        history = [
            {"id": "h-1", "role": "user", "content": "first question"},
            {"id": "h-2", "role": "assistant", "content": "first answer", "type": "text"},
        ]
        await store.start_streaming("session-1", initial_messages=history)
        await store.add_user_message("session-1", "second question", "user-2")
        await store.end_streaming("session-1", metadata={}, interrupted=True)

        discarded = await store.discard_empty_user_turn("session-1")

        assert discarded == "second question"
        session = store.get_session("session-1")
        # Prior history is intact (the new orphan prompt is the only thing removed).
        # NB: end_streaming independently tags the last assistant message with a
        # "[Stopped]" marker — that's existing behavior unrelated to discard — so
        # assert on the user prompts, which is what discard governs.
        user_contents = [m.content for m in session.messages if m.role == "user"]
        assert user_contents == ["first question"]
        assert any(m.role == "assistant" and m.content.startswith("first answer")
                   for m in session.messages)

    @pytest.mark.asyncio
    async def test_no_op_when_no_turn_message_added(self, store):
        """If the turn added nothing (last message is history), discard is a no-op."""
        history = [
            {"id": "h-1", "role": "user", "content": "lonely user message"},
        ]
        await store.start_streaming("session-1", initial_messages=history)
        # No add_user_message — simulate a turn that produced nothing at all.
        await store.end_streaming("session-1", metadata={}, interrupted=True)

        discarded = await store.discard_empty_user_turn("session-1")

        assert discarded is None
        session = store.get_session("session-1")
        assert len(session.messages) == 1

    @pytest.mark.asyncio
    async def test_no_op_on_nonexistent_session(self, store):
        """Discard on a missing session returns None and does not raise."""
        assert await store.discard_empty_user_turn("nonexistent") is None

    @pytest.mark.asyncio
    async def test_ignores_task_notification_user_message(self, store):
        """A trailing SDK task-notification is not a user prompt — never discarded.

        (In practice end_streaming filters these out first; this guards the
        method directly in case it's called on un-filtered state.)"""
        await store.start_streaming("session-1", initial_messages=[])
        await store.add_user_message(
            "session-1", "<task-notification>agent done</task-notification>", "user-1"
        )

        discarded = await store.discard_empty_user_turn("session-1")

        assert discarded is None


# =============================================================================
# Truncate After UUID (rewind trim for refused-cleanup live sessions) Tests
# =============================================================================

class TestTruncateAfterUuid:
    """Tests for truncate_after_uuid — trims a surviving in-memory snapshot at a
    rewind checkpoint when cleanup_ended_session refused to drop the session
    (streaming / team members / parked on background work). Without it the
    longer pre-rewind snapshot wins the /live "longer wins" heal and rewind
    visually no-ops. Mirrors compute_rewind_keep_count semantics over the
    rendered live messages.
    """

    @staticmethod
    async def _seed(store, messages):
        await store.start_streaming("session-1", initial_messages=messages)
        # Simulate an ended-but-not-cleaned session (the RC3 scenario).
        session = store.get_session("session-1")
        session.is_streaming = False
        return session

    @pytest.mark.asyncio
    async def test_user_target_no_response_drops_target_and_after(self, store):
        """User target, include_response=False: drop the target prompt and all after."""
        await self._seed(store, [
            {"id": "m1", "role": "user", "type": "text", "content": "first", "uuid": "u1"},
            {"id": "m2", "role": "assistant", "type": "text", "content": "reply1", "uuid": "a1"},
            {"id": "m3", "role": "user", "type": "text", "content": "second", "uuid": "u2"},
            {"id": "m4", "role": "assistant", "type": "text", "content": "reply2", "uuid": "a2"},
        ])

        removed = await store.truncate_after_uuid(
            "session-1", target_uuid="u2", include_response=False, target_role="user"
        )

        assert removed is True
        session = store.get_session("session-1")
        assert [m.uuid for m in session.messages] == ["u1", "a1"]
        assert session.needs_full_refresh is True

    @pytest.mark.asyncio
    async def test_user_target_include_response_keeps_reply(self, store):
        """User target, include_response=True: keep the target + its reply, drop the next turn."""
        await self._seed(store, [
            {"id": "m1", "role": "user", "type": "text", "content": "first", "uuid": "u1"},
            {"id": "m2", "role": "assistant", "type": "text", "content": "reply1", "uuid": "a1"},
            {"id": "m3", "role": "user", "type": "text", "content": "second", "uuid": "u2"},
            {"id": "m4", "role": "assistant", "type": "text", "content": "reply2", "uuid": "a2"},
            {"id": "m5", "role": "user", "type": "text", "content": "third", "uuid": "u3"},
        ])

        removed = await store.truncate_after_uuid(
            "session-1", target_uuid="u1", include_response=True, target_role="user"
        )

        assert removed is True
        session = store.get_session("session-1")
        # Kept up to (not including) the NEXT real user prompt (u2).
        assert [m.uuid for m in session.messages] == ["u1", "a1"]

    @pytest.mark.asyncio
    async def test_assistant_target_drops_next_turn_onward(self, store):
        """Assistant target: keep the assistant and everything before, drop from next user prompt."""
        await self._seed(store, [
            {"id": "m1", "role": "user", "type": "text", "content": "first", "uuid": "u1"},
            {"id": "m2", "role": "assistant", "type": "text", "content": "reply1", "uuid": "a1"},
            {"id": "m3", "role": "user", "type": "text", "content": "second", "uuid": "u2"},
        ])

        removed = await store.truncate_after_uuid(
            "session-1", target_uuid="a1", include_response=False, target_role="assistant"
        )

        assert removed is True
        session = store.get_session("session-1")
        assert [m.uuid for m in session.messages] == ["u1", "a1"]

    @pytest.mark.asyncio
    async def test_target_is_tail_is_noop(self, store):
        """Target already at the tail (include_response, nothing after): no removal."""
        await self._seed(store, [
            {"id": "m1", "role": "user", "type": "text", "content": "first", "uuid": "u1"},
            {"id": "m2", "role": "assistant", "type": "text", "content": "reply1", "uuid": "a1"},
        ])

        removed = await store.truncate_after_uuid(
            "session-1", target_uuid="u1", include_response=True, target_role="user"
        )

        assert removed is False
        session = store.get_session("session-1")
        assert [m.uuid for m in session.messages] == ["u1", "a1"]

    @pytest.mark.asyncio
    async def test_skips_task_notification_when_finding_next_user(self, store):
        """A <task-notification> callback is not a real prompt — trim past it to the next real user."""
        await self._seed(store, [
            {"id": "m1", "role": "user", "type": "text", "content": "first", "uuid": "u1"},
            {"id": "m2", "role": "assistant", "type": "text", "content": "reply1", "uuid": "a1"},
            {"id": "m3", "role": "user", "type": "text",
             "content": "<task-notification>agent done</task-notification>", "uuid": "n1"},
            {"id": "m4", "role": "assistant", "type": "text", "content": "reply2", "uuid": "a2"},
            {"id": "m5", "role": "user", "type": "text", "content": "second", "uuid": "u2"},
        ])

        removed = await store.truncate_after_uuid(
            "session-1", target_uuid="u1", include_response=True, target_role="user"
        )

        assert removed is True
        session = store.get_session("session-1")
        # Kept through the notification + its reply; dropped from the next REAL prompt (u2).
        assert [m.uuid for m in session.messages] == ["u1", "a1", "n1", "a2"]

    @pytest.mark.asyncio
    async def test_unknown_target_is_noop(self, store):
        """A target uuid absent from the live messages leaves them untouched."""
        await self._seed(store, [
            {"id": "m1", "role": "user", "type": "text", "content": "first", "uuid": "u1"},
        ])

        removed = await store.truncate_after_uuid(
            "session-1", target_uuid="nope", include_response=False, target_role="user"
        )

        assert removed is False
        assert len(store.get_session("session-1").messages) == 1

    @pytest.mark.asyncio
    async def test_missing_session_is_noop(self, store):
        """Trimming a nonexistent session returns False and does not raise."""
        removed = await store.truncate_after_uuid(
            "nonexistent", target_uuid="u1", include_response=False, target_role="user"
        )
        assert removed is False
