"""The Files page against another of the owner's workers.

``/api/v1/workspace/workers/{device_id}/...`` relays to the member worker's
``/api/v1/network/peer/workspace/...`` mirror. The far worker is faked by a
second FastAPI app reached through an ``httpx.MockTransport``, so every test
runs the real relay code and the real peer-side path checks.
"""
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import remote_workspace_files as remote
from app.api import workspace_files
from app.network import browser_panel, files, tokens, transport

REAL_ASYNC_CLIENT = httpx.AsyncClient
BASE = "/api/v1/workspace/workers/remote"


class FakeContext:
    device_id = "self"
    network_id = "net"
    generation = 3

    def member(self, device_id):
        return {
            "self": SimpleNamespace(status="active", kind="worker"),
            "remote": SimpleNamespace(status="active", kind="worker"),
            "relay": SimpleNamespace(status="active", kind="worker"),
            "pending": SimpleNamespace(status="pending", kind="worker"),
            "phone": SimpleNamespace(status="active", kind="consumer"),
            "gone": SimpleNamespace(status="revoked", kind="worker"),
        }.get(device_id)


@pytest.fixture
def peer_workspace(tmp_path, monkeypatch):
    from app.core import config

    workspace = tmp_path / "remote-workspace"
    (workspace / "docs").mkdir(parents=True)
    (workspace / "docs" / "readme.md").write_text("hello from the far worker")
    (tmp_path / "outside.txt").write_text("never")
    monkeypatch.setattr(config.settings, "workspace_dir", workspace)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", workspace)
    return workspace


@pytest.fixture
def peer(peer_workspace, monkeypatch):
    """The far worker: only the device-token mirror, nothing else."""
    peer_app = FastAPI()
    peer_app.include_router(workspace_files.peer_router)
    peer_app.dependency_overrides[tokens.require_worker_device] = lambda: "relay"
    seen = []
    with TestClient(peer_app) as peer_client:
        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            assert request.headers["authorization"] == "Bearer device-only"
            assert "cookie" not in request.headers
            upstream = peer_client.request(
                request.method, str(request.url),
                headers={k: v for k, v in request.headers.items() if k.lower() != "host"},
                content=request.content,
            )
            # A stream, not ``content=``: the download relay reads it raw.
            return httpx.Response(upstream.status_code, headers=upstream.headers,
                                  stream=httpx.ByteStream(upstream.content))

        monkeypatch.setattr(
            httpx, "AsyncClient",
            lambda **kw: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handle), **kw),
        )
        link = SimpleNamespace(base_url="https://peer", token="device-only", authenticate=AsyncMock())
        monkeypatch.setattr(browser_panel, "peer_link", AsyncMock(return_value=(object(), link)))
        monkeypatch.setattr(files, "peer_link", AsyncMock(return_value=(object(), link)))
        monkeypatch.setattr(transport, "note_contact", Mock())
        yield seen


@pytest.fixture
def owner(monkeypatch):
    """This worker, signed in as its owner."""
    monkeypatch.setattr(remote, "require_admin", lambda request: "owner-cookie")
    monkeypatch.setattr(remote.engine, "context", lambda: FakeContext())
    app = FastAPI()
    app.include_router(remote.router)
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# Every operation reaches the selected worker's own files
# ---------------------------------------------------------------------------

def test_owner_lists_the_other_workers_files(owner, peer):
    response = owner.get(f"{BASE}/files", params={"path": "docs"})
    assert response.status_code == 200
    assert [f["name"] for f in response.json()["files"]] == ["readme.md"]
    assert peer[0].url.path == "/api/v1/network/peer/workspace/files"
    assert peer[0].url.params["path"] == "docs"


def test_owner_creates_renames_deletes_uploads_and_downloads(owner, peer, peer_workspace):
    created = owner.post(f"{BASE}/files/folder", params={"path": "docs"}, json={"name": "new"})
    assert created.status_code == 200 and (peer_workspace / "docs" / "new").is_dir()

    renamed = owner.put(f"{BASE}/files/rename", params={"path": "docs/new"}, json={"new_name": "renamed"})
    assert renamed.status_code == 200 and (peer_workspace / "docs" / "renamed").is_dir()

    uploaded = owner.post(f"{BASE}/upload", data={"path": "docs"},
                          files={"file": ("note.txt", b"uploaded bytes", "text/plain")})
    assert uploaded.status_code == 200, uploaded.text
    assert (peer_workspace / "docs" / "note.txt").read_bytes() == b"uploaded bytes"

    downloaded = owner.get(f"{BASE}/files/download", params={"path": "docs/readme.md"})
    assert downloaded.status_code == 200
    assert downloaded.content == b"hello from the far worker"
    assert "readme.md" in downloaded.headers["content-disposition"]

    deleted = owner.request("DELETE", f"{BASE}/files", params={"path": "docs/renamed"})
    assert deleted.status_code == 200 and not (peer_workspace / "docs" / "renamed").exists()


@pytest.mark.parametrize("method,path,params", [
    ("GET", "/files", {"path": "../"}),
    ("GET", "/files/download", {"path": "../outside.txt"}),
    ("DELETE", "/files", {"path": "../outside.txt"}),
])
def test_far_worker_path_refusal_passes_through(owner, peer, method, path, params, tmp_path):
    response = owner.request(method, BASE + path, params=params)
    assert response.status_code == 400
    assert response.json()["detail"] == "Path escapes workspace boundary"
    assert (tmp_path / "outside.txt").exists()


def test_far_worker_missing_directory_passes_through(owner, peer):
    response = owner.get(f"{BASE}/files", params={"path": "nope"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Directory not found"


def test_oversized_upload_is_refused_before_relaying(owner, peer):
    response = owner.post(
        f"{BASE}/upload", content=b"x",
        headers={"content-type": "multipart/form-data; boundary=x",
                 "content-length": str(remote.MAX_UPLOAD_BODY + 1)},
    )
    assert response.status_code == 413
    assert "Maximum upload size" in response.json()["detail"]
    assert peer == []


# ---------------------------------------------------------------------------
# Who may ask
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("principal", ["preview:session-1"])
def test_session_preview_is_refused(owner, peer, monkeypatch, principal):
    monkeypatch.setattr(remote, "require_admin", lambda request: principal)
    response = owner.get(f"{BASE}/files")
    assert response.status_code == 403
    assert peer == []


def test_preview_state_is_refused_even_with_another_principal(owner, peer, monkeypatch):
    def admit(request):
        request.state.auth_token = "preview:session-1"
        return "local-owner"
    monkeypatch.setattr(remote, "require_admin", admit)
    assert owner.get(f"{BASE}/files").status_code == 403
    assert peer == []


@pytest.mark.parametrize("operation", [
    ("GET", "/files"), ("POST", "/files/folder"), ("PUT", "/files/rename"),
    ("DELETE", "/files"), ("POST", "/upload"), ("GET", "/files/download?path=a"),
])
def test_member_or_api_key_is_refused_on_every_route(owner, peer, monkeypatch, operation):
    def refuse(request):
        raise HTTPException(403, "Owner access required")
    monkeypatch.setattr(remote, "require_admin", refuse)
    method, path = operation
    assert owner.request(method, BASE + path).status_code == 403
    assert peer == []


def test_real_owner_gate_refuses_an_unauthenticated_caller(peer, monkeypatch):
    """No mock on require_admin: a bare request is not the owner."""
    from app.core import owner_auth
    monkeypatch.setattr(owner_auth, "local_access", lambda request: False)
    monkeypatch.setattr(remote.engine, "context", lambda: FakeContext())
    app = FastAPI()
    app.include_router(remote.router)
    with TestClient(app) as client:
        assert client.get(f"{BASE}/files").status_code in (401, 403)
    assert peer == []


def test_preview_cookie_is_refused_even_alongside_local_owner_access(peer, monkeypatch):
    """No mock on require_admin: a valid agent-preview cookie riding a request
    that local owner access would admit still never reaches another worker."""
    from app.core import owner_auth, preview_access
    monkeypatch.setattr(owner_auth, "local_access", lambda request: True)
    monkeypatch.setattr(owner_auth, "same_origin", lambda request: None)
    monkeypatch.setattr(preview_access, "verify",
                        lambda token: preview_access.Grant(session_id="s1", expires_at=2**40))
    monkeypatch.setattr(remote.engine, "context", lambda: FakeContext())
    app = FastAPI()
    app.include_router(remote.router)
    with TestClient(app) as client:
        client.cookies.set(preview_access.COOKIE_NAME, "preview-token")
        assert client.get(f"{BASE}/files").status_code == 403
        assert client.request("DELETE", f"{BASE}/files",
                              params={"path": "docs/readme.md"}).status_code == 403
    assert peer == []


@pytest.mark.parametrize("path", ["/files", "/files/download?path=docs/readme.md"])
def test_a_redirect_from_the_far_worker_is_not_followed(owner, monkeypatch, path):
    calls = []

    def redirect(request):
        calls.append(request.url)
        return httpx.Response(307, headers={"location": "https://elsewhere.example/steal"})
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(redirect), **kw),
    )
    link = SimpleNamespace(base_url="https://peer", token="device-only", authenticate=AsyncMock())
    monkeypatch.setattr(browser_panel, "peer_link", AsyncMock(return_value=(object(), link)))
    monkeypatch.setattr(files, "peer_link", AsyncMock(return_value=(object(), link)))
    monkeypatch.setattr(transport, "note_contact", Mock())
    response = owner.get(BASE + path)
    assert response.status_code == 502
    assert [url.host for url in calls] == ["peer"]


@pytest.mark.parametrize("device,status", [
    ("pending", 404), ("stranger", 404), ("self", 400), ("phone", 404), ("gone", 404),
])
def test_only_an_active_member_other_than_this_worker(owner, peer, device, status):
    response = owner.get(f"/api/v1/workspace/workers/{device}/files")
    assert response.status_code == status
    assert peer == []


def test_unpaired_worker_has_no_remote_files(owner, peer, monkeypatch):
    monkeypatch.setattr(remote.engine, "context", lambda: None)
    assert owner.get(f"{BASE}/files").status_code == 404
    assert peer == []


def test_peer_mirror_requires_a_device_token(peer_workspace):
    """An owner cookie or API key does not open the peer mirror."""
    from app.api.auth import require_auth
    app = FastAPI()
    app.include_router(workspace_files.peer_router)
    app.dependency_overrides[require_auth] = lambda: "owner"
    with TestClient(app) as client:
        response = client.get("/api/v1/network/peer/workspace/files")
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# The peer mirror on its own: a real device token, no relay in front
# ---------------------------------------------------------------------------

@pytest.fixture
def mirror(peer_workspace, monkeypatch):
    """The far worker's peer mirror, called directly with real device tokens."""
    tokens.reset_for_tests()
    ctx = FakeContext()
    monkeypatch.setattr(tokens.engine, "context", lambda: ctx)
    app = FastAPI()
    app.include_router(workspace_files.peer_router)

    def token_for(device_id):
        return {"Authorization": "Bearer " + tokens._mint(
            device_id, int(time.time()) + 60, tokens._scope(ctx))}

    with TestClient(app) as client:
        yield client, token_for
    tokens.reset_for_tests()


PEER_OPERATIONS = [
    ("GET", "/files", {"path": "docs"}, None),
    ("GET", "/files/download", {"path": "docs/readme.md"}, None),
    ("POST", "/files/folder", {"path": "docs"}, {"name": "made"}),
    ("PUT", "/files/rename", {"path": "docs/readme.md"}, {"new_name": "moved.md"}),
    ("DELETE", "/files", {"path": "docs/readme.md"}, None),
    ("POST", "/upload", None, None),
]


def _call_mirror(client, headers, method, path, params, body):
    url = "/api/v1/network/peer/workspace" + path
    if path == "/upload":
        return client.post(url, headers=headers, data={"path": "docs"},
                           files={"file": ("dropped.txt", b"x", "text/plain")})
    return client.request(method, url, headers=headers, params=params, json=body)


@pytest.mark.parametrize("method,path,params,body", PEER_OPERATIONS)
@pytest.mark.parametrize("device", ["phone", "gone", "self"])
def test_peer_mirror_admits_only_another_active_worker(mirror, peer_workspace, device,
                                                       method, path, params, body):
    """A consumer device (a phone, a paired browser) holds a valid device token
    too; it must not reach this worker's files with owner-level access."""
    client, token_for = mirror
    response = _call_mirror(client, token_for(device), method, path, params, body)
    assert response.status_code == 403
    assert sorted(p.name for p in (peer_workspace / "docs").iterdir()) == ["readme.md"]
    assert (peer_workspace / "docs" / "readme.md").read_text() == "hello from the far worker"


def test_peer_mirror_serves_an_active_worker(mirror):
    client, token_for = mirror
    response = client.get("/api/v1/network/peer/workspace/files",
                          headers=token_for("relay"), params={"path": "docs"})
    assert response.status_code == 200
    assert [f["name"] for f in response.json()["files"]] == ["readme.md"]


def test_peer_mirror_keeps_its_own_upload_cap(mirror, peer_workspace, monkeypatch):
    """The receiving side enforces the cap itself, whatever the relay sent."""
    from app.api import projects
    monkeypatch.setattr(projects, "MAX_UPLOAD_SIZE", 8)
    client, token_for = mirror
    response = client.post("/api/v1/network/peer/workspace/upload", headers=token_for("relay"),
                           data={"path": "docs"},
                           files={"file": ("big.bin", b"x" * 16, "application/octet-stream")})
    assert response.status_code == 413
    assert sorted(p.name for p in (peer_workspace / "docs").iterdir()) == ["readme.md"]


def test_peer_mirror_refuses_traversal_from_a_worker(mirror, tmp_path):
    client, token_for = mirror
    response = client.request("DELETE", "/api/v1/network/peer/workspace/files",
                              headers=token_for("relay"), params={"path": "../outside.txt"})
    assert response.status_code == 400
    assert (tmp_path / "outside.txt").exists()


# ---------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------

def test_offline_worker_reports_unavailable(owner, monkeypatch):
    monkeypatch.setattr(browser_panel, "peer_link",
                        AsyncMock(side_effect=HTTPException(503, browser_panel.UNAVAILABLE)))
    response = owner.get(f"{BASE}/files")
    assert response.status_code == 503
    assert response.json()["detail"] == remote.WORKER_UNAVAILABLE


@pytest.mark.parametrize("path", ["/files", "/files/download?path=docs/readme.md"])
def test_worker_dropping_mid_request_reports_unavailable(owner, monkeypatch, path):
    def offline(request):
        raise httpx.ConnectError("offline", request=request)
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **kw: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(offline), **kw),
    )
    link = SimpleNamespace(base_url="https://peer", token="device-only", authenticate=AsyncMock())
    monkeypatch.setattr(browser_panel, "peer_link", AsyncMock(return_value=(object(), link)))
    monkeypatch.setattr(files, "peer_link", AsyncMock(return_value=(object(), link)))
    response = owner.get(BASE + path)
    assert response.status_code == 503
    assert response.json()["detail"] == remote.WORKER_UNAVAILABLE
