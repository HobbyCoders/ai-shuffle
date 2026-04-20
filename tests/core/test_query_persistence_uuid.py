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


# =============================================================================
# _persist_turn_results — assistant row
# =============================================================================

@pytest.mark.asyncio
class TestPersistTurnResultsAssistantUUID:
    """
    End-to-end check that ``_persist_turn_results`` carries the assistant
    message's uuid into the batch it hands off for insertion.
    """

    async def test_assistant_row_carries_uuid_from_metadata(self, monkeypatch):
        captured_batches: list[list[dict]] = []

        async def fake_run_sync(fn, *args, **kwargs):
            # add_session_messages_batch is called with a single positional list
            if args:
                captured_batches.append(args[0])
            # Return inserted rows shape for downstream assertions
            return [{**row, "id": i + 1} for i, row in enumerate(args[0] if args else [])]

        async def fake_update_session(*args, **kwargs):
            return None

        # Patch update_session to a no-op (it is also called via run_sync)
        monkeypatch.setattr(qp, "run_sync", fake_run_sync)

        # metadata carries uuid for the assistant row
        metadata = {
            "tokens_in": 10,
            "tokens_out": 20,
            "total_cost_usd": 0.0,
            "model": "claude-sonnet-4",
            "num_turns": 1,
            "jsonl_uuid": "uuid-assistant-final",
        }

        await qp._persist_turn_results(
            session_id="s1",
            sdk_session_id="sdk-1",
            tool_messages=[],
            response_text=["Hello"],
            metadata=metadata,
            profile_id="p1",
            interrupted=False,
            turn_events=[],
        )

        # The last batch captured is the messages batch (the first run_sync
        # call is update_session — it also appends but with different shape).
        # Find the batch that contains an assistant row.
        asst_batch = None
        for batch in captured_batches:
            if isinstance(batch, list) and any(
                isinstance(r, dict) and r.get("role") == "assistant" for r in batch
            ):
                asst_batch = batch
                break
        assert asst_batch is not None, "assistant row was never batched"
        asst_row = next(r for r in asst_batch if r.get("role") == "assistant")
        assert _row_uuid(asst_row) == "uuid-assistant-final"

    async def test_tool_use_row_in_ordered_events_carries_uuid(self, monkeypatch):
        captured_batches: list[list[dict]] = []

        async def fake_run_sync(fn, *args, **kwargs):
            if args:
                captured_batches.append(args[0])
            return [{**row, "id": i + 1} for i, row in enumerate(args[0] if args else [])]

        monkeypatch.setattr(qp, "run_sync", fake_run_sync)

        turn_events = [
            {
                "kind": "tool_use",
                "payload": {
                    "name": "Read",
                    "tool_id": "toolu_1",
                    "input": {"file_path": "/x"},
                    "uuid": "uuid-tu-1",
                },
            },
            {
                "kind": "tool_result",
                "payload": {
                    "name": "Read",
                    "tool_id": "toolu_1",
                    "output": "data",
                    "uuid": "uuid-tr-1",
                },
            },
        ]

        await qp._persist_turn_results(
            session_id="s1",
            sdk_session_id="sdk-1",
            tool_messages=[],
            response_text=["done"],
            metadata={"model": "claude-sonnet-4", "num_turns": 1, "jsonl_uuid": "uuid-asst"},
            profile_id="p1",
            interrupted=False,
            turn_events=turn_events,
        )

        # Find the batch that contains tool rows
        tool_batch = None
        for batch in captured_batches:
            if isinstance(batch, list) and any(
                isinstance(r, dict) and r.get("role") == "tool_use" for r in batch
            ):
                tool_batch = batch
                break
        assert tool_batch is not None, "tool_use row was never batched"
        tu_row = next(r for r in tool_batch if r.get("role") == "tool_use")
        tr_row = next(r for r in tool_batch if r.get("role") == "tool_result")
        assert _row_uuid(tu_row) == "uuid-tu-1"
        assert _row_uuid(tr_row) == "uuid-tr-1"
