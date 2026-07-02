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


@pytest.fixture(autouse=True)
def _force_legacy_message_batch(monkeypatch):
    """Pin the session store mode off ``postgres_only`` for this module.

    _persist_turn_results gates the legacy ``session_messages`` batch write with
    ``settings.session_store_mode != "postgres_only"`` (a temporary gate while
    postgres_only stabilises). The deployment environment leaks
    ``SESSION_STORE_MODE=postgres_only``, which would silently skip that batch and
    make these row-batching assertions fail. Pin a legacy value so the batch runs.
    """
    monkeypatch.setattr(qp.settings, "session_store_mode", "disabled")


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


# =============================================================================
# _persist_turn_results — ordered text/tool sequence (reorder-on-reload fix)
# =============================================================================

@pytest.mark.asyncio
class TestPersistTurnResultsOrderedText:
    """A text -> tool_use -> text turn must persist as three rows in stream
    order (text, tool, text) — NOT as [tool, merged-text]. The old code
    appended all assistant text as a single merged row AFTER the tools, which
    on reload (DB fast-path) hoisted the tool card above the text and merged
    the two distinct text blocks into one bubble.
    """

    async def _run_and_capture(self, monkeypatch, *, turn_events, response_text,
                               interrupted=False):
        captured_batches: list[list[dict]] = []

        async def fake_run_sync(fn, *args, **kwargs):
            if args:
                captured_batches.append(args[0])
            return [{**row, "id": i + 1} for i, row in enumerate(args[0] if args else [])]

        monkeypatch.setattr(qp, "run_sync", fake_run_sync)

        await qp._persist_turn_results(
            session_id="s1",
            sdk_session_id="sdk-1",
            tool_messages=[],
            response_text=response_text,
            metadata={
                "model": "claude-sonnet-4",
                "num_turns": 1,
                "total_cost_usd": 0.012,
                "duration_ms": 1234,
                "jsonl_uuid": "uuid-asst-final",
            },
            profile_id="p1",
            interrupted=interrupted,
            turn_events=turn_events,
        )

        # The message batch is the one containing assistant/tool rows.
        for batch in captured_batches:
            if isinstance(batch, list) and any(
                isinstance(r, dict) and r.get("role") in ("assistant", "tool_use")
                for r in batch
            ):
                return batch
        raise AssertionError("message batch was never captured")

    async def test_text_tool_text_persists_in_order(self, monkeypatch):
        turn_events = [
            {"kind": "text", "payload": {"content": "I'll run a quick check.", "uuid": "u2"}},
            {"kind": "tool_use", "payload": {"name": "Bash", "tool_id": "toolu_01BASQ", "input": {"command": "ls"}, "uuid": "u3"}},
            {"kind": "tool_result", "payload": {"name": "Bash", "tool_id": "toolu_01BASQ", "output": "ok", "uuid": "u4"}},
            {"kind": "text", "payload": {"content": "Output confirmed: ok", "uuid": "u5"}},
        ]
        batch = await self._run_and_capture(
            monkeypatch, turn_events=turn_events,
            response_text=["I'll run a quick check.", "Output confirmed: ok"],
        )

        # Exactly two assistant text rows, in order, NOT merged.
        roles = [r["role"] for r in batch]
        assert roles == ["assistant", "tool_use", "tool_result", "assistant"], roles
        assert batch[0]["content"] == "I'll run a quick check."
        assert batch[3]["content"] == "Output confirmed: ok"
        # No single merged row containing both blocks.
        assert all("\n".join(["I'll run a quick check.", "Output confirmed: ok"]) != r["content"]
                   for r in batch)
        # Closing metadata (cost/duration) lands on the LAST text row.
        assert batch[3]["metadata"].get("total_cost_usd") == 0.012
        assert batch[3]["metadata"].get("duration_ms") == 1234
        # Intermediate text row does NOT carry cost (so cost renders once, on final bubble).
        assert "total_cost_usd" not in (batch[0]["metadata"] or {})

    async def test_turn_ending_on_text_has_no_extra_merged_row(self, monkeypatch):
        turn_events = [
            {"kind": "text", "payload": {"content": "first", "uuid": "u2"}},
            {"kind": "tool_use", "payload": {"name": "Bash", "tool_id": "t1", "input": {}, "uuid": "u3"}},
            {"kind": "text", "payload": {"content": "second", "uuid": "u5"}},
        ]
        batch = await self._run_and_capture(
            monkeypatch, turn_events=turn_events, response_text=["first", "second"],
        )
        assistant_rows = [r for r in batch if r["role"] == "assistant"]
        # Two text rows only — no third merged trailing row.
        assert len(assistant_rows) == 2
        assert [r["content"] for r in assistant_rows] == ["first", "second"]

    async def test_interrupt_marker_folds_into_last_text_row(self, monkeypatch):
        turn_events = [
            {"kind": "text", "payload": {"content": "partial answer", "uuid": "u2"}},
        ]
        batch = await self._run_and_capture(
            monkeypatch, turn_events=turn_events, response_text=["partial answer"],
            interrupted=True,
        )
        assistant_rows = [r for r in batch if r["role"] == "assistant"]
        assert len(assistant_rows) == 1
        assert assistant_rows[0]["content"] == "partial answer\n[Interrupted]"

    async def test_tool_only_turn_still_emits_closing_assistant_row(self, monkeypatch):
        # No text events → legacy trailing assistant row must still appear so
        # the turn has a final bubble (preserves prior behavior).
        turn_events = [
            {"kind": "tool_use", "payload": {"name": "Bash", "tool_id": "t1", "input": {}, "uuid": "u3"}},
        ]
        batch = await self._run_and_capture(
            monkeypatch, turn_events=turn_events, response_text=["done"],
        )
        assistant_rows = [r for r in batch if r["role"] == "assistant"]
        assert len(assistant_rows) == 1
        assert assistant_rows[0]["content"] == "done"
