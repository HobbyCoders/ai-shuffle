"""/api/v1/license: status for any signed-in user, changes for the owner only."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import license as api
from app.api.auth import require_admin, require_auth


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_auth] = lambda: "user"
    app.dependency_overrides[require_admin] = lambda: "owner"
    monkeypatch.setattr(api.preview_access, "grant", lambda request: None)
    calls = []

    async def activate(key):
        calls.append(("activate", key))
        if key == "full":
            raise api.licensing.LicenseError(409, "All 3 workers on this license are in use.", code="seats_full", seats=3, installs=[{"install_id": "x"}])
        if key == "unknown":
            raise api.licensing.LicenseError(404, "License key not recognised")
        return {"state": "paid", "allowed": True}

    async def release_seat(key, install_id):
        calls.append(("release", key, install_id))
        return {"ok": True, "seats_used": 2}

    async def deactivate():
        calls.append(("deactivate",))
        return {"state": "not_linked", "seat_released": True}

    async def refresh():
        calls.append(("refresh",))
        return {"state": "paid"}

    monkeypatch.setattr(api.licensing, "activate", activate)
    monkeypatch.setattr(api.licensing, "deactivate", deactivate)
    monkeypatch.setattr(api.licensing, "release_seat", release_seat)
    monkeypatch.setattr(api.licensing, "refresh", refresh)
    monkeypatch.setattr(api.licensing, "status", lambda: {"mode": "cloud", "state": "not_linked", "allowed": True})
    with TestClient(app) as test_client:
        yield test_client, app, calls


def test_status_is_readable_by_any_signed_in_user(client):
    c, app, _ = client
    app.dependency_overrides.pop(require_admin)
    assert c.get("/api/v1/license").json() == {"mode": "cloud", "state": "not_linked", "allowed": True}


def test_owner_activates_and_errors_keep_their_status_and_details(client):
    c, _, calls = client
    ok = c.post("/api/v1/license/activate", json={"key": "ASH-AAAAA-AAAAA-AAAAA-AAAAA"})
    assert ok.status_code == 200 and ok.json() == {"activated": True, "state": "paid", "allowed": True}
    # Seats full is an answer the card acts on, so it comes back as data.
    full = c.post("/api/v1/license/activate", json={"key": "full"})
    assert full.status_code == 200
    assert full.json() == {"activated": False, "detail": "All 3 workers on this license are in use.", "code": "seats_full", "seats": 3, "installs": [{"install_id": "x"}]}
    unknown = c.post("/api/v1/license/activate", json={"key": "unknown"})
    assert unknown.status_code == 404 and unknown.json() == {"detail": "License key not recognised"}
    released = c.post("/api/v1/license/release", json={"key": "full", "install_id": "install-x1"})
    assert released.json() == {"ok": True, "seats_used": 2}
    assert c.post("/api/v1/license/release", json={"key": "full", "install_id": "x"}).status_code == 422
    assert c.post("/api/v1/license/activate", json={"key": ""}).status_code == 422
    assert c.post("/api/v1/license/activate", json={"key": "k", "extra": 1}).status_code == 422
    assert c.post("/api/v1/license/deactivate").json()["seat_released"] is True
    assert c.post("/api/v1/license/refresh").json() == {"state": "paid"}
    assert calls[0] == ("activate", "ASH-AAAAA-AAAAA-AAAAA-AAAAA")


def test_only_the_owner_changes_the_license(client):
    c, app, calls = client
    app.dependency_overrides.pop(require_admin)
    for path in ("/api/v1/license/activate", "/api/v1/license/deactivate", "/api/v1/license/refresh", "/api/v1/license/release"):
        assert c.post(path, json={"key": "ASH-AAAAA-AAAAA-AAAAA-AAAAA", "install_id": "install-x1"}).status_code in (401, 403)
    assert calls == []


def test_agent_previews_cannot_change_the_license(client, monkeypatch):
    c, _, calls = client
    monkeypatch.setattr(api.preview_access, "grant", lambda request: object())
    assert c.post("/api/v1/license/deactivate").status_code == 403
    assert calls == []
