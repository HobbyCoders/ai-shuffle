"""Generated media retains owner routing and file authorization for all kinds."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient

from app.api import (
    common,
    generated_audio,
    generated_images,
    generated_videos,
    media_serve,
)
from app.api.auth import require_auth
from app.network import control, files, tokens


@pytest.fixture(params=[(generated_images, "images", "png"),
                       (generated_videos, "videos", "mp4"),
                       (generated_audio, "audio", "wav")])
def media(request, monkeypatch, tmp_path):
    module, kind, extension = request.param
    app = FastAPI()
    app.include_router(module.router)
    app.dependency_overrides[require_auth] = lambda: "owner"
    monkeypatch.setattr(module, "settings", SimpleNamespace(effective_workspace_dir=tmp_path))
    monkeypatch.setattr(common, "_registered_serve_roots", lambda: ())
    monkeypatch.setattr(media_serve, "require_session", AsyncMock(return_value={"id": "chat"}))
    monkeypatch.setattr(control, "owner_of", lambda _: "remote")
    directory = tmp_path / f"generated-{kind}"
    directory.mkdir()
    path = directory / f"sample.{extension}"
    path.write_bytes(bytes(range(256)))
    with TestClient(app) as client:
        yield client, module, f"/api/generated-{kind}", path


@pytest.mark.parametrize("filename", [False, True])
def test_owner_serves_actual_media_and_range(media, monkeypatch, filename):
    viewer, module, prefix, path = media
    owner_app = FastAPI()
    owner_app.include_router(module.router)
    owner_app.dependency_overrides[tokens.require_device] = lambda: "viewer"
    monkeypatch.setattr(control, "_require_owned", lambda _: {"id": "chat"})
    seen = []
    with TestClient(owner_app) as owner:
        class Body(httpx.AsyncByteStream):
            def __init__(self, content):
                self.content = content

            async def __aiter__(self):
                yield self.content

        def handle(request):
            seen.append(request)
            upstream = owner.get(str(request.url), headers={"range": request.headers["range"]})
            return httpx.Response(upstream.status_code, headers=upstream.headers,
                                  stream=Body(upstream.content))

        upstream = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        monkeypatch.setattr(files.httpx, "AsyncClient", lambda **_: upstream)
        link = SimpleNamespace(base_url="https://peer", token="device-only", authenticate=AsyncMock())
        monkeypatch.setattr(files, "peer_link", AsyncMock(return_value=(object(), link)))
        monkeypatch.setattr(files.transport, "note_contact", Mock())
        params = {"session_id": "chat"}
        if not filename:
            params["path"] = str(path)
        response = viewer.get(f"{prefix}/{path.name if filename else 'by-path'}", params=params,
                              headers={"range": "bytes=10-29", "cookie": "private=viewer"})
        assert response.status_code == 206
        assert response.content == bytes(range(10, 30))
        assert response.headers["content-range"] == "bytes 10-29/256"
        assert path.name in response.headers["content-disposition"]
        assert response.headers["content-type"].startswith(("image/", "video/", "audio/"))
        assert seen[0].url.path == f"{prefix}/peer/chat"
        assert seen[0].headers["authorization"] == "Bearer device-only"
        assert "cookie" not in seen[0].headers
        assert upstream.is_closed


def test_authorization_precedes_forwarding_and_scope_is_trusted(media, monkeypatch):
    client, _, prefix, path = media
    forward = AsyncMock(return_value=Response(b"remote"))
    monkeypatch.setattr(media_serve, "forward_download", forward)
    monkeypatch.setattr(media_serve, "get_api_user_from_request", lambda _: {"project_id": "allowed"})
    params = {"session_id": "chat", "path": str(path), "project_id": "untrusted"}
    assert client.get(f"{prefix}/by-path", params=params).content == b"remote"
    assert forward.await_args.args[-1] == {"path": str(path), "project_id": "allowed"}
    forward.reset_mock()
    monkeypatch.setattr(media_serve, "require_session", AsyncMock(side_effect=HTTPException(403)))
    assert client.get(f"{prefix}/by-path", params=params).status_code == 403
    forward.assert_not_awaited()


def test_peer_rechecks_device_ownership_and_file_scope(media, monkeypatch):
    client, _, prefix, path = media
    url = f"{prefix}/peer/chat"
    assert client.get(url, params={"path": str(path)}).status_code == 401
    client.app.dependency_overrides[tokens.require_device] = lambda: "viewer"
    monkeypatch.setattr(control, "_require_owned", Mock(side_effect=PermissionError))
    assert client.get(url, params={"path": str(path)}).status_code == 409
    monkeypatch.setattr(control, "_require_owned", lambda _: {"project_id": "allowed"})
    assert client.get(url, params={"path": str(path), "project_id": "other"}).status_code == 403
    assert client.get(url, params={"path": str(path), "filename": path.name}).status_code == 400
    assert client.get(url, params={"filename": "../private.png"}).status_code == 400
    access = Mock(side_effect=HTTPException(403))
    monkeypatch.setattr(media_serve, "require_file_access", access)
    assert client.get(url, params={"path": str(path), "project_id": "allowed"}).status_code == 403
    assert access.call_args.args[2].state.api_user == {"project_id": "allowed"}


def test_owner_offline_does_not_serve_viewer_file(media, monkeypatch):
    client, _, prefix, path = media
    monkeypatch.setattr(media_serve, "forward_download", AsyncMock(side_effect=HTTPException(503)))
    assert client.get(f"{prefix}/{path.name}", params={"session_id": "chat"}).status_code == 503


@pytest.mark.parametrize("scoped", [False, True])
def test_local_and_legacy_media(media, monkeypatch, scoped):
    client, _, prefix, path = media
    monkeypatch.setattr(control, "owner_of", lambda _: None)
    params = {"session_id": "chat"} if scoped else {}
    assert client.get(f"{prefix}/{path.name}", params=params).content == path.read_bytes()
