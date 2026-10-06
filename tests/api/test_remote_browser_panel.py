"""The viewer worker must never use its own browser for a mirrored session."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from app.api import browser_panel as api
from app.api.auth import require_auth
from app.network import browser_panel as remote
from app.network import control, tokens


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_auth] = lambda: "owner"
    monkeypatch.setattr(
        api,
        "require_session",
        AsyncMock(return_value={"id": "session", "owner_device_id": "remote"}),
    )
    monkeypatch.setattr(control, "owner_of", lambda row: "remote")
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize(
    "method,suffix,operation",
    [
        ("GET", "", ""),
        ("POST", "/start", "start"),
        ("POST", "/upload?viewer_id=v&runtime_id=r&tab_id=t&field=f", "upload"),
    ],
)
def test_http_follows_session_owner(client, monkeypatch, method, suffix, operation):
    forward = AsyncMock(
        return_value=Response(b'{"remote":true}', media_type="application/json")
    )
    monkeypatch.setattr(remote, "forward_http", forward)
    response = client.request(method, "/api/v1/sessions/session/browser/panel" + suffix)
    assert response.json() == {"remote": True}
    assert forward.await_args.args[1:] == ("remote", "session", operation)


def test_http_access_check_precedes_remote_call(client, monkeypatch):
    monkeypatch.setattr(
        api, "require_session", AsyncMock(side_effect=HTTPException(403))
    )
    forward = AsyncMock()
    monkeypatch.setattr(remote, "forward_http", forward)
    assert (
        client.post("/api/v1/sessions/session/browser/panel/start").status_code == 403
    )
    forward.assert_not_awaited()


def test_remote_websocket_never_attaches_local_hub(client, monkeypatch):
    monkeypatch.setattr(
        api, "authenticate_websocket", AsyncMock(return_value=(True, None))
    )
    monkeypatch.setattr(api.database, "get_session", lambda _: {"id": "session"})
    local = AsyncMock()
    monkeypatch.setattr(api, "serve_browser_panel", local)

    async def forward(ws, owner, session):
        assert (owner, session) == ("remote", "session")
        await ws.send_json({"type": "hello", "viewer_id": "remote-viewer"})
        assert await ws.receive_json() == {"type": "control", "op": "take"}
        await ws.send_json({"type": "workspace", "runtime_id": "remote-runtime"})

    monkeypatch.setattr(remote, "forward_websocket", forward)
    with client.websocket_connect("/api/v1/ws/browser/session") as ws:
        assert ws.receive_json()["viewer_id"] == "remote-viewer"
        ws.send_json({"type": "control", "op": "take"})
        assert ws.receive_json()["runtime_id"] == "remote-runtime"
    local.assert_not_awaited()


def test_ws_access_check_precedes_remote_call(client, monkeypatch):
    monkeypatch.setattr(
        api,
        "authenticate_websocket",
        AsyncMock(return_value=(True, {"project_id": "allowed"})),
    )
    monkeypatch.setattr(api.database, "get_session", lambda _: {"project_id": "denied"})
    forward = AsyncMock()
    monkeypatch.setattr(remote, "forward_websocket", forward)
    with client.websocket_connect("/api/v1/ws/browser/session") as ws:
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_json()
        assert exc.value.code == 4003
    forward.assert_not_awaited()


def test_peer_http_requires_device_not_user_auth(client):
    assert client.get("/api/v1/network/peer/browser/session").status_code == 401
    assert client.post("/api/v1/network/peer/browser/session/start").status_code == 401


def test_peer_refuses_non_owned_session(client, monkeypatch):
    client.app.dependency_overrides[tokens.require_worker_device] = lambda: "peer"
    monkeypatch.setattr(control, "_require_owned", Mock(side_effect=PermissionError()))
    assert client.post("/api/v1/network/peer/browser/session/start").status_code == 409


@pytest.mark.asyncio
async def test_peer_socket_rejects_missing_credentials(monkeypatch):
    ws = SimpleNamespace(
        headers={},
        state=SimpleNamespace(),
        send_denial_response=AsyncMock(),
        accept=AsyncMock(),
    )
    await remote.serve_peer(ws, "session")
    assert ws.send_denial_response.await_args.args[0].status_code == 401
    ws.accept.assert_not_awaited()


@pytest.mark.asyncio
async def test_bidirectional_relay_preserves_frames_and_input_and_cancels():
    incoming = asyncio.Queue()
    frames = asyncio.Queue()
    sent = []

    class Upstream:
        async def send(self, payload):
            sent.append(payload)
            await frames.put(b"jpeg-frame")

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await frames.get()

    viewer = SimpleNamespace(
        receive=incoming.get,
        send_text=AsyncMock(),
        send_bytes=AsyncMock(),
        close=AsyncMock(),
    )
    await incoming.put({"type": "websocket.receive", "text": '{"type":"input","x":5}'})

    async def receive_frame(payload):
        assert payload == b"jpeg-frame"
        await incoming.put({"type": "websocket.disconnect"})

    viewer.send_bytes.side_effect = receive_frame
    await asyncio.wait_for(remote.relay(viewer, Upstream()), 1)
    assert sent == ['{"type":"input","x":5}']
    viewer.send_bytes.assert_awaited_once_with(b"jpeg-frame")


def request(body=b"payload"):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/unused",
            "query_string": b"viewer_id=v&field=f",
            "headers": [
                (b"content-type", b"multipart/form-data; boundary=sample"),
                (b"cookie", b"session=private-user-token"),
            ],
        },
        receive,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 409, 413])
async def test_http_preserves_upload_bytes_identity_and_errors(monkeypatch, status):
    link = SimpleNamespace(
        token="device-token", base_url="http://peer", authenticate=AsyncMock()
    )
    monkeypatch.setattr(remote, "peer_link", AsyncMock(return_value=(object(), link)))
    requests = []

    def handle(req):
        requests.append(req)
        return httpx.Response(status, json={"detail": "owner-result"})

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw),
    )
    response = await remote.forward_http(request(), "remote", "session", "upload")
    assert response.status_code == status
    req = requests[0]
    assert req.content == b"payload"
    assert req.url.path == "/api/v1/network/peer/browser/session/upload"
    assert req.url.query == b"viewer_id=v&field=f"
    assert req.headers["authorization"] == "Bearer device-token"
    assert "cookie" not in req.headers
    assert req.headers["content-type"] == "multipart/form-data; boundary=sample"


@pytest.mark.asyncio
async def test_http_refreshes_rejected_token_but_never_retries_timeout(monkeypatch):
    link = SimpleNamespace(token="old", base_url="http://peer")

    async def authenticate(*args):
        link.token = "new" if link.token is None else link.token

    link.authenticate = authenticate
    monkeypatch.setattr(remote, "peer_link", AsyncMock(return_value=(object(), link)))
    requests = []

    def handle(req):
        requests.append(req)
        if len(requests) == 1:
            return httpx.Response(401)
        raise httpx.ReadTimeout("outcome unknown")

    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client_type(transport=httpx.MockTransport(handle), **kw),
    )
    with pytest.raises(HTTPException) as exc:
        await remote.forward_http(request(), "remote", "session", "start")
    assert exc.value.status_code == 503
    assert len(requests) == 2
    assert requests[1].headers["authorization"] == "Bearer new"


@pytest.mark.asyncio
async def test_http_upload_limit_before_network(monkeypatch):
    from app.core import browser_uploads

    monkeypatch.setattr(browser_uploads, "MAX_BODY", 2)
    link = SimpleNamespace(authenticate=AsyncMock())
    monkeypatch.setattr(remote, "peer_link", AsyncMock(return_value=(object(), link)))
    with pytest.raises(HTTPException) as exc:
        await remote.forward_http(request(), "remote", "session", "upload")
    assert exc.value.status_code == 413
    link.authenticate.assert_not_awaited()


@pytest.mark.asyncio
async def test_unreachable_worker_has_no_local_fallback(monkeypatch):
    monkeypatch.setattr(remote, "peer_link", AsyncMock(side_effect=HTTPException(503)))
    ws = SimpleNamespace(send_json=AsyncMock(), close=AsyncMock())
    await remote.forward_websocket(ws, "remote", "session")
    assert ws.send_json.await_args.args[0]["type"] == "error"
    assert ws.close.await_args.kwargs["code"] == 1013


def test_websocket_redirects_are_refused():
    from websockets.exceptions import InvalidHandshake

    with pytest.raises(InvalidHandshake):
        remote.PeerConnect.handle_redirect(None, "wss://different-host")


@pytest.mark.asyncio
async def test_peer_stream_stops_on_revocation_and_cleans_viewer(monkeypatch):
    stopped = asyncio.Event()
    checks = 0

    def authorize(ws):
        nonlocal checks
        checks += 1
        if checks > 1:
            raise HTTPException(403)
        return "peer"

    async def serve(ws, session_id):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(tokens, "require_worker_device", authorize)
    monkeypatch.setattr(remote, "require_owned", AsyncMock())
    monkeypatch.setattr(api, "serve_browser_panel", serve)
    monkeypatch.setattr(remote, "AUTH_CHECK_SECONDS", 0)
    ws = SimpleNamespace(accept=AsyncMock(), close=AsyncMock())
    await asyncio.wait_for(remote.serve_peer(ws, "session"), 1)
    assert stopped.is_set()
    assert ws.close.await_args.kwargs["code"] == 1013


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "http://elsewhere"}),
        httpx.Response(200, content=b"oversized"),
    ],
)
async def test_http_refuses_redirects_and_oversized_responses(monkeypatch, response):
    link = SimpleNamespace(
        token="device", base_url="http://peer", authenticate=AsyncMock()
    )
    monkeypatch.setattr(remote, "peer_link", AsyncMock(return_value=(object(), link)))
    monkeypatch.setattr(remote, "MAX_MESSAGE", 2)
    client_type = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: client_type(
            transport=httpx.MockTransport(lambda _: response), **kw
        ),
    )
    with pytest.raises(HTTPException) as exc:
        await remote.forward_http(request(), "remote", "session", "start")
    assert exc.value.status_code == 502


@pytest.mark.asyncio
async def test_token_renewal_reconnect_does_not_report_worker_outage(monkeypatch):
    from websockets.exceptions import ConnectionClosedError
    from websockets.frames import Close

    link = SimpleNamespace(
        token="device", base_url="http://peer", authenticate=AsyncMock()
    )
    monkeypatch.setattr(remote, "peer_link", AsyncMock(return_value=(object(), link)))

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(remote, "PeerConnect", lambda *args, **kwargs: Socket())
    monkeypatch.setattr(
        remote,
        "relay",
        AsyncMock(
            side_effect=ConnectionClosedError(
                Close(1013, "renew"), Close(1013, "renew"), True
            )
        ),
    )
    ws = SimpleNamespace(send_json=AsyncMock(), close=AsyncMock())
    await remote.forward_websocket(ws, "remote", "session")
    ws.send_json.assert_not_awaited()
    assert ws.close.await_args.kwargs["code"] == 1013


@pytest.mark.asyncio
async def test_removing_the_owning_device_ends_the_viewer_relay(monkeypatch):
    """The viewer side re-checks the owner's membership as the peer side re-checks the token."""
    from app.network import engine

    link = SimpleNamespace(token="device", base_url="http://peer", authenticate=AsyncMock())
    monkeypatch.setattr(remote, "peer_link", AsyncMock(return_value=(object(), link)))
    monkeypatch.setattr(remote, "AUTH_CHECK_SECONDS", 0.01)
    member = SimpleNamespace(status="active")
    monkeypatch.setattr(engine, "context", lambda **_: SimpleNamespace(
        member=lambda device_id: member if device_id == "remote" else None))
    idle = asyncio.Event()
    closed = []

    class Socket:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(True)

        async def send(self, payload):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            await idle.wait()  # the owner keeps the stream open
            raise StopAsyncIteration

    monkeypatch.setattr(remote, "PeerConnect", lambda *args, **kwargs: Socket())

    async def viewer_receive():
        await idle.wait()  # the viewer keeps watching
        return {"type": "websocket.disconnect"}

    ws = SimpleNamespace(receive=viewer_receive, send_json=AsyncMock(), send_text=AsyncMock(),
                         send_bytes=AsyncMock(), close=AsyncMock())
    task = asyncio.create_task(remote.forward_websocket(ws, "remote", "session"))
    await asyncio.sleep(0.05)
    assert not task.done(), "an active owner keeps the relay open"
    member.status = "revoked"
    await asyncio.wait_for(task, 1)
    assert closed == [True]
    assert ws.close.await_args.kwargs["code"] == 1013
