"""A valid preview cookie limits a connection whatever else it presents.

``preview_access.enforce_limits`` holds every HTTP credential to the cookie's
mutation ceiling. A chat send has a second limit: ``session_modes`` lets only
the ``authenticated`` caller lift the no-project ceiling or send to another
worker. These tests ask the same question of every credential, over a real
chat socket and a real REST request through the real listener and dependencies:
with the cookie present, who does the send come from?

Credentials keep their own identity and scope. A paired device stays that
device, an API key stays its user, and a credential that fails is never
replaced by the cookie. Nothing here restricts a connection without a live
cookie, or a cookie this listener does not honour.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Depends, FastAPI, Request, WebSocketDisconnect
from fastapi.testclient import TestClient

from app.api import auth, websocket as chat_socket
from app.api.chat_query import _request_query_principal
from app.core import mobile_tool_identity as identity
from app.core import owner_auth, preview_access
from app.core.config import settings
from app.core.session_modes import CALLER_AUTHENTICATED, CALLER_PREVIEW, caller_of
from app.db import database, sessions
from app.db.base import get_db
from app.serve import IngressApplication

SESSION = "sess-preview-every-credential"
LOCAL, PREVIEW, REMOTE = 8000, 8002, 8001
HOST = "10.0.0.30"
PEER = ("10.0.0.20", 54321)
OWNER = "owner-session-token"
PAIRED = "shc_paired-device-token"
PAIRED_ID = "paired-device"
API_KEY = "aih_live-api-key"
API_USER = "api-user"
CHAT = "/api/v1/ws/chat?device_id=device"
WHO = "/test/who"

#: name: (query token / Bearer, session cookie). ``local-listener`` presents no
#: credential: the local listener itself is what admits it.
CREDENTIALS = {
    "local-listener": (None, None),
    "owner-token": (OWNER, None),
    "owner-cookie": (None, OWNER),
    "paired-token": (PAIRED, None),
    "paired-cookie": (None, PAIRED),
    "api-key": (API_KEY, None),
}


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest.fixture
def world(mock_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "cookie_secure", False)
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: True)
    monkeypatch.setattr(owner_auth, "owner_session", lambda request, token: token == OWNER)
    sessions.create_session(SESSION, "default")
    now = datetime.now(timezone.utc)
    with get_db() as conn:
        conn.cursor().execute(
            """
            INSERT INTO paired_clients
                (id, device_id, device_name, platform, client_type, scopes,
                 session_token_hash, session_expires_at, paired_at, last_seen,
                 revoked_at, created_at, updated_at)
            VALUES (%s, %s, %s, 'web', 'browser', '["controller"]', %s, %s, %s, %s, NULL, %s, %s)
            """,
            (PAIRED_ID, "dev-" + PAIRED_ID, PAIRED_ID, _hash(PAIRED),
             (now + timedelta(days=1)).isoformat(), now.isoformat(), now.isoformat(),
             now.isoformat(), now.isoformat()),
        )
    database.create_api_user(API_USER, "key user", _hash(API_KEY))
    assert database.get_api_user_by_key_hash(_hash(API_KEY))["id"] == API_USER


@pytest.fixture
def opened(monkeypatch):
    """The principal of each chat socket that got past authentication."""
    principals = []

    class Handler:
        def __init__(self, websocket, *args, **kwargs):
            self.websocket = websocket

        async def run(self):
            principals.append(self.websocket.state.mobile_tool_principal)
            await self.websocket.close(code=1000)

    monkeypatch.setattr(chat_socket, "ChatWebSocketHandler", Handler)
    return principals


@pytest.fixture
def api(world):
    app = FastAPI()
    app.include_router(chat_socket.router)

    @app.post(WHO)
    def who(request: Request, _: str = Depends(auth.require_auth)):
        # The real REST send's identity, and the record a chat it creates gets.
        value = _request_query_principal(request)
        created = "sess-" + hashlib.sha256(repr(value).encode()).hexdigest()[:12]
        sessions.create_session(created, "default")
        identity.record_creator(created, request)
        return {
            "caller": caller_of(value),
            "kind": value["kind"],
            "principal_id": value["principal_id"],
            "recorded": (database_record(created) or {}).get("principal_id"),
        }

    return app


def database_record(session_id):
    from app.db import mobile_tool_identity as store

    return store.read(session_id)


def _client(app, port=LOCAL, *, preview="valid", session=None, bearer=None, peer=PEER):
    origin = f"http://{HOST}:{port}"
    headers = {"Origin": origin}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    client = TestClient(
        IngressApplication(app, LOCAL, REMOTE, PREVIEW),
        base_url=origin,
        client=peer,
        headers=headers,
    )
    if preview == "valid":
        client.cookies.set(preview_access.cookie_name(), preview_access.mint(SESSION))
    elif preview == "expired":
        client.cookies.set(
            preview_access.cookie_name(),
            preview_access.mint(SESSION, ttl_seconds=60, now=0),
        )
    if session:
        client.cookies.set("session", session)
    return client


def _socket_close(client, token=None):
    url = f"ws://{client.base_url.netloc.decode()}{CHAT}"
    if token:
        url += f"&token={token}"
    with pytest.raises(WebSocketDisconnect) as closed:
        with client.websocket_connect(url) as socket:
            socket.receive_text()
    return closed.value.code


def _rest(app, *, token=None, session=None, preview="valid", port=LOCAL):
    return _client(app, port, preview=preview, session=session, bearer=token).post(WHO)


# ---------------------------------------------------------------------------
# With the cookie, every credential's send is the preview's
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("token, session", CREDENTIALS.values(), ids=CREDENTIALS.keys())
def test_socket_send_is_the_previews_whatever_else_it_presents(api, opened, token, session):
    client = _client(api, session=session)
    assert _socket_close(client, token) == 1000
    assert [caller_of(value) for value in opened] == [CALLER_PREVIEW]


@pytest.mark.parametrize("token, session", CREDENTIALS.values(), ids=CREDENTIALS.keys())
def test_rest_send_is_the_previews_whatever_else_it_presents(api, token, session):
    response = _rest(api, token=token, session=session)
    assert response.status_code == 200, response.text
    assert response.json()["caller"] == CALLER_PREVIEW


# ---------------------------------------------------------------------------
# Without a live cookie nothing changes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("preview", [None, "expired"], ids=["no-cookie", "expired-cookie"])
@pytest.mark.parametrize("token, session", CREDENTIALS.values(), ids=CREDENTIALS.keys())
def test_socket_send_stays_authenticated_without_a_live_cookie(api, opened, token, session, preview):
    client = _client(api, session=session, preview=preview)
    assert _socket_close(client, token) == 1000
    assert [caller_of(value) for value in opened] == [CALLER_AUTHENTICATED]


@pytest.mark.parametrize("preview", [None, "expired"], ids=["no-cookie", "expired-cookie"])
@pytest.mark.parametrize("token, session", CREDENTIALS.values(), ids=CREDENTIALS.keys())
def test_rest_send_stays_authenticated_without_a_live_cookie(api, token, session, preview):
    response = _rest(api, token=token, session=session, preview=preview)
    assert response.status_code == 200, response.text
    assert response.json()["caller"] == CALLER_AUTHENTICATED
    assert response.json()["recorded"] != identity.PREVIEW_PRINCIPAL_ID


def test_another_installs_cookie_does_not_limit_the_owner(api, opened):
    client = _client(api, preview=None, session=OWNER)
    client.cookies.set(preview_access.COOKIE_PREFIX + "0" * 24, preview_access.mint(SESSION))
    assert _socket_close(client) == 1000
    assert [caller_of(value) for value in opened] == [CALLER_AUTHENTICATED]


def test_cookie_for_a_deleted_session_does_not_limit_the_owner(api, opened):
    client = _client(api, session=OWNER)
    sessions.delete_session(SESSION)
    assert _socket_close(client) == 1000
    assert [caller_of(value) for value in opened] == [CALLER_AUTHENTICATED]


@pytest.mark.parametrize("token, session", [(OWNER, None), (None, OWNER)], ids=["token", "cookie"])
def test_remote_listener_refuses_the_cookie_but_not_the_owner(api, opened, token, session):
    """The cookie is only honoured on the local network; a tunnel neither grants
    with it nor treats the owner signed in there as a preview."""
    client = _client(api, REMOTE, session=session)
    assert _socket_close(client, token) == 1000
    assert [caller_of(value) for value in opened] == [CALLER_AUTHENTICATED]


def test_remote_listener_cookie_alone_authenticates_nothing(api, opened):
    assert _socket_close(_client(api, REMOTE)) == 4001
    assert opened == []


# ---------------------------------------------------------------------------
# The other credential keeps what it is
# ---------------------------------------------------------------------------

def test_paired_device_stays_that_device_on_both_transports(api, opened):
    assert _socket_close(_client(api), PAIRED) == 1000
    socket_value = opened[0]
    assert (socket_value["kind"], socket_value["principal_id"]) == ("paired", PAIRED_ID)
    rest = _rest(api, token=PAIRED).json()
    assert (rest["kind"], rest["principal_id"]) == ("paired", PAIRED_ID)
    # The record of a chat it creates names the device, not the install owner.
    assert rest["recorded"] == PAIRED_ID


def test_paired_browser_stays_that_device_on_both_transports(api, opened):
    """Browser pairing carries the device's credential in the session cookie."""
    assert _socket_close(_client(api, session=PAIRED)) == 1000
    assert (opened[0]["kind"], opened[0]["principal_id"]) == ("paired", PAIRED_ID)
    rest = _rest(api, session=PAIRED).json()
    assert (rest["kind"], rest["principal_id"]) == ("paired", PAIRED_ID)


@pytest.mark.parametrize("token, session", [(PAIRED, None), (None, PAIRED)], ids=["token", "cookie"])
def test_paired_device_can_continue_its_own_chat_under_the_cookie(api, opened, token, session):
    """Its recorded chat still recognises it, and a revoked device is noticed."""
    assert _socket_close(_client(api, session=session), token) == 1000
    value = opened[0]
    sessions.create_session("sess-own", "default")
    identity.bind_session("sess-own", value, new=True)
    identity.bind_session("sess-own", value)
    identity.revalidate(value)
    with get_db() as conn:
        conn.cursor().execute("UPDATE paired_clients SET revoked_at = %s WHERE id = %s",
                              (datetime.now(timezone.utc).isoformat(), PAIRED_ID))
    with pytest.raises(PermissionError):
        identity.revalidate(value)


def test_api_key_in_the_session_cookie_stays_its_user_on_the_socket(api, opened):
    assert _socket_close(_client(api, session=API_KEY)) == 1000
    assert [caller_of(value) for value in opened] == [CALLER_PREVIEW]
    assert (opened[0]["kind"], opened[0]["principal_id"]) == ("api_user", API_USER)


def test_obsolete_session_cookie_does_not_stop_the_cookie(api, opened):
    """Another install's cookie on this host, as in an isolated preview."""
    for stale in ("another-install-session", "shc_another-install-device"):
        assert _socket_close(_client(api, session=stale)) == 1000
    assert [caller_of(value) for value in opened] == [CALLER_PREVIEW, CALLER_PREVIEW]
    assert all(value["kind"] == "admin" for value in opened)


def test_api_key_stays_its_user_on_both_transports(api, opened):
    assert _socket_close(_client(api), API_KEY) == 1000
    assert (opened[0]["kind"], opened[0]["principal_id"]) == ("api_user", API_USER)
    rest = _rest(api, token=API_KEY).json()
    assert (rest["kind"], rest["principal_id"]) == ("api_user", API_USER)
    # An API key's chat never gets a record, least of all the install owner's.
    assert rest["recorded"] is None


def test_limit_marker_is_not_stored(api, opened):
    assert _socket_close(_client(api), PAIRED) == 1000
    sessions.create_session("sess-marker", "default")
    identity.bind_session("sess-marker", opened[0], new=True)
    stored = database_record("sess-marker")
    assert (stored["kind"], stored["principal_id"]) == ("paired", PAIRED_ID)
    assert identity.PREVIEW_LIMITED_KEY not in stored


@pytest.mark.parametrize("token, session", [(OWNER, None), (None, OWNER), (None, None)],
                         ids=["owner-token", "owner-cookie", "local-listener"])
def test_chat_created_with_the_cookie_is_recorded_as_the_preview(api, token, session):
    """A later turn nobody stamped must not read the chat as the owner's."""
    assert _rest(api, token=token, session=session).json()["recorded"] == identity.PREVIEW_PRINCIPAL_ID


@pytest.mark.parametrize("token, session", [(OWNER, None), (None, OWNER)], ids=["token", "cookie"])
def test_owner_login_with_the_cookie_still_answers_to_logout(api, opened, token, session):
    """It stays tied to the login it presented, not to the local listener."""
    assert _socket_close(_client(api, session=session), token) == 1000
    assert opened[0]["credential_hash"] == identity.digest(OWNER)


# ---------------------------------------------------------------------------
# The cookie never stands in for a failed credential, or a missing Origin
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["not-a-credential", "shc_retired"])
def test_failed_explicit_credential_is_not_replaced_by_the_cookie(api, opened, bad):
    assert _socket_close(_client(api), bad) == 4001
    assert opened == []
    assert _rest(api, token=bad).status_code == 401


def test_failed_explicit_credential_does_not_fall_back_to_the_local_listener(api, opened):
    assert _socket_close(_client(api, preview=None), "not-a-credential") == 4001
    assert opened == []


@pytest.mark.parametrize("origin", ["http://evil.example", None], ids=["cross-site", "no-origin"])
@pytest.mark.parametrize("session", [None, OWNER], ids=["local-listener", "owner-cookie"])
def test_cookie_socket_still_needs_a_same_origin_page(api, opened, session, origin):
    client = _client(api, session=session)
    if origin is None:
        del client.headers["Origin"]
    else:
        client.headers["Origin"] = origin
    assert _socket_close(client) == 4001
    assert opened == []
