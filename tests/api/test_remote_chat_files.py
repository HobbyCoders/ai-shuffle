from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
from starlette.requests import Request
from app.api import shared_files as api
from app.api.auth import require_auth
from app.network import files, tokens, control


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_auth] = lambda: "owner"
    monkeypatch.setattr(api, "require_session", AsyncMock(return_value={"id": "chat"}))
    monkeypatch.setattr(control, "owner_of", lambda _: "remote")
    with TestClient(app) as client:
        yield client


def test_routes_before_resolving_remote_path(client, monkeypatch):
    forward = AsyncMock(return_value=Response(b"remote file"))
    monkeypatch.setattr(api, "forward_file", forward)
    local = Mock(side_effect=AssertionError("must not resolve remote path here"))
    monkeypatch.setattr(api, "require_file_access", local)
    response = client.get("/api/files/by-path", params={"path": "/linux/report.zip", "session_id": "chat"})
    assert response.content == b"remote file"
    assert forward.await_args.args[1:] == ("remote", "chat", "/linux/report.zip", None)
    local.assert_not_called()


def test_access_denial_precedes_forward(client, monkeypatch):
    monkeypatch.setattr(api, "require_session", AsyncMock(side_effect=HTTPException(403)))
    forward = AsyncMock()
    monkeypatch.setattr(api, "forward_file", forward)
    assert client.get("/api/files/by-path?path=/report&session_id=chat").status_code == 403
    forward.assert_not_awaited()


@pytest.mark.parametrize("scoped", [False, True])
def test_local_and_legacy_files(client, monkeypatch, tmp_path, scoped):
    monkeypatch.setattr(control, "owner_of", lambda _: None)
    monkeypatch.setattr(api, "require_file_access", Mock())
    path = tmp_path / "report.txt"
    path.write_bytes(b"local report")
    params = {"path": str(path)}
    if scoped:
        params["session_id"] = "chat"
    response = client.get("/api/files/by-path", params=params)
    assert response.content == b"local report"
    assert "report.txt" in response.headers["content-disposition"]


def test_peer_needs_device_auth(client):
    assert client.get("/api/files/peer/chat?path=/report").status_code == 401


@pytest.mark.parametrize("error,status", [(LookupError(), 404), (PermissionError(), 409)])
def test_peer_requires_local_owner(client, monkeypatch, error, status):
    client.app.dependency_overrides[tokens.require_device] = lambda: "viewer"
    monkeypatch.setattr(control, "_require_owned", Mock(side_effect=error))
    assert client.get("/api/files/peer/chat?path=/report").status_code == status


def test_peer_checks_file_roots_and_scope(client, monkeypatch):
    client.app.dependency_overrides[tokens.require_device] = lambda: "viewer"
    monkeypatch.setattr(control, "_require_owned", lambda _: {"project_id": "project"})
    access = Mock(side_effect=HTTPException(403))
    monkeypatch.setattr(api, "require_file_access", access)
    assert client.get("/api/files/peer/chat?path=/private&project_id=other").status_code == 403
    access.assert_not_called()
    assert client.get("/api/files/peer/chat?path=/private&project_id=project").status_code == 403
    assert access.call_args.args[2].state.api_user == {"project_id": "project"}


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 206, 403, 404, 416])
async def test_stream_preserves_bytes_headers_and_range(monkeypatch, status):
    seen, closed = [], []
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"file"
            yield b" bytes"
        async def aclose(self):
            closed.append(True)
    def handle(request):
        seen.append(request)
        return httpx.Response(status, stream=Body(), headers={
            "content-type": "application/zip", "content-disposition": 'attachment; filename="report.zip"',
            "content-length": "10", "content-range": "bytes 0-9/20", "set-cookie": "never=forward",
        })
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(files.httpx, "AsyncClient", lambda **_: client)
    link = SimpleNamespace(base_url="https://peer", token="device-only", authenticate=AsyncMock())
    monkeypatch.setattr(files, "peer_link", AsyncMock(return_value=(object(), link)))
    monkeypatch.setattr(files.transport, "note_contact", Mock())
    request = Request({"type": "http", "headers": [(b"range", b"bytes=0-9"), (b"cookie", b"secret")]})
    response = await files.forward_file(request, "remote", "chat", "/linux/report.zip", "project")
    assert response.status_code == status
    assert b"".join([chunk async for chunk in response.body_iterator]) == b"file bytes"
    assert closed and client.is_closed
    assert response.headers["content-disposition"] == 'attachment; filename="report.zip"'
    assert response.headers["content-range"] == "bytes 0-9/20"
    assert "set-cookie" not in response.headers
    assert seen[0].headers["range"] == "bytes=0-9"
    assert "cookie" not in seen[0].headers
    assert seen[0].url.params["project_id"] == "project"


@pytest.mark.asyncio
async def test_refuses_redirect_and_closes_client(monkeypatch):
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(302, headers={"location": "https://evil"})))
    monkeypatch.setattr(files.httpx, "AsyncClient", lambda **_: client)
    link = SimpleNamespace(base_url="https://peer", token="device-only", authenticate=AsyncMock())
    monkeypatch.setattr(files, "peer_link", AsyncMock(return_value=(object(), link)))
    with pytest.raises(HTTPException) as error:
        await files.forward_file(Request({"type": "http", "headers": []}), "remote", "chat", "/file", None)
    assert error.value.status_code == 502
    assert client.is_closed

@pytest.mark.asyncio
async def test_unavailable_peer_never_falls_back_locally(monkeypatch):
    def unavailable(request):
        raise httpx.ConnectError("offline", request=request)
    client = httpx.AsyncClient(transport=httpx.MockTransport(unavailable))
    monkeypatch.setattr(files.httpx, "AsyncClient", lambda **_: client)
    link = SimpleNamespace(base_url="https://peer", token="device-only", authenticate=AsyncMock())
    monkeypatch.setattr(files, "peer_link", AsyncMock(return_value=(object(), link)))
    with pytest.raises(HTTPException) as error:
        await files.forward_file(Request({"type": "http", "headers": []}), "remote", "chat", "/file", None)
    assert error.value.status_code == 503
    assert client.is_closed


def test_viewer_downloads_file_served_by_owner(client, monkeypatch, tmp_path):
    from app.api import common
    owner_app = FastAPI()
    owner_app.include_router(api.router)
    owner_app.dependency_overrides[tokens.require_device] = lambda: "viewer"
    monkeypatch.setattr(control, "_require_owned", lambda _: {"id": "chat"})
    monkeypatch.setattr(api, "settings", SimpleNamespace(effective_workspace_dir=tmp_path))
    monkeypatch.setattr(common, "_registered_serve_roots", lambda: ())
    data = bytes(range(256)) * 100
    path = tmp_path / "remote report.zip"
    path.write_bytes(data)
    with TestClient(owner_app) as owner_client:
        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield data
        def handle(request):
            assert request.headers["authorization"] == "Bearer device-only"
            upstream = owner_client.get(str(request.url))
            assert upstream.status_code == 200
            assert upstream.content == data
            return httpx.Response(upstream.status_code, headers=upstream.headers, stream=Body())
        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        monkeypatch.setattr(files.httpx, "AsyncClient", lambda **_: upstream_client)
        link = SimpleNamespace(base_url="https://peer", token="device-only", authenticate=AsyncMock())
        monkeypatch.setattr(files, "peer_link", AsyncMock(return_value=(object(), link)))
        monkeypatch.setattr(files.transport, "note_contact", Mock())
        response = client.get("/api/files/by-path", params={"path": str(path), "session_id": "chat"})
        assert response.status_code == 200
        assert response.content == data
        assert 'remote%20report.zip' in response.headers["content-disposition"]
        assert upstream_client.is_closed

@pytest.mark.asyncio
async def test_expired_device_token_is_reauthenticated_once(monkeypatch):
    seen = []
    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"download"
    def handle(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(401 if len(seen) == 1 else 200, stream=Body())
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    monkeypatch.setattr(files.httpx, "AsyncClient", lambda **_: client)
    link = SimpleNamespace(base_url="https://peer", token="expired")
    async def authenticate(*_):
        if link.token is None:
            link.token = "fresh"
    link.authenticate = authenticate
    monkeypatch.setattr(files, "peer_link", AsyncMock(return_value=(object(), link)))
    monkeypatch.setattr(files.transport, "note_contact", Mock())
    response = await files.forward_file(Request({"type": "http", "headers": []}), "remote", "chat", "/file", None)
    assert b"".join([chunk async for chunk in response.body_iterator]) == b"download"
    assert seen == ["Bearer expired", "Bearer fresh"]
    assert client.is_closed


def test_forward_scope_comes_from_authenticated_user(client, monkeypatch):
    monkeypatch.setattr(api, "get_api_user_from_request", lambda _: {"project_id": "allowed"})
    forward = AsyncMock(return_value=Response(b"scoped"))
    monkeypatch.setattr(api, "forward_file", forward)
    assert client.get("/api/files/by-path?path=/file&session_id=chat&project_id=other").status_code == 200
    assert forward.await_args.args[-1] == "allowed"
