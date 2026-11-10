"""Admin-only lifecycle API; the payload is shared by reads and actions."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api import auth, settings as api
from app.core import application_updates, claude_cli_install as ci, worker_settings

BASE = "/api/v1/settings/claude-cli"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


@pytest.mark.parametrize("method,suffix,body", [
    ("get", "", None), ("put", "", {"keep_installed": True}),
    ("post", "/install", None), ("post", "/update", None), ("post", "/remove", None),
])
def test_owner_required(client, monkeypatch, method, suffix, body):
    monkeypatch.setattr(auth, "get_paired_client_from_token", lambda *a, **kw: {"account_owner_admin": False})
    response = client.request(method, BASE + suffix, json=body, headers={"Authorization": "Bearer ordinary-user"})
    assert response.status_code == 403 and response.json()["detail"] == "Owner access required"


def _payload():
    return {"installed": False, "version": None, "path": None, "keep_installed": False,
            "installed_by_app": False, "can_install": True, "can_remove": False,
            "platform": "desktop", "job": None}


def test_get_put_and_actions_share_contract(client, monkeypatch):
    client.app.dependency_overrides[auth.require_admin] = lambda: "owner"
    monkeypatch.setattr(application_updates, "all_workers", lambda: False)
    payload = {**_payload(), "update_all_workers": False}
    monkeypatch.setattr(ci, "payload", lambda: dict(payload))
    monkeypatch.setattr(ci, "set_keep_installed", lambda value: payload.update(keep_installed=value))
    def start(kind):
        payload["job"] = {"kind": kind, "state": "running", "started_at": "2026-10-05T12:00:00+00:00",
                          "finished_at": None, "message": None, "output_tail": None}
        return dict(payload)
    monkeypatch.setattr(ci, "start", start)
    assert client.get(BASE).json() == payload
    response = client.put(BASE, json={"keep_installed": True})
    assert response.status_code == 200 and response.json()["keep_installed"]
    assert client.put(BASE, json={"keep_installed": "yes"}).status_code == 422
    for kind in ("install", "update", "remove"):
        response = client.post(BASE + "/" + kind)
        assert response.status_code == 200 and response.json() == payload
        assert client.get(BASE).json() == response.json()


@pytest.mark.parametrize("all_workers", [False, True])
def test_update_reaches_other_workers_only_when_update_all_is_on(client, monkeypatch, all_workers):
    client.app.dependency_overrides[auth.require_admin] = lambda: "owner"
    monkeypatch.setattr(application_updates, "all_workers", lambda: all_workers)
    started = []
    monkeypatch.setattr(ci, "start", lambda kind: started.append(kind) or _payload())
    monkeypatch.setattr(ci, "payload", _payload)
    peers = [{"device_id": "peer", "device_name": "Desktop", "outcome": "accepted", "reason": "done", "status": None}]
    asked = []
    async def fanout():
        asked.append(list(started))  # this worker's own job starts first
        return peers
    monkeypatch.setattr(worker_settings, "update_claude_on_peers", fanout)
    body = client.post(BASE + "/update").json()
    assert started == ["update"] and body["update_all_workers"] is all_workers
    if all_workers:
        assert asked == [["update"]] and body["workers"] == peers
    else:
        assert asked == [] and "workers" not in body
    # Installing or removing stays on the connected worker.
    for kind in ("install", "remove"):
        assert "workers" not in client.post(BASE + "/" + kind).json()
    assert len(asked) == (1 if all_workers else 0)


def test_update_still_starts_here_when_other_workers_cannot_be_asked(client, monkeypatch):
    client.app.dependency_overrides[auth.require_admin] = lambda: "owner"
    monkeypatch.setattr(application_updates, "all_workers", lambda: True)
    monkeypatch.setattr(ci, "start", lambda kind: _payload())
    async def fanout():
        raise RuntimeError("directory unavailable")
    monkeypatch.setattr(worker_settings, "update_claude_on_peers", fanout)
    response = client.post(BASE + "/update")
    assert response.status_code == 200 and response.json()["workers"] is None


def test_remove_refusal(client, monkeypatch):
    client.app.dependency_overrides[auth.require_admin] = lambda: "owner"
    def refuse(kind):
        raise ValueError("This Claude Code install was not installed by AI Shuffle.")
    monkeypatch.setattr(ci, "start", refuse)
    response = client.post(BASE + "/remove")
    assert response.status_code == 400 and "not installed by AI Shuffle" in response.json()["detail"]


# --- Through the real app and the real require_admin (review of #1865) -------

from unittest.mock import Mock

from app.api.auth import hash_api_key
from app.core import preview_access
from app.db import database

ROUTES = [("get", ""), ("put", ""), ("post", "/install"), ("post", "/update"), ("post", "/remove")]


@pytest.fixture
def guarded(monkeypatch):
    start, keep = Mock(), Mock()
    monkeypatch.setattr(ci, "start", start)
    monkeypatch.setattr(ci, "set_keep_installed", keep)
    monkeypatch.setattr(ci, "payload", _payload)
    return start, keep


def _call(c, method, suffix, **kw):
    body = {"keep_installed": False} if method == "put" else None
    return c.request(method, "/api/v1" + BASE.removeprefix("/api/v1") + suffix, json=body, **kw)


@pytest.mark.parametrize("method,suffix", ROUTES)
def test_api_user_and_anonymous_are_refused(client, mock_db, guarded, method, suffix):
    key = "aih_cli_user_key"
    database.create_api_user("cli-user", "CLI User", hash_api_key(key))
    assert _call(client, method, suffix, headers={"Authorization": f"Bearer {key}"}).status_code == 403
    assert _call(client, method, suffix).status_code in (401, 403)
    assert not guarded[0].called and not guarded[1].called


@pytest.mark.parametrize("method,suffix", [r for r in ROUTES if r[0] != "get"])
def test_agent_preview_cannot_change_or_act(authenticated_client, guarded, monkeypatch, method, suffix):
    # Even alongside owner authentication, a live preview cookie refuses changes.
    from app.api import auth as auth_api
    authenticated_client.app.dependency_overrides.pop(auth_api.require_admin, None)
    monkeypatch.setattr(preview_access, "verify", lambda token: object())
    authenticated_client.cookies.set(preview_access.cookie_name(), "preview-token")
    response = _call(authenticated_client, method, suffix)
    assert response.status_code == 403
    assert response.json()["detail"].startswith("Agent previews cannot change")
    assert not guarded[0].called and not guarded[1].called
