"""A forwarding sender supersedes its own unreplicated settle revision."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.requests import Request

from app.api.chat_query import (
    HTTPQuerySender,
    _request_query_principal,
    submit_http_query,
)
from app.api.chat_ws_handler import ChatWebSocketHandler
from app.core import placement, turn_manager
from app.core.sync_engine import sync_engine
from app.core.user_question_handler import user_question_handler
from app.db import database, interactions, turns
from app.db import network as store
from app.network import control, engine, session_active
from tests.core.network.harness import converge, exchange, join
from tests.core.network.test_control import _new_session


@pytest.mark.parametrize("send_kind", ["send", "compact_first", "queue"])
@pytest.mark.parametrize("wait_kind", ["permission", "question"])
async def test_forwarded_send_supersedes_delayed_sender_settle(workers, monkeypatch, send_kind, wait_kind):
    sender, runner = workers("sender"), workers("runner")
    from app.db import mobile_tool_identity
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
    monkeypatch.setattr(placement, "resolve_placement", lambda row: SimpleNamespace(kind="runner", runner_id=runner.device_id))
    with sender.active():
        await session_active.set_shared("forwarded", False)
        delayed = [op for op in store.ops_since({}) if op["collection"] == "session_active"
                   and op["origin_device_id"] == sender.device_id]
        assert delayed
    with runner.active():
        assert database.get_session("forwarded")["is_active"]  # settle has NOT replicated

    broadcast = AsyncMock()
    monkeypatch.setattr(sync_engine, "broadcast_session_updated", broadcast)

    async def deliver(device, action, body):
        assert device == runner.device_id
        assert action == ("compact-first" if send_kind == "compact_first" else "turns")
        # The forwarding device must already have superseded its own settle and
        # told its clients before the network mutation is dispatched.
        assert database.get_session("forwarded")["is_active"]
        broadcast.assert_awaited_once_with(
            session_id="forwarded", updates={"is_active": True}, source_device_id=None,
        )
        with runner.active():
            return await control._turns(body, sender.device_id)

    monkeypatch.setattr(control, "call", deliver)
    request = Request({"type": "http", "headers": [], "state": {"auth_token": "local-owner"}})
    with sender.active():
        if send_kind == "queue":
            handler = HTTPQuerySender(request, "forwarded", _request_query_principal(request))
            await handler.handle_queue_message({"session_id": "forwarded", "content": "continue", "profile": "default"})
        else:
            await submit_http_query(request, "forwarded", {"prompt": "continue"}, compact_first=send_kind == "compact_first")
    with runner.active():
        admitted = turns.get_queued_turns("forwarded")
        assert admitted
        turns.start_turn(admitted[0]["id"])
        if wait_kind == "question":
            await user_question_handler.post_question(
                request_id="new-question", tool_use_id="new-tool", session_id="forwarded",
                questions=[{"question": "Keep going?"}], broadcast_func=AsyncMock(),
            )
        else:
            interactions.create_interaction("forwarded", "permission", {"request_id": "new-permission"}, 300)
        [wait] = interactions.get_pending_interactions("forwarded")
    # Signed catch-up delivers the previously withheld settle AND its later
    # sender reactivation. Dismissal observes the final membership of the batch.
    result = exchange(sender, runner)
    assert not result.rejected and not result.retry
    with runner.active():
        await session_active.dismiss_replicated(["forwarded"])
        # A late duplicate of the old signed settle cannot undo the newer send.
        result = engine.apply_batch(delayed)
        assert not result.rejected and not result.retry
        await session_active.dismiss_replicated(["forwarded"])
        assert interactions.get_interaction(wait["id"])["state"] == "pending"
        assert turns.get_turn(admitted[0]["id"])["state"] == "running"
    for worker in (sender, runner):
        with worker.active():
            assert database.get_session("forwarded")["is_active"]
