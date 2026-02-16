"""
Regression tests for live_session_store.py

Each test targets a specific bug that was found and fixed in production.
These tests exist to prevent those bugs from recurring.

Bug references:
- Commit 756e7b8b: Auto-compaction message loss (needs_full_refresh)
- Commit 53243e4f: Stale streaming session entries
- Commit 1531f472: End streaming retention (30s memory window)
- KNOWN_ISSUES.md: Root Causes 5, 7, 8, 9
"""

import pytest

from app.core.live_session_store import LiveSessionStore


@pytest.fixture
def store():
    return LiveSessionStore()


@pytest.fixture
async def store_with_history(store):
    """Store with a session that has history + streaming messages."""
    history = [
        {"id": "h1", "role": "user", "content": "Hello"},
        {"id": "h2", "role": "assistant", "type": "text", "content": "Hi there!"},
        {"id": "h3", "role": "user", "content": "Analyze this"},
    ]
    await store.start_streaming("s1", initial_messages=history)
    # Add some streaming messages
    await store.append_thinking("s1", "Let me think...")
    await store.finalize_thinking("s1")
    await store.append_text("s1", "Here is my analysis")
    return store


# =============================================================================
# Bug: Auto-compaction message loss (commit 756e7b8b)
#
# During auto-compaction, a compact_boundary system message arrives.
# If the frontend does a delta merge with the old initial_message_count,
# it drops all pre-compaction messages. The fix: needs_full_refresh flag
# forces the next poll to return ALL messages with initial_message_count=0.
# =============================================================================

class TestCompactionBoundary:

    async def test_compact_boundary_sets_needs_full_refresh(self, store_with_history):
        """compact_boundary system message must flag needs_full_refresh."""
        await store_with_history.add_system_message(
            "s1",
            content="Context compacted",
            subtype="compact_boundary",
        )
        session = store_with_history.get_session("s1")
        assert session.needs_full_refresh is True

    async def test_microcompact_boundary_sets_needs_full_refresh(self, store_with_history):
        """microcompact_boundary should also flag needs_full_refresh."""
        await store_with_history.add_system_message(
            "s1",
            content="Micro-compacted",
            subtype="microcompact_boundary",
        )
        session = store_with_history.get_session("s1")
        assert session.needs_full_refresh is True

    async def test_normal_system_message_does_not_set_full_refresh(self, store_with_history):
        """Non-compaction system messages should NOT flag needs_full_refresh."""
        await store_with_history.add_system_message(
            "s1",
            content="Something else",
            subtype="context_output",
        )
        session = store_with_history.get_session("s1")
        assert session.needs_full_refresh is False

    async def test_full_refresh_returns_all_messages_with_zero_initial_count(self, store_with_history):
        """After compaction, get_session_since must return ALL messages with initial_message_count=0.

        This is the actual bug: without this, the frontend's delta merge would try to
        splice streaming messages onto stale history, causing message loss.
        """
        session = store_with_history.get_session("s1")
        update_id_before = session.last_update_id

        # Compaction boundary arrives
        await store_with_history.add_system_message(
            "s1",
            content="Compacted",
            subtype="compact_boundary",
        )

        # Frontend polls with its last known update_id
        result = store_with_history.get_session_since("s1", update_id_before)

        assert result is not None
        # CRITICAL: initial_message_count must be 0 so frontend does full replace
        assert result["initial_message_count"] == 0
        # Must include ALL messages (history + streaming + system)
        total_messages = len(result["messages"])
        assert total_messages >= 5  # 3 history + thinking + text + system

    async def test_full_refresh_flag_cleared_after_poll(self, store_with_history):
        """needs_full_refresh should be cleared after one poll consumes it."""
        session = store_with_history.get_session("s1")
        update_id_before = session.last_update_id

        await store_with_history.add_system_message(
            "s1", content="Compacted", subtype="compact_boundary",
        )

        # First poll consumes the flag
        result1 = store_with_history.get_session_since("s1", update_id_before)
        assert result1["initial_message_count"] == 0

        # Second poll should be a normal delta (flag cleared)
        session = store_with_history.get_session("s1")
        assert session.needs_full_refresh is False

    async def test_delta_poll_skips_history_messages(self, store_with_history):
        """Normal delta polls should skip history messages (optimization).

        When the frontend already has the history, returning it again is wasteful
        and can cause duplicate messages on merge.
        """
        session = store_with_history.get_session("s1")
        # initial_message_count=3 (the 3 history messages)
        assert session.initial_message_count == 3

        # Delta poll (since_update_id > 0) should only return streaming messages
        result = store_with_history.get_session_since("s1", 0)
        assert result is not None
        # since_update_id=0 is the first poll, should get everything
        all_count = len(result["messages"])

        # Now do a delta poll
        await store_with_history.append_text("s1", " more text")
        result_delta = store_with_history.get_session_since("s1", result["last_update_id"])
        assert result_delta is not None
        # Delta should have fewer messages than full (skipping history)
        delta_count = len(result_delta["messages"])
        assert delta_count < all_count
        # Delta should report the original initial_message_count
        assert result_delta["initial_message_count"] == 3


# =============================================================================
# Bug: End streaming retention / cleanup race (commits 1531f472, 53243e4f)
#
# After streaming ends, the session must stay in memory for ~30s so
# reconnecting clients can retrieve messages before JSONL is ready.
# Also, cleanup_ended_session must NOT delete a session that has
# started streaming again (new query on same session ID).
# =============================================================================

class TestEndStreamingRetention:

    async def test_session_stays_in_memory_after_end_streaming(self, store_with_history):
        """Session must remain accessible after end_streaming for reconnecting clients."""
        await store_with_history.end_streaming("s1")

        # Session should still be retrievable
        session = store_with_history.get_session("s1")
        assert session is not None
        assert session.is_streaming is False

        # get_session_dict should work too
        state = store_with_history.get_session_dict("s1")
        assert state is not None
        assert state["is_streaming"] is False

    async def test_end_streaming_removes_whitespace_only_messages(self, store):
        """End streaming must clean up whitespace-only text messages.

        The SDK sometimes emits text blocks with just '\\n\\n' before thinking blocks.
        These should be removed on stream end to avoid blank message bubbles in UI.
        """
        await store.start_streaming("s1", initial_messages=[])
        # Simulate SDK behavior: whitespace text before thinking
        await store.append_text("s1", "\n\n")
        await store.append_thinking("s1", "Thinking...")
        await store.finalize_thinking("s1")
        await store.append_text("s1", "Real response")

        messages_before = store.get_session("s1").messages
        # Should have: thinking, whitespace text, real text (thinking reordered before text)
        # Actually: thinking (inserted before whitespace), whitespace text, real text
        has_whitespace = any(
            m.type == "text" and not m.content.strip() for m in messages_before
        )

        await store.end_streaming("s1")

        session = store.get_session("s1")
        # Whitespace-only text messages should be removed
        for msg in session.messages:
            if msg.type == "text" and msg.role == "assistant":
                assert msg.content.strip() != "", f"Whitespace-only message not cleaned: '{msg.content}'"

    async def test_end_streaming_adds_stopped_marker_when_interrupted(self, store):
        """When interrupted=True, last assistant message should get [Stopped] marker."""
        await store.start_streaming("s1", initial_messages=[])
        await store.append_text("s1", "Partial response")

        await store.end_streaming("s1", interrupted=True)

        session = store.get_session("s1")
        last_assistant = None
        for msg in reversed(session.messages):
            if msg.role == "assistant":
                last_assistant = msg
                break
        assert last_assistant is not None
        assert "[Stopped]" in last_assistant.content

    async def test_end_streaming_attaches_metadata(self, store):
        """Metadata should be attached to the last assistant message."""
        await store.start_streaming("s1", initial_messages=[])
        await store.append_text("s1", "Response")

        metadata = {"duration_ms": 1500, "model": "claude-sonnet"}
        await store.end_streaming("s1", metadata=metadata)

        session = store.get_session("s1")
        last_assistant = None
        for msg in reversed(session.messages):
            if msg.role == "assistant":
                last_assistant = msg
                break
        assert last_assistant is not None
        assert last_assistant.metadata == metadata

    async def test_end_streaming_marks_all_messages_not_streaming(self, store):
        """All messages must have streaming=False after end_streaming."""
        await store.start_streaming("s1", initial_messages=[])
        await store.append_thinking("s1", "thinking")
        await store.append_text("s1", "text")
        await store.add_tool_use("s1", "t1", "Read", {"file": "x"})

        await store.end_streaming("s1")

        session = store.get_session("s1")
        for msg in session.messages:
            assert msg.streaming is False, f"Message {msg.id} still streaming after end"


class TestCleanupRaceCondition:
    """Bug: cleanup_ended_session called after new streaming starts on same session.

    Timeline:
    1. Query A ends on session-1 → end_streaming() → schedule cleanup in 30s
    2. User sends query B on session-1 → start_streaming() creates new session
    3. 30s later: cleanup fires → MUST NOT delete the active session
    """

    async def test_cleanup_skips_active_streaming_session(self, store):
        """cleanup_ended_session must NOT remove a session that is currently streaming."""
        # Query A
        await store.start_streaming("s1", initial_messages=[])
        await store.append_text("s1", "Response A")
        await store.end_streaming("s1")

        # Query B starts (overwrites ended session)
        await store.start_streaming("s1", initial_messages=[])
        await store.append_text("s1", "Response B")

        # Delayed cleanup from Query A fires
        result = await store.cleanup_ended_session("s1")

        # Must NOT have been cleaned up
        assert result is False
        session = store.get_session("s1")
        assert session is not None
        assert session.is_streaming is True
        # Must still have Query B's messages
        assert any("Response B" in m.content for m in session.messages)

    async def test_cleanup_removes_ended_session(self, store):
        """cleanup_ended_session should remove a genuinely ended session."""
        await store.start_streaming("s1", initial_messages=[])
        await store.end_streaming("s1")

        result = await store.cleanup_ended_session("s1")
        assert result is True
        assert store.get_session("s1") is None

    async def test_cleanup_nonexistent_session(self, store):
        """cleanup_ended_session on non-existent session should return False."""
        result = await store.cleanup_ended_session("nonexistent")
        assert result is False


# =============================================================================
# Bug: Stale message overwrites / update ID guard (KNOWN_ISSUES Root Cause 8)
#
# Multiple writers could race to update session messages. The fix uses
# last_update_id to ensure only newer state is applied.
# =============================================================================

class TestUpdateIdConsistency:

    async def test_update_id_monotonically_increases(self, store):
        """last_update_id must only increase, never decrease or stay the same."""
        await store.start_streaming("s1", initial_messages=[])

        prev_id = store.get_session("s1").last_update_id
        operations = [
            lambda: store.append_text("s1", "text"),
            lambda: store.append_thinking("s1", "thinking"),
            lambda: store.finalize_thinking("s1"),
            lambda: store.add_tool_use("s1", "t1", "Read", {}),
            lambda: store.add_tool_result("s1", "t1", "result"),
            lambda: store.append_text("s1", "more text"),
            lambda: store.update_usage("s1", input_tokens=100),
            lambda: store.add_system_message("s1", "system msg"),
        ]

        for op in operations:
            await op()
            current_id = store.get_session("s1").last_update_id
            assert current_id > prev_id, f"update_id did not increase after operation"
            prev_id = current_id

    async def test_get_session_since_returns_none_for_stale_id(self, store):
        """Polling with current update_id should return None (no changes)."""
        await store.start_streaming("s1", initial_messages=[])
        await store.append_text("s1", "text")

        current_id = store.get_session("s1").last_update_id
        result = store.get_session_since("s1", current_id)
        assert result is None

    async def test_get_session_since_returns_none_for_nonexistent(self, store):
        """Polling non-existent session should return None, not raise."""
        result = store.get_session_since("nonexistent", 0)
        assert result is None


# =============================================================================
# Bug: Initial message count tracking for delta polls
#
# When start_streaming is called with history, initial_message_count must
# match the number of history messages so delta polls can skip them.
# =============================================================================

class TestInitialMessageCount:

    async def test_initial_message_count_matches_history(self, store):
        """initial_message_count must equal the number of history messages."""
        history = [
            {"id": "1", "role": "user", "content": "Q1"},
            {"id": "2", "role": "assistant", "type": "text", "content": "A1"},
            {"id": "3", "role": "user", "content": "Q2"},
        ]
        await store.start_streaming("s1", initial_messages=history)

        session = store.get_session("s1")
        assert session.initial_message_count == 3

    async def test_initial_message_count_zero_without_history(self, store):
        """initial_message_count must be 0 when no history is provided."""
        await store.start_streaming("s1", initial_messages=[])
        assert store.get_session("s1").initial_message_count == 0

        await store.start_streaming("s2", initial_messages=None)
        assert store.get_session("s2").initial_message_count == 0

    async def test_delta_only_excludes_history(self, store):
        """to_dict(delta_only=True) must exclude initial history messages."""
        history = [
            {"id": "1", "role": "user", "content": "Hello"},
            {"id": "2", "role": "assistant", "type": "text", "content": "Hi"},
        ]
        await store.start_streaming("s1", initial_messages=history)
        await store.append_text("s1", "New streaming text")

        session = store.get_session("s1")

        full = session.to_dict(delta_only=False)
        delta = session.to_dict(delta_only=True)

        assert len(full["messages"]) == 3  # 2 history + 1 streaming
        assert len(delta["messages"]) == 1  # only streaming
        assert delta["messages"][0]["content"] == "New streaming text"


# =============================================================================
# Bug: Early tool_use deduplication
#
# stream_block_start creates early tool_use messages. When the full
# AssistantMessage arrives with add_tool_use, it must UPDATE the existing
# message instead of creating a duplicate.
# =============================================================================

class TestEarlyToolUseDedup:

    async def test_early_tool_use_updated_by_full_tool_use(self, store):
        """add_tool_use should update an existing early tool_use, not create duplicate."""
        await store.start_streaming("s1", initial_messages=[])

        # Early creation from stream_block_start
        await store.add_early_tool_use("s1", "tool-123", "Read")

        # Full tool_use arrives later
        await store.add_tool_use("s1", "tool-123", "Read", {"file_path": "/test.py"})

        session = store.get_session("s1")
        tool_messages = [m for m in session.messages if m.type == "tool_use"]
        assert len(tool_messages) == 1, "Duplicate tool_use created!"
        assert tool_messages[0].tool_input == {"file_path": "/test.py"}

    async def test_early_tool_use_no_duplicate_creation(self, store):
        """add_early_tool_use should not create duplicate if same tool_id exists."""
        await store.start_streaming("s1", initial_messages=[])

        await store.add_early_tool_use("s1", "tool-123", "Read")
        await store.add_early_tool_use("s1", "tool-123", "Read")

        session = store.get_session("s1")
        tool_messages = [m for m in session.messages if m.type == "tool_use"]
        assert len(tool_messages) == 1

    async def test_convert_early_tool_to_subagent(self, store):
        """Early tool_use should be convertible to subagent in-place."""
        await store.start_streaming("s1", initial_messages=[])
        await store.add_early_tool_use("s1", "tool-task-1", "Task")

        converted = await store.convert_tool_to_subagent(
            "s1", "tool-task-1", "agent-1", "Explore", "Search codebase", "Find files"
        )

        assert converted is True
        session = store.get_session("s1")
        # Should be 1 message, now a subagent
        assert len(session.messages) == 1
        msg = session.messages[0]
        assert msg.type == "subagent"
        assert msg.agent_id == "agent-1"
        assert msg.tool_name is None  # Cleared on conversion


# =============================================================================
# Bug: Compaction + stream end double-update crash (KNOWN_ISSUES Root Cause 10)
#
# When compact_boundary sets needs_full_refresh and then end_streaming()
# follows shortly after, the next poll returns BOTH a full refresh
# (initial_message_count=0) AND is_streaming=False. On the frontend,
# this causes two synchronous store updates in the same call stack:
# one from the streaming bridge (full message replacement) and one from
# the onStreamEnd callback (same messages + isStreaming:false).
# The double-update can corrupt Svelte's internal effect linked list,
# freezing the entire UI.
#
# Backend invariant: needs_full_refresh must survive end_streaming() so
# the combined response is correctly formed for the frontend fix to handle.
# =============================================================================

class TestCompactionStreamEndRace:
    """Regression tests for the compaction + stream end race condition."""

    async def test_needs_full_refresh_survives_end_streaming(self, store_with_history):
        """needs_full_refresh must NOT be cleared by end_streaming().

        This is critical: if end_streaming clears the flag, the frontend
        would get a normal delta response after compaction, missing the
        full refresh needed to reconcile post-compaction messages.
        """
        # Compaction boundary sets the flag
        await store_with_history.add_system_message(
            "s1", content="Compacted", subtype="compact_boundary",
        )
        session = store_with_history.get_session("s1")
        assert session.needs_full_refresh is True

        # Streaming ends shortly after (common for end-of-query compaction)
        await store_with_history.end_streaming("s1")

        # Flag must still be set
        session = store_with_history.get_session("s1")
        assert session.needs_full_refresh is True

    async def test_combined_full_refresh_and_stream_end_response(self, store_with_history):
        """When compaction + stream end coincide, the poll must return
        a full refresh (initial_message_count=0) WITH is_streaming=False.

        The frontend must handle this combined response without double-updating.
        """
        session = store_with_history.get_session("s1")
        update_id_before = session.last_update_id

        # Compaction boundary
        await store_with_history.add_system_message(
            "s1", content="Compacted", subtype="compact_boundary",
        )

        # Streaming ends
        await store_with_history.end_streaming("s1")

        # Frontend polls
        result = store_with_history.get_session_since("s1", update_id_before)

        assert result is not None
        # Must be a full refresh
        assert result["initial_message_count"] == 0
        # Must indicate streaming has ended
        assert result["is_streaming"] is False
        # Must include all messages
        assert len(result["messages"]) >= 5

    async def test_full_refresh_consumed_after_stream_end(self, store_with_history):
        """After the combined response is consumed, subsequent polls
        should return normal responses (not full refresh again).
        """
        session = store_with_history.get_session("s1")
        update_id_before = session.last_update_id

        await store_with_history.add_system_message(
            "s1", content="Compacted", subtype="compact_boundary",
        )
        await store_with_history.end_streaming("s1")

        # First poll consumes the flag
        result1 = store_with_history.get_session_since("s1", update_id_before)
        assert result1["initial_message_count"] == 0

        # Second poll should return None (no new changes)
        result2 = store_with_history.get_session_since("s1", result1["last_update_id"])
        assert result2 is None

        # Flag should be cleared
        session = store_with_history.get_session("s1")
        assert session.needs_full_refresh is False
