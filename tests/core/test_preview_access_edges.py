"""Fail-closed edges of the agent-browser preview token (``app.core.preview_access``).

The main suite covers minting, the cookie redirect and the mutation ceiling.
These pin the refusals around it: a token whose session cannot be looked up,
a correctly signed token with a corrupt body, oversize and wrong-version
tokens, out-of-range TTLs, requests with no usable peer address, and
post-cookie redirects that would leave the app.
"""
from __future__ import annotations

import time

import pytest
from starlette.requests import Request

from app.core import preview_access
from app.core.config import settings

SESSION = "sess-edge-1"


@pytest.fixture(autouse=True)
def _isolated_key(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: False)


def _request(*, peer="10.0.0.200", cookies=None, method="GET", path="/", client=True):
    headers = [(b"host", b"10.0.0.135:8000")]
    if cookies:
        headers.append((b"cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()).encode()))
    scope = {"type": "http", "method": method, "scheme": "http", "path": path, "query_string": b"",
             "server": ("0.0.0.0", 8000), "headers": headers, "shuffle.ingress": "local"}
    if client:
        scope["client"] = (peer, 50000)
    return Request(scope)


# ---------------------------------------------------------------------------
# The bound session
# ---------------------------------------------------------------------------

def test_session_lookup_error_fails_closed(monkeypatch):
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    from app.db import sessions

    def boom(session_id):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(sessions, "get_session", boom)
    token = preview_access.mint(SESSION)
    assert preview_access.verify(token) is None


def test_deleted_session_invalidates_its_token(monkeypatch):
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    from app.db import sessions
    live = {SESSION}
    monkeypatch.setattr(sessions, "get_session", lambda sid: {"id": sid} if sid in live else None)
    token = preview_access.mint(SESSION)
    assert preview_access.verify(token).session_id == SESSION
    live.clear()
    assert preview_access.verify(token) is None


# ---------------------------------------------------------------------------
# Token shape
# ---------------------------------------------------------------------------

def _signed(payload: str) -> str:
    return f"{payload}.{preview_access._sign(payload)}"


def test_correctly_signed_but_undecodable_body_is_refused():
    """Even with a valid MAC, a body that does not decode grants nothing."""
    future = int(time.time()) + 3600
    assert preview_access.verify(_signed(f"v1.%%%.{future}")) is None
    assert preview_access.verify(_signed(f"v1.{preview_access._b64(SESSION)}.soon")) is None
    assert preview_access.verify(_signed(f"v1..{future}")) is None           # empty session id
    assert preview_access.verify(_signed(f"v1.{preview_access._b64(SESSION)}.{future}")) is not None


@pytest.mark.parametrize("token", [
    None, "", 123, "a" * 513, "v1.only.three", "v2.x.1.y", "v1.x.1.y.z",
])
def test_wrong_shape_tokens_are_refused(token):
    assert preview_access.verify(token) is None


def test_non_ascii_signature_is_refused():
    token = preview_access.mint(SESSION)
    payload = token.rsplit(".", 1)[0]
    assert preview_access.verify(payload + ".é" * 64) is None


def test_token_expiring_exactly_now_is_refused():
    now = 1_700_000_000.0
    token = preview_access.mint(SESSION, 60, now=now)
    assert preview_access.verify(token, now=now + 59) is not None
    assert preview_access.verify(token, now=now + 60) is None


@pytest.mark.parametrize("session_id,ttl", [
    ("", 60), (None, 60), (SESSION, 0), (SESSION, -5), (SESSION, 7 * 24 * 3600 + 1),
])
def test_mint_refuses_missing_session_and_out_of_range_ttl(session_id, ttl):
    with pytest.raises(ValueError):
        preview_access.mint(session_id, ttl)


# ---------------------------------------------------------------------------
# grant(): the transport half never raises and never grants on doubt
# ---------------------------------------------------------------------------

def test_grant_without_client_address_is_refused():
    token = preview_access.mint(SESSION)
    assert preview_access.grant(_request(cookies={preview_access.COOKIE_NAME: token}, client=False)) is None
    assert preview_access.grant(_request(cookies={preview_access.COOKIE_NAME: token}, peer="bogus")) is None


def test_grant_swallows_verification_errors(monkeypatch):
    def boom(token):
        raise RuntimeError("key file unreadable")

    monkeypatch.setattr(preview_access, "verify", boom)
    request = _request(cookies={preview_access.COOKIE_NAME: "x"})
    assert preview_access.grant(request) is None


def test_grant_refuses_protected_mutation_but_allows_chat_mutation():
    token = preview_access.mint(SESSION)
    cookies = {preview_access.COOKIE_NAME: token}
    assert preview_access.grant(_request(cookies=cookies, method="POST", path="/api/v1/settings/x")) is None
    assert preview_access.grant(_request(cookies=cookies, method="POST", path="/api/v1/sessions/x")) is not None


def test_enforce_limits_ignores_invalid_cookie_for_ordinary_clients():
    request = _request(cookies={preview_access.COOKIE_NAME: "forged"}, method="POST", path="/api/v1/settings/x")
    preview_access.enforce_limits(request)  # no raise: an invalid cookie restricts nothing
    assert request.state.preview_grant is None


# ---------------------------------------------------------------------------
# safe_next: the post-cookie redirect stays inside the app
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    None, "", "https://evil.example/", "//evil.example/", "/\\evil.example", "/ok\\..\\x",
    "relative/path", "http:/evil.example",
])
def test_safe_next_refuses_off_app_targets(value):
    assert preview_access.safe_next(value) == "/"


def test_safe_next_keeps_in_app_paths():
    assert preview_access.safe_next("/chat/abc?x=1#y") == "/chat/abc?x=1#y"


def test_preview_url_brackets_ipv6_and_sanitizes_next():
    url = preview_access.preview_url(SESSION, "fd00::1", 8000, next_path="https://evil.example/", token="tok")
    assert url.startswith("http://[fd00::1]:8000/api/v1/auth/preview-access?token=tok&next=/")
    assert "evil" not in url
