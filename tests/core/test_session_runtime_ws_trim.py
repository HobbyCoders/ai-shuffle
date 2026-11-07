"""WS-leg trimming of redundant render content (performance review W3).

The turn pump yields the COMPLETE assistant text block (``{"type": "text",
"content": ...}``, query_engine.py:1684) and a ``thinking_block`` marker after
the deltas that already carried the same characters. Neither had a dispatch case
in the frontend WS handler (``frontend/src/lib/stores/session/ws-message-handlers.ts``
— both fell through to ``default: break``), yet both were serialized, pushed to
the origin socket AND fanned out to every watching device, parsed, and dropped.
The same content already rides the SSE event feed as coalesced deltas.

Same class: ``tool_use`` carries the full tool input (a whole file body for
Write/Edit). The frontend's ``tool_use`` case reads only ``session_id`` and
``id`` (it maintains the pendingTools set); the rendered arguments come from the
SSE feed. So the FRAME is kept — it is correctness-bearing — but an oversized
input is replaced by a marker.

Nothing that a client needs for correctness is trimmed: every remaining frame
type (done / error / tool_use / tool_result / permission / question / session
lifecycle) still goes out in full.
"""

import asyncio
import json
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.core import session_runtime as sr
from app.db import session_events, sessions, turns
from tests.core.helpers import make_pump
from tests.core.test_session_runtime import (
    _clean_module_state,  # noqa: F401 — fixture
    _runtime_env,
)


# ─── W3: text / thinking_block are trimmed off the WS leg ───────────────────


@pytest.mark.parametrize("etype", ["text", "thinking_block"])
def test_redundant_render_frames_are_trimmed(etype):
    assert etype in sr._WS_TRIMMED_RENDER_FRAMES
    assert sr._is_trimmed_render_frame(etype) is True


@pytest.mark.parametrize(
    "etype",
    [
        # Lifecycle / coordination the client cannot reconstruct from the feed.
        "done", "error", "interrupted", "start",
        "tool_use", "tool_result",
        "subagent_tool_use", "subagent_tool_result",
        "permission_request", "user_question",
        "session_created", "queued_message_processing",
    ],
)
def test_correctness_bearing_frames_are_not_trimmed(etype):
    assert sr._is_trimmed_render_frame(etype) is False


def test_previously_trimmed_frames_stay_trimmed():
    """No regression on the pre-existing trim set."""
    for etype in ("chunk", "stream_delta", "stream_block_start",
                  "stream_block_stop", "stream_message_delta",
                  "task_progress", "system", "usage_update"):
        assert sr._is_trimmed_render_frame(etype) is True
    # Unkept subagent render frames keep their prefix rule.
    assert sr._is_trimmed_render_frame("subagent_chunk") is True
    assert sr._is_trimmed_render_frame("subagent_tool_use") is False


def test_trim_does_not_disturb_model_output_detection():
    """`text` / `thinking_block` still mark model output for the steer-receipt
    logic — that check runs BEFORE the WS trim, on the raw stream."""
    assert "text" in sr._MODEL_OUTPUT_FRAMES
    assert "thinking_block" in sr._MODEL_OUTPUT_FRAMES


# ─── W3: oversized tool inputs are capped on the WS leg ─────────────────────


def test_small_tool_input_passes_through_untouched():
    event = {"type": "tool_use", "id": "t1", "input": {"path": "a.py"}}
    assert sr._cap_ws_tool_input(event) is event


def test_oversized_tool_input_is_capped_without_mutating_the_recorded_frame():
    big = "x" * (sr._WS_MAX_TOOL_INPUT_CHARS * 4)
    event = {"type": "tool_use", "id": "t1", "input": {"content": big}}

    capped = sr._cap_ws_tool_input(event)

    assert capped is not event
    # The recorder already holds `event`; it must be untouched.
    assert event["input"] == {"content": big}
    assert capped["input"]["_ws_truncated"] is True
    assert capped["input"]["_original_chars"] > sr._WS_MAX_TOOL_INPUT_CHARS
    assert len(capped["input"]["_preview"]) == sr._WS_MAX_TOOL_INPUT_CHARS
    # Everything the frontend actually reads survives.
    assert capped["id"] == "t1"
    assert capped["type"] == "tool_use"
    # And the capped frame is still serializable for the wire.
    json.loads(json.dumps(capped))


def test_subagent_tool_use_input_is_capped_too():
    big = "y" * (sr._WS_MAX_TOOL_INPUT_CHARS * 2)
    event = {"type": "subagent_tool_use", "id": "t2", "input": {"content": big}}
    assert sr._cap_ws_tool_input(event)["input"]["_ws_truncated"] is True


def test_non_tool_frames_are_never_capped():
    big = "z" * (sr._WS_MAX_TOOL_INPUT_CHARS * 2)
    for etype in ("tool_result", "done", "permission_request"):
        event = {"type": etype, "input": {"content": big}}
        assert sr._cap_ws_tool_input(event) is event


def test_unserializable_tool_input_still_caps_instead_of_raising():
    class _Weird:
        def __repr__(self):
            return "W" * (sr._WS_MAX_TOOL_INPUT_CHARS * 2)

    event = {"type": "tool_use", "id": "t3", "input": _Weird()}
    capped = sr._cap_ws_tool_input(event)
    assert capped is not event
    assert capped["input"]["_ws_truncated"] is True


# Error frames must identify the same durable turn as the recorder's failure.
@pytest.mark.parametrize("source", ["user", "queue", "unpark"])
@pytest.mark.parametrize("supplied_turn_id", [None, "a-stale-provider-turn"])
async def test_pump_error_frame_identifies_recorded_failure(mock_db, source, supplied_turn_id):
    sid = "error-correlation"
    sessions.create_session(session_id=sid, profile_id=None)
    turn = turns.enqueue_turn(sid, source, prompt="do the thing")
    event = {"type": "error", "message": "The provider rejected this turn."}
    if supplied_turn_id is not None:
        event["turn_id"] = supplied_turn_id
    create_recorder = sr.TurnRecorder.create_for_run

    with _runtime_env(pump=make_pump([event])) as env, patch.object(
        sr.TurnRecorder, "create_for_run", wraps=create_recorder,
    ) as recorder_factory:
        assert await sr.SessionRuntime(turn).execute() == "aborted"

    (failure,) = [
        row for row in session_events.get_events(sid)
        if row["event_type"] == "turn.aborted"
    ]
    (frame,) = [
        call.args[1] for call in env["broadcaster"].await_args_list
        if call.args[1].get("type") == "error"
    ]
    assert failure["payload"]["message"] == frame["message"]
    assert frame["session_id"] == sid
    assert frame.get("turn_id") == failure["turn_id"] == turn["id"]
    assert recorder_factory.call_args.kwargs["turn_id"] == frame["turn_id"]


async def test_watchdog_error_frame_identifies_recorded_failure(mock_db):
    sid = "watchdog-error-correlation"
    sessions.create_session(session_id=sid, profile_id=None)
    turn = turns.enqueue_turn(sid, "user", prompt="run a tool")
    rt = sr.SessionRuntime(turn)
    create_recorder = sr.TurnRecorder.create_for_run

    async def wedged_pump(**kwargs):
        yield {"type": "tool_use", "id": "tool", "name": "Bash"}
        # Drive the watchdog's real terminalization after the recorder starts,
        # without a wall-clock delay or a live provider/tool dependency.
        await rt._terminalize_silent_turn(300)
        await asyncio.Event().wait()

    with _runtime_env(pump=wedged_pump) as env, patch.object(
        sr.TurnRecorder, "create_for_run", wraps=create_recorder,
    ):
        assert await asyncio.wait_for(rt.execute(), timeout=5) == "aborted"

    (failure,) = [
        row for row in session_events.get_events(sid)
        if row["event_type"] == "turn.aborted"
    ]
    (frame,) = [
        call.args[1] for call in env["broadcaster"].await_args_list
        if call.args[1].get("type") == "error"
    ]
    assert "progress watchdog" in frame["message"]
    assert frame["session_id"] == sid
    assert frame.get("turn_id") == failure["turn_id"] == turn["id"]
    assert turns.get_turn(turn["id"])["state"] == "aborted"


async def test_error_turn_id_survives_origin_and_device_fanout(monkeypatch):
    from app.core.sync_engine import SyncEngine

    origin = AsyncMock()
    engine = SyncEngine()
    viewer = AsyncMock()
    viewer.device_id = "watcher"
    # The device writer enqueues synchronously (it never waits for the socket).
    viewer.send_text = Mock(return_value=True)
    engine._connections["session"] = {"watcher": viewer}
    monkeypatch.setattr("app.core.sync_engine.sync_engine", engine)
    frame = {"type": "error", "session_id": "session", "turn_id": "durable-turn",
             "message": "The provider rejected this turn."}

    await sr._default_broadcast(
        "session", frame, source="user", origin_send=origin, origin_device_id="sender",
    )

    origin.assert_awaited_once_with(frame)
    wire_event = json.loads(viewer.send_text.call_args.args[0])
    assert wire_event["event_type"] == "stream_frame"
    assert wire_event["data"] == frame
