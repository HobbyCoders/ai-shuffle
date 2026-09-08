"""The Android app's bundled UI calls a worker cross-origin with a Bearer paired session.

The page lives on https://appassets.androidplatform.net (WebViewAssetLoader) and
sends `Authorization: Bearer shc_...` with no cookie. Every require_auth route,
the CSRF middleware and CORS must accept exactly that.
"""
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.api import auth
from app.core.config import settings
from app.core.cors import configured_origins
from app.middleware.csrf import CSRFProtectionMiddleware, _get_allowed_origins
from tests.api.test_managed_owner_admin import owner_app, bind_and_enroll, web_session  # noqa: F401

APP_ORIGIN = "https://appassets.androidplatform.net"


def _mobile_app(owner_app):
    """owner_app plus the production CORS/CSRF middleware (non-wildcard branch)."""
    cors_origins = configured_origins("https://shuffle.example")
    assert cors_origins == ["https://shuffle.example", APP_ORIGIN]
    owner_app.add_middleware(CSRFProtectionMiddleware)
    owner_app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=True,
                             allow_methods=["*"], allow_headers=["*"])
    return owner_app


def test_mobile_app_origin_is_always_in_the_cors_and_csrf_allow_lists(monkeypatch):
    monkeypatch.setattr(settings, "cors_origins", "https://shuffle.example")
    assert configured_origins() == ["https://shuffle.example", APP_ORIGIN]
    assert APP_ORIGIN in _get_allowed_origins()
    assert settings.mobile_app_origin == APP_ORIGIN
    # Already listed: not duplicated. Wildcard: left alone (it admits the app anyway).
    assert configured_origins(f"https://a.example, {APP_ORIGIN}") == ["https://a.example", APP_ORIGIN]
    assert configured_origins("*") == ["*"]


def test_bearer_paired_session_from_the_app_origin_passes_auth_csrf_and_cors(owner_app, monkeypatch):
    bind_and_enroll(monkeypatch)
    app = _mobile_app(owner_app)
    _, token = web_session(app)
    assert token.startswith("shc_")

    phone = TestClient(app, headers={"Origin": APP_ORIGIN, "Authorization": "Bearer " + token})
    assert not phone.cookies

    # Safe request: authenticated as the paired client, CORS echoes the app origin.
    principal = phone.get("/principal")
    assert principal.status_code == 200, principal.text
    assert principal.json()["principal"].startswith("paired_client:")
    assert principal.headers["access-control-allow-origin"] == APP_ORIGIN
    assert principal.headers["access-control-allow-credentials"] == "true"

    # State-changing request: Bearer auth is CSRF-exempt even though the Origin
    # is not the worker's own host.
    changed = phone.put("/api/v1/settings/voice", json={"voice_max_call_minutes": 41})
    assert changed.status_code == 200, changed.text
    assert phone.get("/api/v1/settings/voice").json()["voice_max_call_minutes"] == 41

    # Preflight for the same call succeeds.
    preflight = phone.options("/api/v1/settings/voice", headers={
        "Access-Control-Request-Method": "PUT",
        "Access-Control-Request-Headers": "authorization,content-type",
    })
    assert preflight.status_code == 200, preflight.text
    assert preflight.headers["access-control-allow-origin"] == APP_ORIGIN
    assert "authorization" in preflight.headers["access-control-allow-headers"].lower()


def test_without_the_bearer_the_app_origin_gets_401(owner_app, monkeypatch):
    bind_and_enroll(monkeypatch)
    app = _mobile_app(owner_app)
    anonymous = TestClient(app, headers={"Origin": APP_ORIGIN})
    assert anonymous.get("/principal").status_code == 401


def test_revoked_bearer_is_rejected(owner_app, monkeypatch):
    bind_and_enroll(monkeypatch)
    app = _mobile_app(owner_app)
    _, token = web_session(app)
    client = auth.get_paired_client_from_token(token)
    assert client is not None
    from app.db import pairing as store
    assert store.revoke_paired_client(client["id"])
    phone = TestClient(app, headers={"Origin": APP_ORIGIN, "Authorization": "Bearer " + token})
    assert phone.get("/principal").status_code == 401
