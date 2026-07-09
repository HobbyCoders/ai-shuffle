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
from unittest.mock import patch

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

    async def test_compaction_full_refresh_attributed_compaction(self, store_with_history):
        """A COMPACTION full refresh is attributed "compaction" — a COMPLETENESS
        reason the frontend union-merges on. (This replaces the older pin that
        reasons were rewind-only: the client now suppresses its union-merge only
        on DESTRUCTIVE reasons ("rewind"/"discard"); every other value — or none
        — still merges, so attributing compaction cannot cause message loss.)"""
        update_id_before = store_with_history.get_session("s1").last_update_id

        await store_with_history.add_system_message(
            "s1", content="Compacted", subtype="compact_boundary",
        )
        # get_session returns a snapshot copy — re-fetch after each mutation.
        assert store_with_history.get_session("s1").full_refresh_reason == "compaction"

        result = store_with_history.get_session_since("s1", update_id_before)
        assert result["initial_message_count"] == 0
        assert result["full_refresh_reason"] == "compaction"
        # Consumed alongside the one-shot flag.
        assert store_with_history.get_session("s1").full_refresh_reason is None

    async def test_end_streaming_full_refresh_attributed_turn_end(self, store_with_history):
        """end_streaming raises the one-shot full refresh attributed "turn_end"
        (a COMPLETENESS reason). Pre-fix it was unattributed, and the client's
        wholesale replace on an ended full snapshot dropped any rendered cards
        the buffer was momentarily missing — the "message list loses messages
        live, refresh restores them" bug."""
        update_id_before = store_with_history.get_session("s1").last_update_id

        await store_with_history.end_streaming("s1")
        session = store_with_history.get_session("s1")
        assert session.needs_full_refresh is True
        assert session.full_refresh_reason == "turn_end"

        result = store_with_history.get_session_since("s1", update_id_before)
        assert result["initial_message_count"] == 0
        assert result["full_refresh_reason"] == "turn_end"

    async def test_discard_full_refresh_attributed_discard(self, store):
        """discard_empty_user_turn attributes "discard" — DESTRUCTIVE, so the
        client takes the snapshot verbatim and the removed prompt is not
        union-merged back in."""
        await store.start_streaming(
            "s1",
            initial_messages=[
                {"id": "h1", "role": "user", "content": "Hello"},
                {"id": "h2", "role": "assistant", "type": "text", "content": "Hi!"},
            ],
        )
        await store.add_user_message("s1", "stopped prompt", "u-stopped")
        await store.end_streaming("s1", interrupted=True)

        discarded = await store.discard_empty_user_turn("s1")
        assert discarded == "stopped prompt"
        session = store.get_session("s1")
        assert session.needs_full_refresh is True
        # end_streaming ran first with "turn_end"; the destructive discard
        # must have replaced it.
        assert session.full_refresh_reason == "discard"

    async def test_discard_reaches_late_pollers_after_flag_consumed(self, store):
        """The one-shot flag is consumed by whichever device polls first. A
        SECOND poller whose since_update_id predates the discard must still
        get a destructive full snapshot without the discarded prompt (via the
        durable trim marker), not a delta that never mentions the removal."""
        await store.start_streaming(
            "s1",
            initial_messages=[
                {"id": "h1", "role": "user", "content": "Hello"},
                {"id": "h2", "role": "assistant", "type": "text", "content": "Hi!"},
            ],
        )
        pre_discard_id = store.get_session("s1").last_update_id
        await store.add_user_message("s1", "stopped prompt", "u-stopped")
        await store.end_streaming("s1", interrupted=True)
        assert await store.discard_empty_user_turn("s1") == "stopped prompt"

        # Device A consumes the one-shot flag.
        first = store.get_session_since("s1", pre_discard_id)
        assert first["initial_message_count"] == 0
        assert first["full_refresh_reason"] in ("discard", "rewind")

        # Device B polls later with pre-discard knowledge: must get the
        # authoritative destructive snapshot, not a delta.
        second = store.get_session_since("s1", pre_discard_id)
        assert second is not None
        assert second["initial_message_count"] == 0
        assert second["full_refresh_reason"] in ("discard", "rewind")
        assert all(m["id"] != "u-stopped" for m in second["messages"])

    async def test_destructive_reason_not_downgraded_by_turn_end(self, store_with_history):
        """A pending DESTRUCTIVE reason survives a completeness raiser: rewind
        mid-turn followed by end_streaming (before any poll consumed the flag)
        must still ship "rewind" — if "turn_end" won, the client would
        union-merge the trimmed messages right back (rewind visually no-ops)."""
        session = store_with_history.get_session("s1")

        session.flag_full_refresh("rewind")
        session.flag_full_refresh("turn_end")
        assert session.full_refresh_reason == "rewind"

        # A destructive reason may replace anything (incl. another destructive).
        session.flag_full_refresh("discard")
        assert session.full_refresh_reason == "discard"

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
# Bug: Parked sessions do not unpark in the UI (cross-turn update-id starvation)
#
# start_streaming used to build a fresh LiveSession with last_update_id=1 every
# turn. A park-poller holds a since_update_id cursor from the PREVIOUS turn
# (e.g. 50). get_session_since gates on last_update_id <= since_update_id -> None,
# so the unpark follow-up turn's counter (1..~20) never exceeds the stale cursor:
# the poller is permanently starved — it never sees is_streaming=true and never
# receives the follow-up's messages; only a page refresh (cursor 0) recovers.
# Fix: carry the counter forward across turns (start_streaming) and serve a full
# NON-DESTRUCTIVE snapshot to any poller whose cursor predates the turn or came
# from a lost incarnation (get_session_since).
# =============================================================================

class TestCrossTurnUpdateIdMonotonicity:

    async def test_parked_poller_not_starved_across_unpark_followup(self, store):
        """PRIMARY REGRESSION: a park-poller holding a PRIOR turn's cursor must
        still receive the unpark follow-up turn's snapshot, not None."""
        # Turn 1: a few messages that advance the counter, then end.
        await store.start_streaming("s1", initial_messages=[
            {"id": "h1", "role": "user", "content": "Q1"},
        ])
        await store.append_text("s1", "answer one")
        await store.append_text("s1", "answer two")
        await store.end_streaming("s1")

        # The parked poller holds turn 1's final counter as its since_update_id.
        parked_cursor = store.get_session("s1").last_update_id
        assert parked_cursor > 1

        # Turn 2: the unpark follow-up starts with seeded history.
        await store.start_streaming("s1", initial_messages=[
            {"id": "h1", "role": "user", "content": "Q1"},
            {"id": "h2", "role": "assistant", "type": "text", "content": "answer two"},
            {"id": "h3", "role": "user", "content": "<task-notification>done"},
        ])

        result = store.get_session_since("s1", parked_cursor)
        assert result is not None, "parked poller starved: get_session_since returned None"
        assert result["initial_message_count"] == 0
        # NON-destructive: no reason (a short seed must union-merge, not truncate).
        assert result.get("full_refresh_reason") is None
        assert result["is_streaming"] is True

    async def test_counter_carries_forward_across_turns(self, store):
        """After turn 2 starts, last_update_id exceeds turn 1's final counter;
        subsequent appends keep it strictly increasing."""
        await store.start_streaming("s1", initial_messages=[])
        await store.append_text("s1", "one")
        await store.append_text("s1", "two")
        await store.end_streaming("s1")
        turn1_final = store.get_session("s1").last_update_id

        await store.start_streaming("s1", initial_messages=[])
        turn2_start = store.get_session("s1").last_update_id
        assert turn2_start > turn1_final
        # turn_start_update_id marks the counter value at this turn's start.
        assert store.get_session("s1").turn_start_update_id == turn2_start

        await store.append_text("s1", "three")
        assert store.get_session("s1").last_update_id > turn2_start

    async def test_cursor_ahead_of_counter_serves_full_snapshot(self, store):
        """A cursor far larger than the counter (lost incarnation, e.g. server
        restart recreated the store) gets a full NON-DESTRUCTIVE snapshot, not
        None."""
        await store.start_streaming("s1", initial_messages=[
            {"id": "h1", "role": "user", "content": "Q"},
        ])
        await store.append_text("s1", "a")
        far_ahead = store.get_session("s1").last_update_id + 10_000

        result = store.get_session_since("s1", far_ahead)
        assert result is not None
        assert result["initial_message_count"] == 0
        assert result.get("full_refresh_reason") is None

    async def test_equal_cursor_no_op_preserved(self, store):
        """A cursor equal to the counter still yields None (the 304 no-op poll)."""
        await store.start_streaming("s1", initial_messages=[])
        await store.append_text("s1", "a")
        current = store.get_session("s1").last_update_id
        assert store.get_session_since("s1", current) is None

    async def test_same_turn_cursor_returns_delta(self, store):
        """A cursor from WITHIN this turn (>= turn_start_update_id) still gets a
        delta (only the turn's newly streamed messages), not a full snapshot."""
        await store.start_streaming("s1", initial_messages=[
            {"id": "h1", "role": "user", "content": "Q1"},
        ])
        await store.append_text("s1", "a1")
        await store.end_streaming("s1")
        # Turn 2 so turn_start_update_id is a carried-forward (non-1) value.
        await store.start_streaming("s1", initial_messages=[
            {"id": "h1", "role": "user", "content": "Q1"},
            {"id": "h2", "role": "assistant", "type": "text", "content": "a1"},
        ])
        session = store.get_session("s1")
        mid_cursor = session.last_update_id
        assert mid_cursor >= session.turn_start_update_id

        await store.append_text("s1", "new stuff")
        result = store.get_session_since("s1", mid_cursor)
        assert result is not None
        # Delta: history is skipped, so initial_message_count > 0 and only the
        # turn's streamed messages are sliced.
        assert result["initial_message_count"] > 0
        assert result.get("full_refresh_reason") is None
        assert all(m["content"] != "Q1" for m in result["messages"])

    async def test_rewind_snapshot_wins_over_cross_turn_branch(self, store):
        """A destructive rewind must keep its 'rewind' tag even for a parked
        cursor that ALSO predates the turn — the trim branch precedes the new
        cross-turn branch, so the destructive tag survives."""
        # Turn 1 advances the counter; the parked cursor predates turn 2.
        await store.start_streaming("session-1", initial_messages=[
            {"id": "m1", "role": "user", "type": "text", "content": "first", "uuid": "u1"},
        ])
        await store.append_text("session-1", "reply")
        await store.end_streaming("session-1")
        parked_cursor = store.get_session("session-1").last_update_id

        # Turn 2 seeds history, then a rewind trims it mid-turn.
        await store.start_streaming("session-1", initial_messages=[
            {"id": "m1", "role": "user", "type": "text", "content": "first", "uuid": "u1"},
            {"id": "m2", "role": "assistant", "type": "text", "content": "reply1", "uuid": "a1"},
            {"id": "m3", "role": "user", "type": "text", "content": "second", "uuid": "u2"},
        ])
        removed = await store.truncate_after_uuid(
            "session-1", target_uuid="a1", include_response=False, target_role="assistant"
        )
        assert removed is True

        # parked_cursor predates BOTH the trim and turn 2's start; the destructive
        # rewind branch must win (it precedes the cross-turn branch), keeping tag.
        result = store.get_session_since("session-1", parked_cursor)
        assert result is not None
        assert result["initial_message_count"] == 0
        assert result["full_refresh_reason"] == "rewind"

    async def test_stale_cursor_full_snapshot_advances_poller_once(self, store):
        """After a stale-cursor poll receives the full snapshot, adopting the
        payload's last_update_id as the new cursor must make the NEXT poll
        return None — no repeated full serialization on the hot poll path."""
        await store.start_streaming("s1", initial_messages=[
            {"id": "h1", "role": "user", "content": "Q1"},
        ])
        await store.append_text("s1", "a1")
        await store.end_streaming("s1")
        parked_cursor = store.get_session("s1").last_update_id

        await store.start_streaming("s1", initial_messages=[
            {"id": "h1", "role": "user", "content": "Q1"},
            {"id": "h2", "role": "assistant", "type": "text", "content": "a1"},
        ])
        first = store.get_session_since("s1", parked_cursor)
        assert first is not None

        # Poller adopts the served counter; the follow-up poll is a no-op.
        new_cursor = first["last_update_id"]
        assert store.get_session_since("s1", new_cursor) is None

    async def test_cursor_ahead_poll_does_not_steal_pending_full_refresh(self, store):
        """A stale lost-incarnation poller (cursor > counter) must NOT consume
        the one-shot needs_full_refresh flag — it belongs to in-range pollers.
        If it were cleared here, a pending compaction/rewind refresh would be
        stolen from a legitimate in-range poller (multi-device)."""
        await store.start_streaming("s1", initial_messages=[
            {"id": "h1", "role": "user", "content": "Q1"},
        ])
        await store.append_text("s1", "a1")
        in_range_cursor = store.get_session("s1").last_update_id

        # Flag via the public compaction path (get_session returns a COPY, so
        # mutating its return value would never reach the store's session).
        await store.add_system_message(
            "s1", content="Context compacted", subtype="compact_boundary"
        )
        assert store.get_session("s1").needs_full_refresh is True

        # Stale poller polls with a cursor ahead of the counter: gets a plain
        # non-destructive snapshot, and the one-shot flag survives.
        ahead = store.get_session("s1").last_update_id + 10_000
        stale = store.get_session_since("s1", ahead)
        assert stale is not None
        assert stale.get("full_refresh_reason") is None
        assert store.get_session("s1").needs_full_refresh is True

        # The in-range poller still receives the compaction refresh with reason.
        legit = store.get_session_since("s1", in_range_cursor)
        assert legit is not None
        assert legit["full_refresh_reason"] == "compaction"
        assert legit["initial_message_count"] == 0


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


# =============================================================================
# Bug: Duplicate compaction messages on session resume with streaming replay
#
# When a session's JSONL contains a compact_boundary message and streaming
# replays that same event, the message can be added twice:
# 1. From JSONL history (via initial_messages in start_streaming)
# 2. From streamed event (via handle_system -> add_system_message)
#
# The duplicate has different IDs (original UUID vs random system-*) so
# no dedup logic catches it. Result: message appears twice in UI.
# =============================================================================

class TestDuplicateCompactionMessages:

    async def test_no_duplicate_compact_boundary_on_stream_replay(self, store):
        """When streaming replays a compact_boundary that's already in history,
        should not add it twice.

        Reproduces bug: session loaded from JSONL with compact_boundary,
        then same event arrives in stream.
        """
        # Simulate: JSONL history with a compact_boundary message
        history = [
            {
                "id": "user-1",
                "role": "user",
                "type": "text",
                "content": "Hello",
                "streaming": False,
            },
            {
                "id": "compact-uuid-123",
                "role": "system",
                "type": "system",
                "subtype": "compact_boundary",
                "systemSubtype": "compact_boundary",
                "content": "Context was automatically compacted.",
                "systemData": {
                    "compact_metadata": {
                        "trigger": "auto",
                        "pre_tokens": 167000,
                    }
                },
                "streaming": False,
            },
            {
                "id": "user-2",
                "role": "user",
                "type": "text",
                "content": "Continue...",
                "streaming": False,
            },
        ]

        # Start streaming with history
        await store.start_streaming("s1", initial_messages=history)
        session = store.get_session("s1")

        # Verify history loaded correctly
        assert len(session.messages) == 3
        compact_msgs = [m for m in session.messages if m.system_subtype == "compact_boundary"]
        assert len(compact_msgs) == 1
        assert compact_msgs[0].content == "Context was automatically compacted."

        # Now simulate: same compact_boundary event arrives in stream
        # (this can happen due to timing or SDK re-sending)
        await store.add_system_message(
            "s1",
            content="Context was automatically compacted.",
            subtype="compact_boundary",
            data={"compact_metadata": {"trigger": "auto", "pre_tokens": 167000}},
        )

        # VERIFY: no duplicate was added
        # Should still have 3 messages (1 user + 1 compact + 1 user)
        session = store.get_session("s1")
        assert len(session.messages) == 3, f"Expected 3 messages but got {len(session.messages)}"

        # Should still have only 1 compact_boundary
        compact_msgs = [m for m in session.messages if m.system_subtype == "compact_boundary"]
        assert len(compact_msgs) == 1, f"Expected 1 compact_boundary but got {len(compact_msgs)}"

    async def test_no_duplicate_microcompact_boundary_on_stream_replay(self, store):
        """Similar to compact_boundary: microcompact_boundary should not duplicate."""
        history = [
            {
                "id": "user-1",
                "role": "user",
                "type": "text",
                "content": "Hello",
                "streaming": False,
            },
            {
                "id": "microcompact-uuid-456",
                "role": "system",
                "type": "system",
                "subtype": "microcompact_boundary",
                "systemSubtype": "microcompact_boundary",
                "content": "Context was microcompacted.",
                "systemData": {
                    "microcompactMetadata": {
                        "trigger": "auto",
                        "tokensSaved": 5000,
                        "preTokens": 100000,
                    }
                },
                "streaming": False,
            },
        ]

        await store.start_streaming("s1", initial_messages=history)
        session = store.get_session("s1")

        # Verify history loaded
        assert len(session.messages) == 2
        microcompact_msgs = [m for m in session.messages if m.system_subtype == "microcompact_boundary"]
        assert len(microcompact_msgs) == 1

        # Stream replays the same event
        await store.add_system_message(
            "s1",
            content="Context was microcompacted.",
            subtype="microcompact_boundary",
            data={
                "microcompactMetadata": {
                    "trigger": "auto",
                    "tokensSaved": 5000,
                    "preTokens": 100000,
                }
            },
        )

        # No duplicate
        session = store.get_session("s1")
        assert len(session.messages) == 2
        microcompact_msgs = [m for m in session.messages if m.system_subtype == "microcompact_boundary"]
        assert len(microcompact_msgs) == 1

    async def test_different_compaction_events_are_both_added(self, store):
        """Edge case: different compaction events (different token counts) should
        both appear — they're not duplicates, just similar types.
        """
        history = [
            {
                "id": "compact-1",
                "role": "system",
                "type": "system",
                "subtype": "compact_boundary",
                "content": "First compaction.",
                "systemData": {
                    "compact_metadata": {"trigger": "auto", "pre_tokens": 100000}
                },
                "streaming": False,
            },
        ]

        await store.start_streaming("s1", initial_messages=history)
        session = store.get_session("s1")
        assert len(session.messages) == 1

        # Add a DIFFERENT compaction event (different token count = different event)
        await store.add_system_message(
            "s1",
            content="Second compaction.",
            subtype="compact_boundary",
            data={"compact_metadata": {"trigger": "auto", "pre_tokens": 50000}},
        )

        # Both should be added (different events)
        session = store.get_session("s1")
        assert len(session.messages) == 2
        compact_msgs = [m for m in session.messages if m.system_subtype == "compact_boundary"]
        assert len(compact_msgs) == 2

    async def test_dedup_matches_jsonl_snake_case_against_stream_camel_case(self, store):
        """Real-world scenario: JSONL parser stores compact_boundary metadata
        under snake_case (compact_metadata.pre_tokens), but the SDK stream
        emits the raw camelCase shape (compactMetadata.preTokens).

        The dedup helper must recognize these as the same event, otherwise
        the fix does nothing in production for compact_boundary.
        """
        history = [
            {
                "id": "compact-uuid-xyz",
                "role": "system",
                "type": "system",
                "subtype": "compact_boundary",
                "systemSubtype": "compact_boundary",
                "content": "Context was automatically compacted.",
                "systemData": {
                    # As normalized by jsonl_parser (snake_case)
                    "compact_metadata": {
                        "trigger": "auto",
                        "pre_tokens": 167000,
                    }
                },
                "streaming": False,
            },
        ]

        await store.start_streaming("s1", initial_messages=history)
        assert len(store.get_session("s1").messages) == 1

        # Stream replay arrives with raw SDK camelCase shape
        await store.add_system_message(
            "s1",
            content="Context was automatically compacted.",
            subtype="compact_boundary",
            data={"compactMetadata": {"trigger": "auto", "preTokens": 167000}},
        )

        session = store.get_session("s1")
        compact_msgs = [m for m in session.messages if m.system_subtype == "compact_boundary"]
        assert len(compact_msgs) == 1, (
            f"Dedup must bridge snake_case vs camelCase; got {len(compact_msgs)} messages"
        )

    async def test_missing_metadata_does_not_collapse_distinct_events(self, store):
        """Two compact_boundary events that both lack identifying token counts
        must NOT be treated as duplicates. Without a token we cannot positively
        identify a duplicate, so we err on the side of keeping both.
        """
        history = [
            {
                "id": "compact-a",
                "role": "system",
                "type": "system",
                "subtype": "compact_boundary",
                "systemSubtype": "compact_boundary",
                "content": "First.",
                "systemData": {"compact_metadata": {"trigger": "manual"}},
                "streaming": False,
            },
        ]
        await store.start_streaming("s1", initial_messages=history)

        await store.add_system_message(
            "s1",
            content="Second.",
            subtype="compact_boundary",
            data={"compact_metadata": {"trigger": "manual"}},
        )

        session = store.get_session("s1")
        compact_msgs = [m for m in session.messages if m.system_subtype == "compact_boundary"]
        assert len(compact_msgs) == 2, (
            "Events with missing pre_tokens must not collapse via None==None"
        )

    async def test_duplicate_boundary_does_not_latch_needs_full_refresh(self, store):
        """When a duplicate compact_boundary is skipped, needs_full_refresh
        must NOT be flipped on. Otherwise the flag latches onto the next
        unrelated update and forces a spurious full refresh on the frontend.
        """
        history = [
            {
                "id": "compact-uuid-1",
                "role": "system",
                "type": "system",
                "subtype": "compact_boundary",
                "systemSubtype": "compact_boundary",
                "content": "Compacted.",
                "systemData": {
                    "compact_metadata": {"trigger": "auto", "pre_tokens": 42000}
                },
                "streaming": False,
            },
        ]
        await store.start_streaming("s1", initial_messages=history)

        # Ensure flag starts clean after history load
        session = store.get_session("s1")
        session.needs_full_refresh = False

        # Replay the same boundary — should be deduplicated without mutating state
        await store.add_system_message(
            "s1",
            content="Compacted.",
            subtype="compact_boundary",
            data={"compact_metadata": {"trigger": "auto", "pre_tokens": 42000}},
        )

        session = store.get_session("s1")
        assert session.needs_full_refresh is False, (
            "Duplicate boundary must not flip needs_full_refresh — flag would "
            "latch onto the next unrelated update."
        )


# =============================================================================
# Bug: thinking-token counter attaches to the wrong thinking block
#
# update_thinking_tokens omitted the `streaming` filter that append_thinking /
# finalize_thinking both use. Finalized thinking blocks stay in session.messages
# (finalize only sets streaming=False), so in multi-turn chats or a tool-use
# loop (think -> tool -> think again) a thinking_tokens estimate arriving before
# the new block's first delta would overwrite the PREVIOUS finalized block's
# counter. Fix: only target the currently STREAMING thinking block; otherwise
# no-op (the handler drops the estimate when there's no active block).
# =============================================================================

class TestThinkingTokenRouting:

    async def test_update_targets_streaming_block_not_finalized(self, store):
        await store.start_streaming("s1")

        # Turn 1: a thinking block that gets finalized with its own count.
        await store.append_thinking("s1", "turn 1 reasoning")
        await store.finalize_thinking("s1")
        turn1 = store.get_session("s1").messages[-1]
        turn1.thinking_tokens = 100  # simulate the count it earned while live

        # Turn 2: a NEW streaming thinking block.
        await store.append_thinking("s1", "turn 2 reasoning")

        result = await store.update_thinking_tokens("s1", 250)
        assert result is True

        messages = store.get_session("s1").messages
        thinking = [m for m in messages if m.type == "thinking"]
        assert len(thinking) == 2

        turn1_block, turn2_block = thinking[0], thinking[1]
        assert turn2_block.streaming is True
        assert turn2_block.thinking_tokens == 250, "estimate must land on streaming block"
        assert turn1_block.streaming is False
        assert turn1_block.thinking_tokens == 100, "finalized block must be UNCHANGED"

    async def test_update_is_noop_without_streaming_block(self, store):
        await store.start_streaming("s1")

        # A finalized thinking block exists, but nothing is streaming.
        await store.append_thinking("s1", "done thinking")
        await store.finalize_thinking("s1")
        block = store.get_session("s1").messages[-1]
        block.thinking_tokens = 42
        update_id_before = store.get_session("s1").last_update_id

        result = await store.update_thinking_tokens("s1", 999)
        assert result is False, "must drop the estimate when no block is streaming"

        block_after = store.get_session("s1").messages[-1]
        assert block_after.thinking_tokens == 42, "finalized block must be UNCHANGED"
        assert store.get_session("s1").last_update_id == update_id_before, (
            "no-op must not bump update_id"
        )


# =============================================================================
# Bug: Messages mixing up — content bleeding between assistant messages.
#
# Two root causes (see investigation):
#  #2  Two streaming text messages could coexist; append_text routes deltas to
#      "the last streaming text message", so a later delta bleeds into the wrong
#      one. start_assistant_message must finalize the prior streaming text.
#  #4  start_streaming replaced the per-session lock on a same-session re-query,
#      breaking mutual exclusion with an in-flight writer holding the old lock.
# =============================================================================


class TestSingleStreamingTextInvariant:
    """At most one streaming assistant text message may exist at a time."""

    async def test_start_assistant_message_finalizes_prior_streaming_text(self, store):
        await store.start_streaming("s1", initial_messages=[])
        await store.append_text("s1", "first block")

        # A new assistant message starts while the first text is still streaming.
        await store.start_assistant_message("s1", "asst-2")

        session = store.get_session("s1")
        streaming_text = [
            m for m in session.messages
            if m.type == "text" and m.role == "assistant" and m.streaming
        ]
        assert len(streaming_text) == 1, (
            "at most one streaming text message may exist; "
            f"found {len(streaming_text)}"
        )
        assert streaming_text[0].id == "asst-2", "the new message is the live one"

    async def test_deltas_do_not_bleed_into_a_prior_text_message(self, store):
        await store.start_streaming("s1", initial_messages=[])
        await store.append_text("s1", "ANSWER-A")

        # New assistant text begins, then its deltas arrive.
        await store.start_assistant_message("s1", "asst-2")
        await store.append_text("s1", "ANSWER-B")

        texts = [
            m for m in store.get_session("s1").messages
            if m.type == "text" and m.role == "assistant"
        ]
        first = next(m for m in texts if m.id != "asst-2")
        second = next(m for m in texts if m.id == "asst-2")
        assert first.content == "ANSWER-A", "prior message must not absorb new deltas"
        assert second.content == "ANSWER-B", "new deltas land only on the new message"


# =============================================================================
# Bug A/B: a PARKED session (parked on in-flight background work) that is
# force-evicted from the pool — hard-cap LRU force-eviction (reason "lru_force")
# or a dead subprocess (reason "dead") — destroys the pooled SDK client. That
# client was the ONLY thing that could deliver the background work's terminal
# notification, so task_registry.has_active_background_work() stays True FOREVER.
# The plain cleanup_ended_session() background-work guard then refuses cleanup
# permanently: the live buffer + task registry leak, and the UI's task strip is
# stranded on a task that can never complete. `force=True` reaps the orphaned
# tasks to "killed" and tears the session down anyway.
#
# `force` flows from resource_monitor._cleanup_evicted_sessions, which sets it
# only for the "lru_force"/"dead" reasons (normal lru/idle eviction skip parked
# sessions, so they never need it).
# =============================================================================

class TestForceCleanupOrphanedBackgroundWork:

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        # cleanup_ended_session's guard + clear_session use the GLOBAL singleton.
        from app.core.task_registry import task_registry
        task_registry.reset_all()
        yield
        task_registry.reset_all()

    async def test_guard_blocks_plain_cleanup_of_parked_session(self, store):
        """Without force, a parked session's cleanup is refused — the leak."""
        from app.core.task_registry import task_registry

        await store.start_streaming("s1", initial_messages=[])
        await store.end_streaming("s1")
        # Parked on an in-flight background sub-agent.
        task_registry.start_task("s1", "agent-1", "agent", "bg work")
        assert task_registry.has_active_background_work("s1") is True

        result = await store.cleanup_ended_session("s1")
        assert result is False
        assert store.get_session("s1") is not None  # leaked, by design pre-fix

    async def test_force_cleanup_reaps_orphans_and_removes_session(self, store):
        """force=True tears down the session AND reaps the orphaned task."""
        from app.core.task_registry import task_registry

        await store.start_streaming("s1", initial_messages=[])
        await store.end_streaming("s1")
        task_registry.start_task("s1", "agent-1", "agent", "bg work")

        result = await store.cleanup_ended_session("s1", force=True)
        assert result is True
        assert store.get_session("s1") is None
        # Registry cleared for the session -> the next /live returns no
        # activeTasks, so the UI strip clears instead of stranding.
        assert task_registry.get_active("s1") == []
        assert task_registry.has_active_background_work("s1") is False

    async def test_force_still_refuses_a_restreamed_session(self, store):
        """force must NEVER wipe a session a brand-new query just took over."""
        from app.core.task_registry import task_registry

        await store.start_streaming("s1", initial_messages=[])
        await store.end_streaming("s1")
        task_registry.start_task("s1", "agent-1", "agent", "bg work")
        # A new query takes over the same session id.
        await store.start_streaming("s1", initial_messages=[])

        result = await store.cleanup_ended_session("s1", force=True)
        assert result is False
        assert store.get_session("s1") is not None
        assert store.is_streaming("s1") is True

    async def test_force_cleanup_of_session_without_tasks_is_fine(self, store):
        """force on a session with no background work just removes it (no error)."""
        await store.start_streaming("s1", initial_messages=[])
        await store.end_streaming("s1")

        result = await store.cleanup_ended_session("s1", force=True)
        assert result is True
        assert store.get_session("s1") is None


class TestPerSessionLockStability:
    """The per-session lock must be stable for the session's whole lifetime."""

    async def test_lock_is_reused_across_same_session_requery(self, store):
        await store.start_streaming("s1", initial_messages=[])
        lock_before = store._get_session_lock("s1")

        # Rapid re-query on the same session id (new turn).
        await store.start_streaming("s1", initial_messages=[])
        lock_after = store._get_session_lock("s1")

        assert lock_before is lock_after, (
            "replacing the lock on re-query breaks mutual exclusion with an "
            "in-flight writer still holding the old lock"
        )


# =============================================================================
# Bug: "messages vanish after a compaction point while watching a live session"
#
# On SDK compaction, add_system_message(compact_boundary) sets
# needs_full_refresh; the next /live poll returns a FULL snapshot with
# initial_message_count=0, which the frontend treats as a full REPLACE. If the
# turn-start seed was SHORTER than the durable Postgres store transcript the
# client already renders (store-parse failure swallowed to [], a NULL
# sdk_session_id turn, or a crash-truncated tier-1 snapshot), that replace wipes
# the pre-compaction assistant/tool history.
#
# Fix: heal the seed against the durable store transcript ("longer wins"),
# mirroring the non-streaming /live read-path heal — so the buffer the
# compaction snapshot serializes is always at least as complete as the durable
# transcript. See app/core/message_resolver.heal_seed_against_store.
# =============================================================================

# A durable store transcript with a full turn: user + assistant text + tool_use.
_DURABLE_TRANSCRIPT = [
    {"id": "user-hist", "role": "user", "type": "text", "content": "Analyze this"},
    {"id": "text-hist", "role": "assistant", "type": "text", "content": "Analysis"},
    {
        "id": "tool-hist",
        "role": "assistant",
        "type": "tool_use",
        "content": "Using tool: Read",
        "toolName": "Read",
        "toolId": "tool-hist",
    },
]


class TestSeedHealAgainstDurableTranscript:
    """message_resolver seed heal — turn-start seed must never be shorter than
    the durable store transcript when postgres_only + a usable sdk_session_id."""

    def test_store_parse_failure_does_not_wipe_transcript(self, monkeypatch):
        """(a) A one-time store-parse raise must NOT seed [] — resolve_for_
        streaming_init falls through to the JSONL reader and returns the durable
        transcript, not an empty seed."""
        import app.core.config as config_mod
        import app.core.message_resolver as mr

        monkeypatch.setattr(config_mod.settings, "session_store_mode", "postgres_only")

        # No tier-1 snapshot in the live store for this session.
        fresh_store = LiveSessionStore()
        monkeypatch.setattr(mr, "live_session_store", fresh_store, raising=False)

        def _boom(*_a, **_k):
            raise RuntimeError("transient store read failure")

        # Store parse raises. The JSONL reader (fallthrough) returns the durable
        # transcript so visible history survives the failure.
        with patch(
            "app.core.jsonl_parser.parse_session_history_from_store", side_effect=_boom
        ), patch(
            "app.core.jsonl_parser.parse_session_history",
            return_value=(list(_DURABLE_TRANSCRIPT), {}),
        ):
            seed = mr.MessageResolver.resolve_for_streaming_init(
                "sess-fail", sdk_session_id="sdk-1", working_dir="/workspace"
            )

        assert seed, "store-parse failure must not produce an empty seed"
        assert [m["id"] for m in seed] == [m["id"] for m in _DURABLE_TRANSCRIPT]

    def test_ended_snapshot_shorter_than_store_heals_to_longer(self, monkeypatch):
        """(b) An ended live session whose kept snapshot has FEWER messages than
        the store transcript must have its next-turn seed healed to the longer
        durable transcript (longer wins), not the truncated snapshot."""
        import app.core.config as config_mod
        import app.core.message_resolver as mr

        monkeypatch.setattr(config_mod.settings, "session_store_mode", "postgres_only")

        # A crash-truncated tier-1 snapshot: only the user prompt survived.
        truncated_snapshot = [_DURABLE_TRANSCRIPT[0]]

        with patch(
            "app.core.jsonl_parser.parse_session_history_from_store",
            return_value=(list(_DURABLE_TRANSCRIPT), {}),
        ):
            healed = mr.heal_seed_against_store(
                list(truncated_snapshot), "sdk-1", "/workspace"
            )

        assert [m["id"] for m in healed] == [m["id"] for m in _DURABLE_TRANSCRIPT], (
            "seed must heal to the longer durable transcript"
        )

    def test_longer_snapshot_wins_over_store(self, monkeypatch):
        """Tier-1 semantics preserved: a snapshot that is genuinely LONGER than
        the store transcript (post-turn, mirror not yet flushed) is kept — the
        heal never shrinks a seed."""
        import app.core.config as config_mod
        import app.core.message_resolver as mr

        monkeypatch.setattr(config_mod.settings, "session_store_mode", "postgres_only")

        longer_snapshot = _DURABLE_TRANSCRIPT + [
            {"id": "text-fresh", "role": "assistant", "type": "text", "content": "new"}
        ]
        with patch(
            "app.core.jsonl_parser.parse_session_history_from_store",
            return_value=(list(_DURABLE_TRANSCRIPT), {}),
        ):
            healed = mr.heal_seed_against_store(
                list(longer_snapshot), "sdk-1", "/workspace"
            )

        assert [m["id"] for m in healed] == [m["id"] for m in longer_snapshot]

    def test_heal_noop_outside_postgres_only(self, monkeypatch):
        """The heal must no-op (and never touch the store) outside postgres_only."""
        import app.core.config as config_mod
        import app.core.message_resolver as mr

        monkeypatch.setattr(config_mod.settings, "session_store_mode", "mirror")

        snapshot = [_DURABLE_TRANSCRIPT[0]]
        with patch(
            "app.core.jsonl_parser.parse_session_history_from_store"
        ) as parse_mock:
            healed = mr.heal_seed_against_store(list(snapshot), "sdk-1", "/workspace")
        parse_mock.assert_not_called()
        assert healed == snapshot

    def test_empty_seed_heals_to_durable(self, monkeypatch):
        """An empty seed (store-parse swallowed to [] / NULL sdk turn) always
        heals to the durable transcript — nothing to anchor against."""
        import app.core.config as config_mod
        import app.core.message_resolver as mr

        monkeypatch.setattr(config_mod.settings, "session_store_mode", "postgres_only")

        with patch(
            "app.core.jsonl_parser.parse_session_history_from_store",
            return_value=(list(_DURABLE_TRANSCRIPT), {}),
        ):
            healed = mr.heal_seed_against_store([], "sdk-1", "/workspace")

        assert [m["id"] for m in healed] == [m["id"] for m in _DURABLE_TRANSCRIPT]


# =============================================================================
# BUG 2: heal_seed_against_store "longer wins" destroys preserved interrupted
# tails.
#
# The live buffer coalesces a streamed turn's text into ONE message; the store
# parse emits one row PER text block per assistant message (text-{uuid}-{n})
# plus system-ish user rows the buffer never held. So after interrupt_for_query
# the durable transcript can be numerically LONGER while MISSING the interrupted
# turn's unflushed tail that end_streaming deliberately preserved in memory. A
# bare longer-wins destroys that tail (and the id-family switch duplicates cards
# client-side). Fix: anchored superset — durable may only win when it is longer
# AND carries the LAST transcript uuid the snapshot holds.
# =============================================================================


class TestSeedHealAnchoredSuperset:
    """The heal is anchored on the snapshot's last transcript uuid — 'longer'
    alone is not enough to let the durable transcript win."""

    def test_longer_durable_missing_anchor_keeps_seed(self, monkeypatch):
        """Durable is longer (finer per-block rows) but does NOT contain the
        snapshot's anchor uuid — it is missing the interrupted tail. The seed
        must win to preserve that tail and avoid duplicate cards."""
        import app.core.config as config_mod
        import app.core.message_resolver as mr

        monkeypatch.setattr(config_mod.settings, "session_store_mode", "postgres_only")

        # Snapshot: end_streaming backfilled transcript uuids onto the coalesced
        # live turn, INCLUDING the interrupted tail (text-live, uuid a-tail).
        snapshot = [
            {"id": "user-live", "role": "user", "type": "text",
             "content": "Do it", "uuid": "u-req"},
            {"id": "text-live", "role": "assistant", "type": "text",
             "content": "Partial interrupted answer", "uuid": "a-tail"},
        ]
        # Durable is LONGER (one row per text block + an extra system-ish user
        # row) but is from the PRE-interrupt turn — it never saw a-tail.
        durable = [
            {"id": "user-hist", "role": "user", "type": "text",
             "content": "Do it", "uuid": "u-req"},
            {"id": "text-abc-0", "role": "assistant", "type": "text",
             "content": "Block 0", "uuid": "abc"},
            {"id": "text-abc-1", "role": "assistant", "type": "text",
             "content": "Block 1", "uuid": "abc"},
            {"id": "sys-user", "role": "user", "type": "text",
             "content": "<system reminder>", "uuid": "sys"},
        ]
        assert len(durable) > len(snapshot)

        with patch(
            "app.core.jsonl_parser.parse_session_history_from_store",
            return_value=(list(durable), {}),
        ):
            healed = mr.heal_seed_against_store(list(snapshot), "sdk-1", "/workspace")

        assert [m["id"] for m in healed] == [m["id"] for m in snapshot], (
            "durable missing the snapshot anchor must NOT clobber the interrupted tail"
        )

    def test_longer_durable_with_anchor_wins(self, monkeypatch):
        """Durable is longer AND carries the snapshot's anchor uuid — it is a
        genuine superset (fully-flushed turn), so it wins as before."""
        import app.core.config as config_mod
        import app.core.message_resolver as mr

        monkeypatch.setattr(config_mod.settings, "session_store_mode", "postgres_only")

        snapshot = [
            {"id": "user-live", "role": "user", "type": "text",
             "content": "Do it", "uuid": "u-req"},
            {"id": "text-live", "role": "assistant", "type": "text",
             "content": "Answer", "uuid": "a-done"},
        ]
        # Durable contains the anchor a-done AND more (a later tool row) — a real
        # superset the mirror flushed after the snapshot was captured.
        durable = [
            {"id": "user-hist", "role": "user", "type": "text",
             "content": "Do it", "uuid": "u-req"},
            {"id": "text-a-done-0", "role": "assistant", "type": "text",
             "content": "Answer", "uuid": "a-done"},
            {"id": "tool-later", "role": "assistant", "type": "tool_use",
             "content": "Using tool: Read", "uuid": "t-later"},
        ]
        assert len(durable) > len(snapshot)

        with patch(
            "app.core.jsonl_parser.parse_session_history_from_store",
            return_value=(list(durable), {}),
        ):
            healed = mr.heal_seed_against_store(list(snapshot), "sdk-1", "/workspace")

        assert [m["id"] for m in healed] == [m["id"] for m in durable], (
            "durable containing the anchor is a superset and must win"
        )

    def test_no_anchor_uuids_falls_back_to_longer_wins(self, monkeypatch):
        """A snapshot with NO transcript uuids (crash-truncated tier-1 buffer,
        backfill never ran) cannot be anchored — fall back to the original
        longer-wins so the genuine crash-truncation heal still fires."""
        import app.core.config as config_mod
        import app.core.message_resolver as mr

        monkeypatch.setattr(config_mod.settings, "session_store_mode", "postgres_only")

        # No uuid keys anywhere — matches the historical _DURABLE_TRANSCRIPT shape.
        truncated_snapshot = [_DURABLE_TRANSCRIPT[0]]
        with patch(
            "app.core.jsonl_parser.parse_session_history_from_store",
            return_value=(list(_DURABLE_TRANSCRIPT), {}),
        ):
            healed = mr.heal_seed_against_store(
                list(truncated_snapshot), "sdk-1", "/workspace"
            )

        assert [m["id"] for m in healed] == [m["id"] for m in _DURABLE_TRANSCRIPT], (
            "uuid-less snapshot must fall back to longer-wins crash-truncation heal"
        )

    def test_equal_length_keeps_seed_even_with_anchor(self, monkeypatch):
        """When durable is not strictly longer, the seed wins outright — the
        anchor check only gates the longer-durable case."""
        import app.core.config as config_mod
        import app.core.message_resolver as mr

        monkeypatch.setattr(config_mod.settings, "session_store_mode", "postgres_only")

        snapshot = [
            {"id": "user-live", "role": "user", "type": "text",
             "content": "Do it", "uuid": "u-req"},
            {"id": "text-live", "role": "assistant", "type": "text",
             "content": "Answer", "uuid": "a-done"},
        ]
        durable = [
            {"id": "user-hist", "role": "user", "type": "text",
             "content": "Do it", "uuid": "u-req"},
            {"id": "text-a-done-0", "role": "assistant", "type": "text",
             "content": "Answer", "uuid": "a-done"},
        ]
        with patch(
            "app.core.jsonl_parser.parse_session_history_from_store",
            return_value=(list(durable), {}),
        ):
            healed = mr.heal_seed_against_store(list(snapshot), "sdk-1", "/workspace")

        assert [m["id"] for m in healed] == [m["id"] for m in snapshot]


class TestCompactionSnapshotContainsSeededHistory:
    """(c) Full pipeline: seeded history must survive the compaction-triggered
    full snapshot returned by get_session_since (the actual bug scenario)."""

    async def test_seeded_history_survives_compaction_full_snapshot(self, store):
        # Seed a fresh turn from the durable transcript (as the seed path would
        # after the heal), then stream a user + assistant + tool turn.
        await store.start_streaming("s1", initial_messages=list(_DURABLE_TRANSCRIPT))
        update_id_before = store.get_session("s1").last_update_id

        await store.add_user_message("s1", "Do the thing", "user-live")
        await store.append_text("s1", "Working on it")

        # SDK compaction boundary arrives mid-live-session.
        await store.add_system_message(
            "s1", content="Context compacted", subtype="compact_boundary"
        )

        # The next non-304 poll consumes needs_full_refresh → FULL snapshot.
        result = store.get_session_since("s1", update_id_before)
        assert result is not None
        assert result["initial_message_count"] == 0  # frontend full-replace

        ids = [m["id"] for m in result["messages"]]
        # All seeded history ids must be present in the full snapshot — the buffer
        # the snapshot serializes carries them, so a frontend full-replace against
        # THIS snapshot cannot wipe the pre-compaction assistant/tool history.
        for expected in ("user-hist", "text-hist", "tool-hist"):
            assert expected in ids, f"seeded history id {expected} missing from snapshot"
        # And the new turn's user prompt + compaction card are present too.
        assert "user-live" in ids
        assert any(m.get("systemSubtype") == "compact_boundary" for m in result["messages"])
