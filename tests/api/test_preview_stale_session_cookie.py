"""A stale owner-session cookie must not shadow a valid agent-browser preview grant (#1542).

Cookies are scoped to the host, not the port, so an isolated branch preview
opened in the agent browser routinely receives the ``session`` cookie of a
different installation on the same host. ``/api/v1/auth/status`` (and the
WebSocket authenticator) honoured the preview grant regardless, while
``require_auth`` refused the request outright at the obsolete cookie: the UI
read as signed in yet every protected read and the SSE feed returned 401.

The contract pinned here: status and protected reads agree, the legitimate
grant is honoured with its mutation ceiling and same-origin rule intact, an
explicit (Bearer) credential that fails is never replaced by the grant, and
an obsolete cookie still never falls back to passwordless local access.
"""

from __future__ import annotations

import time

import pytest
from fastapi import Depends, FastAPI, Request, WebSocket
from fastapi.testclient import TestClient

from app.api import auth, ws_auth
from app.core import owner_auth, preview_access
from app.core.config import settings

SESSION = "sess-preview-1542"
HOST = "10.0.0.135:18090"
ORIGIN = f"http://{HOST}"
VITE = {"X-Forwarded-Host": "localhost:5173"}
# Obsolete ``session`` cookies another installation on the same host could
# have left behind: an owner session, a retired paired session and a legacy
# opaque session token. None of them is valid on this installation.
STALE_COOKIES = [
    "shr_obsolete-owner-session",
    "shc_retired-paired-session",
    "legacy-session-token",
]


class Surface:
    """Test-only trusted listener boundary. HTTP headers cannot set this mark."""

    def __init__(self, app, ingress):
        self.app, self.ingress = app, ingress

    async def __call__(self, scope, receive, send):
        if self.ingress is not None:
            scope["shuffle.ingress"] = self.ingress
        await self.app(scope, receive, send)


@pytest.fixture
def app(mock_db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "cookie_secure", False)
    # No managed DB here: the preview token's signature alone decides.
    monkeypatch.setattr(
        type(settings), "managed_database_available", lambda self: False
    )
    monkeypatch.setattr(owner_auth.settings, "shuffle_local_access", True)
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: False)
    monkeypatch.setattr(auth, "is_github_authenticated_cached", lambda *args: False)
    monkeypatch.setattr(auth.auth_service, "is_claude_authenticated", lambda: False)

    api = FastAPI()
    api.include_router(auth.router)

    @api.get("/api/v1/sessions")
    def sessions(request: Request, principal: str = Depends(auth.require_auth)):
        return {"principal": principal, "admin": request.state.is_admin}

    @api.post("/api/v1/sessions/abc/messages")
    def post_message(principal: str = Depends(auth.require_auth)):
        return {"posted": principal}

    @api.post("/api/v1/settings/test-only")
    def change_settings(_: str = Depends(auth.require_auth)):
        return {"changed": True}

    @api.websocket("/ws")
    async def socket(websocket: WebSocket):
        valid, _ = await ws_auth.authenticate_websocket(
            websocket, websocket.query_params.get("token")
        )
        await websocket.accept()
        await websocket.send_json({"authenticated": valid})
        await websocket.close()

    return api


def _client(
    app, *, ingress="local", proxied=True, session=None, preview="valid", bearer=None
):
    headers = dict(VITE) if proxied else {}
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    client = TestClient(
        Surface(app, ingress),
        base_url=ORIGIN,
        client=("10.0.0.200", 54321),
        headers=headers,
    )
    if session:
        client.cookies.set("session", session)
    if preview == "valid":
        client.cookies.set(preview_access.COOKIE_NAME, preview_access.mint(SESSION))
    elif preview == "expired":
        client.cookies.set(
            preview_access.COOKIE_NAME,
            preview_access.mint(SESSION, ttl_seconds=60, now=time.time() - 120),
        )
    elif preview == "forged":
        token = preview_access.mint(SESSION)
        # Change the signature's last hex digit (always to a different one).
        tampered = token[:-1] + ("1" if token[-1] == "0" else "0")
        client.cookies.set(preview_access.COOKIE_NAME, tampered)
    return client


def _status(client):
    response = client.get("/api/v1/auth/status")
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("stale", STALE_COOKIES)
@pytest.mark.parametrize("proxied", [True, False])
def test_stale_session_cookie_does_not_shadow_valid_preview_grant(app, stale, proxied):
    client = _client(app, session=stale, proxied=proxied)
    status = _status(client)
    assert status["authenticated"] is True
    assert status["preview_access"] is True

    # The protected reads behind "Loading conversation" now agree with status.
    listed = client.get("/api/v1/sessions")
    assert listed.status_code == 200, listed.text
    assert listed.json() == {"principal": "preview:" + SESSION, "admin": True}
    # Driving the bound chat session stays allowed from the same origin.
    posted = client.post("/api/v1/sessions/abc/messages", headers={"Origin": ORIGIN})
    assert posted.status_code == 200
    assert posted.json() == {"posted": "preview:" + SESSION}
    # The live-update socket agrees as well.
    # (The test client does not derive the WebSocket Host from base_url.)
    with client.websocket_connect(
        "/ws", headers={"Host": HOST, "Origin": ORIGIN}
    ) as socket:
        assert socket.receive_json() == {"authenticated": True}
    # The obsolete cookie is left alone: on a shared host it may be another
    # installation's live login, and the preview grant is not a login.
    assert client.cookies.get("session") == stale


@pytest.mark.parametrize("stale", STALE_COOKIES)
def test_stale_cookie_keeps_preview_mutation_ceiling_and_same_origin(app, stale):
    client = _client(app, session=stale)
    denied = client.post("/api/v1/settings/test-only", headers={"Origin": ORIGIN})
    assert denied.status_code == 403
    assert "Agent previews" in denied.json()["detail"]
    cross_site = client.post(
        "/api/v1/sessions/abc/messages", headers={"Origin": "http://evil.example"}
    )
    assert cross_site.status_code == 403
    missing_origin = client.post("/api/v1/sessions/abc/messages")
    assert missing_origin.status_code == 403


@pytest.mark.parametrize("session", [None, STALE_COOKIES[0]])
def test_failed_explicit_bearer_is_never_replaced_by_the_preview_grant(app, session):
    client = _client(app, session=session, bearer="aih_not-a-real-key")
    assert client.get("/api/v1/sessions").status_code == 401
    assert (
        client.post(
            "/api/v1/sessions/abc/messages", headers={"Origin": ORIGIN}
        ).status_code
        == 401
    )


def test_stale_cookie_never_falls_back_to_passwordless_local_access(app, monkeypatch):
    """Only the preview grant is honoured behind an obsolete cookie."""
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: True)
    client = _client(app, session=STALE_COOKIES[0], preview=None, proxied=False)
    assert client.get("/api/v1/sessions").status_code == 401


@pytest.mark.parametrize("stale", [None, *STALE_COOKIES])
@pytest.mark.parametrize("preview", ["valid", "expired", "forged", None])
@pytest.mark.parametrize("ingress", ["local", "remote"])
def test_auth_status_and_protected_reads_agree(app, stale, preview, ingress):
    client = _client(app, session=stale, preview=preview, ingress=ingress)
    authenticated = _status(client)["authenticated"]
    read = client.get("/api/v1/sessions")
    assert read.status_code == (200 if authenticated else 401)
    # Only a valid grant on the local listener authenticates here at all.
    assert authenticated is (preview == "valid" and ingress == "local")
