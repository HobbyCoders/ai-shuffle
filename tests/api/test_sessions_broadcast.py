"""POST /api/v1/sessions/broadcast sends one message to many chats.

Each chat goes through the composer's own intake, one refusal never stops the
others, and the answer says what happened to each chat.
"""
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.api import chat_query, sessions_broadcast
from app.api.sessions_broadcast import BroadcastRequest, broadcast_message


def _request() -> Request:
    return Request({"type": "http", "headers": [], "state": {"auth_token": "local-owner"}})


@pytest.fixture
def sends(monkeypatch):
    calls = []
    outcomes = {}

    async def fake_require_session(request, session_id):
        if outcomes.get(session_id) == "missing":
            raise HTTPException(status_code=404, detail="Session not found")
        return {"id": session_id}

    async def fake_submit(request, session_id, data, *, compact_first):
        calls.append((session_id, data, compact_first))
        outcome = outcomes.get(session_id)
        if outcome == "refused":
            raise HTTPException(status_code=400, detail="This chat cannot take messages.")
        if outcome == "lost":
            raise HTTPException(status_code=504, detail="The worker did not answer.")
        if outcome == "crash":
            raise RuntimeError("boom")
        return {"turn_id": f"turn-{session_id}", **({"held": True} if outcome == "held" else {})}

    monkeypatch.setattr(sessions_broadcast, "require_session", fake_require_session)
    monkeypatch.setattr(chat_query, "submit_http_query", fake_submit)
    return calls, outcomes


async def test_every_chat_gets_the_message_as_a_normal_send(sends):
    calls, _ = sends
    body = BroadcastRequest(session_ids=["a", "b", "c"], prompt="Wrap up and report.")

    answer = await broadcast_message(_request(), body, token="t")

    assert [r.model_dump() for r in answer.results] == [
        {"session_id": sid, "status": "sent", "detail": None} for sid in ("a", "b", "c")
    ]
    assert answer.sent == 3
    assert sorted(c[0] for c in calls) == ["a", "b", "c"]
    for _sid, data, compact_first in calls:
        assert data["prompt"] == "Wrap up and report."
        assert compact_first is False
        # The intake's correlation handle, one per chat.
        cid = data["client_message_id"]
        assert cid.startswith("client-") and len(cid) == 39
    assert len({c[1]["client_message_id"] for c in calls}) == 3


async def test_a_refusal_does_not_stop_the_other_chats(sends):
    calls, outcomes = sends
    outcomes.update(b="refused", c="lost", d="missing", e="held", f="crash")
    body = BroadcastRequest(session_ids=["a", "b", "c", "d", "e", "f"], prompt="hi")

    answer = await broadcast_message(_request(), body, token="t")

    by_id = {r.session_id: r for r in answer.results}
    assert [r.session_id for r in answer.results] == ["a", "b", "c", "d", "e", "f"]
    assert by_id["a"].status == "sent"
    assert (by_id["b"].status, by_id["b"].detail) == ("failed", "This chat cannot take messages.")
    # Another worker may have taken it: never reported as a refusal.
    assert by_id["c"].status == "maybe_sent"
    assert (by_id["d"].status, by_id["d"].detail) == ("failed", "Chat not found")
    assert by_id["e"].status == "held"
    assert (by_id["f"].status, by_id["f"].detail) == ("failed", "Could not send message")
    assert answer.sent == 2
    # A chat the caller cannot reach is never sent to.
    assert "d" not in {c[0] for c in calls}


async def test_a_chat_listed_twice_gets_one_message(sends):
    calls, _ = sends
    body = BroadcastRequest(session_ids=["a", "a", "b"], prompt="hi")

    answer = await broadcast_message(_request(), body, token="t")

    assert [r.session_id for r in answer.results] == ["a", "b"]
    assert sorted(c[0] for c in calls) == ["a", "b"]


async def test_an_empty_message_is_refused_before_any_send(sends):
    calls, _ = sends
    with pytest.raises(HTTPException) as raised:
        await broadcast_message(_request(), BroadcastRequest(session_ids=["a"], prompt="   "), token="t")
    assert raised.value.status_code == 422
    assert calls == []


def test_the_chat_list_is_bounded():
    with pytest.raises(ValidationError):
        BroadcastRequest(session_ids=[], prompt="hi")
    with pytest.raises(ValidationError):
        BroadcastRequest(session_ids=[str(i) for i in range(sessions_broadcast.MAX_BROADCAST_CHATS + 1)], prompt="hi")


def test_the_route_is_served_under_sessions():
    from app.main import app

    assert "post" in app.openapi()["paths"]["/api/v1/sessions/broadcast"]
