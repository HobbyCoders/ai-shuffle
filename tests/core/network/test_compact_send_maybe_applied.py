"""A forwarded compact-and-send whose answer was lost is not a refusal.

The page retires a send only when it certainly will not run (an HTTP 4xx). An
owner that took the send but whose answer never came back (``MaybeApplied``)
may still run it, so the REST intake must not report it as a 4xx.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.chat_query import submit_http_query
from app.api.chat_ws_handler import ChatWebSocketHandler
from app.core import placement, turn_manager
from app.db import turns
from app.network import control
from tests.core.network.harness import converge, join
from tests.core.network.test_control import _new_session


@pytest.mark.parametrize("answer", ["lost_after_it_was_taken", "never_reached"])
async def test_a_compact_send_the_owner_may_have_taken_is_not_refused(workers, monkeypatch, answer):
    sender, runner = workers("sender"), workers("runner")
    from app.db import mobile_tool_identity
    from app.network import engine
    for worker in (sender, runner):
        with worker.active():
            mobile_tool_identity.init_schema()
    with runner.active():
        engine.create_network("Test network")
    join(runner, sender)
    _new_session(runner, "forwarded", "Remote chat")
    converge([sender, runner])
    monkeypatch.setattr(turn_manager, "_kick", lambda sid: None)
    monkeypatch.setattr(ChatWebSocketHandler, "_try_auto_adopt", AsyncMock(return_value=False))
    monkeypatch.setattr(placement, "resolve_placement",
                        lambda row: SimpleNamespace(kind="runner", runner_id=runner.device_id))

    async def deliver(device, action, body):
        assert (device, action) == (runner.device_id, "compact-first")
        if answer == "never_reached":
            raise control.RunnerUnreachable("runner has no reachable address")
        with runner.active():
            await control._turns(body, sender.device_id)
        raise control.MaybeApplied("runner did not answer in time. It may still have been applied.")

    monkeypatch.setattr(control, "call", deliver)
    request = Request({"type": "http", "headers": [], "state": {"auth_token": "local-owner"}})
    command = "client-" + "a" * 32
    with sender.active(), pytest.raises(HTTPException) as raised:
        await submit_http_query(request, "forwarded", {"prompt": "continue", "client_message_id": command},
                                compact_first=True)
    with runner.active():
        admitted = [row for row in turns.get_queued_turns("forwarded") if row.get("command_id") == command]
    if answer == "lost_after_it_was_taken":
        # The owner queued it and may run it: a gateway timeout, never a refusal.
        assert admitted
        assert raised.value.status_code == 504
    else:
        # Nothing reached the owner: a refusal, so the page may retire the send.
        assert not admitted
        assert 400 <= raised.value.status_code < 500
