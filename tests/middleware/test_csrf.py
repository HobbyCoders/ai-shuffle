"""Tests for CSRFProtectionMiddleware path/prefix exemptions.

These build a minimal Starlette app wrapping only the CSRF middleware so the
behaviour can be asserted without a database or the full app. CSRF is forced
*active* (non-wildcard CORS) so the exemptions are actually exercised — the
production failure mode where wildcard CORS silently disables the check.
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.middleware.csrf as csrf_mod
from app.middleware.csrf import CSRFProtectionMiddleware


@pytest.fixture
def client(monkeypatch):
    # Force CSRF on: a concrete allowed origin, no wildcard.
    monkeypatch.setattr(csrf_mod.settings, "cors_origins", "https://app.example.com")

    api = FastAPI()
    api.add_middleware(CSRFProtectionMiddleware)

    @api.post("/api/v1/mobile/register")
    async def register():  # noqa: D401 - test stub
        return {"ok": True}

    @api.post("/api/v1/mobile/capabilities")
    async def capabilities():  # a non-exempt, non-Bearer mobile POST
        return {"ok": True}

    @api.post("/api/v1/pairing/exchange")
    async def pairing_exchange():
        return {"ok": True}

    return TestClient(api)


@pytest.mark.parametrize("path", ["/api/v1/network/peer/auth/challenge", "/api/v1/network/peer/auth/token"])
@pytest.mark.parametrize("local", [False, True])
def test_peer_bootstrap_has_no_ambient_browser_authority(monkeypatch, path, local):
    from app.core import owner_auth
    monkeypatch.setattr(csrf_mod.settings, "cors_origins", "https://app.example.com")
    monkeypatch.setattr(owner_auth, "local_access", lambda request: local)
    api = FastAPI()
    api.add_middleware(CSRFProtectionMiddleware)

    @api.post(path)
    async def peer_auth():
        return {"signed_challenge_required": True}

    @api.post("/api/v1/network/create")
    async def owner_action():
        return {"ok": True}

    client = TestClient(api)
    assert client.post(path, json={}).json() == {"signed_challenge_required": True}
    assert client.post("/api/v1/network/create", json={}).status_code == 403


def test_mobile_register_exempt_without_origin(client):
    """Native registration has no Origin header and must NOT be CSRF-blocked."""
    resp = client.post("/api/v1/mobile/register", json={"device_info": {}})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}


def test_non_exempt_mobile_post_without_origin_is_blocked(client):
    """A non-exempt, tokenless, originless POST is still rejected (control)."""
    resp = client.post("/api/v1/mobile/capabilities", json={})
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]


def test_pairing_exchange_exempt_before_session_exists(client):
    resp = client.post("/api/v1/pairing/exchange", json={"code": "example"})
    assert resp.status_code == 200, resp.text


def test_bearer_authenticated_request_is_exempt(client):
    """Bearer-token (mobile aim_ key) requests are CSRF-safe regardless of path."""
    resp = client.post(
        "/api/v1/mobile/capabilities",
        json={},
        headers={"Authorization": "Bearer aim_example"},
    )
    assert resp.status_code == 200, resp.text
