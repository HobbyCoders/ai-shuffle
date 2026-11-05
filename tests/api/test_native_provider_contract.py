"""Six native provider slots use the existing encrypted saved-account contract."""
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import model_providers as api
from app.api.auth import require_admin
from app.core import encryption, native_providers
from app.core.account_failover import _is_subscription_session
from app.core.automation_service import resolve_automation_model
from app.core.credential_gate import _default_candidate_rows
from app.core.provider.fluid_credentials import resolve_provider_credentials
from app.core.provider.harness_adapter import _provider_row_kind
from app.core.usage_attribution import provider_billing
from app.db import database as db
from app.db.model_provider_slots import model_provider_slot

ROOT = "/api/v1/model-providers"


@pytest.fixture
def client(mock_db, monkeypatch):
    monkeypatch.setattr(encryption, "_encryption_key", encryption.Fernet.generate_key())
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_admin] = lambda: None
    with TestClient(app) as client:
        yield client


def create(client, provider, mode, **fields):
    response = client.post(ROOT, json={"name": "Saved account", "provider_type": provider, "auth_mode": mode,
                                       "auth_token": "test-native-secret-value", **fields})
    assert response.status_code == 200, response.text
    assert "test-native-secret-value" not in response.text
    return response.json()


@pytest.mark.parametrize("provider", native_providers.PROVIDERS)
async def test_saved_accounts_slots_encryption_routing_and_automation(client, provider):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"content": []}))) as http:
        with patch("app.core.http_client.get_http_client", return_value=http):
            plan = create(client, provider, "plan_key")
            api_key = create(client, provider, "api_key")
            second = create(client, provider, "plan_key", enabled=False)
    assert plan["slot"] == f"{provider}:subscription"
    assert api_key["slot"] == f"{provider}:key"
    assert plan["auth_token_set"] and "*" in plan["auth_token_masked"]
    assert plan["native_provider"]["credential_kind"] == "subscription"
    assert not plan["native_provider"]["usage_meter_available"]
    assert client.post(f"{ROOT}/{second['id']}/activate").status_code == 200
    rows = client.get(ROOT).json()["providers"]
    assert len(rows) == 3
    assert {r["id"] for r in rows if r["enabled"]} == {api_key["id"], second["id"]}
    row = db.get_model_provider(second["id"], include_token=True)
    assert encryption.is_encrypted(row["auth_token"])
    assert encryption.decrypt_value(row["auth_token"]) == "test-native-secret-value"
    assert row["auth_mode"] == "plan_key"
    assert _provider_row_kind(provider, "plan_key", "anthropic") == "anthropic"
    with patch("app.db.database.get_session", return_value={"provider_id": second["id"]}):
        assert _is_subscription_session("test-session")
    assert provider_billing(provider, "plan_key") == "subscription"
    assert model_provider_slot(row) == f"{provider}:subscription"
    assert second["id"] not in {r["id"] for r in _default_candidate_rows(False)}
    creds = await resolve_provider_credentials("anthropic", second["id"])
    assert creds.compatible_provider == provider and creds.api_key == "test-native-secret-value"
    assert creds.base_url == native_providers.endpoint_root(provider, "plan_key")
    model = second["models"][0]["id"]
    choice = resolve_automation_model({"model": f"prov:{second['id']}:{model}"})
    assert choice["error"] is None and choice["provider_id"] == second["id"]
    changed = client.patch(f"{ROOT}/{second['id']}", json={"auth_mode": "api_key"})
    assert changed.status_code == 400


@pytest.mark.parametrize("provider", native_providers.PROVIDERS)
@pytest.mark.parametrize("mode", ["plan_key", "api_key"])
async def test_reject_invalid_key_and_do_not_use_public_catalog(client, provider, mode):
    calls = []
    def reject(request):
        calls.append(request)
        return httpx.Response(401, json={"error": {"message": "test-native-secret-value"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http:
        with patch("app.core.http_client.get_http_client", return_value=http):
            result = client.post(ROOT, json={"name": "Invalid", "provider_type": provider, "auth_mode": mode,
                                             "auth_token": "test-native-secret-value"})
    assert result.status_code == 400 and "test-native-secret-value" not in result.text
    assert calls[0].method == "POST" and str(calls[0].url).endswith("/v1/messages")
    assert not client.get(ROOT).json()["providers"]


def test_first_run_contract_and_unknown_modes(client):
    assert client.get(ROOT).json() == {"providers": []}
    options = client.get(ROOT + "/native-options").json()["providers"]
    assert len(options) == 6
    assert {o["slot"] for o in options} == {f"{p}:{k}" for p in native_providers.PROVIDERS for k in ("key", "subscription")}
    bad = client.post(ROOT, json={"name": "Bad", "provider_type": "kimi", "auth_mode": "oauth"})
    assert bad.status_code == 400


async def test_claude_subscription_guard_and_opencode_model_ids_unchanged(client):
    from types import SimpleNamespace
    from app.core.provider.fluid_credentials import ProviderCredentialError, _SUBSCRIPTION_AUTH_MODES
    from app.core.provider.harness_adapter import _native_api_model

    assert "plan_key" not in _SUBSCRIPTION_AUTH_MODES
    db.create_model_provider("claude-sub", "Claude", "anthropic", "https://api.anthropic.com",
                             auth_mode="subscription", auth_token=encryption.encrypt_value("not-an-api-key"))
    with pytest.raises(ProviderCredentialError, match="delegated"):
        await resolve_provider_credentials("anthropic", "claude-sub")
    opts = SimpleNamespace(extra={"provider_id": "oc", "provider_type": "opencode"})
    assert _native_api_model("claude-opus-4-6", opts) == "claude-opus-4-6"
    assert _provider_row_kind("chatgpt", "chatgpt", None) == "openai_responses"
    assert _provider_row_kind("xai", None, None) == "openai_responses"
    assert _provider_row_kind("anthropic", "subscription", None) == "delegated"


async def test_opencode_catalog_requires_valid_key_not_public_list(client):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"content": []}))) as http:
        with patch("app.core.http_client.get_http_client", return_value=http):
            row = create(client, "opencode", "api_key")
    calls = []
    def unavailable(request):
        calls.append(request.method)
        return httpx.Response(503)
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as http:
        with patch("app.core.http_client.get_http_client", return_value=http):
            response = client.post(f"{ROOT}/{row['id']}/fetch-models")
    assert response.status_code == 502
    assert calls == ["POST"]

@pytest.mark.parametrize("provider", native_providers.PROVIDERS)
@pytest.mark.parametrize("mode", ["plan_key", None])
async def test_catalog_urls_kinds_and_supported_rosters(client, provider, mode):
    calls = []
    def respond(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"content": []})
        return httpx.Response(200, json={"data": [{"id": m} for m in
            ("MiniMax-M3", "MiniMax-M2.7", "kimi-k3.1-pro", "some-free", "gpt-6.1-sol")]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with patch("app.core.http_client.get_http_client", return_value=http):
            row = create(client, provider, mode)
            assert row["auth_mode"] == mode
            calls.clear()
            result = client.post(f"{ROOT}/{row['id']}/fetch-models")
    assert result.status_code == 200, result.text
    expected = native_providers.catalog_url(row)
    if expected is None:
        assert not calls and result.json()["source"] == "preset"
    else:
        gets = [r for r in calls if r.method == "GET"]
        assert len(gets) == 1 and str(gets[0].url) == expected
        assert gets[0].headers["user-agent"].startswith("ai-shuffle/")
        if provider == "opencode":
            assert calls[0].method == "POST"
    ids = {m["id"] for m in result.json()["models"]}
    if provider == "minimax":
        assert ids == {"MiniMax-M3"}
    if provider == "opencode" and mode is None:
        assert "some-free" not in ids

