import hashlib
import json
from urllib.parse import parse_qs, urlsplit
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import connector_oauth as oauth
from app.api.auth import require_admin
from app.core import connector_packages as packages
from app.core.connector_service import decrypt_config
from app.db import database


@pytest.fixture
def client(monkeypatch):
    for key in ("SHUFFLE_GOOGLE_CLIENT_ID", "SHUFFLE_GOOGLE_CLIENT_SECRET", "SHUFFLE_GOOGLE_REDIRECT_URI"):
        monkeypatch.delenv(key, raising=False)
    app = FastAPI()
    app.include_router(oauth.router, prefix="/api/v1/connectors")
    app.dependency_overrides[require_admin] = lambda: "test-admin"
    oauth._pending.clear()
    from cryptography.fernet import Fernet
    monkeypatch.setattr(oauth.encryption, "_encryption_key", Fernet.generate_key())
    monkeypatch.setattr(packages, "require_installed", lambda kind: packages.package_id(kind))
    with TestClient(app, base_url="http://localhost") as client:
        yield client
    oauth._pending.clear()


def configure(client):
    result = client.put("/api/v1/connectors/oauth/google/app", json={
        "client_id": "test.apps.googleusercontent.com", "client_secret": "private-client-secret",
        "redirect_uri": "http://localhost" + oauth.CALLBACK})
    assert result.status_code == 200, result.text


def start(client, **data):
    configure(client)
    response = client.post("/api/v1/connectors/oauth/google/start", json=data)
    assert response.status_code == 200, response.text
    return response.json()


def test_admin_settings_encrypt_secret_and_status_never_returns_it(client):
    configure(client)
    raw = database.get_system_setting(oauth.SETTING)
    assert "private-client-secret" not in raw
    assert oauth.app_config()["client_secret"] == "private-client-secret"
    result = client.get("/api/v1/connectors/oauth/google/status").json()
    assert result["configured"]
    assert "client_secret" not in result


def test_redirect_cannot_be_changed_to_another_origin(client):
    result = client.put("/api/v1/connectors/oauth/google/app", json={
        "client_id": "client", "client_secret": "secret", "redirect_uri": "https://evil.invalid" + oauth.CALLBACK})
    assert result.status_code == 400


def test_start_requires_config_and_installed_package(client, monkeypatch):
    assert client.post("/api/v1/connectors/oauth/google/start", json={}).status_code == 400
    configure(client)
    def missing(_): raise ValueError("Install Gmail first")
    monkeypatch.setattr(packages, "require_installed", missing)
    assert client.post("/api/v1/connectors/oauth/google/start", json={}).status_code == 400


def test_pkce_cookie_binding_and_expiry(client):
    flow = start(client)
    query = parse_qs(urlsplit(flow["authorization_url"]).query)
    state = flow["flow_id"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"][0] != oauth._pending[state]["verifier"]
    assert query["scope"] == [oauth.SCOPE]
    cookies = client.cookies.get_dict() if hasattr(client.cookies, "get_dict") else dict(client.cookies)
    client.cookies.clear()
    assert client.get(oauth.CALLBACK, params={"state": state, "code": "code"}).status_code == 400
    assert state in oauth._pending
    client.cookies.update(cookies)
    oauth._pending[state]["expires"] = 0
    assert client.get(oauth.CALLBACK, params={"state": state, "code": "code"}).status_code == 400


def test_cancel_and_replay_do_not_create_connections(client):
    flow = start(client)
    response = client.get(oauth.CALLBACK, params={"state": flow["flow_id"], "error": "access_denied"})
    assert response.status_code == 200
    assert "cancelled" in response.text
    assert not database.get_all_connectors()
    assert client.get(oauth.CALLBACK, params={"state": flow["flow_id"], "code": "code"}).status_code == 400


def test_callback_produces_encrypted_account_and_reconnect_preserves_id(client, monkeypatch):
    credentials = {"client_id": "client", "client_secret": "secret", "refresh_token": "refresh-secret",
                   "email_address": "person@example.com", "oauth_managed": True}
    monkeypatch.setattr(oauth, "_credentials", AsyncMock(return_value=credentials))
    flow = start(client, name="My Gmail", options={"label_ids": "INBOX,STARRED", "max_results": 25})
    response = client.get(oauth.CALLBACK, params={"state": flow["flow_id"], "code": "one-time-code"})
    assert response.status_code == 200
    assert "refresh-secret" not in response.text
    rows = database.get_all_connectors()
    assert len(rows) == 1
    row = rows[0]
    assert row["name"] == "My Gmail"
    assert row["config"]["label_ids"] == "INBOX,STARRED"
    assert row["config"]["max_results"] == 25
    assert row["config"]["enabled_action_categories"] == ""
    assert "refresh-secret" not in json.dumps(row["config"])
    assert decrypt_config(row["config"], "gmail")["refresh_token"] == "refresh-secret"
    flow = start(client, connector_id=row["id"])
    client.get(oauth.CALLBACK, params={"state": flow["flow_id"], "code": "second-code"})
    assert [c["id"] for c in database.get_all_connectors()] == [row["id"]]
    credentials["email_address"] = "other@example.com"
    flow = start(client, connector_id=row["id"])
    response = client.get(oauth.CALLBACK, params={"state": flow["flow_id"], "code": "third-code"})
    assert "same Google account" in response.text
    assert decrypt_config(database.get_connector(row["id"])["config"], "gmail")["email_address"] == "person@example.com"

@pytest.mark.asyncio
async def test_token_exchange_sends_pkce_and_requires_offline_scope(monkeypatch):
    import httpx
    real_client = httpx.AsyncClient
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path == "/token":
            return httpx.Response(200, json={"access_token": "temporary", "refresh_token": "offline", "scope": oauth.SCOPE})
        assert request.headers["Authorization"] == "Bearer temporary"
        return httpx.Response(200, json={"emailAddress": "person@example.com"})
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(oauth.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))
    pending = {"app": {"client_id": "client", "client_secret": "secret", "redirect_uri": "http://localhost" + oauth.CALLBACK}, "verifier": "pkce-verifier"}
    result = await oauth._credentials(pending, "authorization-code")
    body = parse_qs(requests[0].content.decode())
    assert body["code_verifier"] == ["pkce-verifier"]
    assert body["code"] == ["authorization-code"]
    assert result["refresh_token"] == "offline"
    assert result["email_address"] == "person@example.com"
    transport.handler = lambda _: httpx.Response(200, json={"access_token": "temporary", "scope": "openid"})
    with pytest.raises(ValueError, match="permission"):
        await oauth._credentials(pending, "code")
    transport.handler = lambda _: httpx.Response(200, json={"access_token": "temporary", "scope": oauth.SCOPE})
    with pytest.raises(ValueError, match="offline"):
        await oauth._credentials(pending, "code")

