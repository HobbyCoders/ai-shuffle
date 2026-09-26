"""Origin validation in ``CSRFProtectionMiddleware`` with CSRF actually on.

The existing CSRF tests cover the exempt paths; these cover the decision
itself: which Origin/Referer values reach a state-changing handler, what the
reverse-proxy ``X-Forwarded-*`` headers may and may not vouch for, and what
happens when there is no Origin at all.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.middleware.csrf as csrf_mod
from app.middleware.csrf import CSRFProtectionMiddleware, _extract_origin

ALLOWED = "https://app.example.com"


def _client(monkeypatch, origins=ALLOWED, client_host="203.0.113.9"):
    from app.core import owner_auth
    monkeypatch.setattr(csrf_mod.settings, "cors_origins", origins)
    monkeypatch.setattr(owner_auth, "local_access", lambda request: False)
    api = FastAPI()
    api.add_middleware(CSRFProtectionMiddleware)

    @api.post("/api/v1/things")
    async def create():
        return {"ok": True}

    @api.get("/api/v1/things")
    async def read():
        return {"ok": True}

    return TestClient(api, base_url="http://worker.internal:8000", client=(client_host, 50000))


@pytest.fixture
def client(monkeypatch):
    return _client(monkeypatch)


def test_safe_methods_pass_without_origin(client):
    assert client.get("/api/v1/things", headers={"origin": "https://evil.example"}).status_code == 200


def test_allowed_origin_passes(client):
    assert client.post("/api/v1/things", headers={"origin": ALLOWED}).status_code == 200


def test_foreign_origin_is_blocked_with_a_diagnostic(client):
    response = client.post("/api/v1/things", headers={"origin": "https://evil.example"})
    assert response.status_code == 403
    assert "https://evil.example not allowed" in response.json()["detail"]


def test_origin_must_match_exactly_not_by_prefix(client):
    for origin in ("https://app.example.com.evil.example", "http://app.example.com",
                   "https://app.example.com:8443"):
        assert client.post("/api/v1/things", headers={"origin": origin}).status_code == 403, origin


def test_referer_is_used_when_origin_is_absent(client):
    ok = client.post("/api/v1/things", headers={"referer": ALLOWED + "/settings?tab=1"})
    assert ok.status_code == 200
    bad = client.post("/api/v1/things", headers={"referer": "https://evil.example/page"})
    assert bad.status_code == 403


def test_relative_referer_counts_as_missing(client):
    response = client.post("/api/v1/things", headers={"referer": "/relative/path"})
    assert response.status_code == 403
    assert "missing Origin" in response.json()["detail"]


def test_missing_origin_from_remote_client_is_blocked(client):
    response = client.post("/api/v1/things")
    assert response.status_code == 403
    assert "missing Origin" in response.json()["detail"]


def test_missing_origin_from_loopback_client_is_allowed(monkeypatch):
    loopback = _client(monkeypatch, client_host="127.0.0.1")
    assert loopback.post("/api/v1/things").status_code == 200


def test_same_origin_via_host_header(client):
    # Not in CORS_ORIGINS, but it is the host the browser connected to.
    assert client.post("/api/v1/things", headers={"origin": "http://worker.internal:8000"}).status_code == 200


def test_same_origin_via_forwarded_host_and_proto(client):
    headers = {"origin": "https://shuffle.example.net", "x-forwarded-host": "shuffle.example.net",
               "x-forwarded-proto": "https"}
    assert client.post("/api/v1/things", headers=headers).status_code == 200


def test_multi_hop_forwarded_host_list(client):
    headers = {"origin": "https://edge.example.net", "x-forwarded-host": "inner.lan, edge.example.net",
               "x-forwarded-proto": "https"}
    assert client.post("/api/v1/things", headers=headers).status_code == 200


def test_forwarded_proto_must_match_origin_scheme(client):
    headers = {"origin": "http://shuffle.example.net", "x-forwarded-host": "shuffle.example.net",
               "x-forwarded-proto": "https"}
    assert client.post("/api/v1/things", headers=headers).status_code == 403


def test_bearer_requests_are_exempt(client):
    headers = {"origin": "https://evil.example", "authorization": "Bearer aih_key"}
    assert client.post("/api/v1/things", headers=headers).status_code == 200
    # "Bearer" must be the scheme, not merely present.
    headers = {"origin": "https://evil.example", "authorization": "Basic Zm9vOmJhcg=="}
    assert client.post("/api/v1/things", headers=headers).status_code == 403


def test_websocket_upgrade_header_skips_origin_check(client):
    headers = {"origin": "https://evil.example", "upgrade": "WebSocket"}
    assert client.post("/api/v1/things", headers=headers).status_code == 200


def test_connector_webhook_prefix_is_exempt(monkeypatch):
    client = _client(monkeypatch)
    api = client.app

    @api.post("/api/v1/connectors/webhook/abc")
    async def hook():
        return {"ok": True}

    assert client.post("/api/v1/connectors/webhook/abc").status_code == 200


def test_no_configured_origins_still_enforces_same_origin(monkeypatch):
    client = _client(monkeypatch, origins="not-a-url")
    assert client.post("/api/v1/things", headers={"origin": ALLOWED}).status_code == 403
    assert client.post("/api/v1/things", headers={"origin": "http://worker.internal:8000"}).status_code == 200


def test_local_owner_mutation_needs_same_origin_even_in_wildcard_mode(monkeypatch):
    """Wildcard CORS turns general CSRF off, never the local-owner boundary."""
    from app.core import owner_auth
    client = _client(monkeypatch, origins="*")
    monkeypatch.setattr(owner_auth, "local_access", lambda request: True)
    blocked = client.post("/api/v1/things", headers={"origin": "https://evil.example"})
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "Same-origin request required"
    assert client.post("/api/v1/things", headers={"origin": "http://worker.internal:8000"}).status_code == 200
    # Reads are not gated.
    assert client.get("/api/v1/things", headers={"origin": "https://evil.example"}).status_code == 200


@pytest.mark.parametrize("value,expected", [
    ("https://a.example/x?y#z", "https://a.example"),
    ("http://a.example:8080", "http://a.example:8080"),
    ("a.example", ""),
    ("/relative", ""),
    ("null", ""),
])
def test_extract_origin(value, expected):
    assert _extract_origin(value) == expected
