"""Local-owner smoke bridge: real route/host dispatch, no models or live workers."""
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from fluid.tools.registry import ToolOutcome

from app.api import auth, tools, sessions_helpers
from app.core import session_orchestration as orch


@pytest.fixture
def bridge(monkeypatch):
    app = FastAPI()
    app.include_router(tools.router)
    @app.middleware("http")
    async def ingress(request, call_next):
        request.scope["shuffle.ingress"] = request.headers.get("x-ingress", "local")
        return await call_next(request)
    monkeypatch.setattr(auth.owner_auth, "local_access", lambda r: r.headers.get("x-owner") == "yes")
    monkeypatch.setattr(auth.owner_auth, "same_origin", lambda r: None)
    allowed = AsyncMock(return_value={"id": "sender"})
    monkeypatch.setattr(sessions_helpers, "require_transcript_session", allowed)
    dispatch = AsyncMock(return_value=ToolOutcome(text="", ui={
        "session_id": "receiver", "turn_id": "turn-1", "delivery_status": "consumed"}))
    senders = []
    def factory(supplier):
        senders.append(supplier())
        return dispatch
    monkeypatch.setattr(orch, "make_handler", factory)
    with TestClient(app) as client:
        yield client, allowed, dispatch, senders


BODY = {"session_id": "receiver", "message": "probe", "request_id": "stable"}
PATH = "/api/v1/tools/sessions/sender/peer-message"


def test_bridge_reuses_host_handler_and_stable_arguments(bridge):
    client, allowed, dispatch, senders = bridge
    for _ in range(2):
        response = client.post(PATH, json=BODY, headers={"x-owner": "yes"})
        assert response.status_code == 200
        assert response.json() == {"session_id": "receiver", "turn_id": "turn-1", "delivery_status": "consumed"}
    assert senders == ["sender", "sender"]
    assert allowed.await_count == 2
    for call in dispatch.await_args_list:
        assert call.args == ("message", BODY, None)


@pytest.mark.parametrize("headers", [{}, {"x-owner": "yes", "x-ingress": "remote"},
                                      {"x-owner": "yes", "x-ingress": "preview"}])
def test_bridge_refuses_non_owner_or_nonlocal(bridge, headers):
    client, _allowed, dispatch, _senders = bridge
    assert client.post(PATH, json=BODY, headers=headers).status_code == 403
    dispatch.assert_not_awaited()


def test_bridge_refuses_api_principal_even_on_local_ingress(bridge, monkeypatch):
    client, _allowed, dispatch, _senders = bridge
    monkeypatch.setattr(tools, "get_api_user_from_request", lambda r: {"id": "api-user"})
    assert client.post(PATH, json=BODY, headers={"x-owner": "yes"}).status_code == 403
    dispatch.assert_not_awaited()


def test_bridge_preserves_host_refusal(bridge):
    client, _allowed, dispatch, _senders = bridge
    dispatch.return_value = ToolOutcome(text="request_id was already used with different arguments.", is_error=True)
    response = client.post(PATH, json=BODY, headers={"x-owner": "yes"})
    assert response.status_code == 409
    assert response.json()["detail"] == "request_id was already used with different arguments."


def test_bridge_checks_sender_access_before_dispatch(bridge):
    client, allowed, dispatch, _senders = bridge
    allowed.side_effect = HTTPException(403, "Access denied")
    assert client.post(PATH, json=BODY, headers={"x-owner": "yes"}).status_code == 403
    dispatch.assert_not_awaited()


async def test_bridge_real_queue_receipt_and_dedupe(mock_db, monkeypatch):
    from starlette.requests import Request
    from app.db import database, sessions, turns
    from app.core.turn_recorder import TurnRecorder

    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    for sid in ("sender", "receiver"):
        sessions.create_session(sid, "agent", title=sid, is_active=True, model="sonnet")
        initial = turns.enqueue_turn(sid, "user", prompt="initial", profile_id="agent")
        assert turns.settle_turn(initial["id"], "completed")
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    monkeypatch.setattr(orch, "_wake_peer", AsyncMock())  # no inference/scheduler in a unit test
    monkeypatch.setattr(sessions_helpers, "require_transcript_session", AsyncMock())
    request = Request({"type": "http", "method": "POST", "path": PATH, "headers": [],
                       "shuffle.ingress": "local"})
    body = tools.PeerMessageRequest(**BODY)
    first = await tools.send_peer_message("sender", body, request, "local-owner")
    assert first["delivery_status"] == "queued"
    row = turns.get_turn(first["turn_id"])
    assert row["session_id"] == "receiver" and row["source"] == "agent_message"
    assert "probe" in row["prompt"]
    assert turns.start_turn(row["id"])
    begun = await tools.send_peer_message("sender", body, request, "local-owner")
    assert begun["delivery_status"] == "begun" and begun["turn_id"] == row["id"]
    recorder = TurnRecorder("receiver", row["command_id"], None, None, None)
    recorder.turn_id = row["id"]
    await recorder.start(row["prompt"], profile_id="agent", source="agent_message")
    assert turns.settle_turn(row["id"], "completed")
    consumed = await tools.send_peer_message("sender", body, request, "local-owner")
    assert consumed["delivery_status"] == "consumed" and consumed["turn_id"] == row["id"]
    from app.db import session_events
    delivered = [e for e in session_events.get_events("receiver")
                 if e["event_type"] == "message.user" and e["turn_id"] == row["id"]]
    assert len(delivered) == 1 and delivered[0]["payload"]["content"] == row["prompt"]
    changed = tools.PeerMessageRequest(**{**BODY, "message": "changed"})
    with pytest.raises(HTTPException) as exc:
        await tools.send_peer_message("sender", changed, request, "local-owner")
    assert exc.value.status_code == 409 and "different arguments" in exc.value.detail


async def test_bridge_explicitly_refuses_preview_grant_on_local_listener():
    from starlette.requests import Request
    request = Request({"type": "http", "method": "POST", "path": PATH, "headers": [],
                       "shuffle.ingress": "local"})
    request.state.preview_grant = object()
    with pytest.raises(HTTPException) as exc:
        await tools.send_peer_message("sender", tools.PeerMessageRequest(**BODY), request, "local-owner")
    assert exc.value.status_code == 403
