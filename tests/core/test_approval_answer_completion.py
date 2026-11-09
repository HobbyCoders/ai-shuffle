"""A typed yes is an ordinary message; older queued answers still settle (#1092).

Drive real approval intake and turn execution with a scripted pump.
"""
import asyncio
from unittest.mock import patch

import pytest

from app.core import owner_confirm as confirm, session_runtime, turn_manager
from app.core.agent_types import PermissionResultDeny
from app.db import session_events, turns
from tests.core.test_approval_pending_message_delivery import (  # noqa: F401
    _own_call, _pending, assistant_fixture, clean_confirm, home,
    no_connector_delivery, no_gateway_listener, no_push, quiet_scheduler,
    reset_backfill_memo_on_setup, reset_interaction_wait_state,
)
from tests.core.test_steering_delivery_flows import (  # noqa: F401
    _Adapter, _clean_module_state, _pooled, _pump_env,
)


async def test_a_typed_yes_at_the_final_call_runs_as_an_ordinary_turn(home):
    """In the app a yes approves nothing: the call is let go, the request stays
    on its prompt, and the message gets its own model turn."""
    sid, _ = home
    live = turns.enqueue_turn(sid, "user", prompt="Run the final command",
                              profile_id="assistant-agent")
    requests = []
    results = []

    async def fake_ai(**kwargs):
        requests.append(kwargs["prompt"])
        if len(requests) == 1:
            results.append(await _own_call(sid))
            yield {"type": "chunk", "content": "The final call is finished."}
        yield {"type": "done", "metadata": {}}

    with _pump_env(fake_ai), _pooled(_Adapter()):
        live_task = asyncio.create_task(session_runtime.SessionRuntime(live).execute())
        try:
            [request] = await _pending(sid)
            reply_id = await turn_manager.submit_async(
                sid, "user", prompt="yes", profile_id="assistant-agent")
            assert await live_task == "completed"
            assert isinstance(results[0], PermissionResultDeny) and results[0].message == confirm.NOT_RUN_YET
            answer_turn = turns.get_turn(reply_id)
            assert confirm.ANSWER_KEY not in (answer_turn["payload"] or {})
            assert await session_runtime.SessionRuntime(answer_turn).execute() == "completed"
            assert requests == ["Run the final command", "yes"]
            assert [r["request_id"] for r in confirm.pending_requests(sid)] == [request["request_id"]]
        finally:
            if not live_task.done():
                live_task.cancel()
                await asyncio.gather(live_task, return_exceptions=True)


async def test_a_reply_queued_as_an_answer_by_an_older_version_is_recorded(home):
    """#1092: a turn an older version already consumed as an answer records its
    outcome after the message and makes no model call."""
    sid, _ = home
    requests = []

    async def fake_ai(**kwargs):
        requests.append(kwargs["prompt"])
        yield {"type": "done", "metadata": {}}

    answer = {"request_id": "r-old", "interaction_id": "i-old", "question": "May I run ls?",
              "decision": "approved", "released": False}
    queued = turns.enqueue_turn(sid, "user", prompt="yes", profile_id="assistant-agent",
                                payload={confirm.ANSWER_KEY: answer})
    with _pump_env(fake_ai):
        assert await session_runtime.SessionRuntime(queued).execute() == "completed"
    assert requests == []
    events = session_events.get_events(sid)
    [outcome] = [e for e in events if e["event_type"] == "system.note"
                 and e["payload"].get("subtype") == confirm.SUBTYPE]
    assert outcome["payload"]["data"]["request_id"] == "r-old"


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
