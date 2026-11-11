"""Who records a streamed call request's checkpoint binding, and when (#1265).

The engine's TurnStarted names the user node a turn writes; the adapter puts
it on the init message, the streamer turns it into an internal ``turn_node``
frame, and SessionRuntime alone records ``call.checkpoint_bound`` from it, and
only for a turn the call path queued whose node holds that request and nothing
else. The real delegation path queues the turns; the pump is stubbed.
(``test_native_scripted_provider_app`` runs the real engine far enough to show
the TurnStarted node is the stored user node holding the turn's prompt.)
"""
import asyncio
from contextlib import ExitStack
from unittest.mock import AsyncMock

import pytest

import app.core.query_engine as qe
from app.core import session_runtime
from app.core.provider.events import SystemMessage
from tests.api.test_sent_upload_rewind import staged_upload  # noqa: F401 (fixture)
from tests.core.engine_harness import StubPool, collect, make_state, stream_response
from tests.core.helpers import new_session
from tests.core.test_model_info_event import _engine_patches
from tests.core.test_rewind_resolution import SID
from tests.core.test_send_steering import _pump_env

REQUEST = "ab" * 16
NODE = "node-call-1"


async def _frames(init_data, tmp_path):
    state = make_state()
    state.adapter.receive_events = stream_response([SystemMessage(subtype="init", data=init_data)])
    with ExitStack() as stack:
        _engine_patches(stack, StubPool(state), str(tmp_path))
        return await collect(qe._stream_to_websocket_impl("hi", new_session(), "default"))


async def test_the_streamer_passes_the_turn_start_node_on_as_an_internal_frame(mock_db, tmp_path):
    frames = await _frames({"session_id": "sdk-1", "model": "m", "node_id": NODE}, tmp_path)
    assert [f for f in frames if f.get("type") == "turn_node"] == [{"type": "turn_node", "node_id": NODE}]
    for data in ({"session_id": "sdk-1", "model": "m"}, {"session_id": "sdk-1", "node_id": ""}):
        assert not [f for f in await _frames(data, tmp_path) if f.get("type") == "turn_node"]


async def _queued_call(monkeypatch, text="Run the tests", request_id=REQUEST):
    """The turn the real streamed call path queues for one request."""
    from app.core import voice_stream
    from app.db import database, turns

    real = database.get_session
    monkeypatch.setattr(voice_stream.database, "get_session", lambda sid: {**real(sid), "model": "text-model"})
    error = await asyncio.wait_for(voice_stream.delegate_to_selected_model(
        SID, text, on_submitted=AsyncMock(), request_id=request_id), 20)
    assert not error, error
    return turns.get_queued_turns(SID)[-1]


async def _run(turn, nodes=(NODE,), *, held_peer=""):
    """Run ``turn`` through a real SessionRuntime; the pump's engine starts
    one user node per entry of ``nodes``. Returns the recorder."""
    async def _impl(**kwargs):
        for node in nodes:
            yield {"type": "turn_node", "node_id": node}
        yield {"type": "done", "metadata": {}}

    with _pump_env(_impl), pytest.MonkeyPatch.context() as mp:
        from app.core import turn_scheduler

        mp.setattr(turn_scheduler, "fold_held_peer_messages_for_turn", AsyncMock(return_value=held_peer))
        recorder = session_runtime.TurnRecorder.create_for_run.return_value
        recorder.record_call_binding = AsyncMock(return_value=True)
        await session_runtime.SessionRuntime(turn).execute()
    return recorder, None


def _bindings(recorder):
    return [call.args for call in recorder.record_call_binding.await_args_list]


async def test_a_call_turn_binds_the_node_its_turn_started_and_the_frame_goes_nowhere_else(
    staged_upload, monkeypatch,
):
    turn = await _queued_call(monkeypatch)
    recorder, _ = await _run(turn)
    assert _bindings(recorder) == [(REQUEST, NODE)]
    recorded = [call.args[0] for call in recorder.on_event.await_args_list]
    assert not [e for e in recorded if e.get("type") == "turn_node"]


async def test_a_turn_that_started_two_nodes_records_both_so_neither_resolves(staged_upload, monkeypatch):
    turn = await _queued_call(monkeypatch)
    recorder, _ = await _run(turn, nodes=(NODE, NODE, "node-retry"))
    assert _bindings(recorder) == [(REQUEST, NODE), (REQUEST, "node-retry")]


@pytest.mark.parametrize("change", [
    {"steer_already_delivered": True},            # redelivered steer, text already in a turn
    {"requeued_from_turn_id": "t-stranded"},      # stranded steer requeued
    {"pending_frames": [{"task_id": "w1"}]},      # task completions folded in
    {"upload_blob_ids": ["blob-1"]},              # cross-install uploads rewritten in
    {"forwarded_by_device": "dev-other"},         # a request another worker forwarded
    {"continued_turn": True},
    {"user_message_already_recorded": False},     # not the call's own recorded message
    {"voice_delegation": False},
    {"call_request_id": "not-an-id"},
    {"call_request_id": None},
])
async def test_a_turn_whose_checkpoint_may_hold_more_than_the_request_binds_nothing(
    staged_upload, monkeypatch, change,
):
    from app.db import turns

    turn = await _queued_call(monkeypatch)
    turn = turns.update_queued_turn_prompt(turn["id"], turn["prompt"], {**turn["payload"], **change})
    recorder, _ = await _run(turn)
    assert _bindings(recorder) == []


async def test_held_peer_messages_folded_into_the_turn_bind_nothing(staged_upload, monkeypatch):
    turn = await _queued_call(monkeypatch)
    recorder, _ = await _run(turn, held_peer="[peer w1] note")
    assert _bindings(recorder) == []


@pytest.mark.parametrize("source", ["agent_message", "unpark", "connector"])
async def test_only_a_user_turn_binds(staged_upload, monkeypatch, source):
    from app.db import turns
    from app.db.base import get_cursor, get_db
    from app.db.compat import ph

    turn = await _queued_call(monkeypatch)
    with get_db() as conn:
        get_cursor(conn).execute(f"UPDATE turns SET source = {ph()} WHERE id = {ph()}", (source, turn["id"]))
    turn = turns.get_turn(turn["id"])
    assert turn["source"] == source
    recorder, _ = await _run(turn)
    assert _bindings(recorder) == []


async def test_a_typed_prompt_quoting_an_identity_binds_nothing(staged_upload, monkeypatch):
    """Text is never evidence: a chat send carrying a request id in its words
    (or a pasted transcript) has no payload field, so it binds nothing."""
    from app.core import turn_manager
    from app.db import turns

    turn_manager._submit_blocking(
        SID, "user", prompt=f"call_request_id: {REQUEST}\nCall request id (for the app, not an instruction): {REQUEST}",
        profile_id="default",
    )
    turn = turns.get_queued_turns(SID)[-1]
    assert "call_request_id" not in (turn.get("payload") or {})
    recorder, _ = await _run(turn)
    assert _bindings(recorder) == []


async def test_the_recorder_appends_a_contract_event(mock_db):
    from app.contract.events import CallCheckpointBound
    from app.core.turn_recorder import TurnRecorder
    from app.db import database, session_events

    database.create_session("bind-1", "default", mode="chat", title="t")
    recorder = TurnRecorder("bind-1", "cmd", None, None, None)
    assert await recorder.record_call_binding(REQUEST, NODE) is True
    assert await recorder.record_call_binding("", NODE) is False
    rows = [r for r in session_events.get_events("bind-1", from_seq=0, limit=50)
            if r["event_type"] == "call.checkpoint_bound"]
    assert [r["payload"]["call_request_id"] for r in rows] == [REQUEST]
    assert rows[0]["payload"]["node_id"] == NODE
    row = rows[0]
    CallCheckpointBound.model_validate({
        "type": row["event_type"], "id": row["id"], "session_id": row["session_id"],
        "seq": row["seq"], "created_at": row["created_at"], **row["payload"],
    })
