"""The preview cookie and owner-only project management.

On an installed worker the agent-browser preview cookie is read-only on owner
(``require_admin``) routes: it can neither add a project nor set one up on
another worker. On a launcher-owned throwaway instance (the dev loop, a branch
preview) that cookie is the only way in, so there it may manage projects, and
nothing else owner-only; the mutation ceiling and the same-origin rule still
apply.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api import auth
from app.core import owner_auth, preview_access
from app.core.config import settings

SESSION = "sess-preview-projects"
HOST = "10.0.0.30:18090"
ORIGIN = f"http://{HOST}"


class Surface:
    """Test-only trusted listener boundary. HTTP headers cannot set this mark."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope["shuffle.ingress"] = "local"
        await self.app(scope, receive, send)


@pytest.fixture
def data(tmp_path, monkeypatch, mock_db):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(settings, "data_dir", data_dir)
    monkeypatch.setattr(settings, "cookie_secure", False)
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: False)
    monkeypatch.setattr(owner_auth.settings, "shuffle_local_access", True)
    monkeypatch.setattr(owner_auth, "trusted_lan", lambda: False)
    monkeypatch.delenv("AI_SHUFFLE_PREVIEW", raising=False)
    return data_dir


def _disposable(data_dir, monkeypatch):
    """Mark the data directory the way the preview launcher does."""
    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", "1")
    (data_dir.parent / ".branch-preview").write_text(str(data_dir.resolve()))


@pytest.fixture
def client(data):
    api = FastAPI()

    for path in ("/api/v1/projects", "/api/v1/projects/setup", "/api/v1/projects/setup/op-1/cancel",
                 "/api/v1/automations", "/api/v1/projects/demo/git/worktrees/wt-1/forget",
                 "/api/v1/projects/demo", "/api/v1/projects/demo/link"):
        api.add_api_route(path, lambda principal=Depends(auth.require_admin): {"principal": principal},
                          methods=["POST", "GET", "PUT", "DELETE"])
    api.add_api_route("/api/v1/projects/workers/peer-1",
                      lambda principal=Depends(auth.require_admin): {"principal": principal},
                      methods=["DELETE"])
    test_client = TestClient(Surface(api), base_url=ORIGIN, client=("10.0.0.20", 54321),
                             headers={"X-Forwarded-Host": "localhost:5173"})
    test_client.cookies.set(preview_access.cookie_name(), preview_access.mint(SESSION))
    return test_client


OWNER_PROJECT_CHANGES = [
    ("POST", "/api/v1/projects"),
    ("POST", "/api/v1/projects/setup"),
    ("POST", "/api/v1/projects/setup/op-1/cancel"),
    ("DELETE", "/api/v1/projects/workers/peer-1"),
]


@pytest.mark.parametrize("method,path", OWNER_PROJECT_CHANGES)
def test_an_installed_workers_preview_cookie_cannot_change_projects(client, method, path):
    # Creating a project and setting one up are refused alike: the cookie reads only.
    assert client.request(method, path, headers={"Origin": ORIGIN}).status_code == 403
    assert client.get("/api/v1/projects").status_code == 200


@pytest.mark.parametrize("method,path", OWNER_PROJECT_CHANGES)
def test_a_throwaway_instances_preview_cookie_manages_projects(client, data, monkeypatch, method, path):
    _disposable(data, monkeypatch)
    allowed = client.request(method, path, headers={"Origin": ORIGIN})
    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == {"principal": "preview:" + SESSION}
    # Same-origin still applies to every change.
    assert client.request(method, path, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.request(method, path).status_code == 403


def test_a_throwaway_instance_still_refuses_other_owner_changes(client, data, monkeypatch):
    _disposable(data, monkeypatch)
    assert client.post("/api/v1/automations", headers={"Origin": ORIGIN}).status_code == 403
    assert client.get("/api/v1/automations").status_code == 200
    # The preview mutation ceiling holds under /projects too.
    forget = client.post("/api/v1/projects/demo/git/worktrees/wt-1/forget", headers={"Origin": ORIGIN})
    assert forget.status_code == 403
    # Only add / set up / cancel / remove-a-worker's-copy: deleting, editing or
    # relinking a project stays owner-only even on a throwaway instance.
    for method, path in (("DELETE", "/api/v1/projects/demo"), ("PUT", "/api/v1/projects/demo"),
                         ("POST", "/api/v1/projects/demo/link"), ("DELETE", "/api/v1/projects/setup")):
        assert client.request(method, path, headers={"Origin": ORIGIN}).status_code == 403, (method, path)


def test_a_stray_preview_variable_without_the_launcher_marker_is_a_real_install(client, monkeypatch):
    monkeypatch.setenv("AI_SHUFFLE_PREVIEW", "1")
    assert client.post("/api/v1/projects", headers={"Origin": ORIGIN}).status_code == 403
