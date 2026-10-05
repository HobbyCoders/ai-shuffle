"""The agent browser's own listener.

The browser is refused the worker's local and protected ports, because a
request from the same host to them is the owner. Without another door no
agent could look at the running UI at all. The preview listener is that door:
it never confers local-owner trust, and nothing passes it except the token
redirect and requests that carry a live session grant.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from app.core import owner_auth, preview_access
from app.core.config import settings
from app.serve import IngressApplication, configured_preview_port

SESSION = "sess-preview-listener"
LOCAL, REMOTE, PREVIEW = 8000, 8001, 8002
# The test client ignores base_url for sockets, so the port is spelled out.
WS = f"ws://172.17.0.2:{PREVIEW}/ws"


@pytest.fixture(autouse=True)
def _isolated_key(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: False)
    monkeypatch.setattr(settings, "shuffle_local_access", True)
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: True)


def _app():
    app = FastAPI()

    @app.get("/api/v1/auth/preview-access")
    async def redirect(request: Request):
        return {"ingress": request.scope["shuffle.ingress"]}

    @app.get("/api/v1/whoami")
    @app.post("/api/v1/whoami")
    async def whoami(request: Request):
        grant = preview_access.grant(request)
        return {
            "ingress": request.scope["shuffle.ingress"],
            "local_owner": bool(owner_auth.local_access(request)),
            "grant": grant.session_id if grant else None,
            "https": request.scope["shuffle.forwarded_https"],
        }

    @app.get("/")
    async def shell():
        return {"page": "shell"}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_json({"ingress": websocket.scope["shuffle.ingress"]})
        await websocket.close()

    return IngressApplication(app, LOCAL, REMOTE, PREVIEW)


def _client(port, peer="172.17.0.2", cookies=None):
    client = TestClient(_app(), base_url=f"http://172.17.0.2:{port}", client=(peer, 54321))
    for name, value in (cookies or {}).items():
        client.cookies.set(name, value)
    return client


def _cookie():
    return {preview_access.COOKIE_NAME: preview_access.mint(SESSION)}


# ─── port choice ─────────────────────────────────────────────────────────────


def test_default_port_sits_beside_the_other_two():
    assert configured_preview_port({}, 8000, 8001) == 8002
    # An isolated desktop's ports move together.
    assert configured_preview_port({}, 18080, 18081) == 18082
    # A protected port placed where the default would land is stepped over.
    assert configured_preview_port({}, 8000, 8002) == 8003


@pytest.mark.parametrize("value", ["0", "off", "OFF", "false", "no"])
def test_listener_can_be_switched_off(value):
    assert configured_preview_port({"SHUFFLE_PREVIEW_PORT": value}, 8000, 8001) is None


def test_explicit_port_is_used_and_checked():
    assert configured_preview_port({"SHUFFLE_PREVIEW_PORT": "9100"}, 8000, 8001) == 9100
    for bad in ("8000", "8001", "70000", "abc"):
        with pytest.raises(ValueError):
            configured_preview_port({"SHUFFLE_PREVIEW_PORT": bad}, 8000, 8001)


def test_ingress_refuses_a_shared_port():
    with pytest.raises(ValueError):
        IngressApplication(FastAPI(), 8000, 8001, 8000)


def test_recorded_listener_port_round_trips(monkeypatch):
    monkeypatch.setattr(preview_access, "_listener_port", None)
    assert preview_access.listener_port() is None
    preview_access.set_listener_port(8002)
    assert preview_access.listener_port() == 8002
    preview_access.set_listener_port(None)
    assert preview_access.listener_port() is None


@pytest.mark.parametrize("bind, loopback, advertised", [
    ("127.0.0.1", True, "127.0.0.1"),
    ("::1", True, "::1"),
    ("localhost", True, "127.0.0.1"),
    ("0.0.0.0", False, "10.0.0.134"),
    ("10.0.0.134", False, "10.0.0.134"),
    (None, False, "10.0.0.134"),
])
def test_advertised_host_is_one_the_listener_answers_on(monkeypatch, bind, loopback, advertised):
    """#1789: a loopback-only listener must not be advertised by LAN address."""
    monkeypatch.setattr(preview_access, "_listener_port", None)
    monkeypatch.setattr(preview_access, "_listener_host", None)
    preview_access.set_listener_port(8002, bind)
    assert preview_access.listener_loopback_only() is loopback
    assert preview_access.advertised_host("10.0.0.134") == advertised


def test_no_listener_is_never_loopback_only(monkeypatch):
    monkeypatch.setattr(preview_access, "_listener_port", None)
    monkeypatch.setattr(preview_access, "_listener_host", None)
    preview_access.set_listener_port(None, "127.0.0.1")
    assert preview_access.listener_loopback_only() is False
    assert preview_access.advertised_host("10.0.0.134") == "10.0.0.134"


# ─── the gate ────────────────────────────────────────────────────────────────


def test_nothing_passes_without_a_grant():
    client = _client(PREVIEW)
    for path in ("/", "/api/v1/whoami", "/health"):
        response = client.get(path)
        assert response.status_code == 403, path
        assert "agent preview" in response.json()["detail"]
    assert client.post("/api/v1/whoami").status_code == 403


def test_a_same_host_peer_is_never_the_owner_here():
    """The whole reason the browser is refused :8000: there, this peer is the
    owner without presenting anything. Here it gets a grant and no more."""
    on_local = _client(LOCAL).get("/api/v1/whoami").json()
    assert on_local == {"ingress": "local", "local_owner": True, "grant": None, "https": False}

    on_preview = _client(PREVIEW, cookies=_cookie()).get("/api/v1/whoami").json()
    assert on_preview == {"ingress": "preview", "local_owner": False, "grant": SESSION, "https": False}


def test_redirect_endpoint_is_reachable_without_a_cookie():
    response = _client(PREVIEW).get("/api/v1/auth/preview-access?token=anything")
    assert response.status_code == 200
    assert response.json() == {"ingress": "preview"}
    # Only as a GET: no other method reaches the app without a grant.
    assert _client(PREVIEW).post("/api/v1/auth/preview-access").status_code == 403


@pytest.mark.parametrize("token", ["", "garbage", "v1.a.b.c"])
def test_a_bad_cookie_is_refused(token):
    client = _client(PREVIEW, cookies={preview_access.COOKIE_NAME: token})
    assert client.get("/api/v1/whoami").status_code == 403


def test_an_expired_grant_is_refused():
    stale = preview_access.mint(SESSION, ttl_seconds=60, now=1_000_000.0)
    client = _client(PREVIEW, cookies={preview_access.COOKIE_NAME: stale})
    assert client.get("/api/v1/whoami").status_code == 403


def test_a_grant_for_a_missing_session_is_refused(monkeypatch):
    monkeypatch.setattr(preview_access, "_session_exists", lambda session_id: False)
    assert _client(PREVIEW, cookies=_cookie()).get("/api/v1/whoami").status_code == 403


def test_a_public_peer_gets_no_grant_even_with_the_cookie():
    body = _client(PREVIEW, peer="8.8.8.8", cookies=_cookie()).get("/api/v1/whoami").json()
    assert body["grant"] is None and body["local_owner"] is False


def test_forwarded_https_cannot_be_claimed_on_the_preview_port():
    client = _client(PREVIEW, peer="127.0.0.1", cookies=_cookie())
    body = client.get("/api/v1/whoami", headers={"x-forwarded-proto": "https"}).json()
    assert body["https"] is False


def test_mutation_ceiling_still_applies_on_the_preview_port():
    client = _client(PREVIEW, cookies=_cookie())
    assert client.post("/api/v1/whoami").json()["grant"] == SESSION
    # grant() itself refuses the protected prefixes, whatever the listener.
    scope = {"type": "http", "method": "POST", "path": "/api/v1/settings/x", "query_string": b"",
             "server": ("0.0.0.0", PREVIEW), "client": ("172.17.0.2", 5), "shuffle.ingress": "preview",
             "headers": [(b"cookie", f"{preview_access.COOKIE_NAME}={preview_access.mint(SESSION)}".encode())]}
    assert preview_access.grant(Request(scope)) is None


def test_websocket_needs_the_grant():
    with pytest.raises(WebSocketDisconnect) as refused:
        with _client(PREVIEW).websocket_connect(WS):
            pass
    assert refused.value.code == 4403
    with _client(PREVIEW, cookies=_cookie()).websocket_connect(WS) as socket:
        assert socket.receive_json() == {"ingress": "preview"}


def test_the_other_listeners_are_unchanged():
    assert _client(LOCAL).get("/").json() == {"page": "shell"}
    remote = _client(REMOTE, peer="127.0.0.1").get("/api/v1/whoami").json()
    assert remote["ingress"] == "remote" and remote["local_owner"] is False
    # An unknown socket stays remote, as before.
    assert _client(9999).get("/api/v1/whoami").json()["ingress"] == "remote"


def test_transport_check_accepts_the_preview_listener_only_from_private_peers():
    def request(ingress, peer):
        return Request({"type": "http", "method": "GET", "path": "/", "query_string": b"", "headers": [],
                        "server": ("0.0.0.0", 1), "client": (peer, 5), "shuffle.ingress": ingress})

    assert preview_access.transport_ok(request("preview", "172.17.0.2"))
    assert preview_access.transport_ok(request("local", "10.0.0.5"))
    assert not preview_access.transport_ok(request("preview", "8.8.8.8"))
    assert not preview_access.transport_ok(request("remote", "127.0.0.1"))
