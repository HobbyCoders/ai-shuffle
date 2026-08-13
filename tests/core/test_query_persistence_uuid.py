"""
Tests for JSONL-UUID propagation through ``app.core.query_persistence``.

The Claude Agent SDK exposes a ``uuid`` attribute on ``AssistantMessage``
and ``UserMessage``. The query engine surfaces those uuids in the
``turn_events`` payload it hands to ``_persist_turn_results``. Each row
builder must carry that uuid down into the DB insert so rewind can later
map ``target_uuid`` -> last-kept DB row id.

These tests drive the (still-missing) uuid threading and will fail on
the current ``main``.
"""

import pytest

from app.core import query_persistence as qp


# =============================================================================
# Helpers
# =============================================================================

def _row_uuid(row: dict) -> str | None:
    """Pull jsonl_uuid from wherever the implementation chooses to put it."""
    direct = row.get("jsonl_uuid")
    if direct:
        return direct
    meta = row.get("metadata") or {}
    return meta.get("jsonl_uuid")


# =============================================================================
# _row_for_tool_use
# =============================================================================

class TestRowForToolUse:
    def test_carries_uuid_from_payload(self):
        row = qp._row_for_tool_use(
            session_id="s1",
            tool_msg={
                "name": "Read",
                "tool_id": "toolu_123",
                "input": {"file_path": "/foo"},
                "uuid": "uuid-tooluse-1",
            },
            profile_id="p1",
        )
        assert _row_uuid(row) == "uuid-tooluse-1"

    def test_carries_uuid_for_subagent(self):
        row = qp._row_for_tool_use(
            session_id="s1",
            tool_msg={
                "name": "Task",
                "tool_id": "toolu_agent",
                "input": {"subagent_type": "Explore", "description": "look"},
                "uuid": "uuid-subagent-1",
            },
            profile_id="p1",
            subagent_children={"toolu_agent": []},
        )
        assert row["role"] == "subagent"
        assert _row_uuid(row) == "uuid-subagent-1"

    def test_missing_uuid_is_ok(self):
        row = qp._row_for_tool_use(
            session_id="s1",
            tool_msg={"name": "Read", "tool_id": "toolu_x", "input": {}},
            profile_id="p1",
        )
        assert _row_uuid(row) is None


# =============================================================================
# _row_for_tool_result
# =============================================================================

class TestRowForToolResult:
    def test_carries_uuid_from_payload(self):
        row = qp._row_for_tool_result(
            session_id="s1",
            tool_msg={
                "name": "Read",
                "tool_id": "toolu_123",
                "output": "file contents",
                "uuid": "uuid-toolresult-1",
            },
            profile_id="p1",
        )
        assert _row_uuid(row) == "uuid-toolresult-1"


# =============================================================================
# _row_for_thinking
# =============================================================================

class TestRowForThinking:
    def test_carries_uuid_from_payload(self):
        row = qp._row_for_thinking(
            session_id="s1",
            tb={"content": "deep thought", "uuid": "uuid-thinking-1"},
            profile_id="p1",
            model="claude-sonnet-4",
        )
        assert _row_uuid(row) == "uuid-thinking-1"
