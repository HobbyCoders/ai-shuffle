"""Every door into ``app.api.ws_auth.authenticate_websocket``.

The socket authenticator grants owner-level ``admin`` principals from three
passwordless sources (the local listener, the agent-browser preview cookie,
the owner session) and scoped principals from paired tokens and API keys.
Each passwordless door must still insist on a same-origin browser, and a
failed dependency check on a paired token must surface as "retry later"
(1013) rather than silently authenticate or look like a revoked credential.
"""
import hashlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, WebSocketException

from app.api import ws_auth
from app.core import owner_auth, preview_access
from app.core.pairing import PAIRED_SESSION_PREFIX


def _socket(*, origin="http://localhost", cookies=None, scheme="ws"):
    headers = {"host": "localhost"}
    if origin is not None:
        headers["origin"] = origin
    return SimpleNamespace(
        cookies=dict(cookies or {}),
        headers=headers,
        scope={"type": "websocket", "shuffle.ingress": "local"},
        client=SimpleNamespace(host="127.0.0.1"),
        url=SimpleNamespace(scheme=scheme),
        state=SimpleNamespace(),
    )


@pytest.fixture
def no_local(monkeypatch):
    monkeypatch.setattr(owner_auth, "local_access", lambda request: False)
    monkeypatch.setattr(owner_auth, "owner_session", lambda request, token: False)
    monkeypatch.setattr(ws_auth.database, "get_api_user_by_key_hash", lambda key_hash: None)


# ---------------------------------------------------------------------------
# Local listener
# ---------------------------------------------------------------------------

async def test_local_listener_same_origin_is_admin(monkeypatch, no_local):
    monkeypatch.setattr(owner_auth, "local_access", lambda request: True)
    socket = _socket()
    assert await ws_auth.authenticate_websocket(socket, None) == (True, None)
    principal = socket.state.mobile_tool_principal
    assert principal["kind"] == "admin" and principal["owner"] == "install-admin"


@pytest.mark.parametrize("origin", ["http://evil.example", "http://localhost:5173", "http://user@localhost", None])
async def test_local_listener_refuses_cross_or_missing_origin(monkeypatch, no_local, origin):
    """A web page on another origin reaching the loopback listener gets nothing."""
    monkeypatch.setattr(owner_auth, "local_access", lambda request: True)
    socket = _socket(origin=origin)
    assert await ws_auth.authenticate_websocket(socket, None) == (False, None)
    assert getattr(socket.state, "mobile_tool_principal", None) is None


async def test_local_listener_is_not_used_when_a_token_is_offered(monkeypatch, no_local):
    """A bad explicit credential never falls back to passwordless local access."""
    monkeypatch.setattr(owner_auth, "local_access", lambda request: True)
    assert await ws_auth.authenticate_websocket(_socket(), "bogus-token") == (False, None)


# ---------------------------------------------------------------------------
# Agent-browser preview cookie
# ---------------------------------------------------------------------------

async def test_preview_cookie_same_origin_is_admin(monkeypatch, no_local):
    monkeypatch.setattr(preview_access, "grant", lambda request: preview_access.Grant("s1", 9999999999))
    socket = _socket(cookies={preview_access.COOKIE_NAME: "tok"})
    assert await ws_auth.authenticate_websocket(socket, None) == (True, None)
    assert socket.state.mobile_tool_principal["kind"] == "admin"


async def test_preview_cookie_cross_origin_is_refused(monkeypatch, no_local):
    monkeypatch.setattr(preview_access, "grant", lambda request: preview_access.Grant("s1", 9999999999))
    socket = _socket(origin="http://evil.example", cookies={preview_access.COOKIE_NAME: "tok"})
    assert await ws_auth.authenticate_websocket(socket, None) == (False, None)
    assert getattr(socket.state, "mobile_tool_principal", None) is None


async def test_invalid_preview_cookie_grants_nothing(monkeypatch, no_local):
    monkeypatch.setattr(preview_access, "grant", lambda request: None)
    socket = _socket(cookies={preview_access.COOKIE_NAME: "forged"})
    assert await ws_auth.authenticate_websocket(socket, None) == (False, None)
    assert socket.state.mobile_tool_principal is None


# ---------------------------------------------------------------------------
# Owner session cookie and API keys
# ---------------------------------------------------------------------------

async def test_ambient_session_cookie_needs_same_origin(monkeypatch, no_local):
    monkeypatch.setattr(owner_auth, "owner_session", lambda request, token: True)
    good = _socket(cookies={"session": "shr_owner"})
    assert await ws_auth.authenticate_websocket(good, None) == (True, None)
    bad = _socket(origin="http://evil.example", cookies={"session": "shr_owner"})
    assert await ws_auth.authenticate_websocket(bad, None) == (False, None)


async def test_invalid_query_token_does_not_exempt_cookie_from_origin(monkeypatch, no_local):
    monkeypatch.setattr(owner_auth, "owner_session", lambda request, token: token == "shr_owner")
    socket = _socket(origin="http://evil.example", cookies={"session": "shr_owner"})
    assert await ws_auth.authenticate_websocket(socket, "not-valid") == (False, None)


async def test_api_key_user_must_be_active(monkeypatch, no_local):
    users = {hashlib.sha256(b"aih_live").hexdigest(): {"id": "u1", "is_active": True},
             hashlib.sha256(b"aih_off").hexdigest(): {"id": "u2", "is_active": False}}
    monkeypatch.setattr(ws_auth.database, "get_api_user_by_key_hash", users.get)
    live = _socket()
    ok, user = await ws_auth.authenticate_websocket(live, "aih_live")
    assert ok is True and user["id"] == "u1"
    assert live.state.mobile_tool_principal["kind"] == "api_user"
    assert await ws_auth.authenticate_websocket(_socket(), "aih_off") == (False, None)


# ---------------------------------------------------------------------------
# Paired controller tokens: refusal vs. "retry later"
# ---------------------------------------------------------------------------

TOKEN = PAIRED_SESSION_PREFIX + "abc"


def _paired_lookup(monkeypatch, effect):
    import app.api.auth as auth_mod

    def lookup(token):
        if isinstance(effect, BaseException):
            raise effect
        return effect

    monkeypatch.setattr(auth_mod, "get_paired_client_from_token", lookup)


@pytest.mark.parametrize("effect", [PermissionError("revoked"), HTTPException(401, "expired"), None])
async def test_paired_refusal_is_an_auth_failure(monkeypatch, no_local, effect):
    _paired_lookup(monkeypatch, effect)
    socket = _socket()
    assert await ws_auth.authenticate_websocket(socket, TOKEN) == (False, None)
    assert socket.state.mobile_tool_principal is None


@pytest.mark.parametrize("effect", [HTTPException(503, "hub down"), RuntimeError("db gone")])
async def test_paired_dependency_failure_is_retry_later(monkeypatch, no_local, effect):
    _paired_lookup(monkeypatch, effect)
    with pytest.raises(WebSocketException) as info:
        await ws_auth.authenticate_websocket(_socket(), TOKEN)
    assert info.value.code == 1013


async def test_paired_principal_refusal_after_lookup_is_an_auth_failure(monkeypatch, no_local):
    """The live principal check (revoked controller scope) refuses, not retries."""
    _paired_lookup(monkeypatch, {"id": "pc1"})

    def refuse(kind, identity, token):
        raise PermissionError("Controller expired or revoked")

    monkeypatch.setattr(ws_auth.identity, "principal", refuse)
    assert await ws_auth.authenticate_websocket(_socket(), TOKEN) == (False, None)
