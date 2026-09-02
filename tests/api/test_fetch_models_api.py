"""API tests for the provider catalog-fetch route (phase-2 Chunk D.1/D.2).

``POST /api/v1/model-providers/{id}/fetch-models`` returns (never persists) a
provider's model roster for the curation UI:

* ChatGPT/Codex providers (``auth_mode == "chatgpt"``) → the built-in preset
  roster, no network.
* Everything else → a live ``GET {base_url}/models`` fetch, parsing both the
  OpenAI ``{"data": [...]}`` shape and a bare list, capped at 2000 entries. The
  decrypted auth token rides as ``Authorization: Bearer``.

All upstream HTTP is stubbed via ``httpx.MockTransport`` — NO network. The
shared client used by the route is swapped for a MockTransport-backed one via a
monkeypatch on ``app.core.http_client.get_http_client``.
"""

import httpx
import pytest

from app.api import model_providers as mp_api
from app.core import encryption, model_catalog_presets
from app.db import database as db


ROOT = "/api/v1/model-providers"
PROVIDER_ID = "prov-fetch-1"
RAW_TOKEN = "sk-or-v1-abcdefghijklmnopqrstuvwxyz0123456789"


def _unlock(mock_db):
    encryption.set_encryption_key("test-password", db)


def _make_provider(
    *,
    provider_type="openrouter",
    base_url="https://openrouter.ai/api",
    auth_token=None,
):
    db.create_model_provider(
        provider_id=PROVIDER_ID,
        name="OpenRouter",
        provider_type=provider_type,
        base_url=base_url,
        auth_token=auth_token,
        enabled=True,
    )


def _install_mock_transport(monkeypatch, handler):
    """Point the route's shared HTTP client at a MockTransport running ``handler``.

    Returns nothing; ``handler`` receives each ``httpx.Request`` and returns an
    ``httpx.Response``. The route imports ``get_http_client`` from
    ``app.core.http_client`` at call time, so patching that name suffices.
    """
    transport = httpx.MockTransport(handler)
    mock_client = httpx.AsyncClient(transport=transport)

    from app.core import http_client
    monkeypatch.setattr(http_client, "get_http_client", lambda: mock_client)
    return mock_client


class TestUnauthenticated:
    def test_requires_admin(self, client):
        resp = client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models", json={})
        assert resp.status_code in (401, 403), resp.status_code


class TestPresetPath:
    def test_chatgpt_provider_returns_preset(self, authenticated_client, mock_db):
        _unlock(mock_db)
        _make_provider()
        # Flip the provider into ChatGPT/Codex mode (what enrollment does).
        db.update_model_provider(PROVIDER_ID, auth_mode="chatgpt")

        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "preset"
        assert body["models"] == model_catalog_presets.CODEX_PRESET_MODELS
        # Every preset entry is a well-formed {id, label}.
        for entry in body["models"]:
            assert entry["id"] and entry["label"]

    def test_preset_path_makes_no_network_call(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _make_provider()
        db.update_model_provider(PROVIDER_ID, auth_mode="chatgpt")

        def _boom(request):  # pragma: no cover — must never be reached
            raise AssertionError("preset path must not hit the network")

        _install_mock_transport(monkeypatch, _boom)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        assert resp.json()["source"] == "preset"

    def test_chatgpt_provider_type_returns_codex_preset(self, authenticated_client, mock_db):
        # New enrollments use provider_type='chatgpt' (plan PR1) — the preset
        # selector must return the Codex roster from provider_type alone, even
        # before any auth_mode bundle is persisted.
        _unlock(mock_db)
        _make_provider(provider_type="chatgpt",
                       base_url="https://chatgpt.com/backend-api/codex")

        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "preset"
        assert body["models"] == model_catalog_presets.CODEX_PRESET_MODELS

    def test_openai_provider_type_returns_openai_preset(self, authenticated_client, mock_db):
        # provider_type='openai' rows ship the public-API OpenAI roster with zero
        # user model-picking (plan D4 / ruling PR3).
        _unlock(mock_db)
        _make_provider(provider_type="openai",
                       base_url="https://api.openai.com/v1")

        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "preset"
        assert body["models"] == model_catalog_presets.OPENAI_PRESET_MODELS
        # The OpenAI roster is distinct from the Codex roster.
        assert body["models"] != model_catalog_presets.CODEX_PRESET_MODELS

    def test_xai_provider_type_returns_xai_preset(self, authenticated_client, mock_db):
        _unlock(mock_db)
        _make_provider(provider_type="xai", base_url="https://api.x.ai/v1")

        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "preset"
        assert body["models"] == model_catalog_presets.XAI_PRESET_MODELS

    def test_grok_provider_type_returns_xai_preset(self, authenticated_client, mock_db):
        _unlock(mock_db)
        _make_provider(provider_type="grok", base_url="https://api.x.ai/v1")

        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "preset"
        assert body["models"] == model_catalog_presets.XAI_PRESET_MODELS

    def test_grok_auth_mode_returns_xai_preset(self, authenticated_client, mock_db):
        _unlock(mock_db)
        _make_provider()
        db.update_model_provider(PROVIDER_ID, auth_mode="grok")

        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        assert resp.json()["models"] == model_catalog_presets.XAI_PRESET_MODELS

    def test_codex_roster_excludes_codex_auto_review(self):
        # codex-auto-review is special-purpose — must never appear in the picker.
        ids = {entry["id"] for entry in model_catalog_presets.CODEX_PRESET_MODELS}
        assert "codex-auto-review" not in ids
        # Sanity: the live-confirmed roster ids are present (spike 2026-07-18).
        assert {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
                "gpt-5.5", "gpt-5.4", "gpt-5.4-mini"} == ids


class TestUpstreamPath:
    def test_openai_shape(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(200, json={"data": [
                {"id": "openai/gpt-5.6", "name": "GPT-5.6"},
                {"id": "meta-llama/llama-3.1"},  # label falls back to id
            ]})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "upstream"
        assert body["models"] == [
            {"id": "openai/gpt-5.6", "label": "GPT-5.6"},
            {"id": "meta-llama/llama-3.1", "label": "meta-llama/llama-3.1"},
        ]

    def test_openrouter_strips_author_prefix_from_names(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(200, json={"data": [
                {"id": "anthropic/claude-fable-5.1",
                 "name": "Anthropic: Claude Fable 5.1"},
                {"id": "z-ai/glm-5.3-flash", "name": "Z.ai: GLM 5.3 Flash"},
            ]})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        assert resp.json()["models"] == [
            {"id": "anthropic/claude-fable-5.1", "label": "Claude Fable 5.1"},
            {"id": "z-ai/glm-5.3-flash", "label": "GLM 5.3 Flash"},
        ]

    def test_bare_list_shape(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(200, json=[
                {"id": "a", "label": "Model A"},
                "bare-string-id",
            ])

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == "upstream"
        assert body["models"] == [
            {"id": "a", "label": "Model A"},
            {"id": "bare-string-id", "label": "bare-string-id"},
        ]

    def test_upstream_500_maps_to_502(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(500, text="upstream boom")

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 502, resp.text

    def test_list_capped_at_2000(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        def _handler(request):
            return httpx.Response(200, json={"data": [
                {"id": f"model-{i}"} for i in range(2500)
            ]})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["models"]) == 2000

    def test_auth_header_carries_decrypted_token(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_TOKEN))

        captured = {}

        def _handler(request):
            captured["auth"] = request.headers.get("authorization")
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"data": []})

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 200, resp.text
        # The DECRYPTED plaintext token must ride the request, not the enc: blob.
        assert captured["auth"] == f"Bearer {RAW_TOKEN}"
        assert captured["url"] == "https://openrouter.ai/api/v1/models"

    def test_encryption_locked_returns_400(self, authenticated_client, mock_db, monkeypatch):
        # Provider carries a token blob, but encryption is NOT unlocked → cannot
        # decrypt → 400 (before any network call).
        encryption.clear_encryption_key()
        _make_provider(auth_token="enc:not-decryptable-while-locked")

        def _boom(request):  # pragma: no cover
            raise AssertionError("must 400 before any network call")

        _install_mock_transport(monkeypatch, _boom)
        resp = authenticated_client.post(f"{ROOT}/{PROVIDER_ID}/fetch-models")
        assert resp.status_code == 400, resp.text

    def test_provider_not_found_404(self, authenticated_client, mock_db):
        _unlock(mock_db)
        resp = authenticated_client.post(f"{ROOT}/does-not-exist/fetch-models")
        assert resp.status_code == 404, resp.text


class TestHelperParsing:
    """Direct unit coverage of the normalize helper (shape edge cases)."""

    def test_parse_skips_entries_without_id(self):
        out = mp_api._parse_models_response({"data": [
            {"label": "no id"}, {"id": "keep", "label": "Keep"}, 42,
        ]})
        assert out == [{"id": "keep", "label": "Keep"}]

    def test_parse_strips_openrouter_author_prefix(self):
        out = mp_api._parse_models_response(
            {"data": [
                {"id": "anthropic/claude-fable-5.1",
                 "name": "Anthropic: Claude Fable 5.1"},
                {"id": "z-ai/glm-5.3", "name": "Z.ai: GLM 5.3"},
                {"id": "x-ai/grok-4.6", "name": "Grok 4.6"},
            ]},
            provider_type="openrouter",
        )
        assert [m["label"] for m in out] == [
            "Claude Fable 5.1", "GLM 5.3", "Grok 4.6",
        ]

    def test_parse_keeps_author_prefix_for_non_openrouter(self):
        out = mp_api._parse_models_response(
            {"data": [{"id": "x", "name": "Anthropic: Claude Fable 5.1"}]},
            provider_type="custom",
        )
        assert out == [{"id": "x", "label": "Anthropic: Claude Fable 5.1"}]

    def test_parse_carries_through_upstream_context_length(self):
        """OpenRouter-shaped windows survive normalization as ``context_window``.
        They used to be stripped, forcing every model onto the row's single
        shared column."""
        out = mp_api._parse_models_response({"data": [
            {"id": "a", "label": "A", "context_length": 128000},
            {"id": "b", "label": "B", "top_provider": {"context_length": 1000000}},
            {"id": "c", "label": "C"},
        ]})
        assert out == [
            {"id": "a", "label": "A", "context_window": 128000},
            {"id": "b", "label": "B", "context_window": 1000000},
            {"id": "c", "label": "C"},
        ]

    def test_parse_prefers_top_provider_context_length(self):
        """``top_provider`` is the actual serving provider's window — it wins."""
        out = mp_api._parse_models_response({"data": [
            {"id": "a", "label": "A", "context_length": 128000,
             "top_provider": {"context_length": 64000}},
        ]})
        assert out == [{"id": "a", "label": "A", "context_window": 64000}]

    def test_parse_preserves_only_pricing_needed_for_cost_projection(self):
        out = mp_api._parse_models_response({"data": [{
            "id": "x-ai/grok-4.6",
            "name": "Grok 4.6",
            "pricing": {
                "prompt": "0.000001",
                "completion": "0.000002",
                "input_cache_read": "0.0000002",
                "irrelevant": "must-not-persist",
            },
        }]})
        assert out == [{
            "id": "x-ai/grok-4.6",
            "label": "Grok 4.6",
            "pricing": {
                "prompt": "0.000001",
                "completion": "0.000002",
                "input_cache_read": "0.0000002",
            },
        }]

    def test_parse_ignores_unusable_context_length(self):
        out = mp_api._parse_models_response({"data": [
            {"id": "a", "label": "A", "context_length": "huge"},
            {"id": "b", "label": "B", "context_length": 0},
            {"id": "c", "label": "C", "context_length": None},
        ]})
        assert out == [
            {"id": "a", "label": "A"},
            {"id": "b", "label": "B"},
            {"id": "c", "label": "C"},
        ]

    def test_parse_non_list_returns_empty(self):
        assert mp_api._parse_models_response({"data": "nope"}) == []
        assert mp_api._parse_models_response(123) == []
