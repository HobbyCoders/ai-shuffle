import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import connector_plaid as oauth
from app.api.auth import require_admin
from app.core import connector_packages as packages
from app.core.connector_service import decrypt_config, mask_config
from app.core.connectors.plaid import PlaidConnector, plaid_request
from app.core.connectors.gmail import GmailConnector
from app.db import database


@pytest.fixture
def client(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setattr(oauth.encryption, "_encryption_key", Fernet.generate_key())
    monkeypatch.setattr(packages, "require_installed", packages.package_id)
    app = FastAPI()
    app.include_router(oauth.router, prefix="/api/v1/connectors")
    app.dependency_overrides[require_admin] = lambda: "admin"
    oauth._pending.clear()
    with TestClient(app, base_url="https://localhost") as c:
        yield c
    oauth._pending.clear()


def configure(client):
    return client.put("/api/v1/connectors/oauth/plaid/app", json={
        "client_id": "test-id", "client_secret": "private-secret", "environment": "sandbox",
        "country_codes": ["US"], "redirect_uri": "https://localhost/plaid-link"})


def test_settings_are_encrypted_and_redirect_is_same_origin(client):
    assert configure(client).status_code == 200
    assert "private-secret" not in database.get_system_setting(oauth.SETTING)
    assert oauth.app_config()["client_secret"] == "private-secret"
    assert "client_secret" not in client.get("/api/v1/connectors/oauth/plaid/status").json()
    for uri in ["https://evil.invalid/plaid-link", "https://localhost/other", "http://localhost/plaid-link"]:
        with pytest.raises(ValueError): oauth.validate_redirect(uri, "https://localhost")


def test_start_requires_config_and_only_requests_transactions(client, monkeypatch):
    assert client.post("/api/v1/connectors/oauth/plaid/start").status_code == 400
    configure(client)
    request = AsyncMock(return_value={"link_token": "link-sandbox-token"})
    monkeypatch.setattr(oauth, "plaid_request", request)
    result = client.post("/api/v1/connectors/oauth/plaid/start")
    assert result.status_code == 200
    assert request.call_args.kwargs["products"] == ["transactions"]
    assert "access_token" not in result.text
    assert "HttpOnly" in result.headers["set-cookie"]
    assert "Secure" in result.headers["set-cookie"]
    flow = result.json()["flow_id"]
    client.cookies.clear()
    assert client.post("/api/v1/connectors/oauth/plaid/exchange", json={"flow_id": flow, "public_token": "pub"}).status_code == 400
    assert flow in oauth._pending


@pytest.mark.asyncio
async def test_exchange_produces_encrypted_account_readable_by_agent(client, monkeypatch):
    configure(client)
    account = {"account_id": "account-1", "name": "Credit card", "mask": "1234", "type": "credit", "balances": {"current": 150}}
    async def request(config, path, **payload):
        if path == "/link/token/create": return {"link_token": "link"}
        if path == "/item/public_token/exchange": return {"access_token": "private-access", "item_id": "item-1"}
        assert path == "/accounts/get"
        assert payload["access_token"] == "private-access"
        return {"accounts": [account]}
    monkeypatch.setattr(oauth, "plaid_request", request)
    start = client.post("/api/v1/connectors/oauth/plaid/start").json()
    payload = {"flow_id": start["flow_id"], "public_token": "public"}
    result = client.post("/api/v1/connectors/oauth/plaid/exchange", json=payload)
    assert result.status_code == 200, result.text
    assert "private-access" not in result.text
    row = database.get_connector(result.json()["connector_id"])
    assert row["type"] == "plaid" and row["events"] == []
    assert "private-access" not in json.dumps(row["config"])
    saved = decrypt_config(row["config"], "plaid")
    assert saved["access_token"] == "private-access"
    assert "private-access" not in json.dumps(mask_config(saved, "plaid"))
    monkeypatch.setattr("app.core.connectors.plaid.plaid_request", request)
    conn = PlaidConnector(row["id"], "plaid", saved, AsyncMock(), AsyncMock())
    assert await conn.connect()
    output = await conn.execute_action("accounts", {})
    assert output["accounts"][0]["mask"] == "1234"
    assert output["environment"] == "sandbox"
    assert "access_token" not in json.dumps(output)
    await conn.disconnect()
    assert client.post("/api/v1/connectors/oauth/plaid/exchange", json=payload).status_code == 400


def test_expired_flow_and_disabled_plugin(client, monkeypatch):
    configure(client)
    monkeypatch.setattr(oauth, "plaid_request", AsyncMock(return_value={"link_token": "link"}))
    flow = client.post("/api/v1/connectors/oauth/plaid/start").json()["flow_id"]
    oauth._pending[flow]["expires"] = 0
    assert client.post("/api/v1/connectors/oauth/plaid/exchange", json={"flow_id": flow, "public_token": "pub"}).status_code == 400
    def missing(_): raise ValueError("Install Plaid")
    monkeypatch.setattr(packages, "require_installed", missing)
    assert client.post("/api/v1/connectors/oauth/plaid/start").status_code == 400


@pytest.mark.asyncio
async def test_reader_rejects_mutations_and_projects_paginated_transactions():
    conn = PlaidConnector("c", "plaid", {"environment": "sandbox"}, AsyncMock(), AsyncMock())
    conn._read = AsyncMock(return_value={"transactions": [{"name": "Coffee", "amount": 4.5, "pending": True, "secret_extra": "hidden"}], "total_transactions": 125, "access_token": "hidden"})
    assert not conn.CAPABILITIES.is_messaging
    assert all(action.risk == "safe" for action in conn.ACTIONS)
    with pytest.raises(ValueError): await conn.execute_action("transfer", {"amount": 1})
    conn._read.assert_not_awaited()
    output = await conn.execute_action("transactions", {"start_date": "2026-01-01", "end_date": "2026-01-31", "count": 25, "offset": 100})
    assert output["total_transactions"] == 125 and output["offset"] == 100
    assert output["transactions"][0]["pending"]
    assert "hidden" not in json.dumps(output)
    assert conn._read.call_args.kwargs["options"] == {"count": 25, "offset": 100}
    with pytest.raises(ValueError): await conn.execute_action("transactions", {"start_date": "2026-02-01", "end_date": "2026-01-01"})


@pytest.mark.asyncio
async def test_api_errors_do_not_expose_credentials(monkeypatch):
    real = httpx.AsyncClient
    def handle(request):
        assert request.url.host == "sandbox.plaid.com"
        assert json.loads(request.content)["secret"] == "private-secret"
        return httpx.Response(400, json={"error_code": "ITEM_LOGIN_REQUIRED", "error_message": "private-secret"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handle), **kw))
    with pytest.raises(ValueError, match="authorization expired") as exc:
        await plaid_request({"environment": "sandbox", "client_id": "id", "client_secret": "private-secret"}, "/accounts/get", access_token="private-access")
    assert "private-secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_gmail_is_tool_connection_not_an_incoming_chat_bot():
    incoming, trigger = AsyncMock(), AsyncMock()
    conn = GmailConnector("g", "gmail", {}, incoming, AsyncMock(), trigger)
    conn._build_credentials = MagicMock()
    service = MagicMock()
    conn._build_service = MagicMock(return_value=service)
    conn._seed_seen_messages = AsyncMock()
    await conn._do_connect()
    conn._seed_seen_messages.assert_not_awaited()
    assert conn._poll_task is None
    service.users.return_value.messages.return_value.get.return_value.execute.return_value = {
        "payload": {"headers": [{"name": "From", "value": "sender@example.com"}]}, "snippet": "Untrusted email"}
    await conn._process_message("message-1")
    incoming.assert_not_awaited()
    trigger.assert_awaited_once()
    await conn.disconnect()

@pytest.mark.asyncio
async def test_gmail_explicit_watch_only_emits_automation_triggers():
    conn = GmailConnector("g", "gmail", {"watch_inbox": True}, AsyncMock(), AsyncMock())
    conn._build_credentials = MagicMock()
    conn._build_service = MagicMock()
    conn._seed_seen_messages = AsyncMock()
    conn._poll_inbox = AsyncMock()
    await conn._do_connect()
    conn._seed_seen_messages.assert_awaited_once()
    assert conn._poll_task is not None
    await conn.disconnect()


def test_failed_save_removes_orphan_item(client, monkeypatch):
    configure(client)
    request = AsyncMock(side_effect=[
        {"link_token": "link"}, {"access_token": "private-access", "item_id": "item"},
        {"accounts": []}, {"removed": True},
    ])
    monkeypatch.setattr(oauth, "plaid_request", request)
    monkeypatch.setattr("app.core.connector_service.create_connector", AsyncMock(side_effect=RuntimeError("storage unavailable")))
    flow = client.post("/api/v1/connectors/oauth/plaid/start").json()["flow_id"]
    result = client.post("/api/v1/connectors/oauth/plaid/exchange", json={"flow_id": flow, "public_token": "public"})
    assert result.status_code == 400
    assert request.call_args.args[1] == "/item/remove"
    assert request.call_args.kwargs["access_token"] == "private-access"
    assert database.get_all_connectors() == []


def test_plaid_endpoints_require_admin(monkeypatch):
    app = FastAPI()
    app.include_router(oauth.router, prefix="/api/v1/connectors")
    request = AsyncMock()
    monkeypatch.setattr(oauth, "plaid_request", request)
    with TestClient(app, base_url="https://external.example") as client:
        assert client.get("/api/v1/connectors/oauth/plaid/status").status_code in (401, 403)
        assert client.post("/api/v1/connectors/oauth/plaid/start").status_code in (401, 403)
    request.assert_not_awaited()

