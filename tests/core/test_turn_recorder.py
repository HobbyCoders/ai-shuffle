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
from unittest.mock import AsyncMock

import pytest

from app.core import turn_recorder as tr
from app.core.turn_recorder import (
    TurnRecorder,
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

@pytest.mark.parametrize("phase", ["start", "timer", "done", "close", "usage"])
@pytest.mark.parametrize("callback_raises", [False, True])
async def test_recording_failure_latches_without_retry(mock_db, monkeypatch, phase, callback_raises):
    from unittest.mock import Mock

    _mk_session()
    rec = await _recorder()
    if phase != "start":
        await rec.start("hello")
    if phase in ("timer", "close"):
        await rec.on_event({"type": "stream_delta", "delta_type": "text", "content": "pending"})
    if phase == "usage":
        await rec.on_event({"type": "done", "metadata": {}})
        rec._pending_usage = {"tokens_in": 10}
    callback = Mock(side_effect=RuntimeError("owner failed") if callback_raises else None)
    rec.on_recording_failed = callback
    append = Mock(side_effect=RuntimeError("commit outcome unknown"))
    monkeypatch.setattr(se, "append_events", append)
    if phase == "start":
        await rec.start("hello")
    elif phase == "timer":
        await asyncio.wait_for(rec._flush_task, 1)
    elif phase == "done":
        await rec.on_event({"type": "done", "metadata": {}})
    else:
        await rec.close()
    assert rec.recording_failed
    for _ in range(100):
        await rec.on_event({"type": "stream_delta", "delta_type": "text", "content": "ignored"})
        await rec.on_event({"type": "turn_usage", "usage": {"tokens_in": 20}})
    assert not await rec.record_steered_user_message("ignored")
    assert await rec.record_task_terminal([{"task_id": "task", "status": "completed"}]) == 0
    await rec.start("ignored")
    await rec.close()
    await rec.close()
    assert append.call_count == 1
    callback.assert_called_once_with()
    assert not rec._delta_buffers
    assert rec._pending_usage is None
    assert rec._flush_task is None
    assert rec._renew_task is None
    assert rec.recording_failed_dropped > 0
    if phase != "usage":
        assert "turn.completed" not in _types()


async def test_task_terminal_failed_append_returns_zero(mock_db, monkeypatch):
    from unittest.mock import Mock

    _mk_session()
    rec = await _recorder()
    monkeypatch.setattr(se, "append_events", Mock(side_effect=RuntimeError("unavailable")))
    assert await rec.record_task_terminal([{"task_id": "task", "status": "completed"}]) == 0
    assert rec.recording_failed
    await rec.close()



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


async def test_start_without_record_user_appends_only_turn_started(mock_db):
    # A resumed turn re-runs the loop from the head node; it adds no message,
    # so the transcript must gain turn.started and nothing else.
    _mk_session()
    rec = await _recorder(command_id="cmd-r")
    await rec.start("", profile_id="prof-r", record_user=False)
    rows = _rows()
    assert [r["event_type"] for r in rows] == ["turn.started"]
    assert rows[0]["payload"]["profile_id"] == "prof-r"
    assert rows[0]["turn_id"] == rec.turn_id
    await rec.close()


async def test_start_stamps_profile_id_on_both_events(mock_db):
    # Per-message agent attribution: the live SSE path stamps the turn's
    # profile onto turn.started so the FE can pin streamed messages to it.
    _mk_session()
    rec = await _recorder(command_id="cmd-p")
    await rec.start("hello", profile_id="prof-7")
    rows = _rows()
    assert [r["event_type"] for r in rows] == ["turn.started", "message.user"]
    assert rows[0]["payload"]["profile_id"] == "prof-7"
    assert rows[1]["payload"]["profile_id"] == "prof-7"
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
    assert row["payload"].get("worker_session_id") is None
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


@pytest.mark.parametrize("pump_type,explicit_status,state,via_drain", [
    ("task_started", None, "started", False),
    ("task_progress", None, "running", False),
    ("task_notification", None, "notified", False),
    # The ProviderAgent WorkerFeed's RUNNING frame (provider_agent_feed.started):
    # a task_started carrying an EXPLICIT running status — which beats the
    # per-type "started" default — appended through the unfenced drain tap. That
    # tap admits it on the ``task_`` prefix alone, with no per-type allowlist.
    ("task_started", "running", "running", True),
])
async def test_task_events_map_to_task_updated(
    mock_db, pump_type, explicit_status, state, via_drain
):
    _mk_session()
    event = {
        "type": pump_type, "task_id": "t1", "description": "bg work",
        "summary": "half done", "tool_use_id": "tu_9",
    }
    if explicit_status is not None:
        event["status"] = explicit_status

    rec = None
    if via_drain:
        await record_drained_event(SID, event)
    else:
        rec = await _recorder()
        await rec.on_event(event)

    row = _rows()[-1]
    assert row["event_type"] == "task.updated"
    assert row["payload"]["task_id"] == "t1"
    assert row["payload"]["state"] == state
    assert row["payload"]["description"] == "bg work"
    assert row["payload"]["summary"] == "half done"
    assert row["payload"]["tool_use_id"] == "tu_9"
    if rec is not None:
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
    # {"type": "system", "subtype": "task_updated"} chip. It must land as
    # task.updated — a system.note here leaves the replayed task strip stuck on
    # "running".
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


# ---------------------------------------------------------------------------
# record_task_terminal — the wake turn's claimed completions
# ---------------------------------------------------------------------------

async def test_record_task_terminal_settles_a_shell_frame(mock_db):
    # A background shell that finished BETWEEN turns is delivered by the wake
    # turn's pending_frames. That wake is the only place the log learns the task
    # is done, so the terminal task.updated must land here — with the registry
    # kind, or the frontend badges the shell as an agent.
    from app.core.task_registry import task_registry
    task_registry.reset_all()
    task_registry.start_task(SID, "task-X", "shell", "npm test", tool_use_id="tu_x")
    _mk_session()
    rec = await _recorder()
    await rec.start("background task completed")

    appended = await rec.record_task_terminal([
        {"task_id": "task-X", "status": "completed", "summary": "exit 0"},
    ])

    assert appended == 1
    row = _rows()[-1]
    assert row["event_type"] == "task.updated"
    assert row["payload"]["task_id"] == "task-X"
    assert row["payload"]["state"] == "completed"
    assert row["payload"]["kind"] == "shell"
    assert row["payload"]["summary"] == "exit 0"
    assert row["payload"]["tool_use_id"] == "tu_x"
    assert row["turn_id"] == rec.turn_id
    await rec.close()
    task_registry.reset_all()


@pytest.mark.parametrize("status,state", [
    ("completed", "completed"),
    ("failed", "failed"),
    ("cancelled", "cancelled"),
    # Unknown / missing status still settles the card rather than leaving it
    # running forever.
    ("weird", "completed"),
    (None, "completed"),
])
async def test_record_task_terminal_maps_frame_status(mock_db, status, state):
    _mk_session()
    rec = await _recorder()
    frame = {"task_id": "t1"}
    if status is not None:
        frame["status"] = status

    assert await rec.record_task_terminal([frame]) == 1

    row = _rows()[-1]
    assert row["event_type"] == "task.updated"
    assert row["payload"]["state"] == state
    await rec.close()


async def test_record_task_terminal_takes_kind_from_the_frame(mock_db):
    # An unregistered task (older log, or a registry entry already reaped) still
    # gets its badge when the frame carries the kind itself.
    from app.core.task_registry import task_registry
    task_registry.reset_all()
    _mk_session()
    rec = await _recorder()

    await rec.record_task_terminal([
        {"task_id": "t-unreg", "status": "failed", "kind": "shell"},
    ])

    row = _rows()[-1]
    assert row["payload"]["kind"] == "shell"
    assert row["payload"]["state"] == "failed"
    await rec.close()


async def test_record_task_terminal_ignores_junk_frames(mock_db):
    # No frames, non-dict frames and frames without a task_id append nothing —
    # an empty task_id makes the frontend reducer bail anyway.
    _mk_session()
    rec = await _recorder()
    await rec.start("hi")
    before = len(_rows())

    assert await rec.record_task_terminal([]) == 0
    assert await rec.record_task_terminal(["nope", {"status": "completed"}]) == 0

    assert len(_rows()) == before
    await rec.close()


async def test_record_task_terminal_never_raises(mock_db):
    # Best-effort: a broken append must not kill the wake turn.
    _mk_session()
    rec = await _recorder()
    rec._append = AsyncMock(side_effect=RuntimeError("boom"))

    assert await rec.record_task_terminal([
        {"task_id": "t1", "status": "completed"},
    ]) == 0
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


async def test_provider_alive_maps_to_turn_provider_alive(mock_db):
    # #1418: fluid's ProviderAlive, recorded as worker liveness evidence.
    _mk_session()
    rec = await _recorder()
    await rec.on_event({"type": "provider_alive", "signal": "reasoning"})
    row = _rows()[-1]
    assert row["event_type"] == "turn.provider_alive"
    assert row["payload"]["signal"] == "reasoning"
    await rec.close()


async def test_provider_alive_is_recorded_once_while_the_log_is_busy(mock_db):
    # fluid sends one per model call. A turn whose log keeps moving records
    # the first only; the rows around it already prove the stream is alive.
    _mk_session()
    rec = await _recorder()
    await rec.on_event({"type": "provider_alive", "signal": "accepted"})
    await rec.on_event({"type": "tool_use", "id": "t1", "name": "Read", "input": {}})
    await rec.on_event({"type": "provider_alive", "signal": "accepted"})
    await rec.on_event({"type": "provider_alive", "signal": "keepalive"})
    assert _types().count("turn.provider_alive") == 1
    await rec.close()


async def test_provider_alive_is_recorded_again_after_a_silent_gap(mock_db, monkeypatch):
    # A reasoning stream that stays silent past the gap gets another row, so
    # the log can show it is still open.
    _mk_session()
    rec = await _recorder()
    await rec.on_event({"type": "provider_alive", "signal": "accepted"})
    monkeypatch.setattr(tr, "PROVIDER_ALIVE_MIN_GAP_SECONDS", 0.0)
    await rec.on_event({"type": "provider_alive", "signal": "keepalive"})
    signals = [r["payload"]["signal"] for r in _rows()
               if r["event_type"] == "turn.provider_alive"]
    assert signals == ["accepted", "keepalive"]
    await rec.close()


async def test_indexed_model_call_usage_is_never_coalesced_and_keeps_request_ids(mock_db):
    _mk_session()
    rec = await _recorder()
    for index in (1, 2):
        await rec.on_event({
            "type": "turn_usage",
            "usage": {
                "model_call_index": index,
                "cost_usd": 0.25 * index,
                "provider_requests": {
                    "logical_call_id": f"call-{index}",
                    "attempts": [{"attempt": 1, "generation_id": f"gen-{index}"}],
                },
            },
        })
    usage_rows = [row for row in _rows() if row["event_type"] == "turn.usage"]
    assert [row["payload"]["usage"]["model_call_index"] for row in usage_rows] == [1, 2]
    assert usage_rows[1]["payload"]["usage"]["provider_requests"]["attempts"] == [
        {"attempt": 1, "generation_id": "gen-2"}
    ]
    await rec.close()


async def test_turn_usage_carries_context_snapshot(mock_db):
    _mk_session()
    rec = await _recorder()
    snapshot = {"categories": [], "totalTokens": 42, "maxTokens": 200_000}
    await rec.on_event({
        "type": "turn_usage",
        "usage": {"model_call_index": 1, "context_usage": snapshot},
    })
    row = _rows()[-1]
    assert row["event_type"] == "turn.usage"
    assert row["payload"]["context_usage"] == snapshot
    await rec.close()


async def test_turn_usage_without_snapshot_omits_the_key(mock_db):
    # Every turn.usage would otherwise carry a null; the payload is the wire
    # shape, and the reducer distinguishes "absent" from "cleared".
    _mk_session()
    rec = await _recorder()
    await rec.on_event({"type": "turn_usage", "usage": {"model_call_index": 1}})
    row = _rows()[-1]
    assert "context_usage" not in row["payload"]
    await rec.close()


async def test_coalescing_never_drops_a_context_snapshot(mock_db):
    # Coalescing discards whole usage records. A snapshot that arrived on a
    # discarded one is still the newest truth about the context, so the event
    # that eventually flushes has to carry it.
    _mk_session()
    rec = await _recorder()
    snapshot = {"categories": [], "totalTokens": 99, "maxTokens": 200_000}
    # First record opens the coalescing window and is emitted immediately.
    await rec.on_event({"type": "turn_usage", "usage": {"input_tokens": 1}})
    # These two land inside the window: the snapshot-bearing one is coalesced
    # over by the record after it.
    await rec.on_event({"type": "turn_usage", "usage": {"context_usage": snapshot}})
    await rec.on_event({"type": "turn_usage", "usage": {"input_tokens": 3}})
    await rec.close()

    usage_rows = [r for r in _rows() if r["event_type"] == "turn.usage"]
    assert usage_rows[-1]["payload"]["usage"] == {"input_tokens": 3}
    assert usage_rows[-1]["payload"]["context_usage"] == snapshot


async def test_latest_context_snapshot_wins(mock_db):
    _mk_session()
    rec = await _recorder()
    older = {"categories": [], "totalTokens": 10, "maxTokens": 200_000}
    newer = {"categories": [], "totalTokens": 20, "maxTokens": 200_000}
    for index, snap in ((1, older), (2, newer)):
        await rec.on_event({
            "type": "turn_usage",
            "usage": {"model_call_index": index, "context_usage": snap},
        })
    usage_rows = [r for r in _rows() if r["event_type"] == "turn.usage"]
    assert [r["payload"]["context_usage"] for r in usage_rows] == [older, newer]
    await rec.close()


async def test_usage_after_a_snapshot_still_reports_it(mock_db):
    # A later plain usage record must not blank the breakdown — the context did
    # not stop existing just because this record had nothing to say about it.
    _mk_session()
    rec = await _recorder()
    snapshot = {"categories": [], "totalTokens": 55, "maxTokens": 200_000}
    await rec.on_event({
        "type": "turn_usage",
        "usage": {"model_call_index": 1, "context_usage": snapshot},
    })
    await rec.on_event({"type": "turn_usage", "usage": {"model_call_index": 2}})
    usage_rows = [r for r in _rows() if r["event_type"] == "turn.usage"]
    assert usage_rows[-1]["payload"]["context_usage"] == snapshot
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


async def test_steered_user_message_lands_at_the_stream_position(mock_db):
    """A message steered into the LIVE turn is recorded where the model read it
    — after the tool result that carried it, on this turn, stamped with the
    QUEUED row's handle so the frontend collapses its pending bubble onto it."""
    _mk_session()
    rec = await _recorder(command_id="live-turn-cmd")
    await rec.on_event({"type": "stream_delta", "delta_type": "text", "content": "working"})
    await rec.on_event({
        "type": "tool_result", "tool_use_id": "tu_1", "content": "ok",
    })

    assert await rec.record_steered_user_message(
        "also check staging", profile_id="coder", command_id="queued-row-cmd",
    ) is True

    rows = _rows()
    assert [r["event_type"] for r in rows] == [
        "assistant.text_delta", "tool.result", "message.user",
    ]
    steered = rows[-1]
    assert steered["payload"]["content"] == "also check staging"
    assert steered["payload"]["profile_id"] == "coder"
    assert steered["payload"]["checkpoint_backed"] is False
    assert steered["turn_id"] == rec.turn_id
    assert steered["command_id"] == "queued-row-cmd"
    await rec.close()


async def test_steered_user_message_flushes_buffered_deltas_first(mock_db):
    """It is a non-delta event like any other: buffered text may not jump over
    the user's words."""
    _mk_session()
    rec = await _recorder()
    await rec.on_event({"type": "stream_delta", "delta_type": "text", "content": "half a "})
    await rec.record_steered_user_message("wait, stop")
    assert _types() == ["assistant.text_delta", "message.user"]
    await rec.close()


async def test_steered_user_message_is_inert_after_close(mock_db):
    """The turn is over — nothing more may be appended under its lease."""
    _mk_session()
    rec = await _recorder()
    await rec.close()
    assert await rec.record_steered_user_message("too late") is False


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


async def test_tool_input_deltas_carry_the_tool_name(mock_db):
    """A card opened by the first fragment needs a label, not an anonymous box.

    The native leg puts the name on each delta; the delegated CLI leaves it out
    and the open block is the fallback. Both paths have to end up naming it.
    """
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "stream_block_start", "block_type": "tool_use",
        "content_block": {"type": "tool_use", "id": "tu_open", "name": "Write"},
    })
    await rec.on_event({
        "type": "stream_delta", "delta_type": "tool_input",
        "content": '{"a":', "tool_name": "Write",
    })
    await rec.on_event({"type": "stream_block_stop", "index": 0})
    rows = _rows()
    assert rows[0]["payload"]["name"] == "Write"
    await rec.close()


async def test_tool_input_delta_name_falls_back_to_the_open_block(mock_db):
    """The delegated CLI sends no name on the delta itself."""
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "stream_block_start", "block_type": "tool_use",
        "content_block": {"type": "tool_use", "id": "tu_open", "name": "Edit"},
    })
    await rec.on_event({
        "type": "stream_delta", "delta_type": "tool_input", "content": '{"a":1}',
    })
    await rec.on_event({"type": "stream_block_stop", "index": 0})
    rows = _rows()
    assert rows[0]["payload"]["name"] == "Edit"
    await rec.close()


async def test_tool_input_delta_name_is_absent_when_nothing_knows_it(mock_db):
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "stream_block_start", "block_type": "tool_use",
        "content_block": {"type": "tool_use", "id": "tu_open"},
    })
    await rec.on_event({
        "type": "stream_delta", "delta_type": "tool_input", "content": '{"a":1}',
    })
    await rec.on_event({"type": "stream_block_stop", "index": 0})
    rows = _rows()
    assert rows[0]["payload"]["name"] is None
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
# record_voice_exchange (VW3 / VM9) — synthetic voice turns, no ``turns`` row
# ---------------------------------------------------------------------------

async def test_record_voice_exchange_writes_one_contiguous_batch(mock_db):
    # The VM9 shape: turn.started / message.user / message.assistant /
    # turn.completed, one synthetic ``voice-`` turn id, one append (contiguous
    # seqs), unfenced — exactly the CLI-wake pattern.
    _mk_session()
    turn_id = await tr.record_voice_exchange(
        SID, "how is the deploy going", "green for ten minutes",
        model="gpt-realtime-2.1",
        usage={"voice_realtime": {"total_tokens": 12}},
    )
    assert turn_id and turn_id.startswith("voice-")

    rows = _rows()
    assert [r["event_type"] for r in rows] == [
        "turn.started", "message.user", "message.assistant", "turn.completed",
    ]
    assert all(r["turn_id"] == turn_id for r in rows)
    assert all(r["run_id"] is None for r in rows)          # unfenced
    seqs = [r["seq"] for r in rows]
    assert seqs == list(range(seqs[0], seqs[0] + 4))       # one append_events call

    assert rows[1]["payload"]["content"] == "how is the deploy going"
    assert rows[1]["payload"]["source"] == "voice"         # extra_payload note
    assert rows[2]["payload"]["content"] == "green for ten minutes"
    assert rows[2]["payload"]["kind"] == "text"
    assert rows[2]["payload"]["model"] == "gpt-realtime-2.1"
    assert rows[3]["payload"]["stop_reason"] == "end_turn"
    assert rows[3]["payload"]["usage"] == {"voice_realtime": {"total_tokens": 12}}


async def test_record_voice_exchange_tolerates_missing_user_text(mock_db):
    # A model-initiated response (or a transcription that never arrived) simply
    # omits the message.user event.
    _mk_session()
    await tr.record_voice_exchange(SID, None, "your agent just finished")
    assert _types() == ["turn.started", "message.assistant", "turn.completed"]


async def test_record_voice_exchange_writes_nothing_when_both_sides_empty(mock_db):
    _mk_session()
    assert await tr.record_voice_exchange(SID, "  ", None) is None
    assert _rows() == []


async def test_record_voice_exchange_mirrors_into_session_messages(mock_db):
    _mk_session()
    await tr.record_voice_exchange(SID, "hello there", "hi, what can I do")
    messages = [
        (m["role"], m["content"]) for m in sessions_db.get_session_messages(SID)
    ]
    assert messages == [("user", "hello there"), ("assistant", "hi, what can I do")]
    assert sessions_db.get_session(SID)["turn_count"] == 1


async def test_record_voice_exchange_backfills_the_placeholder_title(mock_db):
    # First exchange renames "Voice session"; later ones leave the title alone.
    sessions_db.create_session(SID, "default", title=tr.VOICE_PLACEHOLDER_TITLE)
    await tr.record_voice_exchange(SID, "spin up the release checklist", "on it")
    assert sessions_db.get_session(SID)["title"] == "spin up the release checklist"

    await tr.record_voice_exchange(SID, "and the changelog", "sure")
    assert sessions_db.get_session(SID)["title"] == "spin up the release checklist"
    assert sessions_db.get_session(SID)["turn_count"] == 2


async def test_record_voice_exchange_title_truncates_on_a_word_boundary(mock_db):
    sessions_db.create_session(SID, "default", title=tr.VOICE_PLACEHOLDER_TITLE)
    spoken = "word " * 40
    await tr.record_voice_exchange(SID, spoken, "ok")
    title = sessions_db.get_session(SID)["title"]
    assert title.endswith("...")
    assert len(title) <= 103


async def test_record_voice_exchange_does_not_retitle_a_named_session(mock_db):
    sessions_db.create_session(SID, "default", title="Release prep")
    await tr.record_voice_exchange(SID, "hello", "hi")
    assert sessions_db.get_session(SID)["title"] == "Release prep"


async def test_record_voice_exchange_stores_a_bridge_note_as_system(mock_db):
    # A backend-injected note ("[call started]", "[agent update]") is not the
    # user's words: it persists as a tagged ``system`` row so it cannot title
    # the session or come back as history on the next call.
    sessions_db.create_session(SID, "default", title=tr.VOICE_PLACEHOLDER_TITLE)
    await tr.record_voice_exchange(
        SID, None, "Welcome back — we were on the release checklist.",
        system_text="[call started] The user just joined the call.",
    )

    rows = _rows()
    assert [r["event_type"] for r in rows] == [
        "turn.started", "message.user", "message.assistant", "turn.completed",
    ]
    assert rows[1]["payload"]["source"] == tr.VOICE_SYSTEM_NOTE_SOURCE

    messages = sessions_db.get_session_messages(SID)
    assert [(m["role"], m["content"]) for m in messages] == [
        ("system", "[call started] The user just joined the call."),
        ("assistant", "Welcome back — we were on the release checklist."),
    ]
    assert messages[0]["metadata"] == {"source": tr.VOICE_SYSTEM_NOTE_SOURCE}
    assert sessions_db.get_session(SID)["title"] == tr.VOICE_PLACEHOLDER_TITLE


async def test_record_voice_exchange_never_titles_from_a_machine_note(mock_db):
    # Belt-and-braces: both audited voice sessions were titled "[call started]
    # The user just joined the call…" because the greeting reached the title
    # deriver as user text.
    sessions_db.create_session(SID, "default", title=tr.VOICE_PLACEHOLDER_TITLE)
    await tr.record_voice_exchange(
        SID, "[call started] The user just joined the call.", "hello",
    )
    assert sessions_db.get_session(SID)["title"] == tr.VOICE_PLACEHOLDER_TITLE


async def test_record_voice_exchange_writes_a_note_only_exchange(mock_db):
    _mk_session()
    turn_id = await tr.record_voice_exchange(
        SID, None, None, system_text="[agent update] Worker 'x' finished.",
    )
    assert turn_id is not None
    # The assistant event is unconditional (same as a user-only exchange).
    assert _types() == [
        "turn.started", "message.user", "message.assistant", "turn.completed",
    ]
    assert _rows()[1]["payload"]["source"] == tr.VOICE_SYSTEM_NOTE_SOURCE


async def test_record_voice_exchange_never_raises(mock_db, monkeypatch):
    _mk_session()

    def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(tr.session_events_db, "append_events", _boom)
    assert await tr.record_voice_exchange(SID, "hi", "hello") is None


async def test_record_voice_exchange_survives_side_effect_failure(mock_db, monkeypatch):
    # The event log is the transcript authority: a session_messages failure must
    # not lose the exchange that was already appended.
    _mk_session()
    from app.db import database as _db

    def _boom(*args, **kwargs):
        raise RuntimeError("no fts")

    monkeypatch.setattr(_db, "add_session_message", _boom)
    turn_id = await tr.record_voice_exchange(SID, "hi", "hello")
    assert turn_id is not None
    assert _types() == [
        "turn.started", "message.user", "message.assistant", "turn.completed",
    ]


async def test_a_thinking_subtype_boundary_is_a_delta_boundary(mock_db):
    """The provider's reasoning summary and its reasoning text are different
    voices: they never coalesce into one thinking delta, and each emitted
    delta carries the subtype the UI labels on."""
    _mk_session()
    rec = await _recorder()
    await rec.on_event({
        "type": "stream_delta", "delta_type": "thinking", "content": "sum ",
        "subtype": "reasoning_summary",
    })
    await rec.on_event({
        "type": "stream_delta", "delta_type": "thinking", "content": "mary",
        "subtype": "reasoning_summary",
    })
    await rec.on_event({
        "type": "stream_delta", "delta_type": "thinking", "content": "real",
        "subtype": "reasoning",
    })
    await rec.on_event({"type": "stream_delta", "delta_type": "thinking", "content": "plain"})
    await rec.on_event({"type": "tool_use", "name": "Read", "id": "tu_9", "input": {}})
    rows = [r for r in _rows() if r["event_type"] == "assistant.thinking_delta"]
    assert [(r["payload"]["text"], r["payload"].get("subtype")) for r in rows] == [
        ("sum mary", "reasoning_summary"),
        ("real", "reasoning"),
        ("plain", None),
    ]
    await rec.close()
