"""The phone apps' bundled UI calls a worker cross-origin with a Bearer paired session.

The Android page lives on https://appassets.androidplatform.net (WebViewAssetLoader),
the iOS page on aishuffle://app (WKURLSchemeHandler). Both send
`Authorization: Bearer shc_...` with no cookie. Every require_auth route, the
CSRF middleware and CORS must accept exactly that, from those two origins only.
"""
import pytest
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from app.api import auth
from app.core.config import settings
from app.core.cors import configured_origins
from app.middleware.csrf import CSRFProtectionMiddleware, _get_allowed_origins
from tests.api.test_managed_owner_admin import owner_app, bind_and_enroll, web_session  # noqa: F401

APP_ORIGIN = "https://appassets.androidplatform.net"
IOS_ORIGIN = "aishuffle://app"
APP_ORIGINS = pytest.mark.parametrize("origin", [APP_ORIGIN, IOS_ORIGIN])


def _mobile_app(owner_app):
    """owner_app plus the production CORS/CSRF middleware (non-wildcard branch)."""
    cors_origins = configured_origins("https://shuffle.example")
    assert cors_origins == ["https://shuffle.example", APP_ORIGIN, IOS_ORIGIN]
    owner_app.add_middleware(CSRFProtectionMiddleware)
    owner_app.add_middleware(CORSMiddleware, allow_origins=cors_origins, allow_credentials=True,
                             allow_methods=["*"], allow_headers=["*"])
    return owner_app


def test_mobile_app_origins_are_always_in_the_cors_and_csrf_allow_lists(monkeypatch):
    monkeypatch.setattr(settings, "cors_origins", "https://shuffle.example")
    assert configured_origins() == ["https://shuffle.example", APP_ORIGIN, IOS_ORIGIN]
    assert {APP_ORIGIN, IOS_ORIGIN} <= _get_allowed_origins()
    assert settings.mobile_app_origin == APP_ORIGIN
    assert settings.mobile_ios_app_origin == IOS_ORIGIN
    # Already listed: not duplicated. Wildcard: left alone (it admits the apps anyway).
    assert configured_origins(f"https://a.example, {IOS_ORIGIN}, {APP_ORIGIN}") == [
        "https://a.example", IOS_ORIGIN, APP_ORIGIN]
    assert configured_origins("*") == ["*"]
    # An emptied setting drops only that app.
    assert configured_origins("https://a.example", ios_app_origin="") == ["https://a.example", APP_ORIGIN]


def test_only_the_exact_ios_origin_is_admitted(owner_app, monkeypatch):
    bind_and_enroll(monkeypatch)
    app = _mobile_app(owner_app)
    _, token = web_session(app)
    for origin in ("aishuffle://app.evil", "aishuffle://evil", "aishuffle://app:8443", "evil://app"):
        client = TestClient(app, headers={"Origin": origin, "Authorization": "Bearer " + token})
        response = client.get("/principal")
        assert "access-control-allow-origin" not in response.headers, origin
        preflight = client.options("/api/v1/settings/voice", headers={
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "authorization,content-type",
        })
        assert preflight.status_code == 400, origin
        assert "access-control-allow-origin" not in preflight.headers, origin


@APP_ORIGINS
def test_bearer_paired_session_from_the_app_origin_passes_auth_csrf_and_cors(owner_app, monkeypatch, origin):
    bind_and_enroll(monkeypatch)
    app = _mobile_app(owner_app)
    _, token = web_session(app)
    assert token.startswith("shc_")

    phone = TestClient(app, headers={"Origin": origin, "Authorization": "Bearer " + token})
    assert not phone.cookies

    # Safe request: authenticated as the paired client, CORS echoes the app origin.
    principal = phone.get("/principal")
    assert principal.status_code == 200, principal.text
    assert principal.json()["principal"].startswith("paired_client:")
    assert principal.headers["access-control-allow-origin"] == origin
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
    assert preflight.headers["access-control-allow-origin"] == origin
    assert "authorization" in preflight.headers["access-control-allow-headers"].lower()


@APP_ORIGINS
def test_without_the_bearer_the_app_origin_gets_401(owner_app, monkeypatch, origin):
    bind_and_enroll(monkeypatch)
    app = _mobile_app(owner_app)
    anonymous = TestClient(app, headers={"Origin": origin})
    assert anonymous.get("/principal").status_code == 401


@APP_ORIGINS
def test_revoked_bearer_is_rejected(owner_app, monkeypatch, origin):
    bind_and_enroll(monkeypatch)
    app = _mobile_app(owner_app)
    _, token = web_session(app)
    client = auth.get_paired_client_from_token(token)
    assert client is not None
    from app.db import pairing as store
    assert store.revoke_paired_client(client["id"])
    phone = TestClient(app, headers={"Origin": origin, "Authorization": "Bearer " + token})
    assert phone.get("/principal").status_code == 401
