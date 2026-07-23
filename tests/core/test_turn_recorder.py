"""Unit tests for app/core/turn_recorder.py — the V2 Phase 1 TurnRecorder.

Covers: maybe_create (un-gated), the full §3 pump→contract translation table
(docs/V2_PHASE1_DESIGN.md), delta batching/coalescing, the fenced run lease,
the crash-path terminal, the drain-pump module-level taps, and the
never-propagate-exceptions invariant.

All tests drive the recorder DIRECTLY with pump-shaped dicts (no HTTP, no
ASGITransport streaming — that buffers infinite bodies and deadlocks). The
autouse ``_auto_db_shim`` conftest fixture routes every DB call to a per-test
in-memory SQLite shim.
"""

import asyncio

import pytest

from app.core import turn_recorder as tr
from app.core.turn_recorder import (
    TurnRecorder,
    record_cli_wake_event,
    record_cli_wake_turn_completed,
    record_cli_wake_turn_started,
    record_drained_event,
    record_interrupting,
    record_parked,
)
from app.db import runs as runs_db
from app.db import session_events as se
from app.db import sessions as sessions_db


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

SID = "sess-v2"


def _mk_session(session_id: str = SID, v2: bool = True) -> None:
    # `v2` is vestigial — create_session always stamps v2_stream TRUE now.
    sessions_db.create_session(session_id, "default")


async def _recorder(session_id: str = SID, command_id=None) -> TurnRecorder:
    rec = await TurnRecorder.maybe_create(session_id, command_id=command_id)
    assert rec is not None
    return rec


def _rows(session_id: str = SID):
    return se.get_events(session_id)


def _types(session_id: str = SID):
    return [r["event_type"] for r in _rows(session_id)]


# ---------------------------------------------------------------------------
# maybe_create — un-gated: a recorder is created for every session
# ---------------------------------------------------------------------------

async def test_maybe_create_recorder_regardless_of_flag(mock_db):
    # The per-session v2 gate is gone (Commit 5): the recorder is created for
    # any session regardless of the now-vestigial v2_stream column value.
    _mk_session(v2=False)
    rec = await TurnRecorder.maybe_create(SID)
    assert rec is not None
    await rec.close()


async def test_maybe_create_recorder_when_session_missing(mock_db):
    # No row read anymore: a session with no row still gets a recorder
    # (ensure_backfilled no-ops on empty history, the run lease is claimed).
    rec = await TurnRecorder.maybe_create("no-such-session")
    assert rec is not None
    await rec.close()


async def test_maybe_create_recorder_when_flag_on(mock_db):
    _mk_session(v2=True)
    rec = await TurnRecorder.maybe_create(SID)
    assert rec is not None
    assert rec.run_id is not None
    assert rec.fence_token is not None
    await rec.close()


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------

async def test_start_appends_turn_started_then_message_user(mock_db):
    _mk_session()
    rec = await _recorder(command_id="cmd-1")
    await rec.start("hello world")
    rows = _rows()
    assert [r["event_type"] for r in rows] == ["turn.started", "message.user"]
    assert rows[1]["payload"]["content"] == "hello world"
    assert rows[0]["command_id"] == "cmd-1"
    assert rows[1]["command_id"] == "cmd-1"
    assert rows[0]["turn_id"] == rows[1]["turn_id"] == rec.turn_id
    assert rows[0]["run_id"] == rec.run_id
    await rec.close()


# ---------------------------------------------------------------------------
# §3 golden translations (non-delta rows append immediately)
# ---------------------------------------------------------------------------

async def test_chunk_maps_to_assistant_text_delta(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({"type": "chunk", "content": "full text block"})
    rows = _rows()
    assert rows[-1]["event_type"] == "assistant.text_delta"
    assert rows[-1]["payload"]["text"] == "full text block"
    await rec.close()


async def test_tool_use_maps_to_tool_use(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "tool_use", "name": "Bash", "id": "tu_1",
        "input": {"command": "ls"},
    })
    row = _rows()[-1]
    assert row["event_type"] == "tool.use"
    assert row["payload"]["tool_use_id"] == "tu_1"
    assert row["payload"]["name"] == "Bash"
    assert row["payload"]["input"] == {"command": "ls"}
    await rec.close()


async def test_tool_result_maps_to_tool_result_with_truncation(mock_db):
    _mk_session()
    rec = await _recorder()
    big = "x" * (tr.RESULT_TRUNCATE_CHARS + 500)
    await rec.on_event({
        "type": "tool_result", "name": "Bash", "tool_use_id": "tu_1",
        "output": big, "is_error": True,
    })
    row = _rows()[-1]
    assert row["event_type"] == "tool.result"
    assert row["payload"]["tool_use_id"] == "tu_1"
    assert row["payload"]["is_error"] is True
    assert len(row["payload"]["content_preview"]) == tr.RESULT_TRUNCATE_CHARS
    await rec.close()


async def test_subagent_start_maps_to_subagent_started(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "subagent_start", "tool_id": "task_1", "agent_id": "task_1",
        "agent_type": "Explore", "description": "look around",
        "prompt": "find things",
    })
    row = _rows()[-1]
    assert row["event_type"] == "subagent.started"
    assert row["payload"]["task_id"] == "task_1"
    assert row["payload"]["agent_type"] == "Explore"
    assert row["payload"]["description"] == "look around"
    assert row["payload"]["prompt_preview"] == "find things"
    await rec.close()


@pytest.mark.parametrize("pump_type,kind", [
    ("subagent_chunk", "chunk"),
    ("subagent_tool_use", "tool_use"),
    ("subagent_tool_result", "tool_result"),
])
async def test_subagent_progress_kinds(mock_db, pump_type, kind):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": pump_type, "agent_id": "task_1", "content": "inner",
        "name": "Read",
    })
    row = _rows()[-1]
    assert row["event_type"] == "subagent.progress"
    assert row["payload"]["task_id"] == "task_1"
    assert row["payload"]["kind"] == kind
    # Remaining pump fields are carried in the progress payload dict.
    assert row["payload"]["payload"]["content"] == "inner"
    await rec.close()


@pytest.mark.parametrize("is_error,status", [
    (False, "completed"),
    (True, "failed"),
])
async def test_subagent_done_maps_to_subagent_completed(mock_db, is_error, status):
    _mk_session()
    rec = await _recorder()
    big = "r" * (tr.RESULT_TRUNCATE_CHARS + 10)
    await rec.on_event({
        "type": "subagent_done", "tool_id": "task_1", "agent_id": "task_1",
        "agent_type": "Explore", "result": big, "is_error": is_error,
    })
    row = _rows()[-1]
    assert row["event_type"] == "subagent.completed"
    assert row["payload"]["task_id"] == "task_1"
    assert row["payload"]["status"] == status
    assert len(row["payload"]["result_preview"]) == tr.RESULT_TRUNCATE_CHARS
    await rec.close()


@pytest.mark.parametrize("pump_type,default_state", [
    ("task_started", "started"),
    ("task_progress", "running"),
    ("task_notification", "notified"),
])
async def test_task_events_map_to_task_updated(mock_db, pump_type, default_state):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": pump_type, "task_id": "t1", "description": "bg work",
        "summary": "half done", "tool_use_id": "tu_9",
    })
    row = _rows()[-1]
    assert row["event_type"] == "task.updated"
    assert row["payload"]["task_id"] == "t1"
    assert row["payload"]["state"] == default_state
    assert row["payload"]["description"] == "bg work"
    assert row["payload"]["summary"] == "half done"
    assert row["payload"]["tool_use_id"] == "tu_9"
    await rec.close()


async def test_task_notification_record_populates_task_id_and_tool_use_id(mock_db):
    # Mirror the EXACT drain-pump record shape idle_reader now emits (with the
    # newly threaded task_id). The contract task.updated event must carry BOTH
    # task_id and tool_use_id — an empty task_id makes the reducer bail and the
    # agent's summary never reaches its card.
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "task_notification",
        "task_id": "task-9",
        "tool_use_id": "tu-9",
        "status": "completed",
        "usage": None,
        "summary": "all done",
    })
    row = _rows()[-1]
    assert row["event_type"] == "task.updated"
    assert row["payload"]["task_id"] == "task-9"
    assert row["payload"]["tool_use_id"] == "tu-9"
    assert row["payload"]["state"] == "completed"
    assert row["payload"]["summary"] == "all done"
    await rec.close()


async def test_task_event_explicit_status_wins(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({"type": "task_progress", "task_id": "t1", "status": "paused"})
    assert _rows()[-1]["payload"]["state"] == "paused"
    await rec.close()


@pytest.mark.parametrize("data,state", [
    # Registry-patch shape (TaskUpdatedMessage / drained chip): patch.status wins.
    ({"task_id": "t1", "patch": {"status": "completed"}}, "completed"),
    # Notification-synthesized chip mirrors status at top level too.
    ({"task_id": "t1", "status": "failed", "patch": {"status": "failed"}}, "failed"),
    # Top-level status only (no patch).
    ({"task_id": "t1", "status": "running"}, "running"),
    # No status anywhere: generic "updated".
    ({"task_id": "t1"}, "updated"),
])
async def test_system_task_updated_maps_to_task_updated(mock_db, data, state):
    # A background task's terminal status crossing a turn boundary arrives as a
    # {"type": "system", "subtype": "task_updated"} chip (see
    # _task_status_events_from_message). It must land as task.updated — a
    # system.note here leaves the replayed task strip stuck on "running".
    _mk_session()
    rec = await _recorder()
    await rec.on_event({"type": "system", "subtype": "task_updated", "data": data})
    row = _rows()[-1]
    assert row["event_type"] == "task.updated"
    assert row["payload"]["task_id"] == "t1"
    assert row["payload"]["state"] == state
    await rec.close()


async def test_system_task_updated_enriches_tool_use_id_from_registry(mock_db):
    # The SDK's TaskUpdatedMessage never carries tool_use_id, so a chip built from
    # its raw data has None. The recorder must enrich it from the registry launch
    # id so the terminal chip can settle the subagent card on the frontend.
    from app.core.task_registry import task_registry
    task_registry.reset_all()
    task_registry.start_task(SID, "t1", "agent", "bg", tool_use_id="toolu_reg")
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "system", "subtype": "task_updated",
        "data": {"task_id": "t1", "patch": {"status": "killed"}},
    })
    row = _rows()[-1]
    assert row["event_type"] == "task.updated"
    assert row["payload"]["task_id"] == "t1"
    assert row["payload"]["tool_use_id"] == "toolu_reg"
    await rec.close()
    task_registry.reset_all()


async def test_system_task_updated_data_tool_use_id_wins_over_registry(mock_db):
    # A tool_use_id already on the data wins over the registry fallback.
    from app.core.task_registry import task_registry
    task_registry.reset_all()
    task_registry.start_task(SID, "t1", "agent", "bg", tool_use_id="toolu_reg")
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "system", "subtype": "task_updated",
        "data": {"task_id": "t1", "status": "killed", "tool_use_id": "toolu_data"},
    })
    row = _rows()[-1]
    assert row["payload"]["tool_use_id"] == "toolu_data"
    await rec.close()
    task_registry.reset_all()


async def test_system_task_updated_without_task_id_falls_back_to_note(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "system", "subtype": "task_updated",
        "data": {"patch": {"status": "completed"}},
    })
    row = _rows()[-1]
    assert row["event_type"] == "system.note"
    assert row["payload"]["subtype"] == "task_updated"
    await rec.close()


async def test_system_maps_to_system_note(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "system", "subtype": "local_command",
        "data": {"content": "output"},
    })
    row = _rows()[-1]
    assert row["event_type"] == "system.note"
    assert row["payload"]["subtype"] == "local_command"
    assert row["payload"]["data"] == {"content": "output"}
    await rec.close()


async def test_system_model_info_folds_top_level_keys_into_data(mock_db):
    # The pump yields model/requested_model nested under "data"; the recorder's
    # generic system-note fallback passes it through so the frontend sees
    # actual-vs-requested on the wire (model-rerouting detection).
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "system", "subtype": "model_info",
        "data": {"model": "claude-actual", "requested_model": "claude-requested"},
    })
    row = _rows()[-1]
    assert row["event_type"] == "system.note"
    assert row["payload"]["subtype"] == "model_info"
    assert row["payload"]["data"] == {
        "model": "claude-actual",
        "requested_model": "claude-requested",
    }
    await rec.close()


async def test_system_model_info_null_requested_model(mock_db):
    # requested_model passthrough is optional (options.model may be None).
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "system", "subtype": "model_info",
        "data": {"model": "claude-actual", "requested_model": None},
    })
    row = _rows()[-1]
    assert row["payload"]["subtype"] == "model_info"
    assert row["payload"]["data"] == {
        "model": "claude-actual", "requested_model": None,
    }
    await rec.close()


async def test_system_compaction_boundary_passthrough(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "system", "subtype": "compact_boundary",
        "data": {"trigger": "auto"},
    })
    row = _rows()[-1]
    assert row["event_type"] == "system.compaction_boundary"
    assert row["payload"]["subtype"] == "compact_boundary"
    assert row["payload"]["data"] == {"trigger": "auto"}
    await rec.close()


async def test_hook_event_maps_to_system_note(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "hook_event",
        "hook_event_name": "PreToolUse",
        "matcher": "Bash",
        "decision": "block",
        "reason": "denied by policy",
        "system_message": "blocked",
        "tool_name": "Bash",
        "session_id": SID,
        "timestamp": "2026-07-12T00:00:00Z",
    })
    row = _rows()[-1]
    assert row["event_type"] == "system.note"
    assert row["payload"]["subtype"] == "hook_event"
    assert row["payload"]["data"] == {
        "hook_event_name": "PreToolUse",
        "matcher": "Bash",
        "decision": "block",
        "reason": "denied by policy",
        "system_message": "blocked",
        "tool_name": "Bash",
        "session_id": SID,
        "timestamp": "2026-07-12T00:00:00Z",
    }
    await rec.close()


async def test_turn_usage_maps_to_turn_usage(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({"type": "turn_usage", "usage": {"input_tokens": 10}})
    row = _rows()[-1]
    assert row["event_type"] == "turn.usage"
    assert row["payload"]["usage"] == {"input_tokens": 10}
    await rec.close()


async def test_done_maps_to_turn_completed_with_usage_summary(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "done", "session_id": SID,
        "metadata": {
            "stop_reason": "end_turn", "tokens_in": 5, "tokens_out": 7,
            "total_cost_usd": 0.01, "duration_ms": 1200, "num_turns": 1,
            "terminal_reason": "completed",
            "last_assistant_uuid": "not-a-usage-key",
        },
    })
    row = _rows()[-1]
    assert row["event_type"] == "turn.completed"
    assert row["payload"]["stop_reason"] == "end_turn"
    # SDK 0.2.126: terminal_reason threads through the done metadata into the
    # turn.completed contract payload.
    assert row["payload"]["terminal_reason"] == "completed"
    assert row["payload"]["usage"] == {
        "tokens_in": 5, "tokens_out": 7, "total_cost_usd": 0.01,
        "duration_ms": 1200, "num_turns": 1,
    }
    await rec.close()


async def test_done_max_turns_carries_terminal_reason(mock_db):
    # A turn that stopped on the max-turns ceiling surfaces
    # terminal_reason='max_turns'; it must reach the turn.completed payload so
    # the frontend can render its "turn limit reached" hint.
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "done", "session_id": SID,
        "metadata": {"stop_reason": "end_turn", "terminal_reason": "max_turns"},
    })
    row = _rows()[-1]
    assert row["event_type"] == "turn.completed"
    assert row["payload"]["terminal_reason"] == "max_turns"
    await rec.close()


async def test_done_with_api_error_status_carried_in_payload(mock_db):
    # A5 amendment (plan §1.6(1)): a usage-limit 429 settles as a normal
    # completion, so the terminal turn.completed payload must durably carry
    # the pump's api_error_status for the scheduler's failover derivation.
    # ResultMessage exposes only api_error_status — no message field exists.
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "done", "session_id": SID,
        "metadata": {
            "stop_reason": "end_turn", "tokens_in": 5,
            "api_error_status": 429,
        },
    })
    row = _rows()[-1]
    assert row["event_type"] == "turn.completed"
    assert row["payload"]["api_error_status"] == 429
    assert "api_error_message" not in row["payload"]
    assert row["payload"]["usage"] == {"tokens_in": 5}  # not a usage key
    await rec.close()


async def test_done_without_api_error_has_no_error_keys(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "done", "session_id": SID,
        "metadata": {"stop_reason": "end_turn"},
    })
    row = _rows()[-1]
    assert row["event_type"] == "turn.completed"
    assert "api_error_status" not in row["payload"]
    assert "api_error_message" not in row["payload"]
    await rec.close()


async def test_done_carries_model_usage_in_usage_payload(mock_db):
    # Per-model usage rides on the turn.completed usage dict (open in the
    # contract) so it stays durable in session_events under postgres_only,
    # where the session_store final-row metadata path is gated off.
    _mk_session()
    rec = await _recorder()
    model_usage = {
        "claude-fable-5": {
            "inputTokens": 10, "outputTokens": 5, "provider": "firstParty",
        },
    }
    await rec.on_event({
        "type": "done", "session_id": SID,
        "metadata": {
            "stop_reason": "end_turn", "tokens_in": 5,
            "model_usage": model_usage,
        },
    })
    row = _rows()[-1]
    assert row["event_type"] == "turn.completed"
    assert row["payload"]["usage"]["model_usage"] == model_usage
    await rec.close()


async def test_done_without_model_usage_omits_key(mock_db):
    # model_usage None/absent must not leak an empty key into the usage dict.
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "done", "session_id": SID,
        "metadata": {
            "stop_reason": "end_turn", "tokens_in": 5,
            "model_usage": None,
        },
    })
    row = _rows()[-1]
    assert row["event_type"] == "turn.completed"
    assert "model_usage" not in row["payload"]["usage"]
    await rec.close()


async def test_interrupted_maps_to_turn_aborted_interrupt(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({"type": "interrupted", "session_id": SID})
    row = _rows()[-1]
    assert row["event_type"] == "turn.aborted"
    assert row["payload"]["reason"] == "interrupt"
    await rec.close()


async def test_interrupted_carries_terminal_reason_from_metadata(mock_db):
    # SDK 0.2.126: the interrupted frame now carries the SDK terminal_reason in
    # its metadata (e.g. aborted_streaming/aborted_tools); the turn.aborted
    # contract payload must reflect it.
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "interrupted", "session_id": SID,
        "metadata": {"terminal_reason": "aborted_streaming"},
    })
    row = _rows()[-1]
    assert row["event_type"] == "turn.aborted"
    assert row["payload"]["reason"] == "interrupt"
    assert row["payload"]["terminal_reason"] == "aborted_streaming"
    await rec.close()


async def test_error_maps_to_turn_aborted_error_with_message(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({"type": "error", "message": "boom"})
    row = _rows()[-1]
    assert row["event_type"] == "turn.aborted"
    assert row["payload"]["reason"] == "error"
    assert row["payload"]["message"] == "boom"
    await rec.close()


@pytest.mark.parametrize("event", [
    {"type": "stream_block_start", "block_type": "text", "content_block": {"type": "text"}},
    {"type": "stream_block_stop", "index": 0},
    {"type": "stream_message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}},
    {"type": "rate_limit", "status": "allowed"},
    {"type": "plugin_message", "message": "hi"},
    {"type": "api_error", "status": 500},
    {"type": "init", "session_id": "sdk-1"},
    {"type": "permission_request", "id": "p1"},
    {"type": "totally_unknown_frame"},
])
async def test_not_recorded_rows_append_nothing(mock_db, event):
    _mk_session()
    rec = await _recorder()
    await rec.on_event(event)
    assert _rows() == []
    await rec.close()


# ---------------------------------------------------------------------------
# delta batching
# ---------------------------------------------------------------------------

async def test_text_deltas_coalesce_into_one_event_on_timer(mock_db, monkeypatch):
    monkeypatch.setattr(tr, "FLUSH_INTERVAL_SECONDS", 0.01)
    _mk_session()
    rec = await _recorder()
    for piece in ("Hel", "lo ", "world"):
        await rec.on_event({"type": "stream_delta", "delta_type": "text", "content": piece})
    assert _rows() == []  # still buffered
    await asyncio.sleep(0.08)
    rows = _rows()
    assert [r["event_type"] for r in rows] == ["assistant.text_delta"]
    assert rows[0]["payload"]["text"] == "Hello world"
    await rec.close()


async def test_deltas_flush_before_any_non_delta_event(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({"type": "stream_delta", "delta_type": "text", "content": "before "})
    await rec.on_event({"type": "stream_delta", "delta_type": "thinking", "content": "hmm"})
    await rec.on_event({"type": "tool_use", "name": "Read", "id": "tu_2", "input": {}})
    types = _types()
    # Coalesced buffers land BEFORE the tool.use, preserving pump order.
    assert types == ["assistant.text_delta", "assistant.thinking_delta", "tool.use"]
    await rec.close()


async def test_size_threshold_flushes_immediately(mock_db):
    _mk_session()
    rec = await _recorder()
    half = "a" * 1500
    await rec.on_event({"type": "stream_delta", "delta_type": "text", "content": half})
    assert _rows() == []
    await rec.on_event({"type": "stream_delta", "delta_type": "text", "content": half})
    rows = _rows()  # 3000 >= 2048 — flushed with no sleep
    assert [r["event_type"] for r in rows] == ["assistant.text_delta"]
    assert rows[0]["payload"]["text"] == half + half
    await rec.close()


async def test_tool_input_deltas_carry_open_block_tool_use_id(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "stream_block_start", "block_type": "tool_use",
        "content_block": {"type": "tool_use", "id": "tu_open"},
    })
    await rec.on_event({"type": "stream_delta", "delta_type": "tool_input", "content": '{"a":'})
    await rec.on_event({"type": "stream_delta", "delta_type": "tool_input", "content": "1}"})
    await rec.on_event({"type": "stream_block_stop", "index": 0})  # forces flush
    rows = _rows()
    assert [r["event_type"] for r in rows] == ["tool.input_delta"]
    assert rows[0]["payload"]["tool_use_id"] == "tu_open"
    assert rows[0]["payload"]["partial_json"] == '{"a":1}'
    await rec.close()


async def test_close_flushes_buffered_deltas(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({"type": "stream_delta", "delta_type": "text", "content": "tail"})
    await rec.close()
    types = _types()
    assert types[0] == "assistant.text_delta"
    assert _rows()[0]["payload"]["text"] == "tail"


# ---------------------------------------------------------------------------
# turn.usage coalescing
# ---------------------------------------------------------------------------

async def test_usage_coalesced_to_one_per_window_and_flushed_at_done(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({"type": "turn_usage", "usage": {"n": 1}})
    await rec.on_event({"type": "turn_usage", "usage": {"n": 2}})  # within 2s → held
    await rec.on_event({"type": "turn_usage", "usage": {"n": 3}})  # replaces held
    assert _types() == ["turn.usage"]
    assert _rows()[0]["payload"]["usage"] == {"n": 1}
    await rec.on_event({"type": "done", "session_id": SID, "metadata": {}})
    types = _types()
    # The held latest usage flushes BEFORE the terminal.
    assert types == ["turn.usage", "turn.usage", "turn.completed"]
    assert _rows()[1]["payload"]["usage"] == {"n": 3}
    await rec.close()


# ---------------------------------------------------------------------------
# run lease
# ---------------------------------------------------------------------------

async def test_lease_run_id_stamped_on_rows_and_released_on_close(mock_db):
    _mk_session()
    rec = await _recorder()
    assert rec.run_id is not None
    await rec.start("hi")
    assert all(r["run_id"] == rec.run_id for r in _rows())
    await rec.close()
    # Lease released: a different runner can claim the session now.
    other = runs_db.claim_run(SID, "other-runner", 60)
    assert other is not None
    runs_db.release_run(other["id"], other["fence_token"])


async def test_claim_conflict_records_unfenced(mock_db):
    _mk_session()
    held = runs_db.claim_run(SID, "someone-else", 60)
    assert held is not None
    rec = await TurnRecorder.maybe_create(SID)
    assert rec is not None  # never blocks the turn
    assert rec.run_id is None
    await rec.start("hi")
    rows = _rows()
    assert len(rows) == 2
    assert all(r["run_id"] is None for r in rows)
    await rec.close()
    # close() must NOT release someone else's lease.
    assert runs_db.claim_run(SID, "third-runner", 60) is None
    runs_db.release_run(held["id"], held["fence_token"])


# ---------------------------------------------------------------------------
# close() semantics (crash path + idempotence)
# ---------------------------------------------------------------------------

async def test_close_without_terminal_appends_turn_aborted(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.start("hi")
    await rec.on_event({"type": "chunk", "content": "partial"})
    await rec.close()
    types = _types()
    assert types[-1] == "turn.aborted"
    assert _rows()[-1]["payload"]["reason"] == "error"


async def test_close_after_done_appends_nothing(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({"type": "done", "session_id": SID, "metadata": {}})
    n = len(_rows())
    await rec.close()
    assert len(_rows()) == n
    assert _types()[-1] == "turn.completed"


async def test_double_close_appends_nothing(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.close()
    n = len(_rows())
    await rec.close()
    await rec.close()
    assert len(_rows()) == n


# ---------------------------------------------------------------------------
# module-level taps: record_drained_event / record_parked / record_interrupting
# ---------------------------------------------------------------------------

async def test_record_drained_event_translates_allowed_types(mock_db):
    _mk_session()
    await record_drained_event(SID, {
        "type": "subagent_chunk", "agent_id": "task_1", "content": "late",
    })
    await record_drained_event(SID, {"type": "task_progress", "task_id": "t1"})
    await record_drained_event(SID, {
        "type": "system", "subtype": "note", "data": {"k": "v"},
    })
    types = _types()
    assert types == ["subagent.progress", "task.updated", "system.note"]
    # Drained events are unfenced, outside any turn.
    assert all(r["run_id"] is None for r in _rows())
    assert all(r["turn_id"] is None for r in _rows())


async def test_record_drained_event_filters_other_types(mock_db):
    _mk_session()
    await record_drained_event(SID, {"type": "chunk", "content": "x"})
    await record_drained_event(SID, {"type": "tool_use", "id": "t", "name": "Bash"})
    await record_drained_event(SID, {"type": "done", "metadata": {}})
    assert _rows() == []


async def test_record_drained_event_records_regardless_of_flag(mock_db):
    # Un-gated (Commit 5): drained events are recorded for any session.
    _mk_session(v2=False)
    await record_drained_event(SID, {
        "type": "subagent_chunk", "agent_id": "task_1", "content": "late",
    })
    assert _types() == ["subagent.progress"]


async def test_record_drained_event_logs_dropped_type(mock_db, caplog):
    # #691: a silently-dropped drain type is now DEBUG-logged (naming the type)
    # so an undelivered event is diagnosable.
    _mk_session()
    import logging
    with caplog.at_level(logging.DEBUG, logger="app.core.turn_recorder"):
        await record_drained_event(SID, {"type": "chunk", "content": "x"})
    assert _rows() == []
    assert any(
        "record_drained_event dropped" in r.message and "chunk" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# CLI-wake taps (#691): record_cli_wake_event + turn.started/turn.completed
# ---------------------------------------------------------------------------

async def test_record_cli_wake_event_translates_render_surface(mock_db):
    # The wide entry point accepts chunk/tool_use/tool_result — the render
    # surface record_drained_event drops — and appends them UNFENCED, stamped
    # with the supplied synthetic wake turn_id.
    _mk_session()
    n1 = await record_cli_wake_event(SID, {"type": "chunk", "content": "hi"}, turn_id="w1")
    n2 = await record_cli_wake_event(
        SID, {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "ls"}},
        turn_id="w1",
    )
    n3 = await record_cli_wake_event(
        SID, {"type": "tool_result", "tool_use_id": "t", "output": "done"}, turn_id="w1",
    )
    assert (n1, n2, n3) == (1, 1, 1)
    assert _types() == ["assistant.text_delta", "tool.use", "tool.result"]
    rows = _rows()
    assert all(r["run_id"] is None for r in rows)          # unfenced
    assert all(r["turn_id"] == "w1" for r in rows)         # stamped wake turn
    assert rows[0]["payload"]["text"] == "hi"


async def test_record_cli_wake_event_also_accepts_drained_types(mock_db):
    # It is a SUPERSET of record_drained_event's filter: subagent_*/task_*/system
    # still translate.
    _mk_session()
    await record_cli_wake_event(
        SID, {"type": "subagent_chunk", "agent_id": "task_1", "content": "x"}, turn_id="w1",
    )
    await record_cli_wake_event(SID, {"type": "task_progress", "task_id": "t1"}, turn_id="w1")
    assert _types() == ["subagent.progress", "task.updated"]


async def test_record_cli_wake_event_filters_unknown_type(mock_db):
    # A non-render, non-drained type (thinking is never forwarded here, but any
    # unknown type) is filtered and reports 0 appended.
    _mk_session()
    n = await record_cli_wake_event(SID, {"type": "thinking_block", "content": "x"}, turn_id="w1")
    assert n == 0
    assert _rows() == []


async def test_record_cli_wake_turn_started_and_completed_bracket(mock_db):
    # The synthetic turn brackets mirror the fenced recorder's turn.started /
    # turn.completed shape, keyed by the synthesized wake turn_id.
    _mk_session()
    await record_cli_wake_turn_started(SID, "w9")
    await record_cli_wake_event(SID, {"type": "chunk", "content": "resp"}, turn_id="w9")
    await record_cli_wake_turn_completed(
        SID, "w9", usage={"tokens_out": 5}, stop_reason="end_turn",
    )
    rows = _rows()
    assert [r["event_type"] for r in rows] == [
        "turn.started", "assistant.text_delta", "turn.completed",
    ]
    assert all(r["turn_id"] == "w9" for r in rows)
    assert all(r["run_id"] is None for r in rows)          # unfenced
    completed = rows[-1]["payload"]
    assert completed["usage"] == {"tokens_out": 5}
    assert completed["stop_reason"] == "end_turn"


async def test_record_cli_wake_taps_never_propagate(mock_db, monkeypatch):
    _mk_session()

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(tr.session_events_db, "append_events", _boom)
    # None may raise; the wide entry point returns 0 on failure.
    assert await record_cli_wake_event(SID, {"type": "chunk", "content": "x"}, turn_id="w") == 0
    await record_cli_wake_turn_started(SID, "w")
    await record_cli_wake_turn_completed(SID, "w")


async def test_record_parked_appends_turn_parked(mock_db):
    _mk_session()
    await record_parked(SID, ["t1", "t2", ""])
    rows = _rows()
    assert [r["event_type"] for r in rows] == ["turn.parked"]
    assert rows[0]["payload"]["task_ids"] == ["t1", "t2"]


async def test_record_interrupting_appends_turn_interrupting(mock_db):
    _mk_session()
    await record_interrupting(SID, "stop")
    rows = _rows()
    assert [r["event_type"] for r in rows] == ["turn.interrupting"]
    assert rows[0]["payload"]["mode"] == "stop"


async def test_record_parked_and_interrupting_record_regardless_of_flag(mock_db):
    # Un-gated (Commit 5): both taps append for any session.
    _mk_session(v2=False)
    await record_parked(SID, ["t1"])
    await record_interrupting(SID, "stop")
    assert _types() == ["turn.parked", "turn.interrupting"]


# ---------------------------------------------------------------------------
# never-propagate invariant
# ---------------------------------------------------------------------------

async def test_append_raises_does_not_propagate(mock_db, monkeypatch):
    _mk_session()
    rec = await _recorder()

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(tr.session_events_db, "append_events", _boom)
    # None of these may raise.
    await rec.start("hi")
    await rec.on_event({"type": "chunk", "content": "x"})
    await rec.on_event({"type": "done", "session_id": SID, "metadata": {}})
    await rec.close()
    await record_drained_event(SID, {"type": "task_progress", "task_id": "t1"})
    await record_parked(SID, ["t1"])
    await record_interrupting(SID, "stop")


async def test_on_event_with_garbage_input_does_not_raise(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event(None)  # type: ignore[arg-type]
    await rec.on_event("not a dict")  # type: ignore[arg-type]
    await rec.on_event({})  # no type key
    await rec.on_event({"type": "tool_use"})  # missing every field
    assert "tool.use" in _types()  # defaults applied, still recorded
    await rec.close()


# ---------------------------------------------------------------------------
# first-seq memory + record_empty_turn_discard (transcript.rewound)
# ---------------------------------------------------------------------------

async def test_first_seq_captured_on_first_append(mock_db):
    _mk_session()
    # A prior turn already occupies seqs 1-2.
    se.append_events(SID, [{"type": "turn.started"}, {"type": "turn.completed"}])
    rec = await _recorder()
    assert rec._first_seq is None
    await rec.start("hello")
    assert rec._first_seq == 3  # this turn's turn.started
    await rec.on_event({"type": "chunk", "content": "x"})
    assert rec._first_seq == 3  # sticky — never moves after the first append
    await rec.close()


async def test_record_empty_turn_discard_appends_rewound(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.start("discard me")
    first = rec._first_seq
    await rec.close()
    await rec.record_empty_turn_discard()
    rows = _rows()
    assert rows[-1]["event_type"] == "transcript.rewound"
    assert rows[-1]["payload"]["from_seq"] == first
    assert rows[-1]["run_id"] is None  # unfenced (lease already released)


async def test_record_empty_turn_discard_noop_without_appends(mock_db):
    _mk_session()
    rec = await _recorder()
    # No events ever appended for this turn -> nothing to erase.
    await rec.close()
    # close() appends the crash-path terminal, so reset to the no-append case:
    rec2 = await _recorder()
    rec2._first_seq = None
    await rec2.record_empty_turn_discard()
    assert "transcript.rewound" not in _types()
    await rec2.close()


async def test_record_empty_turn_discard_never_raises(mock_db, monkeypatch):
    _mk_session()
    rec = await _recorder()
    await rec.start("hi")

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(tr.session_events_db, "append_events", _boom)
    await rec.record_empty_turn_discard()  # must not raise


# ---------------------------------------------------------------------------
# pop_last_turn_recorder — session teardown drops the last-recorder entry
# ---------------------------------------------------------------------------

async def test_pop_last_turn_recorder_removes_entry(mock_db):
    _mk_session()
    rec = await _recorder()
    assert tr._last_turn_recorders.get(SID) is rec
    await rec.close()
    # close() intentionally leaves the entry (discard_empty_turn runs post-turn).
    assert tr._last_turn_recorders.get(SID) is rec

    tr.pop_last_turn_recorder(SID)
    assert SID not in tr._last_turn_recorders
    # discard after teardown is a clean no-op (no recorder to reach).
    await tr.discard_empty_turn(SID)
    assert "transcript.rewound" not in _types()


def test_pop_last_turn_recorder_missing_session_never_raises():
    tr.pop_last_turn_recorder("never-seen-session")  # must not raise
