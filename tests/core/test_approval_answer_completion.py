"""#1092: a reply already consumed by an approval must not start a model turn.

Drive real approval intake and turn execution with a scripted pump. The final
blocked call finishes before intake can enqueue the answer, deterministically
covering the completion race without depending on a scheduler delay.
"""
import asyncio
from unittest.mock import patch

import pytest

from app.core import owner_confirm as confirm, session_runtime, turn_manager
from app.core.agent_types import PermissionResultAllow, PermissionResultDeny
from app.db import session_events, turns
from tests.core.test_approval_pending_message_delivery import (  # noqa: F401
    _own_call, _parked, _pending, assistant_fixture, clean_confirm, home,
    no_connector_delivery, no_gateway_listener, no_push, quiet_scheduler,
    reset_backfill_memo_on_setup, reset_interaction_wait_state,
)
from tests.core.test_steering_delivery_flows import (  # noqa: F401
    _Adapter, _clean_module_state, _pooled, _pump_env,
)


@pytest.mark.parametrize("timing", ["after_completion", "undelivered", "delivered"])
@pytest.mark.parametrize("reply,allowed", [("yes", True), ("no", False)])
async def test_answer_at_final_call_is_recorded_without_an_extra_model_request(
    home, monkeypatch, reply, allowed, timing,
):
    sid, _ = home
    live = turns.enqueue_turn(sid, "user", prompt="Run the final command",
                              profile_id="assistant-agent")
    requests = []
    results = []
    queued = asyncio.Event()
    adapter = _Adapter()

    async def fake_ai(**kwargs):
        requests.append(kwargs["prompt"])
        if len(requests) == 1:
            results.append(await _own_call(sid))
            if timing != "after_completion":
                await asyncio.wait_for(queued.wait(), 5)
                assert await session_runtime.try_steer_pending(sid) == 1
                [(text, steer_id)] = adapter.calls
                assert text == reply
                if timing == "delivered":
                    yield {"type": "steer_delivered", "steers": [{"id": steer_id, "text": text}]}
                # Otherwise the final round drops the steer without a receipt.
            yield {"type": "chunk", "content": "The final call is finished."}
        yield {"type": "done", "metadata": {}}

    real_answer = confirm.on_owner_message
    live_task = None

    async def answer_then_finish(*args):
        answer = await real_answer(*args)
        # respond() releases the permission waiter before returning to intake.
        # Let its last round finish before _submit_blocking writes the reply.
        if timing == "after_completion":
            assert await live_task == "completed"
        return answer

    monkeypatch.setattr(confirm, "on_owner_message", answer_then_finish)
    real_recorder = session_runtime.TurnRecorder.create_for_run
    # Keep real fenced event writes: transcript ordering is part of the bug.
    with _pump_env(fake_ai), _pooled(adapter), patch.object(
        session_runtime.TurnRecorder, "create_for_run", real_recorder,
    ):
        live_task = asyncio.create_task(session_runtime.SessionRuntime(live).execute())
        try:
            [request] = await _pending(sid)
            reply_id = await turn_manager.submit_async(
                sid, "user", prompt=reply, profile_id="assistant-agent",
                command_id=f"answer-{reply}", payload={"accepted_input_id": f"input-{reply}"})
            queued.set()
            assert await live_task == "completed"
            answer_turn = turns.get_turn(reply_id)
            assert answer_turn["payload"][confirm.ANSWER_KEY]["released"] is False
            assert isinstance(results[0], PermissionResultAllow if allowed else PermissionResultDeny)
            assert confirm.pending_requests(sid) == []

            # This is the ordinary queue drain after the last round. It must
            # retain answer/outcome order and identity, but not call the model.
            if timing != "delivered":
                assert await session_runtime.SessionRuntime(answer_turn).execute() == "completed"
            assert requests == ["Run the final command"]
            outcome = "approved" if allowed else "declined"
            events = session_events.get_events(sid)
            answers = [e for e in events if e["event_type"] == "message.user"
                       and e["payload"]["content"] == reply]
            outcomes = [e for e in events if e["event_type"] == "system.note"
                        and e["payload"].get("subtype") == confirm.SUBTYPE]
            assert len(answers) == len(outcomes) == 1
            assert outcomes[0]["payload"]["data"]["request_id"] == request["request_id"]
            assert outcomes[0]["payload"]["data"]["decision"] == outcome
            assert events.index(outcomes[0]) == events.index(answers[0]) + 1
            assert answers[0]["command_id"] == f"answer-{reply}"
            assert answer_turn["payload"]["accepted_input_id"] == f"input-{reply}"
            if timing != "delivered":
                assert answers[0]["payload"]["accepted_input_id"] == f"input-{reply}"
            assert turns.get_turn(reply_id)["state"] == "completed"
            completed_id = live["id"] if timing == "delivered" else reply_id
            assert any(e["event_type"] == "turn.completed" and e["turn_id"] == completed_id
                       for e in events)

            followup_id = await turn_manager.submit_async(
                sid, "user", prompt="Explain the result", profile_id="assistant-agent")
            assert await session_runtime.SessionRuntime(turns.get_turn(followup_id)).execute() == "completed"
            assert requests == ["Run the final command", "Explain the result"]
            assert turns.get_queued_turns(sid) == []
        finally:
            if not live_task.done():
                live_task.cancel()
                await asyncio.gather(live_task, return_exceptions=True)


@pytest.mark.parametrize("reply,allowed", [("yes", True), ("no", False)])
async def test_answer_to_a_released_call_still_reaches_the_model(home, monkeypatch, reply, allowed):
    sid, _ = home
    command = "git status"
    await _parked(sid, monkeypatch, command=command)
    requests = []
    passes = []

    async def fake_ai(**kwargs):
        requests.append(kwargs["prompt"])
        passes.append(confirm.take_approved_once(sid, "Bash", {"command": command}))
        yield {"type": "done", "metadata": {}}

    with _pump_env(fake_ai):
        [other] = turns.get_queued_turns(sid)
        assert await session_runtime.SessionRuntime(other).execute() == "completed"
        answer_id = await turn_manager.submit_async(
            sid, "user", prompt=reply, profile_id="assistant-agent")
        answer_turn = turns.get_turn(answer_id)
        assert answer_turn["payload"][confirm.ANSWER_KEY]["released"] is True
        assert await session_runtime.SessionRuntime(answer_turn).execute() == "completed"
        assert len(requests) == 2 and reply in requests[-1]
        assert passes == [False, allowed]
        assert not confirm.take_approved_once(sid, "Bash", {"command": command})
        assert turns.get_queued_turns(sid) == []


async def test_an_ordinary_undelivered_message_still_runs_as_the_next_turn(home):
    sid, _ = home
    live = turns.enqueue_turn(sid, "user", prompt="Finish the current work",
                              profile_id="assistant-agent")
    queued = asyncio.Event()
    requests = []
    adapter = _Adapter()

    async def fake_ai(**kwargs):
        requests.append(kwargs["prompt"])
        if len(requests) == 1:
            await asyncio.wait_for(queued.wait(), 5)
            assert await session_runtime.try_steer_pending(sid) == 1
            assert adapter.calls[0][0] == "Explain the result"
            # No delivery receipt: ordinary input must survive completion.
        yield {"type": "done", "metadata": {}}

    with _pump_env(fake_ai), _pooled(adapter):
        live_task = asyncio.create_task(session_runtime.SessionRuntime(live).execute())
        try:
            followup_id = await turn_manager.submit_async(
                sid, "user", prompt="Explain the result", profile_id="assistant-agent")
            queued.set()
            assert await live_task == "completed"
            followup = turns.get_turn(followup_id)
            assert followup["state"] == "queued"
            assert await session_runtime.SessionRuntime(followup).execute() == "completed"
            assert requests == ["Finish the current work", "Explain the result"]
            assert turns.get_queued_turns(sid) == []
        finally:
            if not live_task.done():
                live_task.cancel()
                await asyncio.gather(live_task, return_exceptions=True)
