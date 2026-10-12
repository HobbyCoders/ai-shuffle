"""REST transcript reads apply the account rule tool images use (#1877).

A paired controller (an account phone or a directly paired device) is
admitted to ``require_auth``; until #1877 the session routes then checked only
API keys, so another account member's phone or a direct controller read the
owner's chat. Each route that returns transcript content now answers it as
the image route does (``artifact_access.paired_client_may_read``). The owner
login is unchanged. The same answers on a worker that mirrors the chat, and
over the chat WebSocket, are in
tests/core/network/test_mirrored_chat_image_access.py.
"""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api import commands, events, network, sessions, ws_sync
from app.api.swarms import router as swarm_router
from app.api.auth import require_auth
from app.core import artifact_access
from app.core import mobile_tool_identity as identity
from app.db import database, mobile_tool_identity

MEMBER = "account-member"
OWNER = "account-owner"

#: paired client id -> its account (None: paired directly, no account)
PHONES = {"member-phone": MEMBER, "direct-phone": None, "owner-phone": OWNER}


def _principal(owner, kind="paired", principal_id="device", account_id=None):
    return dict(owner=owner, kind=kind, principal_id=principal_id, account_id=account_id,
                credential_hash="fixture")


@pytest.fixture
def client(mock_db, monkeypatch):
    database.create_session(session_id="owner-chat", profile_id="default", title="Owner desktop chat")
    mobile_tool_identity.bind("owner-chat", _principal("install-admin", kind="admin", principal_id="install"),
                              new=True)
    database.create_session(session_id="member-chat", profile_id="default", title="Member phone chat")
    mobile_tool_identity.bind("member-chat", _principal("account:" + MEMBER, account_id=MEMBER), new=True)
    database.create_session(session_id="direct-chat", profile_id="default", title="Direct controller chat")
    mobile_tool_identity.bind("direct-chat", _principal("paired:direct-phone", principal_id="direct-phone"),
                              new=True)

    def ownership(device_id=None, *, paired_client_id=None):
        account = PHONES.get(paired_client_id)
        return {"account_id": account} if account else None

    monkeypatch.setattr(artifact_access.mobile_hub, "ownership", ownership)
    monkeypatch.setattr(identity.pairing, "get_paired_client", lambda cid: {"id": cid} if cid in PHONES else None)
    # Only the owner's own account phone holds this install's owner authority.
    monkeypatch.setattr(identity.account_hub, "check_paired_client", lambda c: c["id"] == "owner-phone")
    # The search routes run real SQL in production; the shim's search SQL is
    # Postgres-only, so return every chat as a match.
    rows = lambda **kw: [dict(database.get_session(s), match_type="content", match_snippet="secret words")
                         for s in ("owner-chat", "member-chat", "direct-chat")]
    monkeypatch.setattr(sessions.database, "search_sessions", rows)
    monkeypatch.setattr(sessions.database, "search_sessions_paginated", lambda **kw: (rows(), 3))

    async def no_events(*args, **kwargs):
        return
        yield  # pragma: no cover - an empty stream

    monkeypatch.setattr(events, "event_stream", no_events)

    app = FastAPI()
    app.include_router(sessions.router)
    app.include_router(events.router)
    app.include_router(commands.router)
    app.include_router(network.router)
    app.include_router(swarm_router, prefix="/api/sessions")
    app.include_router(ws_sync.router, prefix="/api/v1")

    def authenticate(request: Request):
        who = request.headers.get("x-fixture-caller", "admin")
        request.state.api_user = None
        if who == "admin":
            request.state.is_admin = True
            request.state.paired_client = None
        else:
            owner_admin = who == "owner-phone"
            request.state.is_admin = owner_admin
            request.state.paired_client = {"id": who, "account_owner_admin": owner_admin}
        return "fixture"

    app.dependency_overrides[require_auth] = authenticate
    with TestClient(app) as value:
        yield value


def _get(client, method, path, who):
    return client.request(method, path, headers={"x-fixture-caller": who})


#: Every REST route that returns a chat's transcript content (method, path).
TRANSCRIPT_ROUTES = [
    ("GET", "/api/v1/sessions/{sid}"),
    ("GET", "/api/v1/sessions/{sid}/live"),
    ("GET", "/api/v1/sessions/{sid}/sync"),
    ("GET", "/api/v1/sessions/{sid}/events"),
    ("GET", "/api/v1/sessions/{sid}/composer-suggestion"),
    ("GET", "/api/v1/sessions/{sid}/export"),
    ("POST", "/api/v1/sessions/{sid}/fork"),
    ("GET", "/api/v1/commands/rewind/checkpoints/{sid}"),
    ("GET", "/api/v1/commands/rewind/checkpoints/{sid}/legacy"),
    ("POST", "/api/v1/network/sessions/{sid}/fork-here"),
    ("GET", "/api/sessions/{sid}/swarms"),
]

#: Who reads which chat: the image rule's answers.
READABLE = {
    "member-phone": {"member-chat"},
    "direct-phone": {"direct-chat"},
    "owner-phone": {"owner-chat"},
    "admin": {"owner-chat", "member-chat", "direct-chat"},
}


@pytest.mark.parametrize("method, route", TRANSCRIPT_ROUTES)
def test_transcript_routes_follow_the_image_rule(client, method, route):
    for who, readable in READABLE.items():
        for sid in ("owner-chat", "member-chat", "direct-chat"):
            response = _get(client, method, route.format(sid=sid), who)
            if sid in readable:
                # Allowed: whatever the route answers next (a fork of a chat
                # with no SDK transcript is a 400), never the access refusal.
                assert response.status_code != 403, (who, sid, response.text)
            else:
                assert response.status_code == 403, (who, sid, response.status_code)
                assert response.json()["detail"] == "Access denied to this session"
                assert "secret" not in response.text


def test_the_images_give_the_same_answers(client):
    """The REST answers above are the image route's (``paired_client_may_read``)."""
    for who, readable in READABLE.items():
        if who == "admin":
            continue
        request = SimpleNamespace(state=SimpleNamespace(api_user=None, is_admin=who == "owner-phone",
                                                        paired_client={"id": who}))
        for sid in ("owner-chat", "member-chat", "direct-chat"):
            try:
                artifact_access.authorize_session_artifact(request, {"created_by_session": sid}, "hash")
                allowed = True
            except Exception:
                allowed = False
            assert allowed == (sid in readable), (who, sid)


@pytest.mark.parametrize("path", ["/api/v1/sessions/search/query?q=secret",
                                  "/api/v1/sessions/search/paginated?q=secret"])
def test_search_returns_only_chats_the_caller_may_read(client, path):
    for who, readable in READABLE.items():
        body = _get(client, "GET", path, who).json()
        items = body["items"] if isinstance(body, dict) else body
        assert {item["id"] for item in items} == readable, who
        if isinstance(body, dict):
            assert body["total"] == len(readable)


def test_state_route_carries_no_transcript_and_stays_open(client):
    """Left open on purpose: metadata only, like the session list."""
    response = _get(client, "GET", "/api/v1/sessions/owner-chat/state", "member-phone")
    assert response.status_code == 200
    assert set(response.json()) == {
        "session_id", "title", "status", "last_user_message_at", "last_user_message_seq", "last_user_message_epoch",
        "is_streaming", "connected_devices",
        "last_assistant_question_seq", "last_assistant_question_epoch",
    }
    assert response.json()["last_user_message_at"] is None


def test_session_sync_socket_follows_the_image_rule(client, monkeypatch):
    """``/ws/sessions/{id}`` sends every message in its first frame (#1877)."""
    from starlette.websockets import WebSocketDisconnect

    database.add_session_message(session_id="owner-chat", role="user", content="secret words")
    database.add_session_message(session_id="member-chat", role="user", content="secret words")
    database.add_session_message(session_id="direct-chat", role="user", content="secret words")

    async def authenticate(websocket, token):
        websocket.state.mobile_tool_principal = (
            None if token == "admin" else dict(kind="paired", principal_id=token, credential_hash=token))
        return True, None

    monkeypatch.setattr(ws_sync, "authenticate_websocket", authenticate)
    monkeypatch.setattr(database, "get_paired_client_by_session_hash", lambda digest: {
        "id": digest, "account_owner_admin": digest == "owner-phone"} if digest in PHONES else None)
    for who, readable in READABLE.items():
        for sid in ("owner-chat", "member-chat", "direct-chat"):
            url = f"/api/v1/ws/sessions/{sid}?device_id=d&token={who}"
            with client.websocket_connect(url) as socket:
                if sid in readable:
                    frame = socket.receive_json()
                    assert frame["event_type"] == "state", (who, sid)
                    assert [m["content"] for m in frame["data"]["messages"]] == ["secret words"]
                    assert [m["seq"] for m in frame["data"]["messages"]] == [None]
                else:
                    with pytest.raises(WebSocketDisconnect) as closed:
                        socket.receive_json()
                    assert closed.value.code == 4003, (who, sid)


def test_a_chat_created_over_rest_is_readable_by_its_creator(client):
    """The review probe for #1877: a phone's ``POST /sessions`` then its own transcript.

    The creator is recorded as the chat socket records the chats it creates;
    the owner login's chats stay without a record (the owner group reads
    them, other members do not), as before.
    """
    for creator in ("member-phone", "direct-phone", "owner-phone", "admin"):
        created = client.post("/api/v1/sessions", json={}, headers={"x-fixture-caller": creator})
        assert created.status_code == 200, created.text
        sid = created.json()["session_id"]
        record = mobile_tool_identity.read(sid)
        assert (record or {}).get("principal_id") == (None if creator == "admin" else creator)
        readers = {creator, "admin"} | ({"owner-phone"} if creator == "admin" else set())
        for who in READABLE:
            status = _get(client, "GET", f"/api/v1/sessions/{sid}", who).status_code
            assert status == (200 if who in readers else 403), (creator, who, status)
