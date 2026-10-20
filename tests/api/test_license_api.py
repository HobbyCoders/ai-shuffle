"""The legacy status URL stays read-only; key/lease mutation routes are gone."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api import license as api
from app.api.auth import require_auth


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_auth] = lambda: "user"
    monkeypatch.setattr(api.licensing, "status", lambda: {"source": "account", "state": "not_linked", "allowed": True})
    with TestClient(app) as client:
        yield client


def test_status_is_readable_by_any_signed_in_user(client):
    assert client.get("/api/v1/license").json() == {"source": "account", "state": "not_linked", "allowed": True}


@pytest.mark.parametrize("suffix", ["activate", "release", "deactivate", "refresh"])
def test_removed_routes_return_404(client, suffix):
    assert client.post("/api/v1/license/" + suffix, json={"key": "obsolete"}).status_code == 404
