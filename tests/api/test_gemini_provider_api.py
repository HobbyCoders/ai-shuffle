"""The Google Gemini API-key row: enrollment, key check, catalog and routing.

Key values are obviously fake; no request leaves the process (MockTransport).
"""
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import model_providers as api
from app.api.auth import require_admin
from app.core import encryption, gemini_provider
from app.core.model_effort import effort_capabilities, effort_route
from app.core.provider.fluid_credentials import ProviderCredentialError, resolve_provider_credentials
from app.core.provider.harness_adapter import _interactions_url, _provider_row_kind
from app.core.usage_attribution import provider_billing
from app.db import database as db
from app.db.model_provider_slots import model_provider_slot

ROOT = "/api/v1/model-providers"
KEY = "fake-google-test-key"
PRESET_IDS = ["gemini-4-carbon", "gemini-4-argon", "gemini-3.8-flash", "gemini-3.6-flash",
              "gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]


@pytest.fixture
def client(mock_db, monkeypatch):
    monkeypatch.setattr(encryption, "_encryption_key", encryption.Fernet.generate_key())
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_admin] = lambda: None
    with TestClient(app) as client:
        yield client


def _respond(handler):
    return patch("app.core.http_client.get_http_client",
                 return_value=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def create(client, handler=lambda _: httpx.Response(200, json={"models": []}), **fields):
    with _respond(handler):
        return client.post(ROOT, json={"name": "Gemini", "provider_type": "google",
                                       "auth_token": KEY, **fields})


def test_native_options_lists_google_as_its_own_api_key_slot(client):
    options = client.get(ROOT + "/native-options").json()["providers"]
    [google] = [o for o in options if o["provider_type"] == "google"]
    assert google["slot"] == "google:key" and google["credential_kind"] == "api_key"
    assert google["auth_mode"] is None and google["key_page_url"].startswith("https://aistudio.google.com/")


def test_create_checks_the_key_against_the_free_models_list_and_routes_natively(client):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"models": []})

    response = create(client, handler)
    assert response.status_code == 200, response.text
    assert KEY not in response.text
    row = response.json()
    assert row["slot"] == "google:key" and row["auth_token_masked"] == "********"
    assert row["native_provider"]["provider_type"] == "google"
    assert row["base_url"] == gemini_provider.API_ROOT
    assert [m["id"] for m in row["models"]] == PRESET_IDS
    [check] = calls
    assert check.method == "GET" and str(check.url).startswith(gemini_provider.MODELS_URL)
    assert check.headers["x-goog-api-key"] == KEY and KEY not in str(check.url)
    assert parse_qs(urlsplit(str(check.url)).query) == {"pageSize": ["1"]}

    saved = db.get_model_provider(row["id"], include_token=True)
    assert encryption.decrypt_value(saved["auth_token"]) == KEY
    assert model_provider_slot(saved) == "google:key"
    assert _provider_row_kind("google", None, None) == "gemini"
    assert provider_billing("google", None) == "api"


async def test_credentials_and_session_client_use_the_interactions_endpoint(client):
    from app.core.config import settings
    from app.core.provider.harness_adapter import _build_provider_client
    from app.core.provider.types import SessionOptions
    from fluid.providers.gemini.client import API_URL, GeminiClient

    row = create(client).json()
    creds = await resolve_provider_credentials("gemini", row["id"])
    assert creds.api_key == KEY
    with pytest.raises(ProviderCredentialError, match="Gemini"):
        await resolve_provider_credentials("anthropic", row["id"])
    built, session_http = await _build_provider_client(
        "gemini", SessionOptions(model="gemini-3.8-flash", extra={"provider_id": row["id"]}))
    try:
        assert isinstance(built, GeminiClient)
        assert built._base_url == API_URL
        assert built._user_agent == f"ai-shuffle/{settings.version}"
    finally:
        if session_http is not None:
            await session_http.aclose()


@pytest.mark.parametrize(("base", "url"), [
    ("https://generativelanguage.googleapis.com", "https://generativelanguage.googleapis.com/v1beta/interactions"),
    ("https://proxy.example/v1beta/", "https://proxy.example/v1beta/interactions"),
    ("https://proxy.example/v1beta/interactions", "https://proxy.example/v1beta/interactions"),
])
def test_interactions_url_from_a_row_base(base, url):
    assert _interactions_url(base) == url


@pytest.mark.parametrize(("status", "body", "saved"), [
    (400, {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.",
                     "status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}}, False),
    (403, {"error": {"code": 403, "message": "Permission denied", "status": "PERMISSION_DENIED"}}, False),
    (503, {"error": {"code": 503, "message": "Unavailable", "status": "UNAVAILABLE"}}, True),
    (429, {"error": {"code": 429, "message": "Quota", "status": "RESOURCE_EXHAUSTED"}}, True),
])
def test_only_a_clear_rejection_refuses_saving(client, status, body, saved):
    response = create(client, lambda _: httpx.Response(status, json=body))
    assert response.status_code == (200 if saved else 400), response.text
    assert KEY not in response.text
    assert bool(db.get_all_model_providers()) is saved


@pytest.mark.parametrize("fields", [{"auth_mode": "plan_key"}, {"api_dialect": "anthropic"}, {"auth_token": ""}])
def test_invalid_google_rows_are_refused(client, fields):
    assert create(client, **fields).status_code == 400
    assert not db.get_all_model_providers()


def test_rows_cannot_switch_into_or_out_of_google(client):
    row = create(client).json()
    assert client.patch(f"{ROOT}/{row['id']}", json={"provider_type": "custom"}).status_code == 400
    db.create_model_provider("custom", "Custom", "custom", "https://example.com")
    assert client.patch(f"{ROOT}/custom", json={"provider_type": "google"}).status_code == 400
    assert client.patch(f"{ROOT}/{row['id']}", json={"name": "Renamed"}).status_code == 200


def test_key_replacement_is_rechecked_and_a_rejected_key_keeps_the_old_one(client):
    row = create(client).json()
    with _respond(lambda _: httpx.Response(400, text='{"error":{"message":"API key not valid"}}')):
        refused = client.patch(f"{ROOT}/{row['id']}", json={"auth_token": "fake-google-new-key"})
    assert refused.status_code == 400
    saved = db.get_model_provider(row["id"], include_token=True)
    assert encryption.decrypt_value(saved["auth_token"]) == KEY
    with _respond(lambda _: httpx.Response(200, json={"models": []})):
        checked = client.post(f"{ROOT}/{row['id']}/check-key")
    assert checked.json() == {"status": "valid"}
    with _respond(lambda _: httpx.Response(503)):
        checked = client.post(f"{ROOT}/{row['id']}/check-key")
    assert checked.json() == {"status": "unavailable"}


def test_fetch_models_pages_through_googles_list_and_keeps_text_models(client):
    row = create(client).json()
    pages = {
        None: {"models": [
            {"name": "models/gemini-4-carbon", "displayName": "Gemini 4 Carbon",
             "inputTokenLimit": 1048576, "supportedGenerationMethods": ["generateContent", "countTokens"]},
            {"name": "models/gemini-3.8-flash-image", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-005", "supportedGenerationMethods": ["embedContent"]},
        ], "nextPageToken": "p2"},
        "p2": {"models": [
            {"name": "models/gemini-3.8-flash", "displayName": "Gemini 3.8 Flash",
             "inputTokenLimit": 1048576, "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.8-flash-tts", "supportedGenerationMethods": ["generateContent"]},
        ]},
    }
    calls = []

    def handler(request):
        calls.append(request)
        token = parse_qs(urlsplit(str(request.url)).query).get("pageToken", [None])[0]
        return httpx.Response(200, json=pages[token])

    with _respond(handler):
        result = client.post(f"{ROOT}/{row['id']}/fetch-models")
    assert result.status_code == 200, result.text
    models = result.json()["models"]
    assert [m["id"] for m in models] == ["gemini-4-carbon", "gemini-3.8-flash"]
    assert models[0] == {"id": "gemini-4-carbon", "label": "Gemini 4 Carbon", "context_window": 1048576}
    assert len(calls) == 2 and all(c.headers["x-goog-api-key"] == KEY for c in calls)
    assert KEY not in result.text
    # Curation saves the fetched entries as they are.
    saved = client.patch(f"{ROOT}/{row['id']}", json={"models": models, "default_model": models[0]["id"]})
    assert saved.status_code == 200, saved.text


def test_creation_waits_for_every_member_to_advertise_gemini_support(client, monkeypatch):
    from app.db import network as store
    from app.network import engine

    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(device_id="self"))
    monkeypatch.setattr(store, "list_members", lambda **kw: [{"device_id": "peer", "name": "Older worker"}])
    monkeypatch.setattr(engine, "all_presence", lambda: {"peer": {"capabilities": {"native_messages_providers_v1": 1}}})
    refused = create(client)
    assert refused.status_code == 409 and "Older worker" in refused.text
    assert not db.get_all_model_providers()
    monkeypatch.setattr(engine, "all_presence",
                        lambda: {"peer": {"capabilities": {gemini_provider.CAPABILITY: 1}}})
    assert create(client).status_code == 200


@pytest.mark.parametrize(("model", "levels", "default"), [
    ("gemini-4-carbon", ["low", "medium", "high"], "medium"),
    ("gemini-3.6-flash", ["minimal", "low", "medium", "high"], "medium"),
    ("gemini-3.5-flash-lite", ["minimal", "low", "medium", "high"], "minimal"),
])
def test_effort_levels_follow_googles_thinking_table(model, levels, default):
    row = {"provider_type": "google"}
    assert effort_route(row) == "gemini"
    cap = effort_capabilities(model, row)
    assert cap["effort_levels"] == levels and cap["default_effort"] == default
