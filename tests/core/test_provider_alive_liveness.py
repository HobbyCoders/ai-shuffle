"""#1418: fluid's ProviderAlive reaches the worker's log as liveness, not content.

A worker on a model with encrypted-only reasoning publishes nothing visible
until its reasoning item ends. fluid now publishes ``ProviderAlive`` when the
provider accepts the call. This drives that event through the real path a
worker turn takes:

* ``HarnessAdapter._translate`` turns the fluid event into a stream frame;
* the real ``_stream_to_websocket_impl`` pump yields ``provider_alive``;
* a real ``TurnRecorder`` bound to the live turn writes ``turn.provider_alive``;
* ``WorkerFeed`` counts that row as provider progress and mirrors nothing.

The subprocess/profile boundaries are stubbed with the same harness as
``test_native_usage_settlement_paths.py``.
"""

import asyncio
import uuid
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock

import pytest
from fluid.events.model import ProviderAlive, TurnComplete, TurnStarted

import app.core.query_engine as qe
from app.core import provider_agent_feed as feed_mod
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.events import StreamEvent
from app.core.session_pool import SessionState
from app.core.session_runtime import _is_trimmed_render_frame
from app.core.turn_recorder import TurnRecorder
from app.db import database
from app.db import runs as runs_db
from app.db import session_events as se
from app.db import turns as turns_db
from tests.core.engine_harness import StubPool, mock_adapter
from tests.core.test_native_usage_settlement_paths import _engine_patches

ENGINE = "ses_luna_worker"


def test_adapter_turns_provider_alive_into_a_non_content_stream_frame():
    [message] = HarnessAdapter()._translate(
        ProviderAlive(session_id=ENGINE, turn_id="ft-1", signal="reasoning")
    )
    assert isinstance(message, StreamEvent)
    assert message.event == {"type": "provider_alive", "signal": "reasoning"}
    assert message.parent_tool_use_id is None


def test_the_runtime_keeps_the_frame_off_the_ws_leg():
    assert _is_trimmed_render_frame("provider_alive")


async def _run_worker_turn(tmp_path, fluid_events):
    """One worker turn through the real pump and recorder; returns its id."""
    sid = f"worker-{uuid.uuid4()}"
    database.create_session(session_id=sid, profile_id="default")

    async def receive():
        translator = HarnessAdapter()
        for event in fluid_events:
            for message in translator._translate(event):
                yield message

    state = SessionState(adapter=mock_adapter(), is_connected=True)
    state.adapter.is_alive = MagicMock(return_value=True)
    state.adapter.send_query = AsyncMock()
    state.adapter.receive_events = MagicMock(side_effect=lambda: receive())

    frames = []
    with ExitStack() as stack:
        _engine_patches(stack, StubPool(state), str(tmp_path))
        run = runs_db.claim_run(sid, "runner-test", 60)
        turn = turns_db.enqueue_turn(sid, "provider_agent", prompt="go")
        turn = turns_db.start_turn(turn["id"], run["id"], "runner-test")
        recorder = await TurnRecorder.create_for_run(
            sid, run_id=run["id"], fence_token=run["fence_token"],
            turn_id=turn["id"], runner_id="runner-test",
        )
        assert type(recorder) is TurnRecorder, "worker path must use the real recorder"
        await recorder.start("go", profile_id="default")
        try:
            async for frame in qe._stream_to_websocket_impl(
                prompt="go", session_id=sid, profile_id="default",
            ):
                frames.append(frame)
                await recorder.on_event(frame)
        finally:
            await recorder.close()
    return sid, frames


@pytest.mark.asyncio
async def test_provider_alive_lands_in_the_worker_log_and_holds_the_feed(mock_db, tmp_path):
    events = [
        TurnStarted(session_id=ENGINE, turn_id="ft-1", seq=1,
                    model="gpt-5.6-luna", provider="openai_responses"),
        # What the engine publishes for an encrypted-only reasoning call
        # before the reasoning item ends: acceptance, then nothing.
        ProviderAlive(session_id=ENGINE, turn_id="ft-1", seq=2, signal="accepted"),
        TurnComplete(session_id=ENGINE, turn_id="ft-1", seq=3,
                     stop_reason="end_turn", num_model_calls=1),
    ]
    sid, frames = await _run_worker_turn(tmp_path, events)

    alive_frames = [f for f in frames if f.get("type") == "provider_alive"]
    assert alive_frames == [{"type": "provider_alive", "signal": "accepted"}]

    rows = se.get_events(sid)
    alive_rows = [r for r in rows if r["event_type"] == "turn.provider_alive"]
    assert [r["payload"]["signal"] for r in alive_rows] == ["accepted"], rows
    # Nothing content-shaped came of it.
    assert [(r["event_type"], r["payload"]) for r in rows if r["event_type"] in (
        "assistant.text_delta", "assistant.thinking_delta",
        "message.assistant", "tool.use", "tool.input_delta",
    )] == []

    feed = feed_mod.WorkerFeed(
        parent_session_id="parent-of-" + sid,
        worker_session_id=sid,
        label="thinker",
        provider_id="prov-openai",
        model_id="gpt-5.6-luna",
        prompt="go",
    )
    await feed.started()
    await feed.drain()
    assert feed.frames_forwarded == 0
    assert feed.provider_progress >= 1
    parent_types = [r["event_type"] for r in se.get_events("parent-of-" + sid)]
    assert parent_types == ["subagent.started", "task.updated"]


@pytest.mark.asyncio
async def test_a_turn_without_it_gives_the_feed_nothing(mock_db, tmp_path):
    """The same turn minus the event: the watchdog's no-start case."""
    events = [
        TurnStarted(session_id=ENGINE, turn_id="ft-1", seq=1,
                    model="gpt-5.6-luna", provider="openai_responses"),
    ]

    async def run():
        sid, _ = await _run_worker_turn(tmp_path, events)
        feed = feed_mod.WorkerFeed(
            parent_session_id="parent-of-" + sid, worker_session_id=sid,
            label="thinker", provider_id="prov-openai",
            model_id="gpt-5.6-luna", prompt="go",
        )
        await feed.started()
        await feed.drain()
        return sid, feed

    sid, feed = await asyncio.wait_for(run(), 10)
    assert "turn.provider_alive" not in [r["event_type"] for r in se.get_events(sid)]
    assert feed.frames_forwarded == 0
    assert feed.provider_progress == 0
