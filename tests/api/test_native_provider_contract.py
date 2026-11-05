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
    assert second["id"] in {r["id"] for r in _default_candidate_rows(False)}
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
    assert ("x-opencode-session" in calls[0].headers) is (provider == "opencode")
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



@pytest.mark.parametrize("provider", native_providers.PROVIDERS)
@pytest.mark.parametrize("mode", ["plan_key", "api_key"])
async def test_session_client_builds_from_saved_row(client, provider, mode):
    """Chats build their client on a different path from the key check on save."""
    from app.core.config import settings
    from app.core.provider.harness_adapter import _build_provider_client
    from app.core.provider.types import SessionOptions
    from fluid.providers.anthropic.client import AnthropicClient

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"content": []}))) as http:
        with patch("app.core.http_client.get_http_client", return_value=http):
            row = create(client, provider, mode)
    options = SessionOptions(model="any-model", extra={"provider_id": row["id"]})
    built, session_http = await _build_provider_client("anthropic", options)
    try:
        assert isinstance(built, AnthropicClient)
        assert built.name == provider
        assert built._user_agent == f"ai-shuffle/{settings.version}"
        assert not session_http.follow_redirects
    finally:
        await session_http.aclose()


@pytest.mark.parametrize("provider", native_providers.PROVIDERS)
async def test_authenticated_checks_and_catalogs_never_follow_redirects(client, provider):
    calls = []
    redirect = False

    def respond(request):
        calls.append(request)
        if redirect:
            return httpx.Response(307, headers={"location": "https://foreign.example/v1/messages"},
                                  json={"data": [{"id": "redirected-model"}]})
        return httpx.Response(200, json={"content": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as http:
        with patch("app.core.http_client.get_http_client", return_value=http):
            row = create(client, provider, "api_key")
            redirect = True
            calls.clear()
            checked = client.post(f"{ROOT}/{row['id']}/check-key")
            assert checked.status_code == 200 and checked.json()["status"] == "unavailable"
            catalog = client.post(f"{ROOT}/{row['id']}/fetch-models")
            assert catalog.status_code == 502
    assert calls
    assert all(r.url.host != "foreign.example" for r in calls)


@pytest.mark.parametrize("status,error_type,rejected", [
    (401, "permission_error", True), (400, "authentication_error", True),
    (403, "authentication_error", True), (403, "permission_error", False),
    (429, "rate_limit_error", False), (503, "overloaded_error", False),
])
async def test_key_check_rejects_only_authentication_and_patch_keeps_old_key(client, status, error_type, rejected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))) as http:
        with patch("app.core.http_client.get_http_client", return_value=http):
            row = create(client, "kimi", "plan_key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(
            status, json={"error": {"type": error_type, "message": "test-new-key"}}))) as http:
        with patch("app.core.http_client.get_http_client", return_value=http):
            updated = client.patch(f"{ROOT}/{row['id']}", json={"auth_token": "test-new-key"})
            assert updated.status_code == (400 if rejected else 200)
            if not rejected:
                checked = client.post(f"{ROOT}/{row['id']}/check-key")
                assert checked.json()["status"] == "unavailable"
    saved = db.get_model_provider(row["id"], include_token=True)
    expected = "test-native-secret-value" if rejected else "test-new-key"
    assert encryption.decrypt_value(saved["auth_token"]) == expected
    assert "test-new-key" not in updated.text


async def test_native_enrollment_refuses_safely_when_worker_check_fails(client, monkeypatch):
    from app.core import native_providers

    def lookup_failed():
        raise RuntimeError("connection to server at 10.0.0.9 failed: password for appuser")
    monkeypatch.setattr(native_providers, "workers_requiring_update", lookup_failed)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))) as http:
        with patch("app.core.http_client.get_http_client", return_value=http):
            result = client.post(ROOT, json={"name": "Native", "provider_type": "kimi", "auth_token": "fake"})
    assert result.status_code == 503, result.text
    detail = result.json()["detail"]
    assert "up to date" in detail
    assert "10.0.0.9" not in detail and "password" not in detail
    assert not db.get_all_model_providers()


@pytest.mark.parametrize("members,presence,allowed", [
    ([], {}, True),
    ([{"device_id": "self", "name": "This worker"}], {}, True),
    ([{"device_id": "peer", "name": "Updated worker", "capabilities": {}}],
     {"peer": {"capabilities": {native_providers.CAPABILITY: 1}}}, True),
    ([{"device_id": "peer", "name": "Older worker", "capabilities": {native_providers.CAPABILITY: 1}}],
     {"peer": {"capabilities": {native_providers.CAPABILITY: 0}}}, False),
    ([{"device_id": "peer", "name": "Contact only", "capabilities": {native_providers.CAPABILITY: 1}}], {}, False),
    ([{"device_id": "peer", "name": "Offline older worker", "capabilities": {}}], {}, False),
    ([{"device_id": "peer", "name": "Unknown worker"}], {}, False),
    ([{"device_id": "peer"}], {}, False),
])
async def test_native_enrollment_requires_every_current_member_capability(client, monkeypatch, members, presence, allowed):
    from types import SimpleNamespace
    from app.network import engine
    from app.db import network as store

    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(device_id="self"))
    def current_members(*, include_revoked):
        assert not include_revoked
        return members
    monkeypatch.setattr(store, "list_members", current_members)
    monkeypatch.setattr(engine, "all_presence", lambda: presence)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))) as http:
        with patch("app.core.http_client.get_http_client", return_value=http):
            result = client.post(ROOT, json={"name": "Native", "provider_type": "kimi", "auth_token": "fake"})
    assert result.status_code == (200 if allowed else 409), result.text
    if not allowed:
        assert (members[0].get("name") or "peer") in result.json()["detail"]
        assert not db.get_all_model_providers()


async def test_native_enrollment_without_network_and_existing_row_operations_stay_allowed(client, monkeypatch):
    from types import SimpleNamespace
    from app.network import engine
    from app.db import network as store

    monkeypatch.setattr(engine, "context", lambda: None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))) as http:
        with patch("app.core.http_client.get_http_client", return_value=http):
            row = create(client, "kimi", "plan_key")
    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(device_id="self"))
    monkeypatch.setattr(store, "list_members", lambda **kw: [{"device_id": "old", "name": "Older worker"}])
    assert client.patch(f"{ROOT}/{row['id']}", json={"name": "Renamed"}).status_code == 200
    assert client.post(f"{ROOT}/{row['id']}/activate").status_code == 200
    db.create_model_provider("custom", "Custom", "custom", "https://example.com")
    converted = client.patch(f"{ROOT}/custom", json={"provider_type": "opencode"})
    assert converted.status_code == 409 and "Older worker" in converted.text
    assert client.delete(f"{ROOT}/{row['id']}").status_code == 200


@pytest.mark.parametrize("servable_only", [False, True])
async def test_plan_key_is_interactive_and_background_default_without_claude(client, monkeypatch, servable_only):
    from app.core import credential_gate

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={}))) as http:
        with patch("app.core.http_client.get_http_client", return_value=http):
            row = create(client, "kimi", "plan_key")
    monkeypatch.setattr(credential_gate, "_claude_ready_for_default", lambda probe: False)
    monkeypatch.setattr(credential_gate, "_most_used_ranking", lambda: [])
    model = row["models"][0]["id"]
    assert credential_gate.default_run_model(servable_only=servable_only) == (row["id"], model)
    assert credential_gate.default_run_selection(servable_only=servable_only) == f"prov:{row['id']}:{model}"
    picked, picked_model = credential_gate.subscription_provider_model()
    assert picked["id"] == row["id"] and picked_model == model


def test_registry_skips_unknown_rows_and_capability_probe_is_safe(client):
    from app.core.model_registry import _providers_payload
    from app.core.provider.capabilities import supports_auto_permission
    from app.core.environment import ProviderConfigError

    unknown = {"id": "future", "name": "Future provider", "provider_type": "future-native",
               "models_json": [{"id": "claude-future"}], "default_model": "claude-future"}
    supported = {"id": "supported", "name": "Supported", "provider_type": "kimi",
                 "models_json": [{"id": "kimi-k3"}], "default_model": "kimi-k3"}
    with patch.object(db, "get_enabled_model_providers", return_value=[unknown, supported]), \
         patch.object(db, "list_usable_model_providers", return_value=[unknown, supported]):
        assert [r["id"] for r in _providers_payload()] == ["supported"]
    assert not supports_auto_permission("claude-future", unknown)
    with pytest.raises(ProviderConfigError):
        _provider_row_kind("future-native", None)


async def test_opencode_catalog_effort_reaches_wire_only_when_advertised(client):
    import json
    from app.core.model_effort import normalize_effort
    from app.core.provider.harness_adapter import _build_provider_client
    from app.core.provider.types import SessionOptions
    from fluid.providers.base import ProviderRequest

    models = [{"id": "claude-sonnet-5-5", "label": "Sonnet", "reasoning_capabilities": {
        "version": 1, "effort_levels": ["low", "high"], "default_effort": "low"}}]
    calls = []
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        with patch("app.core.http_client.get_http_client", return_value=http):
            saved = create(client, "opencode", "api_key", models=models)
        row = db.get_model_provider(saved["id"])
        normalized = normalize_effort("high", models[0]["id"], row, route="anthropic")
        assert normalized == "high"
        built, session_http = await _build_provider_client("anthropic", SessionOptions(
            model=models[0]["id"], extra={"provider_id": saved["id"]}))
        try:
            built._http = http
            calls.clear()
            for effort in (normalized, "max"):
                _ = [chunk async for chunk in built.stream(ProviderRequest(
                    model=models[0]["id"], messages=[], effort=effort, session_id="conversation"))]
        finally:
            await session_http.aclose()
    assert calls[0]["output_config"] == {"effort": "high"}
    assert "output_config" not in calls[1]
